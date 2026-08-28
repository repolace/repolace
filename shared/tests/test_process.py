"""Unit tests for the shared subprocess runner.

Real child processes, no mocks: the behaviour worth defending here is what
happens to an actual process group under a deadline, and a stubbed
``create_subprocess_exec`` would assert only that we call it.

The two cases that matter most are the ones that look fine from the outside --
an orphan surviving a kill while the timing looks perfect, and a child blocking
forever on a pipe nobody is draining.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

from repolace_shared.process import run_process

pytestmark = pytest.mark.anyio


@pytest.fixture
def script(tmp_path: Path):
    """Write an executable shell script and hand back its path."""

    def make(body: str, name: str = "stub") -> str:
        path = tmp_path / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return str(path)

    return make


def process_alive(pid: int, settle_seconds: float = 3.0) -> bool:
    """Poll rather than check once: a killed orphan is reaped by init, quickly but not instantly."""
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        time.sleep(0.05)
    return True


async def wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} was never written")


class TestOrdinaryRuns:
    async def test_a_non_zero_exit_is_returned_not_raised(self, script):
        """pytest exits 1 for 'tests failed' -- an ordinary result, not an error."""
        result = await run_process(script("exit 7"), timeout=10)

        assert result.returncode == 7
        assert result.timed_out is False

    async def test_both_streams_are_captured(self, script):
        result = await run_process(script('echo out; echo err >&2'), timeout=10)

        assert result.stdout.strip() == b"out"
        assert result.stderr.strip() == b"err"

    async def test_stdin_is_closed_so_a_reader_cannot_hang(self, script, tmp_path):
        fd = tmp_path / "fd0"
        result = await run_process(script(f"readlink /proc/self/fd/0 > {fd}"), timeout=10)

        assert result.returncode == 0
        assert fd.read_text().strip() == "/dev/null"

    async def test_the_environment_is_what_we_pass(self, script, tmp_path):
        out = tmp_path / "env"
        result = await run_process(
            script(f'printf "%s" "$ONLY_THIS" > {out}'), env={"ONLY_THIS": "yes"}, timeout=10
        )

        assert result.returncode == 0
        assert out.read_text() == "yes"


class TestDeadline:
    async def test_a_hung_command_is_killed_rather_than_waited_out(self, script):
        started = time.monotonic()
        result = await run_process(script("sleep 30"), timeout=0.5)
        elapsed = time.monotonic() - started

        assert result.timed_out is True
        assert elapsed < 5, f"waited out the command instead of killing it ({elapsed:.1f}s)"

    async def test_an_orphan_is_killed_even_when_the_parent_exits_first(self, script, tmp_path):
        """The silent case, and the reason the pgid is captured at spawn.

        Here the parent exits immediately while its child keeps the pipe open.
        The parent is reaped, so a kill-time ``os.getpgid`` would raise exactly
        when the kill is needed -- and ``wait()`` returns instantly off the
        recorded exit code, so the timing looks perfect while the orphan lives.
        """
        pid_file = tmp_path / "child.pid"
        result = await run_process(
            script(f"sleep 30 &\necho $! > {pid_file}\nexit 0"), timeout=0.5
        )

        assert result.timed_out is True
        assert not process_alive(int(pid_file.read_text())), "orphan survived the deadline"

    async def test_cancelling_does_not_leave_the_command_running(self, script, tmp_path):
        pid_file = tmp_path / "child.pid"
        task = asyncio.create_task(
            run_process(script(f"sleep 30 &\necho $! > {pid_file}\nwait"), timeout=30)
        )
        await wait_for_file(pid_file)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not process_alive(int(pid_file.read_text())), "cancellation left the child running"


class TestCapture:
    async def test_output_past_the_cap_is_dropped_without_stalling_the_child(self, script):
        """The child must still finish.

        If the runner stopped reading at the cap, the pipe would fill and the
        child would block on write forever -- turning a chatty test suite into a
        hung task. Reaching a real exit code is the proof that never happens.
        """
        body = "python3 -c \"import sys;sys.stdout.write('x'*2000000)\"\nexit 3"
        result = await run_process(script(body), timeout=30, capture_limit=1024)

        assert result.returncode == 3, "the child did not finish; it was blocked on a full pipe"
        assert result.timed_out is False
        assert result.truncated is True
        assert len(result.stdout) == 1024

    async def test_output_under_the_cap_is_not_marked_truncated(self, script):
        result = await run_process(script("echo small"), timeout=10, capture_limit=1024)

        assert result.truncated is False
        assert result.stdout.strip() == b"small"
