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
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_gateway.errors import ConfigError, MissingProviderKey, UnpricedModelError
from repolace_shared.db.models import Task
from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.github.client import GithubClient
from repolace_shared.instances import InstanceError, load_instance_by_id
from repolace_shared.logging import configure_logging

from verify.backends.docker import DockerBackend
from verify.spec import load_specs

from repolace_agents.contracts import AgentRunner
from repolace_pipeline.agent_runner import LLMAgent
from repolace_pipeline.config import get_settings
from repolace_pipeline.errors import TaskNotClaimable, TaskNotFound
from repolace_pipeline.run import run_task
from repolace_pipeline.runners import GoldAgent

if TYPE_CHECKING:
    from repolace_gateway.client import LLMClient
    from repolace_gateway.config import GatewaySettings

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

#: The stage whose model the pipeline's agent calls; both it and its fallback are checked up front.
AGENT_STAGE = "agent"


class UsageError(Exception):
    """The invocation cannot run as asked. Carries the one line to show."""


def open_pr_allowed(agent: str, no_pr: bool) -> bool:
    """Whether this run may open a pull request at all.

    **Gold never does**, whatever else was passed: it runs the reference fix through the real
    pipeline to validate an instance, and a validation run that opened PRs would write to the
    bench repository without anyone having asked it to.
    """
    return not (no_pr or agent == "gold")


def preflight_agent_models(client: "LLMClient") -> None:
    """Refuse a model that cannot be priced or keyed, before any task row is claimed.

    The gateway already refuses an unpriced model -- but at the first *call*, which is after the
    claim, the clone, the index, the baseline and the sandbox build. A task row left `failed` by
    a missing line in `models.toml` is a harness error in the benchmark's report, so the check
    belongs where nothing has happened yet. It runs the gateway's own pre-spend check
    (`_verify_usable`, which is what `complete` runs), so the two cannot disagree about what
    "priced" means.

    `_verify_usable` and `_config` are private: the gateway has no public preflight, and writing a
    second price check here would be the drift this avoids. Requested of the gateway stream as
    `LLMClient.preflight(stage)`.
    """
    route = client._config.route(AGENT_STAGE)
    for model in (route.primary, route.fallback):
        if model is not None:
            client._verify_usable(model)


def build_llm_client(
    session_factory: async_sessionmaker[AsyncSession],
    settings: "GatewaySettings | None" = None,
    **client_kwargs: Any,
) -> "LLMClient":
    """The process's one model client, with its models checked.

    `GatewaySettings` is what reads `GATEWAY_STAGE_MODELS` (the channel the benchmark runner uses
    to point a run at another model), and `LLMClient.from_settings` is what applies it; neither is
    re-implemented here. Imported lazily because the gateway pulls LiteLLM, which `--agent stub`
    and `--agent gold` should not pay for.

    A problem with the models or their keys is a `UsageError` -- exit 2, "this invocation cannot
    run" -- and never exit 1, which means repolace broke on a task that was claimed.
    """
    from repolace_gateway.client import LLMClient
    from repolace_gateway.config import GatewaySettings

    try:
        gateway_settings = settings if settings is not None else GatewaySettings()
        client = LLMClient.from_settings(gateway_settings, session_factory, **client_kwargs)
        preflight_agent_models(client)
    except UnpricedModelError as exc:
        raise UsageError(f"--agent llm cannot run: {exc} (fix the model's [price] in gateway/models.toml)") from exc
    except MissingProviderKey as exc:
        raise UsageError(f"--agent llm cannot run: {exc}") from exc
    except (ConfigError, ValidationError, SettingsError) as exc:
        raise UsageError(
            f"--agent llm cannot run: the gateway configuration is invalid ({type(exc).__name__}: {exc}); "
            f"check gateway/models.toml and GATEWAY_STAGE_MODELS"
        ) from exc
    return client


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


async def _run(task_id: uuid.UUID, *, agent: str, no_pr: bool) -> int:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    github = GithubClient(settings.github_app_id, settings.github_app_private_key)
    # Constructed here rather than inside `run_task`, like the engine and the
    # client: the sandbox backend is a process-level resource, and Phase 2's
    # worker will build one per process and reuse it across tasks.
    backend = DockerBackend()
    specs = load_specs(settings.verify_specs_path)
    session_factory = create_session_factory(engine)
    try:
        # None is the plumbing stub, which has no model and so no client.
        runner: AgentRunner | None = None
        llm = None
        if agent == "gold":
            runner = await _gold_runner(session_factory, settings.instances_dir, task_id)
        elif agent == "llm":
            # Before `run_task` claims the row: a model that cannot be priced must not leave a
            # `failed` task behind. One client per process, as the engine is.
            llm = build_llm_client(session_factory)
            runner = LLMAgent()
        result = await run_task(
            task_id,
            session_factory,
            github,
            backend,
            specs,
            agent=runner,
            llm=llm,
            instances_dir=settings.instances_dir,
            open_pr=open_pr_allowed(agent, no_pr),
            embedding_strategy=settings.embedding_strategy,
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
        await github.aclose()
        await engine.dispose()

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("pipeline")
    return asyncio.run(_run(args.task_id, agent=args.agent, no_pr=args.no_pr))


if __name__ == "__main__":
    sys.exit(main())
