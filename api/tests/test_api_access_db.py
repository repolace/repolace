"""Per-repository access, end to end with real sessions: a user sees only the repositories (and
their tasks) GitHub said they could access at sign-in, and starts tasks only where they can push.
Everything hidden is the same 404 as a row that does not exist. The other API and UI tests run with
access switched off (`ALL_ACCESS`); this file is where it is on."""

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from api_billing_support import seed_user, session_cookie, settings
from api_support import FakeGithub, raw_issue, seed_repo, seed_task
from repolace_api.config import get_billing_settings
from repolace_api.deps import GITHUB_READS, TASK_STARTS, require_viewer
from repolace_shared.db.models import GithubInstallation, Task, TaskStatus, UserRepository
from ui_support import SAME_ORIGIN, ui_app

pytestmark = [pytest.mark.anyio, pytest.mark.db]

OTHER_INSTALLATION = 9999


def app_with_access(db_session_factory, github: FakeGithub | None = None, **billing):
    """`ui_app` with the real access check: its all-access override removed, sign-in configured."""
    app = ui_app(db_session_factory, github or FakeGithub(raw_issue(7)))
    app.dependency_overrides.pop(require_viewer)
    app.dependency_overrides[get_billing_settings] = lambda: settings(**billing)
    return app


def client(app, user=None, *, iat: int | None = None, **kwargs) -> httpx.AsyncClient:
    cookies = session_cookie(user.id, iat=iat) if user is not None else None
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies, **kwargs
    )


async def grant(db_session, user, *github_repo_ids, can_push=True):
    for github_repo_id in github_repo_ids:
        db_session.add(UserRepository(user_id=user.id, github_repo_id=github_repo_id, can_push=can_push))
    await db_session.commit()


@pytest.fixture
async def world(db_session):
    """Repo A (GitHub id 99, installation 4242) with a task, repo B (id 100, installation 9999) with a
    task, and a user GitHub lets push to A only."""
    repo_a = await seed_repo(db_session)
    db_session.add(
        GithubInstallation(id=OTHER_INSTALLATION, account_login="other", account_id=2, account_type="Organization")
    )
    await db_session.commit()
    repo_b = await seed_repo(
        db_session, installation_id=OTHER_INSTALLATION, github_repo_id=100, owner="other", name="secret", full_name="other/secret"
    )
    task_a = await seed_task(db_session, repo_a)
    task_b = await seed_task(db_session, repo_b)
    user = await seed_user(db_session)
    await grant(db_session, user, repo_a.github_repo_id)
    ids = {"repo_a": repo_a.id, "repo_b": repo_b.id, "task_a": task_a.id, "task_b": task_b.id}
    return ids, user


def get_routes(repo, task) -> list[str]:
    return [
        f"/repos/{repo}/issues",
        f"/repos/{repo}/branches",
        f"/repos/{repo}/tasks/estimate",
        f"/repos/{repo}/tasks",
        f"/tasks/{task}",
        f"/ui/repos/{repo}/tasks",
        f"/ui/repos/{repo}/tasks/more?cursor=x",
        f"/ui/repos/{repo}/tasks/new",
        f"/ui/repos/{repo}/tasks/new/issues",
        f"/ui/repos/{repo}/tasks/new/confirm?issue_number=7",
        f"/ui/tasks/{task}",
        f"/ui/tasks/{task}/row",
        f"/ui/tasks/{task}/live",
    ]


