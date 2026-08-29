"""A thin async wrapper over the git CLI.

Async subprocesses rather than sync calls dispatched through
``asyncio.to_thread`` -- the pattern ``retrieval.index`` uses for its own git
work -- because these commands differ in kind. That module runs a ``rev-parse``
and a ``diff --name-only``, both of which finish in milliseconds; a clone or a
push is minutes of network I/O. Parking those on the default thread-pool
executor would hold one of its handful of slots for the whole duration, and
indexing already competes for that pool when it embeds.

Credentials are never written to disk or passed on the command line. See
``_CREDENTIAL_HELPER`` for why that matters more than it looks.
"""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog

from repolace_shared.process import REAP_TIMEOUT_SECONDS, kill_process_tree

log = structlog.get_logger()

TokenProvider = Callable[[], Awaitable[str]]

DEFAULT_TIMEOUT_SECONDS = 120.0
CLONE_TIMEOUT_SECONDS = 900.0
PUSH_TIMEOUT_SECONDS = 300.0

COMMITTER_NAME = "repolace"
COMMITTER_EMAIL = "noreply@repolace.dev"

#: Subprocess environment variables the credential helper reads. Not git
#: variables -- ours, referenced by name inside the helper.
GIT_TOKEN_ENV_VAR = "REPOLACE_GIT_TOKEN"
GIT_HOST_ENV_VAR = "REPOLACE_GIT_HOST"

DEFAULT_CREDENTIAL_HOST = "github.com"

# A shell credential helper that answers git's `get` request from the
# environment, and only for the one host we expect to be talking to.
#
# Why not the remote URL. Baking the token in as
# https://x-access-token:<token>@github.com/... writes it into the checkout's
# .git/config, and the Verify stage runs LLM-generated code against that same
# checkout. Reading .git/config would then hand an agent an installation token
# with contents:write on every repo the App can reach.
#
# Why the host check, which is the part that is easy to leave out. Keeping the
# token out of the tree is not enough on its own, because the tree still says
# *where the token gets sent*. Verify can rewrite `remote.origin.url`, or add
# `url.<evil>.insteadOf` (which rewrites explicit URLs too, so passing a URL
# instead of a remote name is not a fix), or set `http.proxy`. A helper that
# answers whatever it is asked then posts the token to a host of the agent's
# choosing over plain HTTP. Parsing git's stdin and matching protocol+host
# turns that from credential theft into a failed push.
#
# The token stays out of argv as well: the command line carries the variable
# *name*, expanded by the helper's shell at call time. Note this defends
# against other users on the host, not against root or a same-UID process --
# /proc/<pid>/environ is readable by both.
#
# `test ... || return 0` keeps the helper exiting 0 for git's `store` and
# `erase` calls, which it would otherwise fail.
_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || return 0; p=; h=; '
    'while IFS="=" read -r k v; do '
    'case "$k" in protocol) p=$v ;; host) h=$v ;; "") break ;; esac; done; '
    'if test "$p" = https && test "$h" = "$%s"; then '
    "echo username=x-access-token; "
    'echo "password=$%s"; '
    "fi; }; f"
) % (GIT_HOST_ENV_VAR, GIT_TOKEN_ENV_VAR)

#: The config every git invocation against an untrusted tree must carry.
#: Exported so `retrieval.index` can reuse it without also taking on the async
#: wrapper below. Deliberately not the whole of `_BASE_ARGS`: no credential
#: helper and no committer identity, because that module neither authenticates
#: nor commits, and handing it a credential helper would widen its blast radius
#: for no benefit.
UNTRUSTED_TREE_CONFIG_ARGS: tuple[str, ...] = (
    # Without it git C-quotes non-ASCII paths, which then match neither the
    # index nor an on-disk lookup.
    "-c", "core.quotepath=false",
    # Both are arbitrary commands git runs on the host, and `.git` sits inside
    # the tree the sandbox writes to. `git diff` refreshes the index, so
    # core.fsmonitor fires on a command that looks purely read-only.
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=",
)

