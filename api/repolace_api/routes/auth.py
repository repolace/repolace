"""Sign in with GitHub (the GitHub App's user-authorization flow) and a signed session cookie.

Sign-in is what every repo and task route requires (`deps.require_viewer`), and it is where access
is decided: the repositories the user can access through this App's installations, and whether they
can push to each, are read here and stored in `user_repositories`. See "Access" in
`.claude/rules/api-and-ui.md`.

The GitHub token is used for these reads (`/user`, `/user/installations`, and each installation's
`/repositories`) inside one function, and is never stored or logged. Neither is the code: the
access log line of the callback is redacted (`main.RedactOAuthCallback`).
"""

import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import unquote, urlencode, urlsplit

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from repolace_shared.db.models import User, UserRepository
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_api.config import BillingSettings, get_billing_settings
from repolace_api.deps import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    get_db,
    require_same_origin,
    wants_html,
)
from repolace_api.sessions import sign, unsign

router = APIRouter(prefix="/auth", tags=["auth"])
log = structlog.get_logger()

STATE_COOKIE = "repolace_oauth_state"
STATE_MAX_AGE_SECONDS = 600
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
INSTALLATIONS_URL = "https://api.github.com/user/installations"
#: 100 per page, for the installation list and for each installation's repositories. Past 1,000 of
#: either the list is cut short (logged), which can only hide repositories, never show extra ones.
MAX_PAGES = 10
#: The GitHub repository permissions that allow starting a task (it pushes a branch and opens a PR).
PUSH_PERMISSIONS = ("push", "maintain", "admin")
AFTER_LOGIN = "/ui/repos"
MAX_NEXT_LENGTH = 512


def safe_next(value: str | None) -> str:
    """Where to land after sign-in: a `/ui/` path on this site, or the default. Never another origin."""
    if not value or len(value) > MAX_NEXT_LENGTH or "\\" in value or any(ord(c) < 32 for c in value):
        return AFTER_LOGIN
    parts = urlsplit(value)
    # Checked decoded too: a browser resolves `%2e%2e` as a dot segment, so `/ui/%2e%2e/x` would leave `/ui/`.
    paths = (parts.path, unquote(parts.path))
    if parts.scheme or parts.netloc or any(not p.startswith("/ui/") or "//" in p or ".." in p or "\\" in p for p in paths):
        return AFTER_LOGIN
    return value


