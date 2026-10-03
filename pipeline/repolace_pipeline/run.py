"""One task, start to finish.

`run_task` takes its resources as parameters and owns none of them -- no
`argv`, no engine, no logging setup, no `sys.exit`. That is what lets Phase 2
lift the body into a Celery task unchanged: the worker brings a process-level
engine and client, the CLI brings per-invocation ones, and this stays the same.

Verify runs twice: once at the base commit for the baseline, once after each
edit attempt. Both go through `verify.stage.Verifier`, so nothing here knows that
containers exist -- swapping Docker for gVisor or a remote executor is a
different `SandboxBackend`, not a change to this file.

The ordering is load-bearing and matches CLAUDE.md's branch/PR flow: index from
the clean tree, then baseline, then branch, then the agent. Baseline before the
branch because it is what "the tests pass" is measured against -- without it a
repo with pre-existing failures scores as a failure whatever the agent did.

**The agent is a parameter.** `agent` is any `AgentRunner`: the LLM graph, the stub
that keeps the plumbing smoke test, the gold runner that validates a benchmark
instance. This module hands it an `AgentDeps` and reads back an `AgentResult`, and
everything after that -- rewind, score, gate, squash, push, PR, and the one write that
records the outcome -- is the same whichever it was. That is what lets the whole
pipeline be tested end to end before a model is involved.

**What `failed` means, and what it does not.** `FAILED` is reserved for "repolace itself
broke": a clone that would not clone, a GitHub 403, a bug. An agent that ran out of
budget, hit its step cap, made no change, or gave up did not break repolace; the task
**completes**, with the outcome the scorer gives it (no scored attempt scores `failed`).
The report counts a `FAILED` task as a harness error and removes it from the headline's
denominator, so an agent-caused stop that raised `StageFailed` would quietly *improve* the
benchmark number. Nothing after the agent returns may raise for a reason the agent caused.
"""

import asyncio
import subprocess
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from pathlib import Path

import httpx
import structlog
from sqlalchemy import func, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_gateway.budget import BudgetExceeded, BudgetLimit, TaskBudget, task_scope
from repolace_gateway.errors import LLMCallError
from repolace_shared.db.models import (
    BASELINE_ATTEMPT,
    RegisteredRepo,
    Task,
    TaskOutcome,
    TaskStatus,
)
from repolace_shared.git import GitError, TaskWorkspace, installation_token_provider, redact, task_workspace
from repolace_shared.github.client import GithubClient
from repolace_shared.instances import InstanceSpec, load_instance_by_id
from retrieval.embed import get_embedder
from retrieval.index import RepoIndexInProgress, get_current_chunk_count, reindex_if_stale
from retrieval.retrieve import hybrid_search
from retrieval.strategies import DEFAULT_STRATEGY
from verify.errors import SandboxError
from verify.protocol import RepoSpec, SandboxBackend, SuiteResult
from verify.scoring import Score, Verdict, agent_verdict, expected_not_red, score
from verify.spec import resolve_spec, spec_from_mapping
from verify.stage import Verifier

from repolace_agents.contracts import AgentDeps, AgentLimits, AgentResult, AgentRunner, IssueContext, LLMClientLike, StopReason
from repolace_agents.tools import ToolBox, build_toolbox
from repolace_pipeline.agent_context import build_tool_context, make_search
from repolace_pipeline.attempts import AttemptScorer, run_scored_suite
from repolace_pipeline.context import RetrievedChunk, repo_overview, search_hit
from repolace_pipeline.edit import StubEditRequest
from repolace_pipeline.errors import PipelineError, StageFailed, TaskNotClaimable, TaskNotFound
from repolace_pipeline.finalize import first_model, task_cost, write_failure_minimal, write_terminal
from repolace_pipeline.gate import PrDecision, pr_decision
from repolace_pipeline.pr import PrFacts, commit_message, pr_title, render_pr_body
from repolace_pipeline.runners import StubAgent
from repolace_pipeline.testruns import record_test_run

log = structlog.get_logger()

RETRIEVE_LIMIT = 8
MAX_ERROR_CHARS = 4000
REQUIRED_PERMISSIONS = {"contents": "write", "pull_requests": "write"}

#: How long `_index` waits for another task that holds this repo's indexing lock, in steps,
#: and the longest it will wait in all. The lock is a *try*-lock, so two tasks on one repo hit
#: it immediately -- that is the normal case for a benchmark (three runs of one instance), not
#: an error. Twenty minutes is longer than any first index this project has seen.
INDEX_WAIT_STEP_SECONDS = 10.0
INDEX_WAIT_CAP_SECONDS = 1200.0
#: Indirection so a test can replace the sleep and release the lock at exactly that moment.
_sleep = asyncio.sleep

#: The mapping the agents package keeps privately for the same three gateway limits.
_BUDGET_STOPS = {
    BudgetLimit.USD: StopReason.BUDGET_USD,
    BudgetLimit.CALLS: StopReason.BUDGET_CALLS,
    BudgetLimit.WALL_TIME: StopReason.BUDGET_WALL,
}

