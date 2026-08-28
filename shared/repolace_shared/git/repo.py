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

_BASE_ARGS: tuple[str, ...] = (
    # Same reason as retrieval.index: without it git C-quotes non-ASCII paths,
    # which then match neither the index nor an on-disk lookup.
    "-c", "core.quotepath=false",
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
    # Verify writes into the checkout, and .git is inside it. git treats parts
    # of .git as executable configuration, so a hook or a config-named command
    # dropped there would run on the *host* the next time the worker touches
    # the repo -- post-commit on an attempt, post-checkout on a branch,
    # pre-push at the end, core.fsmonitor on every status. These two shut the
    # cheapest doors. They are not the real fix: see the note in CLAUDE.md
    # about the .git trust boundary, which is an architectural decision tied to
    # how the sandbox is built.
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=",
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


def _git_env(token: str | None, credential_host: str) -> dict[str, str]:
    """Build git's environment from an allowlist rather than by subtracting from ours.

    Subtracting means every new secret added to the service environment is
    inherited by git and its helpers by default, and only stops being inherited
    if someone remembers to come back here. An allowlist fails the safe way.
    It also drops the GIT_CONFIG_* family, which is a config-injection route.
    """
    env = {name: value for name, value in os.environ.items() if name in _ENV_ALLOWLIST}
    # Without this an expired or wrong token makes git block on a credential
    # prompt forever. In a worker that is indistinguishable from a hung task,
    # and the timeout would be the only thing that ever ended it.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env[GIT_TOKEN_ENV_VAR] = token or ""
    env[GIT_HOST_ENV_VAR] = credential_host
    return env


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
    command = [*_BASE_ARGS, *args]
    process = await asyncio.create_subprocess_exec(
        "git",
        *command,
        cwd=cwd,
        env=_git_env(token, credential_host),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Some git subcommands read a message or a credential from stdin when
        # an argument is missing. Inheriting ours means such a command blocks
        # until the timeout instead of failing immediately -- and in a worker
        # there is nothing on the other end to answer it.
        stdin=asyncio.subprocess.DEVNULL,
        # Puts git in its own process group so the whole tree can be killed as
        # one. It also detaches from the controlling terminal, which backs up
        # GIT_TERMINAL_PROMPT=0: there is no tty left to prompt on.
        start_new_session=True,
    )
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

    return stdout.decode("utf-8", errors="replace")


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
