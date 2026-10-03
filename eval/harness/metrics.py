"""Retrieval metrics, pure.

What a number here means, because three of them are easy to read wrong:

**An empty gold set has no recall.** It returns `None`, not `0.0` and not `1.0`,
and every aggregate skips `None` while reporting how many it skipped. A query with
nothing to find is not a retrieval failure, and scoring it as one (or as a free
hit) would move the mean for reasons that have nothing to do with retrieval.

**MRR is over the retrieved list only.** A relevant item beyond the cut-off
scores `0.0`, not `1/(cut-off+1)`: the rank is unknown, and inventing one would
credit retrieval for results it never returned.

**File-level ranks are over distinct files.** Retrieval returns chunks, several
per file; counting the same file twice would let one noisy file fill the top-k
and make "k files" mean fewer files. A file is ranked at its first appearance.

**Chunk-level recall is over gold *hunks*, not chunks.** A gold hunk is recalled
if one of the top-k chunks is among the hunk's *innermost* overlapping chunks.
Counting chunks instead would let a class skeleton and its method, which overlap
by construction, each count as a separate find.

**Why innermost.** A chunk's span is not its content. A class skeleton's span is the
whole class and a module chunk's span runs from the first to the last top-level
statement, so a gold hunk inside a function lies inside the module chunk's span
though the module chunk does not contain its text (it holds the imports and
constants). Crediting any overlapping chunk let a retrieved module chunk "recall" a
hunk in `helper()`: a 27-line file with three lines of module content scored chunk
R@5 = 1.0 for a hunk it never showed. So, for each hunk, any overlapping chunk whose
span STRICTLY contains another overlapping chunk's span is discarded, and what is
left is credited. That needs the file's chunks, not only the retrieved ones (the
module chunk that was retrieved has to be compared with the function chunk that was
not), so the scoring functions take the indexed `corpus`. Without one they fall back
to the retrieved chunks alone, which is weaker and says so. A hunk that no indexed
chunk overlaps is unreachable: it is dropped from the score and counted.
"""

from __future__ import annotations

import math
import random
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from math import comb

#: The cut-offs the retrieval eval reports.
DEFAULT_KS = (5, 10, 20)


@dataclass(frozen=True)
class Span:
    """An inclusive, 1-based line range in one file: a chunk, or a gold hunk."""

    path: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 1 or self.end < self.start:
            raise ValueError(f"bad span {self.path}:{self.start}-{self.end}: need 1 <= start <= end")


def spans_overlap(a: Span, b: Span) -> bool:
    """Same file and the inclusive ranges intersect. Touching at one line counts."""
    return a.path == b.path and a.start <= b.end and b.start <= a.end


def _check_k(k: int) -> None:
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")


def distinct_in_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def file_recall_at_k(ranked_paths: Sequence[str], gold_paths: Collection[str], k: int) -> float | None:
    """Fraction of gold files among the first `k` distinct ranked files. `None` if no gold."""
    _check_k(k)
    gold = set(gold_paths)
    if not gold:
        return None
    top = set(distinct_in_order(ranked_paths)[:k])
    return len(gold & top) / len(gold)


def file_reciprocal_rank(ranked_paths: Sequence[str], gold_paths: Collection[str]) -> float | None:
    """1 / rank of the first gold file (over distinct files); 0.0 if none retrieved; `None` if no gold."""
    gold = set(gold_paths)
    if not gold:
        return None
    for rank, path in enumerate(distinct_in_order(ranked_paths), start=1):
        if path in gold:
            return 1.0 / rank
    return 0.0


def strictly_contains(outer: Span, inner: Span) -> bool:
    """`outer` covers all of `inner` and is not the same span."""
    return (
        outer.path == inner.path
        and outer.start <= inner.start
        and inner.end <= outer.end
        and (outer.start, outer.end) != (inner.start, inner.end)
    )


def innermost_overlapping(hunk: Span, pool: Iterable[Span]) -> frozenset[Span]:
    """The chunks of `pool` that overlap `hunk` and contain no other overlapping chunk's span.

    Equal spans are not "strictly" containing each other, so both are kept.
    """
    overlapping = [chunk for chunk in set(pool) if spans_overlap(chunk, hunk)]
    return frozenset(
        chunk for chunk in overlapping
        if not any(strictly_contains(chunk, other) for other in overlapping)
    )


