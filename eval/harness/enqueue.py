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
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from harness.bench_repos import DEFAULT_INSTANCES_DIR, MAPPING_FILENAME, BenchRepoError, load_bench_repos
from harness.db import SessionFactory, check_eval_run_id, open_session_factory
from repolace_shared.db.models import RegisteredRepo, Task, TaskStatus
from repolace_shared.instances import InstanceError, InstanceSpec, load_instances

#: Benchmark tasks target `main` of the benchmark repository: `fork` pushes the
#: base commit there.
TARGET_BRANCH = "main"


class EnqueueError(RuntimeError):
    """The run cannot be enqueued as asked; nothing was inserted."""


@dataclass(frozen=True)
class EnqueueResult:
    created: int
    already_present: int


def _resolve_instances(
    requested: str, instances: Mapping[str, InstanceSpec], bench_repos: Mapping[str, str]
) -> tuple[list[str], list[str]]:
    """The chosen instance ids and every problem found with them, without touching the database."""
    if requested == "all":
        chosen = sorted(instances)
    else:
        chosen = [item for item in requested.split(",") if item]
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
    return EnqueueResult(created=created, already_present=len(values) - created)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repolace-eval enqueue", description="Enqueue benchmark tasks for one run.")
    parser.add_argument("--eval-run-id", required=True)
    parser.add_argument("--instances", default="all", help="comma-separated instance ids, or 'all'")
    parser.add_argument("--runs", type=int, default=3, help="runs per instance (run_index 0..N-1)")
    parser.add_argument("--open-pr-on-failure", action="store_true")
    parser.add_argument("--instances-dir", type=Path, default=DEFAULT_INSTANCES_DIR)
    parser.add_argument("--bench-repos-file", type=Path, default=None, help=f"default: <instances-dir>/{MAPPING_FILENAME}")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[SessionFactory]] | None = None,
) -> int:
    """`repolace-eval enqueue`. `session_factory` is a test seam."""
    args = build_parser().parse_args(argv)

    try:
        check_eval_run_id(args.eval_run_id)
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

    async def run() -> EnqueueResult:
        async with (session_factory or open_session_factory)() as factory:
            return await enqueue_tasks(
                factory,
                instances,
                bench_repos,
                eval_run_id=args.eval_run_id,
                instances_requested=args.instances,
                runs=args.runs,
                open_pr_on_failure=args.open_pr_on_failure,
            )

    try:
        result = asyncio.run(run())
    except EnqueueError as exc:
        print(f"repolace-eval enqueue: {exc}", file=sys.stderr)
        return 2
    print(f"{result.created} created, {result.already_present} already present (run {args.eval_run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
