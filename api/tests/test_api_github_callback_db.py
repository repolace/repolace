"""`GET /github/callback` takes `installation_id` from the query string with no proof of who is calling.

It must therefore not undo what a signed webhook did: an installation a `suspend`
event marked suspended stays suspended, and nothing is fetched or re-activated.
Database-backed, because the claim is about rows.
"""

from datetime import UTC, datetime

import httpx
import pytest
from api_support import INSTALLATION_ID, seed_repo
from fastapi import FastAPI
from repolace_api.deps import get_github_client
from repolace_api.routes.github import router as github_router
from repolace_shared.db.models import GithubInstallation

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class ExplodingGithub:
    """Any call fails: a suspended installation's callback must not reach GitHub."""

    def __getattr__(self, name):
        raise AssertionError(f"the callback called GitHub ({name}) for a suspended installation")


def callback_client(session_factory, github) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(github_router)
    app.state.session_factory = session_factory
    app.dependency_overrides[get_github_client] = lambda: github
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestCallbackDoesNotUndoASuspend:
    async def test_a_suspended_installation_is_left_alone(self, db_session, db_session_factory):
        repo = await seed_repo(db_session, is_active=False)
        installation = await db_session.get(GithubInstallation, INSTALLATION_ID)
        installation.suspended_at = datetime.now(UTC)
        await db_session.commit()

        async with callback_client(db_session_factory, ExplodingGithub()) as client:
            response = await client.get(
                "/github/callback", params={"installation_id": INSTALLATION_ID, "setup_action": "update"}
            )

        assert response.status_code == 200
        await db_session.refresh(repo)
        assert repo.is_active is False
