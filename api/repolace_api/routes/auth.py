"""Sign in with GitHub (the GitHub App's user-authorization flow) and a signed session cookie.

What this is for: attaching a Stripe customer and a plan to a person. It does not protect the JSON
API or the task UI, which still have no authentication (see `.claude/rules/api-and-ui.md`).

The GitHub token is used once, to read `/user`, and is never stored or logged. Neither is the code.
"""

import secrets
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from repolace_shared.db.models import User
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
AFTER_LOGIN = "/ui/account"


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
def login(settings: BillingSettings = Depends(require_signin_ready)) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    query = urlencode(
        {"client_id": settings.github_oauth_client_id, "redirect_uri": _callback_url(settings), "state": state}
    )
    response = RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=302)
    # SameSite=Lax, not Strict: the browser comes back from github.com on a top-level GET, and a
    # Strict cookie would not be sent on it.
    response.set_cookie(
        STATE_COOKIE,
        sign("oauth-state", {"s": state}, settings.session_secret, max_age=STATE_MAX_AGE_SECONDS),
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

    profile = await _fetch_profile(http, settings, code)

    user_id = await _upsert_user(db, profile)
    log.info("auth.signed_in", user_id=str(user_id))

    response = RedirectResponse(AFTER_LOGIN, status_code=303)
    response.delete_cookie(STATE_COOKIE, path="/auth")
    response.set_cookie(
        SESSION_COOKIE,
        sign("session", {"uid": str(user_id)}, settings.session_secret, max_age=SESSION_MAX_AGE_SECONDS),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


async def _fetch_profile(http: httpx.AsyncClient, settings: BillingSettings, code: str) -> dict:
    """Exchange the code for a token and read `/user`. The token lives in this function only."""
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
    return profile


async def _upsert_user(db: AsyncSession, profile: dict):
    email = profile.get("email") if isinstance(profile.get("email"), str) else None
    stmt = insert(User).values(github_user_id=profile["id"], github_login=profile["login"], email=email)
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.github_user_id],
        # A login can be renamed; GitHub hides the email when it is private, so a null never
        # erases one we already have.
        set_={"github_login": stmt.excluded.github_login, "email": stmt.excluded.email}
        if email
        else {"github_login": stmt.excluded.github_login},
    ).returning(User.id)
    user_id = (await db.execute(stmt)).scalar_one()
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
