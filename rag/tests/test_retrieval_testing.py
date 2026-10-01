"""The shared fake embedder is itself test infrastructure, so it gets tests.

A fake that quietly stopped accepting `strategy` would break three suites the day
stream C merges; one that stopped being deterministic would make them flaky with
no visible cause. Both are pinned here, along with the patching, which is where a
fake most often fails to take effect: the call sites import the real function by
name.
"""

import math
import subprocess
import sys

import pytest

from retrieval import testing as fakes
from retrieval.config import EMBEDDING_DIM


class TestVectors:
    def test_a_vector_has_the_indexes_dimension(self):
        assert len(fakes.fake_vector("def f(): pass")) == EMBEDDING_DIM == 768

    def test_a_vector_is_unit_length_because_the_index_is_cosine(self):
        assert math.isclose(math.sqrt(sum(x * x for x in fakes.fake_vector("abc"))), 1.0, rel_tol=1e-9)

    def test_equal_text_gives_equal_vectors(self):
        assert fakes.fake_vector("same") == fakes.fake_vector("same")

    def test_different_text_gives_different_vectors(self):
        assert fakes.fake_vector("one") != fakes.fake_vector("two")

    def test_the_vector_does_not_depend_on_the_process(self):
        """`hash()` is salted per process; a hash-derived vector must not use it."""
        code = (
            "from retrieval.testing import fake_vector; "
            "print(repr(fake_vector('stable')[:3]))"
        )
        runs = {
            subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True, check=True
            ).stdout
            for _ in range(2)
        }
        assert len(runs) == 1
        assert repr(fakes.fake_vector("stable")[:3]) == runs.pop().strip()


class TestSignatures:
    """The reason this module exists: stream C adds a `strategy` argument at the call sites."""

    def test_embed_texts_accepts_a_strategy_positionally_and_by_keyword(self):
        assert len(fakes.fake_embed_texts(["a"], "head_tail")) == 1
        assert len(fakes.fake_embed_texts(["a"], strategy="windows")) == 1

    def test_embed_query_accepts_a_strategy_positionally_and_by_keyword(self):
        assert len(fakes.fake_embed_query("a", "head_tail")) == EMBEDDING_DIM
        assert len(fakes.fake_embed_query("a", strategy="windows")) == EMBEDDING_DIM

    def test_both_ignore_unknown_keywords_rather_than_raising(self):
        fakes.fake_embed_texts(["a"], extra=1)
        fakes.fake_embed_query("a", extra=1)

    def test_the_recorder_methods_accept_the_same_shapes(self):
        recorder = fakes.FakeEmbedder()
        recorder.embed_texts(["a"], "windows", extra=1)
        recorder.embed_query("q", strategy="head_tail", extra=1)

    def test_the_default_strategy_is_the_real_defaults(self):
        recorder = fakes.FakeEmbedder()
        recorder.embed_texts(["a"])
        recorder.embed_query("q")
        from retrieval.strategies import DEFAULT_STRATEGY

        assert recorder.strategies_used == {DEFAULT_STRATEGY}


class TestRecording:
    def test_it_records_texts_and_the_strategy_per_call(self):
        recorder = fakes.FakeEmbedder()
        recorder.embed_texts(["a", "b"], "windows")
        recorder.embed_texts(["c"], "head_tail")
        assert recorder.text_calls == [(("a", "b"), "windows"), (("c",), "head_tail")]
        assert recorder.embedded_texts == ["a", "b", "c"]

    def test_it_records_the_query_text_and_strategy(self):
        recorder = fakes.FakeEmbedder()
        recorder.embed_query("title and body head", "truncate")
        assert recorder.query_calls == [("title and body head", "truncate")]

    def test_strategies_used_spans_both_methods(self):
        recorder = fakes.FakeEmbedder()
        recorder.embed_texts(["a"], "windows")
        recorder.embed_query("q", "head_tail")
        assert recorder.strategies_used == {"windows", "head_tail"}

    def test_reset_forgets_everything(self):
        recorder = fakes.FakeEmbedder()
        recorder.embed_texts(["a"])
        recorder.embed_query("q")
        recorder.reset()
        assert (recorder.text_calls, recorder.query_calls) == ([], [])

    def test_the_returned_vectors_are_the_deterministic_ones(self):
        recorder = fakes.FakeEmbedder()
        assert recorder.embed_texts(["x"]) == [fakes.fake_vector("x")]
        assert recorder.embed_query("x") == fakes.fake_vector("x")


class TestInstalling:
    def test_it_patches_the_names_the_library_actually_looks_up(self, monkeypatch):
        """`index.py` and `retrieve.py` import these BY NAME; patching `retrieval.embed` alone does nothing for them."""
        from retrieval import embed, index, retrieve

        recorder = fakes.install_fake_embedder(monkeypatch)

        assert index.embed_texts == recorder.embed_texts
        assert retrieve.embed_query == recorder.embed_query
        assert embed.embed_texts == recorder.embed_texts
        assert embed.embed_query == recorder.embed_query
        assert embed.get_embedder() is recorder

    def test_it_uses_the_recorder_it_is_given(self, monkeypatch):
        mine = fakes.FakeEmbedder()
        assert fakes.install_fake_embedder(monkeypatch, mine) is mine

    def test_the_patch_is_undone_when_the_test_ends(self, monkeypatch):
        from retrieval import index

        real = index.embed_texts
        with pytest.MonkeyPatch.context() as local:
            fakes.install_fake_embedder(local)
            assert index.embed_texts != real
        assert index.embed_texts == real

    def test_calls_through_the_patched_library_are_recorded(self, monkeypatch):
        from retrieval import index

        recorder = fakes.install_fake_embedder(monkeypatch)
        # What `_insert_chunks` does today (stream C will add a strategy argument).
        vectors = index.embed_texts(["def f(): pass"])
        assert len(vectors) == 1 and recorder.embedded_texts == ["def f(): pass"]


class TestImportWeight:
    def test_importing_the_module_does_not_load_torch_or_sentence_transformers(self):
        code = (
            "import sys, retrieval.testing; "
            "assert 'torch' not in sys.modules and 'sentence_transformers' not in sys.modules, "
            "[m for m in ('torch','sentence_transformers') if m in sys.modules]"
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
