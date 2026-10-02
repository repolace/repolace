"""Database-backed tests for `hybrid_search(keyword_query=, query_strategy=)`.

The two arms of hybrid retrieval want different strings: the semantic arm embeds
a short description, the keyword arm matches identifiers. These pin that the new
parameters split them, and that with neither passed the call behaves as it always
has.

The fake embedder's vectors carry no meaning, so which chunk ranks first is
decided by the keyword arm alone: a chunk that is in both arms outscores one that
is only in the semantic arm whatever its semantic rank (1/61 + 1/(60+r) > 1/61).
That is deliberate -- it makes "which string did the keyword arm search" an
assertion about the result, not about how a fake embeds.
"""

from pathlib import Path

import pytest

from retrieval.index import index_repo
from retrieval.query import build_query
from retrieval.retrieve import hybrid_search

from rag_support import seed_repo, write

pytestmark = [pytest.mark.anyio, pytest.mark.db]


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    write(root / "alpha_mod.py", "def alpha_handler():\n    return 'alpha'\n")
    write(root / "zebra_mod.py", "def zebra_handler():\n    return 'zebra'\n")
    write(root / "other_mod.py", "def other_handler():\n    return 'other'\n")
    return root


@pytest.fixture
async def repo_id(db_session, embedder, source):
    repo = await seed_repo(db_session)
    await index_repo(db_session, repo.id, source, "sha0")
    embedder.reset()
    return repo.id


def top_file(results) -> str:
    return results[0].chunk.file_path


class TestDefaultsAreUnchanged:
    async def test_the_query_drives_both_arms(self, db_session, embedder, repo_id):
        results = await hybrid_search(db_session, repo_id, "alpha")

        assert top_file(results) == "alpha_mod.py"
        assert results[0].keyword_rank == 1

    async def test_the_query_is_embedded_with_the_default_strategy(self, db_session, embedder, repo_id):
        await hybrid_search(db_session, repo_id, "alpha")

        assert embedder.query_calls == [("alpha", "truncate")]

    async def test_the_limit_is_still_the_third_positional_argument(self, db_session, embedder, repo_id):
        assert len(await hybrid_search(db_session, repo_id, "alpha", 1)) == 1

    async def test_an_empty_query_is_still_refused(self, db_session, embedder, repo_id):
        with pytest.raises(ValueError, match="query is empty"):
            await hybrid_search(db_session, repo_id, "   ")

        assert embedder.query_calls == []


class TestKeywordQuery:
    async def test_it_drives_the_keyword_arm(self, db_session, embedder, repo_id):
        results = await hybrid_search(db_session, repo_id, "alpha", keyword_query="zebra")

        assert top_file(results) == "zebra_mod.py"

    async def test_the_semantic_arm_still_embeds_the_query_not_the_keyword_string(
        self, db_session, embedder, repo_id
    ):
        await hybrid_search(db_session, repo_id, "alpha", keyword_query="zebra")

        assert embedder.query_calls == [("alpha", "truncate")]

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
    async def test_a_blank_value_falls_back_to_the_query_instead_of_disabling_the_arm(
        self, db_session, embedder, repo_id, blank
    ):
        """An empty tsquery matches nothing, so honouring it literally would make
        the search silently semantic-only."""
        results = await hybrid_search(db_session, repo_id, "alpha", keyword_query=blank)

        assert top_file(results) == "alpha_mod.py"

    async def test_a_query_with_no_keyword_hit_still_returns_semantic_results(self, db_session, embedder, repo_id):
        results = await hybrid_search(db_session, repo_id, "alpha", keyword_query="nothing_matches_this")

        assert len(results) == 3
        assert all(r.keyword_rank is None for r in results)


class TestQueryStrategy:
    async def test_it_reaches_embed_query(self, db_session, embedder, repo_id):
        await hybrid_search(db_session, repo_id, "alpha", query_strategy="head_tail")

        assert embedder.query_calls == [("alpha", "head_tail")]

    async def test_an_unknown_strategy_is_refused_before_anything_is_embedded(self, db_session, embedder, repo_id):
        with pytest.raises(ValueError, match="unknown strategy"):
            await hybrid_search(db_session, repo_id, "alpha", query_strategy="nonsense")

        assert embedder.query_calls == []

    def test_both_new_parameters_are_keyword_only(self):
        """Stream D and the pipeline call these by name; a positional slot would
        let a later insertion silently swap them with `limit`."""
        with pytest.raises(TypeError):
            hybrid_search(None, None, "q", 10, "kw")  # type: ignore[misc]


class TestWithBuildQuery:
    async def test_the_two_strings_from_build_query_split_across_the_arms(self, db_session, embedder, repo_id):
        built = build_query("handler returns the wrong value", "It happens in zebra_handler when called twice.")

        results = await hybrid_search(db_session, repo_id, built.semantic, keyword_query=built.keyword)

        assert embedder.query_calls == [(built.semantic, "truncate")]
        # The keyword arm's own ranking, not the fused order: the fake's vectors
        # are arbitrary and `handler` is in every chunk, so a chunk that is only
        # second in the keyword arm could still win the fusion.
        assert {r.chunk.file_path: r.keyword_rank for r in results}["zebra_mod.py"] == 1
