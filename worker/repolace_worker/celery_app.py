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
    worker_max_memory_per_child=_worker.max_memory_per_child_kib,
    worker_max_tasks_per_child=_worker.max_tasks_per_child,
    # The backstop. The primary limit is `asyncio.timeout(task_bound_seconds)` inside the task,
    # because a Celery soft limit raises from a signal handler into arbitrary frames.
    task_time_limit=_worker.hard_time_limit_seconds,
    broker_connection_retry_on_startup=True,
    # A prefork child is killed if it has not reported "up" within this many seconds (default 4),
    # and `worker_process_init` (resources, provider preflight, the embedding model and torch)
    # runs before it does. Without this the children would be killed and respawned in a loop.
    worker_proc_alive_timeout=300.0,
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
            "options": {"queue": _worker.maintenance_queue, "expires": _worker.beat_expires_seconds},
        },
        "repolace-reap-abandoned": {
            "task": REAP_TASK_NAME,
            "schedule": _worker.reaper_interval_seconds,
            "options": {"queue": _worker.maintenance_queue, "expires": _worker.beat_expires_seconds},
        },
    },
)

#: What the rest of the code means by "the app".
app = celery_app
