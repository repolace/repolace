"""The sweeper and the reaper against the database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pipeline_support import seed_task
from repolace_shared.db.models import Task, TaskStatus
from repolace_worker import maintenance
from sqlalchemy import func, select, update

pytestmark = [pytest.mark.anyio, pytest.mark.db]

OLD = datetime.now(UTC) - timedelta(hours=1)
FIVE_MIN = timedelta(minutes=5)
THREE_MIN = timedelta(minutes=3)


async def add_task(session, **fields) -> Task:
    """A task on the first one's repo (the first call seeds the installation and repo)."""
    first = (await session.execute(select(Task).limit(1))).scalar_one_or_none()
    if first is None:
        return await seed_task(session, **fields)
    task = Task(
        id=uuid.uuid4(), repo_id=first.repo_id, issue_number=8, issue_title="t",
        issue_url="https://x/8", target_branch="main", **{"status": TaskStatus.QUEUED, **fields},
    )
    session.add(task)
    await session.commit()
    return task


async def backdate(session, task, **columns):
    await session.execute(update(Task).where(Task.id == task.id).values(**columns))
    await session.commit()


class FakeClaims:
    """An in-memory stand-in for the Redis `SET NX EX` key; `expire()` is the TTL running out."""

    def __init__(self):
        self.keys: set[uuid.UUID] = set()

    def claim(self, task_id):
        if task_id in self.keys:
            return False
        self.keys.add(task_id)
        return True

    def release(self, task_id):
        self.keys.discard(task_id)

    def expire(self):
        self.keys.clear()


async def sweep(factory, publish, claims, **kwargs):
    return await maintenance.sweep_queued(factory, FIVE_MIN, publish, claims=claims, **kwargs)


class TestSweeper:
    async def test_only_old_queued_non_benchmark_rows_are_republished(self, db_session, db_session_factory):
        old = await add_task(db_session)
        fresh = await add_task(db_session)
        bench = await add_task(db_session, eval_run_id="r1", instance_id="i1", run_index=0)
        running = await add_task(db_session, status=TaskStatus.RUNNING)
        for task in (old, bench, running):
            await backdate(db_session, task, created_at=OLD)
        sent: list[uuid.UUID] = []

        published = await sweep(db_session_factory, sent.append, FakeClaims())

        assert published == sent == [old.id]
        assert fresh.id not in sent

    async def test_a_second_tick_republishes_nothing_until_the_key_expires(self, db_session, db_session_factory):
        old = await add_task(db_session)
        await backdate(db_session, old, created_at=OLD)
        sent: list[uuid.UUID] = []
        claims = FakeClaims()

        await sweep(db_session_factory, sent.append, claims)
        await sweep(db_session_factory, sent.append, claims)
        assert sent == [old.id]

        claims.expire()  # fifteen minutes later, the row is still queued
        await sweep(db_session_factory, sent.append, claims)
        assert sent == [old.id, old.id]

    async def test_a_tick_is_capped(self, db_session, db_session_factory):
        tasks = [await add_task(db_session) for _ in range(5)]
        for i, task in enumerate(tasks):
            await backdate(db_session, task, created_at=OLD + timedelta(seconds=i))
        sent: list[uuid.UUID] = []

        await sweep(db_session_factory, sent.append, FakeClaims(), max_per_tick=2)

        assert sent == [tasks[0].id, tasks[1].id]

    async def test_a_tick_reads_only_the_oldest_rows_up_to_the_scan_limit(self, db_session, db_session_factory):
        """The query must not load every old queued id: with a backlog that is the whole table each minute.
        The two oldest rows are already claimed, so a scan of two finds nothing to send even though
        three more rows could have been."""
        tasks = [await add_task(db_session) for _ in range(5)]
        for i, task in enumerate(tasks):
            await backdate(db_session, task, created_at=OLD + timedelta(seconds=i))
        claims = FakeClaims()
        claims.keys = {tasks[0].id, tasks[1].id}
        sent: list[uuid.UUID] = []

        assert await sweep(db_session_factory, sent.append, claims, scan_limit=2) == []
        assert sent == []

        assert await sweep(db_session_factory, sent.append, claims, scan_limit=3) == [tasks[2].id]

    async def test_a_broker_error_aborts_the_tick_without_raising_and_frees_the_key(
        self, db_session, db_session_factory
    ):
        first, second = await add_task(db_session), await add_task(db_session)
        await backdate(db_session, first, created_at=OLD)
        await backdate(db_session, second, created_at=OLD + timedelta(minutes=1))
        attempts = []
        claims = FakeClaims()

        def down(task_id):
            attempts.append(task_id)
            raise ConnectionError("broker down")

        assert await sweep(db_session_factory, down, claims) == []
        assert attempts == [first.id]  # the second row was not even tried
        assert claims.keys == {first.id} - {first.id}  # the failed row's key was released


