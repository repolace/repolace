from typing import Final

from repolace_shared.db.models import CODE_CHUNK_EMBEDDING_DIM

EMBEDDING_MODEL: Final[str] = "flax-sentence-embeddings/st-codesearch-distilroberta-base"
EMBEDDING_DIM: Final[int] = CODE_CHUNK_EMBEDDING_DIM

RRF_K: Final[int] = 60
CANDIDATE_MULTIPLIER: Final[int] = 5
DEFAULT_SEARCH_LIMIT: Final[int] = 10

# Keyword-arm terms are OR-combined, so an uncapped query (a pasted issue body)
# would lose nearly all selectivity and force ts_rank across most of the repo.
MAX_QUERY_TERMS: Final[int] = 32

# Chunks are embedded and flushed in batches rather than all at once: a whole
# repo's worth of vectors materialised as Python floats is several GB.
EMBED_BATCH_SIZE: Final[int] = 256

# Inner batch handed to SentenceTransformer.encode for each forward pass.
ENCODE_BATCH_SIZE: Final[int] = 32

# Methods this short (in source lines) are folded into the class-skeleton chunk
# instead of getting their own standalone chunk — a one-line getter/setter
# embeds poorly in isolation (see CLAUDE.md's chunking-strategy discussion).
MIN_STANDALONE_CHUNK_LINES: Final[int] = 2
