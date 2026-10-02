"""`run_limited_git`: the resource limits, the output cap and the process-group kill.

Aliases are used as a way to run something *inside* git's process tree (`git -c
alias.x='!cmd' x` starts a shell as git's child, in git's process group), which is
what lets a test observe the limits the child inherited and whether the group was
really killed. The alias text is the test's own, never a model string.
"""

import asyncio
import os
import resource
import shlex
import time

import pytest

from repolace_agents.tools import gitproc
from repolace_agents.tools.gitproc import prlimit_path, require_prlimit, run_limited_git
from repolace_shared.git import GitTimeoutError

from tools_support import make_checkout, make_harness

pytestmark = pytest.mark.anyio

MIB = 1024 * 1024


@pytest.fixture
def checkout(tmp_path):
    return make_checkout(tmp_path)


async def run(checkout, *args, **overrides):
    options = {
        "timeout": 20.0,
        "max_output_bytes": 1 * MIB,
        "max_memory_bytes": 512 * MIB,
        "max_cpu_seconds": 20,
    }
    options.update(overrides)
    return await run_limited_git(*args, cwd=checkout, **options)


async def alive(pid: int, within: float = 3.0) -> bool:
    """Is the process still there after `within` seconds? A zombie counts as gone once init reaps it."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not os.path.exists(f"/proc/{pid}"):
            return False
        await asyncio.sleep(0.05)
    return True


class TestLimitsReachTheChild:
    async def test_address_space_and_cpu_limits_are_applied_before_git_runs(self, checkout):
        # The shell is git's child and inherits git's limits; /proc/self/limits shows them.
        result = await run(checkout, "-c", "alias.lim=!cat /proc/self/limits", "lim", max_memory_bytes=300 * MIB, max_cpu_seconds=7)

        rows = {line[:25].strip(): line[25:].split() for line in result.stdout.decode().splitlines()}
        assert rows["Max address space"][:2] == [str(300 * MIB)] * 2
        assert rows["Max cpu time"][:2] == ["7", "12"]  # soft 7 (SIGXCPU), hard +5 (SIGKILL)

    async def test_the_limits_are_on_the_git_process_itself_and_not_on_this_one(self, checkout, tmp_path):
        # `prlimit` execs git in place: the pid we hold (and the process-group id we kill) is git's,
        # and /proc/<pid>/limits says git is limited while the Python process is not.
        before = resource.getrlimit(resource.RLIMIT_AS)
        pidfile = tmp_path / "gitpid"
        task = asyncio.ensure_future(
            run(checkout, "-c", f"alias.watch=!echo $PPID > {shlex.quote(str(pidfile))}; exec sleep 30", "watch",
                max_memory_bytes=300 * MIB, timeout=30.0)
        )
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        git_pid = int(pidfile.read_text())

        try:
            limits = open(f"/proc/{git_pid}/limits").read()
            assert open(f"/proc/{git_pid}/comm").read().strip() == "git"  # not a surviving `prlimit` parent
            assert f"{300 * MIB}" in next(line for line in limits.splitlines() if line.startswith("Max address space"))
            assert resource.getrlimit(resource.RLIMIT_AS) == before
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.2)

    def test_a_missing_prlimit_is_a_clear_error_at_construction_not_on_the_first_search(self, monkeypatch, tmp_path):
        gitproc.prlimit_path.cache_clear()
        monkeypatch.setenv("PATH", str(tmp_path))
        try:
            with pytest.raises(RuntimeError, match="prlimit.*util-linux"):
                require_prlimit()
        finally:
            gitproc.prlimit_path.cache_clear()

    def test_building_the_toolbox_without_prlimit_fails_clearly(self, tmp_path, monkeypatch):
        from repolace_agents.tools import build_toolbox

        harness = make_harness(tmp_path)
        gitproc.prlimit_path.cache_clear()
        monkeypatch.setenv("PATH", str(tmp_path))
        try:
            with pytest.raises(RuntimeError, match="prlimit.*util-linux"):
                build_toolbox(harness.ctx)
        finally:
            gitproc.prlimit_path.cache_clear()

    def test_prlimit_resolves_to_an_absolute_path(self):
        assert os.path.isabs(prlimit_path())

    async def test_a_command_that_wants_more_memory_than_the_limit_fails_instead_of_getting_it(self, checkout):
        # Allocates ~300 MiB against a 128 MiB limit; python is the test's own, not a model string.
        script = "x = bytearray(300 * 1024 * 1024)"
        result = await run(checkout, "-c", f"alias.eat=!python3 -c '{script}'", "eat", max_memory_bytes=128 * MIB)

        assert result.returncode != 0
        assert b"MemoryError" in result.stderr

    async def test_the_environment_is_the_sanitized_allowlist(self, checkout, monkeypatch):
        monkeypatch.setenv("REPOLACE_TEST_SECRET", "hunter2")

        result = await run(checkout, "-c", "alias.dumpenv=!env", "dumpenv")

        assert b"hunter2" not in result.stdout and b"REPOLACE_TEST_SECRET" not in result.stdout
        assert b"GIT_CONFIG_GLOBAL=/dev/null" in result.stdout

    async def test_bare_repositories_are_not_discovered(self, checkout):
        result = await run(checkout, "-c", "alias.dumpcfg=!git config --get safe.bareRepository", "dumpcfg")

        assert result.stdout.strip() == b"explicit"


class TestOutputCap:
    async def test_output_past_the_cap_kills_the_process_group_and_returns_a_prefix(self, checkout, tmp_path):
        pidfile = tmp_path / "pid"
        started = time.monotonic()

        result = await run(
            checkout, "-c", f"alias.flood=!echo $$ > {pidfile}; exec yes", "flood",
            max_output_bytes=5000, timeout=15.0,
        )

        assert time.monotonic() - started < 10  # killed at the cap, not left to run to the timeout
        assert result.output_cut and len(result.stdout) == 5000
        assert not await alive(int(pidfile.read_text()))

    async def test_output_under_the_cap_is_returned_whole_and_not_cut(self, checkout):
        result = await run(checkout, "-c", "alias.say=!echo hello", "say", max_output_bytes=5000)

        assert result.stdout == b"hello\n" and not result.output_cut and result.returncode == 0

    async def test_a_flood_of_stderr_is_capped_too(self, checkout):
        result = await run(checkout, "-c", "alias.noise=!yes >&2", "noise", max_stderr_bytes=1000, timeout=15.0)

        assert result.output_cut and len(result.stderr) == 1000


class TestTimeoutAndCancellation:
    async def test_a_timeout_raises_and_reaps_the_whole_group(self, checkout, tmp_path):
        pidfile = tmp_path / "pid"

        with pytest.raises(GitTimeoutError):
            await run(checkout, "-c", f"alias.hang=!echo $$ > {pidfile}; exec sleep 60", "hang", timeout=1.0)

        assert not await alive(int(pidfile.read_text()))

    async def test_a_grandchild_holding_the_pipe_after_git_exits_is_still_killed(self, checkout, tmp_path):
        # git exits at once; its background child keeps the pipe open and the group alive. The
        # group id must come from the pid cached at spawn: after git is reaped, looking it up
        # fails, the kill does nothing, and the orphan outlives the timeout.
        pidfile = tmp_path / "orphan"

        with pytest.raises(GitTimeoutError):
            await run(checkout, "-c", f"alias.orphan=!sleep 60 & echo $! > {shlex.quote(str(pidfile))}", "orphan", timeout=1.0)

        assert not await alive(int(pidfile.read_text()))

    async def test_cancellation_kills_the_group_too(self, checkout, tmp_path):
        pidfile = tmp_path / "pid"
        task = asyncio.ensure_future(run(checkout, "-c", f"alias.hang=!echo $$ > {pidfile}; exec sleep 60", "hang"))
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)  # let the loop see the child exit and close its pipes before the test ends

        assert not await alive(int(pidfile.read_text()))


class TestExitStatusIsReturnedNotRaised:
    async def test_no_match_is_exit_one(self, checkout):
        result = await run(checkout, "grep", "-e", "no_such_text_anywhere", "--", ".")

        assert result.returncode == 1 and result.stdout == b""

    async def test_a_signal_death_is_a_negative_code(self, checkout):
        result = await run(checkout, "-c", "alias.die=!kill -TERM $$", "die")

        assert result.returncode < 0 or result.returncode == 143
