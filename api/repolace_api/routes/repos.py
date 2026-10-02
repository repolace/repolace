from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_api.deps import get_db, get_github_client
from repolace_shared.db.models import RegisteredRepo, Task, TaskStatus
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


class CreateTaskRequest(BaseModel):
    """The first Pydantic request model in this codebase.

    Every other route here returns raw dicts and takes no body. A model is worth
    it for a body with validation: without one, a missing key surfaces as a 500
    from a KeyError rather than a 422 naming the field.
    """

    issue_number: int = Field(gt=0)
    #: Defaults to the repo's default branch. The PR is opened against this.
    target_branch: str | None = None
    #: Open a PR even if the agent could not make the tests pass.
    open_pr_on_failure: bool = False


@router.post("/{repo_id}/tasks", status_code=201)
async def create_task(
    repo_id: UUID,
    body: CreateTaskRequest,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> dict:
    """Queue a task. Does not run it.

    The pipeline is a separate process (`repolace-run-task`); this endpoint only
    inserts the row. Keeping it that way is what stops the API image from
    needing the retrieval stack and torch -- see the packaging rule in
    CLAUDE.md.
    """
    repo = await db.get(RegisteredRepo, repo_id)
    if repo is None or not repo.is_active:
        raise HTTPException(status_code=404, detail="repo not found")

    target_branch = body.target_branch if body.target_branch is not None else repo.default_branch
    if not target_branch.strip():
        raise HTTPException(status_code=400, detail="target_branch must not be empty")

    # issue_title and issue_url are NOT NULL and end up in a real PR body, so
    # they are resolved from GitHub rather than taken from the client.
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

    issue = next((i for i in issues if i.number == body.issue_number), None)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found or not open")

    task = Task(
        repo_id=repo.id,
        issue_number=issue.number,
        issue_title=issue.title,
        issue_body=issue.body,
        issue_url=issue.html_url,
        target_branch=target_branch.strip(),
        status=TaskStatus.QUEUED,
        open_pr_on_failure=body.open_pr_on_failure,
    )
    db.add(task)
    await db.commit()

    log.info(
        "repos.task.queued",
        repo_id=str(repo_id),
        task_id=str(task.id),
        issue_number=issue.number,
        target_branch=task.target_branch,
    )
    return {
        "id": str(task.id),
        "repo_id": str(task.repo_id),
        "issue_number": task.issue_number,
        "issue_title": task.issue_title,
        "target_branch": task.target_branch,
        "status": task.status.value,
        "open_pr_on_failure": task.open_pr_on_failure,
    }


@router.get("/{repo_id}/tasks")
async def list_repo_tasks(repo_id: UUID, db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Newest first. Shows status and outcome separately -- they answer different questions.

    A task can read `pr_opened` and still be unscored: status is how far the
    pipeline got, outcome is whether the issue was actually fixed.
    """
    rows = (
        (
            await db.execute(
                select(Task).where(Task.repo_id == repo_id).order_by(Task.created_at.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(row.id),
            "issue_number": row.issue_number,
            "target_branch": row.target_branch,
            "status": row.status.value,
            "outcome": row.outcome.value if row.outcome else None,
            "pr_url": row.pr_url,
            "error_message": row.error_message,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