#: `task_workspace`'s own shape -- `(owner, name, target_branch, token_provider=...)` -- so
#: the default is the function itself and a test passes `partial(task_workspace, clone_url=...)`.
WorkspaceFactory = Callable[..., AbstractAsyncContextManager[TaskWorkspace]]


@dataclass(frozen=True)
class RunResult:
    task_id: uuid.UUID
    status: TaskStatus
    pr_number: int | None = None
    pr_url: str | None = None
    error_message: str | None = None
    #: How the task scored. `None` means it was never scored -- either repolace
    #: broke before Verify, or the instance was inadmissible. Deliberately not
    #: collapsed into `FAILED`: an instrument limitation charged to the agent is
    #: the exact mistake the success criteria are written to avoid.
    outcome: TaskOutcome | None = None
    score_reason: str | None = None
    #: What the model calls cost in total (the gateway's `TaskBudget`
    #: running sum). None, not zero, when nothing was measured -- no model was
    #: called (the stub and gold runners) or the task failed before the agent --
    #: because "free" and "never priced" are different claims.
    cost_usd: Decimal | None = None
    #: Scored edit attempts that were actually run and verified (1..N). 0 when the
    #: task never reached a scored attempt.
    attempts: int = 0
    #: Whether the agent ended by calling `submit`. None when no agent ran, which
    #: is not the same as False (an agent that ran out of steps).
    submitted: bool | None = None
    #: Why the agent loop ended, as the string stored in
    #: `tasks.agent_stop_reason` (one of `AGENT_STOP_REASONS`). A string rather than
    #: the agents package's enum so this module does not import it for a
    #: label. None when no agent ran.
    stop_reason: str | None = None
    #: The PR gate's own sentence for opening, or withholding, a pull request.
    #: None when the gate never ran -- the task failed first.
    pr_gate_reason: str | None = None


@dataclass(frozen=True)
class _Seams:
    """What a caller may replace. Every default reproduces the production behaviour."""

    agent: AgentRunner | None
    llm: LLMClientLike | None
    workspace_factory: WorkspaceFactory
    embedder_warmup: Callable[[], object]
    instances_dir: Path | None
    budget: TaskBudget | None
    open_pr: bool
    embedding_strategy: str


@dataclass
class _Progress:
    """What a task knows so far, for a failure to record.

    A task that dies at the push stage has an agent result, a diff and a sha worth keeping,
    and `_fail` has only a stage name and a message. `_run_stages` updates this as each fact
    becomes known; `_fail` writes whatever is there. It carries plain data only -- it is read
    after a rollback, when an ORM attribute would raise `MissingGreenlet`.

    `scorer` is the one live object, and it is read-only here: when the agent stage itself fails
    there is no `AgentResult`, but the scorer still knows how many attempts were scored.
    """

    result: AgentResult | None = None
    changed_files: list[str] | None = None
    patch_diff: str | None = None
    patch_sha: str | None = None
    #: Set once a pull request exists, so a task that fails *after* opening one still says so.
    pr_number: int | None = None
    pr_url: str | None = None
    scorer: AttemptScorer | None = None


@dataclass(frozen=True)
class _InstanceMode:
    instance: InstanceSpec
    spec: RepoSpec


def _describe(exc: BaseException) -> str:
    """Turn an exception into something readable in a `tasks.error_message` column."""
    if isinstance(exc, SandboxError):
        return str(exc)  # already redacted and tail-capped at construction
    if isinstance(exc, GitError):
        return str(exc)  # GitError already redacts itself
    if isinstance(exc, httpx.HTTPStatusError):
        # The status code is the diagnosis: a 403 here is almost always a
        # missing installation permission, not a bad request.
        return f"github {exc.response.status_code}: {exc.response.text[:500]}"
    if isinstance(exc, httpx.RequestError):
        return f"github unreachable: {exc}"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"git precondition failed: {(exc.stderr or '').strip()[:500]}"
    return f"{type(exc).__name__}: {exc}"


@asynccontextmanager
async def _stage(name: str):
    """Tag whatever fails inside with the stage it failed in."""
    try:
        yield
    except PipelineError:
        raise  # already tagged
    except Exception as exc:
        raise StageFailed(name, _describe(exc)) from exc


async def _claim(state: AsyncSession, task_id: uuid.UUID) -> tuple[Task, RegisteredRepo]:
    """Move `queued` -> `running`, but only if it is still queued.

    Conditional rather than a read-then-write: it makes a second CLI run against
    a finished task a no-op instead of a duplicate PR, and it is exactly the
    guard Phase 2 needs once Celery delivers at-least-once.
    """
    result = await state.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
        .values(status=TaskStatus.RUNNING, started_at=func.now())
    )
    if result.rowcount == 0:
        existing = await state.get(Task, task_id)
        # Read the status *before* rolling back. `rollback()` expires every
        # loaded instance regardless of `expire_on_commit=False` -- that flag
        # only governs commit -- so touching an attribute afterwards triggers a
        # lazy refresh outside the greenlet and raises MissingGreenlet.
        status = existing.status.value if existing is not None else None
        await state.rollback()
        if status is None:
            raise TaskNotFound(task_id)
        raise TaskNotClaimable(task_id, status)

    await state.commit()

    task = await state.get(Task, task_id)
    repo = await state.get(RegisteredRepo, task.repo_id)
    if repo is None:
        raise StageFailed("claim", f"task references unknown repo {task.repo_id}")
    log.info("pipeline.task.claimed", target_branch=task.target_branch)
    return task, repo


