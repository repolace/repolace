"""`WORKER_ROLE` decides whether a process builds the task machinery."""

import pytest
from repolace_pipeline import resources as pipeline_resources
from repolace_worker import tasks
from repolace_worker.config import WorkerSettings


@pytest.fixture
def probes(monkeypatch):
    calls = {"build": 0, "embed": 0, "orphans": 0}

    async def fake_build(settings, kind):
        calls["build"] += 1

        class Built:
            async def aclose(self):
                pass

        return Built()

    monkeypatch.setattr(pipeline_resources, "build_resources", fake_build)
    monkeypatch.setattr("repolace_pipeline.config.get_settings", object)
    monkeypatch.setattr("retrieval.embed.get_embedder", lambda: calls.__setitem__("embed", calls["embed"] + 1))
    monkeypatch.setattr(tasks, "remove_stale_workspaces", lambda *a, **k: calls.__setitem__("orphans", 1))
    monkeypatch.setattr(tasks, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(tasks.asyncio, "set_event_loop", lambda loop: None)
    yield calls
    tasks._close_child()


def with_role(monkeypatch, role):
    monkeypatch.setattr(tasks, "get_worker_settings", lambda: WorkerSettings(role=role))


def test_the_task_role_builds_resources_warms_the_embedder_and_sweeps_orphans(monkeypatch, probes):
    with_role(monkeypatch, "task")

    tasks._on_worker_start()
    tasks._init_child()

    assert probes == {"build": 1, "embed": 1, "orphans": 1}
    assert tasks._state is not None


def test_the_maintenance_role_builds_nothing(monkeypatch, probes):
    with_role(monkeypatch, "maintenance")

    tasks._on_worker_start()
    tasks._init_child()

    assert probes == {"build": 0, "embed": 0, "orphans": 0}
    assert tasks._state is None


def test_the_role_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("WORKER_ROLE", "maintenance")
    assert WorkerSettings().role == "maintenance"


def test_the_required_broker_timeout_exceeds_the_hard_limit():
    settings = WorkerSettings()
    assert tasks.required_consumer_timeout_ms(settings) == settings.hard_time_limit_seconds * 1000
