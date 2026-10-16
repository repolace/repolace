"""HTML routes. Thin: each calls the query function the JSON route calls, then renders.

There is no HTTP round trip to our own API and no second copy of a query, so a
change to a JSON response cannot leave the pages behind.
"""

import time
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError
from repolace_shared.db.models import AGENT_STOP_REASONS, Task, TaskOutcome, TaskStatus
from repolace_shared.github.client import GithubClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from repolace_api.deps import get_db, get_github_client
from repolace_api.publisher import TaskPublisher, get_publisher
from repolace_api.routes.repos import (
    _LIST_COLUMNS,
    TASK_COST_CAP_USD,
    CreateTaskRequest,
    active_repo,
    compute_estimate,
    enqueue_task,
    fetch_branches,
    fetch_issues,
    list_active_repos,
    list_item,
    query_task_page,
)
from repolace_api.routes.tasks import load_task_detail
from repolace_api.schemas import IssueOut
from repolace_api.ui import format as fmt
from repolace_api.ui.security import same_origin
from repolace_api.ui.views import (
    OUTCOME_FILTERS,
    POLL,
    UNSCORED,
    build_detail,
    build_rows,
    estimate_view,
)

log = structlog.get_logger()
router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
#: How long one repo's open-issue list is reused. The picker filters this list
#: locally, so typing does not cost a GitHub call per keystroke.
ISSUE_CACHE_SECONDS = 60.0
#: The picker shows at most this many matches, and says so.
ISSUE_LIST_LIMIT = 100
MAX_FORM_BYTES = 16_384

env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=True,  # every template is HTML; there is no `|safe` on stored text anywhere
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
env.globals.update(
    statuses=[s.value for s in TaskStatus],
    outcomes=[o.value for o in TaskOutcome],
    outcome_filters=OUTCOME_FILTERS,
    unscored=UNSCORED,
    poll=POLL,
    cap=TASK_COST_CAP_USD,
    stop_gloss=fmt.STOP_REASON_GLOSS,
    stop_reasons=AGENT_STOP_REASONS,
    money=fmt.money,
    money4=fmt.money4,
    duration=fmt.duration,
    relative=fmt.relative,
    absolute=fmt.absolute,
    short_id=fmt.short_id,
    is_https=fmt.is_https,
)



def render(name: str, status_code: int = 200, headers: dict[str, str] | None = None, **ctx: Any) -> HTMLResponse:
    html = env.get_template(name).render(now=datetime.now(UTC), **ctx)
    return HTMLResponse(html, status_code=status_code, headers=headers)


def error_page(status_code: int, message: str) -> HTMLResponse:
    return render("error.html", status_code=status_code, code=status_code, message=message)


def _parse_enum[E: Enum](value: str | None, enum: type[E], label: str) -> E | None:
    if value is None or value in ("", "all"):
        return None
    try:
        return enum(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"unknown {label}") from None


class Filters:
    """The list page's query string: parsed once, and rebuilt into links from here."""

    def __init__(self, status: str | None, outcome: str | None, benchmark: str | None):
        self.status = _parse_enum(status, TaskStatus, "status")
        self.unscored = outcome == UNSCORED
        self.outcome = None if self.unscored else _parse_enum(outcome, TaskOutcome, "outcome")
        self.benchmark = benchmark in ("1", "true", "on")

    @property
    def outcome_value(self) -> str:
        return UNSCORED if self.unscored else (self.outcome.value if self.outcome else "all")

    @property
    def active(self) -> bool:
        return self.status is not None or self.outcome_value != "all"

    def query(self, **overrides: str | None) -> str:
        params = {
            "status": self.status.value if self.status else None,
            "outcome": None if self.outcome_value == "all" else self.outcome_value,
            "benchmark": "1" if self.benchmark else None,
        }
        params.update(overrides)
        return urlencode({k: v for k, v in params.items() if v})


# --------------------------------------------------------------------------- pages