async def _best_effort_cost(session_factory, task_id: uuid.UUID) -> Decimal | None:
    """What the task has spent, or None -- and never an exception.

    Its own try, because it runs on the way out of a failure: a database that cannot answer
    a sum must not replace the reason the task failed, and "the spend is unknown" is a better
    row than no row.
    """
    try:
        return await task_cost(session_factory, task_id)
    except Exception as exc:
        log.error("pipeline.cost.unavailable", error=str(exc))
        return None


async def _fail(
    state: AsyncSession,
    session_factory,
    task_id: uuid.UUID,
    stage: str,
    message: str,
    progress: _Progress,
) -> RunResult:
    """Record the failure without letting a write problem replace the reason for it.

    Also records what the task had by then -- the spend, and whatever the agent produced --
    so a harness-error row is not blind: a task that spent a dollar and then failed to push
    must still show the dollar, and the diff it had.
    """
    detail = redact(f"{stage}: {message}")[:MAX_ERROR_CHARS]
    log.error("pipeline.task.failed", stage=stage, error=detail)
    cost = await _best_effort_cost(session_factory, task_id)
    result = progress.result
    stop_reason = result.stop_reason.value if result is not None else None
    # From the result when the agent returned one, else from the scorer: an agent stage that
    # failed still scored whatever it scored, and those rows are in `task_test_runs`.
    if result is not None:
        attempts = result.attempts
    else:
        attempts = progress.scorer.attempts if progress.scorer is not None else 0
    try:
        # Roll back first: a mid-pipeline DB error would otherwise leave the
        # session dirty and make the failure record itself unwritable.
        await state.rollback()
        await write_terminal(
            state,
            task_id,
            status=TaskStatus.FAILED,
            # Never scored: a failure is repolace's, and must not read as the agent's.
            outcome=None,
            score_reason=None,
            error_message=detail,
            agent_stop_reason=stop_reason,
            retry_count=max(0, attempts - 1),
            cost_usd=cost,
            patch_sha=progress.patch_sha,
            patch_diff=progress.patch_diff,
            changed_files=progress.changed_files,
            pr_number=progress.pr_number,
            pr_url=progress.pr_url,
        )
        await state.commit()
    except SQLAlchemyError as exc:
        log.error("pipeline.state.full_write_failed", error=str(exc)[:300])
        # The row must not stay `running` with the result saying FAILED -- and perhaps a PR open.
        # So the least it can say, with nothing in it the database could refuse.
        try:
            await state.rollback()
            await write_failure_minimal(
                state, task_id, error_message=detail, pr_number=progress.pr_number, pr_url=progress.pr_url
            )
            await state.commit()
        except SQLAlchemyError as fallback_exc:
            log.error("pipeline.state.write_failed", error=str(fallback_exc)[:300])
    return RunResult(
        task_id,
        TaskStatus.FAILED,
        error_message=detail,
        cost_usd=cost,
        attempts=attempts,
        submitted=result.submitted if result is not None else None,
        stop_reason=stop_reason,
    )


async def _preflight(github: GithubClient, repo: RegisteredRepo) -> None:
    """Check the installation's granted permissions before doing five minutes of work.

    Without this, a missing `pull_requests: write` surfaces as a 403 at the very
    last step, after the clone, the index and the push have all succeeded.
    """
    installation = await github.get_installation(repo.installation_id)
    missing = [
        f"{name}:{want}"
        for name, want in REQUIRED_PERMISSIONS.items()
        if installation.permissions.get(name) != want
    ]
    if missing:
        raise StageFailed(
            "preflight",
            f"installation {repo.installation_id} lacks {', '.join(missing)} "
            f"(granted: {installation.permissions or 'none'}); "
            f"update the App's permissions and accept the request on the installation",
        )
    log.info("pipeline.preflight.ok", installation_id=repo.installation_id)


