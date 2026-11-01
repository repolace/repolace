"""Settling a task whose runner is gone, shared by the eval runner and the worker's reaper.

Every status write here is **conditional on `status = 'running'`**. A task row is never run from
scratch twice (`task_test_runs` is unique on `(task_id, attempt)`), so there is deliberately no function
here that sets QUEUED. What a stopped row may become is FAILED, or -- only when it stopped for
**unavailability** (a dead process, an operator's stop) *and* has a checkpoint -- PAUSED, which a resumer
claims and continues from the checkpoint (`settle_running`). A row the agent or model ended is never paused.
"""

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import Update, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_shared.db.models import STOP_DESIGN, STOP_UNAVAILABLE, Task, TaskCheckpoint, TaskStatus
from repolace_shared.process import ProcessResult, run_process

log = structlog.get_logger()

SessionFactory = async_sessionmaker[AsyncSession]

ProcessRunner = Callable[..., Awaitable[ProcessResult]]

_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")


@dataclass(frozen=True)
class TaskRef:
    task_id: uuid.UUID
    instance_id: str | None
    run_index: int | None


UNAVAILABLE = STOP_UNAVAILABLE
DESIGN = STOP_DESIGN

#: How many times a task may be continued before a pause becomes a failure, for product and benchmark rows alike.
DEFAULT_MAX_RESUMES = 10
#: What an `error_message` starts with when a resume was refused (the target branch moved, other code or another
#: model). Such a row is FAILED with its checkpoint kept, and `reopen_refused` is how an operator who has undone
#: the cause puts it back to `paused`.
RESUME_REFUSED_PREFIX = "resume refused: "


async def fail_if_running(
    factory: SessionFactory, task_id: uuid.UUID, message: str, *, stop_class: str | None = None
) -> bool:
    """RUNNING -> FAILED with `message`, only if the row is still RUNNING.

    Conditional so a task that finished in the same instant keeps its real
    status. Returns whether this call changed the row. `stop_class` is recorded when given.
    """
    async with factory() as session:
        result = await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.RUNNING)
            .values(
                status=TaskStatus.FAILED, error_message=message, completed_at=func.now(), stop_class=stop_class
            )
        )
        await session.commit()
        return result.rowcount == 1


async def has_checkpoint(factory: SessionFactory, task_id: uuid.UUID) -> bool:
    async with factory() as session:
        found = await session.scalar(select(exists().where(TaskCheckpoint.task_id == task_id)))
        return bool(found)


def pause_statement(
    task_id: uuid.UUID,
    reason: str,
    *,
    conditions: Sequence[Any] = (),
    made_progress: bool = True,
    claimed_by: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
) -> Update:
    """The one UPDATE that makes a row `paused`; `settle_running`, `settle_if_heartbeat_stale` and `write_paused` use it.

    Only a `running` row, and only one that has a checkpoint (the existence test is part of the statement, so a
    row cannot be paused over a checkpoint that is not there). `claimed_by`, when given, fences it to the process
    that owns the row. `paused_at` always restarts (it is the gate a resume claim waits on); `updated_at` is
    stamped only when the run `made_progress`, so a resume that stopped again before the agent ran does not
    postpone the pause's expiry.
    """
    statement = update(Task).where(
        Task.id == task_id,
        Task.status == TaskStatus.RUNNING,
        exists().where(TaskCheckpoint.task_id == Task.id),
        *conditions,
    )
    if claimed_by is not None:
        statement = statement.where(Task.claimed_by == claimed_by)
    values: dict[str, Any] = {
        "status": TaskStatus.PAUSED,
        "paused_reason": reason,
        "paused_at": func.clock_timestamp(),
        "stop_class": UNAVAILABLE,
        "updated_at": func.clock_timestamp() if made_progress else Task.updated_at,
    }
    if pr_number is not None:
        values["pr_number"] = pr_number
    if pr_url is not None:
        values["pr_url"] = pr_url
    return statement.values(**values)


async def settle_running(
    factory: SessionFactory, task_id: uuid.UUID, message: str, *, stop_class: str | None
) -> TaskStatus | None:
    """RUNNING -> PAUSED when the stop was unavailability and a checkpoint exists, else RUNNING -> FAILED.

    The one place a row somebody else stopped (the eval runner killing a child, a reaper finding a dead
    heartbeat) is settled. PAUSED needs both facts: the class says waiting can help, the checkpoint says
    there is something to continue from. Anything else is FAILED, exactly as before. Returns the status this
    call wrote, or None if the row was no longer RUNNING.
    """
    if stop_class == UNAVAILABLE:
        async with factory() as session:
            paused = await session.execute(pause_statement(task_id, message).returning(Task.id))
            wrote = paused.scalar_one_or_none() is not None
            await session.commit()
        if wrote:
            return TaskStatus.PAUSED
    if await fail_if_running(factory, task_id, message, stop_class=stop_class):
        return TaskStatus.FAILED
    return None


