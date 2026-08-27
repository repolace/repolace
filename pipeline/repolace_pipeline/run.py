"""One task, start to finish.

`run_task` takes its resources as parameters and owns none of them -- no
`argv`, no engine, no logging setup, no `sys.exit`. That is what lets Phase 2
lift the body into a Celery task unchanged: the worker brings a process-level
engine and client, the CLI brings per-invocation ones, and this stays the same.

The Verify stage is deliberately absent. Its sandbox isolation mechanism is an
open decision in CLAUDE.md, so nothing here runs the repo's test suite, nothing
is scored, and no `task_test_runs` row is written.
"""

import asyncio
import subprocess
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx
import structlog
from sqlalchemy import func, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_shared.db.models import RegisteredRepo, Task, TaskStatus
from repolace_shared.git import GitError, installation_token_provider, redact, task_workspace
from repolace_shared.github.client import GithubClient
from retrieval.embed import get_embedder
from retrieval.index import RepoIndexInProgress, get_current_chunk_count, reindex_if_stale
from retrieval.retrieve import hybrid_search

from repolace_pipeline.context import RetrievedChunk
from repolace_pipeline.edit import (
    StubEditRequest,
    apply_stub_edit,
    commit_message,
    pr_title,
    render_pr_body,
)
from repolace_pipeline.errors import PipelineError, StageFailed, TaskNotClaimable, TaskNotFound

log = structlog.get_logger()

RETRIEVE_LIMIT = 8
MAX_ERROR_CHARS = 4000
REQUIRED_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


@dataclass(frozen=True)
class RunResult:
    task_id: uuid.UUID
    status: TaskStatus
    pr_number: int | None = None
    pr_url: str | None = None
    error_message: str | None = None


def _describe(exc: BaseException) -> str:
    """Turn an exception into something readable in a `tasks.error_message` column."""
    if isinstance(exc, RepoIndexInProgress):
        return "another task is indexing this repo; retry shortly"
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


async def _fail(state: AsyncSession, task_id: uuid.UUID, stage: str, message: str) -> RunResult:
    """Record the failure without letting a write problem replace the reason for it."""
    detail = redact(f"{stage}: {message}")[:MAX_ERROR_CHARS]
    log.error("pipeline.task.failed", stage=stage, error=detail)
    try:
        # Roll back first: a mid-pipeline DB error would otherwise leave the
        # session dirty and make the failure record itself unwritable.
        await state.rollback()
        await state.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status=TaskStatus.FAILED, error_message=detail, completed_at=func.now())
        )
        await state.commit()
    except SQLAlchemyError as exc:
        log.error("pipeline.state.write_failed", error=str(exc))
    return RunResult(task_id, TaskStatus.FAILED, error_message=detail)


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


async def _index(session_factory, repo: RegisteredRepo, repo_path, base_sha: str) -> int:
    """Index on a session of its own, and report how many chunks the repo *has*.

    `reindex_if_stale` commits and rolls back the session it is given, because
    the advisory lock making it single-flight is transaction-scoped. Sharing the
    pipeline's session would let a reindex commit half-written task state, or
    discard it on failure.
    """
    async with session_factory() as index_db:
        written = await reindex_if_stale(index_db, repo.id, repo_path, base_sha)
        # Guard on chunks *present*, not chunks *written*: the latter is 0 for
        # an already-current index, which would fail every second task on a repo.
        present = await get_current_chunk_count(index_db, repo.id)
    log.info("pipeline.index.done", chunks_written=written, chunks_present=present)
    return present


async def run_task(
    task_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    github: GithubClient,
) -> RunResult:
    async with session_factory() as state:
        task, repo = await _claim(state, task_id)

        # The context-manager form, so the binding unwinds. `bind_contextvars`
        # is never unbound anywhere in this codebase and bleeds across tasks.
        with structlog.contextvars.bound_contextvars(
            task_id=str(task_id), repo=repo.full_name, issue_number=task.issue_number
        ):
            try:
                return await _execute(state, session_factory, github, task, repo)
            except StageFailed as exc:
                return await _fail(state, task_id, exc.stage, exc.message)
            except Exception as exc:
                return await _fail(state, task_id, "unknown", _describe(exc))
            except BaseException as exc:
                # Ctrl-C or cancellation would otherwise leave the task
                # `running` forever with nothing to reap it.
                await _fail(state, task_id, "interrupted", type(exc).__name__)
                raise


