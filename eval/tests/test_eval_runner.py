"""`repolace-eval run`: the scheduler, and the rule that a task row is never run twice.

The children are `sys.executable -c ...` scripts that claim their row the way the
pipeline does, so the timeout, abandonment and exit-code tests exercise the real
SQL against a real database. What they pin is the failure the runner exists to
survive: a row left RUNNING by a dead process must end FAILED, never QUEUED --
`task_test_runs` is unique on `(task_id, attempt)`, so a second run of a row
cannot be recorded.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from eval_exec_support import (
    TOKEN,
    add_llm_call,
    add_repo,
    add_task,
    child_command,
)
from harness import runner
from harness.bench_repos import TOKEN_ENV_VAR
from harness.db import queued_tasks, run_cost_usd
from harness.runner import (
    ABANDONED_GRACE_SECONDS,
    Outcome,
    RunnerConfig,
    build_parser,
    child_argv,
    child_env,
    classify,
    main,
    mark_abandoned,
    read_peak_rss_kb,
    remove_task_containers,
    run_queue,
)
from repolace_shared.db.models import Task, TaskStatus
from repolace_shared.process import ProcessResult

RUN = "run-1"


def ago(**kwargs) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


class FakeProcesses:
    """A `process_runner` seam recording docker and pgrep invocations; answers are scripted."""

    def __init__(self, *, ps_output: bytes = b"", ps_returncode: int = 0, pgrep=None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.ps_output = ps_output
        self.ps_returncode = ps_returncode
        self.pgrep = pgrep or (lambda task_id: ProcessResult(1, b"", b""))

    async def __call__(self, program: str, *args: str, **kwargs) -> ProcessResult:
        self.calls.append((program, *args))
        if program == "pgrep":
            return self.pgrep(args[-1])
        if args[0] == "ps":
            return ProcessResult(self.ps_returncode, self.ps_output, b"")
        return ProcessResult(0, b"", b"")


def child_base_env(tmp_path: Path, plan: dict | None = None, dsn: str | None = None, **extra: str) -> dict[str, str]:
    records = tmp_path / "records"
    records.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ["PATH"],
        "CHILD_RECORD_DIR": str(records),
        "CHILD_PLAN": json.dumps(plan or {}),
        TOKEN_ENV_VAR: TOKEN,
        "CHILD_KEEP_ME": "kept",
        **extra,
    }
    if dsn is not None:
        env["CHILD_DB_DSN"] = dsn.replace("+asyncpg", "")
    return env


def records(tmp_path: Path) -> dict[str, dict]:
    found = {}
    for path in (tmp_path / "records").glob("*.json"):
        found[path.stem] = json.loads(path.read_text())
    return found


def config(**overrides) -> RunnerConfig:
    return RunnerConfig(eval_run_id=overrides.pop("eval_run_id", RUN), **{"concurrency": 3, **overrides})


class TestPureHelpers:
    @pytest.mark.parametrize(
        ("code", "outcome"),
        [(0, Outcome.RAN), (1, Outcome.REPOLACE_FAILED), (2, Outcome.NOT_FOUND), (3, Outcome.NOT_CLAIMABLE),
         (7, Outcome.CRASHED), (255, Outcome.CRASHED), (-9, Outcome.CRASHED), (-11, Outcome.CRASHED)],
    )
    def test_exit_code_mapping(self, code, outcome):
        assert classify(code) is outcome

    def test_the_exit_codes_are_the_pipelines(self):
        source = (Path(__file__).resolve().parents[2] / "pipeline" / "repolace_pipeline" / "cli.py").read_text()
        declared = {name: int(value) for name, value in re.findall(r"^(EXIT_[A-Z_]+) = (\d+)$", source, re.M)}
        assert declared == {
            "EXIT_OK": runner.EXIT_OK,
            "EXIT_TASK_FAILED": runner.EXIT_TASK_FAILED,
            "EXIT_NOT_FOUND": runner.EXIT_NOT_FOUND,
            "EXIT_NOT_CLAIMABLE": runner.EXIT_NOT_CLAIMABLE,
        }

    def test_the_child_command_line(self):
        task_id = uuid.uuid4()
        assert child_argv(("run-task",), task_id, config()) == ["run-task", str(task_id), "--agent", "llm"]
        assert child_argv(("run-task",), task_id, config(open_pr=False))[-1] == "--no-pr"

    def test_gold_always_passes_no_pr_explicitly(self):
        argv = child_argv(("run-task",), uuid.uuid4(), config(agent="gold", open_pr=True))
        assert argv[-3:] == ["--agent", "gold", "--no-pr"]

    def test_the_child_environment_drops_the_bench_token_and_keeps_everything_else(self):
        base = {TOKEN_ENV_VAR: TOKEN, "DATABASE_URL": "postgres://x", "HF_HOME": "/hf", "ANTHROPIC_API_KEY": "k",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True"}

        env = child_env(base, None)

        assert TOKEN_ENV_VAR not in env
        assert {k: v for k, v in env.items()} == {k: v for k, v in base.items() if k != TOKEN_ENV_VAR}
        assert TOKEN_ENV_VAR in base, "the caller's mapping must not be mutated"

    def test_a_model_key_becomes_gateway_stage_models(self):
        env = child_env({"A": "1"}, "claude-sonnet-5-5")

        assert json.loads(env["GATEWAY_STAGE_MODELS"]) == {"agent": "claude-sonnet-5-5"}

    def test_no_model_leaves_gateway_stage_models_alone(self):
        assert "GATEWAY_STAGE_MODELS" not in child_env({"A": "1"}, None)
        assert child_env({"GATEWAY_STAGE_MODELS": '{"x": "y"}'}, None)["GATEWAY_STAGE_MODELS"] == '{"x": "y"}'

    def test_the_defaults(self):
        args = build_parser().parse_args(["--eval-run-id", "r"])

        assert (args.concurrency, args.timeout_seconds, args.agent) == (3, 5400.0, "llm")
        assert args.no_pr is False and args.mark_abandoned is False
        assert args.max_total_usd is None and args.limit is None and args.model is None

    @pytest.mark.parametrize(
        "bad",
        [["-k", "0"], ["-k", "x"], ["--timeout-seconds", "0"], ["--timeout-seconds", "-1"], ["--timeout-seconds", "inf"],
         ["--max-total-usd", "0"], ["--max-total-usd", "-1"], ["--max-total-usd", "nan"], ["--max-total-usd", "inf"],
         ["--limit", "0"], ["--agent", "stub"]],
    )
    def test_nonsense_options_are_refused(self, bad):
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(["--eval-run-id", "r", *bad])
        assert raised.value.code == 2

    @pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs /proc")
    def test_peak_rss_of_a_live_process_is_read(self):
        assert read_peak_rss_kb(os.getpid()) > 0

    def test_peak_rss_of_a_missing_process_is_none(self):
        assert read_peak_rss_kb(2**22 + 12345) is None

    def test_a_bad_run_id_is_a_usage_error_before_anything_else(self, capsys):
        assert main(["--eval-run-id", "../x"]) == 2
        assert "eval run id" in capsys.readouterr().err


@pytest.mark.anyio
class TestContainerCleanup:
    async def test_lists_by_the_first_twelve_hex_of_the_task_id_then_removes_survivors(self):
        task_id = uuid.UUID("0123456789abcdef0123456789abcdef")
        fake = FakeProcesses(ps_output=b"aabbccddeeff\n112233445566\n")

        removed = await remove_task_containers(task_id, process_runner=fake)

        assert fake.calls == [
            ("docker", "ps", "-q", "--filter", "name=repolace-0123456789ab"),
            ("docker", "rm", "-f", "aabbccddeeff", "112233445566"),
        ]
        assert removed == ("aabbccddeeff", "112233445566")

    async def test_no_survivors_means_no_rm(self):
        fake = FakeProcesses(ps_output=b"")

        assert await remove_task_containers(uuid.uuid4(), process_runner=fake) == ()
        assert [call[1] for call in fake.calls] == ["ps"]

    async def test_output_that_is_not_a_container_id_is_never_passed_to_rm(self):
        fake = FakeProcesses(ps_output=b"--all\n../x\naabbccddeeff\n")

        removed = await remove_task_containers(uuid.uuid4(), process_runner=fake)

        assert removed == ("aabbccddeeff",)
        assert fake.calls[1][-1] == "aabbccddeeff" and "--all" not in fake.calls[1]

    async def test_a_failing_ps_removes_nothing(self):
        fake = FakeProcesses(ps_output=b"aabbccddeeff\n", ps_returncode=1)

        assert await remove_task_containers(uuid.uuid4(), process_runner=fake) == ()
        assert [call[1] for call in fake.calls] == ["ps"]

    async def test_a_missing_docker_binary_does_not_raise(self):
        async def missing(*args, **kwargs):
            raise FileNotFoundError("docker")

        assert await remove_task_containers(uuid.uuid4(), process_runner=missing) == ()


@pytest.mark.anyio
@pytest.mark.db
class TestScheduling:
    async def seed(self, session, *rows: tuple[str, int], status=TaskStatus.QUEUED, run: str = RUN):
        repo = await add_repo(session, "repolace/bench-shared")
        return [await add_task(session, repo, eval_run_id=run, instance_id=i, run_index=k, status=status) for i, k in rows]

    async def test_queued_tasks_come_back_ordered_by_run_index_then_instance(self, db_session, db_session_factory):
        await self.seed(db_session, ("b", 1), ("a", 1), ("b", 0), ("a", 0), ("c", 0))

        queued = await queued_tasks(db_session_factory, RUN)

        assert [(t.run_index, t.instance_id) for t in queued] == [(0, "a"), (0, "b"), (0, "c"), (1, "a"), (1, "b")]

    async def test_dispatch_follows_that_order(self, db_session, db_session_factory, tmp_path):
        tasks = await self.seed(db_session, ("b", 1), ("a", 1), ("b", 0), ("a", 0))

        await run_queue(
            db_session_factory, config(concurrency=1), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        started = sorted(records(tmp_path).items(), key=lambda item: item[1]["start"])
        by_id = {str(t.id): (t.run_index, t.instance_id) for t in tasks}
        assert [by_id[task_id] for task_id, _ in started] == [(0, "a"), (0, "b"), (1, "a"), (1, "b")]

    async def test_concurrency_never_exceeds_k_and_reaches_it(self, db_session, db_session_factory, tmp_path):
        await self.seed(db_session, *[(f"i{n}", 0) for n in range(6)])
        env = child_base_env(tmp_path, {"*": {"sleep": 0.5}})

        summary = await run_queue(
            db_session_factory, config(concurrency=2), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=env,
        )

        assert len(summary.results) == 6
        spans = []
        for task_id, record in records(tmp_path).items():
            spans.append((record["start"], float((tmp_path / "records" / f"{task_id}.end").read_text())))
        peak = max(sum(1 for start, end in spans if start <= moment < end) for moment, _ in spans)
        assert peak == 2

    async def test_rows_that_are_not_queued_are_skipped(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        queued = await add_task(db_session, repo, instance_id="q", run_index=0)
        for index, status in enumerate((TaskStatus.RUNNING, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PR_OPENED)):
            await add_task(db_session, repo, instance_id=f"other{index}", run_index=0, status=status)
        await add_task(db_session, repo, eval_run_id="another-run", instance_id="q", run_index=0)

        summary = await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        assert [r.task.task_id for r in summary.results] == [queued.id]
        assert set(records(tmp_path)) == {str(queued.id)}

    async def test_limit_dispatches_at_most_n_in_queue_order(self, db_session, db_session_factory, tmp_path):
        await self.seed(db_session, ("a", 0), ("b", 0), ("c", 0))

        summary = await run_queue(
            db_session_factory, config(limit=2, concurrency=1), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        assert [r.task.instance_id for r in summary.results] == ["a", "b"]
        assert summary.not_dispatched == 1

    async def test_the_model_reaches_the_child_and_the_bench_token_does_not(self, db_session, db_session_factory, tmp_path):
        (task,) = await self.seed(db_session, ("a", 0))

        await run_queue(
            db_session_factory, config(model="claude-sonnet-5-5", open_pr=False), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        record = records(tmp_path)[str(task.id)]
        assert json.loads(record["gateway"]) == {"agent": "claude-sonnet-5-5"}
        assert record["has_bench_token"] is False
        assert record["has_other"] == "kept"
        assert record["argv"] == [str(task.id), "--agent", "llm", "--no-pr"]

    async def test_child_output_goes_to_the_per_task_log(self, db_session, db_session_factory, tmp_path):
        (task,) = await self.seed(db_session, ("a", 0))

        await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        log = (tmp_path / "runs" / RUN / f"{task.id}.log").read_text()
        assert "child output on stdout" in log and "child output on stderr" in log

    async def test_peak_memory_of_each_child_is_recorded(self, db_session, db_session_factory, tmp_path):
        await self.seed(db_session, ("a", 0))
        env = child_base_env(tmp_path, {"*": {"sleep": 0.6}})

        summary = await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=env, rss_interval=0.05,
        )

        assert summary.results[0].peak_rss_kb and summary.results[0].peak_rss_kb > 0

    async def test_a_command_that_cannot_start_stops_dispatching_and_leaves_rows_queued(self, db_session, db_session_factory, tmp_path):
        await self.seed(db_session, ("a", 0), ("b", 0), ("c", 0))

        summary = await run_queue(
            db_session_factory, config(concurrency=1), runs_dir=tmp_path / "runs",
            child_command=("/nonexistent/repolace-run-task",), base_env=child_base_env(tmp_path),
        )

        assert [r.outcome for r in summary.results] == [Outcome.SPAWN_FAILED]
        assert "cannot start" in summary.stopped and summary.not_dispatched == 2
        assert len(await queued_tasks(db_session_factory, RUN)) == 3

    async def test_an_empty_queue_is_a_clean_no_op(self, db_session_factory, tmp_path):
        summary = await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        assert summary.results == [] and summary.stopped is None


@pytest.mark.anyio
@pytest.mark.db
class TestCostCap:
    async def test_the_run_cost_sums_only_this_runs_llm_calls(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-x")
        mine = await add_task(db_session, repo, instance_id="a", run_index=0)
        theirs = await add_task(db_session, repo, eval_run_id="other", instance_id="a", run_index=0)
        await add_llm_call(db_session, mine, "0.40")
        await add_llm_call(db_session, mine, "0.35")
        await add_llm_call(db_session, mine, None)
        await add_llm_call(db_session, theirs, "9.00")

        assert await run_cost_usd(db_session_factory, RUN) == Decimal("0.75")
        assert await run_cost_usd(db_session_factory, "empty") == Decimal(0)

    async def test_nothing_is_dispatched_once_the_cap_is_already_reached(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        spent = await add_task(db_session, repo, instance_id="done", run_index=0, status=TaskStatus.COMPLETED)
        await add_llm_call(db_session, spent, "1.00")
        await add_task(db_session, repo, instance_id="a", run_index=0)
        await add_task(db_session, repo, instance_id="b", run_index=0)

        summary = await run_queue(
            db_session_factory, config(max_total_usd=Decimal("1.00")), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        assert summary.results == [] and summary.not_dispatched == 2
        assert "cost cap" in summary.stopped
        assert records(tmp_path) == {}
        assert len(await queued_tasks(db_session_factory, RUN)) == 2

    async def test_spend_during_the_run_stops_the_next_dispatch_but_not_the_one_in_flight(
        self, db_session, db_session_factory, tmp_path, postgres_url
    ):
        repo = await add_repo(db_session, "repolace/bench-x")
        first = await add_task(db_session, repo, instance_id="a", run_index=0)
        await add_task(db_session, repo, instance_id="b", run_index=0)
        await add_task(db_session, repo, instance_id="c", run_index=0)
        env = child_base_env(tmp_path, {str(first.id): {"claim": True, "cost": "1.5"}}, dsn=postgres_url)

        summary = await run_queue(
            db_session_factory, config(concurrency=1, max_total_usd=Decimal("1.00")), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=env,
        )

        assert [r.task.task_id for r in summary.results] == [first.id]
        assert summary.not_dispatched == 2 and "cost cap" in summary.stopped
        assert summary.total_cost_usd == Decimal("1.5")

    async def test_without_a_cap_everything_is_dispatched(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        spent = await add_task(db_session, repo, instance_id="done", run_index=0, status=TaskStatus.COMPLETED)
        await add_llm_call(db_session, spent, "50.00")
        await add_task(db_session, repo, instance_id="a", run_index=0)

        summary = await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        assert len(summary.results) == 1 and summary.stopped is None


@pytest.mark.anyio
@pytest.mark.db
class TestChildFailures:
    async def status(self, session, task) -> Task:
        await session.refresh(task)
        return task

    async def run_one(self, db_session, db_session_factory, tmp_path, postgres_url, plan, *, timeout=60.0, processes=None):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)
        env = child_base_env(tmp_path, {str(task.id): plan}, dsn=postgres_url)
        summary = await run_queue(
            db_session_factory, config(timeout_seconds=timeout), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=env, process_runner=processes or FakeProcesses(),
        )
        return task, summary.results[0]

    async def test_a_child_that_finishes_normally_is_left_exactly_as_it_left_the_row(self, db_session, db_session_factory, tmp_path, postgres_url):
        task, result = await self.run_one(
            db_session, db_session_factory, tmp_path, postgres_url, {"claim": True, "finish": "completed"}
        )

        assert result.outcome is Outcome.RAN and result.marked_failed is False
        row = await self.status(db_session, task)
        assert row.status is TaskStatus.COMPLETED and row.error_message is None

    async def test_exit_one_with_the_row_already_failed_by_the_pipeline_is_not_rewritten(self, db_session, db_session_factory, tmp_path, postgres_url):
        task, result = await self.run_one(
            db_session, db_session_factory, tmp_path, postgres_url, {"claim": True, "finish": "failed", "exit": 1}
        )

        assert result.outcome is Outcome.REPOLACE_FAILED and result.marked_failed is False
        assert (await self.status(db_session, task)).error_message is None

    async def test_a_child_that_dies_holding_the_row_leaves_it_failed_not_queued(self, db_session, db_session_factory, tmp_path, postgres_url):
        task, result = await self.run_one(db_session, db_session_factory, tmp_path, postgres_url, {"claim": True, "exit": 9})

        assert result.outcome is Outcome.CRASHED and result.marked_failed is True
        row = await self.status(db_session, task)
        assert row.status is TaskStatus.FAILED
        assert row.error_message.startswith("runner: child exited 9")
        assert row.completed_at is not None

    @pytest.mark.parametrize("code", [2, 3])
    async def test_not_found_and_not_claimable_never_touch_a_row_the_child_did_not_own(
        self, db_session, db_session_factory, tmp_path, code
    ):
        repo = await add_repo(db_session, "repolace/bench-x")
        # Another runner owns this row right now; this child exits 3 because it could not claim it.
        task = await add_task(db_session, repo, instance_id="a", run_index=0, status=TaskStatus.RUNNING)
        run_dir = tmp_path / "runs" / RUN
        run_dir.mkdir(parents=True)

        result = await runner._run_child(
            runner.QueuedTask(task.id, "a", 0), config(), db_session_factory,
            command=child_command(), env=child_base_env(tmp_path, {str(task.id): {"exit": code}}), run_dir=run_dir,
            process_runner=FakeProcesses(), docker="docker", rss_interval=1.0,
        )

        assert result.outcome in (Outcome.NOT_FOUND, Outcome.NOT_CLAIMABLE) and result.marked_failed is False
        assert (await self.status(db_session, task)).status is TaskStatus.RUNNING


@pytest.mark.anyio
@pytest.mark.db
class TestTimeout:
    async def timed_out_run(self, db_session, db_session_factory, tmp_path, postgres_url, plan, processes):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)
        env = child_base_env(tmp_path, {str(task.id): plan}, dsn=postgres_url)
        started = time.monotonic()
        summary = await run_queue(
            db_session_factory, config(timeout_seconds=1.5), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=env, process_runner=processes,
        )
        return task, summary.results[0], time.monotonic() - started

    async def test_a_hung_child_is_killed_with_its_whole_process_group_and_its_row_failed(
        self, db_session, db_session_factory, tmp_path, postgres_url
    ):
        processes = FakeProcesses()

        task, result, elapsed = await self.timed_out_run(
            db_session, db_session_factory, tmp_path, postgres_url, {"claim": True, "grandchild": True, "hang": True}, processes
        )

        assert result.outcome is Outcome.TIMED_OUT and result.marked_failed is True
        assert elapsed < 30
        await db_session.refresh(task)
        assert task.status is TaskStatus.FAILED
        assert task.error_message == "runner: timeout"
        assert task.completed_at is not None
        grandchild = int((tmp_path / "records" / f"{task.id}.grandchild").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            os.kill(grandchild, 9)
            pytest.fail("the grandchild survived the group kill")

    async def test_a_task_that_finished_in_the_same_instant_is_untouched(
        self, db_session, db_session_factory, tmp_path, postgres_url
    ):
        task, result, _ = await self.timed_out_run(
            db_session, db_session_factory, tmp_path, postgres_url,
            {"claim": True, "finish": "completed", "hang": True}, FakeProcesses(),
        )

        assert result.outcome is Outcome.TIMED_OUT and result.marked_failed is False
        await db_session.refresh(task)
        assert task.status is TaskStatus.COMPLETED
        assert task.error_message is None

    async def test_containers_for_the_task_are_removed_after_the_row_is_settled(
        self, db_session, db_session_factory, tmp_path, postgres_url
    ):
        processes = FakeProcesses(ps_output=b"aabbccddeeff\n")

        task, result, _ = await self.timed_out_run(
            db_session, db_session_factory, tmp_path, postgres_url, {"claim": True, "hang": True}, processes
        )

        prefix = f"repolace-{task.id.hex[:12]}"
        assert processes.calls == [
            ("docker", "ps", "-q", "--filter", f"name={prefix}"),
            ("docker", "rm", "-f", "aabbccddeeff"),
        ]
        assert result.removed_containers == ("aabbccddeeff",)

    async def test_a_timeout_before_the_child_claims_leaves_the_queued_row_alone(
        self, db_session, db_session_factory, tmp_path, postgres_url
    ):
        task, result, _ = await self.timed_out_run(
            db_session, db_session_factory, tmp_path, postgres_url, {"hang": True}, FakeProcesses()
        )

        assert result.outcome is Outcome.TIMED_OUT and result.marked_failed is False
        await db_session.refresh(task)
        assert task.status is TaskStatus.QUEUED, "a row nobody claimed is not the runner's to fail"


@pytest.mark.anyio
@pytest.mark.db
class TestAbandoned:
    async def row(self, session, repo, status, started_ago: timedelta | None, instance="a", run=RUN):
        return await add_task(
            session, repo, eval_run_id=run, instance_id=instance, run_index=0, status=status,
            started_at=None if started_ago is None else datetime.now(timezone.utc) - started_ago,
        )

    async def test_a_stale_running_row_with_no_process_becomes_failed_never_queued(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-x")
        stale = await self.row(db_session, repo, TaskStatus.RUNNING, timedelta(hours=3))

        report = await mark_abandoned(db_session_factory, RUN, 600.0, process_runner=FakeProcesses())

        assert [t.task_id for t in report.marked] == [stale.id]
        await db_session.refresh(stale)
        assert stale.status is TaskStatus.FAILED
        assert stale.status is not TaskStatus.QUEUED
        assert stale.error_message == "runner: abandoned"
        assert stale.completed_at is not None

    async def test_a_row_still_inside_timeout_plus_grace_is_not_stale(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-x")
        inside = await self.row(db_session, repo, TaskStatus.RUNNING, timedelta(seconds=600 + ABANDONED_GRACE_SECONDS - 120))
        outside = await self.row(db_session, repo, TaskStatus.RUNNING, timedelta(seconds=600 + ABANDONED_GRACE_SECONDS + 120), instance="b")

        report = await mark_abandoned(db_session_factory, RUN, 600.0, process_runner=FakeProcesses())

        assert [t.task_id for t in report.marked] == [outside.id]
        await db_session.refresh(inside)
        assert inside.status is TaskStatus.RUNNING

    async def test_a_live_process_matching_the_task_id_protects_the_row(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-x")
        stale = await self.row(db_session, repo, TaskStatus.RUNNING, timedelta(hours=3))
        processes = FakeProcesses(pgrep=lambda task_id: ProcessResult(0, b"1234\n", b""))

        report = await mark_abandoned(db_session_factory, RUN, 600.0, process_runner=processes)

        assert report.marked == [] and [t.task_id for t in report.alive] == [stale.id]
        await db_session.refresh(stale)
        assert stale.status is TaskStatus.RUNNING
        assert processes.calls == [("pgrep", "-f", "--", str(stale.id))]

    @pytest.mark.parametrize("answer", [ProcessResult(2, b"", b"bad"), ProcessResult(3, b"", b""), ProcessResult(0, b"", b"", timed_out=True)])
    async def test_when_pgrep_cannot_answer_the_row_is_left_alone(self, db_session, db_session_factory, answer):
        repo = await add_repo(db_session, "repolace/bench-x")
        stale = await self.row(db_session, repo, TaskStatus.RUNNING, timedelta(hours=3))

        report = await mark_abandoned(db_session_factory, RUN, 600.0, process_runner=FakeProcesses(pgrep=lambda _: answer))

        assert report.marked == [] and [t.task_id for t in report.unknown] == [stale.id]
        await db_session.refresh(stale)
        assert stale.status is TaskStatus.RUNNING

    async def test_a_missing_pgrep_binary_leaves_the_row_alone(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-x")
        stale = await self.row(db_session, repo, TaskStatus.RUNNING, timedelta(hours=3))

        async def missing(*args, **kwargs):
            raise FileNotFoundError("pgrep")

        report = await mark_abandoned(db_session_factory, RUN, 600.0, process_runner=missing)

        assert [t.task_id for t in report.unknown] == [stale.id]
        await db_session.refresh(stale)
        assert stale.status is TaskStatus.RUNNING

    async def test_only_stale_running_rows_of_this_run_are_touched(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-x")
        other_run = await self.row(db_session, repo, TaskStatus.RUNNING, timedelta(hours=3), run="another-run")
        queued = await self.row(db_session, repo, TaskStatus.QUEUED, None, instance="q")
        completed = await self.row(db_session, repo, TaskStatus.COMPLETED, timedelta(hours=3), instance="c")
        failed = await self.row(db_session, repo, TaskStatus.FAILED, timedelta(hours=3), instance="f")

        report = await mark_abandoned(db_session_factory, RUN, 600.0, process_runner=FakeProcesses())

        assert report.marked == []
        for row, status in ((other_run, TaskStatus.RUNNING), (queued, TaskStatus.QUEUED),
                            (completed, TaskStatus.COMPLETED), (failed, TaskStatus.FAILED)):
            await db_session.refresh(row)
            assert row.status is status

    async def test_a_running_row_with_no_started_at_is_judged_by_its_creation_time(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-x")
        odd = await self.row(db_session, repo, TaskStatus.RUNNING, None)
        await db_session.execute(
            Task.__table__.update().where(Task.id == odd.id).values(created_at=ago(hours=3))
        )
        await db_session.commit()

        report = await mark_abandoned(db_session_factory, RUN, 600.0, process_runner=FakeProcesses())

        assert [t.task_id for t in report.marked] == [odd.id]

    def test_no_status_write_in_the_harness_ever_sets_queued(self):
        """A tripwire for the rule in the module docstring, beyond the behavioural tests above."""
        harness = Path(runner.__file__).parent
        for path in (harness / "runner.py", harness / "db.py"):
            source = path.read_text()
            assert not re.search(r"values\([^)]*TaskStatus\.QUEUED", source, re.S), path.name
            assert not re.search(r"status\s*=\s*['\"]queued['\"]", source), path.name


@pytest.mark.anyio
@pytest.mark.db
class TestMain:
    """`main` runs its own event loop, so it is driven from a thread against the test database."""

    def factory_seam(self, db_session_factory):
        @asynccontextmanager
        async def seam():
            yield db_session_factory

        return seam

    def inherit(self, monkeypatch, env: dict[str, str]) -> None:
        # `main` builds the child's environment from this process's, as it does for real.
        for name, value in env.items():
            monkeypatch.setenv(name, value)

    async def test_exit_zero_when_every_child_ran(self, db_session, db_session_factory, tmp_path, capsys, monkeypatch):
        repo = await add_repo(db_session, "repolace/bench-x")
        await add_task(db_session, repo, instance_id="a", run_index=0)
        self.inherit(monkeypatch, child_base_env(tmp_path))

        code = await asyncio.to_thread(
            main, ["--eval-run-id", RUN, "--runs-dir", str(tmp_path / "runs"), "-k", "1"],
            session_factory=self.factory_seam(db_session_factory), child_command=child_command(),
        )

        assert code == 0
        assert "dispatched 1: 1 ran" in capsys.readouterr().out

    async def test_exit_one_when_a_child_did_not_exit_zero(self, db_session, db_session_factory, tmp_path, capsys, monkeypatch):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)
        self.inherit(monkeypatch, child_base_env(tmp_path, {str(task.id): {"exit": 1}}))

        code = await asyncio.to_thread(
            main, ["--eval-run-id", RUN, "--runs-dir", str(tmp_path / "runs")],
            session_factory=self.factory_seam(db_session_factory), child_command=child_command(),
        )

        assert code == 1
        assert "repolace_failed" in capsys.readouterr().err

    async def test_mark_abandoned_is_a_mode_that_runs_nothing(self, db_session, db_session_factory, tmp_path, capsys):
        repo = await add_repo(db_session, "repolace/bench-x")
        queued = await add_task(db_session, repo, instance_id="q", run_index=0)
        stale = await add_task(
            db_session, repo, instance_id="s", run_index=0, status=TaskStatus.RUNNING, started_at=ago(hours=3)
        )

        code = await asyncio.to_thread(
            main, ["--eval-run-id", RUN, "--mark-abandoned", "--runs-dir", str(tmp_path / "runs")],
            session_factory=self.factory_seam(db_session_factory), child_command=child_command(),
            process_runner=FakeProcesses(),
        )

        assert code == 0
        assert "marked 1 abandoned" in capsys.readouterr().out
        await db_session.refresh(stale)
        await db_session.refresh(queued)
        assert stale.status is TaskStatus.FAILED
        assert queued.status is TaskStatus.QUEUED, "--mark-abandoned must not dispatch anything"
        assert not (tmp_path / "runs").exists()
