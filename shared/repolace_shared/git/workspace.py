"""The per-task checkout: clone, work in it, throw it away.

One ephemeral clone per task, no persistent cache. This is affordable because
chunks and embeddings live in Postgres, not in the tree -- discarding the
checkout does not discard the index, so a fresh clone still skips re-embedding
every unchanged file, which is the expensive part. A full clone also carries
complete history, so the previously-indexed commit is still resolvable.

The methods below are the steps of the recorded branch-and-PR flow, in order,
so the pipeline reads as that flow rather than as a pile of git calls.
"""

import asyncio
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from repolace_shared.git.repo import GitRepo, TokenProvider, clone, run_git
from repolace_shared.github.client import GithubClient

log = structlog.get_logger()

#: Requested lifetime for the token minted just before a push. The cache's
#: default 60s margin is sized for a single API call; a push is a network
#: round-trip that can stall, and a token that expires mid-push fails the
#: step that everything else in the task was building towards.
PUSH_TOKEN_MIN_TTL_SECONDS = 300.0

_CHECKOUT_DIR_NAME = "repo"
_EXPORT_DIR_PREFIX = "export"
_RESULTS_DIR_PREFIX = "results"

#: The sandbox runs as an unprivileged uid that is not ours, so it needs to be
#: able to write into the exported tree. Not a host exposure: the workspace root
#: is created by `mkdtemp` with mode 0700, so no other user can traverse to it.
_SANDBOX_DIR_MODE = 0o777

#: What a str run label may look like: a letter first, then letters, digits and
#: `_.-`, at most 64 characters. Exactly `verify.stage`'s rule, restated because
#: `shared` cannot import `verify`. The label becomes a directory name and
#: `discard` deletes the tree under it, so a label like `x/..` must never be able
#: to name the workspace root; the letter-first rule also stops `"1"` from
#: aliasing the int attempt 1.
_LABEL = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}")

_REMOTE = "origin"


def github_clone_url(owner: str, name: str) -> str:
    """The plain HTTPS remote. No credentials -- see ``repo._CREDENTIAL_HELPER``."""
    return f"https://github.com/{owner}/{name}.git"


def agent_branch_name(issue_number: int, task_id: uuid.UUID) -> str:
    """A branch name unique per task, so a retried task never collides with its own earlier push.

    The whole task id, not a prefix of it. A prefix would stake uniqueness on
    32 bits happening to be random, which is true of uuid4 and false of any
    time-ordered scheme -- two uuid7 ids minted seconds apart share their
    leading bits. Uniqueness here should hold by construction rather than by
    which generator the Task model happens to use. Postgres accepts the
    undashed form, so the tail pastes straight into a lookup.
    """
    return f"repolace/issue-{issue_number}-{task_id.hex}"


def installation_token_provider(github: GithubClient, installation_id: int) -> TokenProvider:
    """Bind a provider that mints a *fresh* token each time it is awaited."""

    async def provide() -> str:
        return await github.get_installation_token(installation_id, min_ttl_seconds=PUSH_TOKEN_MIN_TTL_SECONDS)

    return provide


