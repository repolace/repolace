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
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from eval_exec_support import (
    RECORDING_CHILD,
    TOKEN,
    add_llm_call,
    add_repo,
    add_task,
    child_command,
)
from harness import runner
from harness.bench_repos import TOKEN_ENV_VAR
from harness.db import queued_tasks, run_cost_usd
from harness.enqueue import (
    DEFAULT_WALL_CLOCK_SECONDS,
    ManifestError,
    ManifestInputs,
    build_manifest,
    ensure_manifest,
    manifest_path,
)
from harness.runner import (
    ABANDONED_GRACE_SECONDS,
    Outcome,
    RunnerConfig,
    allowlisted_env,
    build_parser,
    check_manifest,
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
from repolace_shared.paths import PathEscapesRoot
from repolace_shared.process import ProcessResult

RUN = "run-1"


def ago(**kwargs) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


class FakeProcesses:
    """A `process_runner` seam recording docker and pgrep invocations; answers are scripted."""

    def __init__(self, *, ps_output: bytes = b"", ps_returncode: int = 0, pgrep=None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[dict | None] = []
        self.ps_output = ps_output
        self.ps_returncode = ps_returncode
        self.pgrep = pgrep or (lambda task_id: ProcessResult(1, b"", b""))

    async def __call__(self, program: str, *args: str, **kwargs) -> ProcessResult:
        self.calls.append((program, *args))
        self.envs.append(kwargs.get("env"))
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
        "REPOLACE_CHILD_RECORD_DIR": str(records),
        "REPOLACE_CHILD_PLAN": json.dumps(plan or {}),
        TOKEN_ENV_VAR: TOKEN,
        "REPOLACE_CHILD_KEEP_ME": "kept",
        **extra,
    }
    if dsn is not None:
        env["REPOLACE_CHILD_DB_DSN"] = dsn.replace("+asyncpg", "")
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

    def test_the_child_environment_keeps_what_it_needs_and_drops_the_bench_token_and_provider_keys(self):
        base = {TOKEN_ENV_VAR: TOKEN, "DATABASE_URL": "postgres://x", "HF_HOME": "/hf", "ANTHROPIC_API_KEY": "k",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True"}

        env = child_env(base, None)

        assert env == {"DATABASE_URL": "postgres://x", "HF_HOME": "/hf", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
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


def write_manifest(runs_dir: Path, *, agent="llm", model="claude-test", timeout=DEFAULT_WALL_CLOCK_SECONDS, run=RUN) -> Path:
    inputs = ManifestInputs(runs_dir=runs_dir, agent=agent, model=model, timeout_seconds=timeout)
    document = build_manifest(
        eval_run_id=run, git_sha="a" * 40, instance_ids=["a"], runs=1, inputs=inputs,
        created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )
    path = manifest_path(runs_dir, run)
    ensure_manifest(path, document)
    return path


class TestManifestAgreement:
    """The manifest describes the sweep; a runner that disagrees with it would make that a lie."""

    def test_matching_flags_pass(self, tmp_path):
        write_manifest(tmp_path / "runs")

        assert check_manifest(tmp_path / "runs", config(model="claude-test")) is True

    def test_no_manifest_is_reported_not_refused(self, tmp_path):
        assert check_manifest(tmp_path / "runs", config(model="claude-test")) is False

    @pytest.mark.parametrize(
        ("override", "mentions"),
        [
            ({"model": "another-model"}, "model"),
            ({"model": None}, "model"),
            ({"timeout_seconds": 60.0}, "timeout"),
            ({"agent": "gold", "model": None}, "agent"),
        ],
    )
    def test_a_disagreeing_flag_is_refused_and_named(self, tmp_path, override, mentions):
        write_manifest(tmp_path / "runs")

        with pytest.raises(ManifestError, match=mentions):
            check_manifest(tmp_path / "runs", config(**{"model": "claude-test", **override}))

    def test_the_model_is_not_compared_for_an_agent_that_calls_none(self, tmp_path):
        write_manifest(tmp_path / "runs", agent="gold", model=None)

        assert check_manifest(tmp_path / "runs", config(agent="gold", model="whatever")) is True

    def test_a_malformed_manifest_is_refused(self, tmp_path):
        path = write_manifest(tmp_path / "runs")
        path.write_text("[]")

        with pytest.raises(ManifestError):
            check_manifest(tmp_path / "runs", config(model="claude-test"))

    def test_main_exits_two_on_a_mismatch_before_touching_the_database(self, tmp_path, capsys):
        write_manifest(tmp_path / "runs")

        @asynccontextmanager
        async def seam():
            # Opening the database is allowed (the check runs inside `run_queue`),
            # but nothing may be dispatched.
            from unittest import mock

            yield mock.MagicMock()

        code = main(
            ["--eval-run-id", RUN, "--runs-dir", str(tmp_path / "runs"), "--model", "wrong", "--max-total-usd", "100"],
            session_factory=seam, child_command=("/nonexistent",),
        )

        assert code == 2
        assert "does not match this invocation" in capsys.readouterr().err


@pytest.mark.anyio
@pytest.mark.db
class TestManifestAndDispatch:
    async def test_a_mismatch_dispatches_nothing_and_leaves_the_rows_queued(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        await add_task(db_session, repo, instance_id="a", run_index=0)
        write_manifest(tmp_path / "runs", model="claude-test")

        with pytest.raises(ManifestError):
            await run_queue(
                db_session_factory, config(model="other"), runs_dir=tmp_path / "runs",
                child_command=child_command(), base_env=child_base_env(tmp_path),
            )

        assert len(await queued_tasks(db_session_factory, RUN)) == 1
        assert records(tmp_path) == {}

    async def test_agreement_dispatches_and_reports_the_manifest_as_found(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        await add_task(db_session, repo, instance_id="a", run_index=0)
        write_manifest(tmp_path / "runs", model="claude-test")

        summary = await run_queue(
            db_session_factory, config(model="claude-test"), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        assert summary.manifest_found is True and len(summary.results) == 1

    async def test_no_manifest_still_dispatches_and_says_so(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        await add_task(db_session, repo, instance_id="a", run_index=0)

        summary = await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs",
            child_command=child_command(), base_env=child_base_env(tmp_path),
        )

        assert summary.manifest_found is False and len(summary.results) == 1


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
            main, ["--eval-run-id", RUN, "--runs-dir", str(tmp_path / "runs"), "-k", "1", "--max-total-usd", "100"],
            session_factory=self.factory_seam(db_session_factory), child_command=child_command(),
        )

        assert code == 0
        assert "dispatched 1: 1 ran" in capsys.readouterr().out

    async def test_exit_one_when_a_child_did_not_exit_zero(self, db_session, db_session_factory, tmp_path, capsys, monkeypatch):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)
        self.inherit(monkeypatch, child_base_env(tmp_path, {str(task.id): {"exit": 1}}))

        code = await asyncio.to_thread(
            main, ["--eval-run-id", RUN, "--runs-dir", str(tmp_path / "runs"), "--max-total-usd", "100"],
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


ALLOWED = {
    "PATH": "/usr/bin", "HOME": "/home/x", "USER": "x", "LOGNAME": "x", "LANG": "C.UTF-8", "LC_ALL": "C", "LC_CTYPE": "C",
    "TZ": "UTC", "TMPDIR": "/tmp", "XDG_RUNTIME_DIR": "/run/user/1000", "VIRTUAL_ENV": "/venv",
    "DOCKER_HOST": "unix:///run/user/1000/docker.sock", "DATABASE_URL": "postgresql://x", "GIT_SSL_CAINFO": "/ca",
    "HF_HOME": "/hf", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_CACHE": "/tf", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "SSL_CERT_FILE": "/ssl", "UV_CACHE_DIR": "/uv", "GATEWAY_MODELS_PATH": "/m.toml", "REPOLACE_ANYTHING": "1",
    "http_proxy": "p", "https_proxy": "p", "no_proxy": "n", "HTTP_PROXY": "p", "HTTPS_PROXY": "p", "NO_PROXY": "n",
}
LEAKS = {
    "GH_TOKEN": "ghs_exported_in_the_shell", "GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-x",
    "AWS_SECRET_ACCESS_KEY": "x", "AWS_ACCESS_KEY_ID": "x", "GITHUB_APP_PRIVATE_KEY_BASE64": "LS0t", "SSH_AUTH_SOCK": "/s",
    "GEMINI_API_KEY": "x", "REDIS_URL": "redis://x", "CELERY_BROKER_URL": "amqp://x", TOKEN_ENV_VAR: TOKEN,
}


class TestEnvironmentAllowlist:
    """The child and the docker/pgrep helpers see a named subset of the environment, never the whole shell."""

    def test_the_allowlisted_names_pass_and_nothing_else_does(self):
        env = child_env({**ALLOWED, **LEAKS}, None)

        assert env == ALLOWED

    def test_the_repository_tools_token_is_excluded_even_though_its_prefix_is_allowed(self):
        assert TOKEN_ENV_VAR.startswith("REPOLACE_")
        assert TOKEN_ENV_VAR not in child_env({TOKEN_ENV_VAR: TOKEN, "REPOLACE_OTHER": "1"}, None)
        assert TOKEN_ENV_VAR not in allowlisted_env({TOKEN_ENV_VAR: TOKEN})

    def test_names_that_merely_contain_an_allowed_prefix_are_not_allowed(self):
        env = child_env({"MY_HF_TOKEN": "x", "XLC_ALL": "x", "AWS_UV_KEY": "x", "ghs_GATEWAY_": "x"}, None)

        assert env == {}

    def test_the_model_is_added_after_filtering(self):
        env = child_env({**LEAKS}, "claude-test")

        assert env == {"GATEWAY_STAGE_MODELS": json.dumps({"agent": "claude-test"})}

    @pytest.mark.anyio
    @pytest.mark.db
    async def test_an_exported_secret_never_reaches_a_running_child(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)

        await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs", child_command=child_command(),
            base_env={**child_base_env(tmp_path), **LEAKS, "HF_HOME": "/hf"},
        )

        names = set(records(tmp_path)[str(task.id)]["env_names"])
        assert not names & set(LEAKS), sorted(names & set(LEAKS))
        assert {"PATH", "HF_HOME", "REPOLACE_CHILD_RECORD_DIR"} <= names

    @pytest.mark.anyio
    @pytest.mark.db
    async def test_docker_and_pgrep_get_the_allowlisted_environment_too(self, db_session, db_session_factory, tmp_path, monkeypatch):
        repo = await add_repo(db_session, "repolace/bench-x")
        await add_task(db_session, repo, instance_id="fresh", run_index=0, status=TaskStatus.QUEUED)
        stale = await add_task(
            db_session, repo, instance_id="stale", run_index=1, status=TaskStatus.RUNNING, started_at=ago(hours=3)
        )
        for name, value in LEAKS.items():
            monkeypatch.setenv(name, value)
        processes = FakeProcesses(ps_output=b"aabbccddeeff\n")

        await mark_abandoned(db_session_factory, RUN, 600.0, process_runner=processes)
        await remove_task_containers(stale.id, process_runner=processes, env=allowlisted_env(os.environ))

        assert processes.envs and all(env is not None for env in processes.envs)
        for env in processes.envs:
            assert not set(env) & set(LEAKS), sorted(set(env) & set(LEAKS))


@pytest.mark.anyio
@pytest.mark.db
class TestReportingGaps:
    async def test_a_child_that_exits_zero_with_its_row_still_running_makes_the_run_unhealthy(
        self, db_session, db_session_factory, tmp_path, postgres_url
    ):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)
        env = child_base_env(tmp_path, {str(task.id): {"claim": True}}, dsn=postgres_url)  # claims, exits 0, never finishes

        summary = await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs", child_command=child_command(), base_env=env
        )

        (result,) = summary.results
        assert result.outcome is Outcome.RAN and result.marked_failed is True
        assert summary.healthy is False
        await db_session.refresh(task)
        assert task.status is TaskStatus.FAILED

    async def test_main_exits_one_and_names_the_task(self, db_session, db_session_factory, tmp_path, postgres_url, capsys, monkeypatch):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)
        for name, value in child_base_env(tmp_path, {str(task.id): {"claim": True}}, dsn=postgres_url).items():
            monkeypatch.setenv(name, value)

        @asynccontextmanager
        async def seam():
            yield db_session_factory

        code = await asyncio.to_thread(
            main, ["--eval-run-id", RUN, "--runs-dir", str(tmp_path / "runs"), "--max-total-usd", "100"],
            session_factory=seam, child_command=child_command(),
        )

        assert code == 1
        err = capsys.readouterr().err
        assert "left its row RUNNING (now FAILED)" in err and str(task.id) in err

    async def test_a_clean_run_is_still_healthy(self, db_session, db_session_factory, tmp_path, postgres_url):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)
        env = child_base_env(tmp_path, {str(task.id): {"claim": True, "finish": "completed"}}, dsn=postgres_url)

        summary = await run_queue(
            db_session_factory, config(), runs_dir=tmp_path / "runs", child_command=child_command(), base_env=env
        )

        assert summary.healthy is True