async def _index(
    session_factory, repo: RegisteredRepo, repo_path, base_sha: str, strategy: str
) -> int:
    """Index on a session of its own, and report how many chunks the repo *has*.

    `reindex_if_stale` commits and rolls back the session it is given, because
    the advisory lock making it single-flight is transaction-scoped. Sharing the
    pipeline's session would let a reindex commit half-written task state, or
    discard it on failure.

    **Waits for another task's index instead of failing.** The lock is a try-lock, so a
    second task on the same repo is refused at once -- and that is what three runs of one
    benchmark instance do by design. It retries in `INDEX_WAIT_STEP_SECONDS` steps up to
    `INDEX_WAIT_CAP_SECONDS`, then fails the stage with a message saying so. When the wait
    ends the index is normally current, so the task's own pass finds nothing to do.
    """
    waited = 0.0
    while True:
        try:
            async with session_factory() as index_db:
                written = await reindex_if_stale(index_db, repo.id, repo_path, base_sha, strategy)
                # Guard on chunks *present*, not chunks *written*: the latter is 0 for
                # an already-current index, which would fail every second task on a repo.
                present = await get_current_chunk_count(index_db, repo.id)
            break
        except RepoIndexInProgress:
            if waited >= INDEX_WAIT_CAP_SECONDS:
                raise StageFailed(
                    "index",
                    f"another task held this repo's indexing lock for more than "
                    f"{INDEX_WAIT_CAP_SECONDS:.0f}s; retry the task once it has finished",
                ) from None
            log.info("pipeline.index.waiting", waited_seconds=waited, step_seconds=INDEX_WAIT_STEP_SECONDS)
            await _sleep(INDEX_WAIT_STEP_SECONDS)
            waited += INDEX_WAIT_STEP_SECONDS
    log.info("pipeline.index.done", chunks_written=written, chunks_present=present)
    return present


async def _retrieve(session_factory, repo_id: uuid.UUID, query: str, strategy: str) -> tuple[RetrievedChunk, ...]:
    """Retrieval on a short-lived session of its own, projected to plain data inside it.

    Not the pipeline's `state` session: a read on it would leave a transaction open for the
    whole of a minutes-long agent loop. The projection happens before the session closes --
    touching a deferred column afterwards is the `MissingGreenlet` this codebase has met once.
    """
    async with session_factory() as search_db:
        results = await hybrid_search(search_db, repo_id, query, limit=RETRIEVE_LIMIT, query_strategy=strategy)
        return tuple(RetrievedChunk.from_result(result) for result in results)


def _log_score(scored: Score) -> None:
    log.info(
        "pipeline.score",
        outcome=scored.outcome.value if scored.outcome else None,
        inadmissible=scored.inadmissible,
        reason=scored.reason,
        fail_to_pass=list(scored.fail_to_pass[:10]),
        regressions=list(scored.regressions[:10]),
        neutralized=list(scored.neutralized[:10]),
        disqualified=list(scored.disqualified[:10]),
    )


def _load_instance(task: Task, instances_dir: Path | None) -> _InstanceMode | None:
    """The benchmark instance for a task that names one, and the spec it is run under.

    `tasks.instance_id` is a database value, so the file is found with `load_instance_by_id`
    (which validates the id and goes through `resolve_within`), never by building a path by
    hand. The `RepoSpec`'s key is the instance id: it feeds the image tag, so one instance is
    one environment.
    """
    if task.instance_id is None:
        return None
    if instances_dir is None:
        raise StageFailed(
            "instance",
            f"task names benchmark instance {task.instance_id!r} but no instances directory was supplied",
        )
    instance = load_instance_by_id(instances_dir, task.instance_id)
    # `plain_spec()`, not `.spec`: the loaded mapping is frozen all the way down and
    # `spec_from_mapping` accepts only lists and dicts.
    return _InstanceMode(instance, spec_from_mapping(instance.instance_id, instance.plain_spec()))


def _agent_stop_from(exc: BaseException) -> StopReason | None:
    """The stop an agent-caused exception stands for, or None for one that is repolace's.

    The graph catches these two itself and returns a stop reason, so they should not reach
    here. They are mapped anyway because the rule is that no agent-caused stop may fail the
    task, and a runner that lets one escape would otherwise turn a budget stop into a harness
    error that leaves the benchmark's denominator.
    """
    if isinstance(exc, BudgetExceeded):
        return _BUDGET_STOPS[exc.limit]
    if isinstance(exc, LLMCallError):
        return StopReason.LLM_ERROR
    return None


async def _run_agent(runner: AgentRunner, deps: AgentDeps, scorer: AttemptScorer) -> AgentResult:
    try:
        result = await runner(deps)
    except Exception as exc:
        stop = _agent_stop_from(exc)
        if stop is None:
            raise
        log.warning("pipeline.agent.stopped_by_exception", stop_reason=stop.value, error=str(exc)[:300])
        return AgentResult(
            stop_reason=stop, summary=None, attempts=scorer.attempts, steps=0, last_attempt=scorer.last_record
        )
    if not isinstance(result, AgentResult):
        raise TypeError(f"an agent must return an AgentResult, got {type(result).__name__}")
    return result


def _agent_budget(supplied: TaskBudget | None) -> TaskBudget:
    """The gateway budget for the agent stage, with its wall clock started *now*.

    `TaskBudget` stamps `started_at` when it is built, and its wall cap (an hour by default) is
    meant to bound the agent. If the scope opened at the top of the task, the clone, the index (which
    can wait twenty minutes for another task's lock) and the baseline suite would eat the cap before
    the first model call, the agent would stop `budget_wall` having done nothing, and an instrument
    limit would be charged to it as a FAILED score. Only the agent makes gateway calls, so only its
    stage is inside the scope.

    A budget the caller supplied is honoured -- its cap, its calls and its spend -- and its clock is
    restarted here, because a caller builds it before the task runs and cannot know how long the
    stages before the agent will take.
    """
    budget = supplied if supplied is not None else TaskBudget()
    budget.started_at = budget.clock()
    return budget


