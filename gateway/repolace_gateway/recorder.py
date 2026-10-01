"""Persisting one model call.

Its own module, and its own session, for the reason `repolace_pipeline.testruns`
opens one: an `llm_calls` write must not ride on the pipeline's session, where
it would either commit half-written task state or be discarded together with it
when the task later fails. The second is the worse one here -- a task that
fails after spending a dollar must still show the dollar.

`record` therefore takes a session *factory*, never a session, so there is no
way to hand it the pipeline's by mistake.

A failed write is **not** swallowed. A call that happened but was not recorded
is spend `tasks.cost_usd` can never include, and the benchmark's headline
quotes cost; the same argument that pulled the gateway forward into Phase 1
says a number with holes in it is worse than a task that stops. The budget has
already been charged by then, so the in-memory cap still holds.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_gateway.redaction import Redactor
from repolace_shared.db.models import LLMCall

log = structlog.get_logger()

#: `llm_calls.cost_usd` is Numeric(14, 8). Quantised here so what is stored is
#: what the budget summed, instead of whatever Postgres rounds it to.
_COST_QUANTUM = Decimal("1e-8")


@dataclass(frozen=True)
class CallRecord:
    """Everything one `llm_calls` row holds, before redaction.

    Plain data, so a test can build one and a row can be derived from it without
    a database -- the same shape as `testruns.to_row`.
    """

    task_id: uuid.UUID
    stage: str
    model: str
    provider: str
    attempt: int | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    request: Any = None
    response: Any = None
    error: str | None = None


def to_row(record: CallRecord, redactor: Redactor) -> LLMCall:
    """Project a `CallRecord` onto the table. Pure, and where redaction happens.

    Redaction is here, not in the caller, so that no code path writes an
    `llm_calls` row without it -- there is nothing else that builds one.
    """
    return LLMCall(
        id=uuid.uuid4(),
        task_id=record.task_id,
        attempt=record.attempt,
        stage=record.stage,
        model=record.model,
        provider=record.provider,
        input_tokens=record.input_tokens,
        cached_input_tokens=record.cached_input_tokens,
        output_tokens=record.output_tokens,
        cost_usd=None if record.cost_usd is None else record.cost_usd.quantize(_COST_QUANTUM),
        latency_ms=record.latency_ms,
        request=redactor.json(record.request if record.request is not None else {}),
        response=None if record.response is None else redactor.json(record.response),
        error=None if record.error is None else redactor.text(record.error),
    )


class Recorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], redactor: Redactor) -> None:
        self._session_factory = session_factory
        self._redactor = redactor

    @property
    def redactor(self) -> Redactor:
        """Exposed so the client scrubs the error text it raises with the same rules it stores."""
        return self._redactor

    async def record(self, record: CallRecord) -> uuid.UUID:
        """Write one row on a session of its own and return the row's id."""
        row = to_row(record, self._redactor)
        # Read before the commit. After it, a session built with the default
        # `expire_on_commit=True` has expired every attribute, and touching one
        # on an async session is a MissingGreenlet -- the one defect that has
        # reached a real run in this repo. The factory in `db.session` turns
        # expiry off, but a recorder should not depend on its caller's factory.
        call_id = row.id
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
        log.info(
            "gateway.call.recorded",
            call_id=str(call_id),
            stage=record.stage,
            model=record.model,
            error=record.error is not None,
        )
        return call_id


async def total_cost(session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID) -> Decimal:
    """What a task has spent: the sum over its `llm_calls`.

    The value `tasks.cost_usd` is set to at finalize. Computed from the rows
    rather than carried in memory so it survives a worker that died and was
    resumed -- the rows are the record, the budget is only a running estimate.
    On its own session, like every other gateway write.
    """
    async with session_factory() as session:
        total = (
            await session.execute(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.task_id == task_id)
            )
        ).scalar_one()
    return Decimal(total)
