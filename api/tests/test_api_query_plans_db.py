"""The indexes of migration 0015 exist, and the queries they were made for can use them.

Each test captures the statement the real code sends (the keyset task list, the cost
estimate, the reaper, the sweeper), replays it under `EXPLAIN (FORMAT JSON)` against a
few thousand seeded rows and names the index the plan reads. Capturing rather than
retyping the SQL is the point: a query that is later reshaped so an index no longer
applies fails here instead of in production, months of history later.

`enable_seqscan` is switched off for every plan, and that is deliberate. On a few
thousand rows the planner is right to prefer a sequential scan for some of these
queries, so "the planner chose the index" would be a statement about the seed size.
What the tests assert is that an index *can* serve the query: with the sequential scan
forbidden, the plan has to be built from an index, and which one it picks shows
whether the one we added matches the query's filter and its ORDER BY.
"""

import json
import uuid
from datetime import timedelta

import pytest
from api_support import capture_sql, seed_repo
from repolace_api.routes.repos import compute_estimate, encode_cursor, query_task_page
from repolace_shared.db.models import Task, TaskStatus
from repolace_shared.tasks_recovery import running_with_stale_heartbeat
from sqlalchemy import func, select, text

pytestmark = [pytest.mark.anyio, pytest.mark.db]

TASKS_BY_REPO = "ix_tasks_repo_created_id"
ACTIVE_TASKS = "ix_tasks_active"
LATEST_CALL = "ix_llm_calls_task_created_desc"
SUPERSEDED = "ix_tasks_repo_id_created_at"

TASK_COUNT = 4000
LIVE_STATUS_EVERY = 50  # 2% queued, 2% running: a live set far smaller than the table


@pytest.fixture
async def seeded(db_session, db_engine):
    """Three busy repos, one with almost no history, ~4,000 tasks and ~12,000 calls, analysed.

    Benchmark rows (every 11th) and unpriced rows (every 7th) are in the mix so the
    queries' `eval_run_id IS NULL` and `cost_usd IS NOT NULL` filters have something to remove.
    """
    busy = []
    for i in range(3):
        repo = await seed_repo(
            db_session, id=uuid.uuid4(), github_repo_id=200 + i, name=f"busy{i}", full_name=f"acme/busy{i}"
        )
        busy.append(repo.id)
    quiet = (
        await seed_repo(db_session, id=uuid.uuid4(), github_repo_id=300, name="quiet", full_name="acme/quiet")
    ).id

    await db_session.execute(
        text(
            """
            INSERT INTO tasks (id, repo_id, issue_number, issue_title, issue_url, target_branch, status, retry_count,
                               cost_usd, eval_run_id, instance_id, run_index, created_at, updated_at)
            SELECT gen_random_uuid(), (CAST(:busy AS uuid[]))[1 + g % 3], g, 'title', 'url', 'main',
                   (CASE WHEN g % :every = 0 THEN 'queued' WHEN g % :every = 1 THEN 'running' ELSE 'completed' END)::task_status,
                   0,
                   CASE WHEN g % 7 = 0 THEN NULL ELSE (g % 40) / 20.0 END,
                   CASE WHEN g % 11 = 0 THEN 'run' END,
                   CASE WHEN g % 11 = 0 THEN 'inst' || g END,
                   CASE WHEN g % 11 = 0 THEN g END,
                   timestamptz '2026-01-01 00:00+00' + g * interval '1 minute',
                   timestamptz '2026-01-01 00:00+00' + g * interval '1 minute'
            FROM generate_series(1, :n) AS g
            """
        ),
        {"busy": busy, "n": TASK_COUNT, "every": LIVE_STATUS_EVERY},
    )
    # The quiet repo: three priced tasks, so its own history is below the estimate's minimum of five.
    await db_session.execute(
        text(
            """
            INSERT INTO tasks (id, repo_id, issue_number, issue_title, issue_url, target_branch, status, retry_count,
                               cost_usd, created_at, updated_at)
            SELECT gen_random_uuid(), :quiet, g, 'title', 'url', 'main', 'completed', 0, 0.25,
                   timestamptz '2026-03-01 00:00+00' + g * interval '1 minute',
                   timestamptz '2026-03-01 00:00+00' + g * interval '1 minute'
            FROM generate_series(1, 3) AS g
            """
        ),
        {"quiet": quiet},
    )
    await db_session.execute(
        text(
            """
            INSERT INTO llm_calls (id, task_id, stage, model, provider, created_at)
            SELECT gen_random_uuid(), t.id, 'agent', CASE WHEN (t.issue_number + n) % 2 = 0 THEN 'm1' ELSE 'm2' END,
                   'p', t.created_at + n * interval '1 second'
            FROM tasks t CROSS JOIN generate_series(1, 3) AS n
            """
        )
    )
    await db_session.commit()

    async with db_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        for table in ("tasks", "llm_calls"):
            await conn.execute(text(f"ANALYZE {table}"))
    return {"busy": busy, "quiet": quiet}