async def _run_stages(
    state: AsyncSession,
    session_factory,
    github: GithubClient,
    task: Task,
    repo: RegisteredRepo,
    backend: SandboxBackend,
    specs: Mapping[str, RepoSpec],
    seams: _Seams,
    progress: _Progress,
) -> RunResult:
    async with _stage("instance"):
        mode = _load_instance(task, seams.instances_dir)
    instance = mode.instance if mode is not None else None

    async with _stage("preflight"):
        await _preflight(github, repo)

    async with _stage("embedder"):
        # Before the clone, so a several-hundred-MB first-run model download is
        # its own stage and never looks like a slow clone.
        log.info("pipeline.embedder.warming")
        await asyncio.to_thread(seams.embedder_warmup)
        log.info("pipeline.embedder.warmed")

    provider = installation_token_provider(github, repo.installation_id)

    # An exit stack so the clone itself happens *inside* the "clone" stage --
    # the work is in __aenter__, so constructing the context manager and
    # entering it must not be separated, or a clone failure gets mislabelled.
    async with AsyncExitStack() as stack:
        async with _stage("clone"):
            workspace = await stack.enter_async_context(
                seams.workspace_factory(repo.owner, repo.name, task.target_branch, token_provider=provider)
            )
            if instance is not None:
                # A full clone carries every branch the remote has, and a benchmark remote
                # accumulates earlier runs' agent branches. Their names are the cheap route to a
                # previous PASSED patch; removing them is one layer, not the control (the toolbox
                # refuses `.git`, and the export carries none).
                await workspace.prune_remote_refs(keep=[task.target_branch])
                if workspace.base_sha != instance.base_commit:
                    log.warning(
                        "pipeline.instance.base_mismatch",
                        branch_tip=workspace.base_sha,
                        instance_base_commit=instance.base_commit,
                    )

        async with _stage("index"):
            chunk_count = await _index(
                session_factory, repo, workspace.path, workspace.base_sha, seams.embedding_strategy
            )
        if chunk_count == 0:
            raise StageFailed(
                "index",
                "repo indexed to 0 chunks; indexing is Python-only, so a repo in "
                "another language retrieves nothing (see CLAUDE.md)",
            )

        async with _stage("retrieve"):
            query = task.issue_title.strip() or f"issue {task.issue_number}"
            retrieved = await _retrieve(session_factory, repo.id, query, seams.embedding_strategy)
        if not retrieved:
            raise StageFailed(
                "retrieve",
                f"no chunks matched {query!r} across {chunk_count} indexed chunks",
            )
        log.info(
            "pipeline.retrieve.done",
            result_count=len(retrieved),
            top=retrieved[0].location,
            top_score=round(retrieved[0].rrf_score, 5),
        )

        spec = mode.spec if mode is not None else resolve_spec(specs, repo.full_name)
        verifier = Verifier(
            backend, spec, task.id, overlay=instance.overlay_bytes() if instance is not None else None
        )

        # Before the agent branch, and before any edit: this is what "the tests
        # pass" is measured against. Without it a repo with pre-existing
        # failures scores as a failure whatever the agent did, and "the suite
        # passes" is an unfalsifiable claim.
        async with _stage("verify_baseline"):
            baseline, _ = await run_scored_suite(verifier, workspace, BASELINE_ATTEMPT)
        async with _stage("record_baseline"):
            await record_test_run(session_factory, task.id, BASELINE_ATTEMPT, workspace.base_sha, baseline)
        async with _stage("baseline_files"):
            baseline_files = await workspace.baseline_files()

        # Known from the baseline alone, so decided before the agent is paid to run: an instance
        # that cannot be scored is inadmissible whatever the agent does, and an agent run against
        # one would burn its whole budget for a result that is thrown away.
        skip = _inadmissible_instance(baseline, instance)
        if skip is not None:
            return await _complete_without_agent(state, task, *skip)

        async with _stage("branch"):
            branch = await workspace.start_agent_branch(task.issue_number, task.id)

        runner = seams.agent or StubAgent(
            StubEditRequest(
                task_id=task.id,
                issue_number=task.issue_number,
                issue_title=task.issue_title,
                issue_url=task.issue_url,
                target_branch=task.target_branch,
                base_sha=workspace.base_sha,
                indexed_chunk_count=chunk_count,
                retrieved=retrieved,
            )
        )
        scorer = AttemptScorer(
            verifier=verifier, workspace=workspace, record=partial(record_test_run, session_factory, task.id)
        )
        progress.scorer = scorer

        async with _stage("agent"):
            # The gateway scope: every model call is attributable to this task and charged to its
            # budget. A contextvar, not a parameter, so the agent cannot forget it.
            with task_scope(task.id, _agent_budget(seams.budget)):
                deps = await _build_deps(
                    session_factory, task, repo, workspace, verifier, scorer, seams, baseline, baseline_files, retrieved
                )
                result = await _run_agent(runner, deps, scorer)
        progress.result = result
        log.info(
            "pipeline.agent.done",
            runner=type(runner).__name__,
            stop_reason=result.stop_reason.value,
            attempts=result.attempts,
            steps=result.steps,
        )

        last = result.last_attempt
        async with _stage("review"):
            # `last_attempt` is the only scored state. HEAD may be ahead of it -- `run_python` and
            # `run_tests` leave checkpoint commits no suite ever ran against -- and everything read
            # from here on (changed files, the diff, the squash, the push) must be the scored
            # commit, or the PR would carry edits under a verdict that belongs to an earlier one.
            # With nothing scored the target is the base: there is no patch to propose.
            target = last.commit_sha if last is not None else workspace.base_sha
            if await workspace.repo.head_sha() != target:
                log.info("pipeline.review.rewind", to=target)
                await workspace.rewind_to(target)
            changed = await workspace.changed_files()
            diff = await workspace.review_diff()
        progress.changed_files, progress.patch_diff, progress.patch_sha = changed, diff, target
        log.info("pipeline.review.done", changed_files=changed, diff_bytes=len(diff))

        scored, verdict = _judge(baseline, baseline_files, instance, result, changed)
        _log_score(scored)

        decision = pr_decision(
            benchmark=instance is not None,
            has_change=bool(changed),
            scored=scored,
            verdict=verdict,
            result=result,
            open_pr_on_failure=task.open_pr_on_failure,
            open_pr_allowed=seams.open_pr,
        )
        log.info("pipeline.gate", open=decision.open, reason=decision.reason)

        # Best effort: a database blip here must not turn a finished agent run into a harness error.
        cost = await _best_effort_cost(session_factory, task.id)
        pull_request = None
        squashed: str | None = None
        if decision.open:
            facts = PrFacts(
                task_id=task.id,
                issue_number=task.issue_number,
                issue_title=task.issue_title,
                instance_id=task.instance_id,
                plumbing_only=isinstance(runner, StubAgent),
                summary=result.summary,
                changed_files=tuple(changed),
                baseline=baseline,
                final=last.result if last is not None else SuiteResult(),
                verdict=verdict,
                scored=scored,
                expected_fail_to_pass=instance.fail_to_pass if instance is not None else None,
                attempts=result.attempts,
                stop_reason=result.stop_reason.value,
                model=await first_model(session_factory, task.id),
                cost_usd=cost,
                retrieved=retrieved,
            )
            async with _stage("squash"):
                squashed = await workspace.squash(commit_message(facts))
            if squashed is None:
                # The gate saw a change and the squash saw none. Every route there is a bug or a
                # race in how "change" was read twice, and neither is the agent's -- but failing
                # the task for it would be wrong the other way round, because the usual way to
                # reach it is an agent that left nothing behind. So: no PR, and the task
                # completes with the scorer's outcome, like any other task that opened none.
                log.warning("pipeline.squash.no_net_change", changed_files=changed)
                decision = PrDecision(False, "the branch has no net change against the base commit")
            else:
                progress.patch_sha = squashed
                async with _stage("push"):
                    await workspace.push()
                async with _stage("pr"):
                    pull_request = await github.create_pull_request(
                        repo.installation_id,
                        repo.owner,
                        repo.name,
                        head=branch,
                        base=task.target_branch,
                        title=pr_title(facts),
                        body=render_pr_body(facts),
                    )
                progress.pr_number, progress.pr_url = pull_request.number, pull_request.html_url

        # The sha of what the task produced: the squashed commit if a PR was opened (the commit
        # on GitHub), else the scored commit at HEAD.
        patch_sha = squashed or await workspace.repo.head_sha()
        progress.patch_sha = patch_sha

    status = TaskStatus.PR_OPENED if pull_request is not None else TaskStatus.COMPLETED
    async with _stage("finalize"):
        await write_terminal(
            state,
            task.id,
            status=status,
            outcome=scored.outcome,
            score_reason=scored.reason,
            error_message=None,
            agent_stop_reason=result.stop_reason.value,
            retry_count=max(0, result.attempts - 1),
            cost_usd=cost,
            patch_sha=patch_sha,
            patch_diff=diff,
            changed_files=changed,
            pr_number=pull_request.number if pull_request is not None else None,
            pr_url=pull_request.html_url if pull_request is not None else None,
        )
        await state.commit()
    log.info(
        "pipeline.task.pr_opened" if pull_request is not None else "pipeline.task.completed",
        outcome=scored.outcome.value if scored.outcome else None,
        inadmissible=scored.inadmissible,
        reason=scored.reason,
        stop_reason=result.stop_reason.value,
        pr_number=pull_request.number if pull_request is not None else None,
        pr_url=pull_request.html_url if pull_request is not None else None,
    )
    return RunResult(
        task.id,
        status,
        pull_request.number if pull_request is not None else None,
        pull_request.html_url if pull_request is not None else None,
        outcome=scored.outcome,
        score_reason=scored.reason,
        cost_usd=cost,
        attempts=result.attempts,
        submitted=result.submitted,
        stop_reason=result.stop_reason.value,
        pr_gate_reason=decision.reason,
    )


