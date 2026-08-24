from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_github_client
from repolace_shared.db.models import RegisteredRepo
from repolace_shared.github.client import GithubClient

router = APIRouter(prefix="/repos", tags=["repos"])
log = structlog.get_logger()


@router.get("")
async def list_repos(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(RegisteredRepo).where(RegisteredRepo.is_active.is_(True)))).scalars().all()
    return [
        {
            "id": str(row.id),
            "owner": row.owner,
            "name": row.name,
            "full_name": row.full_name,
            "default_branch": row.default_branch,
            "private": row.private,
        }
        for row in rows
    ]


@router.get("/{repo_id}/issues")
async def list_repo_issues(
    repo_id: UUID,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> list[dict]:
    repo = await db.get(RegisteredRepo, repo_id)
    if repo is None or not repo.is_active:
        raise HTTPException(status_code=404, detail="repo not found")

    try:
        issues = await github.list_repo_open_issues(repo.installation_id, repo.owner, repo.name)
    except httpx.HTTPStatusError as exc:
        log.error(
            "github.api.error", repo_id=str(repo_id), status_code=exc.response.status_code, url=str(exc.request.url)
        )
        raise HTTPException(status_code=502, detail="GitHub API error") from exc
    except httpx.RequestError as exc:
        log.error("github.api.unreachable", repo_id=str(repo_id), error=str(exc))
        raise HTTPException(status_code=502, detail="GitHub API unreachable") from exc

    log.info("repos.issues.fetched", repo_id=str(repo_id), count=len(issues))
    return [{"number": issue.number, "title": issue.title, "html_url": issue.html_url} for issue in issues]
