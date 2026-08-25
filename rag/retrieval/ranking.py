import uuid
from dataclasses import dataclass

from repolace_shared.db.models import CodeChunk
from retrieval.config import RRF_K


@dataclass(frozen=True)
class RRFResult:
    rrf_score: float
    chunk: CodeChunk
    semantic_rank: int | None = None
    keyword_rank: int | None = None


def rrf_score(rank: int, k: int = RRF_K) -> float:
    return 1.0 / (k + rank)


def merge_rrf(
    semantic_results: list[CodeChunk],
    keyword_results: list[CodeChunk],
    limit: int,
    k: int = RRF_K,
) -> list[RRFResult]:
    entries: dict[uuid.UUID, dict] = {}

    for rank, chunk in enumerate(semantic_results, start=1):
        entries.setdefault(chunk.id, {"chunk": chunk})["semantic_rank"] = rank

    for rank, chunk in enumerate(keyword_results, start=1):
        entries.setdefault(chunk.id, {"chunk": chunk})["keyword_rank"] = rank

    results: list[RRFResult] = []
    for entry in entries.values():
        semantic_rank = entry.get("semantic_rank")
        keyword_rank = entry.get("keyword_rank")
        score = (rrf_score(semantic_rank, k) if semantic_rank is not None else 0.0) + (
            rrf_score(keyword_rank, k) if keyword_rank is not None else 0.0
        )
        results.append(
            RRFResult(rrf_score=score, chunk=entry["chunk"], semantic_rank=semantic_rank, keyword_rank=keyword_rank)
        )

    results.sort(key=lambda item: item.rrf_score, reverse=True)
    return results[:limit]
