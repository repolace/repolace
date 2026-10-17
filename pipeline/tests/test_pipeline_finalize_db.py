"""The terminal write and the two measurements it records, against a real database.

What matters is what the write refuses to do. It writes only a `running` row (so a late or duplicate
writer cannot resurrect a finished task), it cannot be made to fail by a NUL byte, and `task_cost` keeps
"never priced" apart from "free" in both ways it can arise.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from pipeline_support import reload, seed_task
from repolace_gateway.recorder import CallRecord, Recorder
from repolace_gateway.redaction import Redactor
from repolace_pipeline.finalize import (
    _clean,
    first_model,
    task_cost,
    write_failure_minimal,
    write_terminal,
)
from repolace_shared.db.models import TaskOutcome, TaskStatus
from sqlalchemy import func, select

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def columns(**overrides):
    values = {
        "status": TaskStatus.COMPLETED, "outcome": TaskOutcome.FAILED, "score_reason": "because", "error_message": None,
        "agent_stop_reason": "submitted", "retry_count": 1, "cost_usd": Decimal("0.25"), "patch_sha": "a" * 40,
        "patch_diff": "diff", "changed_files": ["a.py"], "pr_number": None, "pr_url": None,
        "conflict_reason": None,
    }
    return {**values, **overrides}


async def write(factory, task_id, **overrides) -> bool:
    async with factory() as session:
        wrote = await write_terminal(session, task_id, **columns(**overrides))
        await session.commit()
    return wrote


async def mark(factory, task_id, status: TaskStatus) -> None:
    from repolace_shared.db.models import Task
    from sqlalchemy import update

    async with factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(status=status))
        await session.commit()


async def call(factory, task_id, cost: str | None, model: str = "claude-sonnet-5-5") -> None:
    await Recorder(factory, Redactor()).record(
        CallRecord(
            task_id=task_id, stage="agent", model=model, provider="anthropic",
            cost_usd=None if cost is None else Decimal(cost),
        )
    )


class TestTheTerminalTimestampIsTheRealTime:
    """The task's session holds one transaction open for the whole run; `now()` would be its start."""

    @pytest.mark.parametrize("writer", ["terminal", "minimal"])
    async def test_completed_at_is_when_the_write_happened_not_when_the_transaction_began(
        self, db_session, db_session_factory, writer
    ):
        task = await seed_task(db_session, status=TaskStatus.RUNNING)

        async with db_session_factory() as session:
            await session.execute(select(1))  # begins the transaction, as loading the task row does
            began = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            await asyncio.sleep(1.2)
            if writer == "terminal":
                assert await write_terminal(session, task.id, **columns())
            else:
                assert await write_failure_minimal(
                    session, task.id, error_message="x", pr_number=None, pr_url=None
                )
            await session.commit()

        row, _ = await reload(db_session_factory, task.id)
        assert (row.completed_at - began).total_seconds() >= 1.0
        assert (row.updated_at - began).total_seconds() >= 1.0


