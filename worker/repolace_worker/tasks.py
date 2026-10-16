"""`repolace.run_task`: one pipeline run per message, on a persistent loop per prefork child.

Each prefork child owns one asyncio loop and one `Resources` (engine, GitHub client, sandbox
backend, specs, model client), built in `worker_process_init` and closed in
`worker_process_shutdown`. The embedder is warmed there too: per child, never in the prefork
parent (torch and fork do not mix).

Delivery is at-least-once (`acks_late`, `reject_on_worker_lost`), and a task row is never re-run:
a redelivered message finds the row no longer `queued`, `run_task` raises `TaskNotClaimable`, and
the message is acked and dropped here. Nothing in this module retries.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from celery.signals import worker_init, worker_process_init, worker_process_shutdown
from repolace_shared.logging import configure_logging

from repolace_worker.celery_app import RUN_TASK_NAME, celery_app
from repolace_worker.config import WorkerSettings, get_worker_settings
from repolace_worker.orphans import remove_stale_workspaces

log = structlog.get_logger()

#: Outcomes of `run_one`, returned for logs and tests; Celery ignores the result.
DONE = "done"
NOT_CLAIMABLE = "not_claimable"
NOT_FOUND = "not_found"
TIMED_OUT = "timed_out"
ERRORED = "errored"


@dataclass
class ChildState:
    loop: asyncio.AbstractEventLoop
    resources: Any  # repolace_pipeline.resources.Resources, imported lazily


_state: ChildState | None = None


def install_state(state: ChildState | None) -> None:
    """Set the child's state (tests do this directly; `worker_process_init` does it for real)."""
    global _state
    _state = state


async def run_one(
    task_id: uuid.UUID,
    resources: Any,
    *,
    claimed_by: str,
    bound_seconds: float,
    agent: Any = None,
    **run_task_overrides: Any,
) -> str:
    """Run one task against `resources`, never raising for anything about the task itself.

    `agent` and `run_task_overrides` are test seams (a scripted agent, a local workspace
    factory); the worker never passes them. Imports are local so the module stays light.
    """
    from repolace_pipeline.agent_runner import LLMAgent
    from repolace_pipeline.errors import TaskNotClaimable, TaskNotFound
    from repolace_pipeline.resources import task_options
    from repolace_pipeline.run import run_task
    from repolace_shared.tasks_recovery import fail_if_running, remove_task_containers

    options = {**task_options(resources.settings), **run_task_overrides}
    deadline = asyncio.timeout(bound_seconds)
    try:
        async with deadline:
            result = await run_task(
                task_id,
                resources.session_factory,
                resources.github,
                resources.backend,
                resources.specs,
                agent=agent if agent is not None else LLMAgent(),
                llm=resources.llm,
                open_pr=True,
                claimed_by=claimed_by,
                **options,
            )
    except TaskNotClaimable as exc:
        # A redelivered message, or a second runner that lost the race: ack and drop.
        log.warning("worker.task.not_claimable", task_id=str(task_id), status=exc.status)
        return NOT_CLAIMABLE
    except TaskNotFound:
        log.error("worker.task.not_found", task_id=str(task_id))
        return NOT_FOUND
    except TimeoutError:
        if not deadline.expired():
            # A TimeoutError the task raised itself (a socket, a subprocess), not our bound.
            log.exception("worker.task.error", task_id=str(task_id))
            return ERRORED
        log.error("worker.task.timeout", task_id=str(task_id), bound_seconds=bound_seconds)
        await asyncio.shield(fail_if_running(resources.session_factory, task_id, "worker: timeout"))
        await asyncio.shield(remove_task_containers(task_id))
        return TIMED_OUT
    except Exception:
        # Not retried: a retry would re-enter a row that may be half done.
        log.exception("worker.task.error", task_id=str(task_id))
        return ERRORED
    log.info("worker.task.finished", task_id=str(task_id), status=result.status.value)
    return DONE


@celery_app.task(name=RUN_TASK_NAME, bind=True, acks_late=True, reject_on_worker_lost=True)
def run_task_job(self: Any, task_id: str) -> str:
    state = _state
    if state is None:
        raise RuntimeError("worker child has no resources; worker_process_init did not run")
    settings = get_worker_settings()
    claimed_by = f"{self.request.hostname}:{os.getpid()}"
    return state.loop.run_until_complete(
        run_one(
            uuid.UUID(task_id),
            state.resources,
            claimed_by=claimed_by,
            bound_seconds=settings.task_bound_seconds,
        )
    )


#: RabbitMQ closes a channel whose delivery stays unacked past `consumer_timeout`; an `acks_late`
#: task holds its delivery for its whole run, so the broker setting must exceed the hard limit.
def required_consumer_timeout_ms(settings: WorkerSettings) -> int:
    return settings.hard_time_limit_seconds * 1000


@worker_init.connect
def _on_worker_start(**_: Any) -> None:
    """Once per worker start, in the parent. Only the task role clones, so only it sweeps clones."""
    configure_logging("worker")
    settings: WorkerSettings = get_worker_settings()
    if settings.role != "task":
        log.info("worker.start", role=settings.role)
        return
    remove_stale_workspaces(settings.workspace_parent_dir, settings.hard_time_limit_seconds)
    log.info(
        "worker.start",
        role=settings.role,
        task_time_limit_seconds=settings.hard_time_limit_seconds,
        required_rabbitmq_consumer_timeout_ms=required_consumer_timeout_ms(settings),
        note="the broker's consumer_timeout must exceed task_time_limit or acks_late tasks are killed",
    )


@worker_process_init.connect
def _init_child(**_: Any) -> None:
    configure_logging("worker")
    if get_worker_settings().role != "task":
        # The maintenance worker needs the database and docker, nothing else: no model client,
        # no provider keys, no torch.
        return
    from repolace_pipeline.config import get_settings
    from repolace_pipeline.resources import build_resources
    from retrieval.embed import get_embedder

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        resources = loop.run_until_complete(build_resources(get_settings(), "llm"))
        get_embedder()
    except BaseException:
        # A misconfigured model or key: the child dies loudly and unacked messages stay queued.
        log.critical("worker.child.init_failed", exc_info=True)
        raise
    install_state(ChildState(loop, resources))
    log.info("worker.child.ready", pid=os.getpid())


@worker_process_shutdown.connect
def _close_child(**_: Any) -> None:
    state = _state
    if state is None:
        return
    try:
        state.loop.run_until_complete(state.resources.aclose())
    finally:
        state.loop.close()
        install_state(None)
