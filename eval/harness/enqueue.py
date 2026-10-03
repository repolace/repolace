"""Put benchmark tasks in the queue (`repolace-eval enqueue`).

Inserts `tasks` rows directly instead of going through the API's issue lookup:
a benchmark instance has no issue on the benchmark repository, so the row is
built from the instance file. Everything an agent will read comes from the
instance (`issue_body` is the problem statement, which is untrusted text and
reaches a prompt only as delimited data), and nothing refers to the upstream
project: `issue_url` is the benchmark repository, because a link to upstream in a
task row would end up in a pull-request body and notify an unrelated project.

**Idempotent.** The partial unique index `uq_tasks_eval_instance_run` makes
`ON CONFLICT DO NOTHING` skip a row that already exists, so re-running after a
crash creates only what is missing and never a second row for one
`(run, instance, run_index)` -- which would be scored twice.

**All or nothing on resolution.** Every instance is checked against
`bench_repos.toml` and `registered_repos` before the first insert, and every
problem is reported at once. A half-enqueued run is the harder thing to notice.
A benchmark repository the App has not synced is an error that says so, never a
silent skip: the instance would just be missing from the headline.

**The run manifest** (`eval/runs/<run>/manifest.json`) is written, atomically,
*before* the first row is inserted. It records what the sweep is (the repolace
commit, the model, the limits, which instances and how many runs of each), and the
report refuses to print a headline when the database's `(instance_id, run_index)`
pairs are not exactly `instance_ids x range(runs_per_instance)`. So the manifest
is what makes "some tasks went missing" detectable rather than a quietly smaller
denominator. Re-running enqueue with the same inputs leaves it untouched; asking
for a *different* sweep under the same run id is refused, because the first rows
already belong to the first manifest. The runner checks its own `--agent`,
`--model` and `--timeout-seconds` against it, which is what keeps `limits` and
`model` descriptions of what ran instead of what was intended.

`git_sha` is read with git from the repolace checkout, never from the
environment, and a modified tracked tree is refused (`--allow-dirty-tree` to
override): a sha that does not describe the code that runs is worse than none.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from harness.bench_repos import (
    DEFAULT_INSTANCES_DIR,
    MAPPING_FILENAME,
    BenchRepoError,
    atomic_write_text,
    load_bench_repos,
)
from harness.db import SessionFactory, check_eval_run_id, open_session_factory
from repolace_shared.db.models import RegisteredRepo, Task, TaskStatus
from repolace_shared.git.repo import GitError, run_git
from repolace_shared.instances import InstanceError, InstanceSpec, load_instances
from repolace_shared.paths import PathEscapesRoot, resolve_within

#: Benchmark tasks target `main` of the benchmark repository: `fork` pushes the
#: base commit there.
TARGET_BRANCH = "main"

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_DIR = _REPO_ROOT / "eval" / "runs"

#: The runner's hard wall clock per task, and its default. Defined here, next to the
#: manifest that records it, and imported by `runner`, so the two cannot drift.
DEFAULT_WALL_CLOCK_SECONDS = 5400.0

MANIFEST_FILENAME = "manifest.json"
#: The keys of `manifest.json`, exactly -- no more, no fewer. Stream D's report
#: loader reads these; this tuple is the one place the two are reconciled.
MANIFEST_KEYS: tuple[str, ...] = (
    "eval_run_id",
    "created_at",
    "git_sha",
    "model",
    "stage_models",
    "limits",
    "runs_per_instance",
    "instance_ids",
    "agent",
)
MANIFEST_AGENTS: tuple[str, ...] = ("llm", "gold", "stub")
#: What `model` holds for an agent that calls no model.
NO_MODEL = "none"

log = structlog.get_logger()

_GIT_SHA = re.compile(r"[0-9a-f]{40}")


class EnqueueError(RuntimeError):
    """The run cannot be enqueued as asked; nothing was inserted."""


class ManifestError(EnqueueError):
    """The run manifest is unreadable, malformed, or describes a different sweep."""


@dataclass(frozen=True)
class EnqueueResult:
    created: int
    already_present: int
    manifest_created: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ManifestInputs:
    """What the manifest needs from outside the instance files and the database."""

    runs_dir: Path = DEFAULT_RUNS_DIR
    agent: str = "llm"
    #: The headline model id; required for the `llm` agent.
    model: str | None = None
    timeout_seconds: float = DEFAULT_WALL_CLOCK_SECONDS
    repo_root: Path = _REPO_ROOT
    allow_dirty_tree: bool = False
    clock: Callable[[], datetime] = field(default=_utc_now)


# --- the manifest ---------------------------------------------------------------


def code_limits() -> dict[str, object]:
    """The per-task limits the pipeline runs under, read from where they are defined.

    Imported lazily from the two light modules that own them (neither pulls torch
    or LiteLLM), so `--help` and every other subcommand do not pay for them. These
    are the *defaults in code*: if the pipeline ever overrides a budget from its
    settings, this function is the one to teach about it.
    """
    from repolace_agents.contracts import AgentLimits
    from repolace_gateway.budget import DEFAULT_MAX_CALLS, DEFAULT_MAX_USD, DEFAULT_MAX_WALL_SECONDS

    agent = AgentLimits()
    return {
        "task_cost_cap_usd": float(DEFAULT_MAX_USD),
        "task_call_cap": DEFAULT_MAX_CALLS,
        "budget_wall_clock_seconds": float(DEFAULT_MAX_WALL_SECONDS),
        "max_steps_per_attempt": agent.max_steps_per_attempt,
        "max_attempts": agent.max_attempts,
    }


def build_manifest(
    *,
    eval_run_id: str,
    git_sha: str,
    instance_ids: Sequence[str],
    runs: int,
    inputs: ManifestInputs,
    created_at: datetime,
) -> dict[str, object]:
    if inputs.agent not in MANIFEST_AGENTS:
        raise ManifestError(f"--agent must be one of {', '.join(MANIFEST_AGENTS)}, got {inputs.agent!r}")
    model = (inputs.model or "").strip()
    if inputs.agent == "llm" and not model:
        raise ManifestError("--model is required for the llm agent: the manifest must name the model that ran")
    if not _GIT_SHA.fullmatch(git_sha):
        raise ManifestError(f"git_sha {git_sha!r} is not a full commit sha")
    manifest = {
        "eval_run_id": eval_run_id,
        "created_at": created_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "git_sha": git_sha,
        "model": model if inputs.agent == "llm" else NO_MODEL,
        # Exactly what the runner puts in GATEWAY_STAGE_MODELS for `--model`.
        "stage_models": {"agent": model} if inputs.agent == "llm" else {},
        "limits": {**code_limits(), "runner_wall_clock_seconds": float(inputs.timeout_seconds)},
        "runs_per_instance": runs,
        "instance_ids": sorted(set(instance_ids)),
        "agent": inputs.agent,
    }
    if set(manifest) != set(MANIFEST_KEYS):  # a bug here, not a user error
        raise ManifestError(f"build_manifest produced {sorted(manifest)}, expected {sorted(MANIFEST_KEYS)}")
    return manifest


def manifest_path(runs_dir: Path, eval_run_id: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        return resolve_within(runs_dir, eval_run_id) / MANIFEST_FILENAME
    except PathEscapesRoot as exc:
        raise ManifestError(str(exc)) from None


def read_manifest(path: Path) -> dict[str, object]:
    """The manifest at `path`, validated to carry exactly `MANIFEST_KEYS`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"{path}: cannot read the run manifest: {type(exc).__name__}: {exc}") from None
    if not isinstance(data, dict) or set(data) != set(MANIFEST_KEYS):
        raise ManifestError(
            f"{path}: the manifest must have exactly the keys {', '.join(MANIFEST_KEYS)}; "
            f"found {', '.join(sorted(data)) if isinstance(data, dict) else type(data).__name__}"
        )
    return data


