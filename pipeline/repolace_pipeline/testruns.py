"""Persisting one suite execution.

Its own module, and its own session, for the reason `_index` opens one: a
`task_test_runs` write must not ride on the pipeline's session, where it would
either commit half-written task state or be discarded along with it when the
task later fails.

Writes what the run *produced*, never what the run *meant*. The fail-to-pass and
pass-to-pass lists are derived by `verify.scoring` from these rows, and keeping
the derivation out of the database is what lets a revised rule rescore existing
runs without re-running anything -- migrations 0009, 0010 and 0011 each exist
because a set that was not stored could not be recovered.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_shared.db.models import TaskTestRun
from verify.protocol import SuiteResult

log = structlog.get_logger()


def to_row(
    task_id: uuid.UUID, attempt: int, commit_sha: str, result: SuiteResult
) -> TaskTestRun:
    """Project a `SuiteResult` onto the table. Pure, so it is testable without a database."""
    return TaskTestRun(
        task_id=task_id,
        attempt=attempt,
        commit_sha=commit_sha,
        passed=list(result.passed),
        failed=list(result.failed),
        skipped=list(result.skipped),
        xfailed=list(result.xfailed),
        did_not_run=list(result.did_not_run),
        collect_failures=list(result.collect_failures),
        collected_files=list(result.collected_files),
        conftests=list(result.conftests),
        # `dict(...)` because the field is a Mapping and JSONB serialisation
        # needs a concrete type it can hand to json.dumps.
        fingerprint=dict(result.fingerprint),
        stdout_tail=result.stdout_tail,
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
        error=result.error,
    )


async def record_test_run(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    attempt: int,
    commit_sha: str,
    result: SuiteResult,
) -> None:
    async with session_factory() as session:
        session.add(to_row(task_id, attempt, commit_sha, result))
        await session.commit()
    log.info(
        "pipeline.verify.recorded",
        attempt=attempt,
        commit_sha=commit_sha,
        passed=len(result.passed),
        failed=len(result.failed),
        skipped=len(result.skipped),
        xfailed=len(result.xfailed),
        error=result.error,
    )
