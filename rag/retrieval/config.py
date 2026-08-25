from typing import Final

from repolace_shared.db.models import CODE_CHUNK_EMBEDDING_DIM

EMBEDDING_MODEL: Final[str] = "flax-sentence-embeddings/st-codesearch-distilroberta-base"
EMBEDDING_DIM: Final[int] = CODE_CHUNK_EMBEDDING_DIM

RRF_K: Final[int] = 60
CANDIDATE_MULTIPLIER: Final[int] = 5
DEFAULT_SEARCH_LIMIT: Final[int] = 10

# Methods this short (in source lines) are folded into the class-skeleton chunk
# instead of getting their own standalone chunk — a one-line getter/setter
# embeds poorly in isolation (see CLAUDE.md's chunking-strategy discussion).
MIN_STANDALONE_CHUNK_LINES: Final[int] = 2
