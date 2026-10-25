"""The target branch moved while the task ran: merge it in before the PR, or say why not.

A task clones the target branch, and minutes or hours later opens a PR against it. If someone
pushed to the target in between, that PR conflicts or, worse, merges cleanly into code the
change was never tested against. `merge_target` is the one step that deals with it, in the
order `.claude/rules/git-and-pr-flow.md` records ("Primitives for a target branch that moved
after the clone") and with the resolver `.claude/rules/agent.md` describes ("Conflict
resolver"). `run.py` calls it after the agent's attempt is scored and the PR gate would open a
PR, and before the squash; it never runs for a benchmark task, whose score must stay on the
instance's curated tree.

**Every way it can end, and who it blames.**

* The target did not move: nothing else is touched (`NOT_MOVED`). This is the common path.
* It moved and the merge is clean, or every conflict is plain text and the resolver reconciled
  it: the merge is committed, `base_sha` advances to the target's tip, and the merged tree is
  scored as a new attempt (`MERGED`). **The old score no longer describes the tree**, so the
  caller judges and gates the new one; the pre-merge attempt stays in `task_test_runs`.
* The merged tree is the target's own tree: someone else already landed this change. The
  merge is undone and there is nothing to propose (`ALREADY_CONTAINED`).
* The merge cannot be made honestly (`CONFLICTING`): a conflict a text resolver has nothing to
  edit in (a deletion, a mode change, a symlink, a submodule), a resolver that stopped short,
  a resolution the host refused, a target that was rewritten rather than moved forward. The
  merge is aborted, so the checkout is back at the scored commit, and the reason is a short
  host-written sentence for `tasks.conflict_reason`. None of these is repolace breaking, so
  none sets `error_message`.
* Anything that *is* repolace breaking raises: a git failure (`GitError`, including the
  untrusted-config refusal), a provider that refused our key or quota
  (`LLMCallError.provider_refused`), an unpriced model, a missing key.

**What the resolver is given** is exactly the conflicted paths, the baseline-aware write guard
the main agent gets (`agent_context.protected_check`), a budget of its own and the host's
re-check below. It never touches git; the host stages and commits through
`TaskWorkspace.commit_resolution`, which checks again.
"""

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path

import structlog
from repolace_agents.contracts import AttemptRecord, IssueContext, LLMClientLike
from repolace_agents.resolver import ResolverDeps, ResolverLimits, ResolverResult, ResolverStop
from repolace_gateway.budget import BudgetExceeded, TaskBudget, task_scope
from repolace_gateway.errors import LLMCallError
from repolace_shared.git import GitError, TaskWorkspace, redact
from repolace_shared.git.repo import CONFLICT_BOTH_ADDED, CONFLICT_BOTH_MODIFIED, TooManyConflicts, conflict_kind
from repolace_shared.git.workspace import MAX_RESOLUTION_PATHS, file_has_marker
from repolace_shared.paths import resolve_within
from verify.protocol import SuiteResult

from repolace_pipeline.agent_context import protected_check
from repolace_pipeline.attempts import AttemptScorer

log = structlog.get_logger()

#: The resolver's own budget is half of what the agent's has left, and never more than this. The
#: calls cap is twice the resolver's step cap: a failed call counts too, the same slack the task's
#: 150 calls give the agent's 120 steps.
RESOLVER_MAX_USD = Decimal("0.50")
RESOLVER_MAX_WALL_SECONDS = 600.0
RESOLVER_MAX_CALLS = 2 * ResolverLimits().max_steps

#: The only conflict kinds a text resolver can edit: both sides changed, or both added, a plain file.
TEXT_CONFLICTS = frozenset({CONFLICT_BOTH_MODIFIED, CONFLICT_BOTH_ADDED})

MAX_REASON_CHARS = 1000
MAX_REASON_PATHS = 5
PATH_CHARS = 200

ALREADY_CONTAINED_REASON = "the target branch moved and already contains this change; there is nothing to propose"

#: What `run_task` calls to resolve conflicts; `repolace_agents.resolver.resolve_conflicts` in production.
Resolver = Callable[[ResolverDeps], Awaitable[ResolverResult]]


