"""The monthly task allowance by plan.

PREVIEW NUMBERS: like the prices on the public `/pricing` page, these are hypotheses to be
validated against real cost per task, not commitments. Change them here only.

Wired into `enqueue_task` through the `enqueue_owner` dependency, and only when sign-in is configured
(`BillingSettings.signin_ready`) AND someone is signed in. Otherwise no owner is recorded and no limit
applies, so the benchmark runner, the CLI and a local install with no billing behave as they always did.
Counting is by `tasks.user_id` (migration 0019) over non-benchmark rows since `month_start(now)`.
"""

import uuid
from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from repolace_shared.db.models import Subscription, Task, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_api.config import BillingSettings, get_billing_settings
from repolace_api.deps import optional_user

FREE_TASKS_PER_MONTH = 5
PRO_TASKS_PER_MONTH = 100

_ALLOWANCES = {"free": FREE_TASKS_PER_MONTH, "pro": PRO_TASKS_PER_MONTH}


def allowance_for(plan: str) -> int:
    """Tasks per calendar month (UTC). An unknown plan name gets the free allowance, never more."""
    return _ALLOWANCES.get(plan, FREE_TASKS_PER_MONTH)


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC)


async def plan_for_user(session: AsyncSession, user_id: uuid.UUID) -> str:
    """`pro` or `free`, from the subscription row the verified webhooks keep. No row is `free`."""
    plan = await session.scalar(select(Subscription.plan).where(Subscription.user_id == user_id))
    return plan if plan in _ALLOWANCES else "free"


def assert_within_allowance(plan: str, used: int) -> None:
    """Raise 402 when `used` tasks this month already reach the plan's allowance."""
    limit = allowance_for(plan)
    if used >= limit:
        raise HTTPException(
            status_code=402,
            detail=f"the {plan} plan allows {limit} tasks per month and {used} have been started this month",
        )


async def enqueue_owner(
    user: User | None = Depends(optional_user),
    settings: BillingSettings = Depends(get_billing_settings),
) -> uuid.UUID | None:
    """The id to record as the task's owner and meter, or None: sign-in not configured, or nobody
    signed in. None means "behave exactly as before"."""
    if user is None or not settings.signin_ready:
        return None
    return user.id


async def tasks_started_this_month(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.user_id == user_id, Task.eval_run_id.is_(None), Task.created_at >= month_start(now))
        )
        or 0
    )


async def enforce_allowance(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Raise 402 when the user has used this month's allowance. Takes a transaction-scoped advisory
    lock per user so two concurrent enqueues cannot both read the same count; it is released by the
    caller's commit, which follows straight after the insert."""
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"tasks-allowance:{user_id}", 0))))
    plan = await plan_for_user(db, user_id)
    assert_within_allowance(plan, await tasks_started_this_month(db, user_id))
