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
from repolace_api.routes.auth import STATE_COOKIE
from repolace_api.sessions import unsign
from repolace_shared.db.models import User
from sqlalchemy import func, select

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def github_handler(profile=None, token_ok=True, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/login/oauth/access_token":
            if not token_ok:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "gho_fake", "token_type": "bearer"})
        assert request.headers["authorization"] == "Bearer gho_fake"
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
        assert response.status_code == 303 and response.headers["location"] == "/ui/account"
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