class MergeOutcome(str, Enum):
    NOT_MOVED = "not_moved"
    MERGED = "merged"
    ALREADY_CONTAINED = "already_contained"
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class TargetMerge:
    outcome: MergeOutcome
    #: The target's tip when it was fetched.
    tip_sha: str
    #: How many conflicted files the resolver reconciled; 0 for a clean merge.
    resolved_files: int = 0
    #: The scored attempt on the merged tree. None unless MERGED, and None for MERGED too when the
    #: merged tree is the one already scored (the target's commits changed no file): the old score
    #: then still describes it.
    record: AttemptRecord | None = None
    #: `tasks.conflict_reason` for CONFLICTING, the gate's sentence for ALREADY_CONTAINED.
    reason: str | None = None

    @property
    def note(self) -> str | None:
        """The PR body's host-written line about the merge, or None when nothing was merged."""
        if self.outcome is not MergeOutcome.MERGED:
            return None
        if self.resolved_files:
            return (
                f"moved while the task ran; merged in, with {self.resolved_files} conflicted file(s) "
                f"reconciled by repolace's conflict resolver"
            )
        return "moved while the task ran; merged in cleanly"


def resolver_budget(agent: TaskBudget) -> TaskBudget:
    """Half of what the agent's budget has left, capped, with the same clock.

    Its own budget rather than the agent's, so a resolver cannot spend what is left of a dollar cap
    meant for fixing the issue, and an agent that used everything leaves the resolver nothing (it
    then stops `budget` at once, and the task is `conflicting`, not failed).
    """
    calls_left = max(agent.max_calls - agent.calls, 0)
    return TaskBudget(
        max_usd=min(agent.remaining_usd / 2, RESOLVER_MAX_USD),
        max_calls=min(calls_left // 2, RESOLVER_MAX_CALLS),
        max_wall_seconds=min(agent.remaining_wall_seconds / 2, RESOLVER_MAX_WALL_SECONDS),
        clock=agent.clock,
    )


# --- the host's check --------------------------------------------------------


def _clean(text: str, limit: int) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)[:limit]


def _display(path: str) -> str:
    """A repository path made safe to put in a sentence: printable characters only, bounded."""
    return _clean(path, PATH_CHARS)


def _listed(paths: Sequence[str]) -> str:
    shown = ", ".join(_display(p) for p in paths[:MAX_REASON_PATHS])
    more = len(paths) - MAX_REASON_PATHS
    return shown + (f" and {more} more" if more > 0 else "")


def _file_problems(root: Path, path: str) -> list[str]:
    """What is still wrong with one conflicted file. Sync: run on a thread."""
    shown = _display(path)
    try:
        resolved = resolve_within(root, path)
    except (ValueError, OSError):
        return [f"{shown}: not a file inside the checkout"]
    if resolved.is_symlink() or not resolved.is_file():
        return [f"{shown}: missing, or not a regular file"]
    try:
        number = file_has_marker(resolved)
    except RuntimeError:
        return [f"{shown}: too large to check for conflict markers"]
    if number is not None:
        return [f"{shown}:{number} still holds a conflict marker line"]
    return []


def _has_no_marker(root: Path, path: str) -> bool:
    """True when the conflicted file git wrote holds no marker line (or cannot be scanned). Sync."""
    try:
        resolved = resolve_within(root, path)
        if resolved.is_symlink() or not resolved.is_file():
            return True
        return file_has_marker(resolved) is None
    except (ValueError, OSError, RuntimeError):
        return True


def host_check(workspace: TaskWorkspace, conflicts: Collection[str]) -> Callable[[Sequence[str]], Awaitable[list[str]]]:
    """`ResolverDeps.check`: the problems still present after a `submit_resolution`, none when done.

    The model's claim to be finished is never the evidence. Checked: each conflicted file is a
    regular file inside the checkout with no marker line left; no unmerged entry exists outside the
    conflicted set (the index stays unmerged for the set itself until the host stages it, which is
    `commit_resolution`'s job, and that checks the staged result again); and nothing outside the set
    was edited. The sentences are host-written and name repository paths only.
    """
    allowed = frozenset(conflicts)

    async def check(paths: Sequence[str]) -> list[str]:
        problems: list[str] = []
        for path in paths:
            problems.extend(await asyncio.to_thread(_file_problems, workspace.path, path))
        named = set(paths)
        outside = sorted(p for p in (os.fsdecode(e.path) for e in await workspace.repo.unmerged_files()) if p not in named)
        if outside:
            problems.append(f"{len(outside)} unmerged file(s) outside the conflicted set: {_listed(outside)}")
        stray = sorted(p for p in (os.fsdecode(raw) for raw in await workspace.repo.worktree_changes()) if p not in allowed)
        if stray:
            problems.append(f"{len(stray)} file(s) outside the conflicted set were changed: {_listed(stray)}")
        return problems

    return check


