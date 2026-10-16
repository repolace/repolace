"""Publishing a queued task to the worker.

A Celery *producer* only: `send_task` by name, so the API image never imports the pipeline (and so
never torch). Publishing happens after the task row is committed, and a failure to publish must not
fail the request: the row stays `queued` and the worker's sweeper republishes it.
"""

import asyncio
import uuid
from typing import Protocol

import structlog
from celery import Celery
from fastapi import Request

log = structlog.get_logger()

#: Matches `repolace_worker.celery_app.RUN_TASK_NAME`; a string here because the API must not import the worker.
RUN_TASK_NAME = "repolace.run_task"


class TaskPublisher(Protocol):
    async def publish(self, task_id: uuid.UUID) -> bool:
        """Hand the task to the queue. Returns whether it was published; never raises."""
        ...


class CeleryTaskPublisher:
    def __init__(self, broker_url: str) -> None:
        self._app = Celery("repolace_api", broker=broker_url)
        # Fail fast: a request must not hang on a broker that is down.
        self._app.conf.broker_connection_timeout = 3
        self._app.conf.broker_connection_retry = False

    async def publish(self, task_id: uuid.UUID) -> bool:
        try:
            await asyncio.to_thread(
                self._app.send_task, RUN_TASK_NAME, args=[str(task_id)], task_id=str(task_id), retry=False
            )
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
