"""`repolace-eval enqueue`: rows, idempotency, and the errors that must not be skips.

The DB-backed tests are the point. A benchmark row with a wrong field is scored
as the agent's failure, a duplicate row is scored twice, and an instance that
silently went missing is absent from the headline without anything having failed.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from eval_exec_support import add_repo, make_instance, run_git_sync, write_instances
from harness.bench_repos import load_bench_entries, load_bench_repos
from harness.enqueue import (
    DEFAULT_WALL_CLOCK_SECONDS,
    UNBOUNDED_STAGES_SLACK_SECONDS,
    EnqueueError,
    ManifestInputs,
    build_parser,
    code_limits,
    enqueue_tasks,
    main,
    task_wall_clock_bound,
)
from harness.run_manifest import ManifestError, load_manifest, manifest_path
from repolace_shared.db.models import Task, TaskStatus
from sqlalchemy.exc import DBAPIError

FIXED_NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def read_manifest(path: Path) -> dict:
    """The manifest as it is on disk, for asserting on the keys the report will read."""
    return json.loads(path.read_text())


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A small git repository standing in for the repolace checkout, so no test reads the real one."""
    path = tmp_path / "repolace-checkout"
    path.mkdir()
    run_git_sync("init", "-q", "-b", "main", cwd=path)
    (path / "tracked").write_text("v1\n")
    run_git_sync("add", "tracked", cwd=path)
    run_git_sync("commit", "-q", "-m", "one", cwd=path)
    return path


@pytest.fixture
def inputs(tmp_path: Path, repo_root: Path) -> ManifestInputs:
    return ManifestInputs(runs_dir=tmp_path / "runs", repo_root=repo_root, model="claude-test", clock=lambda: FIXED_NOW)


def instances_for(*ids: str):
    return {i: make_instance(i, issue_number=n + 1, problem_statement=f"Title of {i}\n\nBody of {i}.") for n, i in enumerate(ids)}


def bench(*ids: str) -> dict[str, str]:
    return {i: f"repolace/bench-{i}" for i in ids}


async def enqueue(factory, manifest, ids=("a", "b"), *, repos=None, **kwargs):
    kwargs["manifest"] = manifest
    kwargs.setdefault("eval_run_id", "run-1")
    kwargs.setdefault("instances_requested", "all")
    kwargs.setdefault("runs", 3)
    return await enqueue_tasks(factory, instances_for(*ids), bench(*(repos if repos is not None else ids)), **kwargs)