@dataclass
class TaskWorkspace:
    """A cloned repo plus the base commit the whole task is measured against."""

    root: Path
    repo: GitRepo
    target_branch: str
    base_sha: str
    agent_branch: str | None = field(default=None)

    @property
    def path(self) -> Path:
        """The checkout itself. What indexing walks and what the agent edits.

        **Not** what the sandbox mounts -- that is `export_tree`, deliberately,
        because this directory contains `.git`.
        """
        return self.repo.path

    async def start_agent_branch(self, issue_number: int, task_id: uuid.UUID) -> str:
        """Step 4. A plain branch, not a worktree.

        A worktree exists to share one object store across several trees, which
        is what a persistent clone cache would have needed. One clone per task
        does not.
        """
        name = agent_branch_name(issue_number, task_id)
        await self.repo.create_branch(name)
        self.agent_branch = name
        log.info("workspace.agent_branch", branch=name, base_sha=self.base_sha)
        return name

    async def export_tree(self, attempt: int | str) -> Path:
        """Copy the tracked tree out for the sandbox, without ``.git``.

        This is the whole confused-deputy mitigation, and it is structural
        rather than defensive. ``.git`` is executable configuration: hooks fire
        on the host during ordinary commits and pushes, ``core.fsmonitor`` fires
        on every status, and ``remote.origin.url`` decides where a push sends
        the installation token. Sandboxed code that can write into ``.git``
        needs no escape -- the host reads those files afterwards and acts on
        them.

        Handing over a directory with no ``.git`` in it does not block those
        tricks one at a time; it removes the thing they all require. That
        matters because the list of dangerous config keys grows with every git
        release.

        Lives under ``root`` beside the checkout, so ``task_workspace``'s
        existing cleanup removes it -- including the case where the sandbox
        left files this process does not own.

        ``attempt`` is an int for a scored run and a str label (``"probe-3"``)
        for an unscored one: its own directory, so a probe can never collide with
        an attempt. See ``_run_dir``.
        """
        if not await self.repo.tree_matches_head():
            raise RuntimeError(
                "refusing to export: the working tree does not match HEAD, so the exported "
                "tree would not be the commit the results get attributed to"
            )

        destination = self._run_dir(_EXPORT_DIR_PREFIX, attempt)
        # The mode goes all the way down, not just onto the root: the sandbox
        # runs as an unprivileged uid that is not ours, and a suite writing a
        # `__pycache__` or a sqlite fixture beside its own code needs the
        # *containing* directory writable.
        await self.repo.export_index_to(destination, dir_mode=_SANDBOX_DIR_MODE)
        log.info("workspace.exported", attempt=attempt, destination=str(destination))
        return destination

    async def results_dir(self, attempt: int | str) -> Path:
        """Somewhere for the sandbox to write its report, outside the source tree.

        Separate from the export so a suite that scribbles over its own working
        directory cannot destroy the report that says what it did.
        """
        destination = self._run_dir(_RESULTS_DIR_PREFIX, attempt)
        destination.mkdir(parents=True, exist_ok=True)
        os.chmod(destination, _SANDBOX_DIR_MODE)
        return destination

    def _run_dir(self, prefix: str, attempt: int | str) -> Path:
        """``<root>/<prefix>-<attempt>``, refusing a label that could name anything else.

        An int is formatted as it always was. A str is validated rather than
        sanitised, for the reason `verify.stage.container_name` gives: rewriting
        it would let two distinct labels share a directory, and ``discard``
        removes whatever this returns.
        """
        if isinstance(attempt, str) and not _LABEL.fullmatch(attempt):
            raise ValueError(
                f"run label {attempt!r} must start with a letter, use only letters, digits, "
                f"'_', '.', '-', and be at most 64 characters"
            )
        return self.root / f"{prefix}-{attempt}"

    async def discard(self, attempt: int | str) -> None:
        """Delete one run's export and results directories. Best effort; never raises.

        For unscored runs (a probe, a scratch script): an agent loop of 40 steps
        would otherwise leave 40 full copies of the tree on disk, and a stale file
        in one run's directory must not be visible to the next. Never raises
        because it runs on the way out of a path that may already be failing, and
        a leftover directory is a disk leak worth logging, not an exception worth
        raising -- including for a malformed label, which can only mean there is
        nothing of that name to remove.

        Runs the removal on a worker thread: an export is a whole source tree, and
        deleting it on the event loop would stall every other task in the process.
        """
        try:
            targets = [
                self._run_dir(_EXPORT_DIR_PREFIX, attempt),
                self._run_dir(_RESULTS_DIR_PREFIX, attempt),
            ]
        except ValueError as exc:
            log.warning("workspace.discard_refused", error=str(exc))
            return
        for target in targets:
            # `lexists`, so a dangling symlink is still removed, and a label that
            # was never exported does not log a failure for a path that is not there.
            if os.path.lexists(target):
                await asyncio.to_thread(_remove_tree, target)

    async def prune_remote_refs(self, keep: Sequence[str]) -> tuple[str, ...]:
        """Delete every remote-tracking branch except ``keep``. Returns what it removed.

        Defence in depth for the benchmark. A full clone -- which the index
        freshness protocol requires -- carries every branch the remote has, and a
        benchmark remote accumulates earlier runs' agent branches. With a tree-ish
        in hand a ``git grep <ref>`` or a ``git show`` could reach a previous
        PASSED patch, or a gold branch, and an agent that read one would be copying
        the answer. Removing the names does not make that impossible; it removes
        the cheap routes to it.

        **Names, not objects.** The commits stay in the object store (``gc.auto=0``
        keeps git from collecting them mid-task), so a sha that is already known
        still resolves.
        That is why this is one layer and not the control: the control is the
        toolbox refusing ``.git`` and the export carrying no ``.git`` at all.
        Tags are not touched either.

        ``keep`` is bare branch names (``"main"``, ``"bench/x"``), compared exactly.
        A name with nothing behind it is simply not there to keep. ``origin/HEAD``
        is a convenience symref nothing here reads, and is always removed so it can
        never be left dangling at a branch this deleted.
        """
        prefix = f"refs/remotes/{_REMOTE}/"
        keep_refs = {f"{prefix}{name}" for name in keep}
        head_ref = f"{prefix}HEAD"
        listed = (
            await run_git(
                "for-each-ref",
                "--format=%(refname)",
                prefix.rstrip("/"),
                cwd=self.repo.path,
                credential_host=self.repo.credential_host,
            )
        ).splitlines()
        branches = [ref for ref in listed if ref != head_ref and ref not in keep_refs]

        # The symref goes last, and only if it is there at all.
        for ref in [*branches, *([head_ref] if head_ref in listed else [])]:
            # Full ref names, so none can be read as an option. `--no-deref`
            # deletes the symref itself instead of the branch it points at.
            await run_git(
                "update-ref",
                "--no-deref",
                "-d",
                ref,
                cwd=self.repo.path,
                credential_host=self.repo.credential_host,
            )
        removed = tuple(ref.removeprefix(prefix) for ref in branches)
        log.info("workspace.remote_refs_pruned", removed=len(removed), kept=sorted(keep))
        return removed

    async def record_attempt(self, message: str) -> str | None:
        """Step 5. Commit one edit attempt as a checkpoint.

        These commits are not history for humans -- they are squashed away
        before the PR. They exist so the Debugger can reset back to a known
        attempt instead of trying to un-edit a bad state, and so the trace UI
        has a per-attempt diff.
        """
        return await self.repo.commit_all(message)

    async def rewind_to(self, sha: str) -> None:
        """Step 5, the other direction: abandon the current attempt."""
        await self.repo.reset_hard(sha)

    async def review_diff(self) -> str:
        """Step 7. Exactly what the Reviewer reads and what the PR will show."""
        return await self.repo.diff_from(self.base_sha)

    async def changed_files(self) -> list[str]:
        """Step 7, for the no-test-edits criterion."""
        return await self.repo.changed_files_from(self.base_sha)

    async def baseline_files(self) -> tuple[str, ...]:
        """Every tracked path at the base commit.

        The other half of the no-test-edits criterion: `disqualifying_paths`
        uses it to tell a test file the agent *added* from a shipped module that
        merely looks like one. Answers about the base commit whatever the tree
        currently holds, so it may be called at any point in the task.
        """
        return await self.repo.files_at(self.base_sha)

    async def squash(self, message: str) -> str | None:
        """Step 8, so the PR is not 'attempt 1, attempt 2, fix debug output'."""
        return await self.repo.squash_onto(self.base_sha, message)

    async def push(self) -> str:
        """Step 9. The branch must exist on the remote before the clone is discarded.

        Easy to overlook: a PR cannot reference a branch that only ever existed
        in a temp directory, and the temp directory is gone the moment this
        context manager exits.
        """
        if self.agent_branch is None:
            raise RuntimeError("no agent branch to push; call start_agent_branch first")
        if await self.repo.head_sha() == self.base_sha:
            # `squash` returns None for a branch with no net change; pushing it
            # anyway gets a 422 "No commits between base and head" from the PR
            # call, several seconds later and with a much less obvious message.
            raise RuntimeError("agent branch is identical to the base commit; nothing to push")
        await self.repo.push_branch(self.agent_branch)
        return self.agent_branch


