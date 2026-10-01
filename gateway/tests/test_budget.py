"""The per-task cap: its arithmetic, its two checks, and the scope that carries it.

The semantics worth pinning are the ones a future edit could flip without any
test noticing: a *call that lands exactly on the cap is allowed* (so
`max_calls=3` returns three responses, not two), while a task that has *reached*
the cap is refused a further call. Pre-call is ``>=``, post-call is ``>``, and
that asymmetry is deliberate.
"""

import asyncio
from decimal import Decimal

import pytest
import structlog

from repolace_gateway.budget import (
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_USD,
    DEFAULT_MAX_WALL_SECONDS,
    BudgetExceeded,
    BudgetLimit,
    TaskBudget,
    current_scope,
    task_scope,
)
from repolace_gateway.errors import NoTaskScope

from gateway_support import TASK_ID


class TestArithmetic:
    def test_the_defaults_are_the_plans_two_dollar_cap(self):
        budget = TaskBudget()
        assert budget.max_usd == DEFAULT_MAX_USD == Decimal("2.00")
        assert budget.max_calls == DEFAULT_MAX_CALLS
        assert budget.max_wall_seconds == DEFAULT_MAX_WALL_SECONDS

    def test_a_float_cap_is_held_as_an_exact_decimal(self):
        """`TaskBudget(max_usd=2.0)` is the natural spelling; it must not carry float error."""
        budget = TaskBudget(max_usd=0.1)
        assert isinstance(budget.max_usd, Decimal)
        assert budget.max_usd == Decimal("0.1")

    def test_sums_do_not_drift(self):
        """Why money is a Decimal: three tenths of a dollar are exactly 0.3, not 0.30000000000000004."""
        budget = TaskBudget()
        for _ in range(3):
            budget.charge(Decimal("0.1"))
        assert budget.spent_usd == Decimal("0.3")

    def test_an_unpriced_call_counts_but_adds_no_spend(self):
        budget = TaskBudget()
        budget.charge(None)
        assert (budget.calls, budget.spent_usd) == (1, Decimal(0))

    def test_remaining_never_goes_negative(self):
        budget = TaskBudget(max_usd=1)
        budget.charge(Decimal("1.50"))
        assert budget.remaining_usd == Decimal(0)


class TestBeforeACall:
    def test_below_every_limit_is_allowed(self):
        budget = TaskBudget(max_usd=2, max_calls=3)
        budget.charge(Decimal("1.99"))
        budget.raise_if_reached()

    def test_having_spent_exactly_the_cap_refuses_the_next_call(self):
        budget = TaskBudget(max_usd=2)
        budget.charge(Decimal("2.00"))
        with pytest.raises(BudgetExceeded) as raised:
            budget.raise_if_reached()
        assert raised.value.limit is BudgetLimit.USD
        assert raised.value.response is None

    def test_having_made_the_maximum_number_of_calls_refuses_the_next(self):
        budget = TaskBudget(max_calls=2)
        budget.charge(None)
        budget.raise_if_reached()
        budget.charge(None)
        with pytest.raises(BudgetExceeded) as raised:
            budget.raise_if_reached()
        assert raised.value.limit is BudgetLimit.CALLS

    def test_the_wall_clock_is_measured_from_construction(self):
        now = [1000.0]
        budget = TaskBudget(max_wall_seconds=60, clock=lambda: now[0])
        now[0] = 1059.9
        budget.raise_if_reached()
        now[0] = 1060.0
        with pytest.raises(BudgetExceeded) as raised:
            budget.raise_if_reached()
        assert raised.value.limit is BudgetLimit.WALL_TIME


class TestAfterACall:
    def test_landing_exactly_on_the_cap_is_within_it(self):
        budget = TaskBudget(max_usd=2)
        budget.charge(Decimal("2.00"))
        budget.raise_if_exceeded()

    def test_crossing_the_cap_raises_and_carries_the_response(self):
        budget = TaskBudget(max_usd=2)
        budget.charge(Decimal("2.01"))
        sentinel = object()
        with pytest.raises(BudgetExceeded) as raised:
            budget.raise_if_exceeded(sentinel)
        assert raised.value.limit is BudgetLimit.USD
        assert raised.value.response is sentinel
        assert raised.value.spent_usd == Decimal("2.01")

    def test_max_calls_means_that_many_calls_come_back(self):
        budget = TaskBudget(max_calls=3)
        for _ in range(3):
            budget.charge(None)
            budget.raise_if_exceeded()
        budget.charge(None)
        with pytest.raises(BudgetExceeded) as raised:
            budget.raise_if_exceeded()
        assert raised.value.limit is BudgetLimit.CALLS

    def test_the_message_names_the_limit_and_the_spend(self):
        budget = TaskBudget(max_usd=1)
        budget.charge(Decimal("1.25"))
        with pytest.raises(BudgetExceeded, match=r"usd.*1\.25"):
            budget.raise_if_exceeded()


class TestTaskScope:
    def test_outside_a_scope_there_is_nothing_to_charge(self):
        with pytest.raises(NoTaskScope):
            current_scope()

    def test_inside_a_scope_the_task_and_budget_are_available(self):
        budget = TaskBudget(max_usd=5)
        with task_scope(TASK_ID, budget) as scope:
            assert current_scope() is scope
            assert scope.task_id == TASK_ID
            assert scope.budget is budget

    def test_a_scope_without_a_budget_gets_the_default_one(self):
        with task_scope(TASK_ID) as scope:
            assert scope.budget.max_usd == DEFAULT_MAX_USD

    def test_the_scope_ends_with_the_block_even_when_it_raises(self):
        with pytest.raises(RuntimeError):
            with task_scope(TASK_ID):
                raise RuntimeError("task blew up")
        with pytest.raises(NoTaskScope):
            current_scope()

    def test_nested_scopes_restore_the_outer_one(self):
        outer_id, inner_id = TASK_ID, TASK_ID.__class__(int=TASK_ID.int + 1)
        with task_scope(outer_id):
            with task_scope(inner_id):
                assert current_scope().task_id == inner_id
            assert current_scope().task_id == outer_id

    def test_the_task_id_is_bound_into_the_log_context_and_released(self):
        structlog.contextvars.clear_contextvars()
        with task_scope(TASK_ID):
            assert structlog.contextvars.get_contextvars()["task_id"] == str(TASK_ID)
        assert "task_id" not in structlog.contextvars.get_contextvars()

    @pytest.mark.anyio
    async def test_concurrent_tasks_each_see_their_own_scope(self):
        """The reason it is a contextvar: two tasks sharing a worker must not charge each other."""
        other_id = TASK_ID.__class__(int=TASK_ID.int + 1)
        seen: dict[str, object] = {}

        async def run(label: str, task_id) -> None:
            with task_scope(task_id):
                await asyncio.sleep(0)  # yield, so the other task runs inside its own scope
                seen[label] = current_scope().task_id

        await asyncio.gather(run("a", TASK_ID), run("b", other_id))
        assert seen == {"a": TASK_ID, "b": other_id}