_BASE_ARGS: tuple[str, ...] = (
    *UNTRUSTED_TREE_CONFIG_ARGS,
    "-c", "advice.detachedHead=false",
    # Empty value clears the inherited helper list, so a developer's global
    # osxkeychain/store helper cannot answer with the wrong account before ours
    # is consulted. The second entry then installs ours.
    "-c", "credential.helper=",
    "-c", f"credential.helper={_CREDENTIAL_HELPER}",
    # A clone has no committer identity of its own, and a worker container has
    # no global one, so a commit would fail outright without these.
    "-c", f"user.name={COMMITTER_NAME}",
    "-c", f"user.email={COMMITTER_EMAIL}",
    # A global commit.gpgsign=true would fail every commit in an environment
    # with no signing key.
    "-c", "commit.gpgsign=false",
    # Keep git from forking a background gc partway through a task.
    "-c", "gc.auto=0",
    # `core.hooksPath` and `core.fsmonitor` come from UNTRUSTED_TREE_CONFIG_ARGS
    # above. They shut the cheapest doors into the .git trust boundary -- a hook
    # dropped in the checkout would run on the *host* on the next commit,
    # checkout or push -- but they are not the real fix, because enumerating
    # dangerous keys is a losing game. The structural halves are the config-file
    # pins in `sanitized_git_env` and the export rewrite in `export_index_to`.
    # See the .git trust boundary note in CLAUDE.md.
)

#: Environment passed through to git. An allowlist, not a filter: the process
#: running this holds the GitHub App private key, the database URL and the
#: broker URL, and git spawns helpers that would inherit all of it. The App
#: private key is worse than any single token -- it mints installation tokens
#: for every installation, and rotating tokens does not revoke it.
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "USER",
        "LOGNAME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GIT_SSL_CAINFO",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

# `gh*_` covers the classic token family. `github_pat_` is the fine-grained
# PAT format, which the classic pattern does not match. The JWT arm catches
# `build_app_jwt` output -- a nine-minute bearer credential with authority over
# every installation, which is the worst thing that could reach a log line.
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


