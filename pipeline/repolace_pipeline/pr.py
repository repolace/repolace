"""The words repolace puts on GitHub: PR title, commit message and PR body.

Everything here is a pure function over plain data, because the interesting
failures are in the text and a function that renders text is the one thing in
this pipeline that can be tested without a database, a clone or a daemon.

**Three kinds of text go through here, and they are not equally trusted.**

* *repolace's own words* -- section headings, "Verified", "Not verified". Plain.
* *Facts it measured* -- counts, file paths, test ids, a cost. Written in code
  spans, with backticks removed, so a name cannot open markup of its own.
* *The agent's summary*, which is model output written after reading text anyone
  can file on a public repository. It is quoted as the agent's claim, inside a
  fenced block, **and** sanitised first. Either alone would be enough against
  most inputs; both are here because the failure is a mention that pings a real
  person or team, an issue reference that notifies a real upstream project, or an
  image whose URL carries data out to a server an attacker chose. None of those
  is visible in the diff, and all of them are sent the moment the PR opens.

**The two modes differ in what may appear at all.** A product PR says
`Refs #N`, never `Fixes #N` -- nobody yet knows how often the fix is right, and
merging a PR that closed a real issue it did not fix is the error to avoid. A
benchmark PR (the task has an `instance_id`) has no `Refs`, no URL and no
`#`-reference anywhere, in the title, the commit message or the body: `#N` in a
bench repository resolves to an unrelated object, and an upstream URL posts a
backlink on the real upstream issue. It also carries no outcome-bearing text in
the commit message: that is pushed to a repository the next run clones in full.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from verify.protocol import SuiteResult
from verify.scoring import Score, Verdict

from repolace_agents.render import sanitize_text
from repolace_pipeline.context import RetrievedChunk

#: Inserted after the character that would make GitHub act on a reference. It
#: renders as nothing and stops the autolinker, while the text stays readable and
#: greppable in the raw body.
ZWSP = "​"

MAX_SUMMARY_CHARS = 2000
MAX_TITLE_CHARS = 120
MAX_FILES_LISTED = 30
MAX_IDS_LISTED = 5
MAX_LOCATIONS = 5
MAX_NAME_CHARS = 200
MAX_REASON_CHARS = 400

PLUMBING_SUMMARY_NOTE = (
    "repolace: NOT A FIX. This change was written by the deterministic stub editor, with no model "
    "involved. It proves only that a task can clone, index, retrieve, edit, verify, squash, push and "
    "open a pull request. Please close this pull request."
)

_IMAGE = re.compile(r"!\[[^\]]{0,300}\]\([^)]{0,1000}\)")
_INLINE_LINK = re.compile(r"\[([^\]]{0,300})\]\([^)]{0,1000}\)")
#: `[label]: url` at the start of a line. Broken rather than deleted: `[WARNING]: ...` is
#: also an ordinary sentence, and the definition needs the `]:` to be adjacent.
_REFERENCE_DEFINITION = re.compile(r"^([ \t]*\[[^\]\n]{1,200}\])(?=:)", re.MULTILINE)
#: A well-formed tag (`<img src=//e>`, `</b>`, `<br/>`), a comment, a declaration, a
#: processing instruction, or an autolink (`<https://x>`, `<a@b.c>`). Well-formed only,
#: so prose such as `if x<limit and y > z` -- which a looser "anything between angle
#: brackets" rule would delete up to the `>` -- is left alone; a `<` that is left over is
#: broken by `_OPEN_ANGLE` instead, so nothing that was not matched can still open a tag.
_ATTRIBUTE = r"""\s+[\w:.-]+(?:\s*=\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s"'=<>`]+))?"""
_HTML = re.compile(
    rf"<\s*/?\s*[A-Za-z][\w:-]*(?:{_ATTRIBUTE})*\s*/?>"
    r"|<!--[^\n]*?-->|<![A-Za-z][^<>\n]*>|<\?[^<>\n]*\?>"
    r"|<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>"
    r"|<[^\s<>@]+@[^\s<>]+>"
)
_OPEN_ANGLE = re.compile(r"<(?=[ \t]*[A-Za-z/!?])")
#: `&commat;`, `&#x40;`, `&num;`, `&#35;`: a character reference renders as the character it names, so an
#: entity is a way to spell `@org` or `#123` that none of the rules below can see.
_ENTITY = re.compile(r"&(?=#?[A-Za-z0-9]+;)")
_KEYWORD_BEFORE_REFERENCE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(?=[\s:]*(?:[\w.-]+/[\w.-]+)?#\d)", re.IGNORECASE
)
_URL_SCHEME = re.compile(r"://")
_WWW = re.compile(r"\bwww\.", re.IGNORECASE)
_MENTION = re.compile(r"@(?=\w)")
_ISSUE_REFERENCE = re.compile(r"#(?=\d)")
_GH_REFERENCE = re.compile(r"\bGH-(?=\d)", re.IGNORECASE)


def sanitize_markdown(text: str, *, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """`text` made inert as markdown: nothing in it can link, notify, embed or close an issue.

    In this order, each step for a reason:

    0. *Pre-cut*, so the work below is bounded by what will be shown rather than
       by what was supplied.
    1. Control, bidi and other invisible characters go (`sanitize_text`): a right-
       to-left override is how text reads one way in the diff and another in the
       browser. Done first, because it can sit between a `@` and a name.
    2. **Images are removed** (`![alt](url)`), and any `![` left over -- a
       reference-style or malformed one -- has its `[` broken. An image is the one
       markdown element GitHub fetches on the reader's behalf, through its own
       proxy, with whatever is in the URL.
    3. **Links lose their destination** (`[text](url)` becomes `text`), any `](` left over
       -- a nested bracket the pattern cannot match -- is broken so it links to nothing, reference
       definitions are broken so they define nothing, and a bare `scheme://` or `www.` is broken so it
       does not autolink. All links, not just images: a phishing link in a PR the
       tool opened carries the tool's name.
    4. **Raw HTML is stripped**, autolinks (`<https://...>`) included; a `<` left
       without its `>` has the character after it broken so it cannot open a tag.
    5. **Character references are broken** (`&commat;`, `&#x40;`, `&num;`): they render as the
       `@` or `#` they name, past every rule below.
    6. **`@name` and `@org/team` are broken**, wherever they sit. Not just after
       whitespace: the characters before the `@` were controlled by whoever wrote
       the text, and step 1 may have just removed the one that made it look like
       an email.
    7. **Issue references are broken**: `#123`, `owner/repo#123`, `GH-123`, and
       the closing keywords (`Fixes`, `Closes`, `Resolves` and their tenses) when
       a reference follows, which would otherwise close a real issue on merge.
    8. Cut to `max_chars`, with a marker saying how much was dropped.

    The output keeps its newlines; callers that need one line collapse them.
    """
    precut = max_chars * 4 + 1024
    dropped_unseen = max(len(text) - precut, 0)
    text = sanitize_text(text[:precut])

    text = _IMAGE.sub("", text)
    text = text.replace("![", f"!{ZWSP}[")
    text = _INLINE_LINK.sub(r"\1", text)
    # What the link rules could not match -- a nested bracket, `[a[b]c](url)` -- is still a link
    # to a renderer, and `](` is what makes it one. Breaking the pair is robust where parsing is not.
    text = text.replace("](", f"]{ZWSP}(")
    text = _REFERENCE_DEFINITION.sub(lambda m: m.group(1) + ZWSP, text)
    text = _URL_SCHEME.sub(f":{ZWSP}//", text)
    text = _WWW.sub(f"www{ZWSP}.", text)
    text = _HTML.sub("", text)
    text = _OPEN_ANGLE.sub(f"<{ZWSP}", text)
    text = _ENTITY.sub(f"&{ZWSP}", text)
    text = _MENTION.sub(f"@{ZWSP}", text)
    # Before the reference rule, which would hide the `#123` the keyword rule looks for.
    text = _KEYWORD_BEFORE_REFERENCE.sub(lambda m: m.group(0)[0] + ZWSP + m.group(0)[1:], text)
    text = _ISSUE_REFERENCE.sub(f"#{ZWSP}", text)
    text = _GH_REFERENCE.sub(lambda m: m.group(0) + ZWSP, text)

    text = text.strip()
    cut = max(len(text) - max_chars, 0)
    if cut or dropped_unseen:
        text = text[:max_chars].rstrip() + f"\n[truncated {cut + dropped_unseen} characters]"
    return text


def _one_line(text: str, limit: int) -> str:
    """A single sanitised line, for a title or a table cell, cut with an ellipsis.

    Sanitised with room to spare and cut afterwards: `sanitize_markdown`'s own
    truncation marker is a sentence, and a title ending in `[truncated 50
    characters]` is worse than one ending in an ellipsis.
    """
    cleaned = " ".join(sanitize_markdown(text, max_chars=limit * 4).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "\u2026"


def _code(text: str, limit: int = MAX_NAME_CHARS) -> str:
    """`text` as an inline code span that nothing inside can end.

    Backticks are replaced rather than escaped (a code span has no escape), and the
    controls and invisibles go first. Not run through `sanitize_markdown`: inside a
    code span a mention, an issue number and a tag are all inert, and breaking them
    would corrupt a path or a test id the reader wants to copy.
    """
    cleaned = " ".join(sanitize_text(text).split()).replace("`", "'")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return f"`{cleaned}`"


def _fenced(text: str) -> str:
    """`text` in a fenced code block whose fence is longer than any backtick run inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


@dataclass(frozen=True)
class PrFacts:
    """Everything the PR text is rendered from. Plain data: no ORM row, no suite object graph."""

    task_id: uuid.UUID
    issue_number: int
    #: UNTRUSTED. Only ever shown sanitised, and never in a benchmark PR at all.
    issue_title: str
    #: Set exactly when the task is a benchmark task -- the one signal of benchmark mode.
    instance_id: str | None
    #: The task ran the deterministic stub editor, not an agent. Its PR says so, loudly.
    plumbing_only: bool
    #: The agent's own account. UNTRUSTED model output. None when it never submitted.
    summary: str | None
    changed_files: tuple[str, ...]
    baseline: SuiteResult
    final: SuiteResult
    verdict: Verdict
    scored: Score
    #: The curated fail-to-pass list of a benchmark instance; None in product mode.
    expected_fail_to_pass: tuple[str, ...] | None
    attempts: int
    stop_reason: str | None
    #: The first model the task called, or None when no model ran.
    model: str | None
    cost_usd: Decimal | None
    retrieved: tuple[RetrievedChunk, ...]

    @property
    def benchmark(self) -> bool:
        return self.instance_id is not None


def pr_title(facts: PrFacts) -> str:
    if facts.benchmark:
        # No upstream text at all: an issue title can carry `#123`, and nothing here needs it.
        return f"[repolace] SWE-bench instance {facts.instance_id}"
    if facts.plumbing_only:
        return f"[repolace] plumbing smoke test for issue #{facts.issue_number}"
    title = _one_line(facts.issue_title, MAX_TITLE_CHARS) or f"issue {facts.issue_number}"
    return f"[repolace] {title}"


def commit_message(facts: PrFacts) -> str:
    """The squashed commit's message, which is pushed and so is visible to later runs.

    Carries no outcome: not the score, not a count, not the agent's summary. In
    benchmark mode the commit stays reachable in a repository the next run of the
    same instance clones in full, so a message that said "all expected tests pass"
    would be an answer key in the object store. In product mode the summary is left
    out for a different reason -- a commit message is rendered by GitHub like any
    other text, and the summary is the one string here an attacker can influence.
    """
    task = f"Task {facts.task_id.hex}."
    if facts.benchmark:
        return f"repolace: attempt on SWE-bench instance {facts.instance_id}\n\n{task}"
    if facts.plumbing_only:
        return (
            f"repolace stub edit for issue #{facts.issue_number}\n\n"
            f"Not a fix. Written by the deterministic stub editor to verify the task pipeline "
            f"end to end. {task}"
        )
    title = _one_line(facts.issue_title, MAX_TITLE_CHARS) or f"issue {facts.issue_number}"
    return f"repolace: {title}\n\nRefs #{facts.issue_number}\n\n{task}"


def _counts(result: SuiteResult) -> str:
    return (
        f"{len(result.passed)} passed, {len(result.failed)} failed, "
        f"{len(result.skipped) + len(result.xfailed)} skipped or xfailed"
    )


def _ids(ids: Sequence[str]) -> str:
    listed = ", ".join(_code(i) for i in ids[:MAX_IDS_LISTED])
    more = len(ids) - MAX_IDS_LISTED
    return listed + (f" and {more} more" if more > 0 else "")


def _section_summary(facts: PrFacts) -> str:
    if facts.plumbing_only:
        body = _fenced(sanitize_markdown(PLUMBING_SUMMARY_NOTE))
    elif facts.summary is None or not facts.summary.strip():
        return "## Summary\n\n_The agent did not submit a summary._\n"
    else:
        body = _fenced(sanitize_markdown(facts.summary))
    return (
        "## Summary\n\n"
        "The agent's own account of its change, quoted. It is a claim made by a model, "
        "not something repolace checked.\n\n"
        f"{body}\n"
    )


def _section_files(facts: PrFacts) -> str:
    lines = [f"- {_code(path)}" for path in facts.changed_files[:MAX_FILES_LISTED]]
    more = len(facts.changed_files) - MAX_FILES_LISTED
    if more > 0:
        lines.append(f"- ... and {more} more")
    return "## Files changed\n\n" + ("\n".join(lines) if lines else "_None._") + "\n"


def _verified_product(facts: PrFacts) -> list[str]:
    lines = [f"- Before the change: {_counts(facts.baseline)}."]
    if facts.final.error:
        lines.append("- After the change: the suite run did not produce a usable result.")
    else:
        lines.append(f"- After the change: {_counts(facts.final)}.")

    verdict = facts.verdict
    lines.append(f"- Regressions: {_ids(verdict.regressions)}." if verdict.regressions else "- Regressions: none.")
    lines.append(
        f"- Baseline failures silenced rather than fixed: {_ids(verdict.neutralized)}."
        if verdict.neutralized
        else "- Baseline failures silenced rather than fixed: none."
    )
    lines.append(
        f"- Modules that no longer collect: {_ids(verdict.new_collect_failures)}."
        if verdict.new_collect_failures
        else "- New collection errors: none."
    )
    lines.append(
        f"- Test or configuration files changed: {_ids(verdict.disqualified)}."
        if verdict.disqualified
        else "- Test or configuration files changed: none."
    )
    if not verdict.ok:
        lines.append(
            # A code span, like every other measured fact: the reason carries test ids and may carry
            # sandbox error text, which came from a process that ran repository code.
            f"- **repolace's own checks flagged this change:** {_code(verdict.reason, MAX_REASON_CHARS)}"
        )
    return lines


def _verified_benchmark(facts: PrFacts) -> list[str]:
    """Counts from the `Score`, never ids: in a benchmark an id is the oracle."""
    scored = facts.scored
    lines = [
        f"- Fail-to-pass tests newly passing: {len(scored.fail_to_pass)}.",
        f"- Regressions: {len(scored.regressions) or 'none'}.",
        f"- Baseline failures silenced rather than fixed: {len(scored.neutralized) or 'none'}.",
        f"- Test or configuration files changed: {len(scored.disqualified) or 'none'}.",
    ]
    expected = facts.expected_fail_to_pass
    if expected is not None:
        passing = set(facts.final.passed)
        lines.append(
            f"- Curated fail-to-pass tests passing: {sum(1 for e in expected if e in passing)} of {len(expected)}."
        )
    return lines


def _section_verified(facts: PrFacts) -> str:
    lines = _verified_benchmark(facts) if facts.benchmark else _verified_product(facts)
    return "## Verified\n\n" + "\n".join(lines) + "\n"


def _section_not_verified(facts: PrFacts) -> str:
    if facts.benchmark:
        lines = [
            "- Nothing beyond the curated tests and the repository's own suite was run; "
            "a pass is not a judgement of the change's quality.",
        ]
    else:
        lines = [
            "- **No fail-to-pass evidence:** the repository had no failing test for this issue, so "
            "nothing here shows the issue is fixed. A clean run means no regression was found, and nothing more.",
        ]
    lines.append("- No person has reviewed this change.")
    return "## Not verified\n\n" + "\n".join(lines) + "\n"


def _section_run(facts: PrFacts) -> str:
    cost = f"${facts.cost_usd:.4f}" if facts.cost_usd is not None else "not measured"
    rows = [
        ("attempts", str(facts.attempts)),
        ("model", _code(facts.model) if facts.model else "none called"),
        ("cost", cost),
        ("stopped because", _code(facts.stop_reason) if facts.stop_reason else "-"),
    ]
    table = "\n".join(f"| {name} | {value} |" for name, value in rows)
    return f"## Run\n\n| | |\n|---|---|\n{table}\n"


def _section_retrieved(facts: PrFacts) -> str:
    if not facts.retrieved:
        return "## Retrieved context\n\n_No chunks retrieved._\n"
    rows = [
        f"| {i} | {_code(c.location)} | {_code(c.qualified_symbol)} |"
        for i, c in enumerate(facts.retrieved[:MAX_LOCATIONS], start=1)
    ]
    return "## Retrieved context\n\n| # | location | symbol |\n|---|---|---|\n" + "\n".join(rows) + "\n"


def _footer(facts: PrFacts) -> str:
    if facts.benchmark:
        reference = f"SWE-bench instance {facts.instance_id}"
    else:
        # `Refs`, never `Fixes`: see the module docstring.
        reference = f"Refs #{facts.issue_number}"
    return f"---\n\n{reference}\n\n_Opened automatically by repolace for task `{facts.task_id}`._\n"


def render_pr_body(facts: PrFacts) -> str:
    """The PR description. See the module docstring for what may and may not appear in it."""
    return "\n".join(
        [
            _section_summary(facts),
            _section_files(facts),
            _section_verified(facts),
            _section_not_verified(facts),
            _section_run(facts),
            _section_retrieved(facts),
            _footer(facts),
        ]
    )