async def _execute(
    state: AsyncSession,
    session_factory,
    github: GithubClient,
    task: Task,
    repo: RegisteredRepo,
) -> RunResult:
    async with _stage("preflight"):
        await _preflight(github, repo)

    async with _stage("embedder"):
        # Before the clone, so a several-hundred-MB first-run model download is
        # its own stage and never looks like a slow clone.
        log.info("pipeline.embedder.warming")
        await asyncio.to_thread(get_embedder)
        log.info("pipeline.embedder.warmed")

    provider = installation_token_provider(github, repo.installation_id)

    # An exit stack so the clone itself happens *inside* the "clone" stage --
    # the work is in __aenter__, so constructing the context manager and
    # entering it must not be separated, or a clone failure gets mislabelled.
    async with AsyncExitStack() as stack:
        async with _stage("clone"):
            workspace = await stack.enter_async_context(
                task_workspace(repo.owner, repo.name, task.target_branch, token_provider=provider)
            )

        async with _stage("index"):
            chunk_count = await _index(session_factory, repo, workspace.path, workspace.base_sha)
        if chunk_count == 0:
            raise StageFailed(
                "index",
                "repo indexed to 0 chunks; indexing is Python-only, so a repo in "
                "another language retrieves nothing (see CLAUDE.md)",
            )

        async with _stage("retrieve"):
            query = task.issue_title.strip() or f"issue {task.issue_number}"
            results = await hybrid_search(state, repo.id, query, limit=RETRIEVE_LIMIT)
            retrieved = tuple(RetrievedChunk.from_result(result) for result in results)
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

        request = StubEditRequest(
            task_id=task.id,
            issue_number=task.issue_number,
            issue_title=task.issue_title,
            issue_url=task.issue_url,
            target_branch=task.target_branch,
            base_sha=workspace.base_sha,
            indexed_chunk_count=chunk_count,
            retrieved=retrieved,
        )

        async with _stage("branch"):
            branch = await workspace.start_agent_branch(task.issue_number, task.id)

        async with _stage("edit"):
            edited = await asyncio.to_thread(apply_stub_edit, workspace.path, request)
        log.info("pipeline.edit.applied", file=str(edited.relative_to(workspace.path)))

        async with _stage("commit"):
            attempt = await workspace.record_attempt(commit_message(request))
        if attempt is None:
            raise StageFailed(
                "commit",
                "the stub edit produced no committable change; check whether the "
                "repo's .gitignore covers the edited file",
            )

        async with _stage("review"):
            changed = await workspace.changed_files()
            diff = await workspace.review_diff()
        # Verify is skipped, so this is observation only. Logged explicitly so
        # the record cannot be mistaken for a scored result later.
        log.info(
            "pipeline.review.unscored",
            changed_files=changed,
            diff_bytes=len(diff),
            detail="no test suite was run; nothing scored",
        )

        async with _stage("squash"):
            squashed = await workspace.squash(commit_message(request))
        if squashed is None:
            raise StageFailed("squash", "agent branch has no net change against the base commit")

        async with _stage("push"):
            await workspace.push()

        async with _stage("pr"):
            pull_request = await github.create_pull_request(
                repo.installation_id,
                repo.owner,
                repo.name,
                head=branch,
                base=task.target_branch,
                title=pr_title(request),
                body=render_pr_body(request),
            )

    await state.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(
            status=TaskStatus.PR_OPENED,
            pr_number=pull_request.number,
            pr_url=pull_request.html_url,
            completed_at=func.now(),
        )
    )
    await state.commit()
    log.info("pipeline.task.pr_opened", pr_number=pull_request.number, pr_url=pull_request.html_url)
    return RunResult(task.id, TaskStatus.PR_OPENED, pull_request.number, pull_request.html_url)
