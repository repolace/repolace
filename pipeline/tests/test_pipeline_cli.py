"""The command line: what it refuses, what it passes to `run_task`, and the exit codes.

The exit code is a contract with the benchmark runner, which treats 1 as "repolace broke" and
anything else as the pipeline having worked. So the tests that matter most are the ones that pin
which situation gets which number, and that an agent-caused stop does not get 1.
"""

import uuid
from types import SimpleNamespace

import pytest

import repolace_pipeline.cli as cli
from repolace_shared.db.models import TaskStatus
from repolace_shared.instances import dump_instance

from repolace_pipeline.errors import TaskNotClaimable, TaskNotFound
from repolace_pipeline.run import RunResult
from repolace_pipeline.runners import GoldAgent

from pipeline_support import FakeGithubClient, make_instance, seed_task

TASK = "2f8a1c4e-0000-4000-8000-000000000001"


class TestArguments:
    def test_the_agent_defaults_to_the_stub_until_the_llm_is_wired(self):
        args = cli.build_parser().parse_args([TASK])

        assert args.agent == "stub"
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


class TestTheLlmAgentIsNotWiredYet:
    def test_it_exits_2_with_one_clear_line_and_no_traceback(self, capsys, monkeypatch):
        # If anything past the parser ran, one of these would raise: the refusal must come first.
        monkeypatch.setattr(cli, "get_settings", lambda: pytest.fail("settings were read"))
        monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: pytest.fail("logging was configured"))

        code = cli.main([TASK, "--agent", "llm"])

        captured = capsys.readouterr()
        assert code == 2
        assert "not wired yet" in captured.err
        assert len(captured.err.strip().splitlines()) == 1
        assert "Traceback" not in captured.err and captured.out == ""


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


class TestWhatIsPassedToRunTask:
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
