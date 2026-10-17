"""A target branch that moved while the task ran: merge it in, or say why not.

The per-task clone keeps the window small but not zero: by the time the agent has a scored
commit, someone may have pushed to the branch the PR targets, and the PR would then conflict.
`merge_moved_target` is the step between "the gate would open a PR" and the squash, and it is the
only caller of the merge primitives (`shared/repolace_shared/git/`) and of the conflict resolver
(`repolace_agents.resolver`). `run.py` only calls it and acts on the `TargetMerge` it returns.

**The flow** (decided; `.claude/rules/git-and-pr-flow.md`):

1. Fetch the target and compare its tip with the base. Not moved: nothing changes.
2. Moved: `merge_tree` says whether it would conflict, without touching the tree.
3. Clean: `merge_no_commit`, `commit_merge`, `advance_base`.
4. Conflicts, every one plain text (both-modified or both-added): `merge_no_commit`, then the
   resolver, with the host's own re-check as its `check` and the main agent's baseline-aware
   write guard as its `is_protected`; on `RESOLVED`, `commit_resolution`, then `advance_base`.
5. After any merge the old score describes a tree that no longer exists, so the merged tree is
   scored as a new attempt (`AttemptScorer.verify_attempt`) and the caller judges *that*. The
   pre-merge attempt stays in `task_test_runs`. One more suite run is the price of an honest score.
6. Anything else (a conflict that is not plain text, a resolver that stopped short, a resolution
   the host refused): `abort_merge`, and the task ends `conflicting` with a short reason and no PR.

**What is not a conflict.** A resolver that raised because the provider refused us
(`LLMCallError.provider_refused`), a git command that failed, a `.git/config` the primitives do
not trust: each propagates, and the task fails as a harness error like anywhere else. A
conflict is something about the two histories; those are about repolace.

**The reason is short and names no path.** It is stored in `tasks.conflict_reason` and shown
beside a benchmark number, and a conflicted path is repository data a person did not choose to
publish; counts and conflict kinds say what happened without it.
"""

import os
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path

import structlog
from repolace_agents.contracts import AttemptRecord, IssueContext, LLMClientLike
from repolace_agents.resolver import ResolverDeps, ResolverLimits, ResolverResult, ResolverStop
from repolace_gateway.budget import TaskBudget, task_scope
from repolace_shared.git import GitError, TaskWorkspace, conflict_kind
from repolace_shared.git.repo import CONFLICT_BOTH_ADDED, CONFLICT_BOTH_MODIFIED
from repolace_shared.git.workspace import MAX_RESOLUTION_PATHS
from repolace_shared.paths import PathEscapesRoot, resolve_within

from repolace_pipeline.attempts import AttemptScorer

log = structlog.get_logger()

#: The resolver as the pipeline calls it: `repolace_agents.resolver.resolve_conflicts` in production.
Resolver = Callable[[ResolverDeps], Awaitable[ResolverResult]]

#: The resolver's own budget: half of what the agent has left, never more than these.
RESOLVER_MAX_USD = Decimal("0.50")
RESOLVER_MAX_WALL_SECONDS = 600.0

#: The kinds a text resolver can edit. Everything else (delete/modify, mode, symlink, gitlink)
#: has no text to reconcile, so it is refused before a model is asked.
_PLAIN_TEXT_KINDS = frozenset({CONFLICT_BOTH_MODIFIED, CONFLICT_BOTH_ADDED})

#: A conflict marker line, as git writes them (zdiff3 adds the `|||||||` base line). The same
#: rule `TaskWorkspace.commit_resolution` applies, restated because that one is private: the
#: resolver's check must not pass a file the commit will then refuse. Pinned by a test that
#: runs both over the same files.
_MARKER_LINE = re.compile(rb"^(<{7}|>{7}|\|{7})( .*)?$|^={7}$")

#: A resolved file this large is not a source file a resolver edited (`commit_resolution`'s cap).
_MAX_CHECKED_BYTES = 10_000_000
#: How many marker lines one file's problem list names before it says "and more".
_MAX_MARKERS_NAMED = 5


class MergeStatus(str, Enum):
    #: The target had not moved, or its tip is already contained in HEAD: nothing was merged.
    UNCHANGED = "unchanged"
    #: The tip was merged (cleanly or by the resolver), the base advanced, the result scored.
    MERGED = "merged"
    #: The merge could not be completed and was aborted; HEAD is the pre-merge commit.
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class TargetMerge:
    status: MergeStatus
    #: The target's tip as fetched. None only when nothing was fetched.
    tip: str | None = None
    #: MERGED only: the scored attempt of the merged tree. None when the merged tree has no
    #: change against the new base, which means the target already contains the change.
    attempt: AttemptRecord | None = None
    #: CONFLICTING only: why, in one short sentence naming no path.
    reason: str | None = None
    #: The resolver's result, when it ran.
    resolver: ResolverResult | None = None


