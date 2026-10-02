"""`POST /repos/{id}/tasks` stores the issue body.

Database-backed, because the claim is about what lands in `tasks.issue_body`, and
the column existing since migration 0012 did not make anything write it. The
GitHub client is replaced through FastAPI's dependency override, so no network
and no global mock.
"""

import pytest
from sqlalchemy import select

from repolace_shared.db.models import Task

from api_support import FakeGithub, client_for, raw_issue, seed_repo

pytestmark = [pytest.mark.anyio, pytest.mark.db]


async def post_task(db_session, db_session_factory, github: FakeGithub, issue_number: int = 7):
    repo = await seed_repo(db_session)
    async with client_for(db_session_factory, github) as client:
        response = await client.post(f"/repos/{repo.id}/tasks", json={"issue_number": issue_number})
    return repo, response


async def stored_bodies(db_session) -> list[str | None]:
    return list((await db_session.execute(select(Task.issue_body))).scalars())


class TestIssueBodyIsStored:
    async def test_the_body_of_the_chosen_issue_is_stored_verbatim(self, db_session, db_session_factory):
        body = "Steps:\n\n1. empty `config.toml`\n2. call parse_config\n\nTraceback (most recent call last): ..."
        github = FakeGithub(raw_issue(7, body=body))

        _, response = await post_task(db_session, db_session_factory, github)

        assert response.status_code == 201
        assert await stored_bodies(db_session) == [body]

    async def test_a_null_body_is_stored_as_null(self, db_session, db_session_factory):
        _, response = await post_task(db_session, db_session_factory, FakeGithub(raw_issue(7, body=None)))

        assert response.status_code == 201
        assert await stored_bodies(db_session) == [None]

    async def test_only_the_chosen_issues_body_is_stored(self, db_session, db_session_factory):
        github = FakeGithub(raw_issue(6, body="the other issue"), raw_issue(7, body="the chosen issue"))

        await post_task(db_session, db_session_factory, github, issue_number=7)

        assert await stored_bodies(db_session) == ["the chosen issue"]

    async def test_the_title_and_url_still_come_from_github(self, db_session, db_session_factory):
        github = FakeGithub(raw_issue(7, title="Boom", html_url="https://github.com/acme/sample/issues/7"))

        await post_task(db_session, db_session_factory, github)

        task = (await db_session.execute(select(Task))).scalar_one()
        assert (task.issue_title, task.issue_url) == ("Boom", "https://github.com/acme/sample/issues/7")


class TestResponseShapeIsUnchanged:
    async def test_the_response_has_the_same_keys_and_does_not_echo_the_untrusted_body(
        self, db_session, db_session_factory
    ):
        _, response = await post_task(db_session, db_session_factory, FakeGithub(raw_issue(7)))

        assert set(response.json()) == {
            "id",
            "repo_id",
            "issue_number",
            "issue_title",
            "target_branch",
            "status",
            "open_pr_on_failure",
        }

    async def test_an_issue_that_is_not_open_is_still_a_404_and_stores_nothing(self, db_session, db_session_factory):
        _, response = await post_task(db_session, db_session_factory, FakeGithub(raw_issue(6)), issue_number=7)

        assert response.status_code == 404
        assert await stored_bodies(db_session) == []