class TestReaper:
    async def test_only_stale_running_rows_are_failed_and_their_containers_removed(
        self, db_session, db_session_factory
    ):
        stale = await add_task(db_session, status=TaskStatus.RUNNING)
        never_beat = await add_task(db_session, status=TaskStatus.RUNNING)
        beating = await add_task(db_session, status=TaskStatus.RUNNING)
        just_claimed = await add_task(db_session, status=TaskStatus.RUNNING)
        queued = await add_task(db_session)
        await backdate(db_session, stale, started_at=OLD, heartbeat_at=OLD)
        await backdate(db_session, never_beat, started_at=OLD, heartbeat_at=None)
        await backdate(db_session, beating, started_at=OLD, heartbeat_at=datetime.now(UTC))
        await backdate(db_session, just_claimed, started_at=datetime.now(UTC), heartbeat_at=None)
        removed = []

        async def remove(task_id):
            removed.append(task_id)

        reaped = await maintenance.reap_abandoned(db_session_factory, THREE_MIN, remove)

        assert set(reaped) == set(removed) == {stale.id, never_beat.id}
        db_session.expire_all()
        rows = (await db_session.execute(select(Task))).scalars()
        statuses = {t.id: (t.status, t.error_message) for t in rows}
        assert statuses[stale.id] == (TaskStatus.FAILED, "runner: abandoned")
        assert statuses[never_beat.id] == (TaskStatus.FAILED, "runner: abandoned")
        assert statuses[beating.id][0] is TaskStatus.RUNNING
        assert statuses[just_claimed.id][0] is TaskStatus.RUNNING
        assert statuses[queued.id][0] is TaskStatus.QUEUED

    async def test_a_heartbeat_between_the_select_and_the_update_leaves_the_row_running(
        self, db_session, db_session_factory, monkeypatch
    ):
        racing = await add_task(db_session, status=TaskStatus.RUNNING)
        racing_id = racing.id
        await backdate(db_session, racing, started_at=OLD, heartbeat_at=OLD)
        real = maintenance.running_with_stale_heartbeat

        async def select_then_beat(factory, older_than):
            found = await real(factory, older_than)
            if older_than != THREE_MIN:
                return found  # the reaper's separate long-stale look
            # The task beats after the reaper read it and before the reaper writes.
            async with factory() as session:
                await session.execute(update(Task).where(Task.id == racing_id).values(heartbeat_at=func.now()))
                await session.commit()
            return found

        monkeypatch.setattr(maintenance, "running_with_stale_heartbeat", select_then_beat)
        removed = []

        async def remove(task_id):
            removed.append(task_id)

        reaped = await maintenance.reap_abandoned(db_session_factory, THREE_MIN, remove)

        db_session.expire_all()
        row = await db_session.get(Task, racing_id)
        assert reaped == [] and removed == []
        assert row.status is TaskStatus.RUNNING and row.error_message is None

    async def test_a_fresh_heartbeat_is_never_failed(self, db_session, db_session_factory):
        beating = await add_task(db_session, status=TaskStatus.RUNNING)
        await backdate(db_session, beating, started_at=OLD, heartbeat_at=datetime.now(UTC))

        assert await maintenance.reap_abandoned(db_session_factory, THREE_MIN, lambda _: None) == []
