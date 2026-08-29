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

import structlog

from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.github.client import GithubClient
from repolace_shared.logging import configure_logging

from verify.backends.docker import DockerBackend
from verify.spec import load_specs

from repolace_pipeline.config import get_settings
from repolace_pipeline.errors import TaskNotClaimable, TaskNotFound
from repolace_pipeline.run import run_task

log = structlog.get_logger()

EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_NOT_FOUND = 2
EXIT_NOT_CLAIMABLE = 3


async def _run(task_id: uuid.UUID) -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    github = GithubClient(settings.github_app_id, settings.github_app_private_key)
    # Constructed here rather than inside `run_task`, like the engine and the
    # client: the sandbox backend is a process-level resource, and Phase 2's
    # worker will build one per process and reuse it across tasks.
    backend = DockerBackend()
    specs = load_specs(settings.verify_specs_path)
    try:
        result = await run_task(
            task_id, create_session_factory(engine), github, backend, specs
        )
    except TaskNotFound:
        log.error("pipeline.task.not_found", task_id=str(task_id))
        return EXIT_NOT_FOUND
    except TaskNotClaimable as exc:
        # Expected when re-running a finished task, or when a second runner
        # races for the same row. Not an error in the task.
        log.error("pipeline.task.not_claimable", task_id=str(task_id), status=exc.status)
        return EXIT_NOT_CLAIMABLE
    finally:
        await github.aclose()
        await engine.dispose()

    # The exit code reports whether *repolace* worked, not whether the issue
    # was fixed. A task that ran to the end and scored FAILED is a successful
    # run of the pipeline with a negative result, and a benchmark harness that
    # treated it as a crash would be unable to tell the two apart. The outcome
    # is on the task row and in `pipeline.score`.
    return EXIT_OK if result.error_message is None else EXIT_TASK_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="repolace-run-task",
        description="Run one repolace task end to end: clone, index, retrieve, edit, push, open a PR.",
    )
    parser.add_argument("task_id", type=uuid.UUID, help="the task's UUID")
    args = parser.parse_args(argv)

    configure_logging("pipeline")
    return asyncio.run(_run(args.task_id))


if __name__ == "__main__":
    sys.exit(main())
