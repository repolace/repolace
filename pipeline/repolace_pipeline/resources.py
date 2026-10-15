"""The process-level objects a task runs against, built in one place.

`cli.py` builds them once per process and the Celery worker once per prefork child; both call
`build_resources`, so the two entry points cannot drift on what a task is given (engine, GitHub
client, sandbox backend, specs, the model client and its up-front checks). `run_task` owns none of
them.
"""

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError
from repolace_gateway.errors import ConfigError, MissingProviderKey, UnpricedModelError
from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.git.workspace import task_workspace
from repolace_shared.github.client import GithubClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from verify.backends.docker import DockerBackend
from verify.spec import RepoSpec, load_specs

from repolace_pipeline.config import PipelineSettings

if TYPE_CHECKING:
    from repolace_gateway.client import LLMClient
    from repolace_gateway.config import GatewaySettings

log = structlog.get_logger()

#: The stage whose model the pipeline's agent calls; both it and its fallback are checked up front.
AGENT_STAGE = "agent"
#: The merge-conflict resolver's stage. It follows `agent` unless the config names it
#: (`repolace_gateway.config.OPTIONAL_STAGE_FALLBACKS`), so it is always routable.
RESOLVE_STAGE = "resolve"


class UsageError(Exception):
    """The invocation cannot run as asked. Carries the one line to show."""


def preflight_agent_models(client: "LLMClient") -> None:
    """Refuse a model that cannot be priced or keyed, before any task row is claimed.

    The gateway already refuses an unpriced model -- but at the first *call*, which is after the
    claim, the clone, the index, the baseline and the sandbox build. A task row left `failed` by
    a missing line in `models.toml` is a harness error in the benchmark's report, so the check
    belongs where nothing has happened yet. It runs `LLMClient.preflight`, the gateway's own
    pre-spend check (what `complete` runs), so the two cannot disagree about what "priced" means.

    Both the `agent` and the `resolve` stage are checked.
    """
    client.preflight(AGENT_STAGE)
    client.preflight(RESOLVE_STAGE)


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


@dataclass
class Resources:
    settings: PipelineSettings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    github: GithubClient
    backend: DockerBackend
    specs: dict[str, RepoSpec]
    #: None for the agents with no model (`stub`, `gold`).
    llm: "LLMClient | None"

    async def aclose(self) -> None:
        try:
            await self.github.aclose()
        finally:
            await self.engine.dispose()


async def build_resources(settings: PipelineSettings, agent_kind: str) -> Resources:
    """Everything a task runs against, for `agent_kind` (`llm`, `stub` or `gold`).

    Only `llm` builds the model client (and so checks the models). A `UsageError` from that check
    closes what was already built before it propagates, so a refused model leaks nothing.
    """
    engine = create_engine(settings.database_url)
    github = GithubClient(settings.github_app_id, settings.github_app_private_key)
    # The sandbox backend is a process-level resource, like the engine and the client.
    backend = DockerBackend()
    specs = load_specs(settings.verify_specs_path)
    session_factory = create_session_factory(engine)
    resources = Resources(settings, engine, session_factory, github, backend, specs, None)
    try:
        if agent_kind == "llm":
            # Before `run_task` claims a row: a model that cannot be priced must not leave a
            # `failed` task behind. One client per process, as the engine is.
            resources.llm = build_llm_client(session_factory)
    except BaseException:
        await resources.aclose()
        raise
    return resources


def task_options(settings: PipelineSettings) -> dict[str, Any]:
    """The settings-derived keyword arguments `run_task` gets, so the CLI and the worker agree."""
    return {
        "instances_dir": settings.instances_dir,
        "embedding_strategy": settings.embedding_strategy,
        "max_changed_files": settings.max_changed_files,
        "max_changed_lines": settings.max_changed_lines,
        "workspace_factory": functools.partial(task_workspace, parent_dir=settings.workspace_parent_dir),
    }