def reachable_hunks(gold_hunks: Sequence[Span], corpus: Sequence[Span]) -> tuple[list[Span], int]:
    """The hunks some indexed chunk overlaps, and how many none did.

    A hunk on a line no chunk covers (a comment between functions outside every span, a
    file the indexer skipped) cannot be retrieved by any query, so charging it as a miss
    would lower recall for a reason that is not retrieval.
    """
    kept = [hunk for hunk in gold_hunks if innermost_overlapping(hunk, corpus)]
    return kept, len(gold_hunks) - len(kept)


def hunks_recalled(
    ranked_chunks: Sequence[Span], gold_hunks: Sequence[Span], k: int, corpus: Sequence[Span] | None = None,
) -> int:
    """How many gold hunks have one of their innermost overlapping chunks in the first `k`.

    `corpus` is the file's indexed chunks. Without it the innermost rule can only compare
    the retrieved chunks with each other, so a wide chunk retrieved alone is still credited.
    """
    _check_k(k)
    pool = ranked_chunks if corpus is None else corpus
    top = set(ranked_chunks[:k])
    return sum(1 for hunk in gold_hunks if top & innermost_overlapping(hunk, pool))


def chunk_recall_at_k(
    ranked_chunks: Sequence[Span], gold_hunks: Sequence[Span], k: int, corpus: Sequence[Span] | None = None,
) -> float | None:
    """Fraction of gold hunks recalled by the first `k` chunks. `None` if there are no hunks."""
    _check_k(k)
    if not gold_hunks:
        return None
    return hunks_recalled(ranked_chunks, gold_hunks, k, corpus) / len(gold_hunks)


def chunk_reciprocal_rank(
    ranked_chunks: Sequence[Span], gold_hunks: Sequence[Span], corpus: Sequence[Span] | None = None,
) -> float | None:
    """1 / rank of the first chunk that is an innermost overlap of any gold hunk; 0.0 if none; `None` if no hunks."""
    if not gold_hunks:
        return None
    pool = ranked_chunks if corpus is None else corpus
    credited: set[Span] = set()
    for hunk in gold_hunks:
        credited |= innermost_overlapping(hunk, pool)
    for rank, chunk in enumerate(ranked_chunks, start=1):
        if chunk in credited:
            return 1.0 / rank
    return 0.0


@dataclass(frozen=True)
class QueryMetrics:
    """Everything measured for one query. `None` means "no gold to measure against"."""

    file_recall: dict[int, float | None]
    chunk_recall: dict[int, float | None]
    file_rr: float | None
    chunk_rr: float | None
    #: Gold hunks that no indexed chunk overlapped, dropped from the chunk metrics.
    unreachable_hunks: int = 0


def score_query(
    ranked_chunks: Sequence[Span],
    gold_paths: Collection[str],
    gold_hunks: Sequence[Span],
    ks: Sequence[int] = DEFAULT_KS,
    corpus: Sequence[Span] | None = None,
) -> QueryMetrics:
    """All metrics for one query from its ranked chunks, in rank order.

    `corpus` is the indexed chunks of the gold files. Pass it: it is what makes the
    chunk metrics innermost-aware and lets unreachable hunks be dropped and counted
    (see the module docstring). Without it the chunk metrics are the weaker form.
    """
    ranked_paths = [chunk.path for chunk in ranked_chunks]
    unreachable = 0
    if corpus is not None:
        gold_hunks, unreachable = reachable_hunks(gold_hunks, corpus)
    return QueryMetrics(
        file_recall={k: file_recall_at_k(ranked_paths, gold_paths, k) for k in ks},
        chunk_recall={k: chunk_recall_at_k(ranked_chunks, gold_hunks, k, corpus) for k in ks},
        file_rr=file_reciprocal_rank(ranked_paths, gold_paths),
        chunk_rr=chunk_reciprocal_rank(ranked_chunks, gold_hunks, corpus),
        unreachable_hunks=unreachable,
    )


@dataclass(frozen=True)
class MeanOf:
    """A mean over the scored values, with how many were scored and how many skipped."""

    mean: float | None
    scored: int
    skipped: int


