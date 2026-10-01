"""Projecting a call onto its row, and writing it on a session of its own.

No database here: `to_row` is pure, and the isolation tests use a fake session
factory that remembers every session it hands out. The tests that need the real
table -- including the one that proves a row survives the *pipeline's* rollback --
are in `test_recorder_db.py` under `-m db`.

The shape mirrors `pipeline/tests/test_testruns.py`, and for the same reason:
these columns exist so a run can be audited and repriced later, and a field
dropped in `to_row` is unrecoverable once the call is over.
"""

import uuid
from dataclasses import fields
from decimal import Decimal

import pytest

from repolace_gateway.recorder import CallRecord, Recorder, to_row
from repolace_gateway.redaction import REDACTED, Redactor

from gateway_support import ANTHROPIC_KEY, FOREIGN_OPENAI_KEY, TASK_ID


def record(**overrides) -> CallRecord:
    values = {"task_id": TASK_ID, "stage": "agent", "model": "anthropic/test-main", "provider": "anthropic"}
    values.update(overrides)
    return CallRecord(**values)


@pytest.fixture
def redactor() -> Redactor:
    return Redactor([ANTHROPIC_KEY])


class TestProjection:
    def test_every_field_survives(self, redactor):
        row = to_row(
            record(
                attempt=3,
                input_tokens=1000,
                cached_input_tokens=600,
                output_tokens=200,
                cost_usd=Decimal("0.004455"),
                latency_ms=812,
                request={"messages": []},
                response={"id": "msg_1"},
                error=None,
            ),
            redactor,
        )
        assert (row.task_id, row.attempt, row.stage) == (TASK_ID, 3, "agent")
        assert (row.model, row.provider) == ("anthropic/test-main", "anthropic")
        assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (1000, 600, 200)
        assert row.cost_usd == Decimal("0.004455")
        assert row.latency_ms == 812
        assert row.request == {"messages": []}
        assert row.response == {"id": "msg_1"}

    def test_no_field_of_a_call_record_is_silently_dropped(self, redactor):
        """A guard on the next field added to CallRecord: not projected and not excluded -> this fails."""
        projected = {
            "task_id", "attempt", "stage", "model", "provider", "input_tokens", "cached_input_tokens",
            "output_tokens", "cost_usd", "latency_ms", "request", "response", "error",
        }
        assert {f.name for f in fields(CallRecord)} == projected

    def test_a_fresh_id_is_generated_per_row(self, redactor):
        first, second = to_row(record(), redactor), to_row(record(), redactor)
        assert isinstance(first.id, uuid.UUID) and first.id != second.id

    def test_an_error_row_keeps_null_usage_and_cost_rather_than_zero(self, redactor):
        row = to_row(record(error="RateLimitError: slow down"), redactor)
        assert (row.input_tokens, row.cached_input_tokens, row.output_tokens, row.cost_usd) == (None,) * 4
        assert row.response is None
        assert row.error == "RateLimitError: slow down"

    def test_a_missing_request_is_stored_as_an_empty_object_not_null(self, redactor):
        """The column is NOT NULL with a `{}` default; a record built without a request must still insert."""
        assert to_row(record(request=None), redactor).request == {}

    def test_cost_is_quantised_to_the_columns_eight_decimal_places(self, redactor):
        """What is stored is what the budget summed, not whatever Postgres rounds it to."""
        assert to_row(record(cost_usd=Decimal("0.123456789")), redactor).cost_usd == Decimal("0.12345679")
        assert to_row(record(cost_usd=Decimal("0.00000001")), redactor).cost_usd == Decimal("0.00000001")


class TestRedactionIsInTheProjection:
    """Redaction lives in `to_row`, so no code path can write a row without it."""

    def test_the_request_is_scrubbed(self, redactor):
        row = to_row(
            record(request={"messages": [{"role": "tool", "content": f"OPENAI_API_KEY={FOREIGN_OPENAI_KEY}"}]}),
            redactor,
        )
        assert FOREIGN_OPENAI_KEY not in str(row.request)
        assert REDACTED in str(row.request)

    def test_the_response_is_scrubbed(self, redactor):
        row = to_row(record(response={"choices": [{"text": f"your key is {ANTHROPIC_KEY}"}]}), redactor)
        assert ANTHROPIC_KEY not in str(row.response)

    def test_the_error_is_scrubbed(self, redactor):
        row = to_row(record(error=f"401 for {ANTHROPIC_KEY}"), redactor)
        assert ANTHROPIC_KEY not in row.error

    def test_nul_characters_are_stripped_from_what_will_become_jsonb(self, redactor):
        row = to_row(record(request={"content": "a\x00b"}, response={"x": ["c\x00d"]}), redactor)
        assert row.request == {"content": "ab"} and row.response == {"x": ["cd"]}

    def test_a_pydantic_response_is_dumped_to_plain_json(self, redactor):
        from pydantic import BaseModel

        class Response(BaseModel):
            id: str

        assert to_row(record(response=Response(id="msg_1")), redactor).response == {"id": "msg_1"}


class FakeSession:
    def __init__(self) -> None:
        self.added: list = []
        self.committed = False
        self.closed = False

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.closed = True


class FakeSessionFactory:
    """Remembers every session it hands out, so a test can see how many were opened."""

    def __init__(self, session_cls=FakeSession) -> None:
        self.sessions: list[FakeSession] = []
        self._session_cls = session_cls

    def __call__(self) -> FakeSession:
        session = self._session_cls()
        self.sessions.append(session)
        return session


@pytest.mark.anyio
class TestSessionIsolation:
    async def test_each_record_opens_its_own_session_commits_it_and_closes_it(self, redactor):
        factory = FakeSessionFactory()
        recorder = Recorder(factory, redactor)

        await recorder.record(record(stage="agent"))
        await recorder.record(record(stage="cheap"))

        assert len(factory.sessions) == 2
        for session, stage in zip(factory.sessions, ("agent", "cheap")):
            assert [row.stage for row in session.added] == [stage]
            assert session.committed and session.closed

    async def test_the_returned_id_is_the_rows_id(self, redactor):
        factory = FakeSessionFactory()
        call_id = await Recorder(factory, redactor).record(record())
        assert factory.sessions[0].added[0].id == call_id

    async def test_a_failed_commit_is_raised_not_swallowed(self, redactor):
        class FailingSession(FakeSession):
            async def commit(self) -> None:
                raise RuntimeError("connection lost")

        recorder = Recorder(FakeSessionFactory(FailingSession), redactor)
        with pytest.raises(RuntimeError, match="connection lost"):
            await recorder.record(record())

    async def test_the_session_is_closed_even_when_the_commit_fails(self, redactor):
        class FailingSession(FakeSession):
            async def commit(self) -> None:
                raise RuntimeError("connection lost")

        factory = FakeSessionFactory(FailingSession)
        with pytest.raises(RuntimeError):
            await Recorder(factory, redactor).record(record())
        assert factory.sessions[0].closed

    async def test_the_recorder_exposes_the_redactor_it_stores_with(self, redactor):
        assert Recorder(FakeSessionFactory(), redactor).redactor is redactor
