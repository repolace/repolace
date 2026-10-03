"""Writing a task's terminal state, so that no path can forget a column.

A task ends one of three ways -- `completed` (ran to the end, no PR), `pr_opened`, or
`failed` (repolace itself broke) -- and each used to write its own handful of columns by
hand. That is how `retry_count`, `cost_usd`, `patch_sha` and `changed_files` came to be
columns nothing ever wrote: a path that did not know about a column could not omit it by
mistake, because it never listed it at all.

`write_terminal` takes **every column as a required keyword**. There are no defaults, so
a new terminal path has to say what each one is -- including "nothing", which is `None` --
and a column added later is a `TypeError` at every call site rather than a quietly empty
cell. The same function writes the failure path, which is why a harness-error row can still
carry the spend and whatever the agent had produced by then.

What `None` means is not the same for every column, and the difference is the point:

* `cost_usd` is `None` when the task made no model call at all (`task_cost`). "Free" and
  "never priced" are different claims, and only the second is true for the stub and gold
  runners and for a task that failed before the agent.
* `patch_diff`, `patch_sha` and `changed_files` are `None` when the task never reached the
  agent, and `""`, the base sha and `[]` when it did and left nothing. The first says "no
  patch was ever considered"; the second says "one was, and it changed nothing".
* `cost_usd` is also `None` when the task made calls but none carries a price (every row an
  error row, say): the sum of nothing priced is not zero, it is unknown.
* `outcome` is `None` for a task that was never scored (a failure, or an inadmissible
  instance), which must never be recorded as `failed`: that would charge an instrument
  limitation to the agent.
"""

import uuid
from decimal import Decimal
from typing import TypeVar

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_gateway.recorder import total_cost
from repolace_shared.db.models import LLMCall, Task, TaskOutcome, TaskStatus

log = structlog.get_logger()

#: What Postgres cannot store in a text column is NUL, and nothing here may let it decide whether a
#: finished task is recorded. A diff can carry one (git emits a NUL that sits past its first 8000
#: bytes of "is this binary" sniffing), and a failed write after the push and the PR would leave
#: the row `running` with a pull request already open.
_NUL = "\x00"
_REPLACEMENT = "\ufffd"

_Text = TypeVar("_Text", str, list, None)


def _clean(value: _Text) -> _Text:
    """`value` with NUL replaced by U+FFFD, for a string or a list of strings; anything else as is."""
    if isinstance(value, str):
        return value.replace(_NUL, _REPLACEMENT)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


async def write_terminal(
    state: AsyncSession,
    task_id: uuid.UUID,
    *,
    status: TaskStatus,
    outcome: TaskOutcome | None,
    score_reason: str | None,
    error_message: str | None,
    agent_stop_reason: str | None,
    retry_count: int,
    cost_usd: Decimal | None,
    patch_sha: str | None,
    patch_diff: str | None,
    changed_files: list[str] | None,
    pr_number: int | None,
    pr_url: str | None,
) -> bool:
    """Write the task's final row. Executes the UPDATE; the caller commits. True if a row changed.

    Every column is required (see the module docstring). Not committed here because the
    failure path rolls back first, and the success paths commit as their last act.

    **Only a `running` row is written.** `_claim` is what made it running, so the same claim is
    what lets this write: a late or duplicate writer (Phase 2 delivers at-least-once) finds the row
    already terminal and changes nothing, instead of resurrecting a finished task. That is the
    rule the benchmark runner already applies to its own writes. Zero rows is logged, not raised:
    the task's real state is whatever the first writer recorded.

    Free text goes through `_clean` first, so a NUL byte cannot make the write fail.
    """
    result = await state.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.RUNNING)
        .values(
            status=status,
            outcome=outcome,
            score_reason=_clean(score_reason),
            error_message=_clean(error_message),
            agent_stop_reason=agent_stop_reason,
            retry_count=retry_count,
            cost_usd=cost_usd,
            patch_sha=patch_sha,
            patch_diff=_clean(patch_diff),
            changed_files=_clean(changed_files),
            pr_number=pr_number,
            pr_url=_clean(pr_url),
            completed_at=func.now(),
        )
    )
    return _written(result.rowcount, task_id, status)


async def write_failure_minimal(
    state: AsyncSession,
    task_id: uuid.UUID,
    *,
    error_message: str,
    pr_number: int | None,
    pr_url: str | None,
) -> bool:
    """The least a failed task's row can say: failed, why, when, and the PR if one is open.

    The fallback for a full failure write that itself failed (a payload the database refuses, a
    constraint nobody foresaw). A row that stays `running` while the result says FAILED is the
    worst outcome here -- it is never reaped and a pull request may already exist -- so this
    carries nothing that can be refused: no diff, no list, no cost.
    """
    result = await state.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.RUNNING)
        .values(
            status=TaskStatus.FAILED,
            error_message=_clean(error_message),
            pr_number=pr_number,
            pr_url=_clean(pr_url),
            completed_at=func.now(),
        )
    )
    return _written(result.rowcount, task_id, TaskStatus.FAILED)


def _written(rowcount: int, task_id: uuid.UUID, status: TaskStatus) -> bool:
    if rowcount == 0:
        log.warning("pipeline.terminal_write.skipped", task_id=str(task_id), wanted=status.value)
        return False
    return True


async def task_cost(session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID) -> Decimal | None:
    """What the task's model calls cost, or `None` if it made none.

    The gateway's `total_cost` sums `llm_calls` and answers 0 for a task with no rows, which
    cannot tell "free" from "never priced". A task that called no model has no measurement,
    and writing 0 for it would put zeros into the benchmark's cost mean for every gold and
    stub run. The same goes for calls that exist but carry no price (`cost_usd` is NULL on a
    call that produced no usage): a sum over nothing priced is unknown, not zero.
    """
    async with session_factory() as session:
        calls, priced = (
            await session.execute(
                select(func.count(), func.count(LLMCall.cost_usd)).where(LLMCall.task_id == task_id)
            )
        ).one()
    if calls == 0 or priced == 0:
        return None
    return await total_cost(session_factory, task_id)


async def first_model(session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID) -> str | None:
    """The first model the task called -- what the PR body names -- or `None` if it called none."""
    async with session_factory() as session:
        return (
            await session.execute(
                select(LLMCall.model).where(LLMCall.task_id == task_id).order_by(LLMCall.created_at).limit(1)
            )
        ).scalar_one_or_none()