class TestWhatASignedInUserSees:
    async def test_the_repo_lists_hold_only_reachable_installations(self, world, db_session_factory):
        ids, user = world
        async with client(app_with_access(db_session_factory), user) as c:
            listed = (await c.get("/repos")).json()
            page = (await c.get("/ui/repos")).text
        assert [r["id"] for r in listed] == [str(ids["repo_a"])]
        assert "acme/sample" in page and "other/secret" not in page

    async def test_every_route_on_another_installation_is_404(self, world, db_session_factory):
        ids, user = world
        github = FakeGithub(raw_issue(7))
        async with client(app_with_access(db_session_factory, github), user) as c:
            for path in get_routes(ids["repo_b"], ids["task_b"]):
                assert (await c.get(path)).status_code == 404, path
            json_enqueue = await c.post(f"/repos/{ids['repo_b']}/tasks", json={"issue_number": 7}, headers=SAME_ORIGIN)
            form_enqueue = await c.post(f"/ui/repos/{ids['repo_b']}/tasks", data={"issue_number": "7"}, headers=SAME_ORIGIN)
        assert json_enqueue.status_code == 404 and form_enqueue.status_code == 404
        assert github.calls == [] and github.branch_calls == []  # GitHub was never asked about B

    async def test_the_same_routes_answer_for_the_reachable_installation(self, world, db_session_factory):
        ids, user = world
        async with client(app_with_access(db_session_factory), user) as c:
            for path in get_routes(ids["repo_a"], ids["task_a"]):
                if "cursor=x" in path:
                    continue  # a malformed cursor is its own error, not an access question
                assert (await c.get(path)).status_code in (200, 286), path

    async def test_the_shared_issue_cache_does_not_leak_across_installations(self, world, db_session, db_session_factory):
        ids, user = world
        insider = await seed_user(db_session, github_user_id=2002, login="insider")
        await grant(db_session, insider, 100)
        app = app_with_access(db_session_factory)
        path = f"/ui/repos/{ids['repo_b']}/tasks/new/issues"
        async with client(app, insider) as c:
            assert (await c.get(path)).status_code == 200  # fills the per-repo cache
        async with client(app, user) as c:
            assert (await c.get(path)).status_code == 404

    async def test_another_repo_in_the_same_installation_stays_hidden(self, world, db_session, db_session_factory):
        """GitHub lists an installation for anyone who can read one of its repositories; that must not
        open its other repositories."""
        _, user = world
        sibling = await seed_repo(db_session, github_repo_id=101, name="private-sibling", full_name="acme/private-sibling")
        sibling_task = await seed_task(db_session, sibling)
        async with client(app_with_access(db_session_factory), user) as c:
            listed = [r["full_name"] for r in (await c.get("/repos")).json()]
            assert listed == ["acme/sample"]
            assert (await c.get(f"/repos/{sibling.id}/issues")).status_code == 404
            assert (await c.get(f"/tasks/{sibling_task.id}")).status_code == 404

    async def test_read_access_shows_the_repo_but_cannot_start_a_task(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        reader = await seed_user(db_session, github_user_id=3003, login="reader")
        await grant(db_session, reader, repo.github_repo_id, can_push=False)
        github = FakeGithub(raw_issue(7))
        async with client(app_with_access(db_session_factory, github), reader) as c:
            assert (await c.get(f"/repos/{repo.id}/issues")).status_code == 200
            assert (await c.get(f"/ui/repos/{repo.id}/tasks")).status_code == 200
            json_enqueue = await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7}, headers=SAME_ORIGIN)
            form_enqueue = await c.post(f"/ui/repos/{repo.id}/tasks", data={"issue_number": "7"}, headers=SAME_ORIGIN)
            new_page = await c.get(f"/ui/repos/{repo.id}/tasks/new")
        assert (json_enqueue.status_code, form_enqueue.status_code, new_page.status_code) == (403, 403, 403)
        assert "push access" in json_enqueue.json()["detail"]

    async def test_the_json_enqueue_refuses_a_cross_site_post(self, world, db_session_factory):
        ids, user = world
        async with client(app_with_access(db_session_factory), user) as c:
            response = await c.post(f"/repos/{ids['repo_a']}/tasks", json={"issue_number": 7})
        assert response.status_code == 403


