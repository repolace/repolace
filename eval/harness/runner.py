"""Run the queued tasks of one benchmark run (`repolace-eval run`).

Starts up to K `repolace_pipeline.cli` subprocesses at a time (the same entry
point as the `repolace-run-task` script, started through the interpreter running
this harness so it needs nothing on `PATH`), each in its own session so a hung
task can be killed as a group. Output goes to `eval/runs/<run>/<task>.log`.

**Memory: K is a RAM decision.** Every child loads the ~1-2 GB embedding model
(torch) *and* starts a container with a 2 GB limit, so K=3 wants roughly 10 GB
free before the sandbox's own page cache. The peak resident set of each child is
sampled from `/proc/<pid>/status` (`VmHWM`) every couple of seconds and logged
when it exits; it is a lower bound (a spike between samples is missed) and absent
off Linux. Read it from the dry run before choosing K for the sweep.

**A task row is never re-run, so the runner never makes one QUEUED again.** The
pipeline's `task_test_runs` is unique on `(task_id, attempt)`; a second run of a
row would fail on insert at best and mix two runs' evidence at worst. Every
status write here is `RUNNING -> FAILED`, conditional on the row still being
RUNNING (see `harness.db.fail_if_running`), so a task that finished in the same
instant keeps its real status. That covers three cases: a child killed on the
wall-clock timeout, a child that died (OOM kill, crash) leaving its row RUNNING,
and `--mark-abandoned` for rows whose runner itself died.

**Exit codes are the pipeline's** (`repolace_pipeline.cli`): 0 ran, whatever the
outcome, so a COMPLETED task with a stop reason is never treated as a crash; 1
repolace itself broke (the row is FAILED); 2 not found / not wired; 3 not
claimable. A child that exits 2 or 3 never owned the row, so its row is not
touched: 3 can mean another runner is running it right now.

**The run manifest.** `enqueue` writes `eval/runs/<run>/manifest.json` naming the
agent, the model and the wall-clock cap the sweep is meant to use. The runner
refuses to start (exit 2) when its own `--agent`, `--model` or `--timeout-seconds`
disagree with it, because the report describes the sweep from that file and a
mismatch would make it describe something that did not run. A run with no manifest
is allowed through with a warning. Transient provider errors are retried only by
the gateway inside each task; the runner never retries or re-runs a task.

**Cost cap.** `--max-total-usd` is checked before each dispatch against
`SUM(llm_calls.cost_usd)` for the run, which includes tasks from earlier
invocations. Tasks already in flight finish, so the total can exceed the cap by
up to K times the gateway's per-task cap.

**The child's environment** is an allowlist of this process's environment (see
`allowlisted_env`), never the whole shell: `REPOLACE_BENCH_GITHUB_TOKEN`, `GH_TOKEN`
and provider keys exported in the shell do not reach the child. Provider keys and the
GitHub App key reach the pipeline through `.env` and pydantic-settings, not the process
environment. `GATEWAY_STAGE_MODELS` is added when `--model` is given. The child's own
backends re-sanitise what they hand to git and docker.

On SIGTERM or SIGHUP (and Ctrl-C) the children's process groups are killed, their rows
are settled RUNNING to FAILED (`runner: interrupted`), and their containers are removed;
`--mark-abandoned` settles rows left by a crash.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

import structlog

from harness.bench_repos import TOKEN_ENV_VAR
from harness.db import (
    QueuedTask,
    SessionFactory,
    check_eval_run_id,
    fail_if_running,
    open_session_factory,
    queued_tasks,
    run_cost_usd,
    running_older_than,
)
from harness.enqueue import DEFAULT_RUNS_DIR, DEFAULT_WALL_CLOCK_SECONDS
from harness.run_manifest import ManifestError, load_manifest, manifest_path
from repolace_shared.paths import PathEscapesRoot, resolve_within
from repolace_shared.process import REAP_TIMEOUT_SECONDS, ProcessResult, kill_process_tree, run_process

log = structlog.get_logger()

#: Mirrors `repolace_pipeline.cli`; a test reads that file and holds the two equal.
EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_NOT_FOUND = 2
EXIT_NOT_CLAIMABLE = 3

#: Added to `--timeout-seconds` before a RUNNING row counts as abandoned, so a
#: task that is merely at the end of its allowance is never swept.
ABANDONED_GRACE_SECONDS = 600.0

GATEWAY_STAGE_MODELS_VAR = "GATEWAY_STAGE_MODELS"

ProcessRunner = Callable[..., Awaitable[ProcessResult]]
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")


class Outcome(str, Enum):
    RAN = "ran"
    REPOLACE_FAILED = "repolace_failed"
    NOT_FOUND = "not_found"
    NOT_CLAIMABLE = "not_claimable"
    #: Killed by a signal or an exit code the pipeline never uses.
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    #: The child could not be started at all (the command is missing).
    SPAWN_FAILED = "spawn_failed"


def classify(returncode: int) -> Outcome:
    return {
        EXIT_OK: Outcome.RAN,
        EXIT_TASK_FAILED: Outcome.REPOLACE_FAILED,
        EXIT_NOT_FOUND: Outcome.NOT_FOUND,
        EXIT_NOT_CLAIMABLE: Outcome.NOT_CLAIMABLE,
    }.get(returncode, Outcome.CRASHED)


@dataclass(frozen=True)
class RunnerConfig:
    eval_run_id: str
    concurrency: int = 3
    timeout_seconds: float = DEFAULT_WALL_CLOCK_SECONDS
    agent: str = "llm"
    open_pr: bool = True
    model: str | None = None
    max_total_usd: Decimal | None = None
    limit: int | None = None


@dataclass(frozen=True)
class ChildResult:
    task: QueuedTask
    outcome: Outcome
    returncode: int | None
    seconds: float
    peak_rss_kb: int | None = None
    #: This call moved the row RUNNING -> FAILED (timeout, or a child that died holding it).
    marked_failed: bool = False
    removed_containers: tuple[str, ...] = ()


@dataclass
class RunSummary:
    results: list[ChildResult] = field(default_factory=list)
    #: A run manifest exists and agrees with this invocation. False means there is
    #: none (the run was enqueued without one); a manifest that *disagrees* raises.
    manifest_found: bool = False
    #: Why dispatching stopped early (cost cap, limit, a child that cannot start), or None.
    stopped: str | None = None
    not_dispatched: int = 0
    total_cost_usd: Decimal = Decimal(0)

    @property
    def healthy(self) -> bool:
        # A child that exited 0 but left its row RUNNING was marked FAILED: that is not a clean run.
        return all(r.outcome is Outcome.RAN and not r.marked_failed for r in self.results)


# --- the child process ------------------------------------------------------------


def default_child_command() -> tuple[str, ...]:
    return (sys.executable, "-m", "repolace_pipeline.cli")


def child_argv(command: Sequence[str], task_id: uuid.UUID, config: RunnerConfig) -> list[str]:
    argv = [*command, str(task_id), "--agent", config.agent]
    # The pipeline forces `--no-pr` for the gold agent itself; pass it anyway so
    # the intent is on the command line and in the process list.
    if not config.open_pr or config.agent == "gold":
        argv.append("--no-pr")
    return argv


#: What the child (and the docker and pgrep helpers) may inherit, by name. An allowlist,
#: like every other environment builder in this repository: subtracting one variable
#: would hand the child whatever else happens to be exported in the operator's shell
#: (a `GH_TOKEN`, cloud credentials, a provider key meant for something else). Provider
#: keys and the GitHub App key reach the child through `.env` and pydantic-settings, not
#: through the process environment, so the child does not need them here.
_ENV_EXACT = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR", "XDG_RUNTIME_DIR", "VIRTUAL_ENV",
    "DOCKER_HOST", "DATABASE_URL", "GIT_SSL_CAINFO",
    "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
})
_ENV_PREFIXES = ("LC_", "HF_", "TRANSFORMERS_", "LITELLM_", "SSL_", "UV_", "GATEWAY_", "REPOLACE_")


def allowlisted_env(base: Mapping[str, str]) -> dict[str, str]:
    """`base` reduced to the allowlist, and never the repository tool's token."""
    return {
        name: value
        for name, value in base.items()
        if name != TOKEN_ENV_VAR and (name in _ENV_EXACT or name.startswith(_ENV_PREFIXES))
    }


