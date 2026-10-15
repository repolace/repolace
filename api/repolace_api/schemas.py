"""Response models for the task and repo routes.

Status and outcome are the enums from `repolace_shared.db.models`, so the
vocabulary in the JSON is the vocabulary in the database. Stage status is the
three values the pipeline writes (`running`, `ok`, `failed`); a UI that wants
"pending" or "skipped" derives them from the absence of a row.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from repolace_shared.db.models import TaskOutcome, TaskStatus


class RepoOut(BaseModel):
    id: UUID
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool


class IssueOut(BaseModel):
    number: int
    title: str
    html_url: str
    labels: list[str] = []
    author: str | None = None
    created_at: datetime | None = None


class BranchOut(BaseModel):
    name: str
    default: bool


class TaskCreated(BaseModel):
    id: UUID
    repo_id: UUID
    issue_number: int
    issue_title: str
    target_branch: str
    status: TaskStatus
    open_pr_on_failure: bool


class TaskListItem(BaseModel):
    id: UUID
    issue_number: int
    issue_title: str
    target_branch: str
    status: TaskStatus
    outcome: TaskOutcome | None
    agent_stop_reason: str | None
    cost_usd: Decimal | None
    pr_url: str | None
    pr_number: int | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class TaskPage(BaseModel):
    items: list[TaskListItem]
    #: Pass as `before` for the next page; null when this was the last one.
    next_before: datetime | None


class StageOut(BaseModel):
    stage: str
    status: Literal["running", "ok", "failed"]
    started_at: datetime
    ended_at: datetime | None
    error: str | None


class TestRunSummary(BaseModel):
    """Counts only: the raw sets hold node ids, which can name hidden tests."""

    __test__ = False  # not a pytest class

    attempt: int
    passed: int
    failed: int
    xfailed: int
    skipped: int
    collect_failures: int
    error: bool
    duration_seconds: Decimal | None


class TaskDetail(TaskListItem):
    repo_id: UUID
    issue_url: str
    open_pr_on_failure: bool
    attempts: int
    retry_count: int
    patch_sha: str | None
    patch_diff: str | None
    changed_files: list[str] | None
    score_reason: str | None
    test_edit_justification: str | None
    test_edit_approved_by: str | None
    test_edit_approved_at: datetime | None
    claimed_by: str | None
    heartbeat_at: datetime | None
    updated_at: datetime
    stages: list[StageOut]
    test_runs: list[TestRunSummary]


class CostEstimate(BaseModel):
    #: Where the numbers came from: this repo's recent tasks, other tasks that
    #: used the same model, or nothing (then only `upper_bound_usd` is real).
    basis: Literal["repo", "model", "none"]
    n: int
    p50_usd: Decimal | None
    p90_usd: Decimal | None
    upper_bound_usd: Decimal
