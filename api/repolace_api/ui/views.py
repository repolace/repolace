"""View models: what the templates are handed, built from the same query functions the JSON API uses."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from repolace_shared.db.models import TaskOutcome, TaskStage, TaskStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_api.routes.repos import TASK_COST_CAP_USD, running_costs
from repolace_api.schemas import CostEstimate, TaskDetail, TaskListItem
from repolace_api.ui.diffview import ParsedDiff, parse_diff
from repolace_api.ui.format import duration, first_line
from repolace_api.ui.pipeline import (
    HEARTBEAT_STALE_SECONDS,
    NODE_LABELS,
    STAGE_TO_NODE,
    Node,
    build_strip,
    current_label,
    failed_node,
    failure_title,
)

LIVE_STATUSES = (TaskStatus.QUEUED, TaskStatus.RUNNING)
#: `unscored` is a UI label for `outcome IS NULL`, never a value of `TaskOutcome`.
UNSCORED = "unscored"
OUTCOME_FILTERS = ("all", *(o.value for o in TaskOutcome), UNSCORED)
#: The poll interval, spelled once so the templates and the label cannot disagree.
POLL = "every 3s"


@dataclass
class RowView:
    item: TaskListItem
    live: bool
    note_key: str | None
    note: str | None
    note_is_error: bool
    cost: Decimal | None
    cost_is_so_far: bool


async def _stage_hints(db: AsyncSession, task_ids: list[UUID]) -> dict[UUID, dict[str, str]]:
    """The newest `running` and `failed` stage per task, as UI node labels. One query."""
    if not task_ids:
        return {}
    rows = await db.execute(
        select(TaskStage.task_id, TaskStage.stage, TaskStage.status)
        .where(TaskStage.task_id.in_(task_ids), TaskStage.status.in_(("running", "failed")))
        .order_by(TaskStage.started_at)
    )
    hints: dict[UUID, dict[str, str]] = {}
    for task_id, stage, status in rows:
        node = STAGE_TO_NODE.get(stage)
        if node is not None:
            hints.setdefault(task_id, {})[status] = NODE_LABELS[node]
    return hints


def _note(item: TaskListItem, hints: dict[str, str]) -> tuple[str | None, str | None, bool]:
    if item.status is TaskStatus.RUNNING:
        return "stage", hints.get("running", "starting"), False
    if item.status is TaskStatus.FAILED:
        where = hints.get("failed")
        message = first_line(item.error_message)
        text = ": ".join(part for part in (where, message) if part)
        return "error", text or "no detail recorded", True
    if item.status is TaskStatus.CONFLICTING:
        return "conflict", "files not recorded", False
    if item.status is TaskStatus.PR_OPENED and item.outcome is TaskOutcome.FAILED:
        return "note", "PR opened despite failing score", False
    if item.status is TaskStatus.COMPLETED:
        if item.agent_stop_reason:
            return "stop", item.agent_stop_reason, False
        if item.outcome is None:
            return "note", "not scored: the agent did not run", False
    return None, None, False


async def build_rows(db: AsyncSession, items: list[TaskListItem]) -> list[RowView]:
    live_ids = [i.id for i in items if i.status in LIVE_STATUSES and i.cost_usd is None]
    costs = await running_costs(db, live_ids)
    hints = await _stage_hints(db, [i.id for i in items if i.status in (*LIVE_STATUSES, TaskStatus.FAILED)])
    rows = []
    for item in items:
        key, note, is_error = _note(item, hints.get(item.id, {}))
        so_far = item.cost_usd is None and item.status in LIVE_STATUSES
        cost = item.cost_usd if item.cost_usd is not None else (costs.get(item.id) if so_far else None)
        rows.append(RowView(item, item.status in LIVE_STATUSES, key, note, is_error, cost, so_far and cost is not None))
    return rows


@dataclass
class DetailView:
    detail: TaskDetail
    live: bool
    nodes: list[Node]
    main_nodes: list[Node]
    delivery_nodes: list[Node]
    failed: Node | None
    failure_title: str | None
    running_label: str | None
    diff: ParsedDiff | None
    diff_open: bool
    ran_seconds: float | None
    ran: str
    cost: Decimal | None
    cost_is_so_far: bool
    cost_pct: float
    heartbeat_stale_seconds: int | None
    stop_reason: str | None


def build_detail(detail: TaskDetail, now: datetime | None = None) -> DetailView:
    now = now or datetime.now(UTC)
    live = detail.status in LIVE_STATUSES
    nodes = build_strip(detail, now)
    failed = failed_node(nodes) if detail.status is TaskStatus.FAILED else None
    diff = parse_diff(detail.patch_diff) if detail.patch_diff else None

    ran_seconds = None
    if detail.started_at is not None:
        end = detail.completed_at or (now if live else None)
        if end is not None:
            ran_seconds = max(0.0, (end - detail.started_at).total_seconds())

    cost = detail.cost_usd if detail.cost_usd is not None else detail.cost_so_far_usd
    pct = 0.0
    if cost is not None:
        pct = max(0.0, min(100.0, float(cost / TASK_COST_CAP_USD * 100)))

    stale = None
    if detail.status is TaskStatus.RUNNING and detail.heartbeat_at is not None:
        age = int((now - detail.heartbeat_at).total_seconds())
        if age > HEARTBEAT_STALE_SECONDS:
            stale = age

    return DetailView(
        detail=detail,
        live=live,
        nodes=nodes,
        main_nodes=[n for n in nodes if n.key not in ("push", "pr")],
        delivery_nodes=[n for n in nodes if n.key in ("push", "pr")],
        failed=failed,
        failure_title=failure_title(failed, detail.error_message) if detail.status is TaskStatus.FAILED else None,
        running_label=current_label(nodes),
        diff=diff,
        # A diff that was never pushed is collapsed: it is evidence, not the result.
        diff_open=detail.pr_url is not None,
        ran_seconds=ran_seconds,
        ran=duration(ran_seconds) if ran_seconds is not None else "\N{EM DASH}",
        cost=cost,
        cost_is_so_far=detail.cost_usd is None and cost is not None,
        cost_pct=pct,
        heartbeat_stale_seconds=stale,
        stop_reason=detail.agent_stop_reason,
    )


@dataclass
class EstimateView:
    basis: str
    n: int
    p50: Decimal | None
    p90: Decimal | None  # clipped to the cap
    left_pct: float
    width_pct: float
    low_history: bool
    cap: Decimal
    basis_text: str


def estimate_view(est: CostEstimate) -> EstimateView:
    cap = est.upper_bound_usd
    p50 = est.p50_usd
    p90 = min(est.p90_usd, cap) if est.p90_usd is not None else None
    left = width = 0.0
    if p50 is not None and p90 is not None and cap > 0:
        left = max(0.0, min(100.0, float(p50 / cap * 100)))
        width = max(0.0, min(100.0 - left, float((max(p90, p50) - p50) / cap * 100)))
    low = est.basis != "repo" or est.n < 5
    if est.basis == "repo":
        text = f"Median of the last {est.n} tasks on this repo."
    elif est.basis == "model":
        text = f"Median of {est.n} tasks on the same model, across repos. This repo has too little history of its own."
    else:
        text = f"No history yet, capped at ${cap:.2f}."
    if est.p90_usd is not None and est.p90_usd > cap:
        text += f" p90 is clipped to the ${cap:.2f} cap."
    return EstimateView(est.basis, est.n, p50, p90, left, width, low, cap, text)
