"""`harness.metrics`: recall@k, MRR and hunk-to-chunk overlap, pure arithmetic."""

import random

import pytest

from harness.metrics import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    Span,
    aggregate_metrics,
    chunk_recall_at_k,
    chunk_reciprocal_rank,
    cluster_bootstrap_interval,
    exact_sign_test,
    file_recall_at_k,
    file_reciprocal_rank,
    innermost_overlapping,
    mean_of,
    percentile,
    score_query,
    reachable_hunks,
    spans_overlap,
    strictly_contains,
)


def span(path: str, start: int, end: int) -> Span:
    return Span(path, start, end)


class TestFileRecall:
    def test_fraction_of_gold_files_in_the_top_k(self):
        ranked = ["a.py", "b.py", "c.py", "d.py"]
        assert file_recall_at_k(ranked, {"a.py", "d.py"}, 3) == 0.5
        assert file_recall_at_k(ranked, {"a.py", "d.py"}, 4) == 1.0

    def test_k_larger_than_the_list_is_the_whole_list(self):
        assert file_recall_at_k(["a.py"], {"a.py", "z.py"}, 20) == 0.5

    def test_an_empty_ranking_recalls_nothing(self):
        assert file_recall_at_k([], {"a.py"}, 5) == 0.0

    def test_empty_gold_has_no_recall_not_zero_and_not_one(self):
        assert file_recall_at_k(["a.py"], set(), 5) is None

    def test_k_counts_chunks_so_one_files_chunks_use_up_the_budget(self):
        ranked = ["a.py"] * 5 + ["b.py"]
        assert file_recall_at_k(ranked, {"b.py"}, 5) == 0.0
        assert file_recall_at_k(ranked, {"b.py"}, 6) == 1.0

    def test_recall_at_10_and_at_20_differ_even_when_the_chunks_cover_few_files(self):
        # Twelve chunks of one file, then the gold file: over distinct files R@10 and R@20
        # were both 1.0 (two files). Over chunks the gold file is the 13th.
        ranked = ["a.py"] * 12 + ["b.py"]
        assert file_recall_at_k(ranked, {"b.py"}, 10) == 0.0
        assert file_recall_at_k(ranked, {"b.py"}, 20) == 1.0

    def test_duplicate_gold_paths_count_once(self):
        assert file_recall_at_k(["a.py"], ["a.py", "a.py"], 1) == 1.0

    @pytest.mark.parametrize("k", [0, -1])
    def test_k_must_be_positive(self, k):
        with pytest.raises(ValueError, match="k must be at least 1"):
            file_recall_at_k(["a.py"], {"a.py"}, k)


class TestFileReciprocalRank:
    def test_reciprocal_of_the_rank_of_the_first_chunk_in_a_gold_file(self):
        assert file_reciprocal_rank(["x.py", "x.py", "y.py", "g.py"], {"g.py"}) == pytest.approx(1 / 4)

    def test_first_of_several_gold_files_wins(self):
        assert file_reciprocal_rank(["a.py", "g2.py", "g1.py"], {"g1.py", "g2.py"}) == 0.5

    def test_a_miss_is_zero_not_an_invented_rank(self):
        assert file_reciprocal_rank(["a.py", "b.py"], {"g.py"}) == 0.0

    def test_empty_gold_is_none(self):
        assert file_reciprocal_rank(["a.py"], set()) is None


class TestSpanOverlap:
    def test_inclusive_ranges_that_share_one_line_overlap(self):
        assert spans_overlap(span("a.py", 1, 5), span("a.py", 5, 9))
        assert spans_overlap(span("a.py", 5, 9), span("a.py", 1, 5))

    def test_adjacent_ranges_do_not_overlap(self):
        assert not spans_overlap(span("a.py", 1, 4), span("a.py", 5, 9))

    def test_one_inside_the_other(self):
        assert spans_overlap(span("a.py", 1, 100), span("a.py", 40, 41))

    def test_a_single_line_hunk_hits_a_single_line_chunk_only_on_the_same_line(self):
        assert spans_overlap(span("a.py", 7, 7), span("a.py", 7, 7))
        assert not spans_overlap(span("a.py", 7, 7), span("a.py", 8, 8))

    def test_the_same_lines_in_another_file_do_not_overlap(self):
        assert not spans_overlap(span("a.py", 1, 5), span("b.py", 1, 5))

    @pytest.mark.parametrize("start,end", [(0, 3), (5, 4), (-1, 2)])
    def test_a_span_must_be_one_based_and_ordered(self, start, end):
        with pytest.raises(ValueError, match="bad span"):
            Span("a.py", start, end)


