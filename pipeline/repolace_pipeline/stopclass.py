"""Why a task stopped, in the one sense that decides whether it may resume.

`unavailable`: something it depends on was not there (the provider refused calls past the retry window or
failed throughout, the process was killed or stopped, the sandbox host was down). Nothing about the agent was
learnt, and the work done so far is still good. `design`: the agent or the model ended it, or repolace has a
bug, so the run IS the measurement and is never continued.

Decided here, once, at the code that ends the row, from the *type* of what ended it -- never from a message
string -- and written to `tasks.stop_class`. See docs/CHECKPOINT_RESUME_DESIGN.md for the table.
"""

import asyncio

from repolace_gateway.errors import LLMCallError
from repolace_shared.tasks_recovery import DESIGN, UNAVAILABLE
from verify.errors import SandboxUnavailable

__all__ = ["DESIGN", "UNAVAILABLE", "classify_exception"]

#: Named, not imported: it lives in the worker package, which the pipeline must not depend on. It ends a task
#: that ran past its bound, which is the task's own doing (a slow agent), so it is not unavailability. It is a
#: `CancelledError`, which would otherwise read as an operator's stop.
_NOT_AN_OUTAGE = "WorkerTimeout"


def classify_exception(exc: BaseException) -> str:
    """`unavailable` or `design` for the exception that ended a task."""
    if any(cls.__name__ == _NOT_AN_OUTAGE for cls in type(exc).__mro__):
        return DESIGN
    if isinstance(exc, LLMCallError):
        return UNAVAILABLE if exc.unavailable else DESIGN
    if isinstance(exc, SandboxUnavailable):
        return UNAVAILABLE
    # An operator's stop (Ctrl-C, a deploy cancelling the task, a SIGTERM turned into one).
    if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt | SystemExit):
        return UNAVAILABLE
    return DESIGN
