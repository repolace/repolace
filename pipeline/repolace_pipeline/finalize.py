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
* `outcome` is `None` for a task that was never scored (a failure, or an inadmissible
  instance), which must never be recorded as `failed`: that would charge an instrument
  limitation to the agent.
"""

import uuid
from decimal import Decimal

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_gateway.recorder import total_cost
from repolace_shared.db.models import LLMCall, Task, TaskOutcome, TaskStatus

log = structlog.get_logger()


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
) -> None:
    """Write the task's final row. Executes the UPDATE; the caller commits.

    Every column is required (see the module docstring). Not committed here because the
    failure path rolls back first, and the success paths commit as their last act.
    """
    await state.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=status,
            outcome=outcome,
            score_reason=score_reason,
            error_message=error_message,
            agent_stop_reason=agent_stop_reason,
            retry_count=retry_count,
            cost_usd=cost_usd,
            patch_sha=patch_sha,
            patch_diff=patch_diff,
            changed_files=changed_files,
            pr_number=pr_number,
            pr_url=pr_url,
            completed_at=func.now(),
        )
    )


async def task_cost(session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID) -> Decimal | None:
    """What the task's model calls cost, or `None` if it made none.

    The gateway's `total_cost` sums `llm_calls` and answers 0 for a task with no rows, which
    cannot tell "free" from "never priced". A task that called no model has no measurement,
    and writing 0 for it would put zeros into the benchmark's cost mean for every gold and
    stub run.
    """
    async with session_factory() as session:
        calls = (
            await session.execute(select(func.count()).select_from(LLMCall).where(LLMCall.task_id == task_id))
        ).scalar_one()
    if calls == 0:
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
