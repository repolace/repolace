import asyncio
import re
import uuid
from functools import reduce

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from repolace_shared.db.models import CodeChunk
from retrieval.config import (
    CANDIDATE_MULTIPLIER,
    DEFAULT_SEARCH_LIMIT,
    MAX_QUERY_TERMS,
    RRF_K,
)
from retrieval.embed import embed_query
from retrieval.ranking import RRFResult, merge_rrf

# Postgres' default parser splits identifiers on non-alphanumeric characters, so
# `get_user_id` is indexed as the three lexemes get/user/id. Splitting the query
# the same way is what lets those sub-terms be OR-ed independently.
_TERM_RE = re.compile(r"[A-Za-z0-9]+")


def _candidate_columns(stmt):
    """Skip the columns the caller never reads.

    `embedding` is 768 float4s per row and `content_tsv` is a full tsvector;
    both are used inside the query and discarded afterwards.
    """
    return stmt.options(defer(CodeChunk.embedding), defer(CodeChunk.content_tsv))


async def _semantic_candidates(db: AsyncSession, repo_id: uuid.UUID, query: str, limit: int) -> list[CodeChunk]:
    # A SentenceTransformer forward pass is synchronous and CPU-bound; running
    # it inline would stall the event loop for the whole API process.
    query_embedding = await asyncio.to_thread(embed_query, query)
    stmt = _candidate_columns(
        select(CodeChunk)
        .where(CodeChunk.repo_id == repo_id)
        # CodeChunk.id breaks ties so identical queries rank identically.
        .order_by(CodeChunk.embedding.cosine_distance(query_embedding), CodeChunk.id)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _query_terms(query: str) -> list[str]:
    """Split into deduped, capped search terms.

    Splitting on non-alphanumerics mirrors the text-search parser, so an
    identifier contributes its sub-lexemes as independent OR-able terms rather
    than as one conjunction. The cap matters because these are OR-ed: an
    uncapped issue body would build a several-hundred-term query with almost no
    selectivity, forcing ts_rank across most of the repo.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for word in _TERM_RE.findall(query):
        lowered = word.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(word)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return terms


def _or_tsquery(query: str):
    """OR-combine the query's terms.

    plainto_tsquery ANDs everything it is given, which almost never matches a
    natural-language issue against code. Feeding it one term at a time and
    OR-ing the results gives partial-overlap scoring closer to BM25.
    """
    terms = _query_terms(query)
    if not terms:
        return func.plainto_tsquery("simple", query)
    term_queries = [func.plainto_tsquery("simple", term) for term in terms]
    return reduce(lambda acc, term_query: acc.op("||")(term_query), term_queries)


async def _keyword_candidates(db: AsyncSession, repo_id: uuid.UUID, query: str, limit: int) -> list[CodeChunk]:
    tsquery = _or_tsquery(query)
    stmt = _candidate_columns(
        select(CodeChunk)
        .where(CodeChunk.repo_id == repo_id, CodeChunk.content_tsv.op("@@")(tsquery))
        .order_by(func.ts_rank(CodeChunk.content_tsv, tsquery).desc(), CodeChunk.id)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def hybrid_search(
    db: AsyncSession, repo_id: uuid.UUID, query: str, limit: int = DEFAULT_SEARCH_LIMIT
) -> list[RRFResult]:
    if not query.strip():
        raise ValueError("query is empty")
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")

    candidate_limit = limit * CANDIDATE_MULTIPLIER
    # Sequential rather than gathered: both arms share one AsyncSession, and
    # SQLAlchemy rejects concurrent operations on a single session.
    semantic_results = await _semantic_candidates(db, repo_id, query, candidate_limit)
    keyword_results = await _keyword_candidates(db, repo_id, query, candidate_limit)

    return merge_rrf(semantic_results, keyword_results, limit=limit, k=RRF_K)
