"""Unit tests for Reciprocal Rank Fusion.

`merge_rrf` only ever touches `chunk.id`, so these use a minimal stand-in
rather than real `CodeChunk` rows — no database, no ORM, no model load.
"""

import uuid
from dataclasses import dataclass

import pytest

from retrieval.config import RRF_K
from retrieval.ranking import merge_rrf, rrf_score


@dataclass(frozen=True)
class FakeChunk:
    id: uuid.UUID
    label: str = ""


def chunk(label: str = "") -> FakeChunk:
    return FakeChunk(id=uuid.uuid4(), label=label)


class TestRrfScore:
    def test_matches_the_canonical_formula(self):
        assert rrf_score(1, k=60) == pytest.approx(1 / 61)
        assert rrf_score(10, k=60) == pytest.approx(1 / 70)

    def test_is_strictly_decreasing_in_rank(self):
        scores = [rrf_score(rank) for rank in range(1, 20)]
        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) == len(scores)

    def test_defaults_to_the_configured_k(self):
        assert rrf_score(1) == rrf_score(1, k=RRF_K)


class TestMergeRrf:
    def test_empty_inputs_produce_no_results(self):
        assert merge_rrf([], [], limit=10) == []

    def test_chunk_in_only_the_semantic_arm_scores_from_that_arm_alone(self):
        c = chunk()
        (result,) = merge_rrf([c], [], limit=10, k=60)

        assert result.chunk is c
        assert result.semantic_rank == 1
        assert result.keyword_rank is None
        assert result.rrf_score == pytest.approx(1 / 61)

    def test_chunk_in_only_the_keyword_arm_scores_from_that_arm_alone(self):
        c = chunk()
        (result,) = merge_rrf([], [c], limit=10, k=60)

        assert result.semantic_rank is None
        assert result.keyword_rank == 1
        assert result.rrf_score == pytest.approx(1 / 61)

    def test_chunk_in_both_arms_sums_both_contributions(self):
        c = chunk()
        (result,) = merge_rrf([c], [c], limit=10, k=60)

        assert result.semantic_rank == 1
        assert result.keyword_rank == 1
        assert result.rrf_score == pytest.approx(2 / 61)

    def test_agreement_across_arms_outranks_a_single_strong_hit(self):
        """The whole point of RRF: two mediocre agreeing ranks beat one top rank."""
        both = chunk("both")
        semantic_only = chunk("semantic_only")

        results = merge_rrf(
            semantic_results=[semantic_only, both],
            keyword_results=[both],
            limit=10,
            k=60,
        )

        assert [r.chunk.label for r in results] == ["both", "semantic_only"]
        assert results[0].rrf_score == pytest.approx(1 / 62 + 1 / 61)
        assert results[1].rrf_score == pytest.approx(1 / 61)

    def test_results_are_sorted_by_descending_score(self):
        chunks = [chunk(str(i)) for i in range(5)]
        results = merge_rrf(chunks, [], limit=10)

        scores = [r.rrf_score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_limit_truncates_after_fusion_not_before(self):
        semantic = [chunk(f"s{i}") for i in range(5)]
        # The last semantic chunk is the only one the keyword arm also found,
        # so fusion should promote it into a limit=2 cut it would otherwise miss.
        results = merge_rrf(semantic, [semantic[-1]], limit=2)

        assert len(results) == 2
        assert "s4" in [r.chunk.label for r in results]

    def test_dedupes_distinct_objects_that_share_an_id_value(self):
        """Dedupe keys on the UUID *value*, not object identity.

        Both arms run on one AsyncSession today, so SQLAlchemy's identity map
        happens to hand back the same instance — but that is incidental. This
        pins the value-equality behaviour so a future change (separate sessions,
        a cache, `.unique()`) cannot silently start emitting duplicate rows.
        """
        shared_id = uuid.uuid4()
        from_semantic = FakeChunk(id=shared_id, label="semantic instance")
        from_keyword = FakeChunk(id=shared_id, label="keyword instance")
        assert from_semantic is not from_keyword

        results = merge_rrf([from_semantic], [from_keyword], limit=10, k=60)

        assert len(results) == 1
        assert results[0].semantic_rank == 1
        assert results[0].keyword_rank == 1
        assert results[0].rrf_score == pytest.approx(2 / 61)

    def test_keeps_the_first_seen_instance_on_a_dedupe(self):
        shared_id = uuid.uuid4()
        from_semantic = FakeChunk(id=shared_id, label="semantic instance")
        from_keyword = FakeChunk(id=shared_id, label="keyword instance")

        (result,) = merge_rrf([from_semantic], [from_keyword], limit=10)

        assert result.chunk.label == "semantic instance"
