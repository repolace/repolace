"""The per-task spending cap, and the contextvar that carries it.

The cap lives in the gateway rather than in the agent for the same reason cost
recording does: every model call goes through here, so it is the one place a
runaway loop can actually be stopped. An agent that polices its own budget has
to be trusted to ask.

Semantics, which the tests pin:

* **Before** a call, `raise_if_reached` refuses once any limit has been *reached*
  (``>=``). A task that has spent exactly its cap has nothing left to spend.
* **After** a call, `raise_if_exceeded` refuses only if a limit has been
  *exceeded* (``>``). A call that lands exactly on the cap is within it, and its
  response is returned -- so ``max_calls=3`` means three calls come back, not two.
* The call that crosses the cap has already been paid for, so it is recorded and
  its response rides on the exception (`BudgetExceeded.response`) rather than
  being thrown away. The agent treats the exception as "finalize with what you
  have", never as a crash.

The cap is soft by at most the calls in flight. Two concurrent `complete()`s in
one task can both pass the pre-check before either charges. The agent loop is
sequential, so this does not arise today; it is a limit worth knowing about.
"""

import enum
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from repolace_gateway.errors import GatewayError, NoTaskScope

log = structlog.get_logger()

#: The $2 hard cap from the Phase 1 plan (decision 7).
DEFAULT_MAX_USD = Decimal("2.00")
#: 3 attempts x the agent's 40-step cap = 120 calls, plus slack for retrieval-time
#: calls and the occasional failed call, which counts (see `charge`).
DEFAULT_MAX_CALLS = 150
#: The agent stage, with the scored suites run inside it -- `run_task` restarts the clock
#: when that stage starts, so the clone, index and baseline are not counted. The eval runner
#: has its own, harder per-process timeout over the whole task, so this is the gateway's view
#: of "this is taking too long".
DEFAULT_MAX_WALL_SECONDS = 3600.0


class BudgetLimit(str, enum.Enum):
    USD = "usd"
    CALLS = "calls"
    WALL_TIME = "wall_time"


class BudgetExceeded(GatewayError):
    """A task hit its cap.

    `response` is the `LLMResponse` of the call that crossed the line, when
    there was one; it is None when the call was refused before being made.
    """

    def __init__(
        self,
        limit: BudgetLimit,
        *,
        spent_usd: Decimal,
        calls: int,
        elapsed_seconds: float,
        response: Any = None,
    ) -> None:
        super().__init__(
            f"task budget exceeded ({limit.value}): spent ${spent_usd} over {calls} call(s) "
            f"in {elapsed_seconds:.0f}s"
        )
        self.limit = limit
        self.spent_usd = spent_usd
        self.calls = calls
        self.elapsed_seconds = elapsed_seconds
        self.response = response


@dataclass
class TaskBudget:
    max_usd: Decimal = DEFAULT_MAX_USD
    max_calls: int = DEFAULT_MAX_CALLS
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS
    #: Injectable so a test can move time without sleeping.
    clock: Callable[[], float] = time.monotonic
    spent_usd: Decimal = field(default=Decimal(0), init=False)
    calls: int = field(default=0, init=False)
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        # Accept a float or int for convenience (`TaskBudget(max_usd=2.0)`), but
        # hold a Decimal: sums of float dollars drift, and "did this cross the
        # cap" is a comparison that should not depend on which side of a rounding
        # error it lands.
        self.max_usd = Decimal(str(self.max_usd))
        self.started_at = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        return self.clock() - self.started_at

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.max_usd - self.spent_usd, Decimal(0))

    def charge(self, cost_usd: Decimal | None) -> None:
        """Count one call. `None` is a call that cost nothing knowable.

        A failed call still counts toward `max_calls`: a provider outage that
        has the agent retrying forever is exactly the loop the cap should end.
        """
        self.calls += 1
        if cost_usd is not None:
            self.spent_usd += cost_usd

    def _reached(self) -> BudgetLimit | None:
        if self.spent_usd >= self.max_usd:
            return BudgetLimit.USD
        if self.calls >= self.max_calls:
            return BudgetLimit.CALLS
        if self.elapsed_seconds >= self.max_wall_seconds:
            return BudgetLimit.WALL_TIME
        return None

    def _exceeded(self) -> BudgetLimit | None:
        if self.spent_usd > self.max_usd:
            return BudgetLimit.USD
        if self.calls > self.max_calls:
            return BudgetLimit.CALLS
        if self.elapsed_seconds > self.max_wall_seconds:
            return BudgetLimit.WALL_TIME
        return None

    def _raise(self, limit: BudgetLimit, response: Any) -> BudgetExceeded:
        log.warning(
            "gateway.budget.exceeded",
            limit=limit.value,
            spent_usd=str(self.spent_usd),
            max_usd=str(self.max_usd),
            calls=self.calls,
            elapsed_seconds=round(self.elapsed_seconds, 1),
        )
        return BudgetExceeded(
            limit,
            spent_usd=self.spent_usd,
            calls=self.calls,
            elapsed_seconds=self.elapsed_seconds,
            response=response,
        )

    def raise_if_reached(self) -> None:
        """The pre-call check: refuse a call the budget has nothing left for."""
        limit = self._reached()
        if limit is not None:
            raise self._raise(limit, None)

    def raise_if_exceeded(self, response: Any = None) -> None:
        """The post-call check: refuse to let a call that crossed the cap pass silently."""
        limit = self._exceeded()
        if limit is not None:
            raise self._raise(limit, response)


@dataclass(frozen=True)
class TaskScope:
    """What a model call must be attributable to: a task, and that task's budget."""

    task_id: uuid.UUID
    budget: TaskBudget


_current_scope: ContextVar[TaskScope | None] = ContextVar("repolace_gateway_task_scope", default=None)


@contextmanager
def task_scope(task_id: uuid.UUID, budget: TaskBudget | None = None) -> Iterator[TaskScope]:
    """Bind a task id and budget for every `complete()` inside the block.

    A contextvar rather than a parameter, so the agent loop and anything it calls
    through the gateway cannot forget to pass it -- and so a call made without
    one fails loudly (`NoTaskScope`) instead of recording spend against nothing.

    Also binds `task_id` into structlog's context, so gateway log lines carry it
    even when this is used outside the pipeline, which binds the same key itself.
    """
    scope = TaskScope(task_id=task_id, budget=budget if budget is not None else TaskBudget())
    token = _current_scope.set(scope)
    try:
        with structlog.contextvars.bound_contextvars(task_id=str(task_id)):
            yield scope
    finally:
        _current_scope.reset(token)


def current_scope() -> TaskScope:
    scope = _current_scope.get()
    if scope is None:
        raise NoTaskScope("a model call was made outside task_scope(); there is no task to charge it to")
    return scope
