"""Issues (extra fields), branches, and the branch check on `POST /repos/{id}/tasks`."""

import uuid

import httpx
import pytest
from api_support import FakeGithub, client_for, raw_issue, seed_repo
from repolace_shared.db.models import Task
from repolace_shared.github.schemas import Branch
from sqlalchemy import func, select

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class _Down(FakeGithub):
    async def list_branches(self, *args):
        raise httpx.ConnectError("boom")


class TestIssues:
    async def test_labels_author_and_created_at_are_returned_beside_the_old_fields(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        github = FakeGithub(raw_issue(7, labels=[{"name": "bug"}, {"name": "p1"}], created_at="2026-10-01T12:30:00Z"))

        async with client_for(db_session_factory, github) as client:
            response = await client.get(f"/repos/{repo.id}/issues")

        assert response.status_code == 200
        [issue] = response.json()
        assert issue["number"] == 7
        assert issue["html_url"] == "https://github.com/acme/sample/issues/7"
        assert issue["labels"] == ["bug", "p1"]
        assert issue["author"] == "someone"
        assert issue["created_at"].startswith("2026-10-01T12:30:00")

    async def test_an_issue_without_them_gets_empty_defaults(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        raw = raw_issue(7)
        del raw["labels"]
        del raw["user"]

        async with client_for(db_session_factory, FakeGithub(raw)) as client:
            [issue] = (await client.get(f"/repos/{repo.id}/issues")).json()

        assert (issue["labels"], issue["author"], issue["created_at"]) == ([], None, None)


class TestBranches:
    async def test_names_and_a_default_flag(self, db_session, db_session_factory):
        repo = await seed_repo(db_session, default_branch="develop")
        github = FakeGithub(branches=("main", "develop"))

        async with client_for(db_session_factory, github) as client:
            response = await client.get(f"/repos/{repo.id}/branches")

        assert response.json() == [{"name": "main", "default": False}, {"name": "develop", "default": True}]

    async def test_an_unknown_repo_is_404(self, db_session_factory):
        async with client_for(db_session_factory, FakeGithub()) as client:
            response = await client.get(f"/repos/{uuid.uuid4()}/branches")

        assert response.status_code == 404

    async def test_a_github_failure_is_a_502(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)

        async with client_for(db_session_factory, _Down()) as client:
            response = await client.get(f"/repos/{repo.id}/branches")

        assert response.status_code == 502


class TestCreateTaskChecksTheBranch:
    async def post(self, db_session, db_session_factory, github, **body):
        repo = await seed_repo(db_session)
        async with client_for(db_session_factory, github) as client:
            return await client.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7, **body})

    async def count(self, db_session) -> int:
        return (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()

    async def test_a_branch_that_does_not_exist_is_422_and_queues_nothing(self, db_session, db_session_factory):
        response = await self.post(
            db_session, db_session_factory, FakeGithub(raw_issue(7)), target_branch="no-such-branch"
        )

        assert response.status_code == 422
        assert "no-such-branch" in response.json()["detail"]
        assert await self.count(db_session) == 0

    async def test_an_existing_branch_is_accepted(self, db_session, db_session_factory):
        response = await self.post(db_session, db_session_factory, FakeGithub(raw_issue(7)), target_branch="develop")

        assert response.status_code == 201
        assert response.json()["target_branch"] == "develop"

    async def test_an_empty_branch_is_still_400(self, db_session, db_session_factory):
        response = await self.post(db_session, db_session_factory, FakeGithub(raw_issue(7)), target_branch="  ")

        assert response.status_code == 400

    async def test_the_default_branch_costs_no_branch_lookup(self, db_session, db_session_factory):
        github = FakeGithub(raw_issue(7))

        response = await self.post(db_session, db_session_factory, github)

        assert response.status_code == 201
        assert github.branch_calls == []

    async def test_a_github_failure_while_listing_branches_is_a_502(self, db_session, db_session_factory):
        response = await self.post(db_session, db_session_factory, _Down(raw_issue(7)), target_branch="develop")

        assert response.status_code == 502

    async def test_whitespace_around_the_branch_is_stripped_before_the_check(self, db_session, db_session_factory):
        response = await self.post(db_session, db_session_factory, FakeGithub(raw_issue(7)), target_branch=" develop ")

        assert response.status_code == 201
        assert response.json()["target_branch"] == "develop"

    async def test_a_truncated_listing_falls_back_to_asking_about_the_one_branch(
        self, db_session, db_session_factory
    ):
        github = FakeGithub(raw_issue(7), truncated=True, hidden_branches=("feature/deep",))

        found = await self.post(db_session, db_session_factory, github, target_branch="feature/deep")

        assert found.status_code == 201
        assert github.exists_calls == ["feature/deep"]

    async def test_a_truncated_listing_and_a_missing_branch_is_still_422(self, db_session, db_session_factory):
        github = FakeGithub(raw_issue(7), truncated=True)

        response = await self.post(db_session, db_session_factory, github, target_branch="nope")

        assert response.status_code == 422
        assert github.exists_calls == ["nope"]

    async def test_a_complete_listing_does_not_ask_again(self, db_session, db_session_factory):
        github = FakeGithub(raw_issue(7))

        await self.post(db_session, db_session_factory, github, target_branch="nope")

        assert github.exists_calls == []

    async def test_a_traversal_name_is_refused_without_a_request_when_truncated(self, db_session, db_session_factory):
        github = FakeGithub(raw_issue(7), truncated=True)

        response = await self.post(db_session, db_session_factory, github, target_branch="../../x")

        assert response.status_code == 422
        assert github.exists_calls == []

    async def test_a_malformed_github_payload_is_a_502(self, db_session, db_session_factory):
        class Malformed(FakeGithub):
            async def list_branches(self, *args):
                return [Branch.model_validate({"nope": 1})]

        response = await self.post(db_session, db_session_factory, Malformed(raw_issue(7)), target_branch="develop")

        assert response.status_code == 502
