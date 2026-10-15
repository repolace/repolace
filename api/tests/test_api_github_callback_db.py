"""`GET /github/callback` takes `installation_id` from the query string with no proof of who is calling.

So it acknowledges and does nothing else: no GitHub call, no row written or changed,
whatever the installation's state. The signed webhooks do the sync. Database-backed,
because the claim is about rows.
"""

from datetime import UTC, datetime

import httpx
import pytest
from api_support import INSTALLATION_ID, seed_repo
from fastapi import FastAPI
from repolace_api.deps import get_github_client
from repolace_api.routes.github import router as github_router
from repolace_shared.db.models import GithubInstallation, RegisteredRepo
from sqlalchemy import func, select

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class ExplodingGithub:
    """Any call fails: the callback must not reach GitHub."""

    def __getattr__(self, name):
        raise AssertionError(f"the callback called GitHub ({name})")


def callback_client(session_factory) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(github_router)
    app.state.session_factory = session_factory
    app.dependency_overrides[get_github_client] = ExplodingGithub
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def row_counts(session) -> tuple[int, int]:
    installations = (await session.execute(select(func.count()).select_from(GithubInstallation))).scalar_one()
    repos = (await session.execute(select(func.count()).select_from(RegisteredRepo))).scalar_one()
    return installations, repos


class TestCallbackOnlyAcknowledges:
    @pytest.mark.parametrize("setup_action", ["install", "update", "request"])
    async def test_an_unknown_installation_creates_nothing(self, db_session, db_session_factory, setup_action):
        async with callback_client(db_session_factory) as client:
            response = await client.get(
                "/github/callback", params={"installation_id": 777, "setup_action": setup_action}
            )

        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert await row_counts(db_session) == (0, 0)

    async def test_a_suspended_installation_stays_suspended_and_its_repos_inactive(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session, is_active=False)
        installation = await db_session.get(GithubInstallation, INSTALLATION_ID)
        suspended_at = datetime.now(UTC)
        installation.suspended_at = suspended_at
        await db_session.commit()

        async with callback_client(db_session_factory) as client:
            response = await client.get(
                "/github/callback", params={"installation_id": INSTALLATION_ID, "setup_action": "update"}
            )

        assert response.status_code == 200
        await db_session.refresh(repo)
        await db_session.refresh(installation)
        assert repo.is_active is False and installation.suspended_at == suspended_at

    async def test_a_live_installation_is_not_re_synced(self, db_session, db_session_factory):
        repo = await seed_repo(db_session, is_active=False)

        async with callback_client(db_session_factory) as client:
            await client.get(
                "/github/callback", params={"installation_id": INSTALLATION_ID, "setup_action": "install"}
            )

        await db_session.refresh(repo)
        assert repo.is_active is False
