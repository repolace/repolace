"""Builders for pipeline test data.

Plain constructors rather than fixtures: the stub editor takes plain data by
design, so building it should not need pytest machinery.
"""

import dataclasses
import uuid

from repolace_pipeline.context import RetrievedChunk
from repolace_pipeline.edit import StubEditRequest

TASK_ID = uuid.UUID("2f8a1c4e-0000-4000-8000-000000000001")


def chunk(
    file_path: str = "src/app.py",
    start_line: int = 3,
    end_line: int = 9,
    chunk_type: str = "function",
    symbol_name: str = "parse_config",
    class_name: str | None = None,
    rrf_score: float = 0.0328,
    semantic_rank: int | None = 1,
    keyword_rank: int | None = 3,
) -> RetrievedChunk:
    return RetrievedChunk(
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        chunk_type=chunk_type,
        symbol_name=symbol_name,
        class_name=class_name,
        rrf_score=rrf_score,
        semantic_rank=semantic_rank,
        keyword_rank=keyword_rank,
    )


def request(*chunks: RetrievedChunk, indexed_chunk_count: int = 238) -> StubEditRequest:
    return StubEditRequest(
        task_id=TASK_ID,
        issue_number=7,
        issue_title="parse_config crashes on an empty file",
        issue_url="https://github.com/acme/sample/issues/7",
        target_branch="main",
        base_sha="a1b2c3d4e5f6a7b8c9d0",
        indexed_chunk_count=indexed_chunk_count,
        retrieved=chunks or (chunk(),),
    )


def empty_request() -> StubEditRequest:
    """A request whose retrieval came back empty -- the case the editor must refuse."""
    return dataclasses.replace(request(), retrieved=())
