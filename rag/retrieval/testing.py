"""One fake embedder for every test that indexes or searches.

Mirrors `verify/verify/testing.py`, and for the same reason: three streams (the
indexer, the retrieval eval and the pipeline's end-to-end test) all need to run
`index_repo`/`hybrid_search` without downloading a model, and none of them can
import another's test support -- pytest's prepend import mode makes test-module
names global, so `rag_support` is reachable from `pipeline/tests` only by
accident of `sys.path`. A shared, importable module is the version of this that
does not rot.

**Both fakes accept `strategy` and `**_`.** The real call sites do not pass a
strategy today (`index.py` calls `embed_texts(texts)`), and stream C is about to
add one. A fake written as `fake(texts)` would pass until C merges and then raise
`TypeError` in three suites at once, so the signature that survives the change is
the one to ship now.

The vectors are deterministic and carry no meaning: equal text gives equal
vectors, different text gives (almost surely) different ones, and nothing is
close to anything else. That is deliberate. A test that wants "this chunk ranks
first" should get it from the keyword arm or from an explicit vector, not from a
fake that pretends to understand code and so makes the test depend on how it
fakes it.

Importing this module is cheap (no torch, no sentence-transformers). Only
`install_fake_embedder` imports `retrieval.index`/`retrieval.retrieve`, which pull
the model library in at module level -- unavoidable, and the cost the test was
already going to pay by importing them.
"""

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Any

from retrieval.config import EMBEDDING_DIM
from retrieval.strategies import DEFAULT_STRATEGY


def fake_vector(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """A deterministic unit vector derived from `text`.

    Seeded from a hash rather than `hash()`, which is salted per process: the same
    text must give the same vector across processes, or a test that indexes in one
    and searches in another would be flaky for no visible reason. Unit length
    because the index is cosine-distance (`vector_cosine_ops`) and the real
    embeddings are normalised.
    """
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def fake_embed_texts(texts: list[str], strategy: str = DEFAULT_STRATEGY, **_: Any) -> list[list[float]]:
    """Stand-in for `retrieval.embed.embed_texts`. Accepts `strategy` and ignores extras."""
    return [fake_vector(text) for text in texts]


def fake_embed_query(text: str, strategy: str = DEFAULT_STRATEGY, **_: Any) -> list[float]:
    """Stand-in for `retrieval.embed.embed_query`. Accepts `strategy` and ignores extras."""
    return fake_vector(text)


@dataclass
class FakeEmbedder:
    """A fake that REMEMBERS what it was asked, so a test can assert on it.

    The question these tests actually need answered is rarely "what vector came
    back" and usually "was the strategy threaded through", "was the unchanged file
    skipped" or "what text did the query arm embed" -- all of which are about the
    call, not the result.
    """

    #: One entry per `embed_texts` call: the texts and the strategy it was given.
    text_calls: list[tuple[tuple[str, ...], str]] = field(default_factory=list)
    #: One entry per `embed_query` call: the text and the strategy it was given.
    query_calls: list[tuple[str, str]] = field(default_factory=list)

    def embed_texts(self, texts: list[str], strategy: str = DEFAULT_STRATEGY, **_: Any) -> list[list[float]]:
        self.text_calls.append((tuple(texts), strategy))
        return fake_embed_texts(texts, strategy)

    def embed_query(self, text: str, strategy: str = DEFAULT_STRATEGY, **_: Any) -> list[float]:
        self.query_calls.append((text, strategy))
        return fake_embed_query(text, strategy)

    @property
    def embedded_texts(self) -> list[str]:
        """Every text passed to `embed_texts`, flattened, in call order."""
        return [text for texts, _ in self.text_calls for text in texts]

    @property
    def strategies_used(self) -> set[str]:
        """Every strategy either method was called with."""
        return {s for _, s in self.text_calls} | {s for _, s in self.query_calls}

    def reset(self) -> None:
        self.text_calls.clear()
        self.query_calls.clear()


def install_fake_embedder(monkeypatch: Any, embedder: FakeEmbedder | None = None) -> FakeEmbedder:
    """Patch the embedder everywhere the library looks it up, and return the recorder.

    `index.py` and `retrieve.py` import `embed_texts` / `embed_query` BY NAME, so
    patching `retrieval.embed` alone changes nothing for them; the names that must
    be patched are the ones in the importing modules. `retrieval.embed` is patched
    too, for code that calls it through the module.

    **A module that imported `get_embedder` by name is not covered** --
    `repolace_pipeline.run` does -- so the pipeline takes a warm-up seam
    (`embedder_warmup`) instead and tests pass a no-op there. This function does not
    pretend to reach it.
    """
    from retrieval import embed, index, retrieve

    recorder = embedder or FakeEmbedder()
    monkeypatch.setattr(index, "embed_texts", recorder.embed_texts)
    monkeypatch.setattr(retrieve, "embed_query", recorder.embed_query)
    monkeypatch.setattr(embed, "embed_texts", recorder.embed_texts)
    monkeypatch.setattr(embed, "embed_query", recorder.embed_query)
    # The real one loads a ~hundreds-of-MB model; a test that reaches it by accident
    # should get the fake rather than a download.
    monkeypatch.setattr(embed, "get_embedder", lambda: recorder)
    return recorder