def child_env(base: Mapping[str, str], model: str | None) -> dict[str, str]:
    env = allowlisted_env(base)
    if model is not None:
        env[GATEWAY_STAGE_MODELS_VAR] = json.dumps({"agent": model})
    return env


def read_peak_rss_kb(pid: int) -> int | None:
    """The process's peak resident set (`VmHWM`) in kB, or None where `/proc` has none."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


async def _sample_peak_rss(pid: int, interval: float, peak: list[int]) -> None:
    while True:
        value = read_peak_rss_kb(pid)
        if value is not None and (not peak or value > peak[0]):
            peak[:] = [value]
        await asyncio.sleep(interval)


async def remove_task_containers(
    task_id: uuid.UUID,
    *,
    process_runner: ProcessRunner = run_process,
    docker: str = "docker",
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """`docker ps -q --filter name=repolace-<12 hex>` then `docker rm -f` for any survivors.

    Needed because the sandbox starts containers through a `docker run` client
    that the pipeline launches in its own session: killing the child's process
    group does not reach it, and the container belongs to the daemon anyway.
    Best effort -- a missing docker binary or a daemon that does not answer is
    logged, not raised, since the row has already been settled.
    """
    prefix = f"repolace-{task_id.hex[:12]}"
    try:
        listed = await process_runner(docker, "ps", "-q", "--filter", f"name={prefix}", timeout=timeout, env=env)
        if listed.timed_out or listed.returncode != 0:
            log.error("runner.docker.ps_failed", prefix=prefix, returncode=listed.returncode)
            return ()
        ids = tuple(listed.stdout.decode("utf-8", errors="replace").split())
        # `docker ps -q` prints hex ids; anything else is not passed to `rm`.
        ids = tuple(item for item in ids if _CONTAINER_ID.fullmatch(item))
        if ids:
            await process_runner(docker, "rm", "-f", *ids, timeout=timeout, env=env)
    except OSError as exc:
        log.error("runner.docker.unavailable", prefix=prefix, error=str(exc))
        return ()
    return ids


async def _settle_interrupted(
    task: QueuedTask,
    factory: SessionFactory,
    *,
    process_runner: ProcessRunner,
    docker: str,
    env: Mapping[str, str],
) -> None:
    """After killing an interrupted child: RUNNING -> FAILED, and remove its containers.

    Shielded, so the cleanup survives the cancellation that is unwinding around it,
    and best-effort: a failure here must not replace the interrupt being handled. A
    row that was never claimed stays QUEUED, as on a timeout.
    """
    async def settle() -> None:
        await fail_if_running(factory, task.task_id, "runner: interrupted")
        await remove_task_containers(task.task_id, process_runner=process_runner, docker=docker, env=env)

    with suppress(Exception):
        await asyncio.shield(settle())


async def _run_child(
    task: QueuedTask,
    config: RunnerConfig,
    factory: SessionFactory,
    *,
    command: Sequence[str],
    env: Mapping[str, str],
    run_dir: Path,
    process_runner: ProcessRunner,
    docker: str,
    rss_interval: float,
) -> ChildResult:
    argv = child_argv(command, task.task_id, config)
    log_path = resolve_within(run_dir, f"{task.task_id}.log")
    started = time.monotonic()
    peak: list[int] = []
    sampler: asyncio.Task | None = None
    timed_out = False
    with open(log_path, "ab") as log_file:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                env=dict(env),
                # Its own session: the group is what gets killed on a timeout, and
                # an interrupt of this process must not reach it by terminal signal.
                start_new_session=True,
            )
        except OSError as exc:
            log.error("runner.child.spawn_failed", task_id=str(task.task_id), command=list(command), error=str(exc))
            return ChildResult(task, Outcome.SPAWN_FAILED, None, time.monotonic() - started)

        pgid = process.pid  # captured now; see `kill_process_tree`
        sampler = asyncio.create_task(_sample_peak_rss(process.pid, rss_interval, peak))
        try:
            returncode: int | None = await asyncio.wait_for(process.wait(), config.timeout_seconds)
        except TimeoutError:
            timed_out = True
            returncode = None
            kill_process_tree(pgid, argv)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), REAP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            # Interrupted (SIGTERM, SIGHUP, Ctrl-C): the child is in its own session, so
            # nothing else will stop it and it would keep spending. Kill it, then settle
            # its row and its containers before letting the cancellation continue.
            kill_process_tree(pgid, argv)
            await _settle_interrupted(task, factory, process_runner=process_runner, docker=docker, env=env)
            raise
        finally:
            sampler.cancel()

    seconds = time.monotonic() - started
    peak_kb = peak[0] if peak else None
    removed: tuple[str, ...] = ()
    marked = False

    if timed_out:
        outcome = Outcome.TIMED_OUT
        marked = await fail_if_running(factory, task.task_id, "runner: timeout")
        removed = await remove_task_containers(task.task_id, process_runner=process_runner, docker=docker, env=env)
    else:
        assert returncode is not None
        outcome = classify(returncode)
        if outcome not in (Outcome.NOT_FOUND, Outcome.NOT_CLAIMABLE):
            # The child owned the row and is gone. If it still says RUNNING it
            # died without finishing (an OOM kill, a crash); nothing is running it.
            marked = await fail_if_running(
                factory, task.task_id, f"runner: child exited {returncode} without finishing the task"
            )

    log.info(
        "runner.child.done",
        task_id=str(task.task_id),
        instance_id=task.instance_id,
        run_index=task.run_index,
        outcome=outcome.value,
        returncode=returncode,
        seconds=round(seconds, 1),
        peak_rss_mb=None if peak_kb is None else round(peak_kb / 1024),
        marked_failed=marked,
    )
    return ChildResult(task, outcome, returncode, seconds, peak_kb, marked, removed)


# --- the run manifest -----------------------------------------------------------


def check_manifest(runs_dir: Path, config: RunnerConfig) -> bool:
    """Whether the run's manifest exists and agrees with this invocation.

    `enqueue` recorded the agent, the model and the wall-clock cap the sweep was
    meant to run under; the report describes the sweep from that file. So a runner
    started with different values would make the manifest describe something that
    did not happen, and refuses instead (`ManifestError`). A run enqueued without a
    manifest is allowed through and reported, so the caller can say so.
    """
    path = manifest_path(runs_dir, config.eval_run_id)
    if not path.exists():
        return False
    manifest = load_manifest(path)
    problems = []
    if manifest.agent != config.agent:
        problems.append(f"agent: the manifest says {manifest.agent!r}, this run was given {config.agent!r}")
    if config.agent == "llm" and manifest.model != config.model:
        problems.append(f"model: the manifest says {manifest.model!r}, this run was given {config.model!r}")
    recorded = manifest.limits.get("runner_wall_clock_seconds")
    if recorded != config.timeout_seconds:
        problems.append(
            f"timeout: the manifest says {recorded!r} seconds, this run was given {config.timeout_seconds!r}"
        )
    if problems:
        raise ManifestError(
            f"{path} does not match this invocation of `run`:\n  " + "\n  ".join(problems)
            + "\nRun with the values enqueue recorded, or enqueue a new run id."
        )
    return True


# --- the dispatcher --------------------------------------------------------------


async def run_queue(
    factory: SessionFactory,
    config: RunnerConfig,
    *,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    child_command: Sequence[str] | None = None,
    base_env: Mapping[str, str] | None = None,
    process_runner: ProcessRunner = run_process,
    docker: str = "docker",
    rss_interval: float = 2.0,
) -> RunSummary:
    """Dispatch the run's QUEUED tasks, K at a time, in `(run_index, instance_id)` order."""
    check_eval_run_id(config.eval_run_id)
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        run_dir = resolve_within(runs_dir, config.eval_run_id)
    except PathEscapesRoot as exc:
        raise ValueError(str(exc)) from None
    run_dir.mkdir(exist_ok=True)
    manifest_found = check_manifest(runs_dir, config)

    command = tuple(child_command) if child_command is not None else default_child_command()
    env = child_env(os.environ if base_env is None else base_env, config.model)
    queue = await queued_tasks(factory, config.eval_run_id)
    queued_total = len(queue)
    if config.limit is not None:
        queue = queue[: config.limit]

    summary = RunSummary(manifest_found=manifest_found)
    semaphore = asyncio.Semaphore(config.concurrency)
    workers: list[asyncio.Task[None]] = []
    cannot_start: str | None = None

    async def dispatch(task: QueuedTask) -> None:
        nonlocal cannot_start
        try:
            result = await _run_child(
                task, config, factory,
                command=command, env=env, run_dir=run_dir, process_runner=process_runner,
                docker=docker, rss_interval=rss_interval,
            )
            summary.results.append(result)
            if result.outcome is Outcome.SPAWN_FAILED:
                # Every later task would fail the same way; their rows are still QUEUED.
                cannot_start = f"cannot start {command[0]!r}"
        finally:
            semaphore.release()

    try:
        for task in queue:
            await semaphore.acquire()
            # After acquiring, so the spend of the tasks that just finished is counted.
            reason = cannot_start
            if reason is None and config.max_total_usd is not None:
                spent = await run_cost_usd(factory, config.eval_run_id)
                if spent >= config.max_total_usd:
                    reason = f"cost cap reached: ${spent} spent of ${config.max_total_usd}"
            if reason is not None:
                semaphore.release()
                summary.stopped = reason
                break
            workers.append(asyncio.create_task(dispatch(task)))

        # All of them, finished or not, so an exception in one is raised here, not lost.
        await asyncio.gather(*workers)
    except asyncio.CancelledError:
        # Cancelled while dispatching or waiting: every child still running must be killed
        # and settled before this unwinds, or it outlives the runner and keeps spending.
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    summary.not_dispatched = queued_total - len(workers)
    summary.total_cost_usd = await run_cost_usd(factory, config.eval_run_id)
    return summary


