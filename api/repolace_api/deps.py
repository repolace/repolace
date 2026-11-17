import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from repolace_shared.db.models import User, UserRepository
from repolace_shared.github.client import GithubClient
from sqlalchemy import select
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


def _session(request: Request, settings: BillingSettings) -> dict | None:
    """The verified session cookie payload, or None (no secret configured, no cookie, forged, expired)."""
    if not settings.session_secret:
        return None
    return unsign(request.cookies.get(SESSION_COOKIE), "session", settings.session_secret)


async def _user_of(db: AsyncSession, payload: dict | None) -> User | None:
    if payload is None:
        return None
    try:
        user_id = uuid.UUID(str(payload.get("uid")))
    except ValueError:
        return None
    return await db.get(User, user_id)


async def optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: BillingSettings = Depends(get_billing_settings),
) -> User | None:
    """The signed-in user, or None: no cookie, a forged or expired one, or a user that was deleted.

    For the account and billing routes, which a 7-day session may use. Repo and task routes use
    `require_viewer`, which also requires the session to be fresh."""
    return await _user_of(db, _session(request, settings))


async def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return user


# --- access ----------------------------------------------------------------------------------

#: How long a session's access snapshot is trusted on repo and task routes, counted from the sign-in
#: that issued the session (its signed `iat`). GitHub's user tokens expire after 8 hours too. After it
#: the user signs in again (GitHub re-approves without a prompt), which re-reads the snapshot; a copied
#: older cookie is not revived by a later sign-in elsewhere, because its own `iat` stays old.
ACCESS_MAX_AGE = timedelta(hours=8)


@dataclass(frozen=True)
class Viewer:
    """Who is asking, and which repositories (GitHub repository ids) they may see and start tasks on.

    `repo_ids` None means every repository: only `DEV_NO_AUTH` (and tests) produce that, and then
    `push_repo_ids` is None too. A task is visible when its repository is.
    """

    user: User | None
    repo_ids: frozenset[int] | None
    push_repo_ids: frozenset[int] | None

    def can_see(self, github_repo_id: int) -> bool:
        return self.repo_ids is None or github_repo_id in self.repo_ids

    def can_push(self, github_repo_id: int) -> bool:
        return self.push_repo_ids is None or github_repo_id in self.push_repo_ids


#: Everything visible and startable. `DEV_NO_AUTH` on a loopback-only install, and the test apps.
ALL_ACCESS = Viewer(user=None, repo_ids=None, push_repo_ids=None)


async def require_viewer(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: BillingSettings = Depends(get_billing_settings),
) -> Viewer:
    """Every repo and task route depends on this. 401 without a valid session issued within
    `ACCESS_MAX_AGE`. The `/ui` error handler turns that 401 into a redirect to sign-in."""
    if settings.dev_no_auth:
        return ALL_ACCESS
    payload = _session(request, settings)
    user = await _user_of(db, payload)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    issued = payload.get("iat") if payload else None
    if not isinstance(issued, int) or datetime.now(UTC) - datetime.fromtimestamp(issued, UTC) > ACCESS_MAX_AGE:
        raise HTTPException(status_code=401, detail="sign in again to refresh access")
    rows = (
        await db.execute(
            select(UserRepository.github_repo_id, UserRepository.can_push).where(UserRepository.user_id == user.id)
        )
    ).all()
    return Viewer(
        user=user,
        repo_ids=frozenset(r.github_repo_id for r in rows),
        push_repo_ids=frozenset(r.github_repo_id for r in rows if r.can_push),
    )


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
