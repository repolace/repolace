"""`repolace-eval enqueue`: rows, idempotency, and the errors that must not be skips.

The DB-backed tests are the point. A benchmark row with a wrong field is scored
as the agent's failure, a duplicate row is scored twice, and an instance that
silently went missing is absent from the headline without anything having failed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select

from eval_exec_support import add_repo, make_instance, write_instances
from harness.bench_repos import load_bench_repos
from harness.enqueue import EnqueueError, build_parser, enqueue_tasks, main
from repolace_shared.db.models import Task, TaskStatus
from repolace_shared.instances import load_instances


def instances_for(*ids: str):
    return {i: make_instance(i, issue_number=n + 1, problem_statement=f"Title of {i}\n\nBody of {i}.") for n, i in enumerate(ids)}


def bench(*ids: str) -> dict[str, str]:
    return {i: f"repolace/bench-{i}" for i in ids}


async def enqueue(factory, ids=("a", "b"), *, repos=None, **kwargs):
    kwargs.setdefault("eval_run_id", "run-1")
    kwargs.setdefault("instances_requested", "all")
    kwargs.setdefault("runs", 3)
    return await enqueue_tasks(factory, instances_for(*ids), bench(*(repos if repos is not None else ids)), **kwargs)


@pytest.mark.anyio
@pytest.mark.db
class TestRows:
    async def seed_repos(self, session, *ids):
        return {i: await add_repo(session, f"repolace/bench-{i}") for i in ids}

    async def all_tasks(self, session):
        return (await session.execute(select(Task).order_by(Task.instance_id, Task.run_index))).scalars().all()

    async def test_creates_one_queued_row_per_instance_and_run_with_the_right_fields(self, db_session, db_session_factory):
        repos = await self.seed_repos(db_session, "a", "b")

        result = await enqueue(db_session_factory, runs=3, open_pr_on_failure=True)

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

        await enqueue(db_session_factory, ids=("a",), runs=1)

        (row,) = await self.all_tasks(db_session)
        assert row.open_pr_on_failure is False

    async def test_the_issue_url_is_the_bench_repository_and_names_no_upstream(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "psf__requests-2317")

        await enqueue(db_session_factory, ids=("psf__requests-2317",), runs=1)

        (row,) = await self.all_tasks(db_session)
        assert row.issue_url == "https://github.com/repolace/bench-psf__requests-2317"
        assert "github.com/psf" not in row.issue_url
        assert "psf/requests" not in row.issue_url

    async def test_a_rerun_creates_nothing_and_keeps_the_same_rows(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a", "b")
        await enqueue(db_session_factory)
        before = {r.id for r in await self.all_tasks(db_session)}

        again = await enqueue(db_session_factory)

        assert (again.created, again.already_present) == (0, 6)
        assert {r.id for r in await self.all_tasks(db_session)} == before

    async def test_a_rerun_with_more_runs_adds_only_the_missing_ones(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a", "b")
        await enqueue(db_session_factory, runs=2)

        more = await enqueue(db_session_factory, runs=3)

        assert (more.created, more.already_present) == (2, 4)
        count = await db_session.scalar(select(func.count()).select_from(Task))
        assert count == 6

    async def test_a_rerun_does_not_touch_a_row_that_has_already_moved_on(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")
        await enqueue(db_session_factory, ids=("a",), runs=1)
        row = (await self.all_tasks(db_session))[0]
        row.status = TaskStatus.COMPLETED
        await db_session.commit()

        again = await enqueue(db_session_factory, ids=("a",), runs=1)

        assert again.created == 0
        await db_session.refresh(row)
        assert row.status is TaskStatus.COMPLETED

    async def test_a_different_run_id_is_a_separate_run(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")
        await enqueue(db_session_factory, ids=("a",), runs=1, eval_run_id="run-1")

        other = await enqueue(db_session_factory, ids=("a",), runs=1, eval_run_id="run-2")

        assert other.created == 1

    async def test_a_subset_enqueues_only_the_chosen_instances(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a", "b")

        await enqueue(db_session_factory, instances_requested="b", runs=1)

        assert [r.instance_id for r in await self.all_tasks(db_session)] == ["b"]

    async def test_a_missing_registered_repo_is_an_error_that_says_to_resync_and_inserts_nothing(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")  # `b` has no registered_repos row

        with pytest.raises(EnqueueError) as raised:
            await enqueue(db_session_factory)

        message = str(raised.value)
        assert "repolace/bench-b" in message and "Re-sync" in message
        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_every_problem_is_reported_at_once(self, db_session, db_session_factory):
        with pytest.raises(EnqueueError) as raised:
            await enqueue(db_session_factory)

        message = str(raised.value)
        assert "repolace/bench-a" in message and "repolace/bench-b" in message

    async def test_an_inactive_registered_repo_is_refused(self, db_session, db_session_factory):
        await add_repo(db_session, "repolace/bench-a", is_active=False)

        with pytest.raises(EnqueueError, match="inactive"):
            await enqueue(db_session_factory, ids=("a",))

        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_an_instance_with_no_bench_repo_entry_is_refused_and_names_fork(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a", "b")

        with pytest.raises(EnqueueError, match="repolace-eval fork"):
            await enqueue(db_session_factory, ids=("a", "b"), repos=("a",))

        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    async def test_an_unknown_instance_is_refused(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")

        with pytest.raises(EnqueueError, match="not an instance"):
            await enqueue(db_session_factory, ids=("a",), instances_requested="a,nope")

    async def test_an_instance_with_no_usable_title_is_refused(self, db_session, db_session_factory):
        await self.seed_repos(db_session, "a")
        blank = {"a": make_instance("a", problem_statement="\n   \n")}

        with pytest.raises(EnqueueError, match="title"):
            await enqueue_tasks(
                db_session_factory, blank, bench("a"), eval_run_id="run-1", instances_requested="all", runs=1
            )

    @pytest.mark.parametrize("run_id", ["", "../x", "a/b", "a b", "-x", ".x", "x" * 65, "a\n"])
    async def test_a_hostile_run_id_is_refused_before_anything_is_inserted(self, db_session, db_session_factory, run_id):
        await self.seed_repos(db_session, "a")

        with pytest.raises(EnqueueError):
            await enqueue(db_session_factory, ids=("a",), eval_run_id=run_id)

        assert await db_session.scalar(select(func.count()).select_from(Task)) == 0

    @pytest.mark.parametrize("runs", [0, -1, True])
    async def test_runs_must_be_a_positive_integer(self, db_session, db_session_factory, runs):
        await self.seed_repos(db_session, "a")

        with pytest.raises(EnqueueError, match="--runs"):
            await enqueue(db_session_factory, ids=("a",), runs=runs)


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

        code = main(["--eval-run-id", "r", "--instances-dir", str(directory)], session_factory=self.forbidden_factory)

        assert code == 2
        assert "bench" in capsys.readouterr().err

    def test_an_instance_without_a_bench_repo_is_a_usage_error_naming_fork(self, tmp_path, capsys):
        directory = self.directory(tmp_path, "a", "b", mapped=("a",))

        code = main(["--eval-run-id", "r", "--instances-dir", str(directory)], session_factory=self.forbidden_factory)

        assert code == 2
        assert "repolace-eval fork" in capsys.readouterr().err

    def test_the_example_mapping_shipped_with_the_tool_loads(self):
        example = Path(__file__).resolve().parents[1] / "instances" / "bench_repos.example.toml"
        assert load_bench_repos(example)

    def test_the_defaults_are_three_runs_and_no_pr_on_failure(self):
        args = build_parser().parse_args(["--eval-run-id", "r"])

        assert args.runs == 3
        assert args.open_pr_on_failure is False
        assert args.instances == "all"
