import hashlib
import hmac
import json
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ApiSettings, get_settings
from app.deps import get_db, get_github_client
from repolace_shared.db.models import GithubInstallation, RegisteredRepo
from repolace_shared.github.client import GithubClient
from repolace_shared.github.schemas import Repository

router = APIRouter(prefix="/github", tags=["github"])
log = structlog.get_logger()


@router.get("/install")
def install(settings: ApiSettings = Depends(get_settings)) -> RedirectResponse:
    return RedirectResponse(f"https://github.com/apps/{settings.github_app_slug}/installations/new")


@router.get("/callback")
async def callback(
    installation_id: int,
    setup_action: str,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> dict[str, str]:
    log.info("github.callback", installation_id=installation_id, setup_action=setup_action)
    if setup_action in ("install", "update"):
        await _sync_installation(installation_id, db, github)
    return {"status": "ok"}


@router.post("/webhook")
async def webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
    settings: ApiSettings = Depends(get_settings),
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, str]:
    body = await request.body()
    _verify_signature(body, x_hub_signature_256, settings.github_app_webhook_secret)
    payload = json.loads(body)

    log.info("github.webhook", event=x_github_event, action=payload.get("action"))

    if x_github_event == "installation":
        await _handle_installation_event(payload, db, github)
    elif x_github_event == "installation_repositories":
        await _handle_installation_repositories_event(payload, db, github)
    else:
        log.info("github.webhook.ignored", event=x_github_event)

    return {"status": "ok"}


def _verify_signature(body: bytes, signature: str | None, secret: str) -> None:
    if signature is None:
        raise HTTPException(status_code=401, detail="missing signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="invalid signature")


async def _handle_installation_event(payload: dict, db: AsyncSession, github: GithubClient) -> None:
    action = payload["action"]
    installation_id = payload["installation"]["id"]

    if action in ("created", "unsuspend"):
        await _sync_installation(installation_id, db, github)
        return

    if action == "suspend":
        installation = await db.get(GithubInstallation, installation_id)
        if installation is not None:
            installation.suspended_at = datetime.now(UTC)

    if action in ("suspend", "deleted"):
        await _deactivate_all_repos(installation_id, db)
        await db.commit()
    else:
        log.info("github.webhook.installation.ignored", action=action)


async def _handle_installation_repositories_event(payload: dict, db: AsyncSession, github: GithubClient) -> None:
    installation_id = payload["installation"]["id"]
    await _sync_installation(installation_id, db, github)


async def _sync_installation(installation_id: int, db: AsyncSession, github: GithubClient) -> None:
    installation = await github.get_installation(installation_id)
    await db.merge(
        GithubInstallation(
            id=installation.id,
            account_login=installation.account.login,
            account_id=installation.account.id,
            account_type=installation.account.type,
        )
    )

    repos = await github.list_installation_repos(installation_id)
    await _upsert_repos(installation_id, repos, db)
    await db.commit()
    log.info("github.installation.synced", installation_id=installation_id, repo_count=len(repos))


async def _upsert_repos(installation_id: int, repos: list[Repository], db: AsyncSession) -> None:
    existing_rows = (
        (await db.execute(select(RegisteredRepo).where(RegisteredRepo.installation_id == installation_id)))
        .scalars()
        .all()
    )
    existing_by_repo_id = {row.github_repo_id: row for row in existing_rows}

    seen_repo_ids: set[int] = set()
    for repo in repos:
        seen_repo_ids.add(repo.id)
        row = existing_by_repo_id.get(repo.id)
        if row is None:
            db.add(
                RegisteredRepo(
                    installation_id=installation_id,
                    github_repo_id=repo.id,
                    owner=repo.owner.login,
                    name=repo.name,
                    full_name=repo.full_name,
                    default_branch=repo.default_branch,
                    private=repo.private,
                    is_active=True,
                )
            )
        else:
            row.owner = repo.owner.login
            row.name = repo.name
            row.full_name = repo.full_name
            row.default_branch = repo.default_branch
            row.private = repo.private
            row.is_active = True

    for row in existing_rows:
        if row.github_repo_id not in seen_repo_ids:
            row.is_active = False


async def _deactivate_all_repos(installation_id: int, db: AsyncSession) -> None:
    await db.execute(
        update(RegisteredRepo).where(RegisteredRepo.installation_id == installation_id).values(is_active=False)
    )
