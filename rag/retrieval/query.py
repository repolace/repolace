"""Turn an issue into the two strings hybrid retrieval wants.

The two arms want different things from the same issue, which is why this returns
two strings and not one. The semantic arm embeds its string with a model whose
window is ~128 tokens: it wants a short natural description, and only the head
of the body can matter. The keyword arm OR-matches terms against code: it wants
identifiers (the function in a traceback, a dotted name, a snake_case symbol),
which are often buried deep in the body, past the point the encoder would stop
reading.

**The issue text is untrusted** -- anyone who can open an issue wrote it -- and
this module treats it as data. Everything it emits is bounded, stripped of
control characters, and the keyword string is built only from `[A-Za-z0-9_.]`
tokens, so no quote, operator or SQL fragment in the body survives into it. It is
still handed to `retrieve._or_tsquery`, which binds each term as a parameter;
this module does not rely on its own filtering for that and does not reimplement
it.

Pure and cheap to import (no torch, no database): the regexes below run over a
bounded slice of attacker-controlled text, so each is written to be linear in it.
"""

import re
import unicodedata
from dataclasses import dataclass

from retrieval.config import MAX_QUERY_TERMS

#: The most body text ever examined, for either string. `max_body_chars` shrinks
#: the semantic head below this; nothing raises it. GitHub caps a body near
#: 65k characters, but this is a trust boundary, so the bound lives here and not
#: in an assumption about the sender.
_MAX_SCAN_CHARS = 20_000
_MAX_TITLE_CHARS = 256
#: A single token longer than this is junk (a hash, a base64 blob, a pathological
#: `a.a.a.a...` chain), not an identifier worth matching.
_MAX_TOKEN_CHARS = 128

#: Categories removed from untrusted text: control characters (NUL included) and
#: lone surrogates, which cannot be encoded to UTF-8 and would raise inside the
#: tokenizer. Newline and tab are kept for now, since traceback frames are
#: line-oriented; whitespace is collapsed afterwards where it no longer matters.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cs"})
_KEPT_CONTROLS = frozenset({"\n", "\t"})

# Every pattern anchors on something literal or is guarded by a lookbehind. An
# unanchored `ident(\.ident)+` retried at each position inside a long identifier
# run is quadratic, and the input is attacker-controlled.
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_NOT_IN_IDENT = r"(?<![A-Za-z0-9_])"
_FRAME_RE = re.compile(r'File "([^"\n]{1,256})", line \d+, in ([^\s"]{1,128})')
_DOTTED_RE = re.compile(rf"{_NOT_IN_IDENT}{_IDENT}(?:\.{_IDENT})+")
_WORD_RE = re.compile(rf"{_NOT_IN_IDENT}{_IDENT}")
_IDENT_OR_DOTTED_RE = re.compile(rf"{_IDENT}(?:\.{_IDENT})*")
_CAMEL_BOUNDARY_RE = re.compile(r"[a-z][A-Z]")

#: Words with no selectivity against code: English function words, the boilerplate
#: of a pasted traceback, and the Python keywords and dunders that appear in
#: nearly every chunk. Only 3+ character words are listed; shorter tokens are
#: dropped on length before this is consulted. Deliberately NOT in here: `error`,
#: `get`, `set`, `config` and the like, which are noise in prose but are real
#: identifiers in code.
_STOPWORDS = frozenset(
    """
    about after also and any are because been before being but can could did does each
    for from has have here how into its just many may more most much not now one only
    onto other our out over same should some such than that the their them then there
    these they this those too upon used using very via was were what when where which
    while who why will with would yet you your
    traceback recent call last file line
    self none true false def class return import
    __init__ __main__
    """.split()
)


@dataclass(frozen=True)
class RetrievalQuery:
    """What `hybrid_search(query=..., keyword_query=...)` should be given."""

    semantic: str
    #: Space-separated identifier-shaped terms; empty when the text held none.
    #: `hybrid_search` treats an empty value as "not given" and searches on
    #: `semantic` instead, so an empty keyword never silently disables the arm.
    keyword: str


def _clean(text: str, limit: int) -> str:
    """Bound first, then neutralise: the work stays linear in `limit`, not in what was sent."""
    return "".join(
        " " if unicodedata.category(ch) in _STRIPPED_CATEGORIES and ch not in _KEPT_CONTROLS else ch
        for ch in text[:limit]
    )


