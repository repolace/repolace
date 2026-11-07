import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request
from repolace_shared.db.models import User
from repolace_shared.github.client import GithubClient
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_api.config import BillingSettings, get_billing_settings
from repolace_api.sessions import unsign


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_github_client(request: Request) -> GithubClient:
    return request.app.state.github_client


# --- sign-in ---------------------------------------------------------------------------------

SESSION_COOKIE = "repolace_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600


async def optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: BillingSettings = Depends(get_billing_settings),
) -> User | None:
    """The signed-in user, or None: no cookie, a forged or expired one, or a user that was deleted."""
    if not settings.session_secret:
        return None
    payload = unsign(request.cookies.get(SESSION_COOKIE), "session", settings.session_secret)
    if payload is None:
        return None
    try:
        user_id = uuid.UUID(str(payload.get("uid")))
    except ValueError:
        return None
    return await db.get(User, user_id)


async def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return user


def require_same_origin(request: Request) -> None:
    """The cross-site-forgery guard the UI's enqueue POST uses, for the cookie-authenticated POSTs.

    Imported inside the function: `repolace_api.ui` imports the routes, which import this module.
    """
    from repolace_api.ui.security import same_origin

    if not same_origin(request):
        raise HTTPException(status_code=403, detail="cross-site request refused")


def wants_html(request: Request) -> bool:
    """A browser form post asks for HTML first; a script or a test client asks for anything."""
    return "text/html" in request.headers.get("accept", "").split(",")[0]