async def expire_paused(
    factory: SessionFactory, *, max_age: timedelta, max_resumes: int, message: str
) -> list[uuid.UUID]:
    """PAUSED -> FAILED for **product** rows that waited too long or were resumed too often. Returns the ids.

    A pause is not a place to leave a task forever: the provider may never come back, and a resume that
    keeps stopping at once is not making progress. The age is measured from the last progress (`updated_at`).
    Benchmark rows (`eval_run_id` set) are not touched here: their runner is not always running, and a sweep
    that is stopped for a week must not lose its paused rows; they get the resume cap on the eval path
    (`fail_exhausted_pauses`).
    """
    async with factory() as session:
        result = await session.execute(
            update(Task)
            .where(
                Task.status == TaskStatus.PAUSED,
                Task.eval_run_id.is_(None),
                or_(Task.updated_at < func.now() - max_age, Task.resume_count >= max_resumes),
            )
            .values(status=TaskStatus.FAILED, error_message=message, paused_reason=None, completed_at=func.now())
            .returning(Task.id)
        )
        failed = list(result.scalars())
        await session.commit()
        return failed


async def fail_exhausted_pauses(
    factory: SessionFactory, eval_run_id: str, *, max_resumes: int, message: str
) -> list[uuid.UUID]:
    """PAUSED -> FAILED for a benchmark run's rows already resumed `max_resumes` times. No age limit.

    Called by the eval runner before it dispatches: a child that keeps dying (an OOM kill, a crash) pauses its
    row every time, and without a cap that row would be dispatched, die and pause for ever.
    """
    async with factory() as session:
        result = await session.execute(
            update(Task)
            .where(Task.eval_run_id == eval_run_id, Task.status == TaskStatus.PAUSED, Task.resume_count >= max_resumes)
            .values(status=TaskStatus.FAILED, error_message=message, paused_reason=None, completed_at=func.now())
            .returning(Task.id)
        )
        failed = list(result.scalars())
        await session.commit()
        return failed


async def reopen_refused(factory: SessionFactory, task_id: uuid.UUID) -> bool:
    """FAILED -> PAUSED for a row whose resume was refused, if its checkpoint is still there.

    The way back for a refusal the operator has undone (the code or model put back, the settings restored): the
    row failed cleanly with its checkpoint kept and its `resume_count` not spent, so reopening it makes the next
    dispatch a resume again. Refuses a row that failed for any other reason.
    """
    async with factory() as session:
        result = await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.FAILED,
                Task.error_message.startswith(RESUME_REFUSED_PREFIX),
                exists().where(TaskCheckpoint.task_id == Task.id),
            )
            .values(
                status=TaskStatus.PAUSED,
                paused_reason="reopened by an operator after a refused resume",
                paused_at=func.clock_timestamp(),
                stop_class=UNAVAILABLE,
                error_message=None,
                completed_at=None,
                updated_at=func.clock_timestamp(),
            )
            .returning(Task.id)
        )
        reopened = result.scalar_one_or_none() is not None
        await session.commit()
        return reopened


async def running_older_than(
    factory: SessionFactory, age: timedelta, *, eval_run_id: str | None = None
) -> list[TaskRef]:
    """RUNNING tasks that started more than `age` ago, by the database's clock.

    `eval_run_id` restricts the search to one benchmark run; None looks at every row. A RUNNING
    row with no `started_at` should not exist; it is judged by `created_at` rather than
    skipped, so it cannot hide.
    """
    query = select(Task.id, Task.instance_id, Task.run_index).where(
        Task.status == TaskStatus.RUNNING,
        func.coalesce(Task.started_at, Task.created_at) < func.now() - age,
    )
    if eval_run_id is not None:
        query = query.where(Task.eval_run_id == eval_run_id)
    async with factory() as session:
        rows = await session.execute(query.order_by(Task.run_index, Task.instance_id, Task.id))
        return [TaskRef(task_id, instance_id, run_index) for task_id, instance_id, run_index in rows]


