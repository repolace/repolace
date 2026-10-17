"""Command-line entry point for running one task.

This module owns the process-level resources -- settings, engine, HTTP client,
logging, exit codes -- and `run_task` owns none of them. That split is the whole
reason the Phase 2 move to Celery is a transplant rather than a rewrite: the
worker will supply the same three arguments from its own long-lived objects.

Runs on the host against the containerised Postgres, the same way alembic
already does. Not inside the `worker` container: that image has no bind mount
(code is baked in) and its compose service has no `env_file`, so it cannot see
the GitHub App credentials at all.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import structlog
from repolace_agents.contracts import AgentRunner
from repolace_shared.db.models import Task
from repolace_shared.instances import InstanceError, load_instance_by_id
from repolace_shared.logging import configure_logging
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_pipeline.agent_runner import LLMAgent
from repolace_pipeline.config import get_settings
from repolace_pipeline.errors import TaskNotClaimable, TaskNotFound
from repolace_pipeline.resources import (
    AGENT_STAGE,
    Resources,
    UsageError,
    build_llm_client,
    build_resources,
    preflight_agent_models,
    task_options,
)
from repolace_pipeline.run import run_task
from repolace_pipeline.runners import GoldAgent

__all__ = ["AGENT_STAGE", "UsageError", "build_llm_client", "preflight_agent_models"]

log = structlog.get_logger()

EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_NOT_FOUND = 2
EXIT_NOT_CLAIMABLE = 3
#: A command that cannot run as asked (a model that cannot be priced, gold on a task with no instance).
#: The same number as `EXIT_NOT_FOUND` and as argparse's own usage error: all three mean "this
#: invocation was wrong", never "repolace broke", which is the one meaning exit 1 keeps.
EXIT_USAGE = 2

AGENTS = ("llm", "stub", "gold")
#: A bare `repolace-run-task <id>` runs the real agent. `stub` (the plumbing smoke test, no model)
#: and `gold` (a benchmark instance's reference fix) are opt-in.
DEFAULT_AGENT = "llm"

def open_pr_allowed(agent: str, no_pr: bool) -> bool:
    """Whether this run may open a pull request at all.

    **Gold never does**, whatever else was passed: it runs the reference fix through the real
    pipeline to validate an instance, and a validation run that opened PRs would write to the
    bench repository without anyone having asked it to.
    """
    return not (no_pr or agent == "gold")


async def _gold_runner(
    session_factory: async_sessionmaker[AsyncSession], instances_dir: Path, task_id: uuid.UUID
) -> GoldAgent:
    """The gold runner for a task, built from the instance the task names.

    Read before `run_task` claims the row, because the runner needs the instance and `run_task`
    is what loads it. Loading twice is harmless: the file is operator data and read-only.
    """
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise TaskNotFound(task_id)
        instance_id = task.instance_id
    if instance_id is None:
        raise UsageError(f"--agent gold needs a benchmark task, and task {task_id} has no instance_id")
    try:
        return GoldAgent(load_instance_by_id(instances_dir, instance_id))
    except InstanceError as exc:
        raise UsageError(f"--agent gold cannot load instance {instance_id!r}: {exc}") from exc


async def _run(task_id: uuid.UUID, *, agent: str, no_pr: bool, roles: bool = False) -> int:
    settings = get_settings()
    resources: Resources | None = None
    try:
        resources = await build_resources(settings, agent, roles=roles)
        # None is the plumbing stub, which has no model and so no client.
        runner: AgentRunner | None = None
        if agent == "gold":
            runner = await _gold_runner(resources.session_factory, settings.instances_dir, task_id)
        elif agent == "llm":
            runner = LLMAgent(roles=roles)
        result = await run_task(
            task_id,
            resources.session_factory,
            resources.github,
            resources.backend,
            resources.specs,
            agent=runner,
            llm=resources.llm,
            open_pr=open_pr_allowed(agent, no_pr),
            **task_options(settings),
        )
    except UsageError as exc:
        log.error("pipeline.cli.usage", error=str(exc))
        print(f"repolace-run-task: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except TaskNotFound:
        log.error("pipeline.task.not_found", task_id=str(task_id))
        return EXIT_NOT_FOUND
    except TaskNotClaimable as exc:
        # Expected when re-running a finished task, or when a second runner
        # races for the same row. Not an error in the task.
        log.error("pipeline.task.not_claimable", task_id=str(task_id), status=exc.status)
        return EXIT_NOT_CLAIMABLE
    finally:
        if resources is not None:
            await resources.aclose()

    # The exit code reports whether *repolace* worked, not whether the issue
    # was fixed. A task that ran to the end and scored FAILED is a successful
    # run of the pipeline with a negative result, and a benchmark harness that
    # treated it as a crash would be unable to tell the two apart. The outcome
    # is on the task row and in `pipeline.score`.
    return EXIT_OK if result.error_message is None else EXIT_TASK_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repolace-run-task",
        description="Run one repolace task end to end: clone, index, retrieve, edit, push, open a PR.",
    )
    parser.add_argument("task_id", type=uuid.UUID, help="the task's UUID")
    parser.add_argument(
        "--agent",
        choices=AGENTS,
        default=DEFAULT_AGENT,
        help="who makes the edit: the LLM agent, the deterministic plumbing stub, or the gold runner "
        "(applies a benchmark instance's reference fix, and never opens a PR)",
    )
    parser.add_argument(
        "--no-pr",
        action="store_true",
        help="run everything but never open a pull request, whatever the gate would decide",
    )
    parser.add_argument(
        "--roles",
        action="store_true",
        help="with --agent llm: run the Phase 2 planner/editor/debugger/reviewer split instead of the "
        "single agent (off by default and unbenchmarked)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.roles and args.agent != "llm":
        # The stub and the gold runner call no model, so `--roles` would be silently ignored.
        parser.error("--roles needs --agent llm")
    configure_logging("pipeline")
    # `roles` is passed only when it is set, so a plain run calls `_run` exactly as before.
    extra = {"roles": True} if args.roles else {}
    return asyncio.run(_run(args.task_id, agent=args.agent, no_pr=args.no_pr, **extra))


if __name__ == "__main__":
    sys.exit(main())
