"""Unit tests for the retrieval projection.

**These cannot catch the hazard the projection exists for.** `MissingGreenlet`
is raised when a *session-loaded* row has deferred columns touched outside a
greenlet; a `CodeChunk` built by hand here has no deferred state, so accessing
anything on it succeeds. The real protection is that `from_result` names only
the loaded columns and hands back a detached object.

So what follows tests the field mapping and the derived strings. Treat the
deferred-column safety as guarded by review of `from_result`, not by this file.
"""

import uuid

from repolace_shared.db.models import ChunkType, CodeChunk
from retrieval.ranking import RRFResult

from repolace_pipeline.context import RetrievedChunk


def code_chunk(**overrides) -> CodeChunk:
    fields = {
        "id": uuid.uuid4(),
        "repo_id": uuid.uuid4(),
        "commit_sha": "abc123",
        "file_path": "src/app.py",
        "start_line": 10,
        "end_line": 20,
        "chunk_type": ChunkType.METHOD,
        "class_name": "Loader",
        "symbol_name": "load",
        "content": "def load(self): ...",
    }
    return CodeChunk(**{**fields, **overrides})


def result(**overrides) -> RRFResult:
    fields = {"rrf_score": 0.0328, "chunk": code_chunk(), "semantic_rank": 1, "keyword_rank": 4}
    return RRFResult(**{**fields, **overrides})


class TestFromResult:
    def test_copies_the_located_fields(self):
        projected = RetrievedChunk.from_result(result())

        assert (projected.file_path, projected.start_line, projected.end_line) == ("src/app.py", 10, 20)
        assert projected.symbol_name == "load"
        assert projected.class_name == "Loader"

    def test_carries_the_score_and_both_ranks(self):
        projected = RetrievedChunk.from_result(result(rrf_score=0.5, semantic_rank=2, keyword_rank=7))

        assert projected.rrf_score == 0.5
        assert (projected.semantic_rank, projected.keyword_rank) == (2, 7)

    def test_an_arm_that_missed_stays_none(self):
        """None means the arm did not return this chunk -- distinct from a rank."""
        projected = RetrievedChunk.from_result(result(keyword_rank=None))

        assert projected.keyword_rank is None

    def test_chunk_type_becomes_a_plain_string(self):
        projected = RetrievedChunk.from_result(result(chunk=code_chunk(chunk_type=ChunkType.FUNCTION)))

        assert projected.chunk_type == "function"
        assert not isinstance(projected.chunk_type, ChunkType)

    def test_the_projection_is_frozen(self):
        projected = RetrievedChunk.from_result(result())

        try:
            projected.file_path = "other.py"
        except Exception as exc:
            assert type(exc).__name__ == "FrozenInstanceError"
        else:
            raise AssertionError("projection should be immutable")


class TestDerivedStrings:
    def test_location_is_path_and_line_span(self):
        assert RetrievedChunk.from_result(result()).location == "src/app.py:10-20"

    def test_qualified_symbol_includes_the_class_for_a_method(self):
        assert RetrievedChunk.from_result(result()).qualified_symbol == "Loader.load"

    def test_a_plain_function_has_no_class_prefix(self):
        projected = RetrievedChunk.from_result(
            result(chunk=code_chunk(class_name=None, symbol_name="parse"))
        )

        assert projected.qualified_symbol == "parse"
