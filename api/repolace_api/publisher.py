"""Publishing a queued task to the worker.

A Celery *producer* only: `send_task` by name, so the API image never imports the pipeline (and so
never torch). Publishing happens after the task row is committed, and a failure to publish must not
fail the request: the row stays `queued` and the worker's sweeper republishes it. A connection the
broker dropped is replaced and the publish tried once more before it counts as failed.
"""

import asyncio
import threading
import uuid
from typing import Protocol

import kombu.pools
import structlog
from celery import Celery
from fastapi import Request
from kombu.exceptions import OperationalError

log = structlog.get_logger()

#: Matches `repolace_worker.celery_app.RUN_TASK_NAME`; a string here because the API must not import the worker.
RUN_TASK_NAME = "repolace.run_task"

#: What a dead or restarted broker looks like from a producer: the socket error itself, or kombu's
#: wrapper for it. One reconnect is tried for these and for nothing else.
CONNECTION_ERRORS = (OSError, OperationalError)


class TaskPublisher(Protocol):
    async def publish(self, task_id: uuid.UUID) -> bool:
        """Hand the task to the queue. Returns whether it was published; never raises."""
        ...


class CeleryTaskPublisher:
    def __init__(self, broker_url: str) -> None:
        self._broker_url = broker_url
        self._reset_lock = threading.Lock()
        self._app = self._build_app()

    def _build_app(self) -> Celery:
        app = Celery("repolace_api", broker=self._broker_url)
        # Fail fast: a request must not hang on a broker that is down.
        app.conf.broker_connection_timeout = 3
        app.conf.broker_connection_retry = False
        return app

    def _send(self, task_id: uuid.UUID) -> None:
        self._app.send_task(RUN_TASK_NAME, args=[str(task_id)], task_id=str(task_id), retry=False)

    def _drop_connections(self) -> None:
        """Forget every pooled broker connection, so the next send opens a new one.

        `retry=False` (fail fast) also switches off kombu's own reconnect, so a connection the broker
        closed (a restart) stays in the pool and every publish on it fails with a broken pipe. The
        pools are process-wide and keyed by connection parameters, so a fresh app alone would be handed
        the same dead connection: `kombu.pools.reset()` closes and clears them. This process has no
        other kombu user.
        """
        with self._reset_lock:
            kombu.pools.reset()
            self._app.close()
            self._app = self._build_app()

    def _send_reconnecting_once(self, task_id: uuid.UUID) -> None:
        try:
            self._send(task_id)
        except CONNECTION_ERRORS:
            log.warning("repos.task.publish_reconnecting", task_id=str(task_id))
            self._drop_connections()
            self._send(task_id)

    async def publish(self, task_id: uuid.UUID) -> bool:
        try:
            await asyncio.to_thread(self._send_reconnecting_once, task_id)
        except Exception:
            log.exception("repos.task.publish_failed", task_id=str(task_id))
            return False
        return True

    def close(self) -> None:
        self._app.close()


class NoopPublisher:
    """What an app with no broker configured (the tests) uses."""

    async def publish(self, task_id: uuid.UUID) -> bool:
        return False


def get_publisher(request: Request) -> TaskPublisher:
    return getattr(request.app.state, "publisher", None) or NoopPublisher()
