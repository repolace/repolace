import uuid
from functools import reduce

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_shared.db.models import CodeChunk
from retrieval.config import CANDIDATE_MULTIPLIER, DEFAULT_SEARCH_LIMIT, RRF_K
from retrieval.embed import embed_query
from retrieval.ranking import RRFResult, merge_rrf


async def _semantic_candidates(db: AsyncSession, repo_id: uuid.UUID, query: str, limit: int) -> list[CodeChunk]:
    query_embedding = embed_query(query)
    stmt = (
        select(CodeChunk)
        .where(CodeChunk.repo_id == repo_id)
        .order_by(CodeChunk.embedding.cosine_distance(query_embedding))
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _or_tsquery(query: str):
    # plainto_tsquery ANDs every term with no stemming under the 'simple' config, which
    # almost never matches a natural-language issue query against code identifiers.
    # OR-combine per-word queries instead, closer to how BM25 scores partial term overlap.
    words = [word for word in query.split() if word.strip()]
    if not words:
        return func.plainto_tsquery("simple", query)
    term_queries = [func.plainto_tsquery("simple", word) for word in words]
    return reduce(lambda acc, term_query: acc.op("||")(term_query), term_queries)


async def _keyword_candidates(db: AsyncSession, repo_id: uuid.UUID, query: str, limit: int) -> list[CodeChunk]:
    tsquery = _or_tsquery(query)
    stmt = (
        select(CodeChunk)
        .where(CodeChunk.repo_id == repo_id, CodeChunk.content_tsv.op("@@")(tsquery))
        .order_by(func.ts_rank(CodeChunk.content_tsv, tsquery).desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def hybrid_search(
    db: AsyncSession, repo_id: uuid.UUID, query: str, limit: int = DEFAULT_SEARCH_LIMIT
) -> list[RRFResult]:
    if not query.strip():
        raise ValueError("query is empty")

    candidate_limit = limit * CANDIDATE_MULTIPLIER
    semantic_results = await _semantic_candidates(db, repo_id, query, candidate_limit)
    keyword_results = await _keyword_candidates(db, repo_id, query, candidate_limit)

    return merge_rrf(semantic_results, keyword_results, limit=limit, k=RRF_K)
