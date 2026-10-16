import base64
import binascii
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from repolace_shared.db.models import LLMCall, RegisteredRepo, Task, TaskOutcome, TaskStatus
from repolace_shared.github.client import GithubClient
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from repolace_api.deps import get_db, get_github_client
from repolace_api.publisher import TaskPublisher, get_publisher
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
    except ValidationError as exc:
        # A payload that does not parse is GitHub's failure, not the client's.
        log.error("github.api.malformed", repo_id=str(repo_id), error=str(exc))
        raise HTTPException(status_code=502, detail="GitHub API returned an unexpected payload") from exc


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
    publisher: TaskPublisher = Depends(get_publisher),
) -> TaskCreated:
    """Queue a task. Does not run it.

    The pipeline runs in a Celery worker (or `repolace-run-task`); this endpoint
    inserts the row and publishes its id by name. Keeping it that way is what stops the API image from
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
            exists = await _branch_exists(github, repo, target_branch)
        if not exists:
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
    # After the commit, and never fatal: the row is `queued`, which is what the sweeper republishes.
    await publisher.publish(task.id)

    log.info(
        "repos.task.queued",
        repo_id=str(repo_id),
        task_id=str(task.id),
        issue_number=issue.number,
        target_branch=task.target_branch,
    )
    return TaskCreated.model_validate(task, from_attributes=True)


async def _branch_exists(github: GithubClient, repo: RegisteredRepo, name: str) -> bool:
    branches = await github.list_branches(repo.installation_id, repo.owner, repo.name)
    if name in {b.name for b in branches}:
        return True
    if not getattr(branches, "truncated", False):
        return False
    # The listing hit its page cap, so absence from it proves nothing.
    if any(part in ("", ".", "..") for part in name.split("/")):
        return False
    return await github.branch_exists(repo.installation_id, repo.owner, repo.name, name)


def encode_cursor(created_at: datetime, task_id: UUID) -> str:
    raw = f"{created_at.astimezone(UTC).isoformat()}|{task_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, task_id = raw.split("|")
        created_at = datetime.fromisoformat(stamp)
        if created_at.tzinfo is None:
            raise ValueError("naive timestamp")
        return created_at, UUID(task_id)
    except (ValueError, binascii.Error) as exc:  # UnicodeDecodeError is a ValueError
        raise HTTPException(status_code=422, detail="malformed cursor") from exc


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

    # The model of the latest call of each product task. Benchmark tasks are
    # excluded here, not only from the cost pool: a bench sweep on another model
    # must not choose which model this repo's estimate is quoted for.
    latest_call = (
        select(LLMCall.task_id, LLMCall.model)
        .join(Task, Task.id == LLMCall.task_id)
        .where(Task.eval_run_id.is_(None))
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
    cursor: Annotated[str | None, Query(description="Opaque token from a previous page's next_cursor.")] = None,
    include_benchmark: bool = False,
    db: AsyncSession = Depends(get_db),
) -> TaskPage:
    """Newest first. Shows status and outcome separately -- they answer different questions.

    A task can read `pr_opened` and still be unscored: status is how far the
    pipeline got, outcome is whether the issue was actually fixed. Benchmark
    rows are hidden unless asked for.
    """
    await _active_repo(db, repo_id)
    position = decode_cursor(cursor) if cursor is not None else None

    # Not the whole row: patch_diff and issue_body can be large and the list shows neither.
    columns = [getattr(Task, name) for name in TaskListItem.model_fields]
    query = select(Task).options(load_only(*columns)).where(Task.repo_id == repo_id)
    if status is not None:
        query = query.where(Task.status == status)
    if outcome is not None:
        query = query.where(Task.outcome == outcome)
    if not include_benchmark:
        query = query.where(Task.eval_run_id.is_(None))
    if position is not None:
        query = query.where(tuple_(Task.created_at, Task.id) < tuple_(*position))
    # One extra row says whether there is a next page without a COUNT.
    rows = (await db.execute(query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1))).scalars().all()

    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return TaskPage(items=[TaskListItem.model_validate(r, from_attributes=True) for r in page], next_cursor=next_cursor)
