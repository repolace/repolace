"""GitHub sign-in: the state check, the user upsert and the session cookie. GitHub is a mock transport."""

import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from api_billing_support import (
    SAME_ORIGIN,
    SESSION_SECRET,
    build_app,
    client_for,
    override_github,
    seed_user,
    session_cookie,
    settings,
)
from repolace_api.deps import SESSION_COOKIE
from repolace_api.routes.auth import AFTER_LOGIN, STATE_COOKIE, safe_next
from repolace_api.sessions import unsign
from repolace_shared.db.models import User, UserRepository
from sqlalchemy import func, select

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def github_handler(profile=None, token_ok=True, seen=None, installations=None, status=200):
    """GitHub's token, `/user`, `/user/installations` and `/user/installations/{id}/repositories`.

    `installations` maps an installation id to its repositories as `{repo_id: can_push}` (default: one
    installation, 4242, with repository 99 pushable). A dict body under the key "raw" is served as is
    for the installation list, with `status`. Lists are paged 100 at a time like GitHub's.
    """
    installations = {4242: {99: True}} if installations is None else installations

    def paged(request, items, key):
        page = int(request.url.params.get("page", "1"))
        chunk = items[(page - 1) * 100 : page * 100]
        return httpx.Response(200, json={"total_count": len(items), key: chunk})

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/login/oauth/access_token":
            if not token_ok:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "gho_fake", "token_type": "bearer"})
        assert request.headers["authorization"] == "Bearer gho_fake"
        path = request.url.path
        if path == "/user/installations":
            if "raw" in installations:
                return httpx.Response(status, json=installations["raw"])
            return paged(request, [{"id": i, "client_id": "client-id"} for i in installations], "installations")
        if path.startswith("/user/installations/") and path.endswith("/repositories"):
            repos = installations[int(path.split("/")[3])]
            items = [{"id": r, "permissions": {"pull": True, "push": push}} for r, push in repos.items()]
            return paged(request, items, "repositories")
        return httpx.Response(200, json=profile or {"id": 77, "login": "octocat", "email": "o@example.com"})

    return handler


async def start_login(client) -> str:
    response = await client.get("/auth/github/login")
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


class TestUnconfigured:
    @pytest.mark.parametrize("missing", ["github_oauth_client_id", "github_oauth_client_secret", "session_secret"])
    async def test_login_and_callback_answer_503(self, db_session_factory, missing):
        app = build_app(db_session_factory, billing=settings(**{missing: None}))
        async with client_for(app) as client:
            assert (await client.get("/auth/github/login")).status_code == 503
            assert (await client.get("/auth/github/callback", params={"code": "c", "state": "s"})).status_code == 503

    async def test_a_short_session_secret_counts_as_unconfigured(self, db_session_factory):
        app = build_app(db_session_factory, billing=settings(session_secret="short"))
        async with client_for(app) as client:
            assert (await client.get("/auth/github/login")).status_code == 503


class TestLogin:
    async def test_redirects_to_github_with_state_and_a_signed_httponly_lax_cookie(self, db_session_factory):
        app = build_app(db_session_factory)
        async with client_for(app) as client:
            response = await client.get("/auth/github/login")
        location = urlsplit(response.headers["location"])
        query = parse_qs(location.query)
        assert (location.scheme, location.netloc, location.path) == ("https", "github.com", "/login/oauth/authorize")
        assert query["client_id"] == ["client-id"]
        assert query["redirect_uri"] == ["http://test/auth/github/callback"]
        cookie = response.headers["set-cookie"].lower()
        assert STATE_COOKIE in cookie and "httponly" in cookie and "samesite=lax" in cookie
        assert "secure" not in cookie.replace("samesite", "")  # http base url
        value = response.cookies[STATE_COOKIE]
        assert unsign(value, "oauth-state", SESSION_SECRET)["s"] == query["state"][0]

    async def test_cookies_are_secure_when_the_base_url_is_https(self, db_session_factory):
        app = build_app(db_session_factory, billing=settings(api_base_url="https://app.example.com"))
        async with client_for(app) as client:
            response = await client.get("/auth/github/login")
        assert "secure" in response.headers["set-cookie"].lower()


