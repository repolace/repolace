"""Strategies for fitting an over-long chunk into the encoder's context.

The embedding model's `max_seq_length` is small (128 tokens for the current
model) and 47.6% of this repo's own chunks exceed it. `encode()` silently
truncates, so the choice of what to keep is a real retrieval-quality decision.

Four strategies are implemented as interchangeable functions with the same
signature so the benchmark can run each and compare. They differ in what they
sacrifice:

    truncate             keep the head, discard the rest   (current behaviour)
    head_tail            keep the head and the tail, drop the middle
    windows              cover everything with overlapping windows, mean-pool
    signature_docstring  embed the declared interface only, not the body

Choosing a longer-context model is a separate, orthogonal axis: it changes
`limit` rather than the strategy, and can be combined with any of the four.
Evaluate it as a second variable in the grid, not as a fifth strategy.

Each function returns the list of texts to embed for one chunk. Returning more
than one text means the caller mean-pools the resulting vectors into a single
embedding, so the `code_chunks` schema is unchanged in every case.
"""

from collections.abc import Callable

from retrieval.chunker import extract_signature_docstring

# (content, tokenizer, token_limit) -> texts to embed for this one chunk
EmbeddingStrategy = Callable[[str, object, int], list[str]]

# Marks where content was removed, so the encoder sees a discontinuity rather
# than a false adjacency between the head and the tail.
ELISION = "\n    # ...\n"

# Fraction of the budget given to the head in head_tail. The signature and
# opening lines carry more identifying signal than the closing lines.
_HEAD_SHARE = 0.7

# Below this, a signature-and-docstring summary is too thin to stand alone
# and the strategy falls back to keeping real code.
_MIN_SUMMARY_TOKENS = 12

# Overlap between consecutive windows, as a fraction of the budget. Prevents a
# symbol that straddles a boundary from being split across both windows with
# its context severed.
_WINDOW_OVERLAP = 0.25


def _usable_budget(tokenizer, token_limit: int) -> int:
    """Token budget for content, after reserving the special tokens."""
    reserved = tokenizer.num_special_tokens_to_add(pair=False)
    return max(1, token_limit - reserved)


def _encode(tokenizer, text: str) -> list[int]:
    return tokenizer.encode(text, add_special_tokens=False, truncation=False, verbose=False)


def _decode(tokenizer, token_ids: list[int]) -> str:
    return tokenizer.decode(token_ids, skip_special_tokens=True)


def truncate(content: str, tokenizer, token_limit: int) -> list[str]:
    """Baseline: hand the content over and let the encoder keep the head.

    Cheapest, and the current production behaviour. Everything past the limit
    is lost, so a long function is represented by its signature and first few
    statements alone.
    """
    return [content]


def head_tail(content: str, tokenizer, token_limit: int) -> list[str]:
    """Keep the opening and closing of the chunk, drop the middle.

    Rationale: a function's identity is concentrated at its edges — the
    signature and docstring at the top, the return or raise at the bottom —
    while the middle is often loops and boilerplate that generalise poorly.
    Still one vector per chunk, so it costs no more to embed than `truncate`.
    """
    budget = _usable_budget(tokenizer, token_limit)
    token_ids = _encode(tokenizer, content)
    if len(token_ids) <= budget:
        return [content]

    elision_cost = len(_encode(tokenizer, ELISION))
    content_budget = max(1, budget - elision_cost)
    head_size = max(1, int(content_budget * _HEAD_SHARE))
    tail_size = max(0, content_budget - head_size)

    head = _decode(tokenizer, token_ids[:head_size])
    if tail_size == 0:
        return [head]
    tail = _decode(tokenizer, token_ids[-tail_size:])
    return [f"{head}{ELISION}{tail}"]


def windows(content: str, tokenizer, token_limit: int) -> list[str]:
    """Cover the whole chunk with overlapping windows; the caller mean-pools.

    Discards nothing, at the cost of one forward pass per window — the most
    expensive strategy to index, and the one whose benefit is least certain,
    since averaging many windows can blur a long chunk's vector toward the
    centroid and make it match everything weakly.
    """
    budget = _usable_budget(tokenizer, token_limit)
    token_ids = _encode(tokenizer, content)
    if len(token_ids) <= budget:
        return [content]

    stride = max(1, budget - int(budget * _WINDOW_OVERLAP))
    out: list[str] = []
    for start in range(0, len(token_ids), stride):
        window = token_ids[start : start + budget]
        if not window:
            break
        out.append(_decode(tokenizer, window))
        if start + budget >= len(token_ids):
            break
    return out


def signature_docstring(content: str, tokenizer, token_limit: int) -> list[str]:
    """Embed only the declared interface: decorators, signature, docstring.

    Rationale: an issue is usually phrased in terms of what a function is *for*,
    not how it is implemented, so the interface may carry more of the matching
    signal per token than the body does. It also fits the budget almost always,
    which makes it the only strategy that is largely immune to the context
    limit rather than merely coping with it.

    The bet it makes — that bodies are noise — is the one most likely to be
    wrong, since a bug usually lives in the body and undocumented code gives it
    almost nothing to work with. Falls back to `head_tail` when the chunk is
    not a single definition (module chunks) or has no docstring to lean on.
    """
    summary = extract_signature_docstring(content)
    if summary is None:
        return head_tail(content, tokenizer, token_limit)

    # A signature with no docstring is too thin to embed on its own.
    if len(_encode(tokenizer, summary)) < _MIN_SUMMARY_TOKENS:
        return head_tail(content, tokenizer, token_limit)

    # Long signatures (many arguments) or long docstrings can still overrun.
    return head_tail(summary, tokenizer, token_limit)


STRATEGIES: dict[str, EmbeddingStrategy] = {
    "truncate": truncate,
    "head_tail": head_tail,
    "windows": windows,
    "signature_docstring": signature_docstring,
}

DEFAULT_STRATEGY = "truncate"


def get_strategy(name: str) -> EmbeddingStrategy:
    try:
        return STRATEGIES[name]
    except KeyError:
        raise ValueError(f"unknown strategy {name!r}; expected one of {sorted(STRATEGIES)}") from None
