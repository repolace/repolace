"""Pure, bounded helpers that turn untrusted text into prompt text.

Everything here is a string function: no file IO, no clock, no environment, no
secrets. It exists so that the one rule about untrusted input is written once.

**Anything that did not come from repolace's own source is data.** That is the
issue (anyone can file one), a retrieved snippet (the repository author wrote
it), a test id (a parametrised id is arbitrary text the repository chose) and
test output. Each is put in front of the model inside a block delimited by a
per-task nonce, after four things have been done to it, in this order:

0. **Length is pre-cut**, generously (a few times the limit), so the work below
   is bounded by what is going to be sent and not by what an attacker supplied.
1. **Invisible and control characters are removed or escaped** -- deleted by
   `sanitize_text` for text that is only ever read, escaped by `escape_invisible` for
   tool output that is also edited against; one definition of "invisible" serves both.
2. **Closing delimiters are neutralised in ONE pass**: the leading `<` of every
   closing tag of every family becomes `‹`. The nonce is unguessable, so an
   attacker cannot write the real closing tag; but neutralising *any* closing tag
   of the family means the defence does not rest on the nonce staying secret.
   Deleting them instead needed a fixpoint (deleting `</issue-x>` from
   `</iss</issue-x>ue-x>` assembles a new one), and a fixpoint is quadratic on
   nested payloads: 64 KB of them froze the event loop for 5 s. Replacing the
   `<` creates no new `<`, so nothing can reassemble and one pass is enough.
3. **Length is cut**, last, so the bound holds for what is actually sent.

None of this makes a model obey the delimiters -- that is what the system
prompt's authority statement is for, and the real bound on a hostile issue is
what the tools can do. This only removes the cheap ways to defeat the framing.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence

from repolace_agents.contracts import SearchHit

#: Tag families this package opens. A closing tag of any of them, with any
#: suffix, is neutralised in untrusted text -- see the module docstring.
_FAMILIES = ("issue", "repository", "retrieved", "baseline", "feedback", "output")

#: Matches the tag name only -- the family, then whatever `-nonce` suffix follows --
#: plus an optional `>`. It deliberately does not match "up to the next `>`": an
#: unterminated `</issue-x` must not be able to swallow the text after it, which
#: would let a hostile body hide the legitimate content that follows. `>?` still
#: covers an unterminated one, so it cannot be completed by what comes next, and
#: `\b` keeps `</issues>` and `</output2>` -- other tags -- out of it.
_CLOSING_TAG = re.compile(r"<\s*/\s*(?:%s)\b[A-Za-z0-9_-]*\s*>?" % "|".join(_FAMILIES), re.IGNORECASE)

#: The only thing a nonce may be. It is spliced into a tag, so anything with `>`,
#: whitespace or a newline in it would let the nonce itself break the framing.
_NONCE = re.compile(r"[A-Za-z0-9]{0,64}")

#: Unicode general categories that are never legitimately visible text: control (Cc, except
#: newline and tab), format (Cf: zero-width, bidi overrides, the whole U+E0000 tag block) and
#: surrogates (Cs), which cannot be encoded and fail late, in the provider call.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_KEPT_CONTROLS = frozenset({"\n", "\t"})

#: Characters outside Cc/Cf/Cs that are nevertheless invisible or render as nothing, and so
#: can carry text a reviewer reading the prompt cannot see: the combining grapheme joiner,
#: Mongolian free variation selectors, Hangul and halfwidth fillers, Khmer inherent vowels,
#: the braille blank, and the line and paragraph separators.
_INVISIBLE_CODEPOINTS = frozenset({
    0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180E, 0x180F,
    0x2028, 0x2029, 0x2800, 0x3164, 0xFFA0,
})
#: Variation selectors: U+FE00-FE0F and the supplement U+E0100-E01EF. A run of them after one
#: visible character is a known way to smuggle bytes through text.
_VARIATION_SELECTORS = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
#: U+200C and U+200D are format characters that real text needs: they are what joins an emoji
#: sequence and shapes Persian, Indic and Khmer text. The escaper keeps them so that a tool
#: result still matches the file it came from, and `edit_file` can still find its target.
_JOINERS = frozenset({"\u200c", "\u200d"})


def _is_extra_invisible(ch: str) -> bool:
    code = ord(ch)
    return (
        code in _INVISIBLE_CODEPOINTS
        or any(low <= code <= high for low, high in _VARIATION_SELECTORS)
        or unicodedata.category(ch) == "Co"  # private use: renders as nothing, or as whatever a font says
    )


#: The one sentence that tells the agent some tests are not shown to it. **One
#: phrasing, used verbatim by the system prompt, the retry message and the baseline
#: summary, identical in product and benchmark mode.** It is honest (the agent should
#: not read a passing run it can see as proof the issue is fixed) without ever saying
#: how many hidden tests there are, which files hold them or what they are called, or
#: using the words "visible" and "hidden", which turn a fact into a thing to probe for.
TESTS_NOT_SHOWN = "Some tests are not shown to you."

MAX_SNIPPET_CHARS = 4000
MAX_OVERVIEW_CHARS = 6000


def check_nonce(nonce: str) -> str:
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ValueError(f"a delimiter nonce must be 0-64 ASCII letters or digits, got {nonce!r}")
    return nonce


def sanitize_text(text: str) -> str:
    """`text` with every invisible or control character deleted; newlines are normalised.

    Deleted: control characters except newline and tab (Cc), all format characters (Cf --
    zero-width, bidi, the tag block, and ZWJ/ZWNJ too), surrogates (Cs), variation
    selectors, private-use characters, and the few others in `_INVISIBLE_CODEPOINTS`.
    That is the set `escape_invisible` makes visible in tool output instead, minus the
    two joiners it keeps. For issue text, snippets and test ids deletion is right: nothing
    has to match them byte for byte.

    `\\r\\n` and a lone `\\r` become `\\n`: a bare carriage return is a line break
    to some renderers and not to others, which is a way to make what a reviewer
    sees differ from what the model reads.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch
        for ch in text
        if ch in _KEPT_CONTROLS
        or (unicodedata.category(ch) not in _DROPPED_CATEGORIES and not _is_extra_invisible(ch))
    )


