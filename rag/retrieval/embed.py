from functools import lru_cache

from sentence_transformers import SentenceTransformer

from retrieval.config import EMBEDDING_MODEL


@lru_cache(maxsize=1)
def get_embedder() -> SentenceTransformer:
    return SentenceTransformer(EMBEDDING_MODEL)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    embeddings = get_embedder().encode(texts, show_progress_bar=False, convert_to_numpy=True)
    return embeddings.tolist()


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
