"""`task_stages` rows and the task heartbeat, against a real database.

What these pin: every stage the pipeline enters leaves one row, in order, that ends `ok` or
`failed`; a database that cannot take that row changes nothing about the task; and the claim
and the heartbeat leave `claimed_by` and `heartbeat_at` for the reaper to read.
"""

import asyncio
import os
import socket
import time

import pytest
import repolace_pipeline.run as run_module
from pipeline_support import (
    AFTER,
    BASELINE,
    FakeGithubClient,
    ScriptedAgent,
    agent_result,
    reload,
    seed_task,
)
from repolace_agents.contracts import StopReason
from repolace_shared.db.models import Task, TaskStage, TaskStatus
from sqlalchemy import select
from test_pipeline_run_task_db import agent_that, run
from verify.testing import FakeBackend

pytestmark = [pytest.mark.anyio, pytest.mark.db, pytest.mark.usefixtures("embedder")]

#: The stages a task that reaches the agent and opens no PR enters, in order.
STAGES_TO_THE_REVIEW = [
    "instance",
    "preflight",
    "embedder",
    "clone",
    "index",
    "retrieve",
    "verify_baseline",
    "record_baseline",
    "baseline_files",
    "branch",
    "agent",
    "review",
]


async def stage_rows(factory, task_id) -> list[TaskStage]:
    async with factory() as session:
        result = await session.execute(
            select(TaskStage).where(TaskStage.task_id == task_id).order_by(TaskStage.started_at)
        )
        return list(result.scalars().all())


async def blip(*args, **kwargs):
    raise ConnectionError("database went away")


