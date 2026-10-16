"""Settling a task whose runner is gone, shared by the eval runner and the worker's reaper.

Every status write here is **conditional on `status = 'running'`**. A task row can never be
re-run (`task_test_runs` is unique on `(task_id, attempt)`), so the only transition anyone
but the pipeline itself may perform on a row is RUNNING -> FAILED, and there is deliberately
no function here that sets QUEUED.
"""

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_shared.db.models import Task, TaskStatus
from repolace_shared.process import ProcessResult, run_process

log = structlog.get_logger()

SessionFactory = async_sessionmaker[AsyncSession]

ProcessRunner = Callable[..., Awaitable[ProcessResult]]

_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")


@dataclass(frozen=True)
class TaskRef:
    task_id: uuid.UUID
    instance_id: str | None
    run_index: int | None


async def fail_if_running(factory: SessionFactory, task_id: uuid.UUID, message: str) -> bool:
    """RUNNING -> FAILED with `message`, only if the row is still RUNNING.

    Conditional so a task that finished in the same instant keeps its real
    status. Returns whether this call changed the row.
    """
    async with factory() as session:
        result = await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.RUNNING)
            .values(status=TaskStatus.FAILED, error_message=message, completed_at=func.now())
        )
        await session.commit()
        return result.rowcount == 1


async def running_older_than(
    factory: SessionFactory, age: timedelta, *, eval_run_id: str | None = None
) -> list[TaskRef]:
    """RUNNING tasks that started more than `age` ago, by the database's clock.

    `eval_run_id` restricts the search to one benchmark run; None looks at every row. A RUNNING
    row with no `started_at` should not exist; it is judged by `created_at` rather than
    skipped, so it cannot hide.
    """
    query = select(Task.id, Task.instance_id, Task.run_index).where(
        Task.status == TaskStatus.RUNNING,
        func.coalesce(Task.started_at, Task.created_at) < func.now() - age,
    )
    if eval_run_id is not None:
        query = query.where(Task.eval_run_id == eval_run_id)
    async with factory() as session:
        rows = await session.execute(query.order_by(Task.run_index, Task.instance_id, Task.id))
        return [TaskRef(task_id, instance_id, run_index) for task_id, instance_id, run_index in rows]


async def running_with_stale_heartbeat(
    factory: SessionFactory, older_than: timedelta, *, eval_run_id: str | None = None
) -> list[TaskRef]:
    """RUNNING tasks whose heartbeat is older than `older_than`, by the database's clock.

    A row that has never beaten (`heartbeat_at` NULL) is judged by `started_at` instead, so a
    runner that died before its first beat is still found, and a row that has only just been
    claimed is not.
    """
    cutoff = func.now() - older_than
    query = select(Task.id, Task.instance_id, Task.run_index).where(
        Task.status == TaskStatus.RUNNING,
        or_(
            Task.heartbeat_at < cutoff,
            (Task.heartbeat_at.is_(None)) & (func.coalesce(Task.started_at, Task.created_at) < cutoff),
        ),
    )
    if eval_run_id is not None:
        query = query.where(Task.eval_run_id == eval_run_id)
    async with factory() as session:
        rows = await session.execute(query.order_by(Task.created_at, Task.id))
        return [TaskRef(task_id, instance_id, run_index) for task_id, instance_id, run_index in rows]


async def fail_if_heartbeat_stale(
    factory: SessionFactory, task_id: uuid.UUID, message: str, older_than: timedelta
) -> bool:
    """RUNNING -> FAILED only if the row is *still* running **and still stale** at write time.

    The staleness test is part of the UPDATE's own WHERE clause, so a heartbeat that lands between
    a reaper's SELECT and this write leaves the row running. Returns whether this call failed it
    (decided by RETURNING, not by a count that could include another writer's change).
    """
    cutoff = func.now() - older_than
    async with factory() as session:
        result = await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                or_(
                    Task.heartbeat_at < cutoff,
                    Task.heartbeat_at.is_(None) & (func.coalesce(Task.started_at, Task.created_at) < cutoff),
                ),
            )
            .values(status=TaskStatus.FAILED, error_message=message, completed_at=func.now())
            .returning(Task.id)
        )
        failed = result.scalar_one_or_none() is not None
        await session.commit()
        return failed


async def remove_task_containers(
    task_id: uuid.UUID,
    *,
    process_runner: ProcessRunner = run_process,
    docker: str = "docker",
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """`docker ps -q --filter name=repolace-<12 hex>` then `docker rm -f` for any survivors.

    Needed because the sandbox starts containers through a `docker run` client
    that the pipeline launches in its own session: killing the child's process
    group does not reach it, and the container belongs to the daemon anyway.
    Best effort -- a missing docker binary or a daemon that does not answer is
    logged, not raised, since the row has already been settled.
    """
    prefix = f"repolace-{task_id.hex[:12]}"
    try:
        listed = await process_runner(docker, "ps", "-q", "--filter", f"name=^{prefix}-", timeout=timeout, env=env)
        if listed.timed_out or listed.returncode != 0:
            log.error("runner.docker.ps_failed", prefix=prefix, returncode=listed.returncode)
            return ()
        ids = tuple(listed.stdout.decode("utf-8", errors="replace").split())
        # `docker ps -q` prints hex ids; anything else is not passed to `rm`.
        ids = tuple(item for item in ids if _CONTAINER_ID.fullmatch(item))
        if ids:
            await process_runner(docker, "rm", "-f", *ids, timeout=timeout, env=env)
    except OSError as exc:
        log.error("runner.docker.unavailable", prefix=prefix, error=str(exc))
        return ()
    return ids