# --- the step ----------------------------------------------------------------


def _reason(text: str) -> str:
    return redact(text)[:MAX_REASON_CHARS]


def _merge_message(target_branch: str, tip: str, task_id: uuid.UUID) -> str:
    """The merge commit's message. Squashed away before the push; host-written all the same."""
    return f"Merge {_display(target_branch)} at {tip[:12]} into the agent branch\n\nTask {task_id.hex}."


async def merge_target(
    *,
    task_id: uuid.UUID,
    workspace: TaskWorkspace,
    scorer: AttemptScorer,
    issue: IssueContext,
    baseline: SuiteResult,
    hidden_paths: Collection[str],
    llm: LLMClientLike | None,
    resolver: Resolver,
    agent_budget: TaskBudget,
) -> TargetMerge:
    """Bring the target's new tip into HEAD, or leave HEAD where it was and say why.

    HEAD must be the last scored commit on entry (the pipeline's review stage rewinds to it).
    """
    repo = workspace.repo
    # A fetch failure (target deleted, network down) raises `GitError` and fails a task whose work is
    # done; the push would fail the same way, so it is left to.
    moved, tip = await workspace.target_moved()
    if not moved:
        log.info("pipeline.merge_target.not_moved", tip=tip)
        return TargetMerge(MergeOutcome.NOT_MOVED, tip)

    old_base = workspace.base_sha
    log.info("pipeline.merge_target.moved", base=old_base, tip=tip)
    if not await repo.is_ancestor(old_base, tip):
        # A force-push or a rewind, not new work on top of ours. Merging it would bring back every
        # commit the target dropped, and `advance_base` plus the squash would put them in the PR.
        return _conflicting(
            tip,
            "the target branch was rewritten (force-pushed or rewound) while the task ran, so merging "
            "it would bring back commits it no longer has",
        )

    pre_merge = await repo.head_sha()
    if await repo.has_changes():
        # Edits after the last scored attempt, or files a tool left behind. Neither was scored, and
        # neither may ride into the merge commit or the next attempt's `commit_all`.
        log.info("pipeline.merge_target.cleaned", to=pre_merge)
        await workspace.rewind_to(pre_merge)

    preview = await repo.merge_tree(pre_merge, tip)
    if len(preview.conflicted_paths) > MAX_RESOLUTION_PATHS:
        return _conflicting(
            tip,
            f"merging the target branch conflicts in {len(preview.conflicted_paths)} files, more than "
            f"the {MAX_RESOLUTION_PATHS} one resolution may cover",
        )

    clean = await repo.merge_no_commit(tip)
    if not clean and not await repo.merge_in_progress():
        raise GitError("git merge stopped without starting a merge")
    if clean != preview.clean:
        await repo.abort_merge()
        raise GitError("merge-tree and merge disagree about whether the target branch merges cleanly")

    message = _merge_message(workspace.target_branch, tip, task_id)
    resolved = 0
    if clean:
        await repo.commit_merge(message)
    else:
        outcome = await _resolve(
            task_id=task_id,
            workspace=workspace,
            issue=issue,
            baseline=baseline,
            hidden_paths=hidden_paths,
            llm=llm,
            resolver=resolver,
            agent_budget=agent_budget,
            old_base=old_base,
            tip=tip,
            message=message,
        )
        if isinstance(outcome, str):
            await repo.abort_merge()
            log.info("pipeline.merge_target.conflicting", head=await repo.head_sha())
            return _conflicting(tip, outcome)
        resolved = outcome

    if not await repo.changed_files_from(tip):
        # The merged tree is the target's own: the change is already there. Undo the merge so the
        # row describes the scored commit, and propose nothing.
        await workspace.rewind_to(pre_merge)
        log.info("pipeline.merge_target.already_contained", tip=tip)
        return TargetMerge(MergeOutcome.ALREADY_CONTAINED, tip, reason=ALREADY_CONTAINED_REASON)

    await workspace.advance_base(tip)
    record = await scorer.verify_attempt(scorer.next_attempt)
    log.info(
        "pipeline.merge_target.merged",
        resolved_files=resolved,
        attempt=record.attempt if record is not None else None,
    )
    return TargetMerge(MergeOutcome.MERGED, tip, resolved_files=resolved, record=record)


