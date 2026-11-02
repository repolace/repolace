"""The database access the execution-side harness needs, and nothing else.

`enqueue`, `runner` and `gold` all read or write `tasks`, and the rules about
which rows they may change live here so they are stated once. The one that
matters most is the shape of every status write the runner makes: **conditional
on `status = 'running'`**. A task row is never run from scratch twice (`task_test_runs` has
`UNIQUE(task_id, attempt)`), so the legitimate transitions a harness performs on a row it did not finish are
RUNNING -> FAILED, and RUNNING -> PAUSED for a row that stopped for unavailability and left a checkpoint (the
pipeline continues it from there; see `tasks_recovery.settle_running`); there is no function here that sets
QUEUED, and adding one would reintroduce the double run the whole runner is built to prevent.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal

from repolace_gateway.errors import PROVIDER_FAILURE_MARKERS
from repolace_shared.db.models import STOP_UNAVAILABLE, LLMCall, Task, TaskCheckpoint, TaskStatus
from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.tasks_recovery import RESUME_REFUSED_PREFIX, TaskRef
from repolace_shared.tasks_recovery import running_older_than as _running_older_than
from sqlalchemy import exists, func, or_, select
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
    """The QUEUED and PAUSED tasks of a run, ordered `(run_index, instance_id)`.

    A PAUSED row stopped for unavailability and has a checkpoint; dispatching it resumes it.

    Index 0 of every instance goes first so each repository's index is warmed
    before its second and third runs reach it. A nicety, not a correctness rule:
    the pipeline itself waits on `RepoIndexInProgress`. Rows that are neither
    are not returned, which is what makes the runner resumable.
    """
    async with factory() as session:
        rows = await session.execute(
            select(Task.id, Task.instance_id, Task.run_index)
            .where(Task.eval_run_id == eval_run_id, Task.status.in_((TaskStatus.QUEUED, TaskStatus.PAUSED)))
            .order_by(Task.run_index, Task.instance_id, Task.id)
        )
        return [QueuedTask(task_id, instance_id, run_index) for task_id, instance_id, run_index in rows]


async def refused_resumes(factory: SessionFactory, eval_run_id: str) -> list[QueuedTask]:
    """FAILED rows of the run whose resume was refused (`RESUME_REFUSED_PREFIX`) and whose checkpoint is kept."""
    async with factory() as session:
        rows = await session.execute(
            select(Task.id, Task.instance_id, Task.run_index)
            .where(
                Task.eval_run_id == eval_run_id,
                Task.status == TaskStatus.FAILED,
                Task.error_message.startswith(RESUME_REFUSED_PREFIX),
                exists().where(TaskCheckpoint.task_id == Task.id),
            )
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
    """Tasks of the run that stopped because the provider refused us or was not there.

    Either FAILED with one of the gateway's fixed message prefixes in `error_message` (a rejected key,
    spent quota, a provider that failed throughout), or PAUSED for unavailability. Such a task is a harness
    error, not an agent failure; two of them mean the rest of the run would stop the same way.

    `task_ids` limits the count to the tasks one invocation of the runner dispatched. Without it the
    rows a *previous* invocation left behind would stop a re-run at its first dispatch, after the
    user had fixed the key, and a task row is never re-queued, so they would never go away.
    """
    if task_ids is not None and not task_ids:
        return 0
    conditions = [
        Task.eval_run_id == eval_run_id,
        or_(
            (Task.status == TaskStatus.FAILED)
            & or_(*(Task.error_message.contains(marker) for marker in PROVIDER_FAILURE_MARKERS)),
            # Paused *because the provider was unavailable*, not because a child died: the reason carries the
            # gateway's prefix in the one case. A crashed or OOM-killed child pauses with `runner: child exited
            # ...`, which says nothing about the provider and must not stop the sweep as if it did.
            (Task.status == TaskStatus.PAUSED)
            & (Task.stop_class == STOP_UNAVAILABLE)
            & or_(*(Task.paused_reason.contains(marker) for marker in PROVIDER_FAILURE_MARKERS)),
        ),
    ]
    if task_ids is not None:
        conditions.append(Task.id.in_(list(task_ids)))
    async with factory() as session:
        total = await session.scalar(select(func.count()).select_from(Task).where(*conditions))
        return int(total or 0)


async def running_older_than(factory: SessionFactory, eval_run_id: str, age: timedelta) -> list[QueuedTask]:
    """RUNNING tasks of the run that started more than `age` ago (see `tasks_recovery`)."""
    return await _running_older_than(factory, age, eval_run_id=eval_run_id)