def escape_invisible(text: str) -> str:
    """`text` with invisible and control characters shown as `\\u{hex}` instead of deleted.

    For tool output, which the model reads *and edits against*: a file whose bytes were
    silently altered would no longer match what `edit_file` is told to replace, so
    deletion is the wrong tool and escaping is right -- the model sees exactly that a
    character is there, and which. Escaped: control characters except newline, tab and
    carriage return (kept, so CRLF files read as they are), format characters except
    ZWJ and ZWNJ, surrogates, variation selectors (including the U+E0100 supplement), the
    tag block U+E0000-E007F, bidi controls, private use, and the characters in
    `_INVISIBLE_CODEPOINTS`. Ordinary non-ASCII text -- CJK, accented Latin, emoji
    joined by ZWJ -- is untouched.

    **Known cost:** an emoji presentation selector (U+FE0F, as in a warning sign) is a
    variation selector and is escaped too, so the model reads `\\u{fe0f}` after it. The
    alternative is to leave a channel for smuggling bytes open; a single selector
    after an emoji is not worth that.
    """
    out = []
    for ch in text:
        if ch in _KEPT_CONTROLS or ch == "\r" or ch in _JOINERS:
            out.append(ch)
        elif unicodedata.category(ch) in _DROPPED_CATEGORIES or _is_extra_invisible(ch):
            out.append(f"\\u{{{ord(ch):x}}}")
        else:
            out.append(ch)
    return "".join(out)


def neutralize_closing_tags(text: str) -> str:
    """`text` with the leading `<` of every closing tag of this package's families replaced by `‹`.

    **One `re.sub` pass, and it must stay one.** A replacement removes a `<` and
    adds none, and a match contains a `<` only at its start, so no replacement can
    create or complete a later match: there is nothing to iterate to. The earlier
    version deleted the tags to a fixpoint, which is quadratic on nested
    self-assembling input (`</is</issue>sue>` nested 8,000 deep took 5 s).
    """
    return _CLOSING_TAG.sub(lambda match: "\u2039" + match.group()[1:], text)


def truncate(text: str, limit: int) -> tuple[str, int]:
    """`text` cut to `limit` characters, and how many were dropped (0 if none)."""
    if limit < 0:
        raise ValueError(f"limit must not be negative, got {limit}")
    if len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit


#: How much of an untrusted string is looked at before the cut. A multiple of the limit,
#: because cleaning removes characters and the result is cut to the limit afterwards;
#: the part never looked at is still counted in the "truncated N chars" marker.
_PRECUT_FACTOR = 4
_PRECUT_SLACK = 1024


def _precut(text: str, limit: int) -> tuple[str, int]:
    """The head of `text` that cleaning will look at, and how many characters were left unseen."""
    keep = max(limit, 0) * _PRECUT_FACTOR + _PRECUT_SLACK
    return text[:keep], max(len(text) - keep, 0)


def clean_untrusted(text: str, limit: int) -> str:
    """Pre-cut, sanitise, neutralise closing tags, then cut to `limit` with a marker saying so.

    The pre-cut is what bounds the work: sanitising and the tag pass are linear, but
    linear in a megabyte is still a stall in the API process, and a retrieved
    snippet or a repository overview has no size limit of its own.
    """
    head, unseen = _precut(text, limit)
    text, dropped = truncate(neutralize_closing_tags(sanitize_text(head)), limit)
    dropped += unseen
    return f"{text}\n[truncated {dropped} chars]" if dropped else text


def data_block(tag: str, nonce: str, body: str, *, limit: int) -> str:
    """`body` as untrusted data, between `<tag-nonce>` and `</tag-nonce>`.

    `tag` is one of this package's own constants, never untrusted text, and must
    be in `_FAMILIES` -- a tag outside it would not have its closing form neutralised
    in `body`, which is the property the whole function exists to provide.
    """
    if tag not in _FAMILIES:
        raise ValueError(f"unknown delimiter family {tag!r}; add it to _FAMILIES so its closing tag is neutralised")
    check_nonce(nonce)
    name = f"{tag}-{nonce}" if nonce else tag
    return f"<{name}>\n{clean_untrusted(body, limit)}\n</{name}>"


def inline(text: str, limit: int) -> str:
    """One line of untrusted text -- a test id, a path -- bounded and without line breaks."""
    head, unseen = _precut(text, limit)
    cleaned = neutralize_closing_tags(sanitize_text(head)).replace("\n", " ").replace("\t", " ")
    cleaned, dropped = truncate(cleaned, limit)
    dropped += unseen
    return f"{cleaned}...[{dropped} more chars]" if dropped else cleaned


def bounded_list(items: Iterable[str], *, max_items: int, item_chars: int, indent: str = "  ") -> str:
    """Up to `max_items` lines, each `inline`d, then a line saying how many were left out."""
    items = list(items)
    shown = [f"{indent}{inline(item, item_chars)}" for item in items[:max_items]]
    if len(items) > max_items:
        shown.append(f"{indent}... and {len(items) - max_items} more")
    return "\n".join(shown)


def _clip_lines(text: str, max_lines: int) -> tuple[str, int]:
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text, 0
    return "\n".join(lines[:max_lines]), len(lines) - max_lines


def render_hits(hits: Sequence[SearchHit], *, max_hits: int, max_snippet_lines: int) -> str:
    """The top `max_hits` retrieved locations, each with a bounded snippet.

    A snippet is a slice of the repository's own source, so it is untrusted like
    everything else the repository holds; the caller wraps the whole result in a
    data block, and each snippet is cleaned here as well so one cannot carry a
    closing tag out of its own entry. The header fields (`file_path`, `symbol`)
    come from the index and are cleaned as inline text for the same reason.
    """
    if max_hits < 0 or max_snippet_lines < 0:
        raise ValueError("max_hits and max_snippet_lines must not be negative")
    entries = []
    for hit in hits[:max_hits]:
        snippet, hidden_lines = _clip_lines(sanitize_text(_precut(hit.snippet, MAX_SNIPPET_CHARS)[0]), max_snippet_lines)
        snippet = clean_untrusted(snippet, MAX_SNIPPET_CHARS)
        if hidden_lines:
            snippet += f"\n[{hidden_lines} more lines not shown]"
        header = (
            f"{inline(hit.file_path, 200)}:{hit.start_line}-{hit.end_line} "
            f"({inline(hit.chunk_type, 40)} {inline(hit.symbol, 120)})"
        )
        entries.append(f"## {header}\n{snippet}")
    if len(hits) > max_hits:
        entries.append(f"[{len(hits) - max_hits} further locations not shown; use search_code to find more]")
    return "\n\n".join(entries) if entries else "(retrieval found nothing for this issue)"