class TestCallback:
    async def test_state_mismatch_is_400_and_github_is_never_called(self, db_session, db_session_factory):
        app = build_app(db_session_factory)
        seen: list = []
        override_github(app, github_handler(seen=seen))
        async with client_for(app) as client:
            await start_login(client)
            response = await client.get("/auth/github/callback", params={"code": "c", "state": "not-the-state"})
        assert response.status_code == 400
        assert seen == []
        assert (await db_session.scalar(select(func.count()).select_from(User))) == 0
        assert SESSION_COOKIE not in response.cookies

    async def test_a_missing_state_cookie_is_400(self, db_session_factory):
        app = build_app(db_session_factory)
        override_github(app, github_handler())
        async with client_for(app) as client:
            response = await client.get("/auth/github/callback", params={"code": "c", "state": "anything"})
        assert response.status_code == 400

    async def test_a_state_cookie_signed_with_another_secret_is_400(self, db_session_factory):
        from repolace_api.sessions import sign

        app = build_app(db_session_factory)
        override_github(app, github_handler())
        forged = sign("oauth-state", {"s": "known"}, "x" * 40, max_age=60)
        async with client_for(app, cookies={STATE_COOKIE: forged}) as client:
            response = await client.get("/auth/github/callback", params={"code": "c", "state": "known"})
        assert response.status_code == 400

    async def test_a_session_cookie_cannot_stand_in_for_the_state_cookie(self, db_session_factory):
        app = build_app(db_session_factory)
        override_github(app, github_handler())
        stolen = session_cookie(uuid.uuid4())[SESSION_COOKIE]
        async with client_for(app, cookies={STATE_COOKIE: stolen}) as client:
            response = await client.get("/auth/github/callback", params={"code": "c", "state": "x"})
        assert response.status_code == 400

    async def test_github_error_param_is_400(self, db_session_factory):
        app = build_app(db_session_factory)
        override_github(app, github_handler())
        async with client_for(app) as client:
            state = await start_login(client)
            response = await client.get("/auth/github/callback", params={"error": "access_denied", "state": state})
        assert response.status_code == 400

    async def test_a_refused_code_is_400_and_creates_no_user(self, db_session, db_session_factory):
        app = build_app(db_session_factory)
        override_github(app, github_handler(token_ok=False))
        async with client_for(app) as client:
            state = await start_login(client)
            response = await client.get("/auth/github/callback", params={"code": "bad", "state": state})
        assert response.status_code == 400
        assert (await db_session.scalar(select(func.count()).select_from(User))) == 0

    async def test_success_creates_the_user_and_sets_a_session_cookie(self, db_session, db_session_factory):
        app = build_app(db_session_factory)
        seen: list = []
        override_github(app, github_handler(seen=seen))
        async with client_for(app) as client:
            state = await start_login(client)
            response = await client.get("/auth/github/callback", params={"code": "the-code", "state": state})
        assert response.status_code == 303 and response.headers["location"] == "/ui/repos"
        user = (await db_session.execute(select(User))).scalar_one()
        assert (user.github_user_id, user.github_login, user.email) == (77, "octocat", "o@example.com")
        payload = unsign(response.cookies[SESSION_COOKIE], "session", SESSION_SECRET)
        assert payload["uid"] == str(user.id)
        cookies = [c.lower() for c in response.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE)]
        assert "httponly" in cookies[0] and "samesite=lax" in cookies[0]
        # The token exchange got the code; nothing stored the GitHub token.
        assert b"the-code" in seen[0].content
        assert "gho_fake" not in str(user.__dict__)

    async def test_a_second_sign_in_updates_the_login_and_keeps_one_row(self, db_session, db_session_factory):
        existing = await seed_user(db_session, github_user_id=77, login="old-name")
        app = build_app(db_session_factory)
        override_github(app, github_handler(profile={"id": 77, "login": "new-name", "email": None}))
        async with client_for(app) as client:
            state = await start_login(client)
            response = await client.get("/auth/github/callback", params={"code": "c", "state": state})
        assert response.status_code == 303
        db_session.expire_all()
        rows = (await db_session.execute(select(User))).scalars().all()
        assert [(r.id, r.github_login) for r in rows] == [(existing.id, "new-name")]