def _inadmissible_instance(baseline: SuiteResult, instance: InstanceSpec | None) -> tuple[Score, str] | None:
    """The `(score, gate reason)` for a task that must not run an agent, or None if it may.

    Two things are knowable from the baseline alone. An **unusable baseline** (the environment
    would not build, the suite crashed) makes the instance inadmissible whatever the agent does,
    and the score says so in the scorer's own words. And a curated fail-to-pass test that **was
    not red at the base commit** makes it inadmissible too: every other condition of `score` is
    then met by a patch that changes nothing, so a no-op edit would score PASSED.
    """
    if baseline.error:
        # The scorer's own sentence for it, so the two cannot drift: with no changed files and
        # nothing else to disqualify, the baseline check is the first one that fires.
        return score(baseline, SuiteResult(), []), "baseline unscoreable"
    if instance is not None:
        not_red = expected_not_red(baseline, instance.fail_to_pass)
        if not_red:
            return (
                Score(
                    outcome=None,
                    reason=f"expected fail-to-pass not red at baseline: {', '.join(not_red[:5])}",
                    inadmissible=True,
                ),
                "instance inadmissible: a curated fail-to-pass test was not failing at the base commit",
            )
    return None


async def _complete_without_agent(state: AsyncSession, task: Task, scored: Score, gate_reason: str) -> RunResult:
    """Finish a task whose instance cannot be scored, without having run an agent.

    `completed` with no outcome: nothing broke, nothing was measured. The agent columns stay
    `None` ("never reached the agent"), which is not the same as an agent that did nothing.
    """
    _log_score(scored)
    await write_terminal(
        state,
        task.id,
        status=TaskStatus.COMPLETED,
        outcome=None,
        score_reason=scored.reason,
        error_message=None,
        agent_stop_reason=None,
        retry_count=0,
        cost_usd=None,
        patch_sha=None,
        patch_diff=None,
        changed_files=None,
        pr_number=None,
        pr_url=None,
    )
    await state.commit()
    log.info("pipeline.task.completed", outcome=None, inadmissible=True, reason=scored.reason, gate=gate_reason)
    return RunResult(
        task.id, TaskStatus.COMPLETED, outcome=None, score_reason=scored.reason, pr_gate_reason=gate_reason
    )