def _comparable(manifest: Mapping[str, object]) -> dict[str, object]:
    """Everything that makes two manifests the same sweep: all of it but the creation time."""
    return {key: manifest[key] for key in MANIFEST_KEYS if key != "created_at"}


def ensure_manifest(path: Path, manifest: Mapping[str, object]) -> bool:
    """Write the manifest if there is none; accept an existing identical one; refuse any other.

    Returns True when it wrote the file. "Identical" ignores `created_at`, so a
    re-run after a crash keeps the original time. Anything else -- another model,
    another instance set, another run count, another commit -- is a different
    sweep, and the rows already enqueued belong to the first.
    """
    document = json.loads(json.dumps(manifest))  # the form it would have on disk
    if path.exists():
        new, old = _comparable(document), _comparable(read_manifest(path))
        differing = sorted(key for key in new if new[key] != old[key])
        if differing:
            raise ManifestError(
                f"{path} already describes a different sweep for run {document['eval_run_id']!r} "
                f"(differs in: {', '.join(differing)}). Use a new --eval-run-id, or delete the run's rows and manifest"
            )
        return False
    atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return True


async def repo_git_sha(repo_root: Path, *, allow_dirty_tree: bool) -> str:
    """The repolace commit, read with git, refusing a tree whose tracked files are modified."""
    try:
        sha = (await run_git("rev-parse", "HEAD", cwd=repo_root)).strip()
        modified = (await run_git("status", "--porcelain", "--untracked-files=no", cwd=repo_root)).strip()
    except GitError as exc:
        raise ManifestError(f"cannot read the repolace commit in {repo_root}: {exc}") from None
    if modified and not allow_dirty_tree:
        raise ManifestError(
            "tracked files in the repolace checkout are modified, so the commit sha would not describe the code "
            "that runs; commit them, or pass --allow-dirty-tree to record the sha anyway"
        )
    if modified:
        log.warning("enqueue.dirty_tree", repo_root=str(repo_root), git_sha=sha)
    return sha


