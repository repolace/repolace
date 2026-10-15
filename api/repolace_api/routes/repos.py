from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from repolace_shared.db.models import LLMCall, RegisteredRepo, Task, TaskOutcome, TaskStatus
from repolace_shared.github.client import GithubClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_api.deps import get_db, get_github_client
from repolace_api.schemas import (
    BranchOut,
    CostEstimate,
    IssueOut,
    RepoOut,
    TaskCreated,
    TaskListItem,
    TaskPage,
)

router = APIRouter(prefix="/repos", tags=["repos"])
log = structlog.get_logger()

#: Mirrors `repolace_gateway.budget.DEFAULT_MAX_USD` (the per-task cap). Not
#: imported: the gateway package pulls LiteLLM, which the API image must not
#: carry (see the packaging rule in CLAUDE.md). Change both together.
TASK_COST_CAP_USD = Decimal("2.00")
#: Fewer finished tasks than this is not a distribution worth quoting.
ESTIMATE_MIN_TASKS = 5
ESTIMATE_WINDOW = 30


@contextmanager
def github_errors(repo_id: UUID) -> Iterator[None]:
    """Map a failed GitHub call to the 502 every route here answers with."""
    try:
        yield
    except httpx.HTTPStatusError as exc:
        log.error(
            "github.api.error", repo_id=str(repo_id), status_code=exc.response.status_code, url=str(exc.request.url)
        )
        raise HTTPException(status_code=502, detail="GitHub API error") from exc
    except httpx.RequestError as exc:
        log.error("github.api.unreachable", repo_id=str(repo_id), error=str(exc))
        raise HTTPException(status_code=502, detail="GitHub API unreachable") from exc


async def _active_repo(db: AsyncSession, repo_id: UUID) -> RegisteredRepo:
    repo = await db.get(RegisteredRepo, repo_id)
    if repo is None or not repo.is_active:
        raise HTTPException(status_code=404, detail="repo not found")
    return repo


