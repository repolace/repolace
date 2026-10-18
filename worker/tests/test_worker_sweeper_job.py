"""What the sweeper job hands the broker and the scanner, with no database and no real Redis.

`sweep_queued` itself is covered against Postgres in `test_worker_maintenance_db.py`. What these
tests pin is the wiring the beat job adds around it: the expiry on a republished message, and the
bound on how many old rows one tick may read.
"""

import uuid

import pytest
from repolace_worker import maintenance
from repolace_worker.config import WorkerSettings

TASK_ID = uuid.UUID("3f2b8c1e-0000-4000-8000-000000000001")


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """Every `send_task` call, as its keyword arguments."""
    calls: list[dict] = []

    def send_task(name, **kwargs):
        calls.append({"name": name, **kwargs})

    monkeypatch.setattr(maintenance.celery_app, "send_task", send_task)
    return calls


class TestARepublishedMessageExpires:
    def test_it_carries_the_expiry_it_is_given(self, sent):
        maintenance.publish_by_name(TASK_ID, expires=900)

        assert sent == [
            {"name": "repolace.run_task", "args": [str(TASK_ID)], "task_id": str(TASK_ID), "retry": False, "expires": 900}
        ]

    def test_without_one_it_is_published_exactly_as_the_api_publishes(self, sent):
        maintenance.publish_by_name(TASK_ID)

        assert "expires" not in sent[0]


class TestTheJobWiring:
    @pytest.fixture
    def tick(self, monkeypatch, sent) -> dict:
        """Run `sweep_queued_job` with the scan and the Redis client replaced, and say what it passed."""
        seen: dict = {}

        class FakeClaims:
            def __init__(self, redis_url, ttl_seconds):
                seen["claims_ttl"] = ttl_seconds

            def close(self):
                seen["closed"] = True

        async def fake_sweep(factory, min_age, publish, *, claims, max_per_tick, scan_limit):
            seen.update(min_age=min_age, max_per_tick=max_per_tick, scan_limit=scan_limit)
            publish(TASK_ID)
            return [TASK_ID]

        async def fake_with_factory(job):
            return await job("factory")

        monkeypatch.setattr(maintenance, "RedisSweepClaims", FakeClaims)
        monkeypatch.setattr(maintenance, "sweep_queued", fake_sweep)
        monkeypatch.setattr(maintenance, "_with_factory", fake_with_factory)
        seen["result"] = maintenance.sweep_queued_job()
        return seen

    def test_republished_messages_expire_when_the_next_copy_may_be_published(self, tick, sent):
        """One dedupe window is the longest a task is left alone, so a copy still unconsumed after it
        is dead before the next one goes out: a backlog never holds two live copies of one row."""
        assert [call["expires"] for call in sent] == [WorkerSettings().sweeper_dedupe_seconds]

    def test_one_tick_reads_at_most_what_a_dedupe_window_can_publish(self, tick):
        settings = WorkerSettings()
        ticks_per_window = -(-settings.sweeper_dedupe_seconds // int(settings.sweeper_interval_seconds))
        assert tick["scan_limit"] == settings.sweeper_max_per_tick * ticks_per_window
        assert tick["max_per_tick"] == settings.sweeper_max_per_tick

    def test_the_redis_client_is_closed_and_the_count_returned(self, tick):
        assert tick["closed"] is True
        assert tick["result"] == 1