class TestSignedOut:
    async def test_the_json_api_is_401(self, world, db_session_factory):
        ids, _ = world
        async with client(app_with_access(db_session_factory)) as c:
            assert (await c.get("/repos")).status_code == 401
            assert (await c.get(f"/tasks/{ids['task_a']}")).status_code == 401

    async def test_a_ui_page_redirects_to_sign_in_and_back(self, world, db_session_factory):
        ids, _ = world
        async with client(app_with_access(db_session_factory)) as c:
            response = await c.get(f"/ui/repos/{ids['repo_a']}/tasks", params={"status": "running"})
        assert response.status_code == 303
        location = urlsplit(response.headers["location"])
        assert location.path == "/auth/github/login"
        assert parse_qs(location.query)["next"] == [f"/ui/repos/{ids['repo_a']}/tasks?status=running"]

    async def test_an_htmx_fragment_gets_hx_redirect(self, world, db_session_factory):
        ids, _ = world
        headers = {"HX-Request": "true", "HX-Current-URL": f"http://test/ui/tasks/{ids['task_a']}"}
        async with client(app_with_access(db_session_factory)) as c:
            response = await c.get(f"/ui/tasks/{ids['task_a']}/live", headers=headers)
        assert response.status_code == 401
        target = urlsplit(response.headers["HX-Redirect"])
        assert target.path == "/auth/github/login"
        assert parse_qs(target.query)["next"] == [f"/ui/tasks/{ids['task_a']}"]

    async def test_the_enqueue_form_redirects_to_sign_in(self, world, db_session_factory):
        ids, _ = world
        async with client(app_with_access(db_session_factory)) as c:
            response = await c.post(f"/ui/repos/{ids['repo_a']}/tasks", data={"issue_number": "7"}, headers=SAME_ORIGIN)
        assert response.status_code == 303 and response.headers["location"] == "/auth/github/login"

    async def test_a_session_older_than_the_access_window_counts_as_signed_out(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        user = await seed_user(db_session)
        await grant(db_session, user, repo.github_repo_id)
        async with client(app_with_access(db_session_factory), user, iat=int(time.time()) - 9 * 3600) as c:
            assert (await c.get("/repos")).status_code == 401
            # The account page still takes the 7-day session.
            assert (await c.get("/ui/account")).status_code == 200

    async def test_a_session_without_iat_counts_as_signed_out(self, db_session, db_session_factory):
        """Sessions issued before access existed carry no `iat`."""
        repo = await seed_repo(db_session)
        user = await seed_user(db_session)
        await grant(db_session, user, repo.github_repo_id)
        from repolace_api.deps import SESSION_COOKIE
        from repolace_api.sessions import sign

        old = {SESSION_COOKIE: sign("session", {"uid": str(user.id)}, "s" * 40, max_age=3600)}
        app = app_with_access(db_session_factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=old) as c:
            assert (await c.get("/repos")).status_code == 401


class TestDevNoAuth:
    async def test_everything_is_visible_without_a_session(self, world, db_session_factory):
        ids, _ = world
        async with client(app_with_access(db_session_factory, dev_no_auth=True)) as c:
            listed = {r["id"] for r in (await c.get("/repos")).json()}
            detail = await c.get(f"/tasks/{ids['task_b']}")
        assert listed == {str(ids["repo_a"]), str(ids["repo_b"])} and detail.status_code == 200


class TestShownText:
    async def test_error_text_is_shown_without_host_paths(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        user = await seed_user(db_session)
        await grant(db_session, user, repo.github_repo_id)
        stored = "clone: git clone https://github.com/acme/sample.git /tmp/repolace-task-abc/repo failed (128)"
        task = await seed_task(db_session, repo, status=TaskStatus.FAILED, error_message=stored)
        task_id = task.id
        async with client(app_with_access(db_session_factory), user) as c:
            detail = (await c.get(f"/tasks/{task_id}")).json()
            listed = (await c.get(f"/repos/{repo.id}/tasks")).json()["items"][0]
            page = (await c.get(f"/ui/tasks/{task_id}")).text
        expected = "clone: git clone https://github.com/acme/sample.git <path>/repo failed (128)"
        assert detail["error_message"] == expected and listed["error_message"] == expected
        assert "/tmp/repolace-task-abc" not in page
        db_session.expire_all()
        assert (await db_session.get(Task, task_id)).error_message == stored  # the stored row is untouched


class TestPerUserLimits:
    @pytest.fixture
    def tight(self, monkeypatch):
        """One task start and two GitHub reads per hour, with clean counters."""
        for limiter, limit in ((TASK_STARTS, 1), (GITHUB_READS, 2)):
            monkeypatch.setattr(limiter, "limit", limit)
            monkeypatch.setattr(limiter, "_events", {})

    async def test_task_starts_are_limited_with_retry_after(self, tight, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        user = await seed_user(db_session)
        await grant(db_session, user, repo.github_repo_id)
        async with client(app_with_access(db_session_factory), user) as c:
            first = await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 7}, headers=SAME_ORIGIN)
            second = await c.post(f"/repos/{repo.id}/tasks", json={"issue_number": 8}, headers=SAME_ORIGIN)
        assert first.status_code == 201
        assert second.status_code == 429 and int(second.headers["retry-after"]) > 0

    async def test_github_reads_are_limited_per_user(self, tight, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        alice = await seed_user(db_session)
        bob = await seed_user(db_session, github_user_id=4004, login="bob")
        await grant(db_session, alice, repo.github_repo_id)
        await grant(db_session, bob, repo.github_repo_id)
        app = app_with_access(db_session_factory)
        async with client(app, alice) as c:
            codes = [(await c.get(f"/repos/{repo.id}/branches")).status_code for _ in range(3)]
        async with client(app, bob) as c:
            bobs = (await c.get(f"/repos/{repo.id}/branches")).status_code
        assert codes == [200, 200, 429] and bobs == 200

    async def test_no_limit_without_a_user(self, tight, world, db_session_factory):
        ids, _ = world
        async with client(app_with_access(db_session_factory, dev_no_auth=True)) as c:
            codes = [(await c.get(f"/repos/{ids['repo_a']}/branches")).status_code for _ in range(4)]
        assert codes == [200] * 4


class TestNewTaskButton:
    async def test_read_only_repos_show_no_new_task_button(self, db_session, db_session_factory):
        writable = await seed_repo(db_session)
        readable = await seed_repo(db_session, github_repo_id=100, name="docs", full_name="acme/docs")
        user = await seed_user(db_session)
        await grant(db_session, user, writable.github_repo_id)
        await grant(db_session, user, readable.github_repo_id, can_push=False)
        async with client(app_with_access(db_session_factory), user) as c:
            listing = (await c.get("/ui/repos")).text
            tasks_page = (await c.get(f"/ui/repos/{readable.id}/tasks")).text
            own_page = (await c.get(f"/ui/repos/{writable.id}/tasks")).text
        assert f"/ui/repos/{writable.id}/tasks/new" in listing
        assert f"/ui/repos/{readable.id}/tasks/new" not in listing and "read only" in listing
        assert f"/ui/repos/{readable.id}/tasks/new" not in tasks_page and "needs push access" in tasks_page
        assert f"/ui/repos/{writable.id}/tasks/new" in own_page
