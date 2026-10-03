"""Scoring one edit attempt: the function the agent calls to find out how it did.

`AttemptScorer.verify_attempt` is `AgentDeps.verify_attempt`. It is the only way a
suite result reaches `task_test_runs` and the only thing that turns a working tree into a
*scored* state, so the rules about what counts as an attempt are here and nowhere else.

**What an attempt is.** The tree as it stands, committed. `record_attempt` checkpoints it
(a no-op when a tool already did), and the commit at HEAD is what the suite runs against
and what `task_test_runs.commit_sha` records -- which is why a checkpoint commit made by
`run_python` before `verify_attempt` is not "an attempt that was skipped": the agent's
edits were committed by it, and the sha at HEAD is the state being measured.

**When there is nothing to score (`None`).** Two different questions, both asked, in this
order:

1. *Against the base commit:* does the tree differ at all? A tree identical to the base is
   "the agent did nothing", however many commits it made getting back there.
2. *Against the last scored attempt:* has the **tree** changed since? Compared as a diff, not
   as shas -- an edit followed by its own revert makes a new commit with the old tree, and
   re-running a suite that can take thirty minutes to learn nothing is the cost this avoids.

**What an attempt never does: rewind.** The system prompt tells the agent that earlier edits
persist, and a later attempt builds on them; nothing here resets the tree between attempts.
Only the end of the task rewinds (to the last *scored* commit), and only the pipeline does it.
"""

from collections.abc import Awaitable, Callable

import structlog
from repolace_shared.git import TaskWorkspace, redact
from verify.errors import SandboxError, SandboxUnavailable
from verify.protocol import SuiteResult
from verify.stage import Verifier

from repolace_agents.contracts import AttemptRecord

log = structlog.get_logger()

#: Persists one scored run: `(attempt, commit_sha, result)`. A parameter rather than an import
#: so the rules above are testable without a database; `run_task` binds the real writer.
RecordRun = Callable[[int, str, SuiteResult], Awaitable[None]]


async def run_scored_suite(verifier: Verifier, workspace: TaskWorkspace, attempt: int) -> tuple[SuiteResult, bool]:
    """Run one suite, turning a sandbox failure into an unscoreable result.

    Returns the result and whether the failure was *infrastructure*. The
    distinction decides who gets blamed: `SandboxUnavailable` means the daemon
    was not there, which `score` excludes from the benchmark as an instrument
    failure, while a suite that timed out or was OOM-killed under a patch is the
    patch's doing and scores FAILED.

    Anything that is not a `SandboxError` is left to propagate. An `OSError`
    writing the export is repolace being broken, and it should fail the task
    loudly rather than be laundered into an unscoreable run.
    """
    try:
        return await verifier.run(workspace, attempt), False
    except SandboxUnavailable as exc:
        log.error("pipeline.verify.unavailable", attempt=attempt, error=str(exc))
        return SuiteResult(error=redact(str(exc))), True
    except SandboxError as exc:
        log.error("pipeline.verify.failed", attempt=attempt, error=str(exc))
        return SuiteResult(error=redact(str(exc))), False


class AttemptScorer:
    """One task's scored attempts. Holds the last one's sha, so "nothing new" can be told."""

    def __init__(self, *, verifier: Verifier, workspace: TaskWorkspace, record: RecordRun) -> None:
        self._verifier = verifier
        self._workspace = workspace
        self._record = record
        self._last_sha: str | None = None
        self._last_number = 0
        self._last_record: AttemptRecord | None = None
        self._count = 0

    @property
    def attempts(self) -> int:
        """How many scored attempts have been returned: `AgentResult.attempts`, as the contract defines it."""
        return self._count

    @property
    def last_record(self) -> AttemptRecord | None:
        """The last scored attempt, or None. Only ever replaced by a record, never cleared."""
        return self._last_record

    async def verify_attempt(self, attempt: int) -> AttemptRecord | None:
        """Commit the tree, score it, record it. `None` means there was nothing to score.

        `attempt` is the number the agent is on (1..N). It must be new: 0 is the baseline, and
        `task_test_runs` is unique on `(task_id, attempt)`, so a repeat would fail at the
        database, far from the call that caused it.

        The result is the **unfiltered** suite result -- what `task_test_runs` stores and
        scoring reads -- inside an `AttemptRecord` that says so. Nothing here may put it in front
        of the model.
        """
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError(f"an attempt number is 1 or more (0 is the baseline), got {attempt!r}")
        if attempt <= self._last_number:
            raise ValueError(f"attempt {attempt} was already scored; attempt numbers must increase")
        if not self._verifier.prepared:
            # Unreachable by construction: no agent runs when the baseline failed to build the
            # environment. Guarded because the alternative is `Verifier.run` building it from a
            # tree the agent has already edited, which breaks "built once, from the base commit".
            raise RuntimeError("verify_attempt called before the environment was built from the base commit")

        workspace = self._workspace
        await workspace.record_attempt(f"attempt {attempt}")

        if not await workspace.changed_files():
            return None
        if self._last_sha is not None and not await workspace.repo.changed_files_from(self._last_sha):
            return None

        sha = await workspace.repo.head_sha()
        result, infrastructure_error = await run_scored_suite(self._verifier, workspace, attempt)
        await self._record(attempt, sha, result)

        record = AttemptRecord(
            attempt=attempt, commit_sha=sha, result=result, infrastructure_error=infrastructure_error
        )
        self._last_sha = sha
        self._last_number = attempt
        self._last_record = record
        self._count += 1
        return record
