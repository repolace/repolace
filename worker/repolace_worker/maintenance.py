"""The two beat jobs: republish lost `queued` rows, fail abandoned `running` ones.

They run on their own queue (`WorkerSettings.maintenance_queue`) in a worker of their own, so a
prefork pool whose every child is busy on a long task cannot starve them. Each opens a short-lived
engine in its own event loop; neither needs the pipeline.
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import structlog
from repolace_shared.config import SharedSettings
from repolace_shared.db.models import Task, TaskStatus
from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.tasks_recovery import (
    fail_if_running,
    remove_task_containers,
    running_with_stale_heartbeat,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_worker.celery_app import REAP_TASK_NAME, RUN_TASK_NAME, SWEEP_TASK_NAME, celery_app
from repolace_worker.config import get_worker_settings

log = structlog.get_logger()

SessionFactory = async_sessionmaker[AsyncSession]
Publish = Callable[[uuid.UUID], Any]

ABANDONED_MESSAGE = "runner: abandoned"


def publish_by_name(task_id: uuid.UUID) -> None:
    """`send_task` by name with the row id as the Celery id, as the API does."""
    celery_app.send_task(RUN_TASK_NAME, args=[str(task_id)], task_id=str(task_id))


async def sweep_queued(
    factory: SessionFactory, min_age: timedelta, publish: Publish = publish_by_name
) -> list[uuid.UUID]:
    """Republish `queued` non-benchmark rows older than `min_age`.

    A duplicate message is harmless: same task id, and the claim is conditional. Benchmark rows
    (`eval_run_id` set) belong to the eval runner and are never published.
    """
    async with factory() as session:
        rows = await session.scalars(
            select(Task.id)
            .where(
                Task.status == TaskStatus.QUEUED,
                Task.eval_run_id.is_(None),
                Task.created_at < func.now() - min_age,
            )
            .order_by(Task.created_at, Task.id)
        )
        ids = list(rows)
    published: list[uuid.UUID] = []
    for task_id in ids:
        try:
            publish(task_id)
        except Exception:
            log.exception("worker.sweeper.publish_failed", task_id=str(task_id))
            continue
        published.append(task_id)
    if published:
        log.info("worker.sweeper.republished", count=len(published))
    return published


async def reap_abandoned(
    factory: SessionFactory,
    stale_after: timedelta,
    remove_containers: Callable[[uuid.UUID], Any] = remove_task_containers,
) -> list[uuid.UUID]:
    """Fail `running` rows with a stale heartbeat (`runner: abandoned`) and remove their containers.

    The update is conditional on the row still being `running`, so a task that finished while the
    reaper looked keeps its real status; containers are removed only for rows this call failed.
    """
    reaped: list[uuid.UUID] = []
    for stale in await running_with_stale_heartbeat(factory, stale_after):
        if await fail_if_running(factory, stale.task_id, ABANDONED_MESSAGE):
            reaped.append(stale.task_id)
            log.error("worker.reaper.abandoned", task_id=str(stale.task_id))
            try:
                await remove_containers(stale.task_id)
            except Exception:
                log.exception("worker.reaper.container_cleanup_failed", task_id=str(stale.task_id))
    return reaped


async def _with_factory(job: Callable[[SessionFactory], Any]) -> Any:
    engine = create_engine(SharedSettings().database_url)
    try:
        return await job(create_session_factory(engine))
    finally:
        await engine.dispose()


@celery_app.task(name=SWEEP_TASK_NAME)
def sweep_queued_job() -> int:
    settings = get_worker_settings()
    age = timedelta(seconds=settings.sweeper_min_age_seconds)
    return len(asyncio.run(_with_factory(lambda factory: sweep_queued(factory, age))))


@celery_app.task(name=REAP_TASK_NAME)
def reap_abandoned_job() -> int:
    settings = get_worker_settings()
    stale = timedelta(seconds=settings.reaper_stale_seconds)
    return len(asyncio.run(_with_factory(lambda factory: reap_abandoned(factory, stale))))
