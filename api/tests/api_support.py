"""Helpers for API tests.

Named `api_support` for the reason `verify_support` records: pytest's prepend
import mode makes test-module names global across the workspace.

Not imported from `shared/tests/db_support.py`, which is reachable here only by
accident of `sys.path`.
"""

import uuid
from typing import Any

import httpx
from fastapi import FastAPI

from repolace_api.deps import get_github_client
from repolace_api.routes.repos import router as repos_router
from repolace_shared.db.models import GithubInstallation, RegisteredRepo
from repolace_shared.github.schemas import Issue

INSTALLATION_ID = 4242


def raw_issue(number: int = 7, **overrides: Any) -> dict[str, Any]:
    """An issue as GitHub's REST API sends it, extra keys and all.

    Built as the raw JSON mapping and parsed by the real `Issue` model, so a field
    the model forgets to declare is dropped here exactly as it is in production.
    """
    fields = {
        "number": number,
        "title": "parse_config crashes on an empty config file",
        "html_url": f"https://github.com/acme/sample/issues/{number}",
        "state": "open",
        "body": "Steps to reproduce:\n\n1. Create an empty `config.toml`\n2. Call `parse_config`\n",
        "user": {"login": "someone", "id": 1},
        "labels": [{"name": "bug"}],
        "comments": 0,
    }
    return {**fields, **overrides}


class FakeGithub:
    """The one `GithubClient` method `create_task` calls, over canned issues."""

    def __init__(self, *raw_issues: dict[str, Any]):
        self._issues = [Issue.model_validate(raw) for raw in raw_issues]
        self.calls: list[tuple[int, str, str]] = []

    async def list_repo_open_issues(self, installation_id: int, owner: str, repo: str) -> list[Issue]:
        self.calls.append((installation_id, owner, repo))
        return list(self._issues)


async def seed_repo(session, **overrides: Any) -> RegisteredRepo:
    session.add(
        GithubInstallation(id=INSTALLATION_ID, account_login="acme", account_id=1, account_type="Organization")
    )
    fields = {
        "id": uuid.uuid4(),
        "installation_id": INSTALLATION_ID,
        "github_repo_id": 99,
        "owner": "acme",
        "name": "sample",
        "full_name": "acme/sample",
        "default_branch": "main",
        "private": False,
    }
    repo = RegisteredRepo(**{**fields, **overrides})
    session.add(repo)
    await session.commit()
    return repo


def client_for(session_factory, github: FakeGithub) -> httpx.AsyncClient:
    """An in-process client over the repos router with the GitHub dependency replaced.

    The app is built here and not imported from `repolace_api.main`, whose
    lifespan would open a real engine and build a real `GithubClient`.
    """
    app = FastAPI()
    app.include_router(repos_router)
    app.state.session_factory = session_factory
    app.dependency_overrides[get_github_client] = lambda: github
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
