import pytest
from repolace_worker.celery_app import celery_app
from repolace_worker.config import DEFAULT_TASK_BOUND_SECONDS, WorkerSettings


def test_the_bound_equals_the_eval_runners_derived_bound():
    enqueue = pytest.importorskip("harness.enqueue")
    assert enqueue.task_wall_clock_bound() == DEFAULT_TASK_BOUND_SECONDS


def test_the_hard_limit_is_the_bound_plus_the_grace():
    assert WorkerSettings().hard_time_limit_seconds == int(DEFAULT_TASK_BOUND_SECONDS + 600)


def test_celery_is_configured_for_long_acks_late_work():
    conf = celery_app.conf
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_ignore_result is True
    assert conf.broker_connection_retry_on_startup is True
    assert conf.task_time_limit == WorkerSettings().hard_time_limit_seconds
    assert {entry["task"] for entry in conf.beat_schedule.values()} == {
        "repolace.sweep_queued",
        "repolace.reap_abandoned",
    }
