"""A sandbox that is not there at the baseline is repolace's failure, not an inadmissible instance."""

import pytest

from repolace_shared.db.models import TaskStatus
from verify.errors import SandboxUnavailable
from verify.testing import FakeBackend

from repolace_agents.contracts import StopReason
from pipeline_support import reload, seed_task
from test_pipeline_run_task_db import agent_that, run

pytestmark = [pytest.mark.anyio, pytest.mark.db, pytest.mark.usefixtures("embedder")]


class TestABaselineThatNeverReachedTheDaemon:
    async def test_it_fails_the_task_instead_of_completing_it(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        agent = agent_that(StopReason.SUBMITTED)
        backend = FakeBackend(prepare_error=SandboxUnavailable("cannot connect to the docker daemon"))

        result, github = await run(db_session_factory, origin_url, task, agent, backend)

        row, _ = await reload(db_session_factory, task.id)
        assert agent.calls == []
        assert github.pull_requests == []
        assert result.status is row.status is TaskStatus.FAILED, (result.status, row.score_reason)
        assert row.error_message is not None and "container runtime unavailable" in row.error_message
        assert row.outcome is None