# --- enqueue --------------------------------------------------------------------


def _resolve_instances(
    requested: str, instances: Mapping[str, InstanceSpec], bench_repos: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    """The chosen instance ids and every problem found with them, without touching the database."""
    if requested == "all":
        chosen = sorted(instances)
    else:
        chosen = sorted({item for item in requested.split(",") if item})
    problems: list[str] = []
    for instance_id in chosen:
        if instance_id not in instances:
            problems.append(f"{instance_id}: not an instance in the instances directory")
        elif instance_id not in bench_repos:
            problems.append(
                f"{instance_id}: no entry in {MAPPING_FILENAME}; run `repolace-eval fork` for it first"
            )
        elif not instances[instance_id].issue_title:
            problems.append(f"{instance_id}: the problem statement has no non-blank line to use as a title")
    if not chosen:
        problems.append("no instances selected")
    return chosen, problems


async def enqueue_tasks(
    factory: SessionFactory,
    instances: Mapping[str, InstanceSpec],
    bench_repos: Mapping[str, str],
    *,
    eval_run_id: str,
    instances_requested: str,
    runs: int,
    manifest: ManifestInputs,
    open_pr_on_failure: bool = False,
) -> EnqueueResult:
    try:
        check_eval_run_id(eval_run_id)
    except ValueError as exc:
        raise EnqueueError(str(exc)) from None
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise EnqueueError(f"--runs must be at least 1, got {runs!r}")

    chosen, problems = _resolve_instances(instances_requested, instances, bench_repos)

    repo_rows: dict[str, RegisteredRepo] = {}
    if not problems:
        wanted = sorted({bench_repos[instance_id] for instance_id in chosen})
        async with factory() as session:
            rows = (await session.execute(select(RegisteredRepo).where(RegisteredRepo.full_name.in_(wanted)))).scalars()
            by_full_name = {row.full_name: row for row in rows}
        for instance_id in chosen:
            full_name = bench_repos[instance_id]
            row = by_full_name.get(full_name)
            if row is None:
                problems.append(
                    f"{instance_id}: no registered_repos row for {full_name}. Re-sync the GitHub App "
                    f"installation (the App must be installed on that repository) and try again"
                )
            elif not row.is_active:
                problems.append(f"{instance_id}: {full_name} is registered but inactive (suspended or removed)")
            else:
                repo_rows[instance_id] = row
    if problems:
        raise EnqueueError("cannot enqueue, nothing inserted:\n  " + "\n  ".join(problems))

    # Before the first row, so a crash after the inserts cannot leave a sweep with
    # no description, and a refusal here leaves the database untouched.
    document = build_manifest(
        eval_run_id=eval_run_id,
        git_sha=await repo_git_sha(manifest.repo_root, allow_dirty_tree=manifest.allow_dirty_tree),
        instance_ids=chosen,
        runs=runs,
        inputs=manifest,
        created_at=manifest.clock(),
    )
    manifest_created = ensure_manifest(manifest_path(manifest.runs_dir, eval_run_id), document)

    values = [
        {
            "id": uuid.uuid4(),
            "repo_id": repo_rows[instance_id].id,
            "issue_number": instances[instance_id].issue_number,
            "issue_title": instances[instance_id].issue_title,
            # The benchmark repository, never the upstream project.
            "issue_url": f"https://github.com/{bench_repos[instance_id]}",
            "issue_body": instances[instance_id].problem_statement,
            "target_branch": TARGET_BRANCH,
            "eval_run_id": eval_run_id,
            "instance_id": instance_id,
            "run_index": run_index,
            "status": TaskStatus.QUEUED,
            "open_pr_on_failure": open_pr_on_failure,
            "retry_count": 0,
        }
        for run_index in range(runs)
        for instance_id in chosen
    ]
    async with factory() as session:
        inserted = await session.execute(
            insert(Task)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=["eval_run_id", "instance_id", "run_index"],
                index_where=text("eval_run_id IS NOT NULL"),
            )
            .returning(Task.id)
        )
        created = len(inserted.all())
        await session.commit()
    return EnqueueResult(created=created, already_present=len(values) - created, manifest_created=manifest_created)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repolace-eval enqueue", description="Enqueue benchmark tasks for one run.")
    parser.add_argument("--eval-run-id", required=True)
    parser.add_argument("--instances", default="all", help="comma-separated instance ids, or 'all'")
    parser.add_argument("--runs", type=int, default=3, help="runs per instance (run_index 0..N-1)")
    parser.add_argument("--open-pr-on-failure", action="store_true")
    parser.add_argument("--agent", choices=MANIFEST_AGENTS, default="llm", help="recorded in the run manifest; `run` must match")
    parser.add_argument("--model", help="headline model id, required for --agent llm; recorded in the manifest, `run` must match")
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_WALL_CLOCK_SECONDS,
        help="the per-task wall clock `run` will use; recorded in the manifest, `run` must match",
    )
    parser.add_argument("--allow-dirty-tree", action="store_true", help="record the commit sha even if tracked files are modified")
    parser.add_argument("--instances-dir", type=Path, default=DEFAULT_INSTANCES_DIR)
    parser.add_argument("--bench-repos-file", type=Path, default=None, help=f"default: <instances-dir>/{MAPPING_FILENAME}")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[SessionFactory]] | None = None,
    repo_root: Path = _REPO_ROOT,
) -> int:
    """`repolace-eval enqueue`. The keyword arguments are test seams, not flags."""
    args = build_parser().parse_args(argv)

    try:
        check_eval_run_id(args.eval_run_id)
        if not args.timeout_seconds > 0 or args.timeout_seconds == float("inf"):
            raise ValueError("--timeout-seconds must be a positive, finite number")
        if args.agent == "llm" and not (args.model or "").strip():
            raise ValueError("--model is required for the llm agent: the manifest must name the model that ran")
        instances = load_instances(args.instances_dir)
        bench_repos = load_bench_repos(args.bench_repos_file or args.instances_dir / MAPPING_FILENAME)
    except (ValueError, InstanceError, BenchRepoError) as exc:
        print(f"repolace-eval enqueue: {exc}", file=sys.stderr)
        return 2
    # The file-only checks, so a typo is reported without opening the database.
    # `enqueue_tasks` repeats them: it is also called without this entry point.
    _chosen, problems = _resolve_instances(args.instances, instances, bench_repos)
    if problems:
        print("repolace-eval enqueue: cannot enqueue, nothing inserted:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    inputs = ManifestInputs(
        runs_dir=args.runs_dir, agent=args.agent, model=args.model, timeout_seconds=args.timeout_seconds,
        repo_root=repo_root, allow_dirty_tree=args.allow_dirty_tree,
    )

    async def run() -> EnqueueResult:
        async with (session_factory or open_session_factory)() as factory:
            return await enqueue_tasks(
                factory,
                instances,
                bench_repos,
                eval_run_id=args.eval_run_id,
                instances_requested=args.instances,
                runs=args.runs,
                manifest=inputs,
                open_pr_on_failure=args.open_pr_on_failure,
            )

    try:
        result = asyncio.run(run())
    except EnqueueError as exc:
        print(f"repolace-eval enqueue: {exc}", file=sys.stderr)
        return 2
    print(
        f"{result.created} created, {result.already_present} already present (run {args.eval_run_id}); "
        f"manifest {'written' if result.manifest_created else 'already present and identical'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
