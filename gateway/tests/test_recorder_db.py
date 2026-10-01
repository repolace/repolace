"""Database-backed tests for `llm_calls` and the new `tasks` columns.

Under `-m db`, like `shared/tests/test_db_schema.py`, and for the same reason:
the one defect that ever reached a real run was in a database path with no
coverage. What matters here is not that SQLAlchemy works but the properties the
design leans on:

* a recorded call **survives the pipeline session's rollback** -- the whole point
  of the recorder owning its own session, because a task that fails after
  spending a dollar must still show the dollar;
* what lands in JSONB has been redacted and cannot contain a NUL, which Postgres
  rejects;
* deleting a task takes its calls with it, and a negative attempt is refused.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from repolace_gateway.budget import TaskBudget, task_scope
from repolace_gateway.client import LLMClient
from repolace_gateway.recorder import CallRecord, Recorder, total_cost
from repolace_gateway.redaction import REDACTED, Redactor
from repolace_shared.db.models import LLMCall, Task

from gateway_support import (
    ANTHROPIC_KEY,
    FOREIGN_OPENAI_KEY,
    MESSAGES,
    TASK_ID,
    TOOLS,
    FakeAcompletion,
    make_config,
    make_response,
    make_settings,
    seed_task,
)

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def record(**overrides) -> CallRecord:
    values = {"task_id": TASK_ID, "stage": "agent", "model": "anthropic/test-main", "provider": "anthropic"}
    values.update(overrides)
    return CallRecord(**values)


async def all_calls(session) -> list[LLMCall]:
    return list((await session.execute(select(LLMCall).order_by(LLMCall.created_at))).scalars())


class TestRoundTrip:
    async def test_a_full_call_round_trips(self, db_session, db_session_factory):
        await seed_task(db_session)
        recorder = Recorder(db_session_factory, Redactor())

        call_id = await recorder.record(
            record(
                attempt=2,
                input_tokens=1000,
                cached_input_tokens=600,
                output_tokens=200,
                cost_usd=Decimal("0.00123456"),
                latency_ms=812,
                request={"messages": [{"role": "user", "content": "hi"}], "params": {"max_tokens": 10}},
                response={"usage": {"prompt_tokens": 1000}},
            )
        )

        (row,) = await all_calls(db_session)
        assert row.id == call_id
        assert (row.task_id, row.attempt, row.stage) == (TASK_ID, 2, "agent")
        assert (row.model, row.provider) == ("anthropic/test-main", "anthropic")
        assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (1000, 600, 200)
        assert row.latency_ms == 812
        assert row.request["messages"][0]["content"] == "hi"
        assert row.response == {"usage": {"prompt_tokens": 1000}}
        assert row.error is None
        assert row.created_at is not None

    async def test_cost_comes_back_as_a_decimal_with_eight_places(self, db_session, db_session_factory):
        """Numeric(14, 8): a single cheap call prices in thousandths of a cent, and must not round to zero."""
        await seed_task(db_session)
        await Recorder(db_session_factory, Redactor()).record(record(cost_usd=Decimal("0.00000123")))

        (row,) = await all_calls(db_session)
        assert isinstance(row.cost_usd, Decimal)
        assert row.cost_usd == Decimal("0.00000123")

    async def test_an_error_row_stores_nulls_not_zeros(self, db_session, db_session_factory):
        await seed_task(db_session)
        await Recorder(db_session_factory, Redactor()).record(record(error="RateLimitError: slow down"))

        (row,) = await all_calls(db_session)
        assert (row.input_tokens, row.output_tokens, row.cost_usd, row.response) == (None, None, None, None)
        assert row.error == "RateLimitError: slow down"
        assert row.request == {}

    async def test_a_call_outside_any_attempt_stores_no_attempt(self, db_session, db_session_factory):
        await seed_task(db_session)
        await Recorder(db_session_factory, Redactor()).record(record(attempt=None))
        assert (await all_calls(db_session))[0].attempt is None


class TestWhatReachesJsonb:
    async def test_a_nul_character_does_not_fail_the_insert(self, db_session, db_session_factory):
        """Postgres rejects \\u0000 in JSONB; one binary-looking file in a tool result would fail the task."""
        await seed_task(db_session)
        await Recorder(db_session_factory, Redactor()).record(
            record(request={"messages": [{"content": "before\x00after"}]}, response={"text": "a\x00b"})
        )

        (row,) = await all_calls(db_session)
        assert row.request["messages"][0]["content"] == "beforeafter"
        assert row.response == {"text": "ab"}

    async def test_secrets_are_scrubbed_before_they_are_stored(self, db_session, db_session_factory):
        await seed_task(db_session)
        recorder = Recorder(db_session_factory, Redactor([ANTHROPIC_KEY]))
        await recorder.record(
            record(
                request={"messages": [{"role": "tool", "content": f"OPENAI_API_KEY={FOREIGN_OPENAI_KEY}"}]},
                response={"echo": ANTHROPIC_KEY},
                error=f"401 for {ANTHROPIC_KEY}",
            )
        )

        # Read back as text, so the assertion covers the stored JSON, not our Python view of it.
        stored = (
            await db_session.execute(text("SELECT request::text || response::text || error FROM llm_calls"))
        ).scalar_one()
        assert FOREIGN_OPENAI_KEY not in stored and ANTHROPIC_KEY not in stored
        assert REDACTED in stored


class TestSessionOwnership:
    async def test_a_recorded_call_survives_the_pipeline_sessions_rollback(self, db_session, db_session_factory):
        """The reason the recorder takes a factory: recording must never ride the pipeline's transaction.

        A task that fails after spending a dollar rolls its own session back. The
        dollar has to stay on the record.
        """
        await seed_task(db_session)
        recorder = Recorder(db_session_factory, Redactor())

        async with db_session_factory() as pipeline:
            pipeline_task = await pipeline.get(Task, TASK_ID)
            pipeline_task.issue_body = "half-written pipeline state"
            await pipeline.flush()  # the pipeline's work is in flight, uncommitted

            await recorder.record(record(cost_usd=Decimal("0.50")))

            await pipeline.rollback()  # the task fails

        calls = await all_calls(db_session)
        assert [call.cost_usd for call in calls] == [Decimal("0.50")]
        # And the rollback really did discard the pipeline's own work: the two were independent.
        body = (await db_session.execute(select(Task.issue_body))).scalar_one()
        assert body is None

    async def test_recording_does_not_commit_the_pipelines_pending_work(self, db_session, db_session_factory):
        """The mirror image: recording must not publish half-written task state either."""
        await seed_task(db_session)
        recorder = Recorder(db_session_factory, Redactor())

        async with db_session_factory() as pipeline:
            pipeline_task = await pipeline.get(Task, TASK_ID)
            pipeline_task.issue_body = "uncommitted"
            await pipeline.flush()

            await recorder.record(record())

            body_seen_by_others = (await db_session.execute(select(Task.issue_body))).scalar_one()
            assert body_seen_by_others is None
            await pipeline.rollback()


class TestTotalCost:
    async def test_it_sums_the_tasks_calls_and_ignores_unpriced_error_rows(self, db_session, db_session_factory):
        await seed_task(db_session)
        recorder = Recorder(db_session_factory, Redactor())
        await recorder.record(record(cost_usd=Decimal("0.10")))
        await recorder.record(record(cost_usd=Decimal("0.25")))
        await recorder.record(record(error="failed"))  # no cost: contributes nothing, and does not break the sum

        assert await total_cost(db_session_factory, TASK_ID) == Decimal("0.35")

    async def test_a_task_with_no_calls_has_spent_nothing(self, db_session, db_session_factory):
        await seed_task(db_session)
        assert await total_cost(db_session_factory, TASK_ID) == Decimal(0)

    async def test_only_the_given_tasks_calls_are_counted(self, db_session, db_session_factory):
        await seed_task(db_session)
        other = TASK_ID.__class__(int=TASK_ID.int + 1)
        db_session.add(
            Task(
                id=other,
                repo_id=(await db_session.execute(select(Task.repo_id))).scalar_one(),
                issue_number=2,
                issue_title="another",
                issue_url="https://github.com/acme/sample/issues/2",
                target_branch="main",
            )
        )
        await db_session.commit()
        recorder = Recorder(db_session_factory, Redactor())
        await recorder.record(record(cost_usd=Decimal("0.10")))
        await recorder.record(record(task_id=other, cost_usd=Decimal("9.00")))

        assert await total_cost(db_session_factory, TASK_ID) == Decimal("0.10")


class TestConstraints:
    async def test_a_negative_attempt_is_rejected(self, db_session):
        await seed_task(db_session)
        db_session.add(
            LLMCall(task_id=TASK_ID, attempt=-1, stage="agent", model="m", provider="p", request={})
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()

    async def test_attempt_zero_and_null_are_both_allowed(self, db_session):
        await seed_task(db_session)
        db_session.add_all(
            [
                LLMCall(task_id=TASK_ID, attempt=0, stage="agent", model="m", provider="p", request={}),
                LLMCall(task_id=TASK_ID, attempt=None, stage="agent", model="m", provider="p", request={}),
            ]
        )
        await db_session.commit()
        assert len(await all_calls(db_session)) == 2

    async def test_a_call_for_a_task_that_does_not_exist_is_rejected(self, db_session):
        await seed_task(db_session)
        db_session.add(
            LLMCall(
                task_id=TASK_ID.__class__(int=TASK_ID.int + 99),
                stage="agent", model="m", provider="p", request={},
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()

    async def test_deleting_a_task_takes_its_calls_with_it(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        await Recorder(db_session_factory, Redactor()).record(record())
        assert len(await all_calls(db_session)) == 1

        await db_session.delete(task)
        await db_session.commit()

        assert await all_calls(db_session) == []

    async def test_the_task_and_created_at_index_exists(self, db_session):
        names = (
            await db_session.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'llm_calls'"))
        ).scalars().all()
        assert "ix_llm_calls_task_id_created_at" in names


class TestTaskColumns:
    async def test_the_new_columns_default_to_null(self, db_session):
        task = await seed_task(db_session)
        fresh = (await db_session.execute(select(Task).where(Task.id == task.id))).scalar_one()
        assert (fresh.issue_body, fresh.eval_run_id, fresh.instance_id, fresh.run_index) == (None,) * 4

    async def test_the_new_columns_round_trip(self, db_session):
        await seed_task(
            db_session,
            issue_body="parse_config raises on an empty file.\n\nSteps: ...",
            eval_run_id="sweep-2026-10-sonnet",
            instance_id="psf__requests-2317",
            run_index=2,
        )
        row = (
            await db_session.execute(
                select(Task.issue_body, Task.eval_run_id, Task.instance_id, Task.run_index)
            )
        ).one()
        assert row == (
            "parse_config raises on an empty file.\n\nSteps: ...",
            "sweep-2026-10-sonnet",
            "psf__requests-2317",
            2,
        )


class TestThroughTheClient:
    """The real `Recorder` behind the real `LLMClient`, with only the provider faked."""

    async def test_a_call_leaves_a_scrubbed_priced_row(self, db_session, db_session_factory):
        await seed_task(db_session)
        client = LLMClient(
            make_config(),
            make_settings(),
            Recorder(db_session_factory, Redactor(make_settings().secret_values())),
            acompletion=FakeAcompletion(
                make_response("ok", prompt_tokens=1000, completion_tokens=200, cache_read=600, cache_write=100)
            ),
        )
        messages = [*MESSAGES, {"role": "tool", "tool_call_id": "c", "content": f"KEY={FOREIGN_OPENAI_KEY}"}]

        with task_scope(TASK_ID, TaskBudget()):
            response = await client.complete("agent", messages, TOOLS, attempt=1)

        (row,) = await all_calls(db_session)
        assert row.id == response.call_id
        assert row.cost_usd == Decimal("0.004455")
        assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (1000, 600, 200)
        assert row.attempt == 1 and row.stage == "agent"
        assert row.response["usage"]["prompt_tokens"] == 1000
        assert row.request["gateway"]["model_key"] == "main"
        stored = (await db_session.execute(text("SELECT request::text FROM llm_calls"))).scalar_one()
        assert FOREIGN_OPENAI_KEY not in stored and ANTHROPIC_KEY not in stored

    async def test_a_failed_call_leaves_an_error_row_and_no_cost(self, db_session, db_session_factory):
        from litellm import exceptions as lx

        from repolace_gateway.errors import LLMCallError

        await seed_task(db_session)
        client = LLMClient(
            make_config(),
            make_settings(),
            Recorder(db_session_factory, Redactor(make_settings().secret_values())),
            acompletion=FakeAcompletion(
                lx.AuthenticationError(message="bad key", llm_provider="anthropic", model="test-main")
            ),
        )
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(LLMCallError):
                await client.complete("agent", MESSAGES)

        (row,) = await all_calls(db_session)
        assert "AuthenticationError" in row.error
        assert (row.cost_usd, row.input_tokens, row.response) == (None, None, None)

    async def test_a_tasks_cost_is_the_sum_of_what_its_calls_recorded(self, db_session, db_session_factory):
        await seed_task(db_session)
        client = LLMClient(
            make_config(),
            make_settings(),
            Recorder(db_session_factory, Redactor(make_settings().secret_values())),
            acompletion=FakeAcompletion(
                make_response(prompt_tokens=1000, completion_tokens=100),
                repeat_last=True,
            ),
        )
        with task_scope(TASK_ID, TaskBudget()) as scope:
            for _ in range(3):
                await client.complete("agent", MESSAGES)

        # 1000 x $3/M + 100 x $15/M = $0.0045 a call.
        assert await total_cost(db_session_factory, TASK_ID) == Decimal("0.0135")
        assert await total_cost(db_session_factory, TASK_ID) == scope.budget.spent_usd