def redact(text: str) -> str:
    """Strip anything shaped like a credential before it reaches a log or a traceback.

    Phase 3 ships these logs off the host to Loki, so the cost of a miss goes
    up rather than down over time.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted-secret>", text)
    return text


class GitError(RuntimeError):
    """Base class for git failures, so callers can catch the family."""


class GitCommandError(GitError):
    #: Cap on how much stdout is folded into the message when stderr is empty.
    STDOUT_TAIL_CHARS = 2000

    def __init__(self, command: Sequence[str], returncode: int, stderr: str, stdout: str = "") -> None:
        # Not `self.args`: RuntimeError already owns that attribute.
        self.command = list(command)
        self.returncode = returncode
        self.stderr = redact(stderr.strip())
        self.stdout = redact(stdout.strip())
        # git writes a good deal of its diagnostics to stdout, not stderr --
        # "nothing added to commit but untracked files present" is the one that
        # matters here, and merge/rebase conflict listings will matter to the
        # conflict-resolution agent later. Without this, several real failures
        # arrive with an empty reason.
        detail = self.stderr or self.stdout[-self.STDOUT_TAIL_CHARS :] or "(no output)"
        # Redact the assembled message too, not just its parts: the command
        # itself would carry a token if a caller ever passed a credentialed URL.
        super().__init__(redact(f"git {' '.join(self.command)} failed ({returncode}): {detail}"))


class GitTimeoutError(GitError):
    def __init__(self, command: Sequence[str], timeout: float) -> None:
        self.command = list(command)
        self.timeout = timeout
        super().__init__(redact(f"git {' '.join(self.command)} exceeded {timeout}s"))


def sanitized_git_env() -> dict[str, str]:
    """Build git's environment from an allowlist rather than by subtracting from ours.

    Subtracting means every new secret added to the service environment is
    inherited by git and its helpers by default, and only stops being inherited
    if someone remembers to come back here. An allowlist fails the safe way.

    The allowlist drops an inherited `GIT_CONFIG_*`, which closes the route
    where our *caller* injects config. That is not the same as closing config
    injection, and an audit found the difference the hard way: `HOME` has to be
    on the allowlist, so git still reads the operator's own `~/.gitconfig`.
    A **tracked** `.gitattributes` saying `*.py filter=x` -- an ordinary file in
    the repository, present in every clone, that the sandbox never has to touch
    -- pairs with a `filter.x.smudge` there and runs a command on the host
    during `checkout-index`; `diff=x` plus `diff.x.textconv` runs one during the
    Reviewer's diff. `_BASE_ARGS` does not stop either, because it clears the
    keys it enumerates and this is the enumeration game CLAUDE.md already calls
    unwinnable.

    So the config *files* are pinned, not just the variables. git then reads no
    configuration it was not handed on the command line, which is structural --
    it needs no key list and covers keys nobody has thought of yet.
    """
    env = {name: value for name, value in os.environ.items() if name in _ENV_ALLOWLIST}
    # Without this an expired or wrong token makes git block on a credential
    # prompt forever. In a worker that is indistinguishable from a hung task,
    # and the timeout would be the only thing that ever ended it.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    # The pre-2.32 spelling of the line above. Harmless alongside it, and the
    # only thing that works on an older git.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git_env(token: str | None, credential_host: str) -> dict[str, str]:
    """`sanitized_git_env` plus the credential helper's two inputs."""
    env = sanitized_git_env()
    env[GIT_TOKEN_ENV_VAR] = token or ""
    env[GIT_HOST_ENV_VAR] = credential_host
    return env


def _spawn_git(
    *args: str,
    cwd: Path | None,
    token: str | None,
    credential_host: str,
    stdin: int = asyncio.subprocess.DEVNULL,
):
    """Start git with the base args, the sanitized environment and its own
    process group. Shared so every git this module spawns -- one-shot commands
    and the streaming `cat-file --batch` alike -- gets identical treatment.

    Returns the coroutine; the caller awaits it and is responsible for caching
    the pgid and killing the group on timeout or cancellation.
    """
    return asyncio.create_subprocess_exec(
        "git",
        *_BASE_ARGS,
        *args,
        cwd=cwd,
        env=_git_env(token, credential_host),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Some git subcommands read a message or a credential from stdin when
        # an argument is missing. Inheriting ours means such a command blocks
        # until the timeout instead of failing immediately -- and in a worker
        # there is nothing on the other end to answer it.
        stdin=stdin,
        # Puts git in its own process group so the whole tree can be killed as
        # one. It also detaches from the controlling terminal, which backs up
        # GIT_TERMINAL_PROMPT=0: there is no tty left to prompt on.
        start_new_session=True,
    )


