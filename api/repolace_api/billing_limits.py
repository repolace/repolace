"""The monthly task allowance by plan.

PREVIEW NUMBERS: like the prices on the public `/pricing` page, these are hypotheses to be
validated against real cost per task, not commitments. Change them here only.

Not wired into `enqueue_task` yet, deliberately: `tasks` has no owner column, so "tasks this user
started this month" cannot be counted. Wiring it needs a `tasks.user_id` migration (nullable,
set by the enqueue path from the signed-in user); then the enqueue path calls
`plan_for_user(...)`, counts that user's non-benchmark tasks since `month_start(now)`, and calls
`assert_within_allowance(plan, used)`. Until then the function is exercised on its own.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from repolace_shared.db.models import Subscription
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