def pick(statements: list[tuple[str, tuple]], *needles: str) -> tuple[str, tuple]:
    """The one captured statement that contains every needle."""
    found = [s for s in statements if all(n in s[0] for n in needles)]
    assert len(found) == 1, (needles, [s[0][:120] for s in statements])
    return found[0]


async def explain(db_engine, statement: tuple[str, tuple]) -> dict:
    """The JSON plan of a captured statement with its real parameter values, sequential scans forbidden."""
    sql, parameters = statement
    async with db_engine.connect() as conn, conn.begin():
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        document = (await conn.exec_driver_sql("EXPLAIN (FORMAT JSON) " + sql, parameters)).scalar_one()
    return (json.loads(document) if isinstance(document, str) else document)[0]["Plan"]


def nodes(plan: dict) -> list[dict]:
    return [plan, *(n for child in plan.get("Plans", []) for n in nodes(child))]


def indexes_read(plan: dict) -> dict[str, str]:
    """Index name -> its Index Cond, for every index scan anywhere in the plan (subqueries included)."""
    return {n["Index Name"]: n.get("Index Cond", "") for n in nodes(plan) if "Index Name" in n}


class TestKeysetTaskList:
    async def test_the_cursor_page_is_a_range_scan_in_order_with_no_sort(self, seeded, db_session_factory, db_engine):
        repo_id = seeded["busy"][0]
        async with db_session_factory() as session:
            middle = (
                await session.execute(
                    select(Task.created_at, Task.id).where(Task.repo_id == repo_id).order_by(Task.created_at).offset(300).limit(1)
                )
            ).one()
        async with db_session_factory() as session:
            with capture_sql(db_engine) as seen:
                await query_task_page(session, repo_id, cursor=encode_cursor(*middle))

        plan = await explain(db_engine, pick(seen, "FROM tasks", "ORDER BY tasks.created_at DESC"))

        used = indexes_read(plan)
        assert TASKS_BY_REPO in used, used
        # The boundary is an index condition on (created_at, id) together, not a filter applied after reading.
        assert "created_at, id" in used[TASKS_BY_REPO].replace("tasks.", ""), used[TASKS_BY_REPO]
        # Rows come out of the index in the requested order: nothing is sorted.
        assert not [n for n in nodes(plan) if n["Node Type"] in ("Sort", "Incremental Sort")]

    async def test_the_first_page_with_filters_uses_the_same_index(self, seeded, db_session_factory, db_engine):
        async with db_session_factory() as session:
            with capture_sql(db_engine) as seen:
                await query_task_page(session, seeded["busy"][1], status=TaskStatus.COMPLETED, limit=20)

        plan = await explain(db_engine, pick(seen, "FROM tasks", "ORDER BY tasks.created_at DESC"))

        assert TASKS_BY_REPO in indexes_read(plan)


