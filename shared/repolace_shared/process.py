"""Running one external command, safely, with a deadline.

Extracted from ``repolace_shared.git.repo``, which needs the same spawn and
teardown discipline for git as the Verify sandbox needs for the container
runtime. The subtle parts -- capturing the process group id at spawn rather
than looking it up at kill time, and killing on cancellation as well as on
timeout -- were learned there against real failures, so they are reproduced
here rather than reinvented.

Two deliberate differences from ``run_git``:

* A non-zero exit is returned, not raised. ``pytest`` exits 1 for "tests
  failed", which is an ordinary result this has to report rather than an error.
* Output is captured up to a cap. ``communicate()`` buffers without limit, and
  the code being run is untrusted -- a suite that prints without stopping would
  otherwise exhaust the worker's memory.
"""

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger()

#: How long to wait for a killed process group to actually die.
REAP_TIMEOUT_SECONDS = 5.0

#: Bytes kept per stream. Beyond this the output is still read and thrown away
#: -- see ``_read_capped`` for why it cannot simply stop reading.
DEFAULT_CAPTURE_LIMIT = 256 * 1024

_READ_CHUNK = 64 * 1024


def kill_process_tree(pgid: int, command: Sequence[str]) -> None:
    """SIGKILL the whole process group, not just the process we spawned.

    A command that delegates to helpers -- git to `git-remote-https` and its
    credential shell, `docker run` to nothing much but a container that outlives
    it -- leaves those children holding the stdout/stderr pipe write-ends open.
    That strands the command in one of two ways depending on timing:

    * If the parent has not been reaped yet, ``Process.wait()`` waits on the
      transport, and the transport only finishes once the process has exited
      *and* every pipe is disconnected -- so the orphan holds it open.
    * If the parent has already exited and been reaped, ``wait()``
      short-circuits on the recorded return code and comes back instantly. That
      is the dangerous case: the timing looks perfect while the orphan quietly
      survives.

    ``pgid`` is passed in rather than looked up with ``os.getpgid`` precisely
    because of the second case -- the pid is gone by then and the lookup raises,
    which is exactly when the kill is needed most. ``start_new_session`` makes
    the child a group leader, so its pid *is* the pgid, and the group stays
    addressable while any member is alive even after the leader is reaped.
    """
    if pgid == os.getpgrp():
        # start_new_session did not take effect; killing this group would take
        # the worker down with it.
        log.error("process.kill.refused_own_group", command=list(command), pgid=pgid)
        return

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as exc:
        log.warning("process.kill.group_failed", command=list(command), error=str(exc))


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    #: Either stream hit the capture cap. The command still ran to completion.
    truncated: bool = False
    #: The deadline expired and the process group was killed. ``returncode`` is
    #: whatever the kill produced and carries no information about the work.
    timed_out: bool = False


async def _read_capped(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read to EOF, keeping at most ``limit`` bytes.

    Keeps reading after the cap instead of walking away: an unread pipe fills,
    and a child blocked forever on a full pipe is a worse outcome than the
    memory the discarded bytes would have cost.
    """
    kept: list[bytes] = []
    total = 0
    truncated = False

    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return b"".join(kept), truncated
        if total >= limit:
            truncated = True
            continue
        room = limit - total
        kept.append(chunk[:room])
        total += min(room, len(chunk))
        if len(chunk) > room:
            truncated = True


async def run_process(
    program: str,
    *args: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float,
    capture_limit: int = DEFAULT_CAPTURE_LIMIT,
) -> ProcessResult:
    """Run a command to completion or to its deadline. Never raises on exit status."""
    command = (program, *args)
    process = await asyncio.create_subprocess_exec(
        program,
        *args,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Nothing here is interactive. Inheriting stdin means a command that
        # reads it blocks until the deadline instead of failing at once.
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    # Captured now, while the pid is certainly valid: start_new_session makes
    # the child its own group leader, so the pgid equals the pid, and this stays
    # killable after the child itself has been reaped.
    pgid = process.pid

    async def collect() -> tuple[bytes, bool, bytes, bool]:
        assert process.stdout is not None and process.stderr is not None
        (out, out_cut), (err, err_cut) = await asyncio.gather(
            _read_capped(process.stdout, capture_limit),
            _read_capped(process.stderr, capture_limit),
        )
        await process.wait()
        return out, out_cut, err, err_cut

    try:
        stdout, stdout_cut, stderr, stderr_cut = await asyncio.wait_for(collect(), timeout)
    except TimeoutError:
        kill_process_tree(pgid, command)
        try:
            await asyncio.wait_for(process.wait(), REAP_TIMEOUT_SECONDS)
        except TimeoutError:
            # Nothing more to do -- but a deadline that does not return is worse
            # than a leaked process, so give up on reaping and report.
            log.warning("process.kill.reap_timeout", command=list(command))
        log.warning("process.timeout", command=list(command), timeout=timeout)
        return ProcessResult(
            returncode=process.returncode if process.returncode is not None else -signal.SIGKILL,
            stdout=b"",
            stderr=b"",
            timed_out=True,
        )
    except asyncio.CancelledError:
        # A cancelled task must not leave the command running behind it. No
        # await while unwinding a cancellation: the group is already SIGKILLed
        # and the child watcher will reap it.
        kill_process_tree(pgid, command)
        raise

    return ProcessResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        truncated=stdout_cut or stderr_cut,
    )
