"""The stage strip: `task_stages` rows folded into the seven nodes the UI draws.

Stored stage status is `running|ok|failed`. The strip adds two states that are
derived and never stored: `pending` (no row yet, or never reached) and
`skipped` (Plan, always, until the role split). The word `passed` is NOT a
stage state: it belongs to the task outcome, and "Review passed" beside an
outcome of `failed` would read as a contradiction.

Rows are best-effort writes (see CLAUDE.md, "Stage rows are best-effort"), so a
missing row or a `running` row under a terminal task is a lost write, not
evidence of what the stage did. The task row stays the source of truth for the
result; this module only says what the rows show.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from repolace_shared.db.models import TaskStatus

from repolace_api.schemas import StageOut, TaskDetail
from repolace_api.ui.format import clock, duration, first_line

#: UI node -> the `task_stages.stage` names folded into it. The names with no
#: node of their own (`instance`, `preflight`, `embedder`, `branch`,
#: `baseline_files`, `finalize`) are folded into the neighbour they run beside.
NODE_STAGES: dict[str, tuple[str, ...]] = {
    "retrieve": ("instance", "preflight", "embedder", "clone", "index", "retrieve"),
    "edit": ("branch", "agent"),
    "test": ("verify_baseline", "record_baseline", "baseline_files"),
    "review": ("review",),
    "push": ("squash", "push"),
    "pr": ("pr", "finalize"),
}
#: The stages whose presence means a node ran to its end (the others are optional).
_PRIMARY: dict[str, tuple[str, ...]] = {
    "retrieve": ("clone", "index", "retrieve"),
    "edit": ("agent",),
    "test": ("verify_baseline",),
    "review": ("review",),
    "push": ("push",),
    "pr": ("pr",),
}
NODE_LABELS = {
    "retrieve": "Retrieve",
    "plan": "Plan",
    "edit": "Edit",
    "test": "Test",
    "review": "Review",
    "push": "Push",
    "pr": "PR",
}
MAIN_ORDER = ("retrieve", "plan", "edit", "test", "review")
DELIVERY_ORDER = ("push", "pr")
STAGE_TO_NODE = {stage: node for node, stages in NODE_STAGES.items() for stage in stages}
STATES = ("pending", "running", "ok", "failed", "skipped")

#: A `running` task whose heartbeat is older than this has probably lost its runner.
HEARTBEAT_STALE_SECONDS = 90


@dataclass
class Node:
    key: str
    label: str
    state: str  # one of STATES
    detail: str  # the words after the state: a duration, "not reached", ...
    error: str | None = None


def _seconds(stage: StageOut, now: datetime) -> float:
    end = stage.ended_at or now
    return max(0.0, (end - stage.started_at).total_seconds())


def build_strip(detail: TaskDetail, now: datetime | None = None) -> list[Node]:
    """All seven nodes in display order (main five, then Push and PR)."""
    now = now or datetime.now(UTC)
    terminal = detail.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING)
    by_node: dict[str, list[StageOut]] = {key: [] for key in NODE_STAGES}
    for stage in detail.stages:
        node = STAGE_TO_NODE.get(stage.stage)
        if node is not None:
            by_node[node].append(stage)

    scored = [r for r in detail.test_runs if r.attempt >= 1]
    scored_seconds = sum(float(r.duration_seconds or 0) for r in scored)
    last_run_errored = bool(detail.test_runs) and detail.test_runs[-1].error

    states: dict[str, str] = {}
    errors: dict[str, str | None] = {}
    seconds: dict[str, float | None] = {}

    for key in NODE_STAGES:
        rows = by_node[key]
        seconds[key] = sum(_seconds(r, now) for r in rows) if rows else None
        errors[key] = next((r.error for r in rows if r.status == "failed"), None)
        if not rows:
            states[key] = "pending"
        elif any(r.status == "failed" for r in rows):
            states[key] = "failed"
        elif any(r.status == "running" for r in rows):
            # Under a failed task a stale `running` row is where it stopped; under
            # any other terminal task it is a lost write and proves nothing.
            if terminal:
                states[key] = "failed" if detail.status is TaskStatus.FAILED else "pending"
            else:
                states[key] = "running"
        else:
            states[key] = "ok"

    # Scored suites run inside the `agent` stage, so a sandbox failure there
    # fails `agent`. When the last scored run is the thing that errored, say Test
    # failed rather than Edit.
    if states["edit"] == "failed" and last_run_errored and scored:
        states["edit"] = "ok"
        states["test"] = "failed"
        errors["test"] = errors["edit"]
    if scored:
        seconds["test"] = (seconds["test"] or 0.0) + scored_seconds
        seconds["edit"] = max(0.0, (seconds["edit"] or 0.0) - scored_seconds) if seconds["edit"] is not None else None
        if states["test"] == "pending":
            states["test"] = "ok"

    # A node with rows but not all its primary stages is "running" while the task
    # is live and between stages; once terminal it ran as far as it got.
    for key, rows in by_node.items():
        if states[key] == "ok" and not terminal:
            names = {r.stage for r in rows}
            later = any(by_node[k] for k in _after(key))
            if not all(p in names for p in _PRIMARY[key]) and not later:
                states[key] = "running"

    order = [*MAIN_ORDER, *DELIVERY_ORDER]
    failed_at = next((i for i, k in enumerate(order) if k != "plan" and states.get(k) == "failed"), None)
    nodes: list[Node] = []
    for i, key in enumerate(order):
        if key == "plan":
            nodes.append(Node(key, NODE_LABELS[key], "skipped", "not used yet"))
            continue
        state = states[key]
        if state == "failed":
            text = duration(seconds[key]) if seconds[key] is not None else "stopped here"
        elif state == "running":
            text = clock(seconds[key]) if seconds[key] is not None else "0:00"
        elif state == "ok":
            text = duration(seconds[key])
        elif failed_at is not None and i > failed_at:
            text = "not reached"
        elif terminal:
            text = "not run"
        else:
            text = "pending"
        nodes.append(Node(key, NODE_LABELS[key], state, text, errors.get(key)))
    return nodes


def _after(key: str) -> list[str]:
    order = [k for k in (*MAIN_ORDER, *DELIVERY_ORDER) if k != "plan"]
    return order[order.index(key) + 1 :]


def failed_node(nodes: list[Node]) -> Node | None:
    return next((n for n in nodes if n.state == "failed"), None)


def current_label(nodes: list[Node]) -> str | None:
    running = next((n for n in nodes if n.state == "running"), None)
    return running.label if running else None


def failure_title(node: Node | None, fallback: str | None) -> str:
    """`Failed in Test: <first line>`, from whatever text exists; no invented fields."""
    where = f"Failed in {node.label}" if node else "Failed"
    message = first_line((node.error if node else None) or fallback)
    return f"{where}: {message}" if message else where
