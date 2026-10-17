import importlib
import os

import pytest
from repolace_worker.celery_app import celery_app
from repolace_worker.config import DEFAULT_TASK_BOUND_SECONDS, WorkerSettings

#: Same idea as `REPOLACE_TEST_DB_REQUIRED` in the root conftest: a silent skip is how a missing
#: dependency turns into a green build that checked nothing.
REQUIRE_EVAL_ENV_VAR = "REPOLACE_TEST_EVAL_REQUIRED"


def import_eval(name: str):
    """Import a module of the eval harness, which the worker may not depend on at run time.

    Under `uv sync --all-packages` it is always importable. When it is not, the test that needs it
    skips, unless `REPOLACE_TEST_EVAL_REQUIRED` is set, in which case it fails.
    """
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        reason = f"the eval harness is not importable: {exc}"
        if os.environ.get(REQUIRE_EVAL_ENV_VAR):
            pytest.fail(f"{reason} (and {REQUIRE_EVAL_ENV_VAR} is set)")
        pytest.skip(reason)


def test_the_bound_equals_the_eval_runners_derived_bound():
    enqueue = import_eval("harness.enqueue")
    assert enqueue.task_wall_clock_bound() == DEFAULT_TASK_BOUND_SECONDS


class TestAMissingEvalHarnessIsNotASilentSkip:
    @pytest.fixture
    def no_eval(self, monkeypatch):
        def refuse(name):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)

        monkeypatch.setattr(importlib, "import_module", refuse)

    def test_it_fails_when_the_harness_is_required(self, no_eval, monkeypatch):
        monkeypatch.setenv(REQUIRE_EVAL_ENV_VAR, "1")

        with pytest.raises(pytest.fail.Exception, match=REQUIRE_EVAL_ENV_VAR):
            import_eval("harness.enqueue")

    def test_it_skips_when_the_harness_is_not_required(self, no_eval, monkeypatch):
        monkeypatch.delenv(REQUIRE_EVAL_ENV_VAR, raising=False)

        with pytest.raises(pytest.skip.Exception, match="not importable"):
            import_eval("harness.enqueue")


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


def test_children_are_recycled_and_beat_messages_expire():
    conf = celery_app.conf
    assert conf.worker_max_memory_per_child == 4_000_000
    assert conf.worker_max_tasks_per_child == 50
    assert all(entry["options"]["expires"] == 60 for entry in conf.beat_schedule.values())
    assert WorkerSettings().reaper_stale_seconds == 300.0