@router.get("", response_model=list[RepoOut])
async def list_repos(db: AsyncSession = Depends(get_db)) -> list[RepoOut]:
    rows = (await db.execute(select(RegisteredRepo).where(RegisteredRepo.is_active.is_(True)))).scalars().all()
    return [RepoOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/{repo_id}/issues", response_model=list[IssueOut])
async def list_repo_issues(
    repo_id: UUID,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> list[IssueOut]:
    repo = await _active_repo(db, repo_id)
    with github_errors(repo_id):
        issues = await github.list_repo_open_issues(repo.installation_id, repo.owner, repo.name)

    log.info("repos.issues.fetched", repo_id=str(repo_id), count=len(issues))
    return [
        IssueOut(
            number=i.number,
            title=i.title,
            html_url=i.html_url,
            labels=i.label_names,
            author=i.user.login if i.user else None,
            created_at=i.created_at,
        )
        for i in issues
    ]


@router.get("/{repo_id}/branches", response_model=list[BranchOut])
async def list_repo_branches(
    repo_id: UUID,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> list[BranchOut]:
    repo = await _active_repo(db, repo_id)
    with github_errors(repo_id):
        branches = await github.list_branches(repo.installation_id, repo.owner, repo.name)
    return [BranchOut(name=b.name, default=b.name == repo.default_branch) for b in branches]


class CreateTaskRequest(BaseModel):
    """The first Pydantic request model in this codebase.

    A model is worth it for a body with validation: without one, a missing key
    surfaces as a 500 from a KeyError rather than a 422 naming the field.
    """

    issue_number: int = Field(gt=0)
    #: Defaults to the repo's default branch. The PR is opened against this.
    target_branch: str | None = None
    #: Open a PR even if the agent could not make the tests pass.
    open_pr_on_failure: bool = False


@router.post("/{repo_id}/tasks", status_code=201, response_model=TaskCreated)
async def create_task(
    repo_id: UUID,
    body: CreateTaskRequest,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> TaskCreated:
    """Queue a task. Does not run it.

    The pipeline is a separate process (`repolace-run-task`); this endpoint only
    inserts the row. Keeping it that way is what stops the API image from
    needing the retrieval stack and torch -- see the packaging rule in
    CLAUDE.md.
    """
    repo = await _active_repo(db, repo_id)

    target_branch = body.target_branch if body.target_branch is not None else repo.default_branch
    if not target_branch.strip():
        raise HTTPException(status_code=400, detail="target_branch must not be empty")
    target_branch = target_branch.strip()

    # The PR is opened against this branch much later, in another process, so a
    # typo would otherwise surface as a failed task minutes in. The default
    # branch needs no check: GitHub reported it when the repo was registered.
    if body.target_branch is not None:
        with github_errors(repo_id):
            branches = await github.list_branches(repo.installation_id, repo.owner, repo.name)
        if target_branch not in {b.name for b in branches}:
            raise HTTPException(
                status_code=422, detail=f"target_branch {target_branch!r} is not a branch of {repo.full_name}"
            )

    # issue_title and issue_url are NOT NULL and end up in a real PR body, so
    # they are resolved from GitHub rather than taken from the client.
    with github_errors(repo_id):
        issues = await github.list_repo_open_issues(repo.installation_id, repo.owner, repo.name)

    issue = next((i for i in issues if i.number == body.issue_number), None)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found or not open")

    task = Task(
        repo_id=repo.id,
        issue_number=issue.number,
        issue_title=issue.title,
        issue_body=issue.body,
        issue_url=issue.html_url,
        target_branch=target_branch,
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
    return TaskCreated.model_validate(task, from_attributes=True)


def _usd(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 4)))


@router.get("/{repo_id}/tasks/estimate", response_model=CostEstimate)
async def estimate_task_cost(repo_id: UUID, db: AsyncSession = Depends(get_db)) -> CostEstimate:
    """p50 and p90 of what a task on this repo has cost, with the basis named.

    1. `repo`: the last 30 priced, non-benchmark tasks of this repo, if there
       are at least 5.
    2. `model`: otherwise every priced, non-benchmark task, on any repo, whose
       most recent `llm_calls` row used the same model as this repo's most
       recent task that has one.
    3. `none`: otherwise no numbers, only the per-task cap as an upper bound.
    """
    await _active_repo(db, repo_id)

    async def percentiles(where: list, limit: int | None) -> tuple[int, Decimal | None, Decimal | None]:
        costs = select(Task.cost_usd).where(Task.cost_usd.is_not(None), Task.eval_run_id.is_(None), *where)
        if limit is not None:
            costs = costs.order_by(Task.created_at.desc()).limit(limit)
        sub = costs.subquery()
        row = (
            await db.execute(
                select(
                    func.count(sub.c.cost_usd),
                    func.percentile_cont(0.5).within_group(sub.c.cost_usd),
                    func.percentile_cont(0.9).within_group(sub.c.cost_usd),
                )
            )
        ).one()
        return row[0], _usd(row[1]), _usd(row[2])

    n, p50, p90 = await percentiles([Task.repo_id == repo_id], ESTIMATE_WINDOW)
    if n >= ESTIMATE_MIN_TASKS:
        return CostEstimate(basis="repo", n=n, p50_usd=p50, p90_usd=p90, upper_bound_usd=TASK_COST_CAP_USD)

    # The model of the latest call of each task.
    latest_call = (
        select(LLMCall.task_id, LLMCall.model)
        .distinct(LLMCall.task_id)
        .order_by(LLMCall.task_id, LLMCall.created_at.desc())
        .subquery()
    )
    model = (
        await db.execute(
            select(latest_call.c.model)
            .join(Task, Task.id == latest_call.c.task_id)
            .where(Task.repo_id == repo_id)
            .order_by(Task.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if model is not None:
        same_model = select(latest_call.c.task_id).where(latest_call.c.model == model)
        n, p50, p90 = await percentiles([Task.id.in_(same_model)], None)
        if n >= ESTIMATE_MIN_TASKS:
            return CostEstimate(basis="model", n=n, p50_usd=p50, p90_usd=p90, upper_bound_usd=TASK_COST_CAP_USD)

    return CostEstimate(basis="none", n=n, p50_usd=None, p90_usd=None, upper_bound_usd=TASK_COST_CAP_USD)


@router.get("/{repo_id}/tasks", response_model=TaskPage)
async def list_repo_tasks(
    repo_id: UUID,
    status: TaskStatus | None = None,
    outcome: TaskOutcome | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[datetime | None, Query(description="Keyset cursor: only tasks created strictly before this.")] = None,
    include_benchmark: bool = False,
    db: AsyncSession = Depends(get_db),
) -> TaskPage:
    """Newest first. Shows status and outcome separately -- they answer different questions.

    A task can read `pr_opened` and still be unscored: status is how far the
    pipeline got, outcome is whether the issue was actually fixed. Benchmark
    rows are hidden unless asked for.
    """
    await _active_repo(db, repo_id)
    if before is not None and before.tzinfo is None:
        raise HTTPException(status_code=422, detail="before must carry a timezone offset")

    query = select(Task).where(Task.repo_id == repo_id)
    if status is not None:
        query = query.where(Task.status == status)
    if outcome is not None:
        query = query.where(Task.outcome == outcome)
    if not include_benchmark:
        query = query.where(Task.eval_run_id.is_(None))
    if before is not None:
        query = query.where(Task.created_at < before)
    # One extra row says whether there is a next page without a COUNT.
    rows = (await db.execute(query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1))).scalars().all()

    page = rows[:limit]
    next_before = page[-1].created_at if len(rows) > limit else None
    return TaskPage(items=[TaskListItem.model_validate(r, from_attributes=True) for r in page], next_before=next_before)