def _force_writable(func, path, exc: BaseException) -> None:
    """rmtree error hook for a directory the Verify sandbox left unwritable.

    Chmods the *parent*, not the entry: on POSIX, permission to unlink comes
    from the containing directory, so making the entry itself writable changes
    nothing. (Read-only files, which is what git leaves behind for pack
    objects, therefore never needed this hook at all.)

    ``follow_symlinks=False`` matters because ``shutil.rmtree`` is otherwise
    fd-based and symlink-safe, and a path-based ``chmod`` here would hand that
    property back: a symlink left in the tree pointing at a host file would
    have the *target's* mode changed.
    """
    target = Path(path)
    for candidate in (target.parent, target):
        try:
            os.chmod(candidate, stat.S_IRWXU, follow_symlinks=False)
        except (OSError, NotImplementedError):
            continue
    try:
        func(path)
    except OSError as retry_exc:
        log.warning("workspace.remove_entry_failed", path=str(path), error=str(retry_exc))


def _remove_tree(root: Path) -> None:
    """Delete the workspace, but never at the cost of the exception that is already propagating.

    ``onexc`` absorbs per-entry failures; the outer guard covers the rest, so a
    cleanup problem can never replace the task's own error on the way out.
    """
    try:
        shutil.rmtree(root, onexc=_force_writable)
    except OSError as exc:
        log.warning("workspace.remove_failed", root=str(root), error=str(exc))
        return

    if root.exists():
        # Most likely root-owned files left by the Verify sandbox. Surfaced
        # rather than raised: losing a temp directory is a disk leak worth
        # seeing in the logs, not a reason to mask why the task failed.
        log.warning("workspace.remove_failed", root=str(root))
    else:
        log.info("workspace.removed", root=str(root))


