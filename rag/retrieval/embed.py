from functools import lru_cache

import numpy as np
import structlog
from sentence_transformers import SentenceTransformer

from retrieval.config import EMBEDDING_DIM, EMBEDDING_MODEL, ENCODE_BATCH_SIZE
from retrieval.strategies import DEFAULT_STRATEGY, get_strategy

log = structlog.get_logger()


@lru_cache(maxsize=1)
def get_embedder() -> SentenceTransformer:
    """Load the embedding model.

    Constructing this may download several hundred MB on first use, so callers
    should warm it at startup rather than paying for it inside a request.
    """
    model = SentenceTransformer(EMBEDDING_MODEL)

    # Renamed in sentence-transformers 5; keep working on either name.
    get_dim = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    actual_dim = get_dim()
    if actual_dim != EMBEDDING_DIM:
        # Otherwise this surfaces as an opaque "expected 768 dimensions" error
        # from Postgres on every insert, with no hint that the model is at fault.
        raise RuntimeError(
            f"{EMBEDDING_MODEL} produces {actual_dim}-dim embeddings, but the "
            f"code_chunks.embedding column is {EMBEDDING_DIM}-dim. Change the "
            f"model or migrate the column; they must agree."
        )

    log.info(
        "rag.embed.model_loaded",
        model=EMBEDDING_MODEL,
        dim=actual_dim,
        max_seq_length=model.max_seq_length,
    )
    return model


def _warn_on_truncation(model: SentenceTransformer, texts: list[str]) -> None:
    """Surface silent truncation.

    encode() quietly drops anything past max_seq_length, so an over-long chunk
    is embedded from its opening lines alone with no error.

    This tokenizes to count rather than estimating from character length. A
    chars-per-token estimate is badly miscalibrated on code -- against this
    repo a len > 4*max_seq_length rule found 50 oversized chunks where real
    tokenization found 91. Tokenizing twice costs far less than the forward
    pass, and this number is the signal for whether the model's context is
    large enough to be worth trusting.
    """
    limit = model.max_seq_length
    encoded = model.tokenizer(texts, add_special_tokens=True, truncation=False, verbose=False)
    token_counts = [len(ids) for ids in encoded["input_ids"]]
    oversized = [count for count in token_counts if count > limit]
    if not oversized:
        return

    log.warning(
        "rag.embed.truncated",
        truncated_texts=len(oversized),
        total_texts=len(texts),
        max_seq_length=limit,
        worst_token_count=max(oversized),
    )


def _encode(model: SentenceTransformer, texts: list[str]):
    return model.encode(
        texts,
        batch_size=ENCODE_BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True,
    )


def embed_texts(texts: list[str], strategy: str = DEFAULT_STRATEGY) -> list[list[float]]:
    """Embed one vector per input text, using the named oversize strategy.

    A strategy may expand one text into several windows; those are embedded
    together and mean-pooled back down, so the caller always gets exactly one
    vector per input and the code_chunks schema is unaffected.
    """
    if not texts:
        return []
    model = get_embedder()
    _warn_on_truncation(model, texts)

    if strategy == "truncate":
        # Fast path: no re-tokenization, no pooling.
        return _encode(model, texts).tolist()

    strategy_fn = get_strategy(strategy)
    limit = model.max_seq_length

    flattened: list[str] = []
    spans: list[tuple[int, int]] = []
    for text in texts:
        parts = strategy_fn(text, model.tokenizer, limit)
        spans.append((len(flattened), len(flattened) + len(parts)))
        flattened.extend(parts)

    vectors = _encode(model, flattened)

    pooled = []
    for start, end in spans:
        group = vectors[start:end]
        vector = group[0] if len(group) == 1 else group.mean(axis=0)
        # Renormalize: the mean of unit vectors is not itself a unit vector,
        # and the index is searched by cosine distance.
        norm = np.linalg.norm(vector)
        pooled.append((vector / norm if norm else vector).tolist())
    return pooled


def embed_query(text: str, strategy: str = DEFAULT_STRATEGY) -> list[float]:
    return embed_texts([text], strategy=strategy)[0]
