"""The database access the execution-side harness needs, and nothing else.

`enqueue`, `runner` and `gold` all read or write `tasks`, and the rules about
which rows they may change live here so they are stated once. The one that
matters most is the shape of every status write the runner makes: **conditional
on `status = 'running'`**. A task row can never be re-run (`task_test_runs` has
`UNIQUE(task_id, attempt)`), so the only legitimate transitions a harness
performs on a row it did not finish are RUNNING -> FAILED; there is no function
here that sets QUEUED, and adding one would reintroduce the double run the whole
runner is built to prevent.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_shared.db.models import LLMCall, Task, TaskStatus
from repolace_shared.db.session import create_engine, create_session_factory

SessionFactory = async_sessionmaker[AsyncSession]

#: What `tasks.eval_run_id` may hold. It is also a directory name (`eval/runs/<run>/`)
#: and a command-line argument, so it must start with a letter or digit (never `-`,
#: never `.`/`..`) and use only characters that are safe in all three places.
_EVAL_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def check_eval_run_id(value: object) -> str:
    # `..` is refused too, as `harness.run_manifest.check_run_id` refuses it: the two must
    # accept the same ids, or a run id could pass here and fail when its manifest is written.
    if not isinstance(value, str) or _EVAL_RUN_ID.fullmatch(value) is None or ".." in value:
        raise ValueError(
            f"eval run id {value!r} must be 1-64 characters, start with a letter or digit, "
            f"and use only letters, digits, '_', '.', '-'"
        )
    return value


@asynccontextmanager
async def open_session_factory() -> AsyncIterator[SessionFactory]:
    """A session factory over the configured database; the engine is disposed on exit."""
    from repolace_shared.config import SharedSettings

    engine = create_engine(SharedSettings().database_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class QueuedTask:
    task_id: uuid.UUID
    instance_id: str
    run_index: int


async def queued_tasks(factory: SessionFactory, eval_run_id: str) -> list[QueuedTask]:
    """The QUEUED tasks of a run, ordered `(run_index, instance_id)`.

    Index 0 of every instance goes first so each repository's index is warmed
    before its second and third runs reach it. A nicety, not a correctness rule:
    the pipeline itself waits on `RepoIndexInProgress`. Rows that are not QUEUED
    are not returned, which is what makes the runner resumable.
    """
    async with factory() as session:
        rows = await session.execute(
            select(Task.id, Task.instance_id, Task.run_index)
            .where(Task.eval_run_id == eval_run_id, Task.status == TaskStatus.QUEUED)
            .order_by(Task.run_index, Task.instance_id, Task.id)
        )
        return [QueuedTask(task_id, instance_id, run_index) for task_id, instance_id, run_index in rows]


async def run_cost_usd(factory: SessionFactory, eval_run_id: str) -> Decimal:
    """Everything the run has spent so far, from `llm_calls` (the only complete record).

    Includes tasks from earlier invocations of the same run. `tasks.cost_usd` is
    not used: it is written at finalize, so it misses every task still in flight.
    """
    async with factory() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0))
            .join(Task, Task.id == LLMCall.task_id)
            .where(Task.eval_run_id == eval_run_id)
        )
        return Decimal(total)


async def fail_if_running(factory: SessionFactory, task_id: uuid.UUID, message: str) -> bool:
    """RUNNING -> FAILED with `message`, only if the row is still RUNNING.

    Conditional so a task that finished in the same instant keeps its real
    status. Returns whether this call changed the row. Deliberately the only
    write the runner makes to a task's status.
    """
    async with factory() as session:
        result = await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.RUNNING)
            .values(status=TaskStatus.FAILED, error_message=message, completed_at=func.now())
        )
        await session.commit()
        return result.rowcount == 1


async def running_older_than(factory: SessionFactory, eval_run_id: str, age: timedelta) -> list[QueuedTask]:
    """RUNNING tasks of the run that started more than `age` ago, by the database's clock.

    `started_at` is written by the database (`now()` in the claim), so the cutoff
    uses the database's clock too. A RUNNING row with no `started_at` should not
    exist; it is judged by `created_at` rather than skipped, so it cannot hide.
    """
    async with factory() as session:
        rows = await session.execute(
            select(Task.id, Task.instance_id, Task.run_index)
            .where(
                Task.eval_run_id == eval_run_id,
                Task.status == TaskStatus.RUNNING,
                func.coalesce(Task.started_at, Task.created_at) < func.now() - age,
            )
            .order_by(Task.run_index, Task.instance_id, Task.id)
        )
        return [QueuedTask(task_id, instance_id, run_index) for task_id, instance_id, run_index in rows]