class TestSpendLimitIsRequired:
    def boom_factory(self):
        @asynccontextmanager
        async def seam():
            raise RuntimeError("the database was opened")
            yield  # pragma: no cover

        return seam

    def test_the_llm_agent_needs_max_total_usd(self, tmp_path, capsys):
        code = main(["--eval-run-id", RUN, "--runs-dir", str(tmp_path)], session_factory=self.boom_factory())

        assert code == 2
        assert "--max-total-usd" in capsys.readouterr().err

    @pytest.mark.parametrize("extra", [["--agent", "gold", "--no-pr"], ["--mark-abandoned"]])
    def test_gold_and_the_abandoned_sweep_do_not(self, tmp_path, extra):
        with pytest.raises(RuntimeError, match="the database was opened"):
            main(["--eval-run-id", RUN, "--runs-dir", str(tmp_path), *extra], session_factory=self.boom_factory())

    def test_with_the_cap_the_llm_agent_proceeds(self, tmp_path):
        with pytest.raises(RuntimeError, match="the database was opened"):
            main(
                ["--eval-run-id", RUN, "--runs-dir", str(tmp_path), "--max-total-usd", "5"],
                session_factory=self.boom_factory(),
            )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def wait_until(predicate, timeout=30.0, step=0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(step)
    return False


@pytest.mark.anyio
@pytest.mark.db
class TestInterruption:
    """A killed or hung-up runner must not leave children spending and rows RUNNING."""

    async def start(self, db_session, repo_name="repolace/bench-x"):
        repo = await add_repo(db_session, repo_name)
        return await add_task(db_session, repo, instance_id="a", run_index=0)

    async def claimed(self, db_session, task) -> bool:
        await db_session.refresh(task)
        return task.status is TaskStatus.RUNNING

    async def assert_children_gone(self, tmp_path, task) -> None:
        record = records(tmp_path)[str(task.id)]
        grandchild = int((tmp_path / "records" / f"{task.id}.grandchild").read_text())
        gone = await wait_until(lambda: asyncio.sleep(0, result=not alive(record["pid"]) and not alive(grandchild)), 10)
        if not gone:
            for pid in (record["pid"], grandchild):
                if alive(pid):
                    os.kill(pid, signal.SIGKILL)
        assert gone, "the child's process group survived the interrupt"

    async def test_cancelling_the_run_kills_the_children_settles_their_rows_and_removes_their_containers(
        self, db_session, db_session_factory, tmp_path, postgres_url
    ):
        task = await self.start(db_session)
        env = child_base_env(tmp_path, {str(task.id): {"claim": True, "grandchild": True, "hang": True}}, dsn=postgres_url)
        processes = FakeProcesses(ps_output=b"aabbccddeeff\n")
        running = asyncio.create_task(
            run_queue(
                db_session_factory, config(timeout_seconds=300), runs_dir=tmp_path / "runs",
                child_command=child_command(), base_env=env, process_runner=processes,
            )
        )
        assert await wait_until(lambda: self.claimed(db_session, task)), "the child never claimed its row"

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        await self.assert_children_gone(tmp_path, task)
        await db_session.refresh(task)
        assert task.status is TaskStatus.FAILED and task.error_message == "runner: interrupted"
        prefix = f"repolace-{task.id.hex[:12]}"
        assert ("docker", "ps", "-q", "--filter", f"name={prefix}") in processes.calls
        assert ("docker", "rm", "-f", "aabbccddeeff") in processes.calls

    async def test_an_interrupt_leaves_a_row_that_was_never_claimed_queued(
        self, db_session, db_session_factory, tmp_path
    ):
        task = await self.start(db_session)
        env = child_base_env(tmp_path, {str(task.id): {"hang": True}})  # never claims
        running = asyncio.create_task(
            run_queue(
                db_session_factory, config(timeout_seconds=300), runs_dir=tmp_path / "runs",
                child_command=child_command(), base_env=env, process_runner=FakeProcesses(),
            )
        )
        assert await wait_until(lambda: asyncio.sleep(0, result=bool(records(tmp_path)))), "the child never started"

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        await db_session.refresh(task)
        assert task.status is TaskStatus.QUEUED, "a row nobody claimed is not the runner's to fail"

    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
    async def test_a_real_runner_process_given_the_signal_kills_its_children(
        self, db_session, tmp_path, postgres_url, signum
    ):
        """The shipped `main` in a real process: the signal must reach `_run_child`, not just end Python."""
        task = await self.start(db_session)
        driver = tmp_path / "driver.py"
        driver.write_text(
            "import json, os, pathlib, sys\n"
            "from harness.runner import main\n"
            "from repolace_shared.process import ProcessResult\n"
            "calls = pathlib.Path(os.environ['DRIVER_CALLS'])\n"
            "async def fake(program, *args, **kwargs):\n"
            "    with calls.open('a') as handle:\n"
            "        handle.write(json.dumps([program, *args]) + '\\n')\n"
            "    return ProcessResult(0, b'', b'')\n"
            "sys.exit(main(sys.argv[1:], child_command=(sys.executable, '-c', os.environ['DRIVER_CHILD']), process_runner=fake))\n"
        )
        env = child_base_env(tmp_path, {str(task.id): {"claim": True, "grandchild": True, "hang": True}}, dsn=postgres_url)
        env.update({
            "DATABASE_URL": postgres_url, "DRIVER_CALLS": str(tmp_path / "calls.jsonl"), "DRIVER_CHILD": RECORDING_CHILD,
        })
        out = (tmp_path / "driver.out").open("w")
        process = subprocess.Popen(
            [sys.executable, str(driver), "--eval-run-id", RUN, "--runs-dir", str(tmp_path / "runs"),
             "--max-total-usd", "100", "--timeout-seconds", "300", "-k", "1"],
            env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            assert await wait_until(lambda: self.claimed(db_session, task)), (tmp_path / "driver.out").read_text()[-2000:]

            process.send_signal(signum)
            code = await asyncio.to_thread(process.wait, 30)
        finally:
            if process.poll() is None:
                process.kill()
            out.close()

        assert code == 128 + signum, (tmp_path / "driver.out").read_text()[-2000:]
        await self.assert_children_gone(tmp_path, task)
        await db_session.refresh(task)
        assert task.status is TaskStatus.FAILED and task.error_message == "runner: interrupted"
        calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert ["docker", "ps", "-q", "--filter", f"name=repolace-{task.id.hex[:12]}"] in calls
        assert f"interrupted by {signum.name}" in (tmp_path / "driver.out").read_text()


@pytest.mark.anyio
@pytest.mark.db
class TestPathConfinement:
    """Every path built from a run id or a task id goes through `resolve_within`."""

    async def test_a_symlinked_run_directory_is_refused(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        await add_task(db_session, repo, instance_id="a", run_index=0)
        runs = tmp_path / "runs"
        runs.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (runs / RUN).symlink_to(elsewhere)

        with pytest.raises(ValueError, match="symlink"):
            await run_queue(
                db_session_factory, config(), runs_dir=runs, child_command=child_command(), base_env=child_base_env(tmp_path)
            )

        assert list(elsewhere.iterdir()) == [], "nothing may be written through the link"
        assert len(await queued_tasks(db_session_factory, RUN)) == 1

    async def test_a_symlinked_log_file_is_refused_not_written_through(self, db_session, db_session_factory, tmp_path):
        repo = await add_repo(db_session, "repolace/bench-x")
        task = await add_task(db_session, repo, instance_id="a", run_index=0)
        run_dir = tmp_path / "runs" / RUN
        run_dir.mkdir(parents=True)
        victim = tmp_path / "victim.txt"
        victim.write_text("precious")
        (run_dir / f"{task.id}.log").symlink_to(victim)

        with pytest.raises(PathEscapesRoot):
            await run_queue(
                db_session_factory, config(), runs_dir=tmp_path / "runs", child_command=child_command(),
                base_env=child_base_env(tmp_path),
            )

        assert victim.read_text() == "precious"
        assert records(tmp_path) == {}, "no child may be started when its log path is refused"
