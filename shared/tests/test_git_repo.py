"""Unit tests for the git CLI wrapper, run against real repositories.

Two things here are load-bearing beyond ordinary correctness, and both are
recorded decisions the rest of the system quietly depends on:

* a task's clone must keep full history, or incremental indexing silently
  degrades to a full reindex on every task and never reports why;
* the installation token must never land inside the checkout, because the
  Verify stage runs LLM-generated code against that same tree.
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from repolace_shared.git.repo import (
    GIT_TOKEN_ENV_VAR,
    _BASE_ARGS,
    _git_env,
    GitCommandError,
    GitRepo,
    GitTimeoutError,
    clone,
    redact,
    run_git,
)

from support import git, write

pytestmark = pytest.mark.anyio

TOKEN = "ghs_exampletokenvalue0123456789"


async def clone_into(url: str, destination: Path, branch: str = "main", token: str | None = None) -> GitRepo:
    provider = None
    if token is not None:

        async def provider() -> str:
            return token

    return await clone(url, destination, branch=branch, token_provider=provider)


async def wait_for_pid_file(pid_file: Path, timeout: float = 5.0) -> None:
    """Wait until the stub has recorded its child, content and all -- the file appears before the write lands."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{pid_file} was never written")


def credential_fill(protocol: str, host: str, token: str, credential_host: str = "github.com") -> str:
    """Ask git for credentials the way git itself would, using the real helper and the real env.

    Goes around `run_git` because that closes stdin, and `git credential fill`
    is fed its query on stdin.
    """
    result = subprocess.run(
        ["git", *_BASE_ARGS, "credential", "fill"],
        input=f"protocol={protocol}\nhost={host}\n\n",
        env=_git_env(token, credential_host),
        capture_output=True,
        text=True,
    )
    return result.stdout


def process_alive(pid: int, settle_seconds: float = 3.0) -> bool:
    """Poll rather than check once: an orphan is reaped by init, which is quick but not instant."""
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        time.sleep(0.05)
    return True


@pytest.fixture
def fake_git(tmp_path: Path, monkeypatch):
    """Put a stub `git` first on PATH so failure modes can be provoked deterministically."""

    def install(script: str) -> None:
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        executable = bin_dir / "git"
        executable.write_text(f"#!/bin/sh\n{script}\n")
        executable.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    return install


