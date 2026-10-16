"""Settings for the Celery worker and its two beat jobs."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

#: `harness.enqueue.task_wall_clock_bound()`: index-lock wait 1200 + environment build 2400 + baseline
#: suite 1800 + agent wall budget 3600 + one overrunning scored suite 1800 + 900 unbounded-stage slack.
#: Defined there, next to the sum's terms; the worker may not import eval, so it is a literal here and
#: `worker/tests/test_worker_settings.py` holds it equal when eval is importable.
DEFAULT_TASK_BOUND_SECONDS = 11_700.0


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore", env_prefix="WORKER_", populate_by_name=True)

    #: Which half of the deployment this process is: `task` runs pipelines and builds the per-child
    #: resources (model client, embedder); `maintenance` runs only the beat jobs and needs just the
    #: database and docker.
    role: Literal["task", "maintenance"] = "task"
    #: Prefork children, and so concurrent tasks. A RAM decision: about 10 GB for 3.
    concurrency: int = 3
    #: The primary per-task limit, enforced with `asyncio.timeout` inside the task.
    task_bound_seconds: float = DEFAULT_TASK_BOUND_SECONDS
    #: Added to the bound for Celery's hard `task_time_limit` (the SIGKILL backstop), the same 600 s
    #: the eval runner allows an abandoned row.
    hard_limit_grace_seconds: float = 600.0
    #: Recycle a child whose resident memory passes this (KiB; about 4 GB) or after this many tasks,
    #: so a leak in torch or the model client cannot grow without bound across multi-hour tasks.
    max_memory_per_child_kib: int = 4_000_000
    max_tasks_per_child: int = 50

    #: Sweeper: a `queued` non-benchmark row older than this is republished.
    sweeper_min_age_seconds: float = 300.0
    sweeper_interval_seconds: float = 60.0
    #: A republished row is not republished again for this long (a Redis key per task).
    sweeper_dedupe_seconds: int = 900
    #: At most this many rows are republished per tick, so a backlog cannot become a publish storm.
    sweeper_max_per_tick: int = 50
    #: Reaper: a `running` row whose heartbeat is older than this (the pipeline beats every 30 s; five minutes tolerates a slow database) is failed.
    reaper_stale_seconds: float = 300.0
    reaper_interval_seconds: float = 60.0
    #: The reaper warns when a row has been stale for this many times the threshold: it means the
    #: maintenance worker was down, not that one task died.
    reaper_long_stale_factor: float = 3.0
    #: Beat messages older than this are dropped by the broker, so a maintenance outage does not
    #: replay a backlog of ticks on recovery.
    beat_expires_seconds: int = 60
    #: The queue the beat jobs are routed to. A prefork worker with every child busy on a multi-hour
    #: task would never run them from the default queue, so a separate worker consumes this one.
    maintenance_queue: str = "repolace.maintenance"

    #: Where task clones live (`WORKSPACE_PARENT_DIR`, the same variable `PipelineSettings` reads, so
    #: the clone location and the orphan sweep cannot disagree). Unset means the process's TMPDIR.
    workspace_parent_dir: Path | None = Field(default=None, validation_alias="WORKSPACE_PARENT_DIR")

    @property
    def hard_time_limit_seconds(self) -> int:
        return int(self.task_bound_seconds + self.hard_limit_grace_seconds)


@lru_cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
