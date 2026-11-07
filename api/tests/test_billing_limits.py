"""The monthly allowance by plan, on its own (it is not wired into `enqueue_task`; see the module)."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from repolace_api.billing_limits import (
    FREE_TASKS_PER_MONTH,
    PRO_TASKS_PER_MONTH,
    allowance_for,
    assert_within_allowance,
    month_start,
    plan_for_user,
)


def test_allowances_by_plan():
    assert allowance_for("free") == FREE_TASKS_PER_MONTH == 2
    assert allowance_for("pro") == PRO_TASKS_PER_MONTH == 60


def test_an_unknown_plan_gets_the_free_allowance_never_more():
    assert allowance_for("enterprise-typo") == FREE_TASKS_PER_MONTH


def test_under_the_allowance_passes():
    assert_within_allowance("free", FREE_TASKS_PER_MONTH - 1)
    assert_within_allowance("pro", FREE_TASKS_PER_MONTH)


@pytest.mark.parametrize("plan,limit", [("free", FREE_TASKS_PER_MONTH), ("pro", PRO_TASKS_PER_MONTH)])
def test_reaching_the_allowance_is_a_402_naming_the_limit(plan, limit):
    with pytest.raises(HTTPException) as caught:
        assert_within_allowance(plan, limit)
    assert caught.value.status_code == 402
    assert str(limit) in caught.value.detail


def test_month_start_is_the_first_of_the_month_utc():
    assert month_start(datetime(2026, 11, 7, 13, 5, tzinfo=UTC)) == datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.anyio
@pytest.mark.db
async def test_plan_for_user_defaults_to_free_and_follows_the_row(db_session):
    from api_billing_support import seed_subscription, seed_user

    user = await seed_user(db_session)
    assert await plan_for_user(db_session, user.id) == "free"
    await seed_subscription(db_session, user, plan="pro", status="active")
    assert await plan_for_user(db_session, user.id) == "pro"
