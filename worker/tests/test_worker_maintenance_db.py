"""The sweeper and the reaper against the database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pipeline_support import seed_task
from repolace_shared.db.models import Task, TaskStatus
from repolace_worker import maintenance
from sqlalchemy import select, update

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


class TestSweeper:
    async def test_only_old_queued_non_benchmark_rows_are_republished(self, db_session, db_session_factory):
        old = await add_task(db_session)
        fresh = await add_task(db_session)
        bench = await add_task(db_session, eval_run_id="r1", instance_id="i1", run_index=0)
        running = await add_task(db_session, status=TaskStatus.RUNNING)
        for task in (old, bench, running):
            await backdate(db_session, task, created_at=OLD)
        sent: list[uuid.UUID] = []

        published = await maintenance.sweep_queued(db_session_factory, FIVE_MIN, sent.append)

        assert published == sent == [old.id]
        assert fresh.id not in sent

    async def test_a_failed_publish_is_logged_and_the_rest_still_go(self, db_session, db_session_factory):
        first, second = await add_task(db_session), await add_task(db_session)
        await backdate(db_session, first, created_at=OLD)
        await backdate(db_session, second, created_at=OLD + timedelta(minutes=1))
        sent = []

        def flaky(task_id):
            if task_id == first.id:
                raise ConnectionError("broker down")
            sent.append(task_id)

        assert await maintenance.sweep_queued(db_session_factory, FIVE_MIN, flaky) == [second.id]
        assert sent == [second.id]


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