def _judge(
    baseline: SuiteResult,
    baseline_files: tuple[str, ...],
    instance: InstanceSpec | None,
    result: AgentResult,
    changed: list[str],
) -> tuple[Score, Verdict]:
    """The score and the PR-gate verdict, both from the last *scored* attempt and nothing else.

    **The outcome never depends on `submitted`.** An agent that ran out of steps with a passing
    last attempt scored PASSED; only whether to *open a PR* may depend on how it stopped, and
    that is the gate's decision, not this one's.

    With no scored attempt there is nothing to compare, and the outcome is FAILED: the agent
    was given the issue and produced no result a suite ran against. Not inadmissible -- that
    word is for an instrument that failed, and this instrument worked.

    **What "the two runs are comparable" rests on.** Every pytest run -- the baseline, each
    attempt and each probe -- is started with the same argv, which since the sandbox stream
    carries `--rootdir=/repo` and `--continue-on-collection-errors`. The first pins one node-id
    space, so an id in the baseline names the same test in an attempt (without it a different
    set of targets changes the rootdir and renames every id). The second makes a module that
    stops importing lose its tests *without aborting the session*, so it is reported as a
    collection failure the verdict names, not as an unscoreable run. And `fingerprint_changed`
    compares the rootdir, the watched ini options and the registered plugins between the two,
    which is what refuses a comparison the argv alone could not have guaranteed.
    """
    last = result.last_attempt
    if last is None:
        reason = (
            "agent produced no change"
            if result.stop_reason is StopReason.NO_CHANGE
            else f"no scored attempt: {result.stop_reason.value}"
        )
        return Score(outcome=TaskOutcome.FAILED, reason=reason), Verdict(ok=False, reason=reason)

    scored = score(
        baseline,
        last.result,
        changed,
        baseline_files=baseline_files,
        # The curated ground truth, for a benchmark task. Without it the rule degrades to "some
        # baseline failure went green", which `score` itself flags as not evidence about *this*
        # issue; with it every PASSED reason says how many expected tests pass.
        expected_fail_to_pass=instance.fail_to_pass if instance is not None else None,
        attempt_infrastructure_error=last.infrastructure_error,
    )
    verdict = agent_verdict(
        baseline,
        last.result,
        changed,
        baseline_files=baseline_files,
        attempt_infrastructure_error=last.infrastructure_error,
    )
    return scored, verdict