class TestStageRecords:
    async def test_a_successful_run_leaves_one_ok_row_per_stage_in_order(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.STEP_CAP)

        await run(db_session_factory, origin_url, task, agent, FakeBackend(results=[BASELINE, AFTER]))

        rows = await stage_rows(db_session_factory, task.id)
        assert [r.stage for r in rows] == [*STAGES_TO_THE_REVIEW, "finalize"]
        assert {r.status for r in rows} == {"ok"}
        assert all(r.ended_at is not None and r.ended_at >= r.started_at for r in rows)
        assert all(r.error is None for r in rows)

    async def test_a_failing_stage_leaves_a_failed_row_with_its_error_and_no_later_rows(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)

        result, _ = await run(
            db_session_factory,
            origin_url,
            task,
            agent_that(StopReason.SUBMITTED),
            FakeBackend(results=[BASELINE, AFTER]),
            github=FakeGithubClient(permissions={}),
        )

        assert result.status is TaskStatus.FAILED
        rows = await stage_rows(db_session_factory, task.id)
        assert [(r.stage, r.status) for r in rows] == [("instance", "ok"), ("preflight", "failed")]
        assert rows[1].error.startswith("preflight: ") and "lacks" in rows[1].error
        assert rows[1].ended_at is not None

    async def test_an_unexpected_exception_is_described_and_scrubbed_in_the_agent_row(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)

        async def boom(deps):
            raise RuntimeError("bad \x00 thing")

        result, _ = await run(
            db_session_factory, origin_url, task, ScriptedAgent(boom), FakeBackend(results=[BASELINE, AFTER])
        )

        assert result.status is TaskStatus.FAILED
        rows = await stage_rows(db_session_factory, task.id)
        failed = [r for r in rows if r.status == "failed"]
        assert [r.stage for r in failed] == ["agent"]
        assert "\x00" not in failed[0].error and "RuntimeError" in failed[0].error

    async def test_a_nul_in_the_error_does_not_lose_the_row(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        token = run_module._stage_sink.set(run_module._StageSink(db_session_factory, task.id))
        try:
            with pytest.raises(run_module.StageFailed):
                async with run_module._stage("clone"):
                    raise ValueError("nul \x00 inside")
        finally:
            run_module._stage_sink.reset(token)

        (row,) = await stage_rows(db_session_factory, task.id)
        assert row.status == "failed" and "\x00" not in row.error and "�" in row.error

    async def test_a_stage_record_that_cannot_be_written_changes_nothing_about_the_task(
        self, db_session, db_session_factory, origin_url, monkeypatch
    ):
        task = await seed_task(db_session)
        monkeypatch.setattr(run_module, "_write_stage_start", blip)
        monkeypatch.setattr(run_module, "_write_stage_end", blip)

        result, _ = await run(
            db_session_factory,
            origin_url,
            task,
            agent_that(StopReason.STEP_CAP),
            FakeBackend(results=[BASELINE, AFTER]),
        )

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED
        assert row.agent_stop_reason == "step_cap" and row.error_message is None
        assert [r.attempt for r in runs] == [0, 1]
        assert await stage_rows(db_session_factory, task.id) == []

    async def test_a_stage_record_that_cannot_be_written_does_not_replace_a_stage_failure(
        self, db_session, db_session_factory, origin_url, monkeypatch
    ):
        task = await seed_task(db_session)
        monkeypatch.setattr(run_module, "_write_stage_start", blip)
        monkeypatch.setattr(run_module, "_write_stage_end", blip)

        result, _ = await run(
            db_session_factory,
            origin_url,
            task,
            agent_that(StopReason.SUBMITTED),
            FakeBackend(results=[BASELINE, AFTER]),
            github=FakeGithubClient(permissions={}),
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.FAILED
        assert row.error_message.startswith("preflight: ")

    async def test_an_end_write_that_fails_leaves_the_stage_running_and_the_task_intact(
        self, db_session, db_session_factory, origin_url, monkeypatch
    ):
        task = await seed_task(db_session)
        monkeypatch.setattr(run_module, "_write_stage_end", blip)

        result, _ = await run(
            db_session_factory,
            origin_url,
            task,
            agent_that(StopReason.STEP_CAP),
            FakeBackend(results=[BASELINE, AFTER]),
        )

        assert result.status is TaskStatus.COMPLETED
        assert {r.status for r in await stage_rows(db_session_factory, task.id)} == {"running"}


class TestClaimAndHeartbeat:
    async def test_claimed_by_and_heartbeat_are_set_by_the_claim(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        seen = {}

        async def script(deps):
            row, _ = await reload(db_session_factory, task.id)
            seen["claimed_by"], seen["heartbeat_at"] = row.claimed_by, row.heartbeat_at
            return agent_result(StopReason.STEP_CAP, None)

        await run(
            db_session_factory,
            origin_url,
            task,
            ScriptedAgent(script),
            FakeBackend(results=[BASELINE, AFTER]),
            claimed_by="worker-7:42",
        )

        assert seen["claimed_by"] == "worker-7:42"
        assert seen["heartbeat_at"] is not None

    async def test_the_default_claimant_is_hostname_and_pid(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        await run(
            db_session_factory,
            origin_url,
            task,
            agent_that(StopReason.STEP_CAP),
            FakeBackend(results=[BASELINE, AFTER]),
        )

        row, _ = await reload(db_session_factory, task.id)
        assert row.claimed_by == f"{socket.gethostname()}:{os.getpid()}"

    async def test_the_heartbeat_advances_while_the_task_runs(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        beats = []

        async def script(deps):
            beats.append((await reload(db_session_factory, task.id))[0].heartbeat_at)
            await asyncio.sleep(0.5)
            beats.append((await reload(db_session_factory, task.id))[0].heartbeat_at)
            return agent_result(StopReason.STEP_CAP, None)

        await run(
            db_session_factory,
            origin_url,
            task,
            ScriptedAgent(script),
            FakeBackend(results=[BASELINE, AFTER]),
            heartbeat_interval=0.02,
        )

        assert beats[1] > beats[0]

    async def test_a_heartbeat_that_cannot_be_written_keeps_beating_and_stops_when_cancelled(self):
        calls = 0

        def dead_factory():
            nonlocal calls
            calls += 1
            raise ConnectionError("database went away")

        beating = asyncio.create_task(run_module._heartbeat(dead_factory, None, 0.01))
        await asyncio.sleep(0.1)

        assert not beating.done(), "a failed beat must not end the loop"
        assert calls >= 2
        beating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await beating

    async def test_the_heartbeat_task_is_gone_when_run_task_returns(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        before = set(asyncio.all_tasks())

        await run(
            db_session_factory,
            origin_url,
            task,
            agent_that(StopReason.STEP_CAP),
            FakeBackend(results=[BASELINE, AFTER]),
            heartbeat_interval=0.02,
        )
        await asyncio.sleep(0.05)

        leaked = [t for t in set(asyncio.all_tasks()) - before if "_heartbeat" in repr(t.get_coro())]
        assert leaked == []


class TestHardening:
    async def test_cancelling_run_task_mid_stage_fails_the_stage_and_reraises(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        entered = asyncio.Event()

        async def hang(deps):
            entered.set()
            await asyncio.sleep(3600)

        running = asyncio.create_task(
            run(db_session_factory, origin_url, task, ScriptedAgent(hang), FakeBackend(results=[BASELINE, AFTER]))
        )
        await asyncio.wait_for(entered.wait(), 60)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        row, _ = await reload(db_session_factory, task.id)
        assert row.status is TaskStatus.FAILED and row.error_message.startswith("interrupted: ")
        stages = await stage_rows(db_session_factory, task.id)
        assert (stages[-1].stage, stages[-1].status) == ("agent", "failed")
        assert stages[-1].error and "CancelledError" in stages[-1].error
        assert {r.status for r in stages[:-1]} == {"ok"}

    async def test_a_second_cancellation_during_heartbeat_teardown_is_not_swallowed(self):
        async def slow_to_die():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                raise

        beating = asyncio.create_task(slow_to_die())
        await asyncio.sleep(0)
        stopping = asyncio.create_task(run_module._stop_heartbeat(beating))
        await asyncio.sleep(0.05)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        await asyncio.wait({beating})

    async def test_a_crashed_heartbeat_does_not_raise_from_teardown(self):
        async def crash():
            raise RuntimeError("boom")

        beating = asyncio.create_task(crash())
        await asyncio.sleep(0.01)
        await run_module._stop_heartbeat(beating)

    async def test_concurrent_tasks_each_get_only_their_own_stage_rows(
        self, db_session, db_session_factory, origin_url, monkeypatch
    ):
        first = await seed_task(db_session)
        second = Task(
            repo_id=first.repo_id,
            issue_number=8,
            issue_title="another issue",
            issue_url="https://github.com/acme/sample/issues/8",
            target_branch="main",
            status=TaskStatus.QUEUED,
        )
        db_session.add(second)
        await db_session.commit()
        monkeypatch.setattr(run_module, "INDEX_WAIT_STEP_SECONDS", 0.05)

        await asyncio.gather(
            run(db_session_factory, origin_url, first, agent_that(StopReason.STEP_CAP),
                FakeBackend(results=[BASELINE, AFTER])),
            run(db_session_factory, origin_url, second, agent_that(StopReason.STEP_CAP),
                FakeBackend(results=[BASELINE, AFTER])),
        )

        for task in (first, second):
            rows = await stage_rows(db_session_factory, task.id)
            assert [r.stage for r in rows] == [*STAGES_TO_THE_REVIEW, "finalize"]
            assert {r.status for r in rows} == {"ok"}
        assert run_module._stage_sink.get() is None

    async def test_a_late_heartbeat_does_not_touch_a_finished_task(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        await run(
            db_session_factory, origin_url, task, agent_that(StopReason.STEP_CAP), FakeBackend(results=[BASELINE, AFTER])
        )
        assert run_module._stage_sink.get() is None, "run_task must not leave its sink in the caller's context"
        before = (await reload(db_session_factory, task.id))[0].heartbeat_at
        assert before is not None

        late = asyncio.create_task(run_module._heartbeat(db_session_factory, task.id, 0.01))
        await asyncio.sleep(0.2)
        late.cancel()
        await asyncio.wait({late})

        assert (await reload(db_session_factory, task.id))[0].heartbeat_at == before

    async def test_the_stage_error_is_capped(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        token = run_module._stage_sink.set(run_module._StageSink(db_session_factory, task.id))
        try:
            with pytest.raises(run_module.StageFailed):
                async with run_module._stage("clone"):
                    raise ValueError("x" * 5000)
        finally:
            run_module._stage_sink.reset(token)

        (row,) = await stage_rows(db_session_factory, task.id)
        assert len(row.error) == run_module.MAX_STAGE_ERROR_CHARS

    async def test_describing_the_failure_cannot_replace_the_stages_own_exception(
        self, db_session, db_session_factory, monkeypatch
    ):
        task = await seed_task(db_session)

        def broken(exc):
            raise RuntimeError("describing failed")

        monkeypatch.setattr(run_module, "_stage_error", broken)
        token = run_module._stage_sink.set(run_module._StageSink(db_session_factory, task.id))
        try:
            with pytest.raises(run_module.StageFailed, match="the real problem"):
                async with run_module._stage("clone"):
                    raise ValueError("the real problem")
        finally:
            run_module._stage_sink.reset(token)

    @pytest.mark.parametrize("which", ["_write_stage_start", "_write_stage_end"])
    async def test_a_hanging_stage_write_is_abandoned_without_changing_the_outcome(
        self, db_session, db_session_factory, origin_url, monkeypatch, which
    ):
        task = await seed_task(db_session)

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        monkeypatch.setattr(run_module, which, hang)
        monkeypatch.setattr(run_module, "STAGE_WRITE_TIMEOUT_SECONDS", 0.05)

        started = time.monotonic()
        result, _ = await run(
            db_session_factory, origin_url, task, agent_that(StopReason.STEP_CAP), FakeBackend(results=[BASELINE, AFTER])
        )

        assert time.monotonic() - started < 120
        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.COMPLETED
        assert row.agent_stop_reason == "step_cap" and [r.attempt for r in runs] == [0, 1]