async def running_with_stale_heartbeat(
    factory: SessionFactory, older_than: timedelta, *, eval_run_id: str | None = None
) -> list[TaskRef]:
    """RUNNING tasks whose heartbeat is older than `older_than`, by the database's clock.

    A row that has never beaten (`heartbeat_at` NULL) is judged by `started_at` instead, so a
    runner that died before its first beat is still found, and a row that has only just been
    claimed is not.
    """
    cutoff = func.now() - older_than
    query = select(Task.id, Task.instance_id, Task.run_index).where(
        Task.status == TaskStatus.RUNNING,
        or_(
            Task.heartbeat_at < cutoff,
            (Task.heartbeat_at.is_(None)) & (func.coalesce(Task.started_at, Task.created_at) < cutoff),
        ),
    )
    if eval_run_id is not None:
        query = query.where(Task.eval_run_id == eval_run_id)
    async with factory() as session:
        rows = await session.execute(query.order_by(Task.created_at, Task.id))
        return [TaskRef(task_id, instance_id, run_index) for task_id, instance_id, run_index in rows]


async def fail_if_heartbeat_stale(
    factory: SessionFactory,
    task_id: uuid.UUID,
    message: str,
    older_than: timedelta,
    *,
    stop_class: str | None = None,
) -> bool:
    """RUNNING -> FAILED only if the row is *still* running **and still stale** at write time.

    The staleness test is part of the UPDATE's own WHERE clause, so a heartbeat that lands between
    a reaper's SELECT and this write leaves the row running. Returns whether this call failed it
    (decided by RETURNING, not by a count that could include another writer's change).
    """
    cutoff = func.now() - older_than
    async with factory() as session:
        result = await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                or_(
                    Task.heartbeat_at < cutoff,
                    Task.heartbeat_at.is_(None) & (func.coalesce(Task.started_at, Task.created_at) < cutoff),
                ),
            )
            .values(
                status=TaskStatus.FAILED, error_message=message, completed_at=func.now(), stop_class=stop_class
            )
            .returning(Task.id)
        )
        failed = result.scalar_one_or_none() is not None
        await session.commit()
        return failed


async def settle_if_heartbeat_stale(
    factory: SessionFactory, task_id: uuid.UUID, message: str, older_than: timedelta
) -> TaskStatus | None:
    """A dead runner's row: PAUSED if it left a checkpoint, else FAILED -- only if still running **and still stale**.

    A process that stopped beating died (or was killed), which is unavailability by definition. The staleness
    test is part of each UPDATE, as in `fail_if_heartbeat_stale`.
    """
    cutoff = func.now() - older_than
    stale = or_(
        Task.heartbeat_at < cutoff,
        Task.heartbeat_at.is_(None) & (func.coalesce(Task.started_at, Task.created_at) < cutoff),
    )
    async with factory() as session:
        paused = await session.execute(pause_statement(task_id, message, conditions=[stale]).returning(Task.id))
        wrote = paused.scalar_one_or_none() is not None
        await session.commit()
    if wrote:
        return TaskStatus.PAUSED
    if await fail_if_heartbeat_stale(factory, task_id, message, older_than, stop_class=UNAVAILABLE):
        return TaskStatus.FAILED
    return None


async def remove_task_containers(
    task_id: uuid.UUID,
    *,
    process_runner: ProcessRunner = run_process,
    docker: str = "docker",
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """`docker ps -q --filter name=repolace-<12 hex>` then `docker rm -f` for any survivors.

    Needed because the sandbox starts containers through a `docker run` client
    that the pipeline launches in its own session: killing the child's process
    group does not reach it, and the container belongs to the daemon anyway.
    Best effort -- a missing docker binary or a daemon that does not answer is
    logged, not raised, since the row has already been settled.
    """
    prefix = f"repolace-{task_id.hex[:12]}"
    try:
        listed = await process_runner(docker, "ps", "-q", "--filter", f"name=^{prefix}-", timeout=timeout, env=env)
        if listed.timed_out or listed.returncode != 0:
            log.error("runner.docker.ps_failed", prefix=prefix, returncode=listed.returncode)
            return ()
        ids = tuple(listed.stdout.decode("utf-8", errors="replace").split())
        # `docker ps -q` prints hex ids; anything else is not passed to `rm`.
        ids = tuple(item for item in ids if _CONTAINER_ID.fullmatch(item))
        if ids:
            await process_runner(docker, "rm", "-f", *ids, timeout=timeout, env=env)
    except OSError as exc:
        log.error("runner.docker.unavailable", prefix=prefix, error=str(exc))
        return ()
    return ids
