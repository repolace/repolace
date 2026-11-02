"""The sweeper and the reaper against the database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pipeline_support import seed_task
from repolace_shared.db.models import Task, TaskCheckpoint, TaskStatus
from repolace_shared.tasks_recovery import (
    RESUME_REFUSED_PREFIX,
    expire_paused,
    fail_exhausted_pauses,
    pause_statement,
    reopen_refused,
)
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


async def add_checkpoint(session, task) -> None:
    session.add(TaskCheckpoint(task_id=task.id, agent_state={}, base_sha="b" * 40, provenance={}))
    await session.commit()


class TestPausedRows:
    """A dead runner with a checkpoint is waiting to resume, not failed; everything else is as before."""

    async def test_a_dead_runner_with_a_checkpoint_is_paused_and_one_without_is_failed(
        self, db_session, db_session_factory
    ):
        with_checkpoint = await add_task(db_session, status=TaskStatus.RUNNING)
        without = await add_task(db_session, status=TaskStatus.RUNNING)
        for task in (with_checkpoint, without):
            await backdate(db_session, task, started_at=OLD, heartbeat_at=OLD)
        with_id, without_id = with_checkpoint.id, without.id
        await add_checkpoint(db_session, with_checkpoint)
        removed = []

        async def remove(task_id):
            removed.append(task_id)

        reaped = await maintenance.reap_abandoned(db_session_factory, THREE_MIN, remove)

        assert set(reaped) == set(removed) == {with_id, without_id}
        db_session.expire_all()
        paused = await db_session.get(Task, with_id)
        failed = await db_session.get(Task, without_id)
        assert (paused.status, paused.paused_reason, paused.stop_class) == (
            TaskStatus.PAUSED, "runner: abandoned", "unavailable",
        )
        assert paused.completed_at is None and paused.error_message is None
        assert (failed.status, failed.error_message, failed.stop_class) == (
            TaskStatus.FAILED, "runner: abandoned", "unavailable",
        )

    async def test_the_sweeper_republishes_a_paused_row_by_the_time_since_it_paused(
        self, db_session, db_session_factory
    ):
        long_paused = await add_task(db_session, status=TaskStatus.PAUSED)
        just_paused = await add_task(db_session, status=TaskStatus.PAUSED)
        bench = await add_task(
            db_session, status=TaskStatus.PAUSED, eval_run_id="r1", instance_id="i1", run_index=0
        )
        # All three are old by `created_at`; only the first has also been paused for a while.
        for task in (long_paused, just_paused, bench):
            await backdate(db_session, task, created_at=OLD)
        await backdate(db_session, long_paused, paused_at=OLD)
        await backdate(db_session, just_paused, paused_at=datetime.now(UTC))
        await backdate(db_session, bench, paused_at=OLD)
        long_id = long_paused.id
        sent: list[uuid.UUID] = []

        await sweep(db_session_factory, sent.append, FakeClaims())

        assert sent == [long_id]

    async def test_a_pause_that_waited_too_long_or_was_resumed_too_often_is_failed(
        self, db_session, db_session_factory
    ):
        waiting = await add_task(db_session, status=TaskStatus.PAUSED)
        stale = await add_task(db_session, status=TaskStatus.PAUSED)
        churning = await add_task(db_session, status=TaskStatus.PAUSED)
        waiting_id, stale_id, churning_id = waiting.id, stale.id, churning.id
        await backdate(db_session, stale, updated_at=OLD)
        await backdate(db_session, churning, resume_count=10)

        failed = await expire_paused(
            db_session_factory, max_age=timedelta(minutes=30), max_resumes=10, message="runner: paused too long"
        )

        assert set(failed) == {stale_id, churning_id}
        db_session.expire_all()
        assert (await db_session.get(Task, waiting_id)).status is TaskStatus.PAUSED
        row = await db_session.get(Task, stale_id)
        assert (row.status, row.error_message, row.paused_reason) == (TaskStatus.FAILED, "runner: paused too long", None)


class TestExpiryIsForProductRows:
    async def test_a_benchmark_row_is_never_expired_by_age_or_count_here(self, db_session, db_session_factory):
        bench = await add_task(
            db_session, status=TaskStatus.PAUSED, eval_run_id="r1", instance_id="i1", run_index=0, resume_count=50
        )
        bench_id = bench.id
        await backdate(db_session, bench, updated_at=OLD)

        failed = await expire_paused(
            db_session_factory, max_age=timedelta(minutes=30), max_resumes=10, message="expired"
        )

        assert failed == []
        db_session.expire_all()
        assert (await db_session.get(Task, bench_id)).status is TaskStatus.PAUSED

    async def test_a_benchmark_run_gets_only_the_resume_cap(self, db_session, db_session_factory):
        over = await add_task(
            db_session, status=TaskStatus.PAUSED, eval_run_id="r1", instance_id="i1", run_index=0, resume_count=10
        )
        under = await add_task(
            db_session, status=TaskStatus.PAUSED, eval_run_id="r1", instance_id="i2", run_index=0, resume_count=9
        )
        other_run = await add_task(
            db_session, status=TaskStatus.PAUSED, eval_run_id="r2", instance_id="i1", run_index=0, resume_count=10
        )
        over_id, under_id, other_id = over.id, under.id, other_run.id
        await backdate(db_session, under, updated_at=OLD)  # old, but age is not a reason on the eval path

        failed = await fail_exhausted_pauses(db_session_factory, "r1", max_resumes=10, message="too many resumes")

        assert failed == [over_id]
        db_session.expire_all()
        assert (await db_session.get(Task, under_id)).status is TaskStatus.PAUSED
        assert (await db_session.get(Task, other_id)).status is TaskStatus.PAUSED


class TestThePauseStatement:
    async def pause(self, factory, task_id, **kwargs):
        async with factory() as session:
            result = await session.execute(pause_statement(task_id, "why", **kwargs).returning(Task.id))
            await session.commit()
            return result.scalar_one_or_none()

    async def test_a_resume_that_made_no_progress_keeps_updated_at_but_restarts_the_gate(
        self, db_session, db_session_factory
    ):
        task = await add_task(db_session, status=TaskStatus.RUNNING)
        task_id = task.id
        await add_checkpoint(db_session, task)
        await backdate(db_session, task, updated_at=OLD, paused_at=None)

        assert await self.pause(db_session_factory, task_id, keep_updated_at=OLD) == task_id

        db_session.expire_all()
        row = await db_session.get(Task, task_id)
        assert row.updated_at == OLD and row.paused_at is not None and row.paused_at > OLD

    async def test_progress_stamps_updated_at(self, db_session, db_session_factory):
        task = await add_task(db_session, status=TaskStatus.RUNNING)
        task_id = task.id
        await add_checkpoint(db_session, task)
        await backdate(db_session, task, updated_at=OLD)

        await self.pause(db_session_factory, task_id)

        db_session.expire_all()
        assert (await db_session.get(Task, task_id)).updated_at > OLD

    async def test_it_is_fenced_to_the_process_that_owns_the_row(self, db_session, db_session_factory):
        task = await add_task(db_session, status=TaskStatus.RUNNING, claimed_by="host:2")
        task_id = task.id
        await add_checkpoint(db_session, task)

        assert await self.pause(db_session_factory, task_id, claimed_by="host:1") is None
        assert await self.pause(db_session_factory, task_id, claimed_by="host:2") == task_id

    async def test_it_needs_a_checkpoint(self, db_session, db_session_factory):
        task = await add_task(db_session, status=TaskStatus.RUNNING)

        assert await self.pause(db_session_factory, task.id) is None


class TestReopeningARefusedResume:
    async def failed_refusal(self, session, with_checkpoint=True, message=None):
        task = await add_task(
            session, status=TaskStatus.FAILED, stop_class="design",
            error_message=message or (RESUME_REFUSED_PREFIX + "the target branch moved"),
        )
        task_id = task.id
        if with_checkpoint:
            await add_checkpoint(session, task)
        return task_id

    async def test_a_refused_resume_with_its_checkpoint_goes_back_to_paused(self, db_session, db_session_factory):
        task_id = await self.failed_refusal(db_session)

        assert await reopen_refused(db_session_factory, task_id) is True

        db_session.expire_all()
        row = await db_session.get(Task, task_id)
        assert (row.status, row.error_message, row.completed_at, row.stop_class) == (
            TaskStatus.PAUSED, None, None, "unavailable",
        )
        assert row.paused_at is not None

    async def test_a_row_that_failed_for_anything_else_stays_failed(self, db_session, db_session_factory):
        task_id = await self.failed_refusal(db_session, message="agent: RuntimeError: a bug")

        assert await reopen_refused(db_session_factory, task_id) is False

    async def test_a_refusal_whose_checkpoint_is_gone_cannot_be_reopened(self, db_session, db_session_factory):
        task_id = await self.failed_refusal(db_session, with_checkpoint=False)

        assert await reopen_refused(db_session_factory, task_id) is False