class TestChunkRecall:
    HUNKS = [span("a.py", 10, 12), span("a.py", 50, 50), span("b.py", 3, 3)]

    def test_recall_is_over_hunks_not_over_chunks(self):
        # One big chunk covers both a.py hunks: 2 of 3 hunks, not 1 of 1 chunk.
        chunks = [span("a.py", 1, 100)]
        assert chunk_recall_at_k(chunks, self.HUNKS, 5) == pytest.approx(2 / 3)

    def test_only_the_top_k_chunks_count(self):
        chunks = [span("c.py", 1, 9), span("a.py", 11, 11), span("b.py", 1, 9)]
        assert chunk_recall_at_k(chunks, self.HUNKS, 1) == 0.0
        assert chunk_recall_at_k(chunks, self.HUNKS, 2) == pytest.approx(1 / 3)
        assert chunk_recall_at_k(chunks, self.HUNKS, 3) == pytest.approx(2 / 3)

    def test_two_overlapping_chunks_do_not_double_count_one_hunk(self):
        # A class skeleton and its method both overlap the hunk: still one hunk.
        chunks = [span("a.py", 1, 60), span("a.py", 10, 20)]
        assert chunk_recall_at_k(chunks, [span("a.py", 12, 12)], 5) == 1.0

    def test_no_hunks_is_none(self):
        assert chunk_recall_at_k([span("a.py", 1, 5)], [], 5) is None

    def test_k_larger_than_the_list(self):
        assert chunk_recall_at_k([span("b.py", 1, 5)], self.HUNKS, 50) == pytest.approx(1 / 3)


class TestChunkReciprocalRank:
    def test_first_chunk_overlapping_any_hunk(self):
        chunks = [span("z.py", 1, 5), span("z.py", 6, 9), span("a.py", 9, 10)]
        assert chunk_reciprocal_rank(chunks, [span("a.py", 10, 12)]) == pytest.approx(1 / 3)

    def test_a_miss_is_zero(self):
        assert chunk_reciprocal_rank([span("z.py", 1, 5)], [span("a.py", 1, 1)]) == 0.0

    def test_no_hunks_is_none(self):
        assert chunk_reciprocal_rank([span("z.py", 1, 5)], []) is None


class TestAggregation:
    def test_mean_skips_none_and_says_how_many(self):
        result = mean_of([1.0, None, 0.0, None, 0.5])
        assert (result.mean, result.scored, result.skipped) == (0.5, 3, 2)

    def test_mean_of_nothing_scored_is_none(self):
        result = mean_of([None, None])
        assert (result.mean, result.scored, result.skipped) == (None, 0, 2)

    def test_score_query_computes_every_metric_at_every_k(self):
        chunks = [span("x.py", 1, 5), span("g.py", 10, 20), span("g.py", 40, 50)]
        scored = score_query(chunks, {"g.py"}, [span("g.py", 15, 15)], ks=(1, 2, 3))
        assert scored.file_recall == {1: 0.0, 2: 1.0, 3: 1.0}
        assert scored.chunk_recall == {1: 0.0, 2: 1.0, 3: 1.0}
        assert scored.file_rr == 0.5 and scored.chunk_rr == 0.5

    def test_a_query_with_no_gold_is_skipped_not_scored_as_a_miss(self):
        found = score_query([span("g.py", 1, 5)], {"g.py"}, [span("g.py", 1, 1)], ks=(5,))
        nothing_to_find = score_query([span("g.py", 1, 5)], set(), [], ks=(5,))
        missed = score_query([span("z.py", 1, 5)], {"g.py"}, [span("g.py", 1, 1)], ks=(5,))

        report = aggregate_metrics([found, nothing_to_find, missed], ks=(5,))

        assert report.queries == 3
        assert (report.file_recall[5].mean, report.file_recall[5].scored, report.file_recall[5].skipped) == (0.5, 2, 1)
        assert (report.chunk_recall[5].mean, report.chunk_recall[5].skipped) == (0.5, 1)
        assert (report.file_mrr.mean, report.chunk_mrr.mean) == (0.5, 0.5)

    def test_aggregating_nothing(self):
        report = aggregate_metrics([], ks=(5,))
        assert report.queries == 0 and report.file_recall[5].mean is None and report.file_mrr.mean is None


