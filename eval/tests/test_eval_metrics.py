"""`harness.metrics`: recall@k, MRR and hunk-to-chunk overlap, pure arithmetic."""

import pytest

from harness.metrics import (
    Span,
    aggregate_metrics,
    chunk_recall_at_k,
    chunk_reciprocal_rank,
    distinct_in_order,
    file_recall_at_k,
    file_reciprocal_rank,
    mean_of,
    score_query,
    spans_overlap,
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

    def test_one_file_repeated_does_not_fill_the_top_k(self):
        # Five chunks of a.py are one file: b.py is rank 2, inside k=2.
        assert file_recall_at_k(["a.py"] * 5 + ["b.py"], {"b.py"}, 2) == 1.0

    def test_duplicate_gold_paths_count_once(self):
        assert file_recall_at_k(["a.py"], ["a.py", "a.py"], 1) == 1.0

    @pytest.mark.parametrize("k", [0, -1])
    def test_k_must_be_positive(self, k):
        with pytest.raises(ValueError, match="k must be at least 1"):
            file_recall_at_k(["a.py"], {"a.py"}, k)

    def test_distinct_in_order_keeps_the_first_appearance(self):
        assert distinct_in_order(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


class TestFileReciprocalRank:
    def test_reciprocal_of_the_first_gold_rank_over_distinct_files(self):
        assert file_reciprocal_rank(["x.py", "x.py", "y.py", "g.py"], {"g.py"}) == pytest.approx(1 / 3)

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
