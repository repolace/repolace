from functools import lru_cache

import structlog
from sentence_transformers import SentenceTransformer

from retrieval.config import EMBEDDING_DIM, EMBEDDING_MODEL, ENCODE_BATCH_SIZE

log = structlog.get_logger()

# Rough chars-per-token for code under a BPE tokenizer. Only used to flag
# probable truncation, never to size a buffer.
_CHARS_PER_TOKEN = 4


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


def _warn_on_probable_truncation(model: SentenceTransformer, texts: list[str]) -> None:
    """Surface silent truncation.

    encode() quietly drops anything past max_seq_length, so an over-long chunk
    is embedded from its opening lines alone with no error.
    """
    budget = model.max_seq_length * _CHARS_PER_TOKEN
    oversized = sum(1 for text in texts if len(text) > budget)
    if oversized:
        log.warning(
            "rag.embed.probable_truncation",
            oversized_texts=oversized,
            total_texts=len(texts),
            max_seq_length=model.max_seq_length,
        )


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = get_embedder()
    _warn_on_probable_truncation(model, texts)
    embeddings = model.encode(
        texts,
        batch_size=ENCODE_BATCH_SIZE,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return embeddings.tolist()


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