async def run_git_bytes(
    *args: str,
    cwd: Path | None = None,
    token: str | None = None,
    credential_host: str = DEFAULT_CREDENTIAL_HOST,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Run one git command, returning stdout **undecoded**.

    `run_git` decodes with `errors="replace"`, which is right for messages and
    irreversibly wrong for content: a blob would be corrupted, and a path that
    is not valid UTF-8 would name a *different* file. A repository is entitled
    to both, so anything reading either goes through here.
    """
    process = await _spawn_git(*args, cwd=cwd, token=token, credential_host=credential_host)
    # Captured now, while the pid is certainly still valid: start_new_session
    # makes git its own group leader, so the pgid equals the pid, and this
    # stays killable after git itself has been reaped.
    pgid = process.pid

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        kill_process_tree(pgid, args)
        try:
            await asyncio.wait_for(process.wait(), REAP_TIMEOUT_SECONDS)
        except TimeoutError:
            # Nothing more to do -- but a timeout that does not return is worse
            # than a leaked process, so give up on reaping and raise.
            log.warning("git.kill.reap_timeout", command=list(args))
        raise GitTimeoutError(args, timeout) from None
    except asyncio.CancelledError:
        # A cancelled task must not leave a clone or a push running behind it.
        # No await while unwinding a cancellation: the group is already
        # SIGKILLed and the child watcher will reap it.
        kill_process_tree(pgid, args)
        raise

    if process.returncode != 0:
        raise GitCommandError(
            args,
            process.returncode or -1,
            stderr.decode("utf-8", errors="replace"),
            stdout.decode("utf-8", errors="replace"),
        )

    return stdout


async def run_git(
    *args: str,
    cwd: Path | None = None,
    token: str | None = None,
    credential_host: str = DEFAULT_CREDENTIAL_HOST,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run one git command, returning stdout. Raises on non-zero exit or timeout.

    Always checks the exit status: callers that need to tolerate a specific
    failure ask a question first (``git status --porcelain`` for "is there
    anything to commit") rather than reading a return code, which keeps the
    tolerated cases explicit instead of swallowing every error alike.
    """
    stdout = await run_git_bytes(
        *args, cwd=cwd, token=token, credential_host=credential_host, timeout=timeout
    )
    return stdout.decode("utf-8", errors="replace")


#: A large repository's export is not a `rev-parse`; the default is far too
#: tight for one and far too loose as a hang detector for the other.
EXPORT_TIMEOUT_SECONDS = 300.0

#: Index modes we will materialise, mapped to the filesystem mode they get.
#: Everything else is refused loudly rather than approximated.
_BLOB_MODES = {"100644": 0o644, "100755": 0o755}


class GitExportError(GitError):
    """The index holds an entry the export cannot faithfully represent."""


def _parse_index_entries(raw: bytes) -> list[tuple[str, str, bytes]]:
    """``(mode, oid, path)`` for every entry in the index.

    ``-s -z``: NUL-terminated records carrying literal path bytes, so nothing is
    C-quoted and no path is ever decoded on the way through. Paths stay ``bytes``
    all the way to the filesystem call, because a repository is entitled to a
    filename that is not valid UTF-8 and decoding one with ``errors="replace"``
    would write a *different* file than the commit contains.
    """
    entries: list[tuple[str, str, bytes]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, separator, path = record.partition(b"\t")
        fields = meta.split(b" ")
        if not separator or len(fields) != 3:
            raise GitExportError(f"unparseable ls-files record: {meta!r}")
        mode, oid, stage = (field.decode("ascii", errors="replace") for field in fields)
        if stage != "0":
            # Stages 1/2/3 are an unresolved merge: three entries for one path,
            # none of which is "the commit". Nothing in the recorded task flow
            # produces one, so this means something went wrong upstream, and
            # exporting an arbitrary stage would hide it.
            raise GitExportError(f"index entry {path!r} is at merge stage {stage}")
        if not path or path.startswith(b"/") or b"\0" in path:
            raise GitExportError(f"refusing an unsafe index path: {path!r}")
        # Compared as bytes throughout: decoding to compare would be a second
        # place the path could change shape on the way to the filesystem.
        if b".." in path.split(b"/"):
            raise GitExportError(f"refusing an index path with a parent reference: {path!r}")
        entries.append((mode, oid, path))
    return entries


def _refuse_unsupported_modes(entries: Sequence[tuple[str, str, bytes]]) -> None:
    """Refuse symlinks and submodules, naming every offender at once.

    Both are refused rather than approximated, and for the same reason: the
    export's whole claim is that its bytes are the commit's bytes.

    * ``160000`` (gitlink) has no blob to write. ``checkout-index`` silently left
      an empty directory, so the suite ran against missing sources -- a
      confidently wrong benchmark number rather than an error.
    * ``120000`` (symlink) is not a file with content, so "the bytes match the
      commit" stops being a well-formed claim; and materialising one puts a path
      that resolves outside the export into a tree the host later deletes.

    The cost is real and accepted: a repository containing any symlink cannot be
    exported, and some real Python projects have one. Every offender is listed so
    a rejected benchmark repo is diagnosable at a glance rather than one
    re-run at a time, and the distinct exception type keeps a future
    ``RepoSpec`` opt-in a contained addition.
    """
    offenders = [(mode, path) for mode, _oid, path in entries if mode not in _BLOB_MODES]
    if not offenders:
        return
    described = ", ".join(f"{path.decode('utf-8', errors='replace')} (mode {mode})"
                          for mode, path in offenders[:10])
    raise GitExportError(
        f"{len(offenders)} index entr{'y' if len(offenders) == 1 else 'ies'} cannot be exported "
        f"faithfully: {described}"
    )


def _write_blob(destination: Path, path: bytes, content: bytes, mode: int,
                made: set[bytes], dir_mode: int) -> None:
    parts = path.split(b"/")
    current = destination
    prefix = b""
    for part in parts[:-1]:
        current = current / os.fsdecode(part)
        prefix = prefix + b"/" + part
        if prefix not in made:
            current.mkdir(exist_ok=True)
            # Explicitly, because mkdir's mode argument is masked by the umask.
            os.chmod(current, dir_mode)
            made.add(prefix)
    target = current / os.fsdecode(parts[-1])
    target.write_bytes(content)
    os.chmod(target, mode)


async def _write_blobs(process, entries: Sequence[tuple[str, str, bytes]],
                       destination: Path, dir_mode: int) -> None:
    """Feed oids to ``cat-file --batch`` and write each blob as it arrives.

    Feeding and draining run concurrently on purpose: writing every oid first
    deadlocks as soon as git's stdout pipe fills, which on any real repository is
    almost immediately. Writing each blob straight to disk keeps peak memory at
    one blob rather than one repository.
    """
    async def feed() -> None:
        for _mode, oid, _path in entries:
            process.stdin.write(f"{oid}\n".encode("ascii"))
        await process.stdin.drain()
        process.stdin.close()

    async def drain() -> None:
        made: set[bytes] = set()
        destination.mkdir(parents=True, exist_ok=True)
        os.chmod(destination, dir_mode)
        for mode, oid, path in entries:
            # `<oid> <type> <size>\n`, then <size> bytes, then a newline.
            # readuntil is limit-bound but a header never approaches it;
            # readexactly is not, so a large blob is fine.
            header = (await process.stdout.readuntil(b"\n")).decode("ascii", errors="replace").split()
            if len(header) != 3 or header[1] != "blob":
                # The other shape is `<oid> missing`. These oids came from this
                # repository's own index moments ago, so either means the object
                # store is damaged -- which must not be silently exported over.
                raise GitExportError(f"cat-file did not return a blob for {oid}: {' '.join(header)}")
            content = await process.stdout.readexactly(int(header[2]))
            await process.stdout.readexactly(1)
            _write_blob(destination, path, content, _BLOB_MODES[mode], made, dir_mode)

    await asyncio.gather(feed(), drain())


@dataclass(frozen=True)
class GitRepo:
    """Operations on one checkout.

    ``token_provider`` is awaited afresh for every authenticated command rather
    than resolved once at construction. Installation tokens last an hour and a
    bounded retry loop can outlive that, so a token minted at clone time may
    well be dead by the time the branch is pushed.
    """

    path: Path
    token_provider: TokenProvider | None = None
    #: The only host the credential helper will release the token to. Held here
    #: rather than read from the checkout's remote, because the checkout is
    #: exactly what an agent can rewrite.
    credential_host: str = DEFAULT_CREDENTIAL_HOST

    async def _run(self, *args: str, authenticated: bool = False, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
        token = None
        if authenticated and self.token_provider is not None:
            token = await self.token_provider()
        return await run_git(
            *args, cwd=self.path, token=token, credential_host=self.credential_host, timeout=timeout
        )

    async def head_sha(self) -> str:
        return (await self._run("rev-parse", "HEAD")).strip()

    async def current_branch(self) -> str:
        return (await self._run("rev-parse", "--abbrev-ref", "HEAD")).strip()

    async def is_shallow(self) -> bool:
        return (await self._run("rev-parse", "--is-shallow-repository")).strip() == "true"

    async def create_branch(self, name: str) -> None:
        await self._run("checkout", "-b", name)

    async def checkout(self, ref: str) -> None:
        await self._run("checkout", ref)

    async def status_porcelain(self) -> str:
        """Machine-readable working-tree state. Empty means clean, untracked files included."""
        return (await self._run("status", "--porcelain")).strip()

    async def has_changes(self) -> bool:
        return bool(await self.status_porcelain())

    async def commit_all(self, message: str) -> str | None:
        """Stage everything and commit. Returns the new sha, or None if there was nothing to commit.

        None is a real outcome, not an error: an Edit attempt that produced no
        change needs to be distinguishable from one that did, and `git commit`
        on a clean tree exits non-zero, which would otherwise surface as a
        generic failure.
        """
        if not await self.has_changes():
            log.info("git.commit.empty", path=str(self.path))
            return None

        await self._run("add", "--all")
        # Hooks are not cloned, so in practice there are none -- but a global
        # core.hooksPath would apply, and running a hook we did not vet on the
        # host is exactly what this stage should not do.
        await self._run("commit", "--message", message, "--no-verify")
        sha = await self.head_sha()
        log.info("git.commit", path=str(self.path), sha=sha)
        return sha

    async def reset_hard(self, sha: str) -> None:
        """Discard the working tree back to a commit -- how the Debugger abandons an attempt.

        ``clean -fd``, not ``-fdx``: ignored files stay. That leaves build
        artifacts from a bad attempt (a stale ``.so``, a compiled extension) in
        place for the next Verify run, which is a real hazard -- but ``-x``
        would delete ``.venv`` and ``node_modules`` along with them, which is
        worse. Neither is right; this is the less destructive half.
        """
        if not sha:
            # `record_attempt` returns None for an attempt that changed
            # nothing, and `rewind_to(last_attempt)` is the natural shape of
            # the Debugger loop -- so this is reachable by ordinary use, and
            # would otherwise surface as a TypeError from deep inside asyncio.
            raise ValueError("reset_hard requires a commit sha; got an empty value")
        await self._run("reset", "--hard", sha)
        await self._run("clean", "-fd")

    async def squash_onto(self, base_sha: str, message: str) -> str | None:
        """Collapse every commit since ``base_sha`` into one, in place on the current branch.

        Returns the squashed sha, or None when the branch has no net change
        against the base -- which happens if the agent edited and then reverted
        its own work, and is worth reporting rather than pushing an empty PR.
        The branch is left untouched in that case.

        Builds the commit *before* moving the branch. The obvious
        implementation -- ``reset --soft <base>`` then ``commit`` -- moves the
        ref first and commits second, so anything that fails in between strands
        the branch at base with every attempt commit unreachable, in a clone
        that is about to be deleted. That is not hypothetical: after Verify
        runs a test suite the tree is full of untracked artifacts, so a
        net-zero branch passes a ``status --porcelain`` guard (which counts
        untracked files) and then fails ``git commit`` (which does not) --
        losing the work at step 8, after everything else has succeeded.

        ``commit-tree`` has no such window. If it fails, nothing has moved.
        """
        if not message.strip():
            # `commit-tree` accepts an empty message where `git commit` refuses
            # one, so nothing downstream would catch this -- it would just
            # produce a PR with a blank commit message.
            raise ValueError("squash_onto requires a non-empty commit message")

        head_tree = (await self._run("rev-parse", "HEAD^{tree}")).strip()
        base_tree = (await self._run("rev-parse", f"{base_sha}^{{tree}}")).strip()
        if head_tree == base_tree:
            return None

        squashed = (await self._run("commit-tree", head_tree, "-p", base_sha, "-m", message)).strip()
        # The new commit carries the tree HEAD already has, so moving onto it
        # leaves index and working tree exactly as they were.
        await self._run("reset", "--soft", squashed)
        log.info("git.squash", path=str(self.path), base_sha=base_sha, sha=squashed)
        return squashed

    async def diff_from(self, base_sha: str) -> str:
        """The diff the Reviewer reads and the PR will show.

        Three dots: diff against the merge base, which is what GitHub renders.
        Here the branch descends from ``base_sha`` inside our own clone, so it
        matches the two-dot form -- but keeping the three-dot form means the
        Reviewer and the PR cannot disagree if that ever stops holding.
        """
        return await self._run("diff", f"{base_sha}...HEAD")

    async def changed_files_from(self, base_sha: str) -> list[str]:
        """Paths touched since the base. Feeds the no-test-edits success criterion."""
        out = await self._run("diff", "--name-only", "-z", f"{base_sha}...HEAD")
        return [path for path in out.split("\0") if path]

    async def tree_matches_head(self) -> bool:
        """Whether the index *and* the working tree are exactly HEAD's tree.

        The export reads tracked content at HEAD, so two different disagreements
        matter and the previous implementation only asked about one. It passed
        `diff-index --cached`, which compares the index to HEAD and **ignores
        the working tree** -- and the agent edits the working tree directly, with
        staging happening later in `record_attempt`. So an unstaged edit passed
        the guard, the export handed over HEAD's content, and the resulting
        pass/fail sets were attributed to an attempt whose changes were never in
        the tree that ran. A confidently wrong measurement rather than an error,
        which is the worst shape this stage can fail in.

        `status --porcelain` rather than `diff-index`: it refreshes the index
        itself, so a file whose mtime moved but whose content did not cannot read
        as modified, and it needs no exit-code interpretation. `diff-index
        --quiet` exits 1 for "differs" and 128 for a genuine failure, and the old
        code swallowed both alike -- so a corrupt repository reported "index
        differs", a true-looking answer to a question that was never asked. Any
        non-zero exit now raises, as everywhere else here.

        `--untracked-files=no` because untracked files are not a disagreement
        with HEAD: a test run leaves `.pytest_cache` and `__pycache__` behind,
        and neither belongs in the export or in this decision.
        """
        return not (await self._run("status", "--porcelain", "--untracked-files=no")).strip()

    async def export_index_to(self, destination: Path, *, dir_mode: int = 0o755) -> None:
        """Write the tracked tree into ``destination`` as the bytes HEAD holds.

        Reads the object store directly -- ``ls-files -s -z`` to enumerate,
        ``cat-file --batch`` to fetch -- rather than asking git to materialise a
        working tree. Every alternative applies some transformation, and each
        transformation is a way for what the sandbox runs to differ from what the
        commit contains and the Reviewer reads:

        * ``git archive`` honours ``export-ignore`` in ``.gitattributes``, which
          would let a repository hide its own test files from the run that
          establishes ground truth. An attack on the benchmark number rather than
          on the host, and the kind that would never look like an attack.
        * ``checkout-index`` -- the previous implementation -- honours neither
          ``export-ignore`` nor ``export-subst``, but *does* run clean/smudge
          filters, ``text``/``eol`` conversion and ``ident`` expansion. A tracked
          ``.gitattributes`` saying ``*.py text eol=crlf`` was enough to change
          the bytes; a ``filter`` driver runs an arbitrary command on the host.
          Pinning git's config files closed the half of that which came from the
          operator's ``~/.gitconfig``; it does **not** close the checkout's own
          ``.git/config``, which the sandbox can write. Reading blobs consults no
          attribute or filter machinery at all, so it closes both.

        Only index entries are listed, so ``.git`` is excluded by construction
        and so are untracked and ignored build artifacts -- which matters because
        ``reset_hard`` cleans with ``-fd`` rather than ``-fdx``, leaving a stale
        ``.so`` or ``.pyc`` that would otherwise be tested instead of the source.

        ``dir_mode`` is the caller's, because this module is a generic git
        wrapper and has no business knowing the sandbox's uid. It is applied to
        every directory created, not just the root: ``mkdir``'s mode argument is
        masked by the process umask, and the previous implementation chmodded
        only the top level, so ``checkout-index`` left ``0755`` subdirectories
        under a ``0777`` root and a sandbox running as an unprivileged uid got
        ``EACCES`` creating a ``__pycache__`` beside its own code. Files keep
        ``0644``/``0755`` from their index mode: what the sandbox needs is
        permission to create entries *in a directory*, and the executable bit has
        to survive for suites that shell out to a tracked script.

        Deliberately uncapped in total size. The content is already in the
        repository and already on this disk, so a cap would refuse large but
        legitimate repositories for no gain.
        """
        raw = await run_git_bytes(
            "ls-files", "-s", "-z", cwd=self.path, timeout=EXPORT_TIMEOUT_SECONDS
        )
        entries = _parse_index_entries(raw)
        _refuse_unsupported_modes(entries)
        if not entries:
            return

        process = await _spawn_git(
            "cat-file", "--batch",
            cwd=self.path,
            token=None,
            credential_host=self.credential_host,
            stdin=asyncio.subprocess.PIPE,
        )
        pgid = process.pid
        try:
            await asyncio.wait_for(
                _write_blobs(process, entries, destination, dir_mode), EXPORT_TIMEOUT_SECONDS
            )
        except TimeoutError:
            kill_process_tree(pgid, ("cat-file", "--batch"))
            raise GitTimeoutError(("cat-file", "--batch"), EXPORT_TIMEOUT_SECONDS) from None
        except asyncio.CancelledError:
            kill_process_tree(pgid, ("cat-file", "--batch"))
            raise
        finally:
            if process.returncode is None:
                kill_process_tree(pgid, ("cat-file", "--batch"))

    async def push_branch(self, branch: str, remote: str = "origin") -> None:
        await self._run(
            "push",
            "--set-upstream",
            remote,
            f"{branch}:{branch}",
            authenticated=True,
            timeout=PUSH_TIMEOUT_SECONDS,
        )
        log.info("git.push", path=str(self.path), branch=branch, remote=remote)


async def clone(
    url: str,
    destination: Path,
    branch: str,
    token_provider: TokenProvider | None = None,
) -> GitRepo:
    """Clone ``url`` at ``branch`` into ``destination`` and return a handle on it.

    Deliberately not shallow, and deliberately not ``--single-branch``. The
    index-freshness protocol resolves `git diff <indexed_sha> <current_sha>` in
    a brand-new clone, which only works if that earlier commit is present.
    Either flag would drop it, every index would silently degrade to a full
    reindex, and ``indexed_commit_sha`` would never save any work -- a
    correctness-shaped bug that shows up only as a cost regression.

    ``git clone --branch`` on its own still fetches all refs; it only decides
    which one HEAD lands on.
    """
    token = await token_provider() if token_provider is not None else None
    log.info("git.clone.started", url=redact(url), branch=branch, destination=str(destination))

    await run_git(
        "clone",
        "--branch",
        branch,
        url,
        str(destination),
        token=token,
        timeout=CLONE_TIMEOUT_SECONDS,
    )

    repo = GitRepo(path=destination, token_provider=token_provider)
    if await repo.is_shallow():
        # Nothing above asks for a shallow clone, so this means the environment
        # forced one (a clone filter, a GIT_* override). Failing here beats
        # discovering it later as an unexplained full reindex on every task.
        raise GitError(f"clone of {redact(url)} is shallow; incremental indexing requires full history")

    log.info("git.clone.done", branch=branch, sha=await repo.head_sha())
    return repo