class TestClusterBootstrap:
    """The interval is over INSTANCES: the runs of one instance are not independent trials."""

    #: 20 instances, 3 runs each: 7 always pass, 5 sometimes, 8 never. 28 of 60.
    PASSES = [3] * 7 + [1, 1, 2, 2, 1] + [0] * 8
    RUNS = [3] * 20

    def test_the_documented_defaults(self):
        assert (BOOTSTRAP_SEED, BOOTSTRAP_RESAMPLES) == (0, 10_000)

    def test_it_is_deterministic_for_a_seed_and_varies_with_it(self):
        first = cluster_bootstrap_interval(self.PASSES, self.RUNS, resamples=500)
        assert first == cluster_bootstrap_interval(self.PASSES, self.RUNS, resamples=500)
        assert first != cluster_bootstrap_interval(self.PASSES, self.RUNS, resamples=500, seed=1)

    def test_it_contains_the_point_estimate_and_is_about_thirty_to_forty_points_wide_at_n_20(self):
        low, high = cluster_bootstrap_interval(self.PASSES, self.RUNS)
        assert low < 28 / 60 < high
        assert 0.30 < high - low < 0.46

    def test_it_is_wider_than_an_interval_that_treats_sixty_runs_as_independent(self):
        # Wilson on 28/60 is about 24.5 points wide; the instance-level interval is not.
        low, high = cluster_bootstrap_interval(self.PASSES, self.RUNS)
        assert (high - low) > 0.30

    def test_unanimous_data_has_no_width(self):
        assert cluster_bootstrap_interval([3, 3, 3], [3, 3, 3]) == (1.0, 1.0)
        assert cluster_bootstrap_interval([0, 0, 0], [3, 3, 3]) == (0.0, 0.0)

    def test_with_unequal_denominators_the_statistic_is_the_pooled_ratio(self):
        # Two clusters: (1 of 1) and (0 of 9). A resample is both, or either twice:
        # 1/10, 2/2 = 1.0, or 0/18 = 0.0.
        low, high = cluster_bootstrap_interval([1, 0], [1, 9], resamples=2000)
        assert (low, high) == (0.0, 1.0)

    def test_one_instance_has_no_interval(self):
        assert cluster_bootstrap_interval([2], [3]) is None
        assert cluster_bootstrap_interval([], []) is None

    def test_a_narrower_confidence_gives_a_narrower_interval(self):
        wide = cluster_bootstrap_interval(self.PASSES, self.RUNS, resamples=2000)
        narrow = cluster_bootstrap_interval(self.PASSES, self.RUNS, resamples=2000, confidence=0.5)
        assert narrow[1] - narrow[0] < wide[1] - wide[0]

    def test_refusals(self):
        with pytest.raises(ValueError, match="2 numerators but 1 denominators"):
            cluster_bootstrap_interval([1, 2], [3])
        with pytest.raises(ValueError, match="positive denominator"):
            cluster_bootstrap_interval([1, 2], [3, 0])
        with pytest.raises(ValueError, match="confidence"):
            cluster_bootstrap_interval([1, 2], [3, 3], confidence=1.0)
        with pytest.raises(ValueError, match="resamples"):
            cluster_bootstrap_interval([1, 2], [3, 3], resamples=0)

    def test_it_covers_the_truth_in_a_seeded_monte_carlo(self):
        # Latent per-instance pass probabilities with strong within-instance correlation
        # (Beta(0.15, 0.15): most instances nearly always pass or nearly always fail),
        # true mean 0.5, 20 instances x 3 runs. The instance-level interval should cover
        # the truth close to its nominal rate; the audit measured ~84% or worse for an
        # interval over the 60 pooled rows. Loose band: this is a smoke test of the
        # clustering, not a calibration study.
        rng = random.Random(11)
        covered = 0
        reps = 200
        for _ in range(reps):
            probabilities = [rng.betavariate(0.15, 0.15) for _ in range(20)]
            passes = [sum(rng.random() < p for _ in range(3)) for p in probabilities]
            interval = cluster_bootstrap_interval(passes, [3] * 20, resamples=400, seed=rng.randrange(10**6))
            covered += interval[0] <= 0.5 <= interval[1]
        assert 0.80 <= covered / reps <= 1.0