async def get_oauth_http() -> AsyncIterator[httpx.AsyncClient]:
    """The HTTP client for GitHub's OAuth endpoints; tests override it with a mock transport."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        yield client


def require_signin_ready(settings: BillingSettings = Depends(get_billing_settings)) -> BillingSettings:
    if not settings.signin_ready:
        raise HTTPException(status_code=503, detail="sign-in is not configured")
    return settings


def _callback_url(settings: BillingSettings) -> str:
    return settings.api_base_url.rstrip("/") + "/auth/github/callback"


@router.get("/github/login")
def login(next: str | None = None, settings: BillingSettings = Depends(require_signin_ready)) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    query = urlencode(
        {"client_id": settings.github_oauth_client_id, "redirect_uri": _callback_url(settings), "state": state}
    )
    response = RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=302)
    # SameSite=Lax, not Strict: the browser comes back from github.com on a top-level GET, and a
    # Strict cookie would not be sent on it.
    response.set_cookie(
        STATE_COOKIE,
        sign("oauth-state", {"s": state, "n": safe_next(next)}, settings.session_secret, max_age=STATE_MAX_AGE_SECONDS),
        max_age=STATE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/auth",
    )
    return response


@router.get("/github/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    settings: BillingSettings = Depends(require_signin_ready),
    db: AsyncSession = Depends(get_db),
    http: httpx.AsyncClient = Depends(get_oauth_http),
) -> RedirectResponse:
    expected = unsign(request.cookies.get(STATE_COOKIE), "oauth-state", settings.session_secret)
    # Constant-time compare; a missing cookie, a forged or expired one and a different value are
    # all the same 400, so the response says nothing about which check failed.
    if (
        expected is None
        or not state
        or not secrets.compare_digest(str(expected.get("s", "")).encode(), state.encode())
    ):
        raise HTTPException(status_code=400, detail="invalid sign-in state")
    if error or not code:
        raise HTTPException(status_code=400, detail="sign-in was not completed")

    profile, repositories = await _fetch_profile(http, settings, code)

    user_id = await _upsert_user(db, profile, repositories)
    log.info("auth.signed_in", user_id=str(user_id), repositories=len(repositories))

    response = RedirectResponse(safe_next(expected.get("n")), status_code=303)
    response.delete_cookie(STATE_COOKIE, path="/auth")
    response.set_cookie(
        SESSION_COOKIE,
        # `iat` dates this session's access snapshot: repo and task routes refuse it after
        # `deps.ACCESS_MAX_AGE`, so an old copied cookie is not revived by a later sign-in elsewhere.
        sign("session", {"uid": str(user_id), "iat": int(time.time())}, settings.session_secret, max_age=SESSION_MAX_AGE_SECONDS),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


async def _fetch_profile(http: httpx.AsyncClient, settings: BillingSettings, code: str) -> tuple[dict, dict[int, bool]]:
    """Exchange the code for a token, read `/user` and the repositories the user can access (with
    whether they can push). The token lives in this function only."""
    try:
        token_response = await http.post(
            TOKEN_URL,
            data={
                "client_id": settings.github_oauth_client_id,
                "client_secret": settings.github_oauth_client_secret,
                "code": code,
                "redirect_uri": _callback_url(settings),
            },
            headers={"Accept": "application/json"},
        )
        token_body = token_response.json()
    except (httpx.HTTPError, ValueError):
        log.warning("auth.github_unreachable", step="token")
        raise HTTPException(status_code=502, detail="GitHub could not be reached") from None
    token = token_body.get("access_token") if isinstance(token_body, dict) else None
    if token_response.status_code != 200 or not isinstance(token, str) or not token:
        # `bad_verification_code` and friends: the user's code, not our fault. No body is logged.
        raise HTTPException(status_code=400, detail="GitHub refused the sign-in code")

    try:
        user_response = await http.get(
            USER_URL, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        )
        profile = user_response.json()
    except (httpx.HTTPError, ValueError):
        log.warning("auth.github_unreachable", step="user")
        raise HTTPException(status_code=502, detail="GitHub could not be reached") from None
    if (
        user_response.status_code != 200
        or not isinstance(profile, dict)
        or not isinstance(profile.get("id"), int)
        or not isinstance(profile.get("login"), str)
    ):
        raise HTTPException(status_code=502, detail="GitHub returned an unexpected profile")
    return profile, await _fetch_repositories(http, settings, token)


async def _pages(http: httpx.AsyncClient, url: str, key: str, headers: dict[str, str]) -> list[dict]:
    """Every item of a paged GitHub list (`{key: [...]}`), up to `MAX_PAGES` pages. Fails closed: a
    GitHub error or an unexpected shape is a 502, so no session is issued on a partial or guessed list."""
    found: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            response = await http.get(url, params={"per_page": 100, "page": page}, headers=headers)
            body = response.json()
        except (httpx.HTTPError, ValueError):
            log.warning("auth.github_unreachable", step=key)
            raise HTTPException(status_code=502, detail="GitHub could not be reached") from None
        items = body.get(key) if isinstance(body, dict) else None
        if response.status_code != 200 or not isinstance(items, list):
            raise HTTPException(status_code=502, detail=f"GitHub returned an unexpected {key} list")
        if not all(isinstance(item, dict) and isinstance(item.get("id"), int) for item in items):
            raise HTTPException(status_code=502, detail=f"GitHub returned an unexpected {key} list")
        found.extend(items)
        if len(items) < 100:
            return found
    log.warning("auth.list_truncated", list=key, pages=MAX_PAGES)
    return found


async def _fetch_repositories(http: httpx.AsyncClient, settings: BillingSettings, token: str) -> dict[int, bool]:
    """GitHub repository id -> can push, for every repository the user can access through this App.

    Per repository, not per installation: `GET /user/installations` lists an installation for a user
    who can read any one of its repositories, so each installation's own list
    (`GET /user/installations/{id}/repositories`, the repositories this user can access, with their
    permissions) is what decides.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    repositories: dict[int, bool] = {}
    for installation in await _pages(http, INSTALLATIONS_URL, "installations", headers):
        # The endpoint lists only this App's installations; the client id check is a second guard.
        if installation.get("client_id") not in (None, settings.github_oauth_client_id):
            continue
        url = f"{INSTALLATIONS_URL}/{installation['id']}/repositories"
        for repo in await _pages(http, url, "repositories", headers):
            permissions = repo.get("permissions") if isinstance(repo.get("permissions"), dict) else {}
            can_push = any(permissions.get(name) is True for name in PUSH_PERMISSIONS)
            repositories[repo["id"]] = repositories.get(repo["id"], False) or can_push
    return repositories


async def _upsert_user(db: AsyncSession, profile: dict, repositories: dict[int, bool]):
    """The user row and their repository snapshot, in one transaction: the old snapshot is replaced."""
    now = datetime.now(UTC)
    email = profile.get("email") if isinstance(profile.get("email"), str) else None
    stmt = insert(User).values(github_user_id=profile["id"], github_login=profile["login"], email=email)
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.github_user_id],
        # A login can be renamed; GitHub hides the email when it is private, so a null never
        # erases one we already have.
        set_={"github_login": stmt.excluded.github_login, **({"email": stmt.excluded.email} if email else {})},
    ).returning(User.id)
    user_id = (await db.execute(stmt)).scalar_one()
    await db.execute(delete(UserRepository).where(UserRepository.user_id == user_id))
    if repositories:
        await db.execute(
            insert(UserRepository),
            [
                {"user_id": user_id, "github_repo_id": repo_id, "can_push": can_push, "synced_at": now}
                for repo_id, can_push in sorted(repositories.items())
            ],
        )
    await db.commit()
    return user_id


@router.post("/logout")
async def logout(request: Request) -> Response:
    require_same_origin(request)
    if wants_html(request):
        response: Response = RedirectResponse("/ui/account", status_code=303)
    else:
        response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
