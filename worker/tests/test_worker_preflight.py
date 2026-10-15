"""The worker builds its resources through the same function as the CLI, and preflights both stages."""

from types import SimpleNamespace

import pytest
from repolace_gateway.client import LLMClient
from repolace_gateway.errors import ConfigError
from repolace_pipeline import resources

pytestmark = pytest.mark.anyio


class FakeClient:
    def __init__(self, known=("agent", "resolve")):
        self.known, self.stages = known, []

    def preflight(self, stage):
        self.stages.append(stage)
        if stage not in self.known:
            raise ConfigError(f"unknown stage {stage!r}; configured: {sorted(self.known)}")


def install(monkeypatch, client):
    monkeypatch.setattr(LLMClient, "from_settings", staticmethod(lambda *a, **k: client))
    monkeypatch.setattr("repolace_gateway.config.GatewaySettings", object)
    closed = SimpleNamespace(github=False, engine=False)

    class Engine:
        async def dispose(self):
            closed.engine = True

    class Github:
        async def aclose(self):
            closed.github = True

    monkeypatch.setattr(resources, "create_engine", lambda url: Engine())
    monkeypatch.setattr(resources, "create_session_factory", lambda engine: "factory")
    monkeypatch.setattr(resources, "GithubClient", lambda *a: Github())
    monkeypatch.setattr(resources, "DockerBackend", lambda: "backend")
    monkeypatch.setattr(resources, "load_specs", lambda path: {})
    return closed


SETTINGS = SimpleNamespace(
    database_url="x", github_app_id="1", github_app_private_key="k", verify_specs_path="s.toml"
)


async def test_the_worker_preflights_the_agent_and_resolve_stages(monkeypatch):
    client = FakeClient()
    install(monkeypatch, client)

    built = await resources.build_resources(SETTINGS, "llm")

    assert client.stages == ["agent", "resolve"]
    assert built.llm is client


async def test_a_config_without_the_resolve_stage_is_tolerated(monkeypatch):
    # The `resolve` stage lands with feat/conflict-resolver; until then its absence is not an error.
    client = FakeClient(known=("agent",))
    install(monkeypatch, client)

    built = await resources.build_resources(SETTINGS, "llm")

    assert client.stages == ["agent", "resolve"]
    assert built.llm is client


async def test_any_other_config_error_on_resolve_is_not_swallowed(monkeypatch):
    class Bad(FakeClient):
        def preflight(self, stage):
            if stage == "resolve":
                raise ConfigError("stage 'resolve' names a model with no price")

    install(monkeypatch, Bad())

    with pytest.raises(resources.UsageError):
        await resources.build_resources(SETTINGS, "llm")


async def test_a_refused_model_closes_what_was_built(monkeypatch):
    closed = install(monkeypatch, FakeClient(known=()))

    with pytest.raises(resources.UsageError):
        await resources.build_resources(SETTINGS, "llm")

    assert closed.github and closed.engine


async def test_no_model_agents_build_no_client(monkeypatch):
    client = FakeClient()
    install(monkeypatch, client)

    built = await resources.build_resources(SETTINGS, "stub")

    assert built.llm is None and client.stages == []
