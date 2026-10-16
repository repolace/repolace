"""The task body (`run_one`) against a real database and a real local git remote.

Same seams as `pipeline/tests/test_pipeline_run_task_db.py`: the sandbox is `FakeBackend`, GitHub is
`FakeGithubClient`, the model is a scripted agent. What is under test is what the worker adds on top:
the claimant, the bound, and what happens to a redelivered or failing message.
"""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from pipeline_support import (
    AFTER,
    APP_SOURCE,
    BASELINE,
    FakeGithubClient,
    ScriptedAgent,
    agent_result,
    edit,
    local_workspace_factory,
    reload,
    seed_task,
)
from repolace_agents.contracts import StopReason
from repolace_shared.db.models import TaskStatus
from repolace_worker import tasks
from verify.testing import FakeBackend

pytestmark = [pytest.mark.anyio, pytest.mark.db, pytest.mark.usefixtures("embedder")]

CLAIMANT = "celery@host:4321"


def make_resources(factory):
    settings = SimpleNamespace(
        instances_dir="/nonexistent", embedding_strategy="truncate", max_changed_files=10,
        max_changed_lines=500, workspace_parent_dir=None,
    )
    return SimpleNamespace(
        settings=settings, session_factory=factory, github=FakeGithubClient(),
        backend=FakeBackend(results=[BASELINE, AFTER]), specs={}, llm=None,
    )


def fixing_agent() -> ScriptedAgent:
    async def script(deps):
        edit(deps, "src/app.py", APP_SOURCE + "\n# edited\n")
        return agent_result(StopReason.SUBMITTED, await deps.verify_attempt(1))

    return ScriptedAgent(script)


async def run(factory, origin_url, task_id, agent, *, bound=60.0):
    return await tasks.run_one(
        task_id, make_resources(factory), claimed_by=CLAIMANT, bound_seconds=bound,
        agent=agent, workspace_factory=local_workspace_factory(origin_url), embedder_warmup=lambda: None,
    )


class TestARunThroughTheWorkerBody:
    async def test_a_queued_task_runs_and_records_who_claimed_it(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        outcome = await run(db_session_factory, origin_url, task.id, fixing_agent())

        row, _ = await reload(db_session_factory, task.id)
        assert outcome == tasks.DONE
        assert row.status in (TaskStatus.COMPLETED, TaskStatus.PR_OPENED)
        assert row.claimed_by == CLAIMANT


class TestRedeliveryAndFailure:
    async def test_a_redelivered_message_for_a_running_row_is_dropped_untouched(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session, status=TaskStatus.RUNNING, claimed_by="other:1")
        agent = fixing_agent()

        outcome = await run(db_session_factory, origin_url, task.id, agent)

        row, _ = await reload(db_session_factory, task.id)
        assert outcome == tasks.NOT_CLAIMABLE
        assert agent.calls == []
        assert (row.status, row.claimed_by) == (TaskStatus.RUNNING, "other:1")

    async def test_the_same_message_twice_runs_the_task_once(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        agent = fixing_agent()

        first = await run(db_session_factory, origin_url, task.id, agent)
        second = await run(db_session_factory, origin_url, task.id, agent)

        assert (first, second) == (tasks.DONE, tasks.NOT_CLAIMABLE)
        assert len(agent.calls) == 1

    async def test_an_unknown_task_is_dropped(self, db_session_factory, origin_url):
        assert await run(db_session_factory, origin_url, uuid.uuid4(), fixing_agent()) == tasks.NOT_FOUND

    async def test_an_agent_that_raises_never_raises_out_of_the_task(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        async def boom(deps):
            raise RuntimeError("agent exploded")

        outcome = await run(db_session_factory, origin_url, task.id, ScriptedAgent(boom))

        row, _ = await reload(db_session_factory, task.id)
        assert outcome in (tasks.ERRORED, tasks.DONE)  # `run_task` may settle the row itself and return
        assert row.status is TaskStatus.FAILED

    async def test_the_bound_fails_the_row_and_returns(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)

        async def hang(deps):
            await asyncio.sleep(30)

        outcome = await run(db_session_factory, origin_url, task.id, ScriptedAgent(hang), bound=1.5)

        row, _ = await reload(db_session_factory, task.id)
        assert outcome == tasks.TIMED_OUT
        # `run_task` settles a cancelled row itself ("interrupted"); `worker: timeout` is the backstop.
        assert row.status is TaskStatus.FAILED
        assert row.error_message in ("worker: timeout", "interrupted: CancelledError")


class TestTheCeleryCallPath:
    def test_the_task_runs_the_body_on_the_childs_loop_with_host_and_pid(self, monkeypatch):
        seen = {}

        async def fake_run_one(task_id, resources, **kwargs):
            seen.update(task_id=task_id, resources=resources, **kwargs)
            return tasks.DONE

        monkeypatch.setattr(tasks, "run_one", fake_run_one)
        loop = asyncio.new_event_loop()
        tasks.install_state(tasks.ChildState(loop, "resources"))
        tasks.celery_app.conf.task_always_eager = True
        try:
            result = tasks.run_task_job.apply(args=["3f2b8c1e-0000-4000-8000-000000000001"])
        finally:
            tasks.celery_app.conf.task_always_eager = False
            tasks.install_state(None)
            loop.close()

        assert result.get() == tasks.DONE
        assert seen["resources"] == "resources"
        assert seen["claimed_by"].endswith(f":{os.getpid()}")
        assert seen["bound_seconds"] == tasks.get_worker_settings().task_bound_seconds

    def test_a_child_without_resources_refuses_rather_than_running(self):
        tasks.install_state(None)
        with pytest.raises(RuntimeError, match="no resources"):
            tasks.run_task_job.run("3f2b8c1e-0000-4000-8000-000000000001")

    def test_the_task_is_registered_acks_late_and_not_lost_silently(self):
        task = tasks.celery_app.tasks["repolace.run_task"]
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True