class TestLogout:
    async def test_logout_clears_the_cookie(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        app = build_app(db_session_factory)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            response = await client.post("/auth/logout", headers=SAME_ORIGIN)
        assert response.status_code == 204
        assert SESSION_COOKIE in response.headers["set-cookie"] and "max-age=0" in response.headers["set-cookie"].lower()

    async def test_a_browser_logout_lands_on_the_account_page(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        app = build_app(db_session_factory)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            response = await client.post("/auth/logout", headers={**SAME_ORIGIN, "Accept": "text/html"})
        assert response.status_code == 303 and response.headers["location"] == "/ui/account"

    async def test_a_cross_site_logout_is_refused(self, db_session_factory):
        app = build_app(db_session_factory)
        async with client_for(app) as client:
            response = await client.post("/auth/logout", headers={"Origin": "http://evil.example"})
        assert response.status_code == 403


async def stored(db_session) -> dict[int, bool]:
    db_session.expire_all()
    return dict((await db_session.execute(select(UserRepository.github_repo_id, UserRepository.can_push))).all())


class TestRepositorySync:
    """Access is per repository: what GitHub says the user can reach in each of this App's installations,
    and whether they can push. Read at sign-in."""

    async def sign_in(self, db_session_factory, handler):
        app = build_app(db_session_factory)
        override_github(app, handler)
        async with client_for(app) as client:
            state = await start_login(client)
            return await client.get("/auth/github/callback", params={"code": "c", "state": state})

    async def test_repositories_and_push_are_stored(self, db_session, db_session_factory):
        response = await self.sign_in(db_session_factory, github_handler(installations={11: {1: True, 2: False}, 22: {3: True}}))
        assert response.status_code == 303
        assert await stored(db_session) == {1: True, 2: False, 3: True}

    async def test_the_session_carries_its_issue_time(self, db_session, db_session_factory):
        response = await self.sign_in(db_session_factory, github_handler())
        payload = unsign(response.cookies[SESSION_COOKIE], "session", SESSION_SECRET)
        assert isinstance(payload["iat"], int)

    async def test_a_second_sign_in_replaces_the_set(self, db_session, db_session_factory):
        await self.sign_in(db_session_factory, github_handler(installations={11: {1: True, 2: True}}))
        await self.sign_in(db_session_factory, github_handler(installations={11: {2: False, 3: True}}))
        assert await stored(db_session) == {2: False, 3: True}

    async def test_no_installations_signs_in_with_nothing_visible(self, db_session, db_session_factory):
        response = await self.sign_in(db_session_factory, github_handler(installations={}))
        assert response.status_code == 303 and await stored(db_session) == {}

    async def test_more_than_one_page_is_read(self, db_session, db_session_factory):
        repos = {r: r % 2 == 0 for r in range(1, 251)}
        seen: list = []
        await self.sign_in(db_session_factory, github_handler(installations={11: repos}, seen=seen))
        assert await stored(db_session) == repos
        pages = [r.url.params["page"] for r in seen if r.url.path == "/user/installations/11/repositories"]
        assert pages == ["1", "2", "3"]

    async def test_another_apps_installation_is_skipped(self, db_session, db_session_factory):
        body = {"total_count": 2, "installations": [{"id": 11, "client_id": "client-id"}, {"id": 22, "client_id": "other"}]}
        seen: list = []
        handler = github_handler(installations={"raw": body}, seen=seen)

        def both(request):
            if request.url.path == "/user/installations/11/repositories":
                return httpx.Response(200, json={"total_count": 1, "repositories": [{"id": 5, "permissions": {"push": True}}]})
            return handler(request)

        await self.sign_in(db_session_factory, both)
        assert await stored(db_session) == {5: True}
        assert not any(r.url.path == "/user/installations/22/repositories" for r in seen)

    @pytest.mark.parametrize(
        "body,status",
        [({"message": "Bad credentials"}, 401), ({"installations": "nope"}, 200), ({"installations": [{"id": "x"}]}, 200)],
    )
    async def test_a_github_failure_is_502_and_no_session(self, db_session, db_session_factory, body, status):
        response = await self.sign_in(db_session_factory, github_handler(installations={"raw": body}, status=status))
        assert response.status_code == 502
        assert SESSION_COOKIE not in response.cookies
        assert (await db_session.scalar(select(func.count()).select_from(User))) == 0

    async def test_the_github_token_is_not_stored(self, db_session, db_session_factory):
        await self.sign_in(db_session_factory, github_handler())
        rows = (await db_session.execute(select(UserRepository))).scalars().all()
        assert rows and "gho_fake" not in str([r.__dict__ for r in rows])


class TestNext:
    async def test_login_carries_a_ui_path_to_the_landing(self, db_session, db_session_factory):
        app = build_app(db_session_factory)
        override_github(app, github_handler())
        async with client_for(app) as client:
            login = await client.get("/auth/github/login", params={"next": "/ui/tasks/abc?x=1"})
            state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
            response = await client.get("/auth/github/callback", params={"code": "c", "state": state})
        assert response.headers["location"] == "/ui/tasks/abc?x=1"

    @pytest.mark.parametrize(
        "value",
        [
            "//evil.example", "https://evil.example/ui/", "/ui//evil.example", "/auth/github/login", "/ui/../auth",
            "\\\\evil", "/ui/\nx", "/uix", "x" * 600, "/ui/%2e%2e/github/install", "/ui/%2E%2E/x", "/ui/%2f%2fevil",
            "/ui/%5cevil",
        ],
    )
    async def test_anything_but_a_ui_path_lands_on_the_default(self, value):
        assert safe_next(value) == AFTER_LOGIN
