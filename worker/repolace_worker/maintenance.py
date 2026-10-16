"""The two beat jobs: republish lost `queued` rows, fail abandoned `running` ones.

They run on their own queue (`WorkerSettings.maintenance_queue`) in a worker of their own, so a
prefork pool whose every child is busy on a long task cannot starve them. Each opens a short-lived
engine in its own event loop; neither needs the pipeline.
"""

import asyncio
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import Any, Protocol

import structlog
from repolace_shared.config import SharedSettings
from repolace_shared.db.models import Task, TaskStatus
from repolace_shared.db.session import create_engine, create_session_factory
from repolace_shared.tasks_recovery import (
    fail_if_heartbeat_stale,
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


class SweepClaims(Protocol):
    """The per-task "already republished" marker, so a backlog is republished once per window."""

    def claim(self, task_id: uuid.UUID) -> bool: ...

    def release(self, task_id: uuid.UUID) -> None: ...


class RedisSweepClaims:
    """`SET repolace:swept:<task id> 1 NX EX <seconds>`: one republish per task per window."""

    def __init__(self, redis_url: str, ttl_seconds: int) -> None:
        import redis

        self._redis = redis.Redis.from_url(redis_url, socket_connect_timeout=3, socket_timeout=3)
        self._ttl = ttl_seconds

    def claim(self, task_id: uuid.UUID) -> bool:
        return bool(self._redis.set(f"repolace:swept:{task_id}", 1, nx=True, ex=self._ttl))

    def release(self, task_id: uuid.UUID) -> None:
        self._redis.delete(f"repolace:swept:{task_id}")

    def close(self) -> None:
        self._redis.close()


def publish_by_name(task_id: uuid.UUID) -> None:
    """`send_task` by name with the row id as the Celery id, as the API does. No broker retry."""
    celery_app.send_task(RUN_TASK_NAME, args=[str(task_id)], task_id=str(task_id), retry=False)


async def sweep_queued(
    factory: SessionFactory,
    min_age: timedelta,
    publish: Publish = publish_by_name,
    *,
    claims: SweepClaims,
    max_per_tick: int = 50,
) -> list[uuid.UUID]:
    """Republish `queued` non-benchmark rows older than `min_age`, at most once per dedupe window.

    A row is republished only if its Redis key could be set, so the next tick (and the next after
    that) leaves it alone until the key expires; at most `max_per_tick` are sent per call. The first
    connection error (broker or Redis) **aborts the tick** and is logged once: retrying each row
    against a dead broker is what turns an outage into a storm. A duplicate message that does get
    through is harmless: same task id, and the claim is conditional. Benchmark rows (`eval_run_id`
    set) belong to the eval runner and are never published.
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
        if len(published) >= max_per_tick:
            break
        try:
            if not claims.claim(task_id):
                continue
        except Exception:
            log.exception("worker.sweeper.dedupe_unavailable", aborted=True)
            break
        try:
            publish(task_id)
        except Exception:
            # Free the key so the next tick may try again, then stop: the broker is not answering.
            with suppress(Exception):
                claims.release(task_id)
            log.exception("worker.sweeper.publish_failed", task_id=str(task_id), aborted=True)
            break
        published.append(task_id)
    if published:
        log.info("worker.sweeper.republished", count=len(published))
    return published


async def reap_abandoned(
    factory: SessionFactory,
    stale_after: timedelta,
    remove_containers: Callable[[uuid.UUID], Any] = remove_task_containers,
    *,
    long_stale_factor: float = 3.0,
) -> list[uuid.UUID]:
    """Fail `running` rows with a stale heartbeat (`runner: abandoned`) and remove their containers.

    The staleness test is re-evaluated **inside the UPDATE** (`fail_if_heartbeat_stale`), so a
    heartbeat that lands between the SELECT and the write leaves the row running, and containers are
    removed only for rows that UPDATE actually failed. Reaches only containers on this process's own
    docker host.
    """
    long_stale = await running_with_stale_heartbeat(factory, stale_after * long_stale_factor)
    if long_stale:
        log.warning(
            "worker.reaper.long_stale",
            count=len(long_stale),
            hint="rows stale for several thresholds: the maintenance worker was probably down",
        )
    reaped: list[uuid.UUID] = []
    for stale in await running_with_stale_heartbeat(factory, stale_after):
        if await fail_if_heartbeat_stale(factory, stale.task_id, ABANDONED_MESSAGE, stale_after):
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
    claims = RedisSweepClaims(SharedSettings().redis_url, settings.sweeper_dedupe_seconds)
    try:
        return len(
            asyncio.run(
                _with_factory(
                    lambda factory: sweep_queued(
                        factory, age, claims=claims, max_per_tick=settings.sweeper_max_per_tick
                    )
                )
            )
        )
    finally:
        claims.close()


@celery_app.task(name=REAP_TASK_NAME)
def reap_abandoned_job() -> int:
    settings = get_worker_settings()
    stale = timedelta(seconds=settings.reaper_stale_seconds)
    factor = settings.reaper_long_stale_factor
    return len(asyncio.run(_with_factory(lambda factory: reap_abandoned(factory, stale, long_stale_factor=factor))))
