"""Plain projections of retrieval results.

``hybrid_search`` returns live ORM ``CodeChunk`` rows with ``embedding`` and
``content_tsv`` deferred. Touching either outside a greenlet context raises
``MissingGreenlet``, and the rows stay bound to the session they were loaded
from -- so passing them further into the pipeline couples every later stage to
a session lifetime and to which columns happen to be loaded.

Projecting at the call site cuts both. Nothing downstream of retrieval ever
sees a ``CodeChunk``, so no downstream code *can* trip the deferred-column
hazard, whatever it does with what it is given.
"""

from dataclasses import dataclass

from retrieval.ranking import RRFResult


@dataclass(frozen=True)
class RetrievedChunk:
    """One retrieval hit, detached from the ORM and from the session."""

    file_path: str
    start_line: int
    end_line: int
    chunk_type: str
    symbol_name: str
    class_name: str | None
    rrf_score: float
    semantic_rank: int | None
    keyword_rank: int | None

    @classmethod
    def from_result(cls, result: RRFResult) -> "RetrievedChunk":
        chunk = result.chunk
        return cls(
            file_path=chunk.file_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            # ChunkType subclasses str, but .value keeps the plain string out of
            # anything that later formats or compares it as an enum.
            chunk_type=chunk.chunk_type.value,
            symbol_name=chunk.symbol_name,
            class_name=chunk.class_name,
            rrf_score=result.rrf_score,
            semantic_rank=result.semantic_rank,
            keyword_rank=result.keyword_rank,
        )

    @property
    def location(self) -> str:
        """`path:start-end`, the form used in logs, the edit and the PR body."""
        return f"{self.file_path}:{self.start_line}-{self.end_line}"

    @property
    def qualified_symbol(self) -> str:
        return f"{self.class_name}.{self.symbol_name}" if self.class_name else self.symbol_name