class TestPercentileLivesHere:
    def test_the_report_still_exports_it(self):
        from harness.report import percentile as reexported

        assert reexported is percentile
        assert percentile([1.0, 2.0], 95) == pytest.approx(1.95)


class TestExactSignTest:
    """McNemar's exact test on discordant pairs: the arithmetic the report quotes."""

    @pytest.mark.parametrize(
        "b,c,expected",
        [(8, 0, 0.0078125), (7, 1, 0.0703125), (6, 2, 0.2890625), (5, 3, 0.7265625), (4, 4, 1.0), (1, 0, 1.0)],
    )
    def test_the_published_cases(self, b, c, expected):
        assert exact_sign_test(b, c) == pytest.approx(expected)
        assert exact_sign_test(c, b) == pytest.approx(expected)

    def test_no_discordant_pair_has_nothing_to_test(self):
        assert exact_sign_test(0, 0) is None

    def test_it_never_exceeds_one(self):
        assert exact_sign_test(3, 3) == 1.0

    def test_negative_counts_are_refused(self):
        with pytest.raises(ValueError, match="must not be negative"):
            exact_sign_test(-1, 2)


AUDIT_SOURCE = '''import os

CONST = 1


def helper(x):
    y = x + 1
    z = y * 2
    return z


class Box:
    """A box."""

    size = 3

    def small(self):
        return self.size

    def big(self):
        a = 1
        b = 2
        c = 3
        return a + b + c


__all__ = ["helper", "Box"]
'''


def audit_corpus() -> dict[str, Span]:
    """The real chunker's spans for the audit's file: a 27-line module with 3 lines of module content."""
    from retrieval.chunker import chunk_python_file

    return {c.symbol_name: Span(c.file_path, c.start_line, c.end_line) for c in chunk_python_file("pkg/m.py", AUDIT_SOURCE)}