def resolver_budget(agent: TaskBudget) -> TaskBudget:
    """The resolver's own `TaskBudget`: half of the agent's remaining dollars and wall clock, capped.

    Its own budget rather than the agent's, so a resolver cannot spend what the agent was
    given and the agent's spend cannot starve it below half of what is left. The call limit is
    half of the agent's remaining calls, at most twice the resolver's step cap (a failed call
    counts). The clock is the agent budget's, so a test that moves one moves both.
    """
    usd = min(agent.remaining_usd / 2, RESOLVER_MAX_USD)
    wall = min(agent.remaining_wall_seconds / 2, RESOLVER_MAX_WALL_SECONDS)
    calls = min(max(agent.max_calls - agent.calls, 0) // 2, 2 * ResolverLimits().max_steps)
    return TaskBudget(max_usd=usd, max_calls=calls, max_wall_seconds=wall, clock=agent.clock)


def _marker_lines(data: bytes) -> list[int]:
    return [
        number
        for number, line in enumerate(data.split(b"\n"), start=1)
        if _MARKER_LINE.match(line.rstrip(b"\r"))
    ]


def file_problems(root: Path, path: str) -> list[str]:
    """What is still wrong with one conflicted file, as sentences the resolver is shown.

    Host-written, but each names a repository path, so the resolver frames them as data.
    """
    try:
        resolved = resolve_within(root, path)
    except PathEscapesRoot:
        return [f"{path} is not a regular file inside the checkout"]
    if not resolved.exists():
        return [f"{path} no longer exists; a resolution keeps the file and edits it"]
    if not resolved.is_file():
        return [f"{path} is not a regular file"]
    if resolved.stat().st_size > _MAX_CHECKED_BYTES:
        return [f"{path} is too large to check for conflict markers"]
    lines = _marker_lines(resolved.read_bytes())
    if not lines:
        return []
    named = ", ".join(str(n) for n in lines[:_MAX_MARKERS_NAMED])
    more = f" and {len(lines) - _MAX_MARKERS_NAMED} more" if len(lines) > _MAX_MARKERS_NAMED else ""
    return [f"{path} still holds conflict marker lines (line {named}{more})"]


def make_check(workspace: TaskWorkspace, conflicts: Collection[str]) -> Callable[[Sequence[str]], Awaitable[list[str]]]:
    """`ResolverDeps.check`: the host's re-check after each submit, never the model's word.

    Two questions. Does any named file still hold a marker line (or is it gone, or not a
    file)? And is anything unmerged that was not in the conflicted set the resolver was given?
    The second cannot be caused by the resolver's tools, which touch no index; it is asked so a
    check that passes means the same thing `commit_resolution` will then require.
    """
    allowed = {os.fsencode(path) for path in conflicts}

    async def check(paths: Sequence[str]) -> list[str]:
        root = workspace.path.resolve()
        problems: list[str] = []
        unmerged = {entry.path for entry in await workspace.repo.unmerged_files()}
        stray = sorted(unmerged - allowed)
        if stray:
            problems.append(f"{len(stray)} unmerged path(s) are outside the conflicted files")
        for path in paths:
            problems.extend(file_problems(root, path))
        return problems

    return check


def _merge_message(target_branch: str, tip: str) -> str:
    """The merge commit's message. Host facts only: no issue text, no resolver summary.

    It never reaches a PR (the squash replaces it), but it is a commit in a tree that later
    runs may clone, so it carries nothing a model wrote.
    """
    return f"Merge {target_branch} at {tip[:12]} into the agent branch"


def _conflicting(tip: str, reason: str, resolver: ResolverResult | None = None) -> TargetMerge:
    log.warning("pipeline.conflicts.unresolved", tip=tip, reason=reason)
    return TargetMerge(MergeStatus.CONFLICTING, tip=tip, reason=reason, resolver=resolver)


async def _resolve(
    workspace: TaskWorkspace,
    *,
    head: str,
    tip: str,
    message: str,
    task_id,
    llm: LLMClientLike | None,
    resolver: Resolver,
    issue: IssueContext,
    is_protected: Callable[[str], bool],
    agent_budget: TaskBudget,
) -> tuple[str | None, ResolverResult | None]:
    """Settle a conflicted merge in progress. `(None, result)` when committed, else `(reason, result)`.

    The caller aborts the merge on any reason; nothing here leaves the tree half-resolved and
    committed.
    """
    entries = await workspace.repo.unmerged_files()
    if not entries:
        return "the merge stopped without naming a conflicted path", None
    kinds = sorted({conflict_kind(entry.stages) for entry in entries} - _PLAIN_TEXT_KINDS)
    if kinds:
        reason = (
            f"{len(entries)} conflicted path(s) include a {', '.join(kinds)} conflict, which is not "
            f"plain text and is not handed to the resolver"
        )
        return reason, None
    try:
        paths = tuple(entry.path.decode("utf-8") for entry in entries)
    except UnicodeDecodeError:
        return "a conflicted path is not valid UTF-8, so it cannot be handed to the resolver", None
    if llm is None:
        return f"{len(paths)} path(s) conflict and this run has no model to resolve them", None

    deps = ResolverDeps(
        llm=llm,
        checkout=workspace.path,
        conflicts=paths,
        issue=issue,
        base_sha=head,
        theirs_sha=tip,
        check=make_check(workspace, paths),
        is_protected=is_protected,
    )
    with task_scope(task_id, resolver_budget(agent_budget)):
        outcome = await resolver(deps)
    log.info("pipeline.conflicts.resolver", stop=outcome.stop.value, rounds=outcome.rounds, steps=outcome.steps)
    if outcome.stop is not ResolverStop.RESOLVED:
        return f"{len(paths)} path(s) conflict and the resolver stopped: {outcome.stop.value}", outcome
    try:
        await workspace.commit_resolution(paths, message)
    except GitError:
        raise  # git itself failed: repolace's problem, not the resolution's
    except RuntimeError as exc:
        # The host's own check refusing what the resolver left (a marker, a stray edit). The
        # check before RESOLVED makes this unlikely; when it happens it is the safety net
        # working, not repolace breaking. The message names paths, so only its kind is kept.
        log.warning("pipeline.conflicts.resolution_refused", error=str(exc)[:300])
        return f"{len(paths)} path(s) conflict and the host refused the resolution", outcome
    return None, outcome


async def merge_moved_target(
    workspace: TaskWorkspace,
    *,
    scorer: AttemptScorer,
    task_id,
    llm: LLMClientLike | None,
    resolver: Resolver,
    issue: IssueContext,
    is_protected: Callable[[str], bool],
    agent_budget: TaskBudget,
) -> TargetMerge:
    """Bring a moved target branch into HEAD, or report why it cannot be. See the module docstring.

    HEAD must be the last scored commit (the caller's review stage rewinds to it), so what is
    merged is exactly what was scored.
    """
    moved, tip = await workspace.target_moved()
    if not moved:
        log.info("pipeline.conflicts.target_unchanged", tip=tip)
        return TargetMerge(MergeStatus.UNCHANGED, tip=tip)
    repo = workspace.repo
    if await repo.is_ancestor(tip, "HEAD"):
        # A force-push that rewound the target onto a commit we already contain: there is
        # nothing to bring in, and moving the base back would put commits the target no longer
        # has into the squash. Left as it is.
        log.info("pipeline.conflicts.tip_already_contained", tip=tip, base=workspace.base_sha)
        return TargetMerge(MergeStatus.UNCHANGED, tip=tip)

    head = await repo.head_sha()
    preview = await repo.merge_tree("HEAD", tip)
    log.info("pipeline.conflicts.preview", tip=tip, clean=preview.clean, conflicted=len(preview.conflicted_paths))
    if len(preview.conflicted_paths) > MAX_RESOLUTION_PATHS:
        return _conflicting(tip, f"{len(preview.conflicted_paths)} paths conflict, more than can be resolved")

    message = _merge_message(workspace.target_branch, tip)
    outcome: ResolverResult | None = None
    if await repo.merge_no_commit(tip):
        await repo.commit_merge(message)
    else:
        reason, outcome = await _resolve(
            workspace,
            head=head,
            tip=tip,
            message=message,
            task_id=task_id,
            llm=llm,
            resolver=resolver,
            issue=issue,
            is_protected=is_protected,
            agent_budget=agent_budget,
        )
        if reason is not None:
            await repo.abort_merge()
            return _conflicting(tip, f"the target branch moved and {reason}", outcome)

    await workspace.advance_base(tip)
    last = scorer.last_record
    attempt = await scorer.verify_attempt((last.attempt if last is not None else 0) + 1)
    log.info(
        "pipeline.conflicts.merged",
        tip=tip,
        resolved_by_model=outcome is not None,
        scored=attempt is not None,
    )
    return TargetMerge(MergeStatus.MERGED, tip=tip, attempt=attempt, resolver=outcome)