# --- abandoned rows -------------------------------------------------------------


@dataclass
class AbandonedReport:
    marked: list[QueuedTask] = field(default_factory=list)
    #: A process still matches the task id: left alone.
    alive: list[QueuedTask] = field(default_factory=list)
    #: `pgrep` could not answer: left alone, because abandoning a live task is worse.
    unknown: list[QueuedTask] = field(default_factory=list)


async def _process_matches(
    task_id: uuid.UUID, process_runner: ProcessRunner, env: Mapping[str, str] | None = None
) -> bool | None:
    """True if a process's command line contains the task id, False if none, None if unknown."""
    try:
        result = await process_runner("pgrep", "-f", "--", str(task_id), timeout=15.0, env=env)
    except OSError:
        return None
    if result.timed_out:
        return None
    return {0: True, 1: False}.get(result.returncode)


async def mark_abandoned(
    factory: SessionFactory,
    eval_run_id: str,
    timeout_seconds: float,
    *,
    process_runner: ProcessRunner = run_process,
    grace_seconds: float = ABANDONED_GRACE_SECONDS,
    env: Mapping[str, str] | None = None,
) -> AbandonedReport:
    """RUNNING -> FAILED (`runner: abandoned`) for stale rows with no live process. Never QUEUED."""
    report = AbandonedReport()
    # The helper gets the allowlisted environment, never the operator's whole shell.
    tool_env = allowlisted_env(os.environ if env is None else env)
    stale = await running_older_than(factory, eval_run_id, timedelta(seconds=timeout_seconds + grace_seconds))
    for task in stale:
        matches = await _process_matches(task.task_id, process_runner, tool_env)
        if matches is None:
            report.unknown.append(task)
        elif matches:
            report.alive.append(task)
        elif await fail_if_running(factory, task.task_id, "runner: abandoned"):
            report.marked.append(task)
    return report