async def _build_deps(
    session_factory,
    task: Task,
    repo: RegisteredRepo,
    workspace: TaskWorkspace,
    verifier: Verifier,
    scorer: AttemptScorer,
    seams: _Seams,
    baseline: SuiteResult,
    baseline_files: tuple[str, ...],
    retrieved: tuple[RetrievedChunk, ...],
) -> AgentDeps:
    """Everything the agent is given. The model client is the caller's; the rest is built here."""
    limits = AgentLimits()
    # From the clean tree, before the agent has edited anything.
    hits = await asyncio.to_thread(
        lambda: tuple(search_hit(chunk, workspace.path, limits.max_context_snippet_lines) for chunk in retrieved)
    )
    tools: ToolBox | None = None
    if seams.llm is not None:
        # The stub and gold runners have no model and so no tools. A model-driven agent gets the
        # nine tools, wired to this task's own resources and nothing else.
        tools = build_toolbox(
            build_tool_context(
                workspace=workspace,
                verifier=verifier,
                search=make_search(
                    session_factory,
                    repo.id,
                    workspace.path,
                    strategy=seams.embedding_strategy,
                    max_lines=limits.max_context_snippet_lines,
                ),
                hidden_paths=verifier.hidden_paths,
                baseline=baseline,
            )
        )
    return AgentDeps(
        llm=seams.llm,
        tools=tools,
        checkout=workspace.path,
        issue=IssueContext(
            number=task.issue_number,
            title=task.issue_title,
            body=task.issue_body,
            url=task.issue_url,
            # `instance_id is not None` is what means benchmark mode: it flips the feedback filter
            # into its fail-closed overlay mode even for an overlay that happens to be empty.
            instance_id=task.instance_id,
        ),
        retrieved=hits,
        repo_overview=repo_overview(baseline_files),
        baseline=baseline,
        baseline_files=baseline_files,
        hidden_paths=verifier.hidden_paths,
        verify_attempt=scorer.verify_attempt,
        changed_files=workspace.changed_files,
        limits=limits,
    )


async def run_task(
    task_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    github: GithubClient,
    backend: SandboxBackend,
    specs: Mapping[str, RepoSpec] | None = None,
    *,
    agent: AgentRunner | None = None,
    llm: LLMClientLike | None = None,
    workspace_factory: WorkspaceFactory = task_workspace,
    embedder_warmup: Callable[[], object] = get_embedder,
    instances_dir: Path | None = None,
    budget: TaskBudget | None = None,
    open_pr: bool = True,
    embedding_strategy: str = DEFAULT_STRATEGY,
) -> RunResult:
    """Run one task. `backend` and `specs` are the caller's, like everything else.

    `backend` is injected rather than constructed here for the same reason the
    engine and the client are: the CLI builds a `DockerBackend`, Phase 2's
    worker will build one per process, and a test can pass a fake and exercise
    the whole pipeline without a daemon.

    The keyword arguments are the seams, and every default is today's behaviour:

    * `agent` -- the `AgentRunner`. None runs the plumbing stub.
    * `llm` -- the model client the agent is given. A model-driven agent also gets the
      toolbox; the stub and gold runners (no `llm`) do not.
    * `workspace_factory` -- how the checkout is made; a test passes one that clones a local remote.
    * `embedder_warmup` -- called once before the clone, so a model download is its own stage.
    * `instances_dir` -- where benchmark instances live; required only for a task with an `instance_id`.
    * `budget` -- the gateway cap for the agent; None is the default cap. Its wall clock starts when
      the agent stage does, not when the task does (see `_agent_budget`).
    * `open_pr` -- False never opens one, whatever the gate would say.
    * `embedding_strategy` -- how the index is built and queried.
    """
    seams = _Seams(
        agent=agent,
        llm=llm,
        workspace_factory=workspace_factory,
        embedder_warmup=embedder_warmup,
        instances_dir=instances_dir,
        budget=budget,
        open_pr=open_pr,
        embedding_strategy=embedding_strategy,
    )
    progress = _Progress()
    async with session_factory() as state:
        task, repo = await _claim(state, task_id)

        # The context-manager form, so the binding unwinds. `bind_contextvars`
        # is never unbound anywhere in this codebase and bleeds across tasks.
        with structlog.contextvars.bound_contextvars(
            task_id=str(task_id), repo=repo.full_name, issue_number=task.issue_number
        ):
            try:
                return await _run_stages(
                    state, session_factory, github, task, repo, backend, specs or {}, seams, progress
                )
            except StageFailed as exc:
                return await _fail(state, session_factory, task_id, exc.stage, exc.message, progress)
            except Exception as exc:
                return await _fail(state, session_factory, task_id, "unknown", _describe(exc), progress)
            except BaseException as exc:
                # Ctrl-C or cancellation would otherwise leave the task
                # `running` forever with nothing to reap it.
                await _fail(state, session_factory, task_id, "interrupted", type(exc).__name__, progress)
                raise
