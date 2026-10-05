"""Gold-run analysis (`repolace-eval gold`): which benchmark instances are fit to score.

Reads the rows of two runs of the **gold agent** (`gold-1` and `gold-2`, each
`runner --agent gold --no-pr`, which applies the reference fix through the real
pipeline and sandbox at $0 LLM cost) and decides, per instance, whether the
instance can be used. It reads the database only and **never edits an instance
file**: the verdicts and the `targeted_p2p` proposals go to
`eval/instances/VALIDATION.md` for the maintainer to act on.

An instance is accepted only if all of these hold:

1. **Both runs scored `passed`.** The reference fix must pass the real scorer.
2. **The attempt-0 baselines are identical across the two runs** -- the six
   pass/fail sets. A suite whose baseline changes between two runs of the same
   commit is flaky, and a flaky baseline makes every later score ambiguous.
3. **Every curated fail-to-pass test was red at baseline**
   (`verify.scoring.expected_not_red`). One that already passes lets a patch that
   changes nothing score PASSED, which is a pass nobody could defend.
4. **Every fail-to-pass id was collected at baseline.** An id that appears in no
   result bucket and sits in no module that failed to import is almost always a
   node-id format mismatch between SWE-bench and our plugin; scored as an agent
   run it would be charged to the agent as a failure. This is checked first, and
   (3) then reports only the ids that *were* collected and not red.

It also reports each suite's wall time and, when the longest run takes at least
`TARGETED_P2P_FRACTION` of the suite timeout, a `targeted_p2p` **proposal**: the
fix's own test files, offered because every task runs the suite once at baseline
and again after each attempt, so a suite near its timeout will turn into
unscoreable runs. The same proposal is made when a task whose suite runs that
long (padded the same way) could outlast the **runner's** wall clock
(`--runner-timeout-seconds`, default the runner's own): the runner would kill it and
record a harness error, which leaves the secondary `passed / admissible` figure.
Whether to apply it is the maintainer's call; the proposal says how much of the
pass-to-pass list it would still cover.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from harness.bench_repos import DEFAULT_INSTANCES_DIR, atomic_write_text
from harness.db import SessionFactory, check_eval_run_id, open_session_factory
from harness.enqueue import DEFAULT_WALL_CLOCK_SECONDS, task_wall_clock_bound
from repolace_shared.db.models import Task, TaskOutcome, TaskStatus, TaskTestRun
from repolace_shared.instances import InstanceError, InstanceSpec, load_instances
from verify.config import DockerConfig
from verify.protocol import SuiteResult
from verify.scoring import expand_truncated_ids, expected_not_red

DEFAULT_RUNS = ("gold-1", "gold-2")
VALIDATION_FILENAME = "VALIDATION.md"

#: A suite whose longest run reaches this share of the suite timeout is proposed for
#: `targeted_p2p`. Half, because an attempt's run is slower than the baseline's more
#: often than not (the patch adds work, a cold cache) and the timeout is a cliff: the
#: run that crosses it is unscoreable, not slow.
TARGETED_P2P_FRACTION = 0.5

#: How many offending ids a reason spells out before it summarises.
_SAMPLE = 5
_CELL_LIMIT = 400


@dataclass(frozen=True)
class GoldRun:
    """What the database holds for one instance in one gold run."""

    run_id: str
    status: TaskStatus
    outcome: TaskOutcome | None
    score_reason: str | None
    error_message: str | None
    baseline: SuiteResult | None
    #: The agent's attempts (attempt >= 1), in order. The gold agent makes one.
    attempts: tuple[SuiteResult, ...]


@dataclass(frozen=True)
class Proposal:
    test_targets: tuple[str, ...]
    #: Of the instance's pass-to-pass ids, how many live in a targeted file.
    covered_pass_to_pass: int
    total_pass_to_pass: int
    reason: str


@dataclass(frozen=True)
class InstanceVerdict:
    instance_id: str
    reasons: tuple[str, ...]
    baseline_seconds: tuple[float, ...]
    gold_seconds: tuple[float, ...]
    proposal: Proposal | None

    @property
    def accepted(self) -> bool:
        return not self.reasons

    @property
    def longest_seconds(self) -> float | None:
        every = (*self.baseline_seconds, *self.gold_seconds)
        return max(every) if every else None


def _suite(row: TaskTestRun) -> SuiteResult:
    return SuiteResult(
        passed=tuple(row.passed), failed=tuple(row.failed), skipped=tuple(row.skipped), xfailed=tuple(row.xfailed),
        did_not_run=tuple(row.did_not_run), collect_failures=tuple(row.collect_failures),
        collected_files=tuple(row.collected_files), conftests=tuple(row.conftests), fingerprint=dict(row.fingerprint),
        exit_code=row.exit_code, duration_seconds=None if row.duration_seconds is None else float(row.duration_seconds),
        stdout_tail=row.stdout_tail, error=row.error,
    )


async def load_gold_runs(factory: SessionFactory, run_ids: Sequence[str]) -> dict[tuple[str, str], GoldRun]:
    """`(run_id, instance_id) -> GoldRun` for `run_index` 0 of each run."""
    async with factory() as session:
        tasks = (
            await session.execute(
                select(Task)
                .where(Task.eval_run_id.in_(list(run_ids)), Task.run_index == 0)
                .options(selectinload(Task.test_runs))
            )
        ).scalars()
        found: dict[tuple[str, str], GoldRun] = {}
        for task in tasks:
            runs = sorted(task.test_runs, key=lambda row: row.attempt)
            found[(task.eval_run_id, task.instance_id)] = GoldRun(
                run_id=task.eval_run_id,
                status=task.status,
                outcome=task.outcome,
                score_reason=task.score_reason,
                error_message=task.error_message,
                baseline=next((_suite(row) for row in runs if row.attempt == 0), None),
                attempts=tuple(_suite(row) for row in runs if row.attempt >= 1),
            )
        return found


def _sample(items: Sequence[str]) -> str:
    shown = ", ".join(sorted(items)[:_SAMPLE])
    return f"{shown} (+{len(items) - _SAMPLE} more)" if len(items) > _SAMPLE else shown


_SET_NAMES = ("passed", "failed", "skipped", "xfailed", "did_not_run", "collect_failures")


def _differences(first: SuiteResult, second: SuiteResult) -> list[str]:
    found = []
    for name in _SET_NAMES:
        a, b = set(getattr(first, name)), set(getattr(second, name))
        if a != b:
            found.append(f"{name} differs ({len(a - b)} only in the first run, {len(b - a)} only in the second: {_sample(sorted(a ^ b))})")
    return found


def _uncollected(baseline: SuiteResult, expected: Sequence[str]) -> list[str]:
    """Ids in no result bucket and in no module that failed to import.

    The module prefix mirrors `verify.scoring`'s rule (a `.py` collect failure,
    plus `::`, so `tests/test_a.py` does not cover `tests/test_ab.py::t`), because
    an id inside a module that would not import is legitimately absent from every
    bucket: it is red, and its absence says nothing about the id's format.
    """
    seen = {
        item
        for bucket in (baseline.passed, baseline.failed, baseline.skipped, baseline.xfailed, baseline.did_not_run)
        for item in bucket
    }
    prefixes = tuple(f"{path}::" for path in baseline.collect_failures if path.endswith(".py"))
    expected = expand_truncated_ids(expected, seen)
    return sorted(item for item in expected if item not in seen and not item.startswith(prefixes))


def _proposal(spec: InstanceSpec, longest: float, timeout: float, runner_timeout: float) -> Proposal | None:
    if spec.targeted_p2p:
        return None
    reasons = []
    if longest >= TARGETED_P2P_FRACTION * timeout:
        reasons.append(f"longest suite run {longest:.0f}s is at least {TARGETED_P2P_FRACTION:.0%} of the {timeout:.0f}s timeout")
    # The suite padded as above (an attempt's run is often slower), but never past its own
    # timeout, which is where the sandbox stops it whatever the runner allows.
    padded = min(timeout, longest / TARGETED_P2P_FRACTION)
    projected = task_wall_clock_bound(padded)
    if projected > runner_timeout:
        reasons.append(
            f"with suite runs of up to {padded:.0f}s a task can take {projected:.0f}s, longer than the "
            f"runner's {runner_timeout:.0f}s wall clock, which would kill it as a harness error"
        )
    if not reasons:
        return None
    files = {item.split("::", 1)[0] for item in spec.fail_to_pass} | set(spec.test_files)
    covered = sum(1 for item in spec.pass_to_pass if item.split("::", 1)[0] in files)
    return Proposal(
        test_targets=tuple(sorted(files)),
        covered_pass_to_pass=covered,
        total_pass_to_pass=len(spec.pass_to_pass),
        reason="; ".join(reasons),
    )


def validate_instance(
    spec: InstanceSpec,
    runs: Mapping[str, GoldRun | None],
    *,
    timeout_seconds: float,
    runner_timeout_seconds: float = DEFAULT_WALL_CLOCK_SECONDS,
) -> InstanceVerdict:
    """Apply the four checks and the timing report to one instance. Pure."""
    reasons: list[str] = []
    present: list[GoldRun] = []
    for run_id, run in runs.items():
        if run is None:
            reasons.append(f"{run_id}: no task row (not enqueued, or not for run_index 0)")
        else:
            present.append(run)

    for run in present:
        if run.outcome is not TaskOutcome.PASSED:
            detail = run.score_reason or run.error_message or "no reason recorded"
            outcome = run.outcome.value if run.outcome else "none"
            reasons.append(f"{run.run_id}: outcome is {outcome}, not passed (status {run.status.value}): {detail}")

    baselines = [run for run in present if run.baseline is not None]
    for run in present:
        if run.baseline is None:
            reasons.append(f"{run.run_id}: no baseline run was recorded")
        elif run.baseline.error is not None:
            reasons.append(f"{run.run_id}: baseline is unscoreable: {run.baseline.error}")

    scoreable = [run for run in baselines if run.baseline.error is None]
    if len(scoreable) == len(runs) and len(scoreable) >= 2:
        first, *rest = scoreable
        for other in rest:
            for difference in _differences(first.baseline, other.baseline):
                reasons.append(f"baseline is not identical between {first.run_id} and {other.run_id} (flaky suite): {difference}")

    uncollected: set[str] = set()
    not_red: set[str] = set()
    for run in scoreable:
        missing = _uncollected(run.baseline, spec.fail_to_pass)
        uncollected.update(missing)
        not_red.update(set(expected_not_red(run.baseline, spec.fail_to_pass)) - set(missing))
    if uncollected:
        reasons.append(
            f"{len(uncollected)} fail-to-pass id(s) were not collected at baseline, so they cannot be matched "
            f"(node-id format mismatch?): {_sample(sorted(uncollected))}"
        )
    if not_red:
        reasons.append(
            f"{len(not_red)} fail-to-pass id(s) were not red at baseline, so a patch that changes nothing "
            f"could score passed: {_sample(sorted(not_red))}"
        )

    baseline_seconds = tuple(run.baseline.duration_seconds for run in baselines if run.baseline.duration_seconds is not None)
    gold_seconds = tuple(
        attempt.duration_seconds for run in present for attempt in run.attempts if attempt.duration_seconds is not None
    )
    every = (*baseline_seconds, *gold_seconds)
    proposal = _proposal(spec, max(every), timeout_seconds, runner_timeout_seconds) if every else None
    return InstanceVerdict(spec.instance_id, tuple(reasons), baseline_seconds, gold_seconds, proposal)


def suite_timeout_seconds(spec: InstanceSpec) -> float:
    """The suite deadline this instance runs under: its own, else the sandbox default."""
    own = spec.spec.get("timeout_seconds")
    if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0:
        return float(own)
    return DockerConfig().run_timeout_seconds


async def validate_gold(
    factory: SessionFactory,
    instances: Mapping[str, InstanceSpec],
    run_ids: Sequence[str] = DEFAULT_RUNS,
    *,
    runner_timeout_seconds: float = DEFAULT_WALL_CLOCK_SECONDS,
) -> list[InstanceVerdict]:
    rows = await load_gold_runs(factory, run_ids)
    return [
        validate_instance(
            spec,
            {run_id: rows.get((run_id, instance_id)) for run_id in run_ids},
            timeout_seconds=suite_timeout_seconds(spec),
            runner_timeout_seconds=runner_timeout_seconds,
        )
        for instance_id, spec in sorted(instances.items())
    ]


# --- the report -----------------------------------------------------------------


def _cell(text: object) -> str:
    """One markdown table cell: reasons carry test ids and sentences from rows, so neutralise them."""
    flat = " ".join(str(text).split())
    flat = flat.replace("|", "\\|").replace("`", "'").replace("<", "&lt;").replace(">", "&gt;")
    return flat if len(flat) <= _CELL_LIMIT else flat[: _CELL_LIMIT - 1] + "…"


def _seconds(values: Sequence[float]) -> str:
    return ", ".join(f"{value:.0f}" for value in values) or "n/a"


def render_validation(verdicts: Sequence[InstanceVerdict], run_ids: Sequence[str]) -> str:
    accepted = [v for v in verdicts if v.accepted]
    rejected = [v for v in verdicts if not v.accepted]
    lines = [
        "# Gold validation",
        "",
        f"Compared runs: {', '.join(f'`{run}`' for run in run_ids)}. Written by `repolace-eval gold`; "
        "this tool reads the database and **never edits an instance file**. "
        "Proposals below are for the maintainer to apply or ignore.",
        "",
        f"{len(verdicts)} instance(s): {len(accepted)} accepted, {len(rejected)} rejected.",
        "",
        "## Rejected",
        "",
    ]
    if rejected:
        lines += ["| instance | reasons |", "| --- | --- |"]
        lines += [f"| `{v.instance_id}` | {_cell('; '.join(v.reasons))} |" for v in rejected]
    else:
        lines.append("None.")
    lines += ["", "## Accepted", ""]
    if accepted:
        lines += ["| instance | baseline wall (s) | gold run wall (s) | targeted_p2p |", "| --- | --- | --- | --- |"]
        lines += [
            f"| `{v.instance_id}` | {_seconds(v.baseline_seconds)} | {_seconds(v.gold_seconds)} | "
            f"{'proposed' if v.proposal else 'no'} |"
            for v in accepted
        ]
    else:
        lines.append("None.")
    proposals = [v for v in verdicts if v.proposal]
    lines += ["", "## targeted_p2p proposals", ""]
    if proposals:
        for v in proposals:
            p = v.proposal
            targets = ", ".join(f"`{_cell(t)}`" for t in p.test_targets)
            lines += [
                f"### `{v.instance_id}`",
                "",
                f"- {_cell(p.reason)}",
                f"- proposed `spec.test_targets`: {targets}",
                f"- would still cover {p.covered_pass_to_pass} of {p.total_pass_to_pass} pass-to-pass ids",
                "- applying it means setting `targeted_p2p` to true as well; the two must agree",
                "",
            ]
    else:
        lines += ["None.", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


# --- command line ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repolace-eval gold", description="Analyse two gold runs and write VALIDATION.md.", allow_abbrev=False
    )
    parser.add_argument("--runs", default=",".join(DEFAULT_RUNS), help="the two gold run ids, comma-separated")
    parser.add_argument("--instances-dir", type=Path, default=DEFAULT_INSTANCES_DIR)
    parser.add_argument("--output", type=Path, default=None, help=f"default: <instances-dir>/{VALIDATION_FILENAME}")
    parser.add_argument(
        "--runner-timeout-seconds",
        type=float,
        default=DEFAULT_WALL_CLOCK_SECONDS,
        help="the per-task wall clock the sweep will run under (default: the runner's own)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[SessionFactory]] | None = None,
) -> int:
    """`repolace-eval gold`. Exit 0 if every instance is accepted, 1 if any is rejected, 2 on bad input."""
    args = build_parser().parse_args(argv)

    run_ids = [item for item in args.runs.split(",") if item]
    try:
        if not args.runner_timeout_seconds > 0 or args.runner_timeout_seconds == float("inf"):
            raise ValueError("--runner-timeout-seconds must be a positive, finite number")
        if len(run_ids) != 2 or run_ids[0] == run_ids[1]:
            raise ValueError("--runs needs exactly two different run ids")
        for run_id in run_ids:
            check_eval_run_id(run_id)
        instances = load_instances(args.instances_dir)
    except (ValueError, InstanceError) as exc:
        print(f"repolace-eval gold: {exc}", file=sys.stderr)
        return 2
    if not instances:
        print(f"repolace-eval gold: no instances in {args.instances_dir}", file=sys.stderr)
        return 2

    async def run() -> list[InstanceVerdict]:
        async with (session_factory or open_session_factory)() as factory:
            return await validate_gold(factory, instances, run_ids, runner_timeout_seconds=args.runner_timeout_seconds)

    verdicts = asyncio.run(run())
    output = args.output or args.instances_dir / VALIDATION_FILENAME
    atomic_write_text(output, render_validation(verdicts, run_ids))
    rejected = [v.instance_id for v in verdicts if not v.accepted]
    print(f"{len(verdicts) - len(rejected)} accepted, {len(rejected)} rejected; wrote {output}")
    for instance_id in rejected:
        print(f"  rejected: {instance_id}", file=sys.stderr)
    return 1 if rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
