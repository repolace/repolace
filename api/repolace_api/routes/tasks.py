from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from repolace_shared.db.models import LLMCall, RegisteredRepo, Task, TaskStage, TaskStatus, TaskTestRun
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from repolace_api.deps import get_db
from repolace_api.schemas import StageOut, TaskDetail, TestRunSummary

router = APIRouter(prefix="/tasks", tags=["tasks"])

_NOT_COPIED = {"attempts", "stages", "test_runs", "repo_full_name", "cost_so_far_usd", "is_benchmark"}

#: A task in one of these states is polled every few seconds by the detail page.
_LIVE = (TaskStatus.QUEUED, TaskStatus.RUNNING)
#: Unbounded text that only a finished task has: the pipeline writes both in the UPDATE that ends it. They are
#: left out of the row load and read separately, once, for a task that is not live -- see `load_task_detail`.
_TERMINAL_TEXT = ("patch_diff", "score_reason")

#: Oracle material on a benchmark task: see `load_task_detail`.
BENCHMARK_SCRUBBED_FIELDS = (
    "score_reason",
    "patch_diff",
    "changed_files",
    "error_message",
    "test_edit_justification",
)


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: UUID, db: AsyncSession = Depends(get_db)) -> TaskDetail:
    detail = await load_task_detail(db, task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="task not found")
    return detail


async def load_task_detail(db: AsyncSession, task_id: UUID) -> TaskDetail | None:
    """The one place a task's detail is assembled: the JSON route and the HTML pages both call it.

    Benchmark scrubbing happens here, so no caller can forget it.

    The detail page polls this while a task runs, so the row is loaded **without** `patch_diff`,
    `score_reason` and `issue_body` (large, and the last is not in `TaskDetail` at all). The first two are
    read in a second query only when the task is no longer live, and never for a benchmark task, whose copy
    is scrubbed anyway. A live task has neither value (they are written when it ends), so the response is
    the same as when the whole row was loaded.
    """
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .options(*(defer(getattr(Task, name), raiseload=True) for name in (*_TERMINAL_TEXT, "issue_body")))
        )
    ).scalar_one_or_none()
    if task is None:
        return None
    terminal_text: dict[str, str | None] = dict.fromkeys(_TERMINAL_TEXT)
    if task.status not in _LIVE and task.eval_run_id is None:
        row = (await db.execute(select(Task.patch_diff, Task.score_reason).where(Task.id == task_id))).one_or_none()
        if row is None:  # deleted between the two reads: the same answer as a task that was never there
            return None
        terminal_text = dict(zip(_TERMINAL_TEXT, row, strict=True))
    repo = await db.get(RegisteredRepo, task.repo_id)
    assert repo is not None  # the foreign key is NOT NULL with ON DELETE CASCADE

    stages = (
        (await db.execute(select(TaskStage).where(TaskStage.task_id == task_id).order_by(TaskStage.started_at)))
        .scalars()
        .all()
    )
    # Counted in SQL: the raw sets hold every node id of the suite, and the
    # detail view needs neither the ids nor the transfer.
    run = TaskTestRun
    runs = (
        await db.execute(
            select(
                run.attempt,
                func.cardinality(run.passed),
                func.cardinality(run.failed),
                func.cardinality(run.xfailed),
                func.cardinality(run.skipped),
                func.cardinality(run.collect_failures),
                run.error.is_not(None),
                run.duration_seconds,
            )
            .where(run.task_id == task_id)
            .order_by(run.attempt)
        )
    ).all()
    summaries = [
        TestRunSummary(
            attempt=r[0],
            passed=r[1],
            failed=r[2],
            xfailed=r[3],
            skipped=r[4],
            collect_failures=r[5],
            error=r[6],
            duration_seconds=r[7],
        )
        for r in runs
    ]
    copied = {
        name: getattr(task, name)
        for name in TaskDetail.model_fields
        if name not in _NOT_COPIED and name not in _TERMINAL_TEXT
    }
    copied.update(terminal_text)
    stage_out = [StageOut.model_validate(s, from_attributes=True) for s in stages]
    cost_so_far: Decimal | None = None
    if task.cost_usd is None and task.status in _LIVE:
        # Derived from the call log: the terminal write has not happened yet.
        cost_so_far = (
            await db.execute(select(func.sum(LLMCall.cost_usd)).where(LLMCall.task_id == task_id))
        ).scalar_one_or_none()
    if task.eval_run_id is not None:
        # Oracle material. A benchmark task's score sentence can name hidden
        # test ids, its diff and files are the agent's answer, and stage and
        # task errors carry sandbox output. The UI hides benchmark rows from the
        # list; this closes the by-id route. Status, outcome, stop reason, cost,
        # attempts and timings stay.
        for name in BENCHMARK_SCRUBBED_FIELDS:
            copied[name] = None
        for stage in stage_out:
            stage.error = None
    return TaskDetail(
        **copied,
        repo_full_name=repo.full_name,
        cost_so_far_usd=cost_so_far,
        is_benchmark=task.eval_run_id is not None,
        attempts=max((s.attempt for s in summaries), default=0),
        stages=stage_out,
        test_runs=summaries,
    )
