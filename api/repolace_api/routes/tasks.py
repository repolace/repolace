from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from repolace_shared.db.models import Task, TaskStage, TaskTestRun
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_api.deps import get_db
from repolace_api.schemas import StageOut, TaskDetail, TestRunSummary

router = APIRouter(prefix="/tasks", tags=["tasks"])

_NOT_COPIED = {"attempts", "stages", "test_runs"}


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: UUID, db: AsyncSession = Depends(get_db)) -> TaskDetail:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

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
    copied = {name: getattr(task, name) for name in TaskDetail.model_fields if name not in _NOT_COPIED}
    return TaskDetail(
        **copied,
        attempts=max((s.attempt for s in summaries), default=0),
        stages=[StageOut.model_validate(s, from_attributes=True) for s in stages],
        test_runs=summaries,
    )
