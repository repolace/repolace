"""`POST /repos/{id}/tasks` publishes the committed row, and a publish failure never fails the request."""

import uuid

import httpx
import pytest
from api_support import FakeGithub, raw_issue, seed_repo
from fastapi import FastAPI
from repolace_api.deps import get_github_client
from repolace_api.publisher import CeleryTaskPublisher, get_publisher
from repolace_api.routes.repos import router as repos_router
from repolace_shared.db.models import Task, TaskStatus
from sqlalchemy import select

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class RecordingPublisher:
    def __init__(self, error: Exception | None = None):
        self.published: list[uuid.UUID] = []
        self.error = error

    async def publish(self, task_id: uuid.UUID) -> bool:
        if self.error is not None:
            raise self.error
        self.published.append(task_id)
        return True


def client_with(session_factory, publisher) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(repos_router)
    app.state.session_factory = session_factory
    app.dependency_overrides[get_github_client] = lambda: FakeGithub(raw_issue(7))
    app.dependency_overrides[get_publisher] = lambda: publisher
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_the_committed_task_id_is_published(db_session, db_session_factory):
    repo = await seed_repo(db_session)
    publisher = RecordingPublisher()

    async with client_with(db_session_factory, publisher) as client:
        response = await client.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7})

    assert response.status_code == 201
    assert publisher.published == [uuid.UUID(response.json()["id"])]
    row = (await db_session.execute(select(Task))).scalar_one()
    assert row.status is TaskStatus.QUEUED


async def test_a_publish_failure_does_not_fail_the_request_and_the_row_stays_queued(
    db_session, db_session_factory, monkeypatch
):
    repo = await seed_repo(db_session)
    publisher = CeleryTaskPublisher("amqp://nobody:nobody@127.0.0.1:1//")

    def refuse(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(publisher._app, "send_task", refuse)

    async with client_with(db_session_factory, publisher) as client:
        response = await client.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7})

    assert response.status_code == 201
    assert await publisher.publish(uuid.uuid4()) is False
    row = (await db_session.execute(select(Task))).scalar_one()
    assert row.status is TaskStatus.QUEUED
