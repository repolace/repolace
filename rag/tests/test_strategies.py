"""Unit tests for the oversize-chunk embedding strategies.

Uses a fake word-level tokenizer so the suite stays free of the embedding
model. The strategies only need encode/decode/num_special_tokens_to_add, and
budget arithmetic is what is under test here — not real BPE behaviour.
"""

import textwrap

import pytest

from retrieval.strategies import (
    DEFAULT_STRATEGY,
    ELISION,
    STRATEGIES,
    get_strategy,
    head_tail,
    signature_docstring,
    truncate,
    windows,
)


class FakeTokenizer:
    """Whitespace tokenizer: one token per word, ids are indices into a vocab."""

    def __init__(self):
        self._vocab: list[str] = []

    def encode(self, text, add_special_tokens=False, truncation=False, verbose=False):
        ids = []
        for word in text.split():
            self._vocab.append(word)
            ids.append(len(self._vocab) - 1)
        return ids

    def decode(self, ids, skip_special_tokens=True):
        return " ".join(self._vocab[i] for i in ids)

    def num_special_tokens_to_add(self, pair=False):
        return 2


@pytest.fixture
def tok():
    return FakeTokenizer()


def n_tokens(tok, text: str) -> int:
    return len(tok.encode(text))


SHORT = "def add(a, b): return a + b"
LONG = " ".join(f"word{i}" for i in range(200))


class TestRegistry:
    def test_all_four_strategies_are_registered(self):
        assert set(STRATEGIES) == {"truncate", "head_tail", "windows", "signature_docstring"}

    def test_default_is_a_registered_strategy(self):
        assert DEFAULT_STRATEGY in STRATEGIES

    def test_get_strategy_returns_the_function(self):
        assert get_strategy("head_tail") is head_tail

    def test_unknown_strategy_names_the_valid_options(self):
        with pytest.raises(ValueError, match="unknown strategy"):
            get_strategy("nonsense")

    def test_every_strategy_returns_a_nonempty_list_of_strings(self, tok):
        for name, fn in STRATEGIES.items():
            out = fn(LONG, tok, 32)
            assert out and all(isinstance(part, str) for part in out), name


class TestTruncate:
    def test_passes_content_through_untouched(self, tok):
        assert truncate(LONG, tok, 32) == [LONG]

    def test_always_yields_exactly_one_text(self, tok):
        assert len(truncate(LONG, tok, 8)) == 1


class TestHeadTail:
    def test_short_content_is_returned_unchanged(self, tok):
        assert head_tail(SHORT, tok, 64) == [SHORT]

    def test_long_content_fits_the_budget(self, tok):
        (out,) = head_tail(LONG, tok, 32)

        assert n_tokens(tok, out) <= 32

    def test_keeps_both_ends_and_marks_the_gap(self, tok):
        (out,) = head_tail(LONG, tok, 32)

        assert "word0" in out
        assert "word199" in out
        assert ELISION.strip() in out

    def test_drops_the_middle(self, tok):
        (out,) = head_tail(LONG, tok, 32)

        assert "word100" not in out

    def test_yields_one_text_so_no_pooling_is_needed(self, tok):
        assert len(head_tail(LONG, tok, 32)) == 1


class TestWindows:
    def test_short_content_is_returned_unchanged(self, tok):
        assert windows(SHORT, tok, 64) == [SHORT]

    def test_every_window_fits_the_budget(self, tok):
        for part in windows(LONG, tok, 32):
            assert n_tokens(tok, part) <= 32

    def test_covers_the_whole_content(self, tok):
        """The point of this strategy: nothing is discarded."""
        joined = " ".join(windows(LONG, tok, 32))

        for probe in ("word0", "word100", "word199"):
            assert probe in joined

    def test_windows_overlap(self, tok):
        parts = windows(LONG, tok, 32)

        assert len(parts) > 1
        first_words = set(parts[0].split())
        second_words = set(parts[1].split())
        assert first_words & second_words, "consecutive windows should share tokens"

    def test_terminates_on_content_far_over_budget(self, tok):
        parts = windows(" ".join(f"w{i}" for i in range(5000)), tok, 16)

        assert 0 < len(parts) < 5000


class TestSignatureDocstring:
    FUNC = textwrap.dedent(
        '''
        def process(items, retries=3):
            """Transform every item in the batch, retrying transient failures.

            Each item is passed through transform() and collected in order.
            """
            for item in items:
                transform(item)
            return items
        '''
    ).lstrip("\n")

    def test_keeps_signature_and_docstring(self, tok):
        (out,) = signature_docstring(self.FUNC, tok, 64)

        assert "def process(items, retries=3):" in out
        assert "Transform every item" in out

    def test_drops_the_body(self, tok):
        (out,) = signature_docstring(self.FUNC, tok, 64)

        assert "transform(item)" not in out

    def test_keeps_decorators(self, tok):
        source = "@cache\n" + self.FUNC
        (out,) = signature_docstring(source, tok, 64)

        assert "@cache" in out

    def test_falls_back_when_content_is_not_a_single_definition(self, tok):
        """Module chunks have no signature to extract."""
        module = "import os\n\nMAX_RETRIES = 3\nTIMEOUT = 30\n"
        (out,) = signature_docstring(module, tok, 64)

        assert "MAX_RETRIES" in out

    def test_falls_back_when_the_summary_would_be_too_thin(self, tok):
        """An undocumented one-liner gives the encoder almost nothing."""
        (out,) = signature_docstring("def f(): return 1", tok, 64)

        assert "return 1" in out

    def test_result_fits_the_budget_even_with_a_long_docstring(self, tok):
        source = 'def f(a):\n    """' + " ".join(f"word{i}" for i in range(300)) + '"""\n    return a\n'
        (out,) = signature_docstring(source, tok, 32)

        assert n_tokens(tok, out) <= 32