# --- command line ----------------------------------------------------------------


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not value > 0 or value == float("inf"):
        raise argparse.ArgumentTypeError("must be a positive, finite number")
    return value


def _positive_usd(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not an amount") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive, finite amount")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repolace-eval run",
        allow_abbrev=False,
        description="Run the QUEUED tasks of one benchmark run as repolace-run-task subprocesses. "
        "See the module docstring for memory, exit codes and the never-requeue rule.",
    )
    parser.add_argument("--eval-run-id", required=True)
    parser.add_argument("-k", "--concurrency", type=_positive_int, default=3, help="subprocesses at a time (RAM: ~4 GB each)")
    parser.add_argument(
        "--timeout-seconds", type=_positive_float, default=DEFAULT_WALL_CLOCK_SECONDS, help="wall clock per task"
    )
    parser.add_argument("--agent", choices=("llm", "gold"), default="llm")
    parser.add_argument("--no-pr", action="store_true", help="never open a pull request (always on for --agent gold)")
    parser.add_argument("--model", help="gateway model key for the agent stage (sets GATEWAY_STAGE_MODELS)")
    parser.add_argument("--max-total-usd", type=_positive_usd, default=None, help="stop dispatching once the run has spent this")
    parser.add_argument("--limit", type=_positive_int, default=None, help="dispatch at most this many tasks")
    parser.add_argument(
        "--mark-abandoned", action="store_true",
        help="instead of running: mark stale RUNNING rows of the run FAILED ('runner: abandoned') and exit",
    )
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    return parser