class TestInnermostChunkRule:
    """A chunk's span is not its content: the module chunk spans the file and holds three lines of it."""

    def setup_method(self):
        self.chunks = audit_corpus()
        self.corpus = list(self.chunks.values())

    def recall(self, retrieved, hunk_line, *, corpus="given", k=5):
        hunks = [Span("pkg/m.py", hunk_line, hunk_line)]
        return chunk_recall_at_k(retrieved, hunks, k, self.corpus if corpus == "given" else None)

    def test_the_real_chunker_gives_the_spans_the_audit_measured(self):
        assert self.chunks["pkg/m.py"] == Span("pkg/m.py", 1, 27)
        assert self.chunks["helper"] == Span("pkg/m.py", 6, 9)
        assert self.chunks["Box"] == Span("pkg/m.py", 12, 24)

    def test_a_retrieved_module_chunk_does_not_recall_a_hunk_inside_a_function(self):
        # The audit's repro: this scored 1.0.
        assert self.recall([self.chunks["pkg/m.py"]], 8) == 0.0

    def test_the_function_chunk_does_recall_it(self):
        assert self.recall([self.chunks["helper"]], 8) == 1.0

    def test_the_module_chunk_and_the_function_chunk_together_recall_it_through_the_function(self):
        assert self.recall([self.chunks["pkg/m.py"], self.chunks["helper"]], 8, k=1) == 0.0
        assert self.recall([self.chunks["pkg/m.py"], self.chunks["helper"]], 8, k=2) == 1.0

    def test_a_class_skeleton_does_not_recall_a_hunk_inside_a_method(self):
        assert self.recall([self.chunks["Box"]], 18) == 0.0
        assert self.recall([self.chunks["small"]], 18) == 1.0

    def test_a_hunk_in_the_class_body_outside_any_method_is_the_skeletons(self):
        assert self.recall([self.chunks["Box"]], 15) == 1.0
        assert self.recall([self.chunks["pkg/m.py"]], 15) == 0.0

    def test_a_hunk_in_module_level_code_is_the_module_chunks(self):
        assert self.recall([self.chunks["pkg/m.py"]], 3) == 1.0
        assert self.recall([self.chunks["pkg/m.py"]], 26) == 1.0
        assert self.recall([self.chunks["helper"]], 3) == 0.0

    def test_one_of_two_hunks_found_is_half(self):
        hunks = [Span("pkg/m.py", 8, 8), Span("pkg/m.py", 22, 22)]
        assert chunk_recall_at_k([self.chunks["helper"]], hunks, 5, self.corpus) == 0.5

    def test_the_reciprocal_rank_skips_a_wide_chunk_that_is_not_innermost(self):
        hunks = [Span("pkg/m.py", 8, 8)]
        ranked = [self.chunks["pkg/m.py"], self.chunks["Box"], self.chunks["helper"]]
        assert chunk_reciprocal_rank(ranked, hunks, self.corpus) == pytest.approx(1 / 3)

    def test_without_a_corpus_the_rule_is_the_weaker_retrieved_only_form(self):
        # Documented limitation: with nothing to compare against, the lone wide chunk counts.
        assert self.recall([self.chunks["pkg/m.py"]], 8, corpus=None) == 1.0
        # But a retrieved inner chunk still outranks the wide one beside it.
        assert self.recall([self.chunks["pkg/m.py"], self.chunks["helper"]], 8, corpus=None, k=1) == 0.0

    def test_unreachable_hunks_are_dropped_and_counted_not_scored_as_misses(self):
        hunks = [Span("pkg/m.py", 8, 8), Span("pkg/m.py", 40, 40), Span("other.py", 3, 3)]
        kept, dropped = reachable_hunks(hunks, self.corpus)
        assert kept == [Span("pkg/m.py", 8, 8)] and dropped == 2

    def test_a_query_whose_hunks_are_all_unreachable_has_no_chunk_score(self):
        scored = score_query([Span("pkg/m.py", 6, 9)], {"pkg/m.py"}, [Span("pkg/m.py", 40, 40)], ks=(5,), corpus=self.corpus)
        assert scored.unreachable_hunks == 1
        assert scored.chunk_recall[5] is None and scored.chunk_rr is None
        assert scored.file_recall[5] == 1.0

    def test_score_query_threads_the_corpus_through(self):
        scored = score_query([self.chunks["pkg/m.py"]], {"pkg/m.py"}, [Span("pkg/m.py", 8, 8)], ks=(5,), corpus=self.corpus)
        assert scored.chunk_recall[5] == 0.0 and scored.chunk_rr == 0.0 and scored.file_recall[5] == 1.0

    def test_strictly_contains(self):
        outer, inner = Span("a.py", 1, 10), Span("a.py", 3, 5)
        assert strictly_contains(outer, inner) and not strictly_contains(inner, outer)
        assert not strictly_contains(outer, outer)
        assert not strictly_contains(Span("a.py", 1, 10), Span("b.py", 3, 5))
        assert strictly_contains(Span("a.py", 1, 10), Span("a.py", 1, 5))

    def test_equal_spans_are_both_innermost(self):
        twin_a, twin_b = Span("a.py", 3, 5), Span("a.py", 3, 5)
        assert innermost_overlapping(Span("a.py", 4, 4), [twin_a, twin_b, Span("a.py", 1, 9)]) == {twin_a}
        assert len(innermost_overlapping(Span("a.py", 4, 4), [Span("a.py", 2, 5), Span("a.py", 3, 6)])) == 2
