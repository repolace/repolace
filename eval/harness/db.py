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
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal

from repolace_gateway.errors import PROVIDER_FAILURE_MARKERS
from repolace_shared.db.models import LLMCall, Task, TaskStatus
from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.tasks_recovery import TaskRef
from repolace_shared.tasks_recovery import running_older_than as _running_older_than
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


#: The same record the shared recovery helpers return, so a stale row is one type everywhere.
QueuedTask = TaskRef


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


async def run_provider_failures(
    factory: SessionFactory, eval_run_id: str, task_ids: Collection[uuid.UUID] | None = None
) -> int:
    """Tasks of the run that FAILED because the provider refused us (a rejected key, spent quota).

    Recognised by the gateway's fixed message prefixes in `error_message`. Such a task is a harness
    error, not an agent failure; two of them mean the rest of the run would fail the same way.

    `task_ids` limits the count to the tasks one invocation of the runner dispatched. Without it the
    rows a *previous* invocation left behind would stop a re-run at its first dispatch, after the
    user had fixed the key, and a task row is never re-queued, so they would never go away.
    """
    if task_ids is not None and not task_ids:
        return 0
    conditions = [
        Task.eval_run_id == eval_run_id,
        Task.status == TaskStatus.FAILED,
        or_(*(Task.error_message.contains(marker) for marker in PROVIDER_FAILURE_MARKERS)),
    ]
    if task_ids is not None:
        conditions.append(Task.id.in_(list(task_ids)))
    async with factory() as session:
        total = await session.scalar(select(func.count()).select_from(Task).where(*conditions))
        return int(total or 0)


async def running_older_than(factory: SessionFactory, eval_run_id: str, age: timedelta) -> list[QueuedTask]:
    """RUNNING tasks of the run that started more than `age` ago (see `tasks_recovery`)."""
    return await _running_older_than(factory, age, eval_run_id=eval_run_id)
