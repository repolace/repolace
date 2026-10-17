"""The pipeline's side of the Phase 2 roles: the flag, the runner, and the up-front model checks.

The roles are off unless `repolace-run-task --roles` asks for them. With them on, every stage the
roles call (`agent`, `resolve`, `plan`, `review`) must be checked before a task is claimed, the
runner must still go through `run_agent`, and the only thing that changes in the deps it hands
over is `limits.roles`.
"""

import dataclasses
import uuid
from types import SimpleNamespace

import pytest
from pipeline_support import make_deps
from repolace_agents import roles as agent_roles
from repolace_agents.contracts import AgentLimits, AgentResult, StopReason
from repolace_gateway.client import LLMClient
from repolace_gateway.errors import UnpricedModelError
from repolace_pipeline import agent_runner, cli, finalize, resources
from repolace_pipeline.agent_runner import LLMAgent
from repolace_pipeline.run import RunResult
from repolace_shared.db.models import TaskStatus
from test_pipeline_cli import TASK, Recorder


class FakeClient:
    def __init__(self, refuse: str | None = None) -> None:
        self.stages: list[str] = []
        self.refuse = refuse

    def preflight(self, stage: str) -> None:
        self.stages.append(stage)
        if stage == self.refuse:
            raise UnpricedModelError(f"stage {stage!r} names a model with no price")


class TestTheFlag:
    def test_roles_is_off_by_default(self):
        assert cli.build_parser().parse_args([TASK]).roles is False

    def test_roles_parses_with_the_llm_agent(self):
        assert cli.build_parser().parse_args([TASK, "--agent", "llm", "--roles"]).roles is True

    @pytest.mark.parametrize("agent", ["stub", "gold"])
    def test_roles_without_a_model_is_a_usage_error(self, agent, monkeypatch, capsys):
        monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)

        with pytest.raises(SystemExit) as caught:
            cli.main([TASK, "--agent", agent, "--roles"])

        assert caught.value.code == 2 and "--roles needs --agent llm" in capsys.readouterr().err

    def test_main_hands_roles_to_run(self, monkeypatch):
        seen = {}

        async def fake_run(task_id, *, agent, no_pr, roles=False):
            seen.update(agent=agent, no_pr=no_pr, roles=roles)
            return 0

        monkeypatch.setattr(cli, "_run", fake_run)
        monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)

        assert cli.main([TASK, "--roles"]) == 0
        assert seen == {"agent": "llm", "no_pr": False, "roles": True}


    def test_what_the_eval_runner_builds_for_a_roles_run_parses(self):
        """Reads the real builder, so the two cannot drift apart. Skipped where the harness is not installed."""
        runner = pytest.importorskip("harness.runner")
        config = SimpleNamespace(agent="llm", open_pr=True, roles=True)

        argv = runner.child_argv(("python", "-m", "repolace_pipeline.cli"), uuid.UUID(TASK), config)

        assert cli.build_parser().parse_args(argv[3:]).roles is True


class TestTheRunner:
    @pytest.fixture
    def captured(self, monkeypatch):
        seen = []

        async def fake_run_agent(deps):
            seen.append(deps)
            return AgentResult(stop_reason=StopReason.SUBMITTED, summary=None, attempts=0, steps=0, last_attempt=None)

        monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
        return seen

    @pytest.mark.anyio
    async def test_with_roles_it_hands_run_agent_the_same_deps_with_roles_on(self, captured, tmp_path):
        deps = make_deps(tmp_path, llm=object(), tools=object())

        await LLMAgent(roles=True)(deps)

        (handed,) = captured
        assert handed.limits == dataclasses.replace(AgentLimits(), roles=True)
        assert dataclasses.replace(handed, limits=deps.limits) == deps, "nothing else changed"

    @pytest.mark.anyio
    async def test_without_roles_the_deps_are_handed_on_untouched(self, captured, tmp_path):
        deps = make_deps(tmp_path, llm=object(), tools=object())

        await LLMAgent()(deps)

        assert captured == [deps] and captured[0].limits.roles is False

    def test_the_default_runner_is_the_single_agent(self):
        assert LLMAgent().roles is False and LLMAgent() == LLMAgent(roles=False)


class TestTheRolesPreflight:
    @pytest.mark.anyio
    async def test_the_cli_path_with_roles_preflights_every_stage_the_roles_use(self, monkeypatch):
        client = FakeClient()
        real_build = resources.build_llm_client
        recorder = Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.COMPLETED))
        monkeypatch.setattr(resources, "build_llm_client", real_build)  # undo the recorder's stub
        monkeypatch.setattr(LLMClient, "from_settings", staticmethod(lambda *a, **k: client))
        monkeypatch.setattr("repolace_gateway.config.GatewaySettings", object)

        assert await cli._run(uuid.UUID(TASK), agent="llm", no_pr=False, roles=True) == 0

        assert client.stages == ["agent", "resolve", "plan", "review"]
        assert recorder.run_task_kwargs["agent"] == LLMAgent(roles=True)

    def test_without_roles_only_agent_and_resolve_are_checked(self):
        client = FakeClient()

        resources.preflight_agent_models(client)

        assert client.stages == ["agent", "resolve"]

    @pytest.mark.parametrize("stage", ["plan", "review"])
    def test_a_role_stage_that_cannot_be_priced_is_a_usage_error_before_any_claim(self, stage, monkeypatch):
        monkeypatch.setattr(LLMClient, "from_settings", staticmethod(lambda *a, **k: FakeClient(refuse=stage)))
        monkeypatch.setattr("repolace_gateway.config.GatewaySettings", object)

        with pytest.raises(resources.UsageError, match="gateway/models.toml"):
            resources.build_llm_client("factory", roles=True)

    @pytest.mark.parametrize("stage", ["plan", "review"])
    def test_without_roles_an_unpriced_role_stage_does_not_stop_a_single_agent_run(self, stage, monkeypatch):
        monkeypatch.setattr(LLMClient, "from_settings", staticmethod(lambda *a, **k: FakeClient(refuse=stage)))
        monkeypatch.setattr("repolace_gateway.config.GatewaySettings", object)

        assert isinstance(resources.build_llm_client("factory"), FakeClient)

    def test_the_stage_names_are_the_ones_the_graph_calls(self):
        assert (resources.AGENT_STAGE, resources.PLAN_STAGE, resources.REVIEW_STAGE) == (
            agent_roles.EDIT_STAGE, agent_roles.PLAN_STAGE, agent_roles.REVIEW_STAGE
        )
        assert finalize.EDITOR_STAGE == agent_roles.EDIT_STAGE