def _is_usable(token: str) -> bool:
    return (
        3 <= len(token) <= _MAX_TOKEN_CHARS
        and any(ch.isalpha() for ch in token)
        and token.lower() not in _STOPWORDS
    )


def _is_code_shaped(token: str) -> bool:
    """snake_case, camelCase/CamelCase, an acronym or a name with a digit.

    A plain lowercase word is as likely to be prose as code; these are not, so
    they are ranked ahead of prose when the term cap forces a choice.
    """
    return (
        "_" in token
        or any(ch.isdigit() for ch in token)
        or token.isupper()
        or _CAMEL_BOUNDARY_RE.search(token) is not None
    )


def _module_path(file_path: str) -> str:
    """`/venv/lib/site-packages/pkg/mod.py` -> `pkg.mod`; at most the last three components.

    Absolute paths from someone else's machine are mostly noise (`home`, `alice`,
    `work`), so a `site-packages` prefix is cut and the rest is bounded.
    """
    parts = [p for p in re.split(r"[\\/]", file_path) if p and p not in {".", ".."}]
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][: -len(".py")]
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            parts = parts[parts.index(marker) + 1 :]
    kept = [p for p in parts[-3:] if _WORD_RE.fullmatch(p) and _is_usable(p)]
    return ".".join(kept)


def _traceback_terms(text: str) -> list[str]:
    """Function names and module paths from `File "...", line N, in fn` frames.

    Innermost frame first: it is where the exception was raised, so when the term
    cap cuts something it should cut the callers and not the culprit.
    """
    terms: list[str] = []
    for file_path, function in reversed(_FRAME_RE.findall(text)):
        # `<module>`, `<lambda>` and friends fail the full match and are skipped.
        if _IDENT_OR_DOTTED_RE.fullmatch(function) and _is_usable(function):
            terms.append(function)
        module = _module_path(file_path)
        if module:
            terms.append(module)
    return terms


def _dotted_terms(text: str) -> list[str]:
    """`pkg.mod.Class.method`. Kept if any component is itself worth matching, which drops `e.g`."""
    return [
        name
        for name in _DOTTED_RE.findall(text)
        if len(name) <= _MAX_TOKEN_CHARS and any(_is_usable(part) for part in name.split("."))
    ]


def _identifier_terms(text: str) -> list[str]:
    words = [word for word in _WORD_RE.findall(text) if _is_usable(word)]
    return [w for w in words if _is_code_shaped(w)] + [w for w in words if not _is_code_shaped(w)]


def _keyword_terms(text: str) -> list[str]:
    """Most specific first, so the cap drops the least informative terms.

    The frames are blanked out once their terms are taken. Left in, the rest of
    the scan would pick the *path* back up as words (`home`, `alice`, `venv`,
    `site`, `packages`) and as dotted names (`main.py`), none of which is in the
    repository being searched.
    """
    terms: list[str] = []
    seen: set[str] = set()
    traceback_terms = _traceback_terms(text)
    text = _FRAME_RE.sub(" ", text)
    for candidate in (*traceback_terms, *_dotted_terms(text), *_identifier_terms(text)):
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(candidate)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return terms


def build_query(title: str, body: str | None, *, max_body_chars: int = 1500) -> RetrievalQuery:
    """Build the semantic and keyword query strings for one issue.

    `semantic` is the title plus the first `max_body_chars` characters of the
    body, whitespace-collapsed. `keyword` is drawn from a larger slice of the
    body (still bounded by `_MAX_SCAN_CHARS`), because the identifiers that
    matter most -- a pasted traceback -- usually sit well past the opening
    paragraph the encoder can read.

    Total over its input: a missing body or an empty title gives an empty string
    for that part, not an error. `hybrid_search` is what refuses an empty query.
    """
    if max_body_chars < 0:
        # A negative slice bound would keep all but the last N characters, which
        # is the opposite of a cap.
        raise ValueError(f"max_body_chars must not be negative, got {max_body_chars}")

    clean_title = _clean(title, _MAX_TITLE_CHARS)
    clean_body = _clean(body or "", _MAX_SCAN_CHARS)

    head = " ".join(clean_body.split())[:max_body_chars]
    semantic = " ".join(part for part in (" ".join(clean_title.split()), head) if part)

    keyword = " ".join(_keyword_terms(f"{clean_title}\n{clean_body}"))
    return RetrievalQuery(semantic=semantic, keyword=keyword)
