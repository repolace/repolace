"""The benchmark report: DB rows and run manifests in, the headline number and everything around it out.

Reads the database (and the run manifests) only, so a finished sweep can be rescored
or re-sliced without running anything. `aggregate` is pure; `load_rows` is the one
place that touches SQL. **The accounting below is the claim**, so it is written down
once, here, and the Markdown repeats it next to the number.

**The headline is `passed / planned`, per run.** The denominator is the grid the run
planned -- its manifest's `instance_ids x range(runs_per_instance)` -- and every
planned row that is not a pass is a non-pass, whatever the reason: a failure, a
`passed_with_test_edit` (never a pass, approved or not), a harness error, an
inadmissible row. Excluding rows after seeing the results is what a skeptical reviewer
calls cherry-picking, so the headline does not. The figure that does exclude them,
`passed / admissible`, is printed as a labelled SECONDARY, with the exclusions listed.

**The grid is checked against the database, not the other way round.** A planned row
that does not exist, or is still queued or running, WITHHOLDS the headline ("headline
withheld: N of M planned rows missing/unfinished", with the list) unless
`--allow-partial`, in which case the figure itself carries `PARTIAL (n of m)`. A run
with no manifest has no grid to check; its headline is computed over the rows that
exist and carries `UNANCHORED`.

**Every finished agent row lands in exactly one bucket**, decided by `classify` in
this order (first match wins):

1. `unfinished` -- status `queued`/`running`.
2. `harness_error` -- status `failed` (repolace itself broke), and only that. A stop
   reason of `llm_error` is **not** a harness error: the agent loop reports it for
   non-transient model errors too (a context-window overflow, an unparseable
   replayed tool call), which depend on how hard the instance is and how large the
   model's context is, and the scorer already gives such a task an outcome ("no
   scored attempt" is FAILED). Such a row is counted by that outcome, and shown in
   its own `llm_error` column so the reader can see how many there were.
3. `inadmissible` -- finished, no harness error, `outcome` NULL: the instrument
   could not score it (`score()` returned inadmissible). Listed with the reason.
4. `passed_with_test_edit` -- scored, but the diff touched tests. **Never counted
   as passed**, and a non-pass in every denominator.
5. `passed` / `failed`.

**The interval is over instances.** Per-instance pass rate is passes over that
instance's planned runs; a percentile bootstrap (seed and resample count are fixed
constants in `harness.metrics`) resamples INSTANCES, because the runs of one instance
are not independent -- an instance a model can fix it mostly fixes every time, so an
interval over the pooled rows is far too narrow. The per-run table and its min-max
range are the run-to-run spread with the instances held fixed, and are labelled as
not an interval.

Gold-validation rows (`eval_run_id` starting `gold-`) are reported separately and
are in none of the above.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harness.metrics import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    CONFIDENCE,
    cluster_bootstrap_interval,
    percentile,
)
from harness.run_manifest import ManifestError, RunManifest, load_manifest, manifest_path
from repolace_shared.db.models import LLMCall, Task, TaskOutcome, TaskStatus, TaskTestRun
from repolace_shared.instances import InstanceError, InstanceSpec, load_instances

GOLD_RUN_PREFIX = "gold-"
#: Gold runs per instance needed to call it validated: the second run is what
#: catches a flaky suite (the first could pass by luck).
GOLD_MIN_RUNS = 2

PASSED = "passed"
FAILED = "failed"
PASSED_WITH_TEST_EDIT = "passed_with_test_edit"
INADMISSIBLE = "inadmissible"
HARNESS_ERROR = "harness_error"
UNFINISHED = "unfinished"
_BUCKETS = (PASSED, FAILED, PASSED_WITH_TEST_EDIT, INADMISSIBLE, HARNESS_ERROR, UNFINISHED)

#: `agent_stop_reason` of a run the model provider failed. Counted, never excluded.
LLM_ERROR = "llm_error"
#: What `verify.scoring.score` writes into the reason of a PASSED task scored without
#: a curated fail-to-pass list: "some baseline failure went green", which is not
#: evidence about THIS issue.
UNCURATED_MARKER = "uncurated"

CONTAMINATION_CAVEAT = (
    "SWE-bench Verified is public and widely used, so these instances and their upstream fixes are "
    "likely present in the training data of the models evaluated. This figure measures the pipeline on "
    "a possibly memorised set: it shows the loop works end to end, not how it would do on unseen "
    "issues, and it is not comparable with numbers reported on other benchmarks."
)
SUBSET_CAVEAT = (
    "The instances are a filtered subset (pure-Python pytest repositories whose test patches and gold "
    "patches passed the selection filters), not the official Verified set, so the number is not the "
    "published SWE-bench Verified score. A cross-check against the official harness is possible with "
    "the predictions export."
)
SMALL_N_CAVEAT = (
    "At N = 15 to 20 instances the 95% interval is about 30 to 40 percentage points wide (a Wilson interval at "
    "p = 0.5 is 30-75% for N = 15 and 30-70% for N = 20), and one instance moves the rate by 1/N. The "
    "run-to-run range is the spread of the run rates with the instances held fixed. It is not an interval and "
    "does not capture the uncertainty from having sampled N instances."
)


class ReportError(RuntimeError):
    """The rows cannot be reported on without hiding something."""


@dataclass(frozen=True)
class TaskRow:
    """One benchmark task, as the report sees it. Built by `load_rows`, or by hand in tests."""

    eval_run_id: str
    instance_id: str
    run_index: int
    status: TaskStatus
    outcome: TaskOutcome | None
    agent_stop_reason: str | None = None
    score_reason: str | None = None
    error_message: str | None = None
    #: `task_test_runs` rows with attempt >= 1 (the baseline is attempt 0).
    attempts: int = 0
    last_run_error: str | None = None
    model: str | None = None
    llm_calls: int = 0
    #: Calls with a NULL cost: an error row, or one nothing priced. The sum below
    #: leaves them out, so a non-zero count means the cost is understated.
    unpriced_calls: int = 0
    #: `SUM(llm_calls.cost_usd)`; None when no call carried a cost.
    cost_usd: Decimal | None = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: When the task row was created; what `--supersede latest` orders by.
    created_at: datetime | None = None
    test_edit_approved: bool = False
    patch_diff: str | None = None
    #: From the instance files, not the database (the task's repo is the bench repo).
    repo: str | None = None
    targeted_p2p: bool | None = None


def is_gold(row: TaskRow) -> bool:
    return row.eval_run_id.startswith(GOLD_RUN_PREFIX)


def classify(row: TaskRow) -> str:
    """The one bucket a row belongs to. The order is the accounting; see the module docstring."""
    if row.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
        return UNFINISHED
    if row.status is TaskStatus.FAILED:
        return HARNESS_ERROR
    if row.outcome is None:
        return INADMISSIBLE
    if row.outcome is TaskOutcome.PASSED_WITH_TEST_EDIT:
        return PASSED_WITH_TEST_EDIT
    if row.outcome is TaskOutcome.PASSED:
        return PASSED
    return FAILED


# --- statistics --------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    n: int
    mean: float | None
    median: float | None
    p95: float | None
    minimum: float | None
    maximum: float | None
    total: float | None


#: A p95 needs enough points to have a tail. Below this it is within a rank or two of
#: the maximum, and printing it beside n = 20 invites reading it as a tail estimate;
#: the median and the maximum are what such a sample can honestly give.
P95_MIN_N = 40

LATENCY_NOTE = (
    "Latency is claim to completion: it includes the clone, indexing, the baseline run, the agent, verification "
    "and the push, excludes the time a task waited in the queue, and includes contention from the sweep's "
    "parallel runs, so it is not what one task takes alone."
)
CENSORING_NOTE = (
    "The per-task budget (the $2 cap) and the wall-clock limit CENSOR cost and latency: a task stopped by either "
    "shows the limit, not what it would have cost to finish, so these figures are floors for the tasks that hit one."
)


def describe(values: Sequence[float]) -> Stats:
    """n, mean, median, max, total; `p95` only from `P95_MIN_N` points up."""
    if not values:
        return Stats(0, None, None, None, None, None, None)
    return Stats(
        n=len(values),
        mean=sum(values) / len(values),
        median=statistics.median(values),
        p95=percentile(values, 95) if len(values) >= P95_MIN_N else None,
        minimum=min(values),
        maximum=max(values),
        total=sum(values),
    )


# --- groups and the headline -------------------------------------------------


@dataclass(frozen=True)
class GroupSummary:
    """Descriptive counts for one set of rows. Never the headline."""

    rows: int
    instances: int
    counts: dict[str, int]
    #: Finished rows whose stop reason is `llm_error`. Already in `counts` under
    #: whatever bucket their outcome puts them in; shown apart so nobody has to
    #: wonder how many of the failures were the provider's.
    llm_errors: int
    passed: int
    finished: int


def summarize(rows: Sequence[TaskRow]) -> GroupSummary:
    """Counts for one set of agent rows."""
    totals: Counter[str] = Counter(classify(row) for row in rows)
    finished = sum(n for bucket, n in totals.items() if bucket != UNFINISHED)
    return GroupSummary(
        rows=len(rows),
        instances=len({row.instance_id for row in rows}),
        counts={bucket: totals[bucket] for bucket in _BUCKETS},
        llm_errors=sum(1 for row in rows if row.agent_stop_reason == LLM_ERROR and classify(row) != UNFINISHED),
        passed=totals[PASSED],
        finished=finished,
    )


@dataclass(frozen=True)
class Interval:
    low: float
    high: float
    #: Instances resampled; the runs of one instance are not independent.
    instances: int
    resamples: int
    seed: int
    confidence: float


@dataclass(frozen=True)
class PerRun:
    run_index: int
    passed: int
    planned: int
    rate: float | None


@dataclass(frozen=True)
class SecondaryRate:
    """`passed / admissible`: the figure with the instrument failures taken out. Never the headline."""

    passed: int
    admissible: int
    excluded_harness_errors: int
    excluded_inadmissible: int
    rate: float | None


@dataclass(frozen=True)
class Headline:
    """The pass rate of one run over its PLANNED grid, every non-pass counted as one.

    `passed / planned`. The denominator is the grid the run planned (the manifest's
    `instance_ids x range(runs_per_instance)`), not the rows that happen to exist and
    not the rows left after the results were looked at: a harness error, an
    inadmissible row, a `passed_with_test_edit` and a failure are all non-passes here.
    Taking rows out after seeing them is cherry-picking; the figure that does so
    (`secondary`) is labelled as that and listed.
    """

    run_id: str
    #: The manifest's model, else the one model seen in the rows.
    model: str | None
    git_sha: str | None
    anchored: bool
    instances: int
    #: Distinct repositories among the planned instances whose repository is known.
    repos: int
    runs_per_instance: int
    planned: int
    passed: int
    #: None when withheld.
    rate: float | None
    interval: Interval | None
    #: Why the figure is not printed, or None.
    withheld: str | None
    #: Planned `instance#run` pairs with no row at all.
    missing: tuple[str, ...]
    #: Planned pairs whose row is still queued or running.
    unfinished: tuple[str, ...]
    #: Rows in the database that the manifest does not plan; ignored, and listed.
    unplanned: tuple[str, ...]
    #: Loud labels printed on the figure itself: UNANCHORED, PARTIAL (n of m), ...
    flags: tuple[str, ...]
    per_run: tuple[PerRun, ...]
    #: Min and max of the per-run rates. A spread with the instances held fixed, not an interval.
    run_range: tuple[float, float] | None
    secondary: SecondaryRate | None


def _pair_label(instance_id: str, run_index: int) -> str:
    return f"{instance_id}#{run_index}"


def _headline(
    run_id: str,
    rows: Sequence[TaskRow],
    manifest: RunManifest | None,
    *,
    allow_partial: bool,
    instance_repos: Mapping[str, str],
) -> Headline:
    by_pair = {(r.instance_id, r.run_index): r for r in rows}
    if manifest is not None:
        planned = sorted(manifest.planned_pairs())
        planned_set = set(planned)
        unplanned = tuple(sorted(_pair_label(*pair) for pair in by_pair if pair not in planned_set))
    else:
        planned = sorted(by_pair)
        unplanned = ()

    instance_ids = sorted({i for i, _ in planned})
    per_instance_runs = Counter(i for i, _ in planned)
    flags: list[str] = []
    if manifest is None:
        flags.append("UNANCHORED")
    if len(set(per_instance_runs.values())) > 1:
        flags.append("UNEQUAL RUNS PER INSTANCE")

    buckets: dict[tuple[str, int], str] = {}
    missing: list[str] = []
    unfinished: list[str] = []
    for pair in planned:
        row = by_pair.get(pair)
        if row is None:
            missing.append(_pair_label(*pair))
            continue
        buckets[pair] = classify(row)
        if buckets[pair] == UNFINISHED:
            unfinished.append(_pair_label(*pair))

    models = sorted({r.model for r in rows if r.model})
    model = manifest.model if manifest is not None else (models[0] if len(models) == 1 else None)
    if manifest is not None and manifest.agent != "llm":
        raise ReportError(
            f"run {run_id} is a {manifest.agent!r} run according to its manifest, not an LLM agent run: "
            f"refusing to report it as one"
        )
    repos = {instance_repos.get(i) or next((r.repo for r in rows if r.instance_id == i and r.repo), None) for i in instance_ids}
    repos.discard(None)
    runs_per_instance = manifest.runs_per_instance if manifest is not None else max(per_instance_runs.values(), default=0)

    common = dict(
        run_id=run_id, model=model, git_sha=manifest.git_sha if manifest is not None else None,
        anchored=manifest is not None, instances=len(instance_ids), repos=len(repos),
        runs_per_instance=runs_per_instance, planned=len(planned),
        missing=tuple(missing), unfinished=tuple(unfinished), unplanned=unplanned,
    )

    not_finished = len(missing) + len(unfinished)
    reasons = []
    if not_finished and not allow_partial:
        reasons.append(
            f"{not_finished} of {len(planned)} planned rows missing/unfinished "
            f"({len(missing)} missing, {len(unfinished)} unfinished); pass --allow-partial to print it marked PARTIAL"
        )
    if len(models) > 1:
        # A model switched mid-sweep, or two models were filed under one run id. One
        # rate over both is a statement about neither; the By model counts are the figures.
        reasons.append(
            f"mixed models within the run ({', '.join(models)}): no pooled figure, read the By model counts"
        )
    if reasons:
        return Headline(
            **common, passed=sum(1 for b in buckets.values() if b == PASSED), rate=None, interval=None,
            withheld="headline withheld: " + "; ".join(reasons),
            flags=tuple(flags), per_run=(), run_range=None, secondary=None,
        )
    if not_finished:
        flags.append(f"PARTIAL ({len(planned) - not_finished} of {len(planned)})")

    passed = sum(1 for b in buckets.values() if b == PASSED)
    passes = [by_pair[pair] for pair, bucket in buckets.items() if bucket == PASSED]
    uncurated = sum(1 for r in passes if UNCURATED_MARKER in (r.score_reason or ""))
    if uncurated:
        flags.append(f"UNCURATED ({uncurated} of {passed} passes)")
    no_calls = sum(1 for r in passes if r.llm_calls == 0)
    if no_calls:
        flags.append(f"NO-LLM-CALL PASSES ({no_calls} of {passed})")
    pass_counts = [sum(1 for pair in planned if pair[0] == i and buckets.get(pair) == PASSED) for i in instance_ids]
    run_counts = [per_instance_runs[i] for i in instance_ids]
    interval = None
    bounds = cluster_bootstrap_interval(pass_counts, run_counts)
    if bounds is not None:
        interval = Interval(bounds[0], bounds[1], len(instance_ids), BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, CONFIDENCE)

    per_run = []
    for index in sorted({r for _, r in planned}):
        pairs = [pair for pair in planned if pair[1] == index]
        wins = sum(1 for pair in pairs if buckets.get(pair) == PASSED)
        per_run.append(PerRun(index, wins, len(pairs), wins / len(pairs)))
    rates = [r.rate for r in per_run if r.rate is not None]

    present = [b for b in buckets.values() if b != UNFINISHED]
    admissible = sum(1 for b in present if b in (PASSED, FAILED, PASSED_WITH_TEST_EDIT))
    secondary = SecondaryRate(
        passed=passed, admissible=admissible,
        excluded_harness_errors=sum(1 for b in present if b == HARNESS_ERROR),
        excluded_inadmissible=sum(1 for b in present if b == INADMISSIBLE),
        rate=passed / admissible if admissible else None,
    )
    return Headline(
        **common, passed=passed, rate=passed / len(planned) if planned else None, interval=interval,
        withheld=None, flags=tuple(flags), per_run=tuple(per_run),
        run_range=(min(rates), max(rates)) if len(rates) >= 2 else None, secondary=secondary,
    )


# --- duplicate rows ----------------------------------------------------------


def _conflict(a: TaskRow, b: TaskRow) -> bool:
    """The same `(instance, run_index)` for what could be the same model.

    A row whose model is unknown (it died before its first LLM call) is a
    wildcard: it could be the failed first attempt at a pair a real-model row
    re-ran. Keying on the model alone gave that case no warning at all.
    """
    return (
        a.instance_id == b.instance_id
        and a.run_index == b.run_index
        and (a.model is None or b.model is None or a.model == b.model)
    )


def find_duplicates(rows: Sequence[TaskRow]) -> list[tuple[TaskRow, TaskRow]]:
    """Every pair of agent rows that would count one `(instance, run_index)` twice."""
    by_pair: dict[tuple[str, int], list[TaskRow]] = {}
    for row in rows:
        if not is_gold(row):
            by_pair.setdefault((row.instance_id, row.run_index), []).append(row)
    return [
        (a, b)
        for group in by_pair.values()
        for index, a in enumerate(group)
        for b in group[index + 1:]
        if _conflict(a, b)
    ]


SUPERSEDE_MODES = ("none", "latest")


def resolve_duplicates(rows: Sequence[TaskRow], mode: str = "none") -> tuple[list[TaskRow], tuple[RowRef, ...]]:
    """Refuse duplicate rows, or keep the newest of each and say what was dropped.

    A re-run needs a new `eval_run_id`, so re-running failed tasks creates a second
    row for the same `(instance, run_index)`, and counting both double counts it.
    `none` (the default) refuses and lists them; `latest` keeps the row with the
    newest `created_at` (a missing one is the oldest) and returns what it dropped,
    for the report to print. Gold rows are never touched: gold validation repeats
    each pair on purpose.
    """
    if mode not in SUPERSEDE_MODES:
        raise ReportError(f"unknown --supersede mode {mode!r}; use one of {', '.join(SUPERSEDE_MODES)}")
    duplicates = find_duplicates(rows)
    if not duplicates:
        return list(rows), ()
    if mode == "none":
        labels = sorted({f"{a.instance_id}#{a.run_index} ({a.eval_run_id}, {b.eval_run_id})" for a, b in duplicates})
        shown = "; ".join(labels[:10]) + (f"; ... ({len(labels) - 10} more)" if len(labels) > 10 else "")
        raise ReportError(
            f"{len(labels)} (instance, run_index) pair(s) have more than one row, which would count them twice: "
            f"{shown}. A re-run needs a new eval run id; pass --supersede latest to keep the newest row of each "
            f"(what is dropped is printed)"
        )

    floor = datetime.min.replace(tzinfo=timezone.utc)
    kept: list[TaskRow] = []
    dropped: list[RowRef] = []
    gold = [r for r in rows if is_gold(r)]
    newest_first = sorted(
        (r for r in rows if not is_gold(r)),
        key=lambda r: (r.created_at or floor, r.eval_run_id), reverse=True,
    )
    for row in newest_first:
        winner = next((k for k in kept if _conflict(k, row)), None)
        if winner is None:
            kept.append(row)
        else:
            dropped.append(RowRef(row.eval_run_id, row.instance_id, row.run_index, f"superseded by the newer row in {winner.eval_run_id}"))
    kept_ids = {id(r) for r in kept}
    ordered = [r for r in rows if is_gold(r) or id(r) in kept_ids]
    assert len(ordered) == len(kept) + len(gold)
    return ordered, tuple(sorted(dropped, key=lambda d: (d.instance_id, d.run_index, d.eval_run_id)))


# --- the report --------------------------------------------------------------


@dataclass(frozen=True)
class RowRef:
    eval_run_id: str
    instance_id: str
    run_index: int
    detail: str


@dataclass(frozen=True)
class PassedWithTestEditBucket:
    count: int
    #: Signed off by a human. Still not in the headline: the criteria say it counts
    #: towards nothing until reviewed, and the report does not make that call.
    approved: int
    pending: int
    rows: tuple[RowRef, ...]


@dataclass(frozen=True)
class TokenTotals:
    calls: int
    unpriced_calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    #: `cached_input_tokens / input_tokens`; `input_tokens` already includes the
    #: cached part (it is the total prompt). None when nothing was sent.
    cache_read_ratio: float | None


@dataclass(frozen=True)
class TargetedP2P:
    rows: int
    instances: tuple[str, ...]
    unknown_rows: int


@dataclass(frozen=True)
class GoldInstance:
    instance_id: str
    #: validated | validated-once | flaky | failed | unscoreable | pending
    status: str
    outcomes: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class GoldSummary:
    instances: tuple[GoldInstance, ...]
    validated: int
    #: Agent-run instances with no `validated` gold: the headline's ceiling is lower
    #: than N, and the instrument is unproven for these.
    agent_instances_not_validated: tuple[str, ...]


@dataclass(frozen=True)
class BucketCost:
    rows: int
    cost: Stats
    latency: Stats


@dataclass(frozen=True)
class ModelBlock:
    """Everything that is a distribution, for ONE model.

    Cost, latency, tokens, attempts and stop reasons are never pooled across models:
    a mean between a $0.10 and a $5.00 model is a number no task ever cost.
    """

    model: str
    #: Finished rows of this model (harness errors included: the money was spent).
    rows: int
    cost: Stats
    cost_rows_without_data: int
    total_cost_usd: float
    passes: int
    #: Total cost of every finished row, failures included, over passes: what a pass
    #: costs once the attempts that did not pass are paid for. None with no passes.
    cost_per_pass: float | None
    latency: Stats
    latency_rows_without_data: int
    #: Cost and latency by outcome bucket (passed, failed, ...): a pass and a
    #: failure that hit the budget are different populations.
    by_bucket: dict[str, BucketCost]
    tokens: TokenTotals
    attempts: dict[int, int]
    stop_reasons: dict[str, int]


@dataclass(frozen=True)
class Report:
    agent_runs: tuple[str, ...]
    gold_runs: tuple[str, ...]
    #: One per agent run, each over that run's own planned grid. There is no pooled
    #: figure across runs: a run is one model (see `Headline`), and pooling two
    #: models is not a statement about either.
    headlines: tuple[Headline, ...]
    #: Counts only, over every agent row. Not a rate anyone should read as a headline.
    overall: GroupSummary
    #: More than one model among the rows.
    mixed_models: bool
    by_model: dict[str, GroupSummary]
    by_repo: dict[str, GroupSummary]
    harness_errors: tuple[RowRef, ...]
    inadmissible: tuple[RowRef, ...]
    passed_with_test_edit: PassedWithTestEditBucket
    unfinished: tuple[RowRef, ...]
    #: Planned rows that have no row in the database at all.
    missing: tuple[RowRef, ...]
    #: Older rows dropped by `--supersede latest`, kept here so the drop is visible.
    superseded: tuple[RowRef, ...]
    unfinished_cost_usd: float | None
    #: Instances with finished rows and not one admissible row.
    instances_never_admissible: tuple[str, ...]
    models: dict[str, ModelBlock]
    targeted_p2p: TargetedP2P
    gold: GoldSummary
    warnings: tuple[str, ...]
    caveats: tuple[str, ...]


def _detail(row: TaskRow, bucket: str) -> str:
    if bucket == HARNESS_ERROR:
        parts = [
            f"status={row.status.value}",
            f"stop={row.agent_stop_reason or '-'}",
            f"outcome={row.outcome.value if row.outcome is not None else '-'}",
        ]
        if row.score_reason:
            parts.append(f"score: {row.score_reason.strip()[:200]}")
        if row.error_message:
            parts.append(row.error_message.strip()[:200])
        return "; ".join(parts)
    if bucket == INADMISSIBLE:
        return row.score_reason or row.last_run_error or "(no reason recorded)"
    if bucket == PASSED_WITH_TEST_EDIT:
        return row.score_reason or "(no reason recorded)"
    return f"status={row.status.value}"


def _ref(row: TaskRow, bucket: str) -> RowRef:
    return RowRef(row.eval_run_id, row.instance_id, row.run_index, _detail(row, bucket))


def _sorted_refs(rows: Iterable[TaskRow], bucket: str) -> tuple[RowRef, ...]:
    return tuple(_ref(row, bucket) for row in sorted(rows, key=lambda r: (r.instance_id, r.run_index, r.eval_run_id)))


def _latency(row: TaskRow) -> float | None:
    if row.started_at is None or row.completed_at is None:
        return None
    seconds = (row.completed_at - row.started_at).total_seconds()
    return seconds if seconds >= 0 else None


def _gold_summary(gold_rows: Sequence[TaskRow], agent_instances: Iterable[str]) -> GoldSummary:
    by_instance: dict[str, list[TaskRow]] = {}
    for row in gold_rows:
        by_instance.setdefault(row.instance_id, []).append(row)

    instances = []
    for instance_id in sorted(by_instance):
        rows = sorted(by_instance[instance_id], key=lambda r: (r.eval_run_id, r.run_index))
        buckets = [classify(r) for r in rows]
        passed = sum(1 for b in buckets if b == PASSED)
        reasons = [r.score_reason or r.error_message for r, b in zip(rows, buckets) if b != PASSED]
        reason = next((text for text in reasons if text), "")
        if UNFINISHED in buckets:
            status = "pending"
        elif passed == len(rows):
            status = "validated" if len(rows) >= GOLD_MIN_RUNS else "validated-once"
        elif passed:
            status = "flaky"
        elif any(b in (HARNESS_ERROR, INADMISSIBLE) for b in buckets):
            status = "unscoreable"
        else:
            status = "failed"
        instances.append(GoldInstance(instance_id, status, tuple(buckets), reason))

    validated = {g.instance_id for g in instances if g.status == "validated"}
    return GoldSummary(
        instances=tuple(instances),
        validated=len(validated),
        agent_instances_not_validated=tuple(sorted(set(agent_instances) - validated)),
    )


def _model_block(label: str, rows: Sequence[TaskRow]) -> ModelBlock:
    costs = [float(r.cost_usd) for r in rows if r.cost_usd is not None]
    latencies = [v for v in (_latency(r) for r in rows) if v is not None]
    passes = sum(1 for r in rows if classify(r) == PASSED)
    input_tokens = sum(r.input_tokens for r in rows)
    cached = sum(r.cached_input_tokens for r in rows)
    by_bucket = {}
    for bucket in (PASSED, FAILED, PASSED_WITH_TEST_EDIT, INADMISSIBLE, HARNESS_ERROR):
        members = [r for r in rows if classify(r) == bucket]
        if members:
            by_bucket[bucket] = BucketCost(
                rows=len(members),
                cost=describe([float(r.cost_usd) for r in members if r.cost_usd is not None]),
                latency=describe([v for v in (_latency(r) for r in members) if v is not None]),
            )
    return ModelBlock(
        model=label,
        rows=len(rows),
        cost=describe(costs),
        cost_rows_without_data=len(rows) - len(costs),
        total_cost_usd=sum(costs),
        passes=passes,
        cost_per_pass=sum(costs) / passes if passes else None,
        latency=describe(latencies),
        latency_rows_without_data=len(rows) - len(latencies),
        by_bucket=by_bucket,
        tokens=TokenTotals(
            calls=sum(r.llm_calls for r in rows),
            unpriced_calls=sum(r.unpriced_calls for r in rows),
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=sum(r.output_tokens for r in rows),
            cache_read_ratio=cached / input_tokens if input_tokens else None,
        ),
        attempts=dict(sorted(Counter(r.attempts for r in rows).items())),
        stop_reasons=dict(sorted(Counter(r.agent_stop_reason or "(none)" for r in rows).items())),
    )


def aggregate(
    rows: Sequence[TaskRow],
    *,
    manifests: Mapping[str, RunManifest] | None = None,
    allow_partial: bool = False,
    instance_repos: Mapping[str, str] | None = None,
    superseded: Sequence[RowRef] = (),
) -> Report:
    """Everything the report says, from rows (and run manifests) alone. Pure and deterministic.

    `manifests` maps an eval run id to its plan. A run with a manifest is anchored
    to that grid; a run without one is headlined over the rows that exist and
    marked UNANCHORED. An incomplete grid withholds the headline unless
    `allow_partial`, in which case the figure carries PARTIAL (n of m).
    """
    manifests = manifests or {}
    instance_repos = instance_repos or {}
    gold_rows = [r for r in rows if is_gold(r)]
    agent_rows = [r for r in rows if not is_gold(r)]
    by_bucket: dict[str, list[TaskRow]] = {b: [] for b in _BUCKETS}
    for row in agent_rows:
        by_bucket[classify(row)].append(row)
    finished = [row for bucket, members in by_bucket.items() if bucket != UNFINISHED for row in members]

    agent_runs = sorted({r.eval_run_id for r in agent_rows} | {run for run in manifests})
    headlines = tuple(
        _headline(
            run_id, [r for r in agent_rows if r.eval_run_id == run_id], manifests.get(run_id),
            allow_partial=allow_partial, instance_repos=instance_repos,
        )
        for run_id in agent_runs
    )

    real_models = sorted({r.model for r in agent_rows if r.model})
    by_model = {
        (model or "(no model call)"): summarize([r for r in agent_rows if r.model == model])
        for model in sorted({r.model for r in agent_rows}, key=lambda m: (m is None, m or ""))
    }
    by_repo = {
        (repo or "(unknown)"): summarize([r for r in agent_rows if r.repo == repo])
        for repo in sorted({r.repo for r in agent_rows}, key=lambda m: (m is None, m or ""))
    }

    admissible_instances = {
        row.instance_id for bucket in (PASSED, FAILED, PASSED_WITH_TEST_EDIT) for row in by_bucket[bucket]
    }
    never_admissible = sorted({r.instance_id for r in finished} - admissible_instances)

    models = {
        (model or "(no model call)"): _model_block(
            model or "(no model call)", [r for r in finished if r.model == model],
        )
        for model in sorted({r.model for r in finished}, key=lambda m: (m is None, m or ""))
    }
    unpriced_calls = sum(r.unpriced_calls for r in finished)

    unfinished_costs = [float(r.cost_usd) for r in by_bucket[UNFINISHED] if r.cost_usd is not None]
    approved = sum(1 for r in by_bucket[PASSED_WITH_TEST_EDIT] if r.test_edit_approved)

    targeted = [r for r in finished if r.targeted_p2p is True]
    targeted_p2p = TargetedP2P(
        rows=len(targeted),
        instances=tuple(sorted({r.instance_id for r in targeted})),
        unknown_rows=sum(1 for r in finished if r.targeted_p2p is None),
    )

    gold = _gold_summary(gold_rows, {r.instance_id for r in agent_rows})

    warnings: list[str] = []
    for headline in headlines:
        if not headline.anchored:
            warnings.append(
                f"No run manifest for {headline.run_id}: the expected grid is unknown, so its headline is "
                f"computed over the rows that exist and is marked UNANCHORED."
            )
        if headline.withheld:
            warnings.append(f"{headline.run_id}: {headline.withheld}.")
        if headline.unplanned:
            warnings.append(
                f"{headline.run_id}: {len(headline.unplanned)} row(s) are not in the manifest's grid and are "
                f"ignored by the headline: {', '.join(headline.unplanned[:5])}."
            )
        for flag in headline.flags:
            if flag.startswith("UNCURATED"):
                warnings.insert(0, (
                    f"{headline.run_id}: {flag}. These passes were scored without a curated fail-to-pass list, "
                    f"so each means only that some test that failed at the base commit now passes -- not that "
                    f"this issue was fixed. The headline overstates until the curated list is wired in."
                ))
            elif flag.startswith("NO-LLM-CALL"):
                warnings.insert(0, (
                    f"{headline.run_id}: {flag}. A pass that made no LLM call is a gold or stub run, not an "
                    f"agent run: check the run id (a gold run belongs under --gold-run)."
                ))
        if "UNEQUAL RUNS PER INSTANCE" in headline.flags:
            warnings.append(
                f"{headline.run_id}: instances have different numbers of runs; the headline is the pooled "
                f"count, not an average of instance rates."
            )
    if by_bucket[UNFINISHED]:
        warnings.append(f"{len(by_bucket[UNFINISHED])} row(s) are still queued or running.")
    if by_bucket[HARNESS_ERROR]:
        warnings.append(
            f"{len(by_bucket[HARNESS_ERROR])} harness error row(s) (status failed) are counted as non-passes "
            f"in the headline and excluded from the secondary passed/admissible figure. Re-run them: an "
            f"instrument failure is not an agent failure, but it is not a pass either."
        )
    llm_error_rows = [r for r in finished if r.agent_stop_reason == LLM_ERROR]
    if llm_error_rows:
        scored = sum(1 for r in llm_error_rows if r.outcome is not None)
        warnings.append(
            f"{len(llm_error_rows)} row(s) stopped on llm_error ({scored} scored by their outcome, "
            f"{len(llm_error_rows) - scored} with no outcome). They are counted by outcome, never excluded "
            f"for the stop reason: a context overflow or an unparseable tool call depends on the instance."
        )
    if by_bucket[INADMISSIBLE]:
        warnings.append(
            f"{len(by_bucket[INADMISSIBLE])} inadmissible row(s) (the instrument could not score them) are "
            f"counted as non-passes in the headline and excluded from the secondary passed/admissible figure."
        )
    if never_admissible:
        warnings.append(
            f"{len(never_admissible)} instance(s) have no admissible row at all: {', '.join(never_admissible)}."
        )
    if len(real_models) > 1:
        warnings.append(
            f"MIXED MODELS ({', '.join(real_models)}): each run has its own headline; nothing here pools them."
        )
    duplicates = sorted({f"{a.instance_id}#{a.run_index}" for a, _ in find_duplicates(agent_rows)})
    if duplicates:
        warnings.append(
            f"{len(duplicates)} (instance, run_index) pair(s) appear more than once (a row with no model counts "
            f"as the same model), across eval runs: {', '.join(duplicates[:5])}. The count and cost tables below "
            f"include both; build_report refuses this unless --supersede latest."
        )
    if superseded:
        warnings.append(
            f"--supersede latest dropped {len(superseded)} older row(s) for the same (instance, run_index): "
            f"{', '.join(f'{d.instance_id}#{d.run_index} ({d.eval_run_id})' for d in superseded[:5])}"
            f"{' ...' if len(superseded) > 5 else ''}."
        )
    if unpriced_calls:
        warnings.append(f"{unpriced_calls} LLM call(s) carry no cost; the cost figures understate spend.")
    if not gold_rows:
        warnings.append(
            "No gold-validation rows were supplied: nothing here shows the instrument can score these "
            "instances. Run gold validation before trusting the headline."
        )
    elif gold.agent_instances_not_validated:
        warnings.append(
            f"{len(gold.agent_instances_not_validated)} instance(s) in the agent runs have no validated "
            f"gold run (at least {GOLD_MIN_RUNS} passing): {', '.join(gold.agent_instances_not_validated)}."
        )
    if targeted_p2p.rows:
        warnings.append(
            f"{targeted_p2p.rows} row(s) ran pass-to-pass over a targeted subset, not the full suite; "
            f"their results are not comparable with a full-suite run."
        )
    if targeted_p2p.unknown_rows:
        warnings.append(
            f"{targeted_p2p.unknown_rows} row(s) have no instance data, so whether pass-to-pass was "
            f"targeted is unknown."
        )

    return Report(
        agent_runs=tuple(agent_runs),
        gold_runs=tuple(sorted({r.eval_run_id for r in gold_rows})),
        headlines=headlines,
        overall=summarize(agent_rows),
        mixed_models=len(real_models) > 1,
        by_model=by_model,
        by_repo=by_repo,
        harness_errors=_sorted_refs(by_bucket[HARNESS_ERROR], HARNESS_ERROR),
        inadmissible=_sorted_refs(by_bucket[INADMISSIBLE], INADMISSIBLE),
        passed_with_test_edit=PassedWithTestEditBucket(
            count=len(by_bucket[PASSED_WITH_TEST_EDIT]),
            approved=approved,
            pending=len(by_bucket[PASSED_WITH_TEST_EDIT]) - approved,
            rows=_sorted_refs(by_bucket[PASSED_WITH_TEST_EDIT], PASSED_WITH_TEST_EDIT),
        ),
        unfinished=_sorted_refs(by_bucket[UNFINISHED], UNFINISHED),
        missing=tuple(
            RowRef(h.run_id, label.rsplit("#", 1)[0], int(label.rsplit("#", 1)[1]), "planned, but no row in the database")
            for h in headlines for label in h.missing
        ),
        superseded=tuple(superseded),
        unfinished_cost_usd=sum(unfinished_costs) if unfinished_costs else None,
        instances_never_admissible=tuple(never_admissible),
        models=models,
        targeted_p2p=targeted_p2p,
        gold=gold,
        warnings=tuple(warnings),
        caveats=(CONTAMINATION_CAVEAT, SUBSET_CAVEAT, SMALL_N_CAVEAT),
    )


# --- rendering ---------------------------------------------------------------


def to_json(report: Report) -> str:
    return json.dumps(asdict(report), indent=2, ensure_ascii=False) + "\n"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.4f}"


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _whole(value: float) -> str:
    return f"{value * 100:.0f}%"


def _frac(passed: int, total: int) -> str:
    """Counts first, then the whole-number percentage."""
    return f"{passed} of {total} ({_whole(passed / total)})" if total else f"{passed} of {total}"


def _group_row(name: str, group: GroupSummary) -> str:
    c = group.counts
    return (
        f"| {_cell(name)} | {group.instances} | {group.rows} | {c[PASSED]} | {c[FAILED]} | "
        f"{c[PASSED_WITH_TEST_EDIT]} | {c[INADMISSIBLE]} | {c[HARNESS_ERROR]} | {group.llm_errors} | "
        f"{c[UNFINISHED]} | {_frac(group.passed, group.finished)} |"
    )


_GROUP_HEADER = (
    "| group | instances | rows | passed | failed | passed_with_test_edit | inadmissible | harness error "
    "| of which llm_error (counted by outcome) | unfinished | passed of finished |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)


def _headline_lines(h: Headline) -> list[str]:
    title = f"### Run {h.run_id}" + (f", model {h.model}" if h.model else "")
    out = [title, ""]
    if h.withheld:
        out += [f"**{h.withheld[0].upper()}{h.withheld[1:]}.**", ""]
        for label, items in (("Missing", h.missing), ("Unfinished", h.unfinished)):
            if items:
                shown = ", ".join(items[:20]) + (f", ... ({len(items) - 20} more)" if len(items) > 20 else "")
                out += [f"{label} ({len(items)}): {shown}", ""]
        return out

    figure = f"Pass rate: {h.passed} of {h.planned} instance-runs ({_whole(h.rate)})" if h.rate is not None else "Pass rate: n/a"
    if h.interval is not None:
        figure += (
            f" [{h.interval.confidence * 100:.0f}% interval over {h.interval.instances} instances: "
            f"{_whole(h.interval.low)}-{_whole(h.interval.high)}]"
        )
    elif h.instances < 2:
        figure += " [no interval: fewer than 2 instances]"
    flags = "".join(f" **{flag}**" for flag in h.flags)
    out += [f"**{figure}**{flags}", ""]
    sha = h.git_sha or "unknown (no manifest)"
    out.append(
        f"N = {h.instances} instances from {h.repos} Python repositor{'y' if h.repos == 1 else 'ies'}, "
        f"{h.runs_per_instance} run{'' if h.runs_per_instance == 1 else 's'} each, model {h.model or 'unknown'}, "
        f"pipeline commit {sha}."
    )
    sec = h.secondary
    if sec is not None:
        out += [
            "",
            f"Every planned (instance, run) row is counted: {sec.excluded_harness_errors} harness-error and "
            f"{sec.excluded_inadmissible} inadmissible row(s) (listed below) are counted as non-passes, and so is "
            f"every `passed_with_test_edit`.",
            "",
            f"Secondary, not the headline: passed / admissible = {_frac(sec.passed, sec.admissible)}, excluding "
            f"{sec.excluded_harness_errors} harness-error and {sec.excluded_inadmissible} inadmissible row(s) (listed).",
        ]
    out += ["", "| run_index | passed of planned | rate |", "|---|---|---|"]
    out += [f"| {r.run_index} | {_frac(r.passed, r.planned)} | {_whole(r.rate) if r.rate is not None else 'n/a'} |" for r in h.per_run]
    if h.run_range is not None:
        out += [
            "",
            f"Run-to-run range of the {len(h.per_run)} run rates: {_whole(h.run_range[0])}-{_whole(h.run_range[1])}. "
            f"This holds the instances fixed; it is not an interval.",
        ]
    return out + [""]


def _refs_table(refs: Sequence[RowRef]) -> list[str]:
    if not refs:
        return ["_none_", ""]
    lines = ["| instance | run | eval run | detail |", "|---|---|---|---|"]
    lines += [f"| {_cell(r.instance_id)} | {r.run_index} | {_cell(r.eval_run_id)} | {_cell(r.detail)} |" for r in refs]
    return lines + [""]


def _stats_row(name: str, stats: Stats, fmt) -> str:
    p95 = fmt(stats.p95) if stats.p95 is not None else f"- (n < {P95_MIN_N})"
    return (
        f"| {name} | {stats.n} | {fmt(stats.median)} | {fmt(stats.maximum)} | {fmt(stats.mean)} | "
        f"{p95} | {fmt(stats.total)} |"
    )


_STATS_HEADER = "| measure | n | median | max | mean | p95 | total |\n|---|---|---|---|---|---|---|"


def _model_block_lines(block: ModelBlock) -> list[str]:
    out = [f"### {block.model}", "", _STATS_HEADER]
    out.append(_stats_row("cost (USD)", block.cost, _usd))
    out.append(_stats_row("latency (s)", block.latency, _num))
    per_pass = _usd(block.cost_per_pass) if block.cost_per_pass is not None else "n/a (no passes)"
    out += [
        "",
        f"Total spend {_usd(block.total_cost_usd)} over {block.rows} finished task(s) and {block.passes} pass(es): "
        f"cost per pass {per_pass} (every finished task's cost, failures included, over the passes).",
        f"Rows without cost data: {block.cost_rows_without_data}. Rows without both timestamps: {block.latency_rows_without_data}.",
        "",
        "By outcome bucket:",
        "",
        "| bucket | tasks | median cost | max cost | median latency (s) | max latency (s) |",
        "|---|---|---|---|---|---|",
    ]
    for bucket, part in block.by_bucket.items():
        out.append(
            f"| {bucket} | {part.rows} | {_usd(part.cost.median)} | {_usd(part.cost.maximum)} | "
            f"{_num(part.latency.median)} | {_num(part.latency.maximum)} |"
        )
    t = block.tokens
    out += [
        "",
        f"LLM calls: {t.calls} ({t.unpriced_calls} unpriced). Input tokens: {t.input_tokens:,} (of which cache reads "
        f"{t.cached_input_tokens:,}, {_pct(t.cache_read_ratio)}). Output tokens: {t.output_tokens:,}.",
        "",
        "| attempts | tasks |",
        "|---|---|",
    ]
    out += [f"| {attempts} | {count} |" for attempts, count in block.attempts.items()]
    out += ["", "| agent stop reason | tasks |", "|---|---|"]
    out += [f"| {_cell(reason)} | {count} |" for reason, count in block.stop_reasons.items()]
    return out + [""]


def to_markdown(report: Report) -> str:
    out: list[str] = ["# Benchmark report", ""]
    out.append(f"Agent runs: {', '.join(report.agent_runs) or '(none)'}. Gold runs: {', '.join(report.gold_runs) or '(none)'}.")
    out.append("")
    if report.warnings:
        out += ["## Read this first", ""] + [f"- {w}" for w in report.warnings] + [""]

    out += ["## Headline", ""]
    if not report.headlines:
        out += ["No agent runs.", ""]
    for headline in report.headlines:
        out += _headline_lines(headline)
    out += [
        "How rows are counted. The headline is passed / planned: the denominator is the run's planned grid "
        "(its manifest's instances x runs), and every planned row that did not pass is a non-pass, whatever the reason: "
        "failed, `passed_with_test_edit` (never a pass, approved or not), a harness error (status `failed`), an "
        "inadmissible row (finished, no outcome) or a row that is missing or unfinished (which withholds the headline "
        "unless `--allow-partial`). A stop reason of `llm_error` is not an exclusion: such a row is counted by its "
        "outcome. The interval is a percentile bootstrap over instances, because the runs of one instance are not "
        "independent.",
        "",
        "## Rows counted as non-passes, by reason",
        "",
        f"### Missing ({len(report.missing)})",
        "",
    ]
    out += _refs_table(report.missing)
    out += [f"### Harness errors ({len(report.harness_errors)})", ""] + _refs_table(report.harness_errors)
    out += [f"### Inadmissible ({len(report.inadmissible)})", ""] + _refs_table(report.inadmissible)
    edit = report.passed_with_test_edit
    out += [
        f"### passed_with_test_edit ({edit.count}: {edit.approved} approved, {edit.pending} pending)",
        "",
        "Never counted as passed, approved or not.",
        "",
    ] + _refs_table(edit.rows)
    out += [f"### Unfinished ({len(report.unfinished)})", ""] + _refs_table(report.unfinished)
    if report.superseded:
        out += [f"### Superseded by --supersede latest ({len(report.superseded)}), not counted anywhere", ""] + _refs_table(report.superseded)
    if report.unfinished_cost_usd is not None:
        out += [f"Spend on unfinished rows (not in the cost figures below): {_usd(report.unfinished_cost_usd)}.", ""]
    if report.instances_never_admissible:
        out += [f"Instances with no admissible row: {', '.join(report.instances_never_admissible)}.", ""]

    out += ["## By model", "", "Counts. The model is that of each task's first LLM call; a task that fell back is labelled by its first.", "", _GROUP_HEADER]
    out += [_group_row(name, group) for name, group in report.by_model.items()] + [""]
    out += ["## By repository", "", "Counts, not rates: a repository holds a handful of instances.", "", _GROUP_HEADER]
    out += [_group_row(name, group) for name, group in report.by_repo.items()] + [""]

    out += ["## Cost, latency, tokens", "", f"Per model, over its finished tasks (harness errors included: the money was spent). {LATENCY_NOTE}", "", CENSORING_NOTE, ""]
    for block in report.models.values():
        out += _model_block_lines(block)

    out += [
        "",
        "## Targeted pass-to-pass",
        "",
        f"{report.targeted_p2p.rows} finished row(s) over {len(report.targeted_p2p.instances)} instance(s) ran "
        f"pass-to-pass over a targeted subset"
        + (f": {', '.join(report.targeted_p2p.instances)}." if report.targeted_p2p.instances else "."),
        "",
        "## Gold validation",
        "",
    ]
    if report.gold.instances:
        out += [
            f"{report.gold.validated} of {len(report.gold.instances)} gold-validated instance(s) passed "
            f"{GOLD_MIN_RUNS}+ times.",
            "",
            "| instance | status | runs | detail |",
            "|---|---|---|---|",
        ]
        out += [
            f"| {_cell(g.instance_id)} | {g.status} | {_cell(', '.join(g.outcomes))} | {_cell(g.detail)} |"
            for g in report.gold.instances
        ]
        if report.gold.agent_instances_not_validated:
            out += ["", f"Agent instances without validated gold: {', '.join(report.gold.agent_instances_not_validated)}."]
    else:
        out.append("No gold-validation rows supplied.")
    out += ["", "## Caveats", ""] + [f"- {c}" for c in report.caveats] + [""]
    return "\n".join(out)


def export_predictions(rows: Sequence[TaskRow]) -> str:
    """JSONL `{instance_id, model_name_or_path, model_patch}` for the official SWE-bench harness.

    One line per instance, so the caller picks a run first (one `run_index`):
    two lines for one id would make the official harness score whichever it read
    last. Rows without a recorded patch are skipped (NULL means the task never got
    as far as an agent); an empty patch is kept, because "changed nothing" is a
    result. A gold-run row is refused: its patch is the reference fix, and
    exporting it as a model's prediction would be a fabricated result.
    """
    lines = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: (r.instance_id, r.run_index, r.eval_run_id)):
        if is_gold(row):
            raise ValueError(f"{row.eval_run_id}/{row.instance_id}: gold rows hold the reference patch, not a prediction")
        if row.patch_diff is None:
            continue
        if row.instance_id in seen:
            raise ValueError(
                f"more than one row for {row.instance_id}: pick one run_index (the official harness "
                f"takes one prediction per instance)"
            )
        seen.add(row.instance_id)
        lines.append(
            json.dumps(
                {
                    "instance_id": row.instance_id,
                    "model_name_or_path": row.model or "repolace",
                    "model_patch": row.patch_diff,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) + ("\n" if lines else "")


# --- database ----------------------------------------------------------------


async def load_rows(session: AsyncSession, eval_run_ids: Sequence[str]) -> list[TaskRow]:
    """The report's rows: `tasks`, joined with their LLM spend, first model and test runs.

    Raises `ReportError` for an eval run id that matches no task: a mistyped id
    must not produce a short report that looks complete.
    """
    ids = list(dict.fromkeys(eval_run_ids))
    if not ids:
        raise ReportError("no eval run ids given")

    tasks = list(
        (
            await session.execute(
                select(Task)
                .where(Task.eval_run_id.in_(ids))
                .order_by(Task.eval_run_id, Task.instance_id, Task.run_index)
            )
        ).scalars()
    )
    found = {t.eval_run_id for t in tasks}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ReportError(f"no tasks for eval run id(s): {', '.join(missing)}")
    task_ids = [t.id for t in tasks]

    spend = {
        row.task_id: row
        for row in (
            await session.execute(
                select(
                    LLMCall.task_id,
                    func.count().label("calls"),
                    func.count().filter(LLMCall.cost_usd.is_(None)).label("unpriced"),
                    func.sum(LLMCall.cost_usd).label("cost"),
                    func.sum(LLMCall.input_tokens).label("input_tokens"),
                    func.sum(LLMCall.cached_input_tokens).label("cached_input_tokens"),
                    func.sum(LLMCall.output_tokens).label("output_tokens"),
                )
                .where(LLMCall.task_id.in_(task_ids))
                .group_by(LLMCall.task_id)
            )
        )
    }
    first_models = await session.execute(
        select(LLMCall.task_id, LLMCall.model)
        .where(LLMCall.task_id.in_(task_ids))
        .distinct(LLMCall.task_id)
        .order_by(LLMCall.task_id, LLMCall.created_at, LLMCall.id)
    )
    # Not `dict(result)`: a Result has `.keys()`, so dict() would read it as a mapping.
    first_model = {task_id: model for task_id, model in first_models.all()}

    attempts: dict[Any, int] = {}
    last: dict[Any, tuple[int, str | None]] = {}
    for task_id, attempt, error in (
        await session.execute(
            select(TaskTestRun.task_id, TaskTestRun.attempt, TaskTestRun.error).where(TaskTestRun.task_id.in_(task_ids))
        )
    ).tuples():
        if attempt >= 1:
            attempts[task_id] = attempts.get(task_id, 0) + 1
        if task_id not in last or attempt > last[task_id][0]:
            last[task_id] = (attempt, error)

    rows = []
    for task in tasks:
        sums = spend.get(task.id)
        rows.append(
            TaskRow(
                eval_run_id=task.eval_run_id,
                instance_id=task.instance_id,
                run_index=task.run_index,
                status=task.status,
                outcome=task.outcome,
                agent_stop_reason=task.agent_stop_reason,
                score_reason=task.score_reason,
                error_message=task.error_message,
                attempts=attempts.get(task.id, 0),
                last_run_error=last[task.id][1] if task.id in last else None,
                model=first_model.get(task.id),
                llm_calls=sums.calls if sums else 0,
                unpriced_calls=sums.unpriced if sums else 0,
                cost_usd=sums.cost if sums else None,
                input_tokens=int(sums.input_tokens or 0) if sums else 0,
                cached_input_tokens=int(sums.cached_input_tokens or 0) if sums else 0,
                output_tokens=int(sums.output_tokens or 0) if sums else 0,
                started_at=task.started_at,
                completed_at=task.completed_at,
                created_at=task.created_at,
                test_edit_approved=task.test_edit_approved_at is not None,
                patch_diff=task.patch_diff,
            )
        )
    return rows


def attach_instance_data(rows: Sequence[TaskRow], instances: Mapping[str, InstanceSpec]) -> list[TaskRow]:
    """Fill `repo` and `targeted_p2p` from the instance files; unknown ids stay `None`."""
    out = []
    for row in rows:
        spec = instances.get(row.instance_id)
        out.append(row if spec is None else replace(row, repo=spec.repo, targeted_p2p=spec.targeted_p2p))
    return out


def load_manifests(runs_dir: Path, run_ids: Sequence[str], expect: bool | None) -> dict[str, RunManifest]:
    """The manifests of `run_ids` that exist under `runs_dir`.

    `expect` is tri-state: `None` (the default) anchors a run to its manifest when
    one exists and says so when it does not; `True` makes a missing manifest an
    error (the planned grid is required); `False` ignores manifests, so every run
    is UNANCHORED. A manifest that exists but is malformed is always an error --
    skipping it would fall back to the observed grid without a word.
    """
    if expect is False:
        return {}
    manifests: dict[str, RunManifest] = {}
    for run_id in dict.fromkeys(run_ids):
        try:
            path = manifest_path(runs_dir, run_id)
            if not path.is_file():
                if expect:
                    raise ReportError(f"no manifest for {run_id} at {path}: the planned grid is unknown")
                continue
            manifests[run_id] = load_manifest(path)
        except ManifestError as exc:
            raise ReportError(str(exc)) from exc
    return manifests


async def build_report(
    session: AsyncSession,
    agent_runs: Sequence[str],
    gold_runs: Sequence[str] = (),
    instances: Mapping[str, InstanceSpec] | None = None,
    *,
    manifests: Mapping[str, RunManifest] | None = None,
    allow_partial: bool = False,
    supersede: str = "none",
) -> tuple[Report, list[TaskRow]]:
    """Load, join with instance data, aggregate. Returns the rows too, for the predictions export."""
    rows = await load_rows(session, [*agent_runs, *gold_runs])
    wrong = [r.eval_run_id for r in rows if r.eval_run_id in agent_runs and is_gold(r)]
    if wrong:
        raise ReportError(f"run id(s) {', '.join(sorted(set(wrong)))} start with {GOLD_RUN_PREFIX!r}: pass them with --gold-run")
    misfiled = [r.eval_run_id for r in rows if r.eval_run_id in gold_runs and not is_gold(r)]
    if misfiled:
        raise ReportError(
            f"--gold-run id(s) {', '.join(sorted(set(misfiled)))} do not start with {GOLD_RUN_PREFIX!r}, "
            f"so they would be counted as agent runs"
        )
    rows, superseded = resolve_duplicates(rows, supersede)
    instance_repos: dict[str, str] = {}
    if instances is not None:
        rows = attach_instance_data(rows, instances)
        instance_repos = {instance_id: spec.repo for instance_id, spec in instances.items()}
    report = aggregate(
        rows, manifests=manifests, allow_partial=allow_partial, instance_repos=instance_repos, superseded=superseded,
    )
    return report, rows


# --- command line ------------------------------------------------------------


def _default_instances_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "instances"


def _default_runs_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "runs"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repolace-eval report", description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", action="append", default=[], metavar="EVAL_RUN_ID", help="an agent eval run (repeatable)")
    parser.add_argument("--gold-run", action="append", default=[], metavar="EVAL_RUN_ID", help="a gold-validation run (repeatable; id starts 'gold-')")
    parser.add_argument("--instances-dir", type=Path, default=None, help="instance files, for repo and targeted-P2P data")
    parser.add_argument("--runs-dir", type=Path, default=None, help="where eval/runs/<id>/manifest.json live")
    parser.add_argument(
        "--expect", action=argparse.BooleanOptionalAction, default=None,
        help="anchor each run to its manifest's planned grid (default: when a manifest exists); "
             "--expect requires one, --no-expect ignores them",
    )
    parser.add_argument("--allow-partial", action="store_true", help="print a headline for an incomplete grid, marked PARTIAL")
    parser.add_argument(
        "--supersede", choices=SUPERSEDE_MODES, default="none",
        help="duplicate (instance, run_index) rows: refuse (none, the default) or keep the newest and print what is dropped (latest)",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, default=None, help="write here instead of stdout")
    parser.add_argument("--predictions", type=Path, default=None, help="also write the SWE-bench predictions JSONL here")
    parser.add_argument("--predictions-run-index", type=int, default=None, help="export only this run_index (needed when runs repeat)")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    from repolace_shared.config import SharedSettings
    from repolace_shared.db.session import create_engine, create_session_factory

    instances: dict[str, InstanceSpec] | None = None
    directory = args.instances_dir or _default_instances_dir()
    if directory.is_dir():
        instances = load_instances(directory)
    elif args.instances_dir is not None:
        print(f"repolace-eval report: {directory} is not a directory", file=sys.stderr)
        return 2

    runs_dir = args.runs_dir or _default_runs_dir()
    manifests = load_manifests(runs_dir, args.run, args.expect)

    try:
        database_url = SharedSettings().database_url
    except Exception:  # noqa: BLE001 -- a settings error can echo the environment; say only what is missing
        print("repolace-eval report: DATABASE_URL is not configured", file=sys.stderr)
        return 1
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as session:
            report, rows = await build_report(
                session, args.run, args.gold_run, instances, manifests=manifests, allow_partial=args.allow_partial,
                supersede=args.supersede,
            )
    finally:
        await engine.dispose()

    if instances is None:
        report = replace(
            report,
            warnings=(*report.warnings, f"No instance data found at {directory}: the by-repository and targeted-P2P sections are unknown."),
        )
    text = to_json(report) if args.format == "json" else to_markdown(report)
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    if args.predictions is not None:
        chosen = [r for r in rows if not is_gold(r)]
        if args.predictions_run_index is not None:
            chosen = [r for r in chosen if r.run_index == args.predictions_run_index]
        args.predictions.write_text(export_predictions(chosen), encoding="utf-8")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.run:
        print("repolace-eval report: at least one --run is required", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_amain(args))
    except (ReportError, InstanceError, ValueError) as exc:
        print(f"repolace-eval report: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