def _cancel_on_signals(received: list[int]) -> None:
    """Turn SIGTERM and SIGHUP into a cancellation of the running task.

    Without it Python's default handler ends the runner on the spot, and every child
    (each in its own session) keeps running and spending, with its row left RUNNING.
    A cancellation instead reaches `_run_child`, which kills the child and settles its
    row. Only installable from the main thread; elsewhere (a test driving `main` from a
    worker thread) the default behaviour stands.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(signum, lambda signum=signum: (received.append(signum), task.cancel()))


def _print_summary(summary: RunSummary) -> None:
    counts: dict[str, int] = {}
    for result in summary.results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
    print(f"dispatched {len(summary.results)}: " + (", ".join(f"{n} {name}" for name, n in sorted(counts.items())) or "nothing"))
    print(f"run cost so far: ${summary.total_cost_usd}")
    if not summary.manifest_found:
        print(
            "warning: this run has no manifest.json (it was enqueued without one), so nothing checks that "
            "--agent, --model and --timeout-seconds match what the sweep was meant to use",
            file=sys.stderr,
        )
    if summary.stopped:
        print(f"stopped dispatching: {summary.stopped}; {summary.not_dispatched} task(s) left QUEUED", file=sys.stderr)
    for result in summary.results:
        if result.outcome is not Outcome.RAN or result.marked_failed:
            note = " and left its row RUNNING (now FAILED)" if result.marked_failed else ""
            print(
                f"  {result.outcome.value}{note}: {result.task.instance_id} run {result.task.run_index} "
                f"({result.task.task_id}, exit {result.returncode})",
                file=sys.stderr,
            )


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[SessionFactory]] | None = None,
    child_command: Sequence[str] | None = None,
    process_runner: ProcessRunner = run_process,
) -> int:
    """`repolace-eval run`. The keyword arguments are test seams, not flags."""
    args = build_parser().parse_args(argv)
    try:
        check_eval_run_id(args.eval_run_id)
    except ValueError as exc:
        print(f"repolace-eval run: {exc}", file=sys.stderr)
        return 2

    if args.agent == "llm" and not args.mark_abandoned and args.max_total_usd is None:
        print(
            "repolace-eval run: --max-total-usd is required for --agent llm: a sweep spends real money "
            "and the per-task cap does not bound the total",
            file=sys.stderr,
        )
        return 2

    from repolace_shared.logging import configure_logging

    configure_logging("eval-runner")
    received: list[int] = []

    async def run() -> int:
        _cancel_on_signals(received)
        async with (session_factory or open_session_factory)() as factory:
            if args.mark_abandoned:
                report = await mark_abandoned(
                    factory, args.eval_run_id, args.timeout_seconds, process_runner=process_runner
                )
                print(
                    f"marked {len(report.marked)} abandoned task(s) FAILED; "
                    f"left {len(report.alive)} with a live process and {len(report.unknown)} that could not be checked"
                )
                return 0
            config = RunnerConfig(
                eval_run_id=args.eval_run_id,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout_seconds,
                agent=args.agent,
                open_pr=not args.no_pr,
                model=args.model,
                max_total_usd=args.max_total_usd,
                limit=args.limit,
            )
            summary = await run_queue(
                factory, config, runs_dir=args.runs_dir, child_command=child_command, process_runner=process_runner
            )
            _print_summary(summary)
            return 0 if summary.healthy else 1

    try:
        return asyncio.run(run())
    except ManifestError as exc:
        print(f"repolace-eval run: {exc}", file=sys.stderr)
        return 2
    except asyncio.CancelledError:
        signum = received[0] if received else signal.SIGTERM
        print(
            f"repolace-eval run: interrupted by {signal.Signals(signum).name}; running children were killed and "
            f"their rows marked FAILED ('runner: interrupted'); rows not yet started stay QUEUED",
            file=sys.stderr,
        )
        return 128 + signum


if __name__ == "__main__":
    raise SystemExit(main())