class TestActiveTasks:
    async def test_the_reapers_stale_heartbeat_scan_reads_only_the_active_index(self, seeded, db_session_factory, db_engine):
        with capture_sql(db_engine) as seen:
            await running_with_stale_heartbeat(db_session_factory, timedelta(minutes=10))

        plan = await explain(db_engine, pick(seen, "FROM tasks", "tasks.heartbeat_at"))

        assert ACTIVE_TASKS in indexes_read(plan)

    async def test_the_sweepers_queued_scan_reads_only_the_active_index(self, seeded, db_session_factory, db_engine):
        # The query of `worker/repolace_worker/maintenance.py::sweep_queued`, rebuilt here: the worker package is
        # not importable from the API's tests, and its SQL text is owned by another change. Keep the two in step.
        query = (
            select(Task.id)
            .where(
                Task.status == TaskStatus.QUEUED,
                Task.eval_run_id.is_(None),
                Task.created_at < func.now() - timedelta(minutes=2),
            )
            .order_by(Task.created_at, Task.id)
        )
        async with db_session_factory() as session:
            with capture_sql(db_engine) as seen:
                await session.scalars(query)

        plan = await explain(db_engine, pick(seen, "FROM tasks"))

        used = indexes_read(plan)
        assert ACTIVE_TASKS in used, used
        # The live rows are all that is read. A bitmap scan of them plus a sort of that handful is as good as
        # reading them in order, so what the plan does to order them is not asserted; which rows it reads is.
        assert set(used) == {ACTIVE_TASKS}, used

    async def test_the_active_index_holds_only_the_active_rows(self, seeded, db_session):
        indexed = (
            await db_session.execute(
                text("SELECT relpages, reltuples FROM pg_class WHERE relname = :name"), {"name": ACTIVE_TASKS}
            )
        ).one()
        total = (await db_session.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
        live = (
            await db_session.execute(text("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running')"))
        ).scalar_one()

        assert 0 < live < total * 0.05
        # Estimated by ANALYZE/CREATE INDEX, so only the order of magnitude is asserted.
        assert indexed.reltuples < total * 0.5


class TestCostEstimate:
    async def test_the_repo_window_reads_the_task_index_in_order(self, seeded, db_session_factory, db_engine):
        async with db_session_factory() as session:
            with capture_sql(db_engine) as seen:
                estimate = await compute_estimate(session, seeded["busy"][2])
        assert estimate.basis == "repo"

        plan = await explain(db_engine, pick(seen, "percentile_cont", "tasks.repo_id"))

        assert TASKS_BY_REPO in indexes_read(plan)
        assert not [n for n in nodes(plan) if n["Node Type"] in ("Sort", "Incremental Sort")]

    async def test_the_latest_call_per_task_reads_the_descending_call_index(self, seeded, db_session_factory, db_engine):
        async with db_session_factory() as session:
            with capture_sql(db_engine) as seen:
                estimate = await compute_estimate(session, seeded["quiet"])
        assert estimate.basis == "model"

        for statement in (
            pick(seen, "DISTINCT ON", "SELECT anon_1.model"),  # which model does this repo use
            pick(seen, "DISTINCT ON", "percentile_cont"),  # the pool of tasks on that model
        ):
            plan = await explain(db_engine, statement)
            assert LATEST_CALL in indexes_read(plan), indexes_read(plan)


class TestTheIndexesInTheMigratedSchema:
    async def definitions(self, db_session) -> dict[str, tuple[bool, str]]:
        rows = await db_session.execute(
            text(
                "SELECT c.relname, i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(:names)"
            ),
            {"names": [TASKS_BY_REPO, ACTIVE_TASKS, LATEST_CALL, SUPERSEDED, "ix_llm_calls_task_id_created_at"]},
        )
        return {name: (valid, definition) for name, valid, definition in rows}

    async def test_the_new_indexes_are_valid_and_shaped_for_their_queries(self, db_session):
        found = await self.definitions(db_session)

        assert found[TASKS_BY_REPO][0] and "ON public.tasks" in found[TASKS_BY_REPO][1]
        assert "(repo_id, created_at DESC, id DESC)" in found[TASKS_BY_REPO][1]
        assert found[ACTIVE_TASKS][0] and "(created_at, id)" in found[ACTIVE_TASKS][1]
        predicate = found[ACTIVE_TASKS][1].split("WHERE", 1)[1]
        assert "queued" in predicate and "running" in predicate and "pr_opened" not in predicate
        assert found[LATEST_CALL][0] and "(task_id, created_at DESC)" in found[LATEST_CALL][1]

    async def test_the_ascending_call_index_stays_and_the_superseded_task_index_is_gone(self, db_session):
        found = await self.definitions(db_session)

        # Still serves the per-task cost sum and the relationship's `order_by created_at`.
        assert found["ix_llm_calls_task_id_created_at"][0]
        # The new task index has the old one's columns, in the same order and direction, as its prefix.
        assert SUPERSEDED not in found
