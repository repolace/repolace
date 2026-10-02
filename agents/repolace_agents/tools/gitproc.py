"""Running git with resource limits, for the one tool whose argument is a regular expression.

`run_git` bounds wall-clock time and nothing else, which is not enough for
`grep -E`. git's regex engine is glibc's: a counted repetition nested three deep
(`((a{1,200}){1,200}){1,200}b`, 28 characters) compiles to billions of states
and was measured at ~5 GB resident after twelve seconds, and a backreference
makes matching exponential in CPU. Both are reachable from a pattern the model
chose after reading text anyone can file. So this runner puts the child under
`RLIMIT_AS` and `RLIMIT_CPU`, and bounds the bytes it will read back instead of
buffering whatever the process prints.

It reuses `run_git`'s hardening rather than inventing its own: the environment is
`sanitized_git_env` (an allowlist, with the config files pinned to /dev/null), the
config pins are `UNTRUSTED_TREE_CONFIG_ARGS` plus `safe.bareRepository=explicit`,
git runs in its own process group, and on a timeout, an output overflow or a
cancellation the **whole group** is killed with the pgid cached at spawn (see
`kill_process_tree` for why it cannot be looked up later).

Lives in the tools package, not in `repolace_shared.git.repo`, because that is
shared with every other stream; the one place that needs limits carries them.
"""

from __future__ import annotations

import asyncio
import resource
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from repolace_shared.git import UNTRUSTED_TREE_CONFIG_ARGS, GitTimeoutError, sanitized_git_env
from repolace_shared.process import REAP_TIMEOUT_SECONDS, kill_process_tree

_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class LimitedGitResult:
    #: Negative when git was killed by a signal -- `-24` is `SIGXCPU`, the CPU limit.
    returncode: int
    stdout: bytes
    stderr: bytes
    #: stdout or stderr reached its byte cap, so the process group was killed and
    #: `stdout` is a prefix of what git would have printed. `returncode` then says
    #: nothing about the work.
    output_cut: bool


def _limit_child(memory_bytes: int, cpu_seconds: int) -> Callable[[], None]:
    """The `preexec_fn` that puts the child under its limits before it execs git.

    Before exec rather than `prlimit` after spawn, so there is no window in which git
    runs unlimited. The CPU hard limit is a few seconds past the soft one: the soft
    limit sends SIGXCPU, which ends git; the hard limit is SIGKILL if it somehow does not.
    """

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))

    return apply


async def _read_capped(stream: asyncio.StreamReader, limit: int, on_overflow: Callable[[], None]) -> tuple[bytes, bool]:
    """Read to EOF, keeping at most `limit` bytes, and call `on_overflow` once past them.

    `on_overflow` kills git, and the stream is **still read to EOF afterwards**, the bytes
    discarded. Walking away instead hangs: asyncio pauses a pipe once its buffer is full,
    a paused pipe never delivers EOF, and `Process.wait()` waits on the transport as well
    as on the exit -- so a killed git whose pipe still holds data is never reaped. (Seen
    as a test that passed alone and timed out one run in three.)
    """
    kept = bytearray()
    overflowed = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return bytes(kept), overflowed
        room = limit - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > max(room, 0) and not overflowed:
            overflowed = True
            on_overflow()


async def run_limited_git(
    *args: str,
    cwd: Path,
    timeout: float,
    max_output_bytes: int,
    max_stderr_bytes: int = 64 * 1024,
    max_memory_bytes: int,
    max_cpu_seconds: int,
) -> LimitedGitResult:
    """Run one git command under resource limits. Never raises on exit status.

    Raises `GitTimeoutError` when `timeout` expires (the process group is killed and
    reaped first). A non-zero exit, a signal death and an output overflow are all
    returned: the caller decides what each means for its tool.
    """
    command = ("git", *args)
    process = await asyncio.create_subprocess_exec(
        "git",
        *UNTRUSTED_TREE_CONFIG_ARGS,
        # A bare repository planted in the tree must not be picked up as the one to operate on.
        "-c", "safe.bareRepository=explicit",
        *args,
        cwd=cwd,
        env=sanitized_git_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Nothing here is interactive; an inherited stdin would block until the timeout.
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        preexec_fn=_limit_child(max_memory_bytes, max_cpu_seconds),
    )
    # Cached while the pid is certainly valid: `start_new_session` makes git its own group
    # leader, so the pgid is the pid and the group stays killable after git is reaped.
    pgid = process.pid

    def kill() -> None:
        kill_process_tree(pgid, command)

    async def collect() -> tuple[bytes, bool, bytes, bool]:
        assert process.stdout is not None and process.stderr is not None
        (out, out_cut), (err, err_cut) = await asyncio.gather(
            _read_capped(process.stdout, max_output_bytes, kill),
            _read_capped(process.stderr, max_stderr_bytes, kill),
        )
        await process.wait()
        return out, out_cut, err, err_cut

    try:
        stdout, stdout_cut, stderr, stderr_cut = await asyncio.wait_for(collect(), timeout)
    except TimeoutError:
        kill()
        try:
            await asyncio.wait_for(process.wait(), REAP_TIMEOUT_SECONDS)
        except TimeoutError:
            pass  # a deadline that does not return is worse than a leaked process
        raise GitTimeoutError(args, timeout) from None
    except asyncio.CancelledError:
        # No await while unwinding a cancellation: the group is SIGKILLed and the child
        # watcher reaps it.
        kill()
        raise

    return LimitedGitResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        output_cut=stdout_cut or stderr_cut,
    )
