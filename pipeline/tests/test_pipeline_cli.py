"""The command line: what it refuses, what it passes to `run_task`, and the exit codes.

The exit code is a contract with the benchmark runner, which treats 1 as "repolace broke" and
anything else as the pipeline having worked. So the tests that matter most are the ones that pin
which situation gets which number, and that an agent-caused stop does not get 1.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import repolace_pipeline.cli as cli
from repolace_gateway.config import GatewaySettings
from repolace_shared.db.models import TaskStatus
from repolace_shared.instances import dump_instance

from repolace_pipeline.agent_runner import LLMAgent
from repolace_pipeline.errors import TaskNotClaimable, TaskNotFound
from repolace_pipeline.run import RunResult
from repolace_pipeline.runners import GoldAgent

from pipeline_support import FakeGithubClient, make_instance, seed_task

TASK = "2f8a1c4e-0000-4000-8000-000000000001"


class TestArguments:
    def test_the_agent_defaults_to_the_llm(self):
        """The bare command is the real agent; the stub and gold runners are opt-in."""
        args = cli.build_parser().parse_args([TASK])

        assert args.agent == "llm"
        assert cli.DEFAULT_AGENT == "llm"
        assert args.no_pr is False
        assert args.task_id == uuid.UUID(TASK)

    @pytest.mark.parametrize("agent", ["llm", "stub", "gold"])
    def test_each_agent_is_accepted(self, agent):
        assert cli.build_parser().parse_args([TASK, "--agent", agent]).agent == agent

    def test_an_unknown_agent_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as caught:
            cli.build_parser().parse_args([TASK, "--agent", "devin"])

        assert caught.value.code == 2

    def test_no_pr_is_a_flag(self):
        assert cli.build_parser().parse_args([TASK, "--no-pr"]).no_pr is True


class TestTheLlmAgentIsWired:
    def test_there_is_no_not_wired_branch_left(self, monkeypatch, capsys):
        """`main` hands `--agent llm` to `_run` like any other agent, and prints nothing of its own."""
        seen = {}

        async def fake_run(task_id, *, agent, no_pr):
            seen.update(task_id=task_id, agent=agent, no_pr=no_pr)
            return 0

        monkeypatch.setattr(cli, "_run", fake_run)
        monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)

        assert cli.main([TASK, "--agent", "llm"]) == 0

        assert seen == {"task_id": uuid.UUID(TASK), "agent": "llm", "no_pr": False}
        assert capsys.readouterr().err == ""
        assert not hasattr(cli, "NOT_WIRED")

    def test_a_bare_invocation_runs_the_llm_agent(self, monkeypatch):
        seen = {}

        async def fake_run(task_id, *, agent, no_pr):
            seen["agent"] = agent
            return 0

        monkeypatch.setattr(cli, "_run", fake_run)
        monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)

        cli.main([TASK])

        assert seen["agent"] == "llm"


class TestTheRunnersArgv:
    """The benchmark runner starts `python -m repolace_pipeline.cli <task_id> --agent {llm,gold} [--no-pr]`."""

    @pytest.mark.parametrize(
        ("argv", "agent", "no_pr"),
        [
            ([TASK, "--agent", "llm"], "llm", False),
            ([TASK, "--agent", "llm", "--no-pr"], "llm", True),
            ([TASK, "--agent", "gold", "--no-pr"], "gold", True),
            ([TASK, "--agent", "gold"], "gold", False),
        ],
    )
    def test_this_exact_argv_parses(self, argv, agent, no_pr):
        args = cli.build_parser().parse_args(argv)

        assert (args.task_id, args.agent, args.no_pr) == (uuid.UUID(TASK), agent, no_pr)

    @pytest.mark.parametrize("agent", ["llm", "gold"])
    @pytest.mark.parametrize("open_pr", [True, False])
    def test_what_the_runner_really_builds_parses(self, agent, open_pr):
        """Reads the real builder, so the two cannot drift apart. Skipped where the harness is not installed."""
        runner = pytest.importorskip("harness.runner")
        config = SimpleNamespace(agent=agent, open_pr=open_pr)

        argv = runner.child_argv(("python", "-m", "repolace_pipeline.cli"), uuid.UUID(TASK), config)
        args = cli.build_parser().parse_args(argv[3:])

        assert (args.task_id, args.agent) == (uuid.UUID(TASK), agent)


class TestOpenPrAllowed:
    @pytest.mark.parametrize(
        ("agent", "no_pr", "allowed"),
        [
            ("stub", False, True),
            ("llm", False, True),
            ("stub", True, False),
            ("llm", True, False),
            ("gold", False, False),
            ("gold", True, False),
        ],
    )
    def test_the_matrix(self, agent, no_pr, allowed):
        assert cli.open_pr_allowed(agent, no_pr) is allowed

    def test_gold_never_opens_one_whatever_was_passed(self):
        """A validation run that opened PRs would write to the bench repository unasked."""
        assert cli.open_pr_allowed("gold", False) is False


class Recorder:
    """What `_run` handed to the things it builds, with a `run_task` that returns a canned result."""

    def __init__(self, monkeypatch, result=None, raises=None):
        self.run_task_kwargs = None
        self.github = FakeGithubClient()
        self.disposed = False
        settings = SimpleNamespace(
            database_url="postgresql+asyncpg://x/y",
            github_app_id="1",
            github_app_private_key="key",
            verify_specs_path="specs.toml",
            instances_dir="/instances",
            embedding_strategy="head_tail",
        )

        class Engine:
            async def dispose(inner):
                self.disposed = True

        async def run_task(task_id, factory, github, backend, specs, **kwargs):
            self.run_task_kwargs = kwargs
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        monkeypatch.setattr(cli, "create_engine", lambda url: Engine())
        monkeypatch.setattr(cli, "create_session_factory", lambda engine: "factory")
        monkeypatch.setattr(cli, "GithubClient", lambda app_id, key: self.github)
        monkeypatch.setattr(cli, "DockerBackend", lambda: "backend")
        monkeypatch.setattr(cli, "load_specs", lambda path: {})
        monkeypatch.setattr(cli, "run_task", run_task)
        self.llm = object()
        self.llm_built_with = []

        def build_llm_client(session_factory, *args, **kwargs):
            self.llm_built_with.append(session_factory)
            return self.llm

        monkeypatch.setattr(cli, "build_llm_client", build_llm_client)


class TestWhatIsPassedToRunTask:
    @pytest.mark.anyio
    async def test_the_llm_agent_gets_the_real_runner_and_the_client_built_once(self, monkeypatch):
        recorder = Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.COMPLETED))

        code = await cli._run(uuid.UUID(TASK), agent="llm", no_pr=False)

        assert code == 0
        assert isinstance(recorder.run_task_kwargs["agent"], LLMAgent)
        assert recorder.run_task_kwargs["llm"] is recorder.llm
        assert recorder.llm_built_with == ["factory"], "one client, on the process's own session factory"
        assert recorder.run_task_kwargs["open_pr"] is True

    @pytest.mark.anyio
    @pytest.mark.parametrize("agent", ["stub", "gold"])
    async def test_the_agents_with_no_model_build_no_client(self, monkeypatch, agent):
        recorder = Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.COMPLETED))

        async def gold_runner(factory, instances_dir, task_id):
            return object()

        monkeypatch.setattr(cli, "_gold_runner", gold_runner)

        await cli._run(uuid.UUID(TASK), agent=agent, no_pr=False)

        assert recorder.llm_built_with == []
        assert recorder.run_task_kwargs["llm"] is None

    @pytest.mark.anyio
    async def test_a_model_the_gateway_refuses_exits_2_before_run_task_is_called(self, monkeypatch, capsys):
        """`run_task` is what claims the row, so a refusal that comes first leaves no failed task behind."""
        recorder = Recorder(monkeypatch)

        def refuse(session_factory, *args, **kwargs):
            raise cli.UsageError("--agent llm cannot run: no price (fix the model's [price] in gateway/models.toml)")

        monkeypatch.setattr(cli, "build_llm_client", refuse)

        code = await cli._run(uuid.UUID(TASK), agent="llm", no_pr=False)

        assert code == 2
        assert recorder.run_task_kwargs is None, "run_task was never reached"
        assert "gateway/models.toml" in capsys.readouterr().err
        assert recorder.github.closed and recorder.disposed

    @pytest.mark.anyio
    async def test_the_stub_passes_no_agent_and_may_open_a_pr(self, monkeypatch):
        recorder = Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.COMPLETED))

        code = await cli._run(uuid.UUID(TASK), agent="stub", no_pr=False)

        assert code == 0
        assert recorder.run_task_kwargs["agent"] is None
        assert recorder.run_task_kwargs["open_pr"] is True
        assert recorder.run_task_kwargs["instances_dir"] == "/instances"
        assert recorder.run_task_kwargs["embedding_strategy"] == "head_tail"

    @pytest.mark.anyio
    async def test_no_pr_is_passed_through(self, monkeypatch):
        recorder = Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.COMPLETED))

        await cli._run(uuid.UUID(TASK), agent="stub", no_pr=True)

        assert recorder.run_task_kwargs["open_pr"] is False

    @pytest.mark.anyio
    async def test_gold_builds_its_runner_and_forces_no_pr(self, monkeypatch):
        recorder = Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.COMPLETED))
        marker = object()

        async def gold_runner(factory, instances_dir, task_id):
            assert (factory, instances_dir, task_id) == ("factory", "/instances", uuid.UUID(TASK))
            return marker

        monkeypatch.setattr(cli, "_gold_runner", gold_runner)

        await cli._run(uuid.UUID(TASK), agent="gold", no_pr=False)

        assert recorder.run_task_kwargs["agent"] is marker
        assert recorder.run_task_kwargs["open_pr"] is False, "whatever the flags said"

    @pytest.mark.anyio
    async def test_the_clients_are_closed_after_the_run(self, monkeypatch):
        recorder = Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.COMPLETED))

        await cli._run(uuid.UUID(TASK), agent="stub", no_pr=False)

        assert recorder.github.closed and recorder.disposed


class TestExitCodes:
    """Unchanged, and the contract the benchmark runner depends on."""

    @pytest.mark.anyio
    async def test_a_completed_task_exits_0_even_when_it_scored_failed(self, monkeypatch):
        """The pipeline worked; the agent did not fix the issue. Those are different facts."""
        Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.COMPLETED, error_message=None))

        assert await cli._run(uuid.UUID(TASK), agent="stub", no_pr=False) == 0

    @pytest.mark.anyio
    async def test_a_pr_opened_exits_0(self, monkeypatch):
        Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.PR_OPENED, 1, "https://x"))

        assert await cli._run(uuid.UUID(TASK), agent="stub", no_pr=False) == 0

    @pytest.mark.anyio
    async def test_a_failed_task_exits_1(self, monkeypatch):
        Recorder(monkeypatch, RunResult(uuid.UUID(TASK), TaskStatus.FAILED, error_message="clone: boom"))

        assert await cli._run(uuid.UUID(TASK), agent="stub", no_pr=False) == 1

    @pytest.mark.anyio
    async def test_an_unknown_task_exits_2(self, monkeypatch):
        Recorder(monkeypatch, raises=TaskNotFound(uuid.UUID(TASK)))

        assert await cli._run(uuid.UUID(TASK), agent="stub", no_pr=False) == 2

    @pytest.mark.anyio
    async def test_a_task_that_is_not_queued_exits_3(self, monkeypatch):
        Recorder(monkeypatch, raises=TaskNotClaimable(uuid.UUID(TASK), "completed"))

        assert await cli._run(uuid.UUID(TASK), agent="stub", no_pr=False) == 3

    @pytest.mark.anyio
    async def test_a_usage_problem_exits_2_with_its_line_on_stderr(self, monkeypatch, capsys):
        Recorder(monkeypatch, raises=cli.UsageError("--agent gold needs a benchmark task"))

        code = await cli._run(uuid.UUID(TASK), agent="stub", no_pr=False)

        assert code == 2
        assert "--agent gold needs a benchmark task" in capsys.readouterr().err

    def test_the_exit_codes_keep_their_numbers(self):
        assert (cli.EXIT_OK, cli.EXIT_TASK_FAILED, cli.EXIT_NOT_FOUND, cli.EXIT_NOT_CLAIMABLE, cli.EXIT_USAGE) == (
            0, 1, 2, 3, 2,
        )


PRICE = "[models.main.price]\ninput = 3.0\noutput = 15.0\n"


def gateway_settings(tmp_path: Path, models_toml: str, **overrides) -> GatewaySettings:
    """Settings that read neither `.env` nor the developer's real keys, over a `models.toml` of the test's own."""
    path = tmp_path / "models.toml"
    path.write_text(models_toml)
    values = {
        "anthropic_api_key": "sk-ant-api03-" + "Zq9Xk2" * 6,
        "openai_api_key": None,
        "gemini_api_key": None,
        "models_path": path,
        "stage_models": {},
        **overrides,
    }
    return GatewaySettings(_env_file=None, **values)


def models_toml(*, price: str = "", fallback: str = "") -> str:
    return f"""
