"""The benchmark report: DB rows in, the headline number and everything it excludes out.

Reads the database only, so a finished sweep can be rescored or re-sliced without
running anything. `aggregate` is pure; `load_rows` is the one place that touches
SQL. **The accounting below is the claim**, so it is written down once, here, and
the Markdown repeats it next to the number.

**Every finished agent row lands in exactly one bucket**, decided by `classify` in
this order (first match wins):

1. `unfinished` -- status `queued`/`running`. In no rate, and flagged: a half-done
   sweep must not read as a row of failures, nor as nothing at all.
2. `harness_error` -- status `failed` (repolace itself broke), and only that. A
   stop reason of `llm_error` is **not** a harness error: the agent loop reports it
   for non-transient model errors too (a context-window overflow, an unparseable
   replayed tool call), which depend on how hard the instance is and how large the
   model's context is, and the scorer already gives such a task an outcome ("no
   scored attempt" is FAILED). Such a row is counted by that outcome, and shown in
   its own `llm_error` column so the reader can see how many there were.
3. `inadmissible` -- finished, no harness error, `outcome` NULL: the instrument
   could not score it (`score()` returned inadmissible). Excluded, listed with the
   reason, never a failure.
4. `passed_with_test_edit` -- scored, but the diff touched tests. **Never counted
   as passed.** It stays in the *denominator* of the headline, as a non-pass: it is
   an agent result, and dropping it from the denominator would remove exactly the
   instances where the agent leaned on the loophole, flattering the rate.
5. `passed` / `failed`.

**Two rates, both stated, each the mean over runs with the min-max spread over
`run_index`:**

* `passed / admissible`, `admissible = passed + failed + passed_with_test_edit`:
  the headline. Excludes harness errors and inadmissible rows.
* `passed / total`, `total = admissible + inadmissible + harness_error` (every
  finished row): the pessimistic bound, in which nothing is excluded. If the two
  differ a lot, the exclusions are doing the work, and that is visible.

Per-run rates are computed first (one per `run_index`, over that run's rows) and
then averaged, so the spread is run-to-run noise at the instance set's size. It is
**not** a confidence interval: with N instances one instance moves the rate by 1/N,
and that sampling uncertainty is not in the spread.

Gold-validation rows (`eval_run_id` starting `gold-`) are reported separately and
are in none of the above.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    "With N instances one instance moves a rate by 1/N. The min-max spread is run-to-run variation "
    "only; it does not capture the uncertainty from having sampled N instances."
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


def percentile(values: Sequence[float], q: float) -> float | None:
    """The `q`th percentile by linear interpolation between closest ranks.

    Rank `q/100 * (n - 1)` over the sorted values (the numpy default), so one
    value is its own percentile at any `q`, and p95 of two values sits 95% of the
    way from the smaller to the larger. On the sample sizes here (tens of tasks) a
    p95 is within a rank or two of the maximum; `Stats.n` is reported beside it so
    nobody reads it as a tail estimate.
    """
    if not 0 <= q <= 100:
        raise ValueError(f"q must be between 0 and 100, got {q}")
    if not values:
        return None
    ordered = sorted(values)
    position = q / 100 * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True)
class Stats:
    n: int
    mean: float | None
    median: float | None
    p95: float | None
    minimum: float | None
    maximum: float | None
    total: float | None


def describe(values: Sequence[float]) -> Stats:
    if not values:
        return Stats(0, None, None, None, None, None, None)
    return Stats(
        n=len(values),
        mean=sum(values) / len(values),
        median=statistics.median(values),
        p95=percentile(values, 95),
        minimum=min(values),
        maximum=max(values),
        total=sum(values),
    )


# --- rates -------------------------------------------------------------------


@dataclass(frozen=True)
class RunRate:
    run_index: int
    numerator: int
    denominator: int
    #: None when the run had no rows in the denominator.
    rate: float | None


@dataclass(frozen=True)
class RateSummary:
    #: Pooled over runs, for the reader checking the arithmetic; the headline is `mean`.
    numerator: int
    denominator: int
    #: Mean of the per-run rates (runs with an empty denominator are left out).
    mean: float | None
    minimum: float | None
    maximum: float | None
    runs: tuple[RunRate, ...]


@dataclass(frozen=True)
class GroupSummary:
    rows: int
    instances: int
    run_indexes: tuple[int, ...]
    #: "1 run" / "3 runs": a single run has no spread and must say so.
    runs_label: str
    counts: dict[str, int]
    #: Finished rows whose stop reason is `llm_error`. Already in `counts` under
    #: whatever bucket their outcome puts them in; shown apart so nobody has to
    #: wonder how many of the failures were the provider's.
    llm_errors: int
    passed_over_admissible: RateSummary
    passed_over_total: RateSummary


def _runs_label(n: int) -> str:
    return f"{n} run" if n == 1 else f"{n} runs"


def _rate_summary(per_run: Mapping[int, Counter[str]], numerator: Iterable[str], denominator: Iterable[str]) -> RateSummary:
    numerator = tuple(numerator)
    denominator = tuple(denominator)
    runs = []
    for run_index in sorted(per_run):
        counts = per_run[run_index]
        top = sum(counts[b] for b in numerator)
        bottom = sum(counts[b] for b in denominator)
        runs.append(RunRate(run_index, top, bottom, top / bottom if bottom else None))
    rates = [r.rate for r in runs if r.rate is not None]
    return RateSummary(
        numerator=sum(r.numerator for r in runs),
        denominator=sum(r.denominator for r in runs),
        mean=sum(rates) / len(rates) if rates else None,
        minimum=min(rates) if rates else None,
        maximum=max(rates) if rates else None,
        runs=tuple(runs),
    )


def summarize(rows: Sequence[TaskRow]) -> GroupSummary:
    """Counts and both rates for one set of agent rows."""
    per_run: dict[int, Counter[str]] = {}
    totals: Counter[str] = Counter()
    for row in rows:
        bucket = classify(row)
        per_run.setdefault(row.run_index, Counter())[bucket] += 1
        totals[bucket] += 1
    finished = (PASSED, FAILED, PASSED_WITH_TEST_EDIT, INADMISSIBLE, HARNESS_ERROR)
    admissible = (PASSED, FAILED, PASSED_WITH_TEST_EDIT)
    # Only runs that have a finished row: a run index holding nothing but
    # unfinished rows would otherwise appear as a run with an undefined rate.
    per_run = {i: c for i, c in per_run.items() if any(c[b] for b in finished)}
    return GroupSummary(
        rows=len(rows),
        instances=len({row.instance_id for row in rows}),
        run_indexes=tuple(sorted(per_run)),
        runs_label=_runs_label(len(per_run)),
        counts={bucket: totals[bucket] for bucket in _BUCKETS},
        llm_errors=sum(1 for row in rows if row.agent_stop_reason == LLM_ERROR and classify(row) != UNFINISHED),
        passed_over_admissible=_rate_summary(per_run, (PASSED,), admissible),
        passed_over_total=_rate_summary(per_run, (PASSED,), finished),
    )


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
class Report:
    agent_runs: tuple[str, ...]
    gold_runs: tuple[str, ...]
    overall: GroupSummary
    #: More than one model among the rows: `overall` pools them and is not a
    #: statement about any one of them.
    mixed_models: bool
    by_model: dict[str, GroupSummary]
    by_repo: dict[str, GroupSummary]
    harness_errors: tuple[RowRef, ...]
    inadmissible: tuple[RowRef, ...]
    passed_with_test_edit: PassedWithTestEditBucket
    unfinished: tuple[RowRef, ...]
    unfinished_cost_usd: float | None
    #: Instances with finished rows and not one admissible row.
    instances_never_admissible: tuple[str, ...]
    cost_usd: Stats
    cost_rows_without_data: int
    latency_seconds: Stats
    latency_rows_without_data: int
    tokens: TokenTotals
    attempts: dict[int, int]
    stop_reasons: dict[str, int]
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


def aggregate(rows: Sequence[TaskRow]) -> Report:
    """Everything the report says, from rows alone. Pure and deterministic."""
    gold_rows = [r for r in rows if is_gold(r)]
    agent_rows = [r for r in rows if not is_gold(r)]
    by_bucket: dict[str, list[TaskRow]] = {b: [] for b in _BUCKETS}
    for row in agent_rows:
        by_bucket[classify(row)].append(row)
    finished = [row for bucket, members in by_bucket.items() if bucket != UNFINISHED for row in members]

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

    costs = [float(r.cost_usd) for r in finished if r.cost_usd is not None]
    latencies = [v for v in (_latency(r) for r in finished) if v is not None]
    input_tokens = sum(r.input_tokens for r in finished)
    cached = sum(r.cached_input_tokens for r in finished)
    tokens = TokenTotals(
        calls=sum(r.llm_calls for r in finished),
        unpriced_calls=sum(r.unpriced_calls for r in finished),
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=sum(r.output_tokens for r in finished),
        cache_read_ratio=cached / input_tokens if input_tokens else None,
    )

    unfinished_costs = [float(r.cost_usd) for r in by_bucket[UNFINISHED] if r.cost_usd is not None]
    approved = sum(1 for r in by_bucket[PASSED_WITH_TEST_EDIT] if r.test_edit_approved)

    targeted = [r for r in finished if r.targeted_p2p is True]
    targeted_p2p = TargetedP2P(
        rows=len(targeted),
        instances=tuple(sorted({r.instance_id for r in targeted})),
        unknown_rows=sum(1 for r in finished if r.targeted_p2p is None),
    )

    gold = _gold_summary(gold_rows, {r.instance_id for r in agent_rows})

    overall = summarize(agent_rows)
    warnings: list[str] = []
    if by_bucket[UNFINISHED]:
        warnings.append(
            f"PARTIAL: {len(by_bucket[UNFINISHED])} row(s) are still queued or running; the rates cover "
            f"finished rows only."
        )
    if by_bucket[HARNESS_ERROR]:
        warnings.append(
            f"{len(by_bucket[HARNESS_ERROR])} harness error row(s) are excluded from passed/admissible "
            f"(an instrument failure is not an agent failure). Re-run them: the headline is inflated "
            f"if they would have failed. passed/total keeps them in the denominator."
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
            f"{len(by_bucket[INADMISSIBLE])} inadmissible row(s) (the instrument could not score them) "
            f"are excluded from passed/admissible."
        )
    if never_admissible:
        warnings.append(
            f"{len(never_admissible)} instance(s) have no admissible row at all and contribute nothing "
            f"to passed/admissible: {', '.join(never_admissible)}."
        )
    if len(real_models) > 1:
        warnings.append(
            f"MIXED MODELS ({', '.join(real_models)}): the overall figure pools them. Read the per-model "
            f"table; the pooled number is not a statement about any one model."
        )
    seen: Counter[tuple[str | None, str, int]] = Counter((r.model, r.instance_id, r.run_index) for r in agent_rows)
    duplicates = sorted(f"{i}#{n}" for (_, i, n), count in seen.items() if count > 1)
    if duplicates:
        warnings.append(
            f"{len(duplicates)} (instance, run_index) pair(s) appear more than once for one model, across "
            f"eval runs: {', '.join(duplicates[:5])}. Per-run rates merge them."
        )
    if tokens.unpriced_calls:
        warnings.append(
            f"{tokens.unpriced_calls} LLM call(s) carry no cost; the cost figures understate spend."
        )
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
        agent_runs=tuple(sorted({r.eval_run_id for r in agent_rows})),
        gold_runs=tuple(sorted({r.eval_run_id for r in gold_rows})),
        overall=overall,
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
        unfinished_cost_usd=sum(unfinished_costs) if unfinished_costs else None,
        instances_never_admissible=tuple(never_admissible),
        cost_usd=describe(costs),
        cost_rows_without_data=len(finished) - len(costs),
        latency_seconds=describe(latencies),
        latency_rows_without_data=len(finished) - len(latencies),
        tokens=tokens,
        attempts=dict(sorted(Counter(r.attempts for r in finished).items())),
        stop_reasons=dict(sorted(Counter(r.agent_stop_reason or "(none)" for r in finished).items())),
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


def _rate_cell(rate: RateSummary) -> str:
    defined = [r for r in rate.runs if r.rate is not None]
    if rate.mean is None:
        return "n/a (no run has a denominator)"
    label = _runs_label(len(defined))
    if len(defined) == 1:
        return f"{_pct(rate.mean)} ({label}, no spread)"
    return f"{_pct(rate.mean)} (min {_pct(rate.minimum)}, max {_pct(rate.maximum)}; {label})"


def _group_row(name: str, group: GroupSummary) -> str:
    admissible = group.passed_over_admissible
    total = group.passed_over_total
    return (
        f"| {_cell(name)} | {group.instances} | {group.runs_label} | {group.counts[PASSED]} | "
        f"{group.counts[FAILED]} | {group.counts[PASSED_WITH_TEST_EDIT]} | {group.counts[INADMISSIBLE]} | "
        f"{group.counts[HARNESS_ERROR]} | {group.llm_errors} | {_pct(admissible.mean)} ({admissible.numerator}/{admissible.denominator}) | "
        f"{_pct(total.mean)} ({total.numerator}/{total.denominator}) |"
    )


_GROUP_HEADER = (
    "| group | instances | runs | passed | failed | passed_with_test_edit | inadmissible | harness error "
    "| of which llm_error (counted by outcome) | passed / admissible | passed / total |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)


def _refs_table(refs: Sequence[RowRef]) -> list[str]:
    if not refs:
        return ["_none_", ""]
    lines = ["| instance | run | eval run | detail |", "|---|---|---|---|"]
    lines += [f"| {_cell(r.instance_id)} | {r.run_index} | {_cell(r.eval_run_id)} | {_cell(r.detail)} |" for r in refs]
    return lines + [""]


def _stats_line(name: str, stats: Stats, fmt) -> str:
    return (
        f"| {name} | {stats.n} | {fmt(stats.mean)} | {fmt(stats.median)} | {fmt(stats.p95)} | "
        f"{fmt(stats.minimum)} | {fmt(stats.maximum)} | {fmt(stats.total)} |"
    )


def to_markdown(report: Report) -> str:
    overall = report.overall
    out: list[str] = ["# Benchmark report", ""]
    out.append(f"Agent runs: {', '.join(report.agent_runs) or '(none)'}. Gold runs: {', '.join(report.gold_runs) or '(none)'}.")
    out.append("")
    if report.warnings:
        out += ["## Read this first", ""] + [f"- {w}" for w in report.warnings] + [""]

    out += ["## Headline", ""]
    out.append(
        f"**N = {overall.instances} instances**, {overall.runs_label}, {overall.rows} rows"
        + (" (pooled across models)." if report.mixed_models else ".")
    )
    out += ["", "| rate | value | pooled counts |", "|---|---|---|"]
    admissible, total = overall.passed_over_admissible, overall.passed_over_total
    out.append(f"| passed / admissible | {_rate_cell(admissible)} | {admissible.numerator}/{admissible.denominator} |")
    out.append(f"| passed / total | {_rate_cell(total)} | {total.numerator}/{total.denominator} |")
    out += ["", "Per run:", "", "| run_index | passed | admissible | passed / admissible | total | passed / total |", "|---|---|---|---|---|---|"]
    totals_by_run = {r.run_index: r for r in total.runs}
    for run in admissible.runs:
        t = totals_by_run[run.run_index]
        out.append(f"| {run.run_index} | {run.numerator} | {run.denominator} | {_pct(run.rate)} | {t.denominator} | {_pct(t.rate)} |")
    out += [
        "",
        "How rows are counted. `admissible = passed + failed + passed_with_test_edit`; "
        "`total = admissible + inadmissible + harness error` (every finished row). "
        "A harness error is status `failed` only: an instrument failure, excluded "
        "from passed/admissible, kept in passed/total. A stop reason of `llm_error` is not one: such a row "
        "is counted by its outcome (see the llm_error column). An inadmissible row is finished with no outcome "
        "(the instrument could not score it): the same. `passed_with_test_edit` is never a pass and "
        "stays in both denominators as a non-pass. Rates are the mean of per-run rates; the bracket is "
        "the min-max over `run_index`, not a confidence interval.",
        "",
        "## Excluded from the headline",
        "",
        f"### Harness errors ({len(report.harness_errors)})",
        "",
    ]
    out += _refs_table(report.harness_errors)
    out += [f"### Inadmissible ({len(report.inadmissible)})", ""] + _refs_table(report.inadmissible)
    edit = report.passed_with_test_edit
    out += [
        f"### passed_with_test_edit ({edit.count}: {edit.approved} approved, {edit.pending} pending)",
        "",
        "Never counted as passed, approved or not.",
        "",
    ] + _refs_table(edit.rows)
    out += [f"### Unfinished ({len(report.unfinished)})", ""] + _refs_table(report.unfinished)
    if report.unfinished_cost_usd is not None:
        out += [f"Spend on unfinished rows (not in the cost figures below): {_usd(report.unfinished_cost_usd)}.", ""]
    if report.instances_never_admissible:
        out += [f"Instances with no admissible row: {', '.join(report.instances_never_admissible)}.", ""]

    out += ["## By model", "", "First model of each task's first LLM call; a task that fell back is labelled by its first.", "", _GROUP_HEADER]
    out += [_group_row(name, group) for name, group in report.by_model.items()] + [""]
    out += ["## By repository", "", _GROUP_HEADER]
    out += [_group_row(name, group) for name, group in report.by_repo.items()] + [""]

    out += [
        "## Cost, latency, tokens",
        "",
        "Per finished task (harness errors included: the money was spent). p95 on a small n is within a rank or two of the maximum.",
        "",
        "| measure | n | mean | median | p95 | min | max | total |",
        "|---|---|---|---|---|---|---|---|",
        _stats_line("cost (USD)", report.cost_usd, _usd),
        _stats_line("latency (s)", report.latency_seconds, _num),
        "",
        f"Rows without cost data: {report.cost_rows_without_data}. Rows without both timestamps: {report.latency_rows_without_data}.",
        "",
        f"LLM calls: {report.tokens.calls} ({report.tokens.unpriced_calls} unpriced). Input tokens: "
        f"{report.tokens.input_tokens:,} (of which cache reads {report.tokens.cached_input_tokens:,}, "
        f"{_pct(report.tokens.cache_read_ratio)}). Output tokens: {report.tokens.output_tokens:,}.",
        "",
        "## Attempts and stop reasons",
        "",
        "| attempts | tasks |",
        "|---|---|",
    ]
    out += [f"| {attempts} | {count} |" for attempts, count in report.attempts.items()]
    out += ["", "| agent stop reason | tasks |", "|---|---|"]
    out += [f"| {_cell(reason)} | {count} |" for reason, count in report.stop_reasons.items()]

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


async def build_report(
    session: AsyncSession,
    agent_runs: Sequence[str],
    gold_runs: Sequence[str] = (),
    instances: Mapping[str, InstanceSpec] | None = None,
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
    if instances is not None:
        rows = attach_instance_data(rows, instances)
    return aggregate(rows), rows


# --- command line ------------------------------------------------------------


def _default_instances_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "instances"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repolace-eval report", description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", action="append", default=[], metavar="EVAL_RUN_ID", help="an agent eval run (repeatable)")
    parser.add_argument("--gold-run", action="append", default=[], metavar="EVAL_RUN_ID", help="a gold-validation run (repeatable; id starts 'gold-')")
    parser.add_argument("--instances-dir", type=Path, default=None, help="instance files, for repo and targeted-P2P data")
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

    try:
        database_url = SharedSettings().database_url
    except Exception:  # noqa: BLE001 -- a settings error can echo the environment; say only what is missing
        print("repolace-eval report: DATABASE_URL is not configured", file=sys.stderr)
        return 1
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as session:
            report, rows = await build_report(session, args.run, args.gold_run, instances)
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
