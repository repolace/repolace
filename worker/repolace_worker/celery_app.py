"""The Celery application: broker, limits, and the two beat jobs.

Importing this module must stay light (the API image never imports it, and `celery inspect` does),
so nothing here pulls the pipeline, torch or LiteLLM; the task modules do that inside functions.
"""

from celery import Celery
from repolace_shared.config import SharedSettings

from repolace_worker.config import get_worker_settings

_settings = SharedSettings()
_worker = get_worker_settings()

RUN_TASK_NAME = "repolace.run_task"
SWEEP_TASK_NAME = "repolace.sweep_queued"
REAP_TASK_NAME = "repolace.reap_abandoned"

celery_app = Celery(
    "repolace_worker",
    broker=_settings.celery_broker_url,
    include=["repolace_worker.tasks", "repolace_worker.maintenance"],
)

celery_app.conf.update(
    # One task per child at a time: a task runs for minutes to hours, so prefetching would park
    # messages behind a long one while other children sit idle.
    worker_prefetch_multiplier=1,
    # Nothing reads a Celery result: the outcome is on the task row.
    task_ignore_result=True,
    worker_concurrency=_worker.concurrency,
    # The backstop. The primary limit is `asyncio.timeout(task_bound_seconds)` inside the task,
    # because a Celery soft limit raises from a signal handler into arbitrary frames.
    task_time_limit=_worker.hard_time_limit_seconds,
    broker_connection_retry_on_startup=True,
    # RabbitMQ's default `consumer_timeout` (30 min) would kill an acks_late task; it must be raised
    # above `task_time_limit` in the broker's config (infra, item 6).
    task_routes={
        SWEEP_TASK_NAME: {"queue": _worker.maintenance_queue},
        REAP_TASK_NAME: {"queue": _worker.maintenance_queue},
    },
    beat_schedule={
        "repolace-sweep-queued": {
            "task": SWEEP_TASK_NAME,
            "schedule": _worker.sweeper_interval_seconds,
            "options": {"queue": _worker.maintenance_queue},
        },
        "repolace-reap-abandoned": {
            "task": REAP_TASK_NAME,
            "schedule": _worker.reaper_interval_seconds,
            "options": {"queue": _worker.maintenance_queue},
        },
    },
)

#: What the rest of the code means by "the app".
app = celery_app
