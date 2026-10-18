"""`bounded`: a wall-clock limit whose expiry is a `WorkerTimeout`, not a bare `CancelledError`.

`run_task` records an interruption by the *name* of the exception that reached it, and
`asyncio.timeout` can only ever cancel with a plain `CancelledError`, so the row of a task that
hit the bound used to read `interrupted: CancelledError`. `WorkerTimeout` subclasses
`CancelledError` on purpose: every kill-on-cancel handler in git, process and sandbox code catches
that class, and they must still run.
"""

import asyncio

import pytest
from repolace_worker.bound import WorkerTimeout, bounded

pytestmark = pytest.mark.anyio

SHORT = 0.05


async def value_after(delay: float, value: str = "done") -> str:
    await asyncio.sleep(delay)
    return value


class TestWithinTheBound:
    async def test_the_result_comes_back_and_nothing_is_left_cancelling(self):
        assert await bounded(value_after(0.01), 5) == "done"

        assert asyncio.current_task().cancelling() == 0

    async def test_the_coroutines_own_exception_propagates_and_the_timer_is_gone(self):
        async def boom():
            raise RuntimeError("agent exploded")

        with pytest.raises(RuntimeError, match="agent exploded"):
            await bounded(boom(), SHORT)

        # A timer left armed would cancel *this* task when it fires.
        await asyncio.sleep(SHORT * 3)
        assert asyncio.current_task().cancelling() == 0


class TestWhenTheBoundExpires:
    async def test_the_coroutine_sees_a_worker_timeout_where_it_was_waiting(self):
        seen: list[BaseException] = []

        async def wait_forever():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError as exc:
                seen.append(exc)
                raise

        with pytest.raises(WorkerTimeout, match="exceeded its 0.05 s bound"):
            await bounded(wait_forever(), SHORT)

        # What every `except asyncio.CancelledError` cleanup in git, process and the sandbox catches.
        assert len(seen) == 1 and type(seen[0]) is WorkerTimeout
        assert isinstance(seen[0], asyncio.CancelledError)
        assert asyncio.current_task().cancelling() == 0

    async def test_the_cancellation_is_consumed_so_cleanup_can_still_await(self):
        log: list[str] = []

        async def tidy_up():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.01)  # a rollback, a process reap: must not be cancelled again
                log.append("cleaned up")
                raise

        with pytest.raises(WorkerTimeout):
            await bounded(tidy_up(), SHORT)

        assert log == ["cleaned up"]

    async def test_a_child_task_is_cancelled_and_the_parent_sees_the_timeout(self):
        child = asyncio.ensure_future(asyncio.sleep(30))

        async def wait_for_child():
            await child

        with pytest.raises(WorkerTimeout):
            await bounded(wait_for_child(), SHORT)

        assert child.cancelled()

    async def test_a_coroutine_that_swallows_the_timeout_returns_normally(self):
        async def stubborn():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return "gave up quietly"

        assert await bounded(stubborn(), SHORT) == "gave up quietly"
        assert asyncio.current_task().cancelling() == 0

    async def test_it_can_be_rescheduled_to_expire_sooner(self):
        """The seam the database test uses to fire the bound while the agent is provably running."""
        bound = bounded(value_after(30), 60)

        async def pull_in():
            await asyncio.sleep(0.01)
            bound.reschedule(0)

        puller = asyncio.ensure_future(pull_in())
        with pytest.raises(WorkerTimeout):
            await bound
        await puller


class TestACancellationFromOutside:
    async def test_it_passes_through_as_a_plain_cancelled_error(self):
        task = asyncio.ensure_future(bounded(value_after(30), 60))
        await asyncio.sleep(0.01)

        task.cancel()

        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert not isinstance(raised.value, WorkerTimeout)
