"""Helpers for the UI tests. Unique basename on purpose (see `api_support`)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from api_support import FakeGithub
from fastapi import FastAPI
from repolace_api.deps import get_github_client
from repolace_api.routes.repos import router as repos_router
from repolace_api.routes.tasks import router as tasks_router
from repolace_api.ui import mount_ui
from repolace_shared.db.models import LLMCall, TaskStage, TaskTestRun

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SAME_ORIGIN = {"Origin": "http://test"}


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def ui_app(session_factory, github: FakeGithub | None = None) -> FastAPI:
    """The JSON routers plus the UI, built as `main.py` builds them, with GitHub faked."""
    app = FastAPI()
    app.include_router(repos_router)
    app.include_router(tasks_router)
    mount_ui(app, allowed_hosts=["test"])
    app.state.session_factory = session_factory
    fake = github or FakeGithub()
    app.dependency_overrides[get_github_client] = lambda: fake
    return app


def ui_client(session_factory, github: FakeGithub | None = None) -> httpx.AsyncClient:
    app = ui_app(session_factory, github)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def seed_stage(session, task, stage: str, status: str, start: float, end: float | None, error: str | None = None):
    row = TaskStage(
        task_id=task.id,
        stage=stage,
        status=status,
        started_at=at(start),
        ended_at=None if end is None else at(end),
        error=error,
    )
    session.add(row)
    await session.commit()
    return row


async def seed_run(session, task, attempt: int, *, passed: int = 3, failed: int = 0, error: str | None = None, seconds=5):
    row = TaskTestRun(
        task_id=task.id,
        attempt=attempt,
        commit_sha="a" * 40,
        passed=[f"tests/test_a.py::t{i}" for i in range(passed)],
        failed=[f"tests/test_a.py::bad{i}" for i in range(failed)],
        error=error,
        duration_seconds=Decimal(seconds),
    )
    session.add(row)
    await session.commit()
    return row


async def seed_call(session, task, cost: str, **overrides: Any):
    fields = {"task_id": task.id, "stage": "agent", "model": "m", "provider": "anthropic", "cost_usd": Decimal(cost)}
    session.add(LLMCall(**{**fields, **overrides}))
    await session.commit()