[gateway]
max_attempts = 2

[stages.agent]
model = "main"
{fallback}
[stages.cheap]
model = "main"

[models.main]
provider = "anthropic"
litellm_model = "anthropic/test-model-with-no-known-price"
supports_prompt_caching = false

{price}
[models.backup]
provider = "anthropic"
litellm_model = "anthropic/test-backup-with-no-known-price"
supports_prompt_caching = false
"""


def failing_model_info(**kwargs):
    raise KeyError("this model is not in the price map")


class TestTheModelIsCheckedBeforeAnyTaskIsClaimed:
    """`build_llm_client` is what `_run` calls ahead of `run_task`; each refusal becomes exit 2's message."""

    def test_a_model_with_no_price_is_refused_naming_models_toml(self, tmp_path):
        settings = gateway_settings(tmp_path, models_toml())

        with pytest.raises(cli.UsageError, match=r"gateway/models\.toml") as caught:
            cli.build_llm_client("factory", settings, model_info_fn=failing_model_info)

        assert "test-model-with-no-known-price" in str(caught.value), "it says which model"

    def test_a_priced_model_is_accepted(self, tmp_path):
        settings = gateway_settings(tmp_path, models_toml(price=PRICE))

        assert cli.build_llm_client("factory", settings, model_info_fn=failing_model_info) is not None

    def test_an_unpriced_fallback_is_refused_too(self, tmp_path):
        """The fallback is only called after the primary fails for good, which is the worst moment to learn of it."""
        settings = gateway_settings(tmp_path, models_toml(price=PRICE, fallback='fallback = "backup"'))

        with pytest.raises(cli.UsageError, match=r"gateway/models\.toml") as caught:
            cli.build_llm_client("factory", settings, model_info_fn=failing_model_info)

        assert "test-backup-with-no-known-price" in str(caught.value)

    def test_a_missing_provider_key_is_a_usage_error_naming_the_variable(self, tmp_path):
        settings = gateway_settings(tmp_path, models_toml(price=PRICE), anthropic_api_key=None)

        with pytest.raises(cli.UsageError, match="ANTHROPIC_API_KEY"):
            cli.build_llm_client("factory", settings)

    def test_a_stage_override_naming_an_unknown_model_is_a_usage_error(self, tmp_path):
        """`GATEWAY_STAGE_MODELS` is the benchmark runner's `--model`; the gateway applies it, this does not re-parse it."""
        settings = gateway_settings(tmp_path, models_toml(price=PRICE), stage_models={"agent": "no-such-model"})

        with pytest.raises(cli.UsageError, match="no-such-model"):
            cli.build_llm_client("factory", settings)

    def test_a_stage_override_is_the_model_the_check_looks_at(self, tmp_path):
        """The override routes the agent stage to the unpriced `backup`, and that is what must be refused."""
        settings = gateway_settings(tmp_path, models_toml(price=PRICE), stage_models={"agent": "backup"})

        with pytest.raises(cli.UsageError, match="test-backup-with-no-known-price"):
            cli.build_llm_client("factory", settings, model_info_fn=failing_model_info)