class TestClone:
    async def test_checks_out_the_requested_branch(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work", branch="develop")

        assert await repo.current_branch() == "develop"
        assert (repo.path / "src" / "extra.py").exists()

    async def test_clone_is_not_shallow(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work")

        assert await repo.is_shallow() is False

    async def test_the_first_commit_is_still_resolvable(self, origin_url, tmp_path, source_repo):
        """The property incremental indexing rests on.

        ``reindex_if_stale`` runs ``git diff <indexed_sha> <current_sha>`` in a
        brand-new clone. A shallow or single-branch clone would not carry the
        older commit, the diff would fail, and every task would fall back to a
        full reindex -- costing money without ever failing.
        """
        first_sha = git(source_repo, "rev-list", "--max-parents=0", "HEAD")

        repo = await clone_into(origin_url, tmp_path / "work")

        assert await repo._run("rev-parse", "--verify", f"{first_sha}^{{commit}}")
        diff = await repo._run("diff", "--name-only", first_sha, "HEAD")
        assert "src/app.py" in diff

    async def test_branches_other_than_the_target_are_fetched(self, origin_url, tmp_path):
        """A commit indexed from another branch must still resolve, so --single-branch is out."""
        repo = await clone_into(origin_url, tmp_path / "work", branch="main")

        assert (await repo._run("rev-parse", "--verify", "origin/develop")).strip()

    async def test_head_sha_matches_the_origin_tip(self, origin_url, tmp_path, source_repo):
        repo = await clone_into(origin_url, tmp_path / "work")

        assert await repo.head_sha() == git(source_repo, "rev-parse", "main")


class TestCredentialHandling:
    async def test_the_token_is_not_written_anywhere_into_the_checkout(self, origin_url, tmp_path):
        """The Verify sandbox gets this tree. A token in it is a token the agent can read."""
        repo = await clone_into(origin_url, tmp_path / "work", token=TOKEN)

        leaked = [
            path
            for path in repo.path.rglob("*")
            if path.is_file() and TOKEN in path.read_bytes().decode("utf-8", errors="ignore")
        ]
        assert leaked == []

    async def test_the_remote_url_carries_no_credentials(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work", token=TOKEN)

        remote = (await repo._run("remote", "get-url", "origin")).strip()
        assert remote == origin_url
        assert "@" not in remote

    async def test_the_token_reaches_git_by_environment_not_argv(self, fake_git, tmp_path):
        """argv is world-readable on Linux; a process environment is not."""
        argv_file = tmp_path / "argv.txt"
        env_file = tmp_path / "env.txt"
        fake_git(
            f'printf "%s\\n" "$@" > {argv_file}\n'
            f'printf "token=%s\\nprompt=%s\\n" "${GIT_TOKEN_ENV_VAR}" "$GIT_TERMINAL_PROMPT" > {env_file}'
        )

        await run_git("status", token=TOKEN)

        argv = argv_file.read_text()
        assert TOKEN not in argv
        assert f"${GIT_TOKEN_ENV_VAR}" in argv, "the helper should reference the variable, not its value"
        assert f"token={TOKEN}" in env_file.read_text()

    async def test_terminal_prompting_is_disabled(self, fake_git, tmp_path):
        """Otherwise a bad token makes git wait for input forever, which reads as a hung task."""
        env_file = tmp_path / "env.txt"
        fake_git(f'printf "%s" "$GIT_TERMINAL_PROMPT" > {env_file}')

        await run_git("status")

        assert env_file.read_text() == "0"

    def test_the_token_is_released_to_github(self):
        """Drives the real helper through git, which nothing else here does.

        Every integration test above uses a `file://` remote, which never
        invokes git's credential machinery at all -- so a typo inside the
        helper's shell would pass the whole suite and fail on the first push.
        """
        assert TOKEN in credential_fill("https", "github.com", TOKEN)

    @pytest.mark.parametrize(
        ("protocol", "host", "why"),
        [
            ("https", "evil.example.com", "a remote.origin.url the sandbox rewrote"),
            ("https", "github.com.evil.net", "a lookalike domain"),
            ("http", "github.com", "a downgrade to cleartext"),
            ("http", "169.254.169.254", "the cloud metadata endpoint"),
        ],
    )
    def test_the_token_is_withheld_from_every_other_host(self, protocol, host, why):
        """Keeping the token out of the tree is not enough on its own.

        The tree still decides *where* git sends it: Verify can rewrite
        `remote.origin.url`, add `url.<evil>.insteadOf` (which rewrites
        explicit URLs too), or set `http.proxy`. A helper that answers whatever
        it is asked turns any of those into credential theft.
        """
        assert TOKEN not in credential_fill(protocol, host, TOKEN), f"token released to {why}"

    async def test_service_secrets_are_not_handed_to_git(self, fake_git, tmp_path, monkeypatch):
        """Phase 1 runs the pipeline inline in the API process, which holds the App private key.

        git spawns helpers, and anything in git's environment reaches them. The
        private key is worse than any single token: it mints installation
        tokens for every installation and survives token rotation.
        """
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", "SUPER-SECRET-APP-KEY")
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:hunter2@db/repolace")
        env_file = tmp_path / "env.txt"
        fake_git(f"env > {env_file}")

        await run_git("status", token=TOKEN)

        dumped = env_file.read_text()
        assert "SUPER-SECRET-APP-KEY" not in dumped
        assert "hunter2" not in dumped
        assert f"{GIT_TOKEN_ENV_VAR}={TOKEN}" in dumped, "the token itself must still get through"

    async def test_repository_hooks_are_disabled(self, origin_url, tmp_path):
        """`.git` lives inside the tree Verify writes to, and git runs parts of it as code.

        `--no-verify` covers pre-commit and commit-msg only -- not post-commit,
        and nothing at all on checkout or push.
        """
        repo = await clone_into(origin_url, tmp_path / "work")
        marker = tmp_path / "hook-ran"
        hooks = repo.path / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for hook in ("post-commit", "post-checkout"):
            path = hooks / hook
            path.write_text(f"#!/bin/sh\ntouch {marker}\n")
            path.chmod(0o755)

        await repo.create_branch("agent")
        write(repo.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")
        await repo.commit_all("attempt")

        assert not marker.exists(), "a hook planted in the checkout executed on the host"


class TestFailures:
    async def test_non_zero_exit_raises_with_the_return_code(self, fake_git):
        fake_git('echo "fatal: nope" >&2\nexit 3')

        with pytest.raises(GitCommandError) as exc_info:
            await run_git("status")

        assert exc_info.value.returncode == 3
        assert "fatal: nope" in exc_info.value.stderr

    async def test_a_token_in_git_stderr_is_redacted(self, fake_git):
        """Errors end up in logs, and in Phase 3 those logs leave the host."""
        fake_git(f'echo "fatal: bad credentials for {TOKEN}" >&2\nexit 128')

        with pytest.raises(GitCommandError) as exc_info:
            await run_git("status")

        assert TOKEN not in str(exc_info.value)
        assert "<redacted-secret>" in exc_info.value.stderr

    async def test_a_hung_command_is_killed_at_the_timeout(self, fake_git, tmp_path):
        """A backgrounded child makes this a test of the process *group* kill.

        Killing only git leaves helpers -- its transport, its credential helper
        -- holding the stdout pipe open, and ``Process.wait()`` does not return
        until every pipe closes. The timeout would then wait out the orphan it
        was meant to cut short, which is the opposite of having a timeout.
        """
        pid_file = tmp_path / "child.pid"
        fake_git(f"sleep 30 &\necho $! > {pid_file}\nwait")

        started = time.monotonic()
        with pytest.raises(GitTimeoutError):
            await run_git("status", timeout=0.5)
        elapsed = time.monotonic() - started

        assert elapsed < 5, f"waited out the orphan instead of killing it ({elapsed:.1f}s)"
        assert not process_alive(int(pid_file.read_text())), "the backgrounded child outlived the timeout"

    async def test_an_orphan_is_killed_even_when_git_exits_first(self, fake_git, tmp_path):
        """The silent variant, and the reason the pgid is cached rather than looked up.

        Here git exits immediately while its child keeps the pipe open. asyncio
        reaps git, so `os.getpgid(pid)` would raise exactly when the kill is
        needed -- and `wait()` returns instantly off the recorded exit code, so
        the timing looks perfect while the orphan survives.
        """
        pid_file = tmp_path / "child.pid"
        fake_git(f"sleep 30 &\necho $! > {pid_file}\nexit 0")

        with pytest.raises(GitTimeoutError):
            await run_git("status", timeout=0.5)

        assert not process_alive(int(pid_file.read_text())), "orphan survived after git was reaped"

    async def test_git_gets_no_stdin_to_block_on(self, fake_git, tmp_path):
        """Some git subcommands read a message or a credential from stdin when an argument
        is missing. Inheriting ours means such a command blocks until the timeout, with
        nothing on the other end to answer it.

        Partial guard, deliberately: pytest already puts /dev/null on fd 0, so
        deleting the `stdin=DEVNULL` argument does not change what this sees.
        It does catch a change to `PIPE` (fd 0 would read `pipe:[...]`), which
        is the plausible edit. The removal case was verified by hand instead --
        `git commit-tree` with no `-m` blocks indefinitely on an inherited
        stdin.
        """
        fd_file = tmp_path / "stdin.txt"
        fake_git(f"readlink /proc/self/fd/0 > {fd_file}")

        await run_git("status")

        assert fd_file.read_text().strip() == "/dev/null"

    async def test_a_failure_reported_only_on_stdout_still_explains_itself(self, fake_git):
        """git writes plenty of diagnostics to stdout; an error with an empty reason is useless."""
        fake_git('echo "nothing added to commit but untracked files present"\nexit 1')

        with pytest.raises(GitCommandError) as exc_info:
            await run_git("commit")

        assert "nothing added to commit" in str(exc_info.value)

    async def test_cancelling_a_task_does_not_leave_git_running(self, fake_git, tmp_path):
        """A bounded-retry loop that gives up mid-clone must not leak the clone."""
        pid_file = tmp_path / "child.pid"
        fake_git(f"sleep 30 &\necho $! > {pid_file}\nwait")

        task = asyncio.create_task(run_git("status", timeout=30))
        await wait_for_pid_file(pid_file)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not process_alive(int(pid_file.read_text())), "cancellation left the child running"


class TestCommits:
    async def test_commit_all_returns_the_new_head(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work")
        write(repo.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")

        sha = await repo.commit_all("fix add")

        assert sha == await repo.head_sha()

    async def test_untracked_files_are_included(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work")
        write(repo.path / "src" / "new_module.py", "X = 1\n")

        await repo.commit_all("add module")

        assert not await repo.has_changes()

    async def test_a_clean_tree_commits_nothing_and_says_so(self, origin_url, tmp_path):
        """An Edit attempt that changed nothing must be distinguishable from one that did."""
        repo = await clone_into(origin_url, tmp_path / "work")
        before = await repo.head_sha()

        assert await repo.commit_all("no-op") is None
        assert await repo.head_sha() == before

    async def test_rewinding_to_a_missing_sha_is_rejected_clearly(self, origin_url, tmp_path):
        """`record_attempt` returns None for an empty attempt, and feeding that straight
        to `rewind_to` is the natural shape of the Debugger loop."""
        repo = await clone_into(origin_url, tmp_path / "work")

        with pytest.raises(ValueError, match="requires a commit sha"):
            await repo.reset_hard(None)

    async def test_reset_hard_discards_edits_and_untracked_files(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work")
        checkpoint = await repo.head_sha()
        write(repo.path / "src" / "app.py", "broken(\n")
        write(repo.path / "junk.log", "debug output\n")

        await repo.reset_hard(checkpoint)

        assert not await repo.has_changes()
        assert not (repo.path / "junk.log").exists()
        assert "return a - b" in (repo.path / "src" / "app.py").read_text()


class TestSquash:
    async def test_three_attempts_collapse_to_one_commit(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work")
        base = await repo.head_sha()
        await repo.create_branch("agent")
        for attempt in range(3):
            write(repo.path / "src" / "app.py", f"def add(a, b):\n    return a + b  # {attempt}\n")
            await repo.commit_all(f"attempt {attempt}")

        squashed = await repo.squash_onto(base, "Fix add")

        assert squashed is not None
        assert (await repo._run("rev-list", "--count", f"{base}..HEAD")).strip() == "1"
        assert (await repo._run("log", "-1", "--pretty=%s")).strip() == "Fix add"
        assert "# 2" in (repo.path / "src" / "app.py").read_text()

    async def test_no_commits_means_nothing_to_squash(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work")
        base = await repo.head_sha()
        await repo.create_branch("agent")

        assert await repo.squash_onto(base, "Fix") is None

    async def test_work_that_nets_out_to_nothing_is_reported_not_committed(self, origin_url, tmp_path):
        """An agent that edits and then reverts should not produce an empty PR.

        The branch must be left alone while reporting that. Rewinding it to
        base here would orphan the attempt commits in a clone that is about to
        be deleted, destroying the only record of what the agent tried.
        """
        repo = await clone_into(origin_url, tmp_path / "work")
        base = await repo.head_sha()
        original = (repo.path / "src" / "app.py").read_text()
        await repo.create_branch("agent")
        write(repo.path / "src" / "app.py", "def add(a, b):\n    return 0\n")
        await repo.commit_all("attempt")
        write(repo.path / "src" / "app.py", original)
        revert_sha = await repo.commit_all("revert")

        assert await repo.squash_onto(base, "Fix") is None
        assert await repo.head_sha() == revert_sha, "the branch should not have moved"
        assert (await repo._run("rev-list", "--count", f"{base}..HEAD")).strip() == "2"

    async def test_untracked_artifacts_do_not_destroy_the_branch(self, origin_url, tmp_path):
        """The exact shape that used to lose the work.

        Verify leaves untracked artifacts behind, so `status --porcelain` is
        non-empty while the net diff against base is zero. The old
        reset-then-commit order moved the branch to base and *then* failed the
        commit, stranding every attempt.
        """
        repo = await clone_into(origin_url, tmp_path / "work")
        base = await repo.head_sha()
        original = (repo.path / "src" / "app.py").read_text()
        await repo.create_branch("agent")
        write(repo.path / "src" / "app.py", "def add(a, b):\n    return 0\n")
        attempt = await repo.commit_all("attempt")
        write(repo.path / "src" / "app.py", original)
        await repo.commit_all("revert")
        write(repo.path / ".pytest_cache" / "lastfailed", "{}\n")

        assert await repo.status_porcelain(), "precondition: the tree looks dirty"
        assert await repo.squash_onto(base, "Fix") is None
        assert attempt in (await repo._run("rev-list", f"{base}..HEAD")), "attempt commits must survive"

    async def test_an_empty_commit_message_is_refused_before_anything_moves(self, origin_url, tmp_path):
        """`commit-tree` accepts a blank message where `git commit` refuses one."""
        repo = await clone_into(origin_url, tmp_path / "work")
        base = await repo.head_sha()
        await repo.create_branch("agent")
        write(repo.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")
        attempt = await repo.commit_all("attempt")

        with pytest.raises(ValueError, match="non-empty commit message"):
            await repo.squash_onto(base, "   ")

        assert await repo.head_sha() == attempt, "the branch should not have moved"


class TestDiff:
    async def test_diff_from_base_shows_the_agent_change(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work")
        base = await repo.head_sha()
        await repo.create_branch("agent")
        write(repo.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")
        await repo.commit_all("fix")

        diff = await repo.diff_from(base)

        assert "src/app.py" in diff
        assert "+    return a + b" in diff
        assert "-    return a - b" in diff

    async def test_changed_files_lists_paths_relative_to_the_root(self, origin_url, tmp_path):
        repo = await clone_into(origin_url, tmp_path / "work")
        base = await repo.head_sha()
        await repo.create_branch("agent")
        write(repo.path / "src" / "app.py", "def add(a, b):\n    return a + b\n")
        write(repo.path / "tests" / "test_app.py", "def test_add():\n    assert True\n")
        await repo.commit_all("fix and test")

        assert sorted(await repo.changed_files_from(base)) == ["src/app.py", "tests/test_app.py"]


class TestRedact:
    def test_installation_tokens_are_masked(self):
        assert redact(f"remote: rejected {TOKEN}") == "remote: rejected <redacted-secret>"

    def test_ordinary_text_is_untouched(self):
        assert redact("fatal: could not read from remote repository") == (
            "fatal: could not read from remote repository"
        )
