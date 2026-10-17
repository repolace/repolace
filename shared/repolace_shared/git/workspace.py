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
from repolace_shared.paths import resolve_within

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

        **A label is single-use.** If its directory already exists this raises
        before touching it. The sandbox writes into the directory it was given, so
        what is left there is not ours: exporting over it would follow a symlink the
        sandbox planted and write through it onto a host file (and chmod a host
        directory). It would also mix two runs' reports. A repeated label is a
        logic bug; ``discard`` first if the reuse is intended.
        """
        # First, and before any git: both are logic errors in the caller, touch
        # nothing, and need no repository to detect.
        destination = self._run_dir(_EXPORT_DIR_PREFIX, attempt)
        _refuse_reuse(destination, "export", attempt)

        if not await self.repo.tree_matches_head():
            raise RuntimeError(
                "refusing to export: the working tree does not match HEAD, so the exported "
                "tree would not be the commit the results get attributed to"
            )

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
        _refuse_reuse(destination, "results", attempt)
        destination.mkdir(parents=True)
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
            if os.path.islink(target):
                # A symlink where the directory should be: remove the link itself and
                # never what it points at. `rmtree` refuses a symlink, which would leave
                # it in place, and following it would delete a host directory.
                await asyncio.to_thread(_unlink, target)
            elif os.path.lexists(target):
                # `lexists` rather than `exists`, and checked at all so a label that was
                # never exported does not log a failure for a path that is not there.
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

    async def target_moved(self) -> tuple[bool, str]:
        """Fetch the target branch and compare its tip with ``base_sha``.

        Returns ``(moved, tip_sha)``. Authenticated and non-shallow (see
        ``GitRepo.fetch_branch``). ``moved`` is simply "the tip is not the base":
        a force-push that rewound the branch is movement too, and the caller's
        merge-tree check says whether anything actually conflicts.
        """
        tip = await self.repo.fetch_branch(self.target_branch)
        return tip != self.base_sha, tip

    async def advance_base(self, new_base_sha: str) -> None:
        """Move ``base_sha`` forward after the target's new tip was merged into HEAD.

        The one place ``base_sha`` is reassigned. ``review_diff``, ``changed_files``,
        ``baseline_files`` and ``squash`` all read it, so after this they are
        relative to the new base: the PR shows only the agent's own change.

        Refused unless ``new_base_sha`` is a full commit sha already contained in
        HEAD. Without that check a caller could point the base at a commit the
        branch does not descend from, and every diff and the squash would then
        include (or silently drop) someone else's work. Note ``baseline_files``
        then describes the *new* base, which is the tree the diff is against;
        the sandbox baseline the scorer holds is still the old one.
        """
        if not _FULL_SHA.fullmatch(new_base_sha):
            raise ValueError(f"advance_base requires a full commit sha, got {new_base_sha!r}")
        if not await self.repo.is_ancestor(new_base_sha, "HEAD"):
            raise RuntimeError("refusing to advance the base: that commit is not contained in HEAD")
        log.info("workspace.base_advanced", old=self.base_sha, new=new_base_sha)
        self.base_sha = new_base_sha

    async def commit_resolution(self, paths: Sequence[str], message: str) -> str:
        """Conclude an in-progress merge from resolved ``paths``; return the merge commit sha.

        The host's check, never the resolver's word. Refuses unless:

        * a merge is in progress, and ``paths`` cover every unmerged path;
        * every path stays inside the checkout (``resolve_within``, which refuses
          absolute paths, ``..`` and symlinks) and none is under ``.git``;
        * no named file still holds a conflict marker line (``<<<<<<<``,
          ``|||||||``, ``=======`` alone on a line, ``>>>>>>>``);
        * after staging, no unmerged entries remain and nothing else in the tree
          was edited (a resolver that touches a non-conflicted file is refused,
          not committed).

        ``=======`` alone on a line is also a Markdown/RST heading underline, so
        a legitimate file can be refused: that is the cautious direction, and the
        refusal names the file and line.
        """
        if not message.strip():
            raise ValueError("commit_resolution requires a non-empty commit message")
        if not paths:
            raise ValueError("commit_resolution requires at least one path")
        if len(paths) > MAX_RESOLUTION_PATHS:
            raise ValueError(f"refusing {len(paths)} paths; at most {MAX_RESOLUTION_PATHS} can be resolved")
        if not await self.repo.merge_in_progress():
            raise RuntimeError("no merge in progress; nothing to resolve")

        root = self.repo.path.resolve()
        relative: list[str] = []
        for candidate in paths:
            if "\0" in candidate:
                raise ValueError("a path contains NUL")
            resolved = resolve_within(root, candidate)
            rel = resolved.relative_to(root)
            if resolved == root or any(part.lower() == ".git" for part in rel.parts):
                raise ValueError(f"refusing path {candidate!r}")
            relative.append(rel.as_posix())
        wanted = {os.fsencode(rel) for rel in relative}

        unmerged = await self.repo.unmerged_files()
        unmerged_paths = {entry.path for entry in unmerged}
        # Both directions. A path that is not itself unmerged is not something
        # the resolver was asked to touch, and staging it would commit whatever
        # the working tree holds there.
        extra = sorted(wanted - unmerged_paths)
        if extra:
            raise RuntimeError(
                f"{len(extra)} path(s) are not unmerged and may not be committed as a resolution: "
                + ", ".join(os.fsdecode(p) for p in extra[:5])
            )
        missing = sorted(unmerged_paths - wanted)
        if missing:
            raise RuntimeError(
                f"{len(missing)} unmerged path(s) are not in the resolved set: "
                + ", ".join(os.fsdecode(p) for p in missing[:5])
            )

        for rel in relative:
            _refuse_markers(root / rel, rel)

        await self.repo.stage_paths(relative)
        if await self.repo.unmerged_files():
            raise RuntimeError("unmerged entries remain after staging the resolved paths")
        stray = [p for p in await self.repo.worktree_changes() if p not in wanted]
        if stray:
            raise RuntimeError(
                "files outside the resolved set were modified: "
                + ", ".join(os.fsdecode(p) for p in stray[:5])
            )
        sha = await self.repo.commit_merge(message)
        log.info("workspace.resolution_committed", sha=sha, paths=len(relative))
        return sha

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


#: A merge touching more conflicted paths than this is not one to hand a resolver.
MAX_RESOLUTION_PATHS = 500

_FULL_SHA = re.compile(r"[0-9a-f]{40}")

#: A conflict marker line, as git writes them (also zdiff3's `|||||||` base line).
_MARKER_LINE = re.compile(rb"^(<{7}|>{7}|\|{7})( .*)?$|^={7}$")

#: A resolved file this large is not a source file a resolver edited.
_MAX_RESOLVED_BYTES = 10_000_000


def _refuse_markers(path: Path, display: str) -> None:
    """Raise if ``path`` still holds a conflict marker line. A deleted file has none."""
    if not path.exists():
        return
    if path.stat().st_size > _MAX_RESOLVED_BYTES:
        raise RuntimeError(f"{display} is too large to check for conflict markers")
    for number, line in enumerate(path.read_bytes().split(b"\n"), start=1):
        if _MARKER_LINE.match(line.rstrip(b"\r")):
            raise RuntimeError(f"{display}:{number} still holds a conflict marker")


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


def _refuse_reuse(destination: Path, what: str, attempt: int | str) -> None:
    """Raise if `destination` exists in any form, a dangling symlink included.

    `lexists`, not `exists`: a symlink the sandbox left in place of the directory
    is precisely the thing that must not be followed, and `exists` follows it.
    """
    if os.path.lexists(destination):
        raise RuntimeError(f"{what} for {attempt} already exists; labels are single-use")


def _unlink(path: Path) -> None:
    """Remove one symlink, best effort: a failure is a warning, never an exception."""
    try:
        os.unlink(path)
    except OSError as exc:
        log.warning("workspace.remove_failed", root=str(path), error=str(exc))


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

    if os.path.lexists(root):
        # Most likely root-owned files left by the Verify sandbox. Surfaced
        # rather than raised: losing a temp directory is a disk leak worth
        # seeing in the logs, not a reason to mask why the task failed. A warning,
        # never quieter: every probe that leaves one behind is a full copy of the
        # tree, and the parent directory has to be quota-limited in production for
        # exactly this reason. `lexists` so a symlink left in place is also seen.
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