@asynccontextmanager
async def task_workspace(
    owner: str,
    name: str,
    target_branch: str,
    token_provider: TokenProvider | None = None,
    parent_dir: Path | None = None,
    clone_url: str | None = None,
) -> AsyncIterator[TaskWorkspace]:
    """Steps 1 and 10: clone at the target branch, yield the workspace, delete it.

    Because each task gets its own clone, two concurrent tasks on one repo are
    isolated at the filesystem level for free. They still contend for the
    shared ``code_chunks`` rows, which is what the advisory lock in
    ``reindex_if_stale`` protects -- separate checkouts do not.

    The caller must index from ``workspace.path`` *before* any agent edits it,
    or the reindex captures the agent's own uncommitted work as repo state.

    ``clone_url`` overrides the derived github.com remote -- for a GitHub
    Enterprise host, and for tests that want a real clone and a real push
    against a local remote instead of a mocked one.
    """
    root = Path(tempfile.mkdtemp(prefix="repolace-task-", dir=parent_dir))
    log.info("workspace.created", root=str(root), repo=f"{owner}/{name}", target_branch=target_branch)
    try:
        repo = await clone(
            clone_url or github_clone_url(owner, name),
            root / _CHECKOUT_DIR_NAME,
            branch=target_branch,
            token_provider=token_provider,
        )
        yield TaskWorkspace(
            root=root,
            repo=repo,
            target_branch=target_branch,
            base_sha=await repo.head_sha(),
        )
    finally:
        _remove_tree(root)