@pytest.mark.anyio
@pytest.mark.db
class TestGoldRunnerFromATask:
    async def test_it_is_built_from_the_instance_the_task_names(self, db_session, db_session_factory, tmp_path):
        instance = make_instance()
        dump_instance(instance, tmp_path / f"{instance.instance_id}.json")
        task = await seed_task(db_session, instance_id=instance.instance_id, eval_run_id="r", run_index=0)

        runner = await cli._gold_runner(db_session_factory, tmp_path, task.id)

        assert isinstance(runner, GoldAgent)
        assert runner.instance == instance

    async def test_a_product_task_has_no_gold_fix_to_apply(self, db_session, db_session_factory, tmp_path):
        task = await seed_task(db_session)

        with pytest.raises(cli.UsageError, match="has no instance_id"):
            await cli._gold_runner(db_session_factory, tmp_path, task.id)

    async def test_an_unknown_task_is_not_found(self, db_session, db_session_factory, tmp_path):
        with pytest.raises(TaskNotFound):
            await cli._gold_runner(db_session_factory, tmp_path, uuid.uuid4())

    async def test_a_missing_instance_file_is_a_usage_error_not_a_traceback(self, db_session, db_session_factory, tmp_path):
        task = await seed_task(db_session, instance_id="acme__sample-7", eval_run_id="r", run_index=0)

        with pytest.raises(cli.UsageError, match="cannot load instance"):
            await cli._gold_runner(db_session_factory, tmp_path, task.id)

    async def test_an_instance_id_that_is_a_path_is_refused(self, db_session, db_session_factory, tmp_path):
        (tmp_path / "secret.json").write_text("{}")
        (tmp_path / "instances").mkdir()
        task = await seed_task(db_session, instance_id="../secret", eval_run_id="r", run_index=0)

        with pytest.raises(cli.UsageError, match="cannot load instance"):
            await cli._gold_runner(db_session_factory, tmp_path / "instances", task.id)