@pytest.mark.anyio
@pytest.mark.db
class TestRows:
    @pytest.fixture(autouse=True)
    def _wire(self, inputs):
        self.inputs = inputs

    async def enqueue(self, factory, **kwargs):
        ids = kwargs.pop("ids", ("a", "b"))
        return await enqueue(factory, self.inputs, ids, **kwargs)

    async def seed_repos(self, session, *ids):
        return {i: await add_repo(session, f"repolace/bench-{i}") for i in ids}

    async def all_tasks(self, session):
        return (await session.execute(select(Task).order_by(Task.instance_id, Task.run_index))).scalars().all()

    async def test_creates_one_queued_row_per_instance_and_run_with_the_right_fields(self, db_session, db_session_factory):
        repos = await self.seed_repos(db_session, "a", "b")

        result = await self.enqueue(db_session_factory, runs=3, open_pr_on_failure=True)

        assert (result.created, result.already_present) == (6, 0)
        rows = await self.all_tasks(db_session)
        assert [(r.instance_id, r.run_index) for r in rows] == [(i, k) for i in "ab" for k in range(3)]
        a0 = rows[0]
        assert a0.repo_id == repos["a"].id
        assert a0.issue_number == 1
        assert a0.issue_title == "Title of a"
        assert a0.issue_body == "Title of a\n\nBody of a."
        assert a0.target_branch == "main"
        assert a0.eval_run_id == "run-1"
        assert a0.status is TaskStatus.QUEUED
        assert a0.open_pr_on_failure is True
        assert a0.outcome is None and a0.started_at is None and a0.completed_at is None
        assert a0.retry_count == 0

    async def test_open_pr_on_failure_defaults_off(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")

        await self.enqueue(db_session_factory, ids=("a",), runs=1)

        (row,) = await self.all_tasks(db_session)
        assert row.open_pr_on_failure is False

    async def test_the_issue_url_is_the_bench_repository_and_names_no_upstream(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "psf__requests-2317")

        await self.enqueue(db_session_factory, ids=("psf__requests-2317",), runs=1)

        (row,) = await self.all_tasks(db_session)
        assert row.issue_url == "https://github.com/repolace/bench-psf__requests-2317"
        assert "github.com/psf" not in row.issue_url
        assert "psf/requests" not in row.issue_url

    async def test_a_rerun_creates_nothing_and_keeps_the_same_rows(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a", "b")
        await self.enqueue(db_session_factory)
        before = {r.id for r in await self.all_tasks(db_session)}

        again = await self.enqueue(db_session_factory)

        assert (again.created, again.already_present) == (0, 6)
        assert {r.id for r in await self.all_tasks(db_session)} == before

    async def test_a_rerun_asking_for_more_runs_is_a_different_sweep_and_is_refused(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a", "b")
        await self.enqueue(db_session_factory, runs=2)

        with pytest.raises(ManifestError, match="runs_per_instance"):
            await self.enqueue(db_session_factory, runs=3)

        count = await db_session.scalar(select(func.count()).select_from(Task))
        assert count == 4, "a refused sweep must not insert anything"

    async def test_a_rerun_does_not_touch_a_row_that_has_already_moved_on(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")
        await self.enqueue(db_session_factory, ids=("a",), runs=1)
        row = (await self.all_tasks(db_session))[0]
        row.status = TaskStatus.COMPLETED
        await db_session.commit()

        again = await self.enqueue(db_session_factory, ids=("a",), runs=1)

        assert again.created == 0
        await db_session.refresh(row)
        assert row.status is TaskStatus.COMPLETED

    async def test_a_different_run_id_is_a_separate_run(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")
        await self.enqueue(db_session_factory, ids=("a",), runs=1, eval_run_id="run-1")

        other = await self.enqueue(db_session_factory, ids=("a",), runs=1, eval_run_id="run-2")

        assert other.created == 1

    async def test_a_subset_enqueues_only_the_chosen_instances(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a", "b")

        await self.enqueue(db_session_factory, instances_requested="b", runs=1)

        assert [r.instance_id for r in await self.all_tasks(db_session)] == ["b"]

    async def test_a_missing_registered_repo_is_an_error_that_says_to_resync_and_inserts_nothing(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")  # `b` has no registered_repos row

        with pytest.raises(EnqueueError) as raised:
            await self.enqueue(db_session_factory)

        message = str(raised.value)
        assert "repolace/bench-b" in message and "Re-sync" in message
        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_every_problem_is_reported_at_once(self, db_session, db_session_factory):
        with pytest.raises(EnqueueError) as raised:
            await self.enqueue(db_session_factory)

        message = str(raised.value)
        assert "repolace/bench-a" in message and "repolace/bench-b" in message

    async def test_an_inactive_registered_repo_is_refused(self, db_session, db_session_factory):
        await add_repo(db_session, "repolace/bench-a", is_active=False)

        with pytest.raises(EnqueueError, match="inactive"):
            await self.enqueue(db_session_factory, ids=("a",))

        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_an_instance_with_no_bench_repo_entry_is_refused_and_names_fork(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a", "b")

        with pytest.raises(EnqueueError, match="repolace-eval fork"):
            await self.enqueue(db_session_factory, ids=("a", "b"), repos=("a",))

        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_an_unknown_instance_is_refused(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")

        with pytest.raises(EnqueueError, match="not an instance"):
            await self.enqueue(db_session_factory, ids=("a",), instances_requested="a,nope")

    async def test_an_instance_with_no_usable_title_is_refused(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")
        blank = {"a": make_instance("a", problem_statement="\n   \n")}

        with pytest.raises(EnqueueError, match="title"):
            await enqueue_tasks(
                db_session_factory, blank, bench("a"), eval_run_id="run-1", instances_requested="all", runs=1,
                manifest=self.inputs,
            )

    @pytest.mark.parametrize("run_id", ["", "../x", "a/b", "a b", "-x", ".x", "x" * 65, "a\n", "a..b"])
    async def test_a_hostile_run_id_is_refused_before_anything_is_inserted(self, db_session, db_session_factory, run_id):
        await self.seed_repos(db_session, "a")

        with pytest.raises(EnqueueError):
            await self.enqueue(db_session_factory, ids=("a",), eval_run_id=run_id)

        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    @pytest.mark.parametrize("runs", [0, -1, True])
    async def test_runs_must_be_a_positive_integer(self, db_session, db_session_factory, runs):
        await self.seed_repos(db_session, "a")

        with pytest.raises(EnqueueError, match="--runs"):
            await self.enqueue(db_session_factory, ids=("a",), runs=runs)


@pytest.mark.anyio
@pytest.mark.db
class TestManifest:
    """`eval/runs/<run>/manifest.json`: what the report checks the database against."""

    @pytest.fixture(autouse=True)
    def _wire(self, inputs):
        self.inputs = inputs

    async def run(self, factory, *, inputs=None, ids=("a", "b"), runs=2, run_id="run-1", **kwargs):
        return await enqueue(
            factory, inputs or self.inputs, ids, eval_run_id=run_id, runs=runs, **kwargs
        )

    def path(self, run_id="run-1") -> Path:
        return manifest_path(self.inputs.runs_dir, run_id)

    async def seed(self, session):
        for instance_id in ("a", "b"):
            await add_repo(session, f"repolace/bench-{instance_id}")

    async def test_it_has_exactly_the_agreed_keys(self, db_session, db_session_factory):
        await self.seed(db_session)

        await self.run(db_session_factory)

        document = json.loads(self.path().read_text())
        assert set(document) == {
            "eval_run_id", "created_at", "git_sha", "model", "stage_models", "limits",
            "runs_per_instance", "instance_ids", "agent",
        }

    async def test_its_values_describe_the_sweep(self, db_session, db_session_factory, repo_root):
        await self.seed(db_session)

        result = await self.run(db_session_factory, runs=3, instances_requested="a,b")

        document = read_manifest(self.path())
        assert result.manifest_created is True
        assert document["eval_run_id"] == "run-1"
        assert document["created_at"] == "2026-10-03T12:00:00+00:00"
        assert datetime.fromisoformat(document["created_at"]).utcoffset() == timedelta(0)
        assert document["git_sha"] == run_git_sync("rev-parse", "HEAD", cwd=repo_root)
        assert document["model"] == "claude-test"
        assert document["stage_models"] == {"agent": "claude-test"}
        assert document["runs_per_instance"] == 3
        assert document["instance_ids"] == ["a", "b"]
        assert document["agent"] == "llm"

    async def test_the_limits_are_the_ones_in_code_plus_the_runners_wall_clock(self, db_session, db_session_factory):
        from repolace_agents.contracts import AgentLimits
        from repolace_gateway.budget import DEFAULT_MAX_CALLS, DEFAULT_MAX_USD, DEFAULT_MAX_WALL_SECONDS

        await self.seed(db_session)

        await self.run(db_session_factory)

        limits = read_manifest(self.path())["limits"]
        assert limits == {
            "task_cost_cap_usd": float(DEFAULT_MAX_USD),
            "task_call_cap": DEFAULT_MAX_CALLS,
            "budget_wall_clock_seconds": float(DEFAULT_MAX_WALL_SECONDS),
            "max_steps_per_attempt": AgentLimits().max_steps_per_attempt,
            "max_attempts": AgentLimits().max_attempts,
            "runner_wall_clock_seconds": DEFAULT_WALL_CLOCK_SECONDS,
        }
        assert limits["task_cost_cap_usd"] == 2.0 and limits["max_attempts"] == 3 and limits["max_steps_per_attempt"] == 40
        assert set(limits) >= set(code_limits())

    async def test_the_instance_ids_are_sorted_and_unique(self, db_session, db_session_factory):
        await self.seed(db_session)

        await self.run(db_session_factory, instances_requested="b,a,b")

        assert read_manifest(self.path())["instance_ids"] == ["a", "b"]

    async def test_it_is_written_before_any_row_exists(self, db_session, db_session_factory):
        """A database failure on the insert leaves the manifest and no rows, never the reverse."""
        await self.seed(db_session)

        class FailingInsert:
            def __call__(self):
                session = db_session_factory()
                execute = session.execute

                async def guarded(statement, *args, **kwargs):
                    if "INSERT INTO tasks" in str(statement):
                        raise RuntimeError("database fell over")
                    return await execute(statement, *args, **kwargs)

                session.execute = guarded
                return session

        with pytest.raises(RuntimeError, match="fell over"):
            await self.run(FailingInsert())

        assert self.path().exists()
        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_a_rerun_with_the_same_inputs_leaves_the_file_byte_identical(self, db_session, db_session_factory):
        await self.seed(db_session)
        await self.run(db_session_factory)
        before = self.path().read_bytes()
        later = ManifestInputs(
            runs_dir=self.inputs.runs_dir, repo_root=self.inputs.repo_root, model="claude-test",
            clock=lambda: FIXED_NOW + timedelta(days=3),
        )

        result = await self.run(db_session_factory, inputs=later)

        assert result.manifest_created is False
        assert self.path().read_bytes() == before, "the original creation time must survive a re-run"

    @pytest.mark.parametrize(
        ("change", "differs"),
        [
            ({"runs": 3}, "runs_per_instance"),
            ({"ids": ("a",)}, "instance_ids"),
            ({"model": "another-model"}, "model"),
            ({"timeout_seconds": 100.0}, "limits"),
        ],
    )
    async def test_a_different_sweep_under_the_same_run_id_is_refused_and_inserts_nothing(
        self, db_session, db_session_factory, change, differs
    ):
        await self.seed(db_session)
        await self.run(db_session_factory)
        rows_before = await db_session.scalar(select(func.count()).select_from(Task))
        before = self.path().read_bytes()
        kwargs = dict(change)
        replaced = ManifestInputs(
            runs_dir=self.inputs.runs_dir, repo_root=self.inputs.repo_root,
            model=kwargs.pop("model", "claude-test"), timeout_seconds=kwargs.pop("timeout_seconds", DEFAULT_WALL_CLOCK_SECONDS),
            clock=lambda: FIXED_NOW,
        )

        with pytest.raises(ManifestError, match=differs):
            await self.run(db_session_factory, inputs=replaced, **kwargs)

        assert await db_session.scalar(select(func.count()).select_from(Task)) == rows_before
        assert self.path().read_bytes() == before

    async def test_a_new_commit_is_a_different_sweep(self, db_session, db_session_factory, repo_root):
        await self.seed(db_session)
        await self.run(db_session_factory)
        (repo_root / "tracked").write_text("v2\n")
        run_git_sync("commit", "-q", "-am", "two", cwd=repo_root)

        with pytest.raises(ManifestError, match="git_sha"):
            await self.run(db_session_factory)

    async def test_a_different_agent_is_a_different_sweep(self, db_session, db_session_factory):
        await self.seed(db_session)
        await self.run(db_session_factory)
        gold = ManifestInputs(runs_dir=self.inputs.runs_dir, repo_root=self.inputs.repo_root, agent="gold", clock=lambda: FIXED_NOW)

        with pytest.raises(ManifestError, match="agent"):
            await self.run(db_session_factory, inputs=gold)

    async def test_a_second_run_id_has_its_own_manifest(self, db_session, db_session_factory):
        await self.seed(db_session)
        await self.run(db_session_factory, run_id="run-1")

        await self.run(db_session_factory, run_id="run-2", runs=5)

        assert read_manifest(self.path("run-1"))["runs_per_instance"] == 2
        assert read_manifest(self.path("run-2"))["runs_per_instance"] == 5

    async def test_no_temporary_file_is_left_beside_it(self, db_session, db_session_factory):
        await self.seed(db_session)

        await self.run(db_session_factory)

        assert [p.name for p in self.path().parent.iterdir()] == ["manifest.json"]

    async def test_an_unreadable_existing_manifest_is_refused_not_overwritten(self, db_session, db_session_factory):
        await self.seed(db_session)
        self.path().parent.mkdir(parents=True)
        self.path().write_text("{not json")

        with pytest.raises(ManifestError, match="not valid JSON"):
            await self.run(db_session_factory)

        assert self.path().read_text() == "{not json"
        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_a_manifest_with_the_wrong_keys_is_refused(self, db_session, db_session_factory):
        await self.seed(db_session)
        self.path().parent.mkdir(parents=True)
        self.path().write_text(json.dumps({"eval_run_id": "run-1"}))

        with pytest.raises(ManifestError, match="missing key"):
            await self.run(db_session_factory)

    async def test_a_resolution_failure_writes_no_manifest(self, db_session, db_session_factory):
        # No registered repositories at all.
        with pytest.raises(EnqueueError, match="Re-sync"):
            await self.run(db_session_factory)

        assert not self.path().exists()

    async def test_a_modified_tracked_tree_is_refused(self, db_session, db_session_factory, repo_root):
        await self.seed(db_session)
        (repo_root / "tracked").write_text("uncommitted\n")

        with pytest.raises(ManifestError, match="modified"):
            await self.run(db_session_factory)

        assert not self.path().exists()
        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_the_allow_flag_records_the_sha_of_a_modified_tree(self, db_session, db_session_factory, repo_root):
        await self.seed(db_session)
        (repo_root / "tracked").write_text("uncommitted\n")
        dirty_ok = ManifestInputs(
            runs_dir=self.inputs.runs_dir, repo_root=repo_root, model="claude-test", allow_dirty_tree=True,
            clock=lambda: FIXED_NOW,
        )

        await self.run(db_session_factory, inputs=dirty_ok)

        assert read_manifest(self.path())["git_sha"] == run_git_sync("rev-parse", "HEAD", cwd=repo_root)

    async def test_untracked_files_do_not_count_as_a_modified_tree(self, db_session, db_session_factory, repo_root):
        await self.seed(db_session)
        (repo_root / "scratch.txt").write_text("not tracked\n")

        await self.run(db_session_factory)

        assert self.path().exists()

    async def test_the_llm_agent_needs_a_model(self, db_session, db_session_factory):
        await self.seed(db_session)
        nameless = ManifestInputs(runs_dir=self.inputs.runs_dir, repo_root=self.inputs.repo_root, model=None, clock=lambda: FIXED_NOW)

        with pytest.raises(ManifestError, match="--model"):
            await self.run(db_session_factory, inputs=nameless)

        assert not self.path().exists()

    @pytest.mark.parametrize("agent", ["gold", "stub"])
    async def test_an_agent_that_calls_no_model_records_none_and_no_stage_models(self, db_session, db_session_factory, agent):
        await self.seed(db_session)
        no_model = ManifestInputs(runs_dir=self.inputs.runs_dir, repo_root=self.inputs.repo_root, agent=agent, clock=lambda: FIXED_NOW)

        await self.run(db_session_factory, inputs=no_model)

        document = read_manifest(self.path())
        assert (document["agent"], document["model"], document["stage_models"]) == (agent, "none", {})

    async def test_an_unknown_agent_is_refused(self, db_session, db_session_factory):
        await self.seed(db_session)
        odd = ManifestInputs(runs_dir=self.inputs.runs_dir, repo_root=self.inputs.repo_root, agent="human", model="m", clock=lambda: FIXED_NOW)

        with pytest.raises(ManifestError, match="--agent"):
            await self.run(db_session_factory, inputs=odd)

    async def test_a_hostile_run_id_cannot_place_the_manifest_outside_the_runs_directory(self, db_session, db_session_factory):
        await self.seed(db_session)

        with pytest.raises(EnqueueError):
            await self.run(db_session_factory, run_id="../escape")

        assert not (self.inputs.runs_dir.parent / "escape").exists()


@pytest.mark.anyio
@pytest.mark.db
class TestReportLoaderAndInputChecks:
    """Stream D's `run_manifest` is the one definition; enqueue writes through it, and checks input the database cannot take."""

    @pytest.fixture(autouse=True)
    def _wire(self, inputs):
        self.inputs = inputs

    async def run(self, factory, instances=None, **kwargs):
        kwargs.setdefault("eval_run_id", "run-1")
        kwargs.setdefault("runs", 2)
        instances = instances if instances is not None else instances_for("a", "b")
        return await enqueue_tasks(
            factory, instances, bench("a", "b"), manifest=self.inputs, instances_requested="all", **kwargs
        )

    async def seed(self, session):
        for instance_id in ("a", "b"):
            await add_repo(session, f"repolace/bench-{instance_id}")

    async def test_a_manifest_enqueue_wrote_loads_through_the_report_loader(self, db_session, db_session_factory, repo_root):
        await self.seed(db_session)

        await self.run(db_session_factory)

        loaded = load_manifest(manifest_path(self.inputs.runs_dir, "run-1"))
        assert loaded.eval_run_id == "run-1" and loaded.agent == "llm" and loaded.model == "claude-test"
        assert loaded.instance_ids == ("a", "b") and loaded.runs_per_instance == 2
        assert loaded.git_sha == run_git_sync("rev-parse", "HEAD", cwd=repo_root)
        assert loaded.stage_models == {"agent": "claude-test"}
        assert loaded.limits["runner_wall_clock_seconds"] == DEFAULT_WALL_CLOCK_SECONDS
        assert loaded.planned_pairs() == {(i, r) for i in ("a", "b") for r in (0, 1)}

    async def test_a_nul_in_a_problem_statement_is_refused_before_anything_is_written(self, db_session, db_session_factory):
        await self.seed(db_session)
        poisoned = {**instances_for("a", "b"), "b": make_instance("b", problem_statement="Title\n\nbad \x00 byte")}

        with pytest.raises(EnqueueError, match=r"b: the problem statement contains a NUL"):
            await self.run(db_session_factory, poisoned)

        assert not manifest_path(self.inputs.runs_dir, "run-1").exists(), "nothing may be written for a refused sweep"
        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    @pytest.mark.parametrize("char", ["\x00", "\x01", "\x07", "\x0b", "\x0c", "\x1b", "\x7f"])
    async def test_every_control_character_is_refused(self, db_session, db_session_factory, char):
        await self.seed(db_session)
        poisoned = {"a": make_instance("a", problem_statement=f"Title\nbody {char} body"), "b": instances_for("b")["b"]}

        with pytest.raises(EnqueueError, match="control character"):
            await self.run(db_session_factory, poisoned)

    async def test_tabs_newlines_and_carriage_returns_are_fine(self, db_session, db_session_factory):
        await self.seed(db_session)
        fine = {i: make_instance(i, problem_statement="Title\r\n\tindented\nline") for i in ("a", "b")}

        result = await self.run(db_session_factory, fine)

        assert result.created == 4

    async def test_a_database_error_becomes_an_enqueue_error_that_leaks_nothing_and_keeps_the_manifest(
        self, db_session, db_session_factory
    ):
        await self.seed(db_session)

        class FailingInsert:
            def __call__(self):
                session = db_session_factory()
                execute = session.execute

                async def guarded(statement, *args, **kwargs):
                    if "INSERT INTO tasks" in str(statement):
                        raise DBAPIError(
                            "INSERT INTO tasks ... VALUES (SECRET-PROBLEM-TEXT)", {"issue_body": "SECRET-PROBLEM-TEXT"},
                            Exception("integrity constraint boom\nsecond line"),
                        )
                    return await execute(statement, *args, **kwargs)

                session.execute = guarded
                return session

        with pytest.raises(EnqueueError) as raised:
            await self.run(FailingInsert())

        message = str(raised.value)
        assert "integrity constraint boom" in message and "second line" not in message
        assert "SECRET-PROBLEM-TEXT" not in message and "INSERT INTO" not in message
        assert "manifest was already written" in message
        assert raised.value.__cause__ is None and raised.value.__suppress_context__
        assert manifest_path(self.inputs.runs_dir, "run-1").exists()
        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_main_exits_two_with_the_resync_message_when_the_repository_is_not_registered(
        self, db_session, db_session_factory, tmp_path, capsys
    ):
        """A refusal found while resolving the sweep is a usage error with the reason, never a traceback."""
        directory = tmp_path / "instances"
        directory.mkdir()
        write_instances(directory, make_instance("a"))
        (directory / "bench_repos.toml").write_text('"a" = "repolace/bench-a"\n')

        @asynccontextmanager
        async def seam():
            yield db_session_factory

        code = await asyncio.to_thread(
            main,
            ["--eval-run-id", "run-9", "--model", "m", "--instances-dir", str(directory), "--runs-dir", str(tmp_path / "runs")],
            session_factory=seam, repo_root=self.inputs.repo_root,
        )

        assert code == 2, "no registered repo for the instance"
        assert "Re-sync" in capsys.readouterr().err


class TestCommandLine:
    """Usage errors are decided before the database is touched."""

    @asynccontextmanager
    async def forbidden_factory(self):
        raise AssertionError("the database must not be opened for a usage error")
        yield  # pragma: no cover

    def directory(self, tmp_path: Path, *ids: str, mapped: tuple[str, ...] | None = None) -> Path:
        directory = tmp_path / "instances"
        directory.mkdir()
        write_instances(directory, *(make_instance(i, issue_number=n + 1) for n, i in enumerate(ids)))
        entries = mapped if mapped is not None else ids
        (directory / "bench_repos.toml").write_text("".join(f'"{i}" = "repolace/bench-{i}"\n' for i in entries))
        return directory

    def test_a_bad_run_id_is_a_usage_error(self, tmp_path, capsys):
        directory = self.directory(tmp_path, "a")

        code = main(["--eval-run-id", "../x", "--instances-dir", str(directory)], session_factory=self.forbidden_factory)

        assert code == 2
        assert "eval run id" in capsys.readouterr().err

    def test_a_poisoned_bench_repos_file_is_a_usage_error(self, tmp_path, capsys):
        directory = self.directory(tmp_path, "a", mapped=())
        (directory / "bench_repos.toml").write_text('"a" = "repolace/repolace"\n')

        code = main(["--eval-run-id", "r", "--model", "m", "--instances-dir", str(directory)], session_factory=self.forbidden_factory)

        assert code == 2
        assert "bench" in capsys.readouterr().err

    def test_an_instance_without_a_bench_repo_is_a_usage_error_naming_fork(self, tmp_path, capsys):
        directory = self.directory(tmp_path, "a", "b", mapped=("a",))

        code = main(["--eval-run-id", "r", "--model", "m", "--instances-dir", str(directory)], session_factory=self.forbidden_factory)

        assert code == 2
        assert "repolace-eval fork" in capsys.readouterr().err

    def test_the_example_mapping_shipped_with_the_tool_loads(self):
        example = Path(__file__).resolve().parents[1] / "instances" / "bench_repos.example.toml"
        entries = load_bench_entries(example)
        assert {e.status for e in entries.values()} == {"ready", "incomplete"}
        assert set(load_bench_repos(example)) == {i for i, e in entries.items() if e.status == "ready"}

    def test_the_defaults_are_three_runs_and_no_pr_on_failure(self):
        args = build_parser().parse_args(["--eval-run-id", "r"])

        assert args.runs == 3
        assert args.open_pr_on_failure is False
        assert args.instances == "all"
        assert args.agent == "llm" and args.model is None and args.allow_dirty_tree is False
        assert args.timeout_seconds == DEFAULT_WALL_CLOCK_SECONDS == 11700.0

    def test_the_llm_agent_without_a_model_is_a_usage_error(self, tmp_path, capsys):
        directory = self.directory(tmp_path, "a")

        code = main(["--eval-run-id", "r", "--instances-dir", str(directory)], session_factory=self.forbidden_factory)

        assert code == 2
        assert "--model" in capsys.readouterr().err

    @pytest.mark.parametrize("seconds", ["0", "-5", "inf", "nan"])
    def test_a_nonsense_timeout_is_a_usage_error(self, tmp_path, seconds):
        directory = self.directory(tmp_path, "a")

        code = main(
            ["--eval-run-id", "r", "--model", "m", "--timeout-seconds", seconds, "--instances-dir", str(directory)],
            session_factory=self.forbidden_factory,
        )

        assert code == 2


class TestRunnerWallClock:
    """The runner's default wall clock covers the longest a task can legitimately take.

    A task the runner kills is FAILED, a harness error, and leaves the secondary
    `passed / admissible` denominator; so the default must be at least the worst-case sum of
    the stages, each read from where it is defined, not from a mirror of it.
    """

    @staticmethod
    def index_wait_cap() -> float:
        # Read from the source: importing the pipeline's module would pull torch into this test.
        source = (Path(__file__).resolve().parents[2] / "pipeline" / "repolace_pipeline" / "run.py").read_text()
        found = re.findall(r"^INDEX_WAIT_CAP_SECONDS = ([0-9.]+)$", source, re.M)
        assert len(found) == 1, "INDEX_WAIT_CAP_SECONDS moved; teach this test where"
        return float(found[0])

    def worst_case(self) -> float:
        from repolace_gateway.budget import DEFAULT_MAX_WALL_SECONDS
        from verify.config import DockerConfig

        docker = DockerConfig()
        return (
            self.index_wait_cap()  # waiting for another task's index
            + docker.build_timeout_seconds  # the environment build, inside the baseline
            + docker.run_timeout_seconds  # the baseline suite
            + DEFAULT_MAX_WALL_SECONDS  # the agent stage, scored suites included
            + docker.run_timeout_seconds  # a scored suite started just before the budget ran out
        )

    def test_the_default_covers_the_worst_case_sum_of_the_stages(self):
        assert DEFAULT_WALL_CLOCK_SECONDS >= self.worst_case() + UNBOUNDED_STAGES_SLACK_SECONDS

    def test_the_overrun_is_bounded_by_the_suite_not_by_a_probe_or_a_script(self):
        """Stage 5 counts a scored suite because it is the longest thing that can start late."""
        from repolace_agents.tools.base import ToolLimits
        from verify.config import DockerConfig

        limits = ToolLimits()
        assert max(limits.max_probe_seconds, limits.max_script_timeout) <= DockerConfig().run_timeout_seconds

    def test_the_bound_is_monotone_in_the_suite_time_and_counts_it_twice(self):
        assert task_wall_clock_bound(100.0) - task_wall_clock_bound(0.0) == 200.0

    def test_the_mirrored_index_wait_is_the_pipelines(self):
        from harness import enqueue

        assert enqueue.INDEX_LOCK_WAIT_SECONDS == self.index_wait_cap()