def mean_of(values: Iterable[float | None]) -> MeanOf:
    """Mean of the non-`None` values. `mean` is `None` when nothing was scored."""
    present: list[float] = []
    skipped = 0
    for value in values:
        if value is None:
            skipped += 1
        else:
            present.append(value)
    return MeanOf(sum(present) / len(present) if present else None, len(present), skipped)


@dataclass(frozen=True)
class AggregateMetrics:
    queries: int
    file_recall: dict[int, MeanOf]
    chunk_recall: dict[int, MeanOf]
    file_mrr: MeanOf
    chunk_mrr: MeanOf


def aggregate_metrics(results: Sequence[QueryMetrics], ks: Sequence[int] = DEFAULT_KS) -> AggregateMetrics:
    """Mean recall@k and MRR over queries, each skipping the queries that had no gold."""
    return AggregateMetrics(
        queries=len(results),
        file_recall={k: mean_of(r.file_recall[k] for r in results) for k in ks},
        chunk_recall={k: mean_of(r.chunk_recall[k] for r in results) for k in ks},
        file_mrr=mean_of(r.file_rr for r in results),
        chunk_mrr=mean_of(r.chunk_rr for r in results),
    )


# --- uncertainty -------------------------------------------------------------

#: Fixed and documented so a report can be regenerated to the digit. A seed is a
#: degree of freedom only if it is chosen after seeing the data; this one is not.
BOOTSTRAP_SEED = 0
BOOTSTRAP_RESAMPLES = 10_000
CONFIDENCE = 0.95


def percentile(values: Sequence[float], q: float) -> float | None:
    """The `q`th percentile by linear interpolation between closest ranks.

    Rank `q/100 * (n - 1)` over the sorted values (the numpy default), so one
    value is its own percentile at any `q`, and p95 of two values sits 95% of the
    way from the smaller to the larger.
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


def cluster_bootstrap_interval(
    numerators: Sequence[float],
    denominators: Sequence[float],
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = CONFIDENCE,
) -> tuple[float, float] | None:
    """A percentile bootstrap interval for `sum(numerators) / sum(denominators)`, resampling CLUSTERS.

    A cluster is one instance: its `numerator` is its passes and its `denominator`
    its runs. Resampling instances, not pooled rows, is the point: the runs of one
    instance are not independent (an instance a model can fix, it mostly fixes every
    time), so an interval over the pooled rows treats three runs of twenty
    instances as sixty independent trials and is far too narrow. With equal
    denominators the statistic is the mean of the per-instance rates; with unequal
    ones it is the pooled ratio, which is what the headline reports.

    `None` below two clusters, where a resample can only reproduce the data.
    Deterministic for a given seed. The statistic of a resample whose denominators
    sum to zero is undefined, so denominators must be positive.
    """
    if len(numerators) != len(denominators):
        raise ValueError(f"{len(numerators)} numerators but {len(denominators)} denominators")
    if any(d <= 0 for d in denominators):
        raise ValueError("every cluster needs a positive denominator")
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be between 0 and 1, got {confidence}")
    if resamples < 1:
        raise ValueError(f"resamples must be at least 1, got {resamples}")
    n = len(numerators)
    if n < 2:
        return None
    rng = random.Random(seed)
    statistics_: list[float] = []
    for _ in range(resamples):
        picks = rng.choices(range(n), k=n)
        statistics_.append(sum(numerators[i] for i in picks) / sum(denominators[i] for i in picks))
    tail = (1 - confidence) / 2 * 100
    low, high = percentile(statistics_, tail), percentile(statistics_, 100 - tail)
    assert low is not None and high is not None
    return low, high


def exact_sign_test(b: int, c: int) -> float | None:
    """Two-sided exact p-value for `b` against `c` discordant pairs (McNemar's exact test).

    Under the null a discordant pair is equally likely to go either way, so the
    smaller count is binomial(b + c, 1/2). `None` with no discordant pair, where
    there is nothing to test. Used for two models scored on the same instances:
    8:0 gives p = 0.008, 7:1 p = 0.07, 6:2 p = 0.29 -- at 20 instances a 20 to 30
    point gap between two models is not statistically distinguishable.
    """
    if b < 0 or c < 0:
        raise ValueError(f"counts must not be negative, got {b} and {c}")
    n = b + c
    if n == 0:
        return None
    tail = sum(comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2**n)
