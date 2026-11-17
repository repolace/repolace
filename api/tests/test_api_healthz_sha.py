"""deploy-app.sh proves a deploy by reading the build sha from the API's /healthz."""

import httpx
import pytest
from repolace_api.main import app

pytestmark = pytest.mark.anyio


async def _healthz() -> dict:
    transport = httpx.ASGITransport(app=app)  # no lifespan: nothing here needs the database
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    return response.json()



async def test_healthz_reports_the_build_sha(monkeypatch):
    monkeypatch.setenv("REPOLACE_GIT_SHA", "a" * 40)
    assert await _healthz() == {"status": "ok", "sha": "a" * 40}



async def test_healthz_reports_unknown_without_a_build_sha(monkeypatch):
    monkeypatch.delenv("REPOLACE_GIT_SHA", raising=False)
    assert await _healthz() == {"status": "ok", "sha": "unknown"}