@router.get("/repos", response_class=HTMLResponse)
async def repos_page(db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return render("repos.html", repos=await list_active_repos(db))


@router.get("/repos/{repo_id}/tasks", response_class=HTMLResponse)
async def tasks_page(
    repo_id: UUID,
    status: str | None = None,
    outcome: str | None = None,
    benchmark: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    filters = Filters(status, outcome, benchmark)
    repo = await active_repo(db, repo_id)
    page = await _page(db, repo_id, filters, cursor=None)
    rows = await build_rows(db, page.items)
    return render(
        "tasks.html",
        repo=repo,
        rows=rows,
        next_cursor=page.next_cursor,
        filters=filters,
        running_count=sum(1 for r in rows if r.item.status is TaskStatus.RUNNING),
    )


@router.get("/repos/{repo_id}/tasks/more", response_class=HTMLResponse)
async def tasks_more(
    repo_id: UUID,
    cursor: str,
    status: str | None = None,
    outcome: str | None = None,
    benchmark: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    filters = Filters(status, outcome, benchmark)
    repo = await active_repo(db, repo_id)
    page = await _page(db, repo_id, filters, cursor=cursor)
    return render(
        "_task_rows.html", repo=repo, rows=await build_rows(db, page.items), next_cursor=page.next_cursor, filters=filters
    )


async def _page(db: AsyncSession, repo_id: UUID, filters: Filters, cursor: str | None):
    return await query_task_page(
        db,
        repo_id,
        status=filters.status,
        outcome=filters.outcome,
        unscored=filters.unscored,
        cursor=cursor,
        include_benchmark=filters.benchmark,
    )


@router.get("/tasks/{task_id}/row", response_class=HTMLResponse)
async def task_row(task_id: UUID, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    columns = [getattr(Task, name) for name in _LIST_COLUMNS]
    task = (await db.execute(select(Task).options(load_only(*columns)).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    (row,) = await build_rows(db, [list_item(task)])
    return render("_row_only.html", row=row)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_page(task_id: UUID, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    detail = await load_task_detail(db, task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="task not found")
    # Diff parsing and highlighting is CPU work on agent-written text: off the event loop.
    return render("task.html", v=await run_in_threadpool(build_detail, detail))


@router.get("/tasks/{task_id}/live", response_class=HTMLResponse)
async def task_live(task_id: UUID, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """The polled part of the detail page. 286 tells htmx to stop polling once the task is terminal."""
    detail = await load_task_detail(db, task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="task not found")
    view = await run_in_threadpool(build_detail, detail)
    return render("_task_live.html", status_code=200 if view.live else 286, v=view)


# --------------------------------------------------------------------------- new task


async def _cached_issues(request: Request, db: AsyncSession, github: GithubClient, repo_id: UUID) -> list[IssueOut]:
    cache: dict[UUID, tuple[float, list[IssueOut]]] = getattr(request.app.state, "ui_issue_cache", None) or {}
    request.app.state.ui_issue_cache = cache
    hit = cache.get(repo_id)
    if hit is not None and time.monotonic() - hit[0] < ISSUE_CACHE_SECONDS:
        return hit[1]
    issues = await fetch_issues(db, github, repo_id)
    cache[repo_id] = (time.monotonic(), issues)
    return issues


def _filter_issues(issues: list[IssueOut], q: str) -> list[IssueOut]:
    needle = q.strip().lower().lstrip("#")
    if not needle:
        return issues
    return [i for i in issues if needle in i.title.lower() or needle in str(i.number) or any(needle in lb.lower() for lb in i.labels)]


async def _new_task_context(
    request: Request,
    db: AsyncSession,
    github: GithubClient,
    repo_id: UUID,
    *,
    issue_number: int | None,
    target_branch: str | None,
    form_error: str | None = None,
    branch_error: str | None = None,
) -> dict[str, Any]:
    repo = await active_repo(db, repo_id)
    problems: list[str] = []
    issues: list[IssueOut] = []
    branches = []
    try:
        issues = await _cached_issues(request, db, github, repo_id)
    except HTTPException as exc:
        problems.append(f"Could not load open issues: {exc.detail}")
    try:
        branches = await fetch_branches(db, github, repo_id)
    except HTTPException as exc:
        problems.append(f"Could not load branches: {exc.detail}")
    estimate = estimate_view(await compute_estimate(db, repo_id))
    names = [b.name for b in branches]
    chosen = target_branch or repo.default_branch
    if chosen not in names and names:
        # An unknown posted branch is shown as posted (so the error can name it); the
        # select still lists the real ones.
        names = [chosen, *names] if target_branch else names
    picked = next((i for i in issues if i.number == issue_number), None)
    return {
        "repo": repo,
        "issues": issues[:ISSUE_LIST_LIMIT],
        "issue_total": len(issues),
        "branches": branches,
        "branch_names": names,
        "default_branch": repo.default_branch,
        "chosen_branch": chosen,
        "picked": picked,
        "issue_number": issue_number,
        "estimate": estimate,
        "problems": problems,
        "form_error": form_error,
        "branch_error": branch_error,
        "q": "",
        "oob": False,
    }


@router.get("/repos/{repo_id}/tasks/new", response_class=HTMLResponse)
async def new_task_page(
    request: Request,
    repo_id: UUID,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> HTMLResponse:
    ctx = await _new_task_context(request, db, github, repo_id, issue_number=None, target_branch=None)
    return render("new_task.html", **ctx)


@router.get("/repos/{repo_id}/tasks/new/issues", response_class=HTMLResponse)
async def new_task_issues(
    request: Request,
    repo_id: UUID,
    q: str = "",
    issue_number: str | None = None,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> HTMLResponse:
    await active_repo(db, repo_id)
    issues = await _cached_issues(request, db, github, repo_id)
    matches = _filter_issues(issues, q)
    return render(
        "_issues.html",
        issues=matches[:ISSUE_LIST_LIMIT],
        issue_total=len(matches),
        issue_number=_int_or_none(issue_number),
        q=q,
        oob=True,
    )


@router.get("/repos/{repo_id}/tasks/new/confirm", response_class=HTMLResponse)
async def new_task_confirm(
    request: Request,
    repo_id: UUID,
    issue_number: str | None = None,
    target_branch: str | None = None,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
) -> HTMLResponse:
    repo = await active_repo(db, repo_id)
    number = _int_or_none(issue_number)
    issues = await _cached_issues(request, db, github, repo_id) if number is not None else []
    picked = next((i for i in issues if i.number == number), None)
    estimate = estimate_view(await compute_estimate(db, repo_id))
    return render(
        "_confirm.html",
        repo=repo,
        picked=picked,
        issue_number=number,
        chosen_branch=(target_branch or repo.default_branch),
        estimate=estimate,
    )


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


@router.post("/repos/{repo_id}/tasks")
async def create_task_form(
    request: Request,
    repo_id: UUID,
    db: AsyncSession = Depends(get_db),
    github: GithubClient = Depends(get_github_client),
    publisher: TaskPublisher = Depends(get_publisher),
):
    """Enqueue through the same function as `POST /repos/{id}/tasks`, then redirect to the task."""
    if not same_origin(request):
        log.warning("ui.enqueue.cross_origin", origin=request.headers.get("origin"), host=request.headers.get("host"))
        return error_page(403, "Cross-origin request refused. Submit the form from this site.")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        return error_page(400, "Bad Content-Length.")
    if declared > MAX_FORM_BYTES:
        return error_page(413, "Form too large.")
    buffer = bytearray()
    async for chunk in request.stream():  # a chunked body has no Content-Length, so cap what arrives too
        buffer += chunk
        if len(buffer) > MAX_FORM_BYTES:
            return error_page(413, "Form too large.")
    raw = bytes(buffer)
    form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True).items()}
    number = _int_or_none(form.get("issue_number"))
    branch = (form.get("target_branch") or "").strip() or None

    async def again(message: str, status_code: int, is_branch: bool = False) -> HTMLResponse:
        ctx = await _new_task_context(
            request, db, github, repo_id, issue_number=number, target_branch=branch,
            form_error=message, branch_error=message if is_branch else None,
        )
        return render("new_task.html", status_code=status_code, **ctx)

    if number is None or number <= 0:
        return await again("Pick an issue first.", 422)
    try:
        body = CreateTaskRequest(issue_number=number, target_branch=branch)
    except ValidationError:
        return await again("That issue number is not valid.", 422)
    await active_repo(db, repo_id)  # 404 for an unknown repo, not a form error
    try:
        created = await enqueue_task(db, github, repo_id, body, publisher)
    except HTTPException as exc:
        return await again(str(exc.detail), exc.status_code, is_branch=exc.status_code in (400, 422))
    return RedirectResponse(f"/ui/tasks/{created.id}", status_code=303)


def redirect_root() -> RedirectResponse:
    return RedirectResponse("/ui/repos", status_code=307)