def _conflicting(tip: str, reason: str) -> TargetMerge:
    reason = _reason(reason)
    log.info("pipeline.merge_target.unresolved", reason=reason)
    return TargetMerge(MergeOutcome.CONFLICTING, tip, reason=reason)


async def _resolve(
    *,
    task_id: uuid.UUID,
    workspace: TaskWorkspace,
    issue: IssueContext,
    baseline: SuiteResult,
    hidden_paths: Collection[str],
    llm: LLMClientLike | None,
    resolver: Resolver,
    agent_budget: TaskBudget,
    old_base: str,
    tip: str,
    message: str,
) -> int | str:
    """Resolve the merge in progress. The number of files resolved and committed, or why not.

    A string means the caller must abort the merge. Exceptions that mean repolace broke propagate
    with the merge still in progress; the checkout is about to be deleted with the failed task.
    """
    try:
        entries = await workspace.repo.unmerged_files()
    except TooManyConflicts as exc:
        return f"the merge leaves more unmerged files than one resolution may cover ({_clean(str(exc), 120)})"
    if not entries:
        # A rename-directory or similar conflict git reports but leaves no index entries for: nothing
        # a text resolver can edit, and nothing the host could stage honestly.
        return "git reported a conflicted merge but left no unmerged files for a resolver to edit"
    kinds = {entry.path: conflict_kind(entry.stages) for entry in entries}
    other = sorted(path for path, kind in kinds.items() if kind not in TEXT_CONFLICTS)
    if other:
        named = ", ".join(sorted({kinds[path] for path in other}))
        return (
            f"{named} conflict(s) that a text resolver cannot edit, in {len(other)} file(s): "
            f"{_listed([os.fsdecode(p) for p in other])}"
        )
    try:
        paths = sorted(path.decode("utf-8") for path in kinds)
    except UnicodeDecodeError:
        return "a conflicted path is not valid UTF-8, which the resolver's tools cannot address"
    # Whatever changes how git writes or stores conflict text puts the file beyond the host's check: a
    # longer marker, another encoding, a `-merge` that leaves no markers at all.
    overridden = await workspace.repo.marker_attribute_overrides(paths)
    if overridden:
        return (
            f".gitattributes changes how git writes conflict text in {len(overridden)} file(s), "
            f"which the host cannot check: {_listed([os.fsdecode(p) for p in overridden])}"
        )
    # And a conflicted file git left no marker in (binary content, a driver) has nothing to reconcile:
    # an unedited submission would stage our side and silently drop the target's change.
    unmarked = [p for p in paths if await asyncio.to_thread(_has_no_marker, workspace.path, p)]
    if unmarked:
        return (
            f"{len(unmarked)} conflicted file(s) hold no conflict markers (binary or generated "
            f"content), so a text resolver has nothing to reconcile: {_listed(unmarked)}"
        )
    if llm is None:
        return f"no model client to run the conflict resolver ({len(paths)} conflicted file(s): {_listed(paths)})"

    deps = ResolverDeps(
        llm=llm,
        checkout=workspace.path,
        conflicts=tuple(paths),
        issue=issue,
        base_sha=old_base,
        theirs_sha=tip,
        check=host_check(workspace, paths),
        is_protected=protected_check(baseline, hidden_paths),
    )
    try:
        with task_scope(task_id, resolver_budget(agent_budget)):
            result = await resolver(deps)
        stop = None if result.stop is ResolverStop.RESOLVED else result.stop.value
    except BudgetExceeded:
        # The resolver maps these itself; one that escapes is still the resolver's stop, not ours,
        # the rule `run._agent_stop_from` applies to the main agent.
        stop = ResolverStop.BUDGET.value
    except LLMCallError as exc:
        if exc.provider_refused:
            raise  # the provider refusing us is a harness failure wherever it happens
        stop = ResolverStop.LLM_ERROR.value
    if stop is not None:
        return f"the conflict resolver stopped ({stop}) with {len(paths)} conflicted file(s): {_listed(paths)}"

    try:
        await workspace.commit_resolution(paths, message)
    except GitError:
        raise
    except (RuntimeError, ValueError) as exc:
        # The resolver said it was done and the host's own check disagrees: its resolution is refused.
        return f"the host refused the resolver's resolution: {_clean(str(exc), 300)}"
    return len(paths)
