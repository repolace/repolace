"""A wall-clock limit on one awaitable whose expiry is a `WorkerTimeout`.

Why not `asyncio.timeout`: `run_task` settles a cancelled task itself, before the exception
leaves it, and records *the name of the exception that reached it* (`interrupted: <name>`).
`asyncio.timeout` can only cancel with a plain `CancelledError`, so a task that hit the bound
read `interrupted: CancelledError` and the worker's own `worker: timeout` write, which runs
afterwards and is conditional on the row still running, never landed.

`bounded` puts a thin iterator between the event loop and the awaitable. When its own deadline
has cancelled the task, it throws a `WorkerTimeout` into the awaitable in place of the
`CancelledError` the loop delivered, so everything below sees (and records) the better name. A
cancellation from anywhere else passes through unchanged.

`WorkerTimeout` is a `CancelledError` subclass on purpose. The git, process and sandbox code
kills its child process groups in `except asyncio.CancelledError`, and those handlers must run
for a timeout exactly as they do for any other cancellation. Like `asyncio.timeout`, the expiry
is consumed with `Task.uncancel()`, so the task is no longer "being cancelled" afterwards and its
cleanup can await.
"""

import asyncio
from collections.abc import Awaitable, Generator
from typing import Any


class WorkerTimeout(asyncio.CancelledError):
    """The worker's bound on one task ran out."""


class Bound[T]:
    """Await it to run `awaitable` under a deadline of `seconds`; it fires at most once.

    Single use. `reschedule` moves the deadline (a test seam: the database test pulls it in once
    the agent is provably running, instead of racing a fixed short bound against pipeline speed).
    """

    def __init__(self, awaitable: Awaitable[T], seconds: float) -> None:
        self._awaitable = awaitable
        self._seconds = seconds
        self._task: asyncio.Task | None = None
        self._handle: asyncio.TimerHandle | None = None
        self._cancelling = 0
        self._fired = False

    def __await__(self) -> Generator[Any, Any, T]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("bounded() must be awaited from inside a task")
        self._task = task
        self._cancelling = task.cancelling()
        self._arm(self._seconds)
        return _Delegate(self, self._awaitable.__await__())  # type: ignore[return-value]

    def reschedule(self, seconds: float) -> None:
        """Move the deadline to `seconds` from now. Only meaningful while awaited."""
        self._seconds = seconds
        if self._task is not None and not self._fired:
            self._disarm()
            self._arm(seconds)

    def _arm(self, seconds: float) -> None:
        self._handle = asyncio.get_running_loop().call_later(seconds, self._fire)

    def _disarm(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _fire(self) -> None:
        self._fired = True
        self._handle = None
        assert self._task is not None
        self._task.cancel()

    def _translate(self, exc: BaseException) -> BaseException:
        """`WorkerTimeout` for the cancellation this deadline caused, else `exc` untouched."""
        if self._fired and type(exc) is asyncio.CancelledError and self._task is not None:
            self._fired = False  # one expiry, one translation
            # Another cancel requested on top of ours is still pending: do not hide it.
            if self._task.uncancel() <= self._cancelling:
                return WorkerTimeout(f"worker: task exceeded its {self._seconds:g} s bound")
        return exc


class _Delegate:
    """The iterator `await bound` drives: `yield from` semantics, with `throw` translated."""

    def __init__(self, bound: Bound, inner: Any) -> None:
        self._bound = bound
        self._inner = inner

    def __iter__(self) -> "_Delegate":
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def send(self, value: Any) -> Any:
        try:
            return self._inner.send(value)
        except BaseException:  # the result (StopIteration) or any failure ends the bound
            self._bound._disarm()
            raise

    def throw(self, exc: BaseException) -> Any:
        try:
            return self._inner.throw(self._bound._translate(exc))
        except BaseException:
            self._bound._disarm()
            raise

    def close(self) -> None:
        self._bound._disarm()
        self._inner.close()


def bounded[T](awaitable: Awaitable[T], seconds: float) -> Bound[T]:
    """Run `awaitable` for at most `seconds`; on expiry it receives a `WorkerTimeout`."""
    return Bound(awaitable, seconds)