class TestOnlyARunningRowIsWritten:
    async def test_a_running_row_is_written(self, db_session, db_session_factory):
        task = await seed_task(db_session, status=TaskStatus.RUNNING)

        assert await write(db_session_factory, task.id) is True

        row, _ = await reload(db_session_factory, task.id)
        assert (row.status, row.retry_count, row.patch_diff) == (TaskStatus.COMPLETED, 1, "diff")
        assert row.completed_at is not None

    @pytest.mark.parametrize("terminal", [TaskStatus.COMPLETED, TaskStatus.PR_OPENED, TaskStatus.FAILED])
    async def test_a_row_that_is_already_terminal_is_left_exactly_as_it_was(self, db_session, db_session_factory, terminal):
        """Phase 2 delivers at-least-once: the second writer must find a finished task finished."""
        task = await seed_task(db_session, status=TaskStatus.RUNNING)
        await write(db_session_factory, task.id, status=terminal, score_reason="first writer", patch_diff="first")

        assert await write(db_session_factory, task.id, score_reason="late writer", patch_diff="late") is False

        row, _ = await reload(db_session_factory, task.id)
        assert (row.status, row.score_reason, row.patch_diff) == (terminal, "first writer", "first")

    async def test_a_row_that_was_never_claimed_is_not_written(self, db_session, db_session_factory):
        task = await seed_task(db_session)

        assert await write(db_session_factory, task.id) is False

        row, _ = await reload(db_session_factory, task.id)
        assert row.status is TaskStatus.QUEUED and row.completed_at is None

    async def test_the_minimal_failure_write_follows_the_same_rule(self, db_session, db_session_factory):
        task = await seed_task(db_session, status=TaskStatus.RUNNING)
        await write(db_session_factory, task.id, score_reason="first writer")

        async with db_session_factory() as session:
            wrote = await write_failure_minimal(session, task.id, error_message="late", pr_number=3, pr_url="u")
            await session.commit()

        row, _ = await reload(db_session_factory, task.id)
        assert wrote is False
        assert row.status is TaskStatus.COMPLETED and row.error_message is None and row.pr_number is None

    async def test_the_minimal_failure_write_says_failed_why_and_the_pr(self, db_session, db_session_factory):
        task = await seed_task(db_session, status=TaskStatus.RUNNING)

        async with db_session_factory() as session:
            wrote = await write_failure_minimal(
                session, task.id, error_message="finalize: boom\x00", pr_number=3, pr_url="https://x/pull/3"
            )
            await session.commit()

        row, _ = await reload(db_session_factory, task.id)
        assert wrote is True
        assert row.status is TaskStatus.FAILED and row.error_message == "finalize: boom�"
        assert (row.pr_number, row.pr_url) == (3, "https://x/pull/3") and row.completed_at is not None
        assert row.patch_diff is None and row.cost_usd is None


class TestAnUnstorableCharacterCannotFailTheWrite:
    def test_nul_is_replaced_in_a_string_and_in_a_list(self):
        assert _clean("a\x00b") == "a�b"
        assert _clean(["x\x00", "y"]) == ["x�", "y"]

    def test_everything_else_is_returned_as_it_was(self):
        assert _clean(None) is None and _clean("plain") == "plain" and _clean([]) == []

    async def test_a_nul_in_every_free_text_column_is_written_not_refused(self, db_session, db_session_factory):
        task = await seed_task(db_session, status=TaskStatus.RUNNING)

        wrote = await write(
            db_session_factory, task.id,
            score_reason="r\x00", error_message="e\x00", patch_diff="d\x00", changed_files=["f\x00.py"], pr_url="u\x00",
        )

        row, _ = await reload(db_session_factory, task.id)
        assert wrote is True
        assert (row.score_reason, row.error_message, row.patch_diff) == ("r�", "e�", "d�")
        assert row.changed_files == ["f�.py"] and row.pr_url == "u�"


class TestTaskCost:
    async def test_no_calls_is_unknown_not_zero(self, db_session, db_session_factory):
        task = await seed_task(db_session)

        assert await task_cost(db_session_factory, task.id) is None

    async def test_calls_that_carry_no_price_are_unknown_not_zero(self, db_session, db_session_factory):
        """A call with no usage has a NULL cost. The sum of nothing priced is not zero."""
        task = await seed_task(db_session)
        await call(db_session_factory, task.id, None)
        await call(db_session_factory, task.id, None)

        assert await task_cost(db_session_factory, task.id) is None

    async def test_a_priced_call_among_unpriced_ones_is_summed(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        await call(db_session_factory, task.id, None)
        await call(db_session_factory, task.id, "0.25")
        await call(db_session_factory, task.id, "0.50")

        assert await task_cost(db_session_factory, task.id) == Decimal("0.75")

    async def test_a_real_zero_cost_is_zero(self, db_session, db_session_factory):
        """Priced at nothing is a claim; unpriced is not. A free model is `0`, not unknown."""
        task = await seed_task(db_session)
        await call(db_session_factory, task.id, "0")

        assert await task_cost(db_session_factory, task.id) == Decimal(0)

    async def test_another_tasks_spend_is_not_included(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        await call(db_session_factory, task.id, "0.25")

        assert await task_cost(db_session_factory, uuid.uuid4()) is None


class TestFirstModel:
    async def test_it_is_the_first_model_called(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        await call(db_session_factory, task.id, "0.1", model="first-model")
        await call(db_session_factory, task.id, "0.1", model="second-model")

        assert await first_model(db_session_factory, task.id) == "first-model"

    async def test_none_when_no_model_was_called(self, db_session, db_session_factory):
        task = await seed_task(db_session)

        assert await first_model(db_session_factory, task.id) is None
