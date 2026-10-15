"""Unified diff -> a structure the template can render, with Pygments highlighting.

`tasks.patch_diff` is attacker-influenced text (the agent wrote it, from a
repository anyone may have written). Nothing here returns text that a template
has to trust:

* every line of the diff is escaped by `markupsafe.escape` before it is wrapped
  in markup, whether or not it was highlighted;
* the markup around it (`<span class="k">`) is built here from a fixed table of
  class names, never from the diff and never from Pygments' own HTML formatter;
* the result is a `Markup`, so a template prints it without `|safe`.

The parser is driven by the hunk header's line counts, not by prefixes, so a
removed line that reads `-- foo` is not mistaken for a `--- a/file` header.
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from markupsafe import Markup, escape
from pygments import lex
from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, Keyword, Name, Number, String, _TokenType
from pygments.util import ClassNotFound

#: Rendering cap, from the design spec: past either, the rest is dropped and said so.
MAX_LINES = 2000
MAX_BYTES = 300_000
#: A single long line (a minified file) is shown escaped but not highlighted.
MAX_HIGHLIGHT_LINE = 300
#: Total wall time one diff may spend in Pygments; past it every remaining line is plain.
HIGHLIGHT_BUDGET_SECONDS = 2.0

#: Only these extensions are lexed. The file name is chosen by the agent, and some
#: Pygments lexers go quadratic on a short line (an 800-character `.ly` line took
#: 9 s), so picking a lexer from an arbitrary name would hand it a CPU-burn knob.
LEXERS: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx", ".json": "json",
    ".md": "markdown", ".toml": "toml", ".yml": "yaml", ".yaml": "yaml", ".sh": "bash",
    ".css": "css", ".html": "html", ".go": "go", ".rs": "rust", ".sql": "sql", ".txt": "text",
}  # fmt: skip

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    kind: str  # add | del | ctx | hunk | meta
    old: int | None
    new: int | None
    sign: str
    html: Markup


@dataclass
class DiffFile:
    path: str
    status: str = "modified"  # modified | added | deleted | renamed
    added: int = 0
    deleted: int = 0
    binary: bool = False
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class ParsedDiff:
    files: list[DiffFile]
    added: int
    deleted: int
    truncated_lines: int = 0  # how many diff lines were dropped by the cap


def _token_class(ttype: _TokenType) -> str | None:
    if ttype in Comment:
        return "cm"
    if ttype in String:
        return "s"
    if ttype in Number:
        return "nu"
    if ttype in Keyword or ttype in Name.Tag:
        return "k"
    if ttype in Name.Function or ttype in Name.Class or ttype in Name.Builtin or ttype in Name.Decorator or ttype in Name.Attribute:
        return "f"
    return None


def highlight_line(text: str, filename: str, deadline: float | None = None) -> Markup:
    """One line of code as escaped markup; highlighted only for an allowlisted extension and inside the budget."""
    ext = PurePosixPath(filename).suffix.lower()
    name = LEXERS.get(ext)
    if name is None or len(text) > MAX_HIGHLIGHT_LINE or (deadline is not None and time.monotonic() > deadline):
        return Markup(escape(text))
    try:
        lexer = get_lexer_by_name(name, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return Markup(escape(text))
    parts: list[Markup] = []
    for ttype, value in lex(text, lexer):
        cls = _token_class(ttype)
        escaped = escape(value)
        parts.append(Markup('<span class="{}">{}</span>').format(cls, escaped) if cls else Markup(escaped))
    return Markup("").join(parts)


def _path_from_git_header(line: str) -> str:
    # `diff --git a/x b/y`: split on the last ` b/`, which survives spaces in x.
    rest = line[len("diff --git ") :]
    idx = rest.rfind(" b/")
    if idx != -1:
        return rest[idx + 3 :]
    return rest


def _strip_ab(path: str) -> str:
    path = path.split("\t", 1)[0]
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_diff(text: str, budget_seconds: float = HIGHLIGHT_BUDGET_SECONDS) -> ParsedDiff:
    """Parse a unified diff. Never raises on odd input: unrecognised lines are shown as meta."""
    deadline = time.monotonic() + budget_seconds
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    files: list[DiffFile] = []
    current: DiffFile | None = None
    old_no = new_no = 0
    old_left = new_left = 0
    kept = 0
    size = 0
    dropped = 0

    def meta(line: str, file: DiffFile) -> None:
        file.lines.append(DiffLine("meta", None, None, " ", Markup(escape(line))))

    for index, line in enumerate(raw_lines):
        size += len(line) + 1
        if kept >= MAX_LINES or size > MAX_BYTES:
            dropped = len(raw_lines) - index
            break
        kept += 1

        in_hunk = current is not None and (old_left > 0 or new_left > 0)
        if in_hunk and current is not None:
            if line.startswith("\\"):
                meta(line, current)
                continue
            marker = line[:1]
            body = line[1:]
            if marker == "+":
                current.lines.append(DiffLine("add", None, new_no, "+", highlight_line(body, current.path, deadline)))
                current.added += 1
                new_no += 1
                new_left -= 1
                continue
            if marker == "-":
                current.lines.append(DiffLine("del", old_no, None, "-", highlight_line(body, current.path, deadline)))
                current.deleted += 1
                old_no += 1
                old_left -= 1
                continue
            if marker == " " or line == "":
                current.lines.append(DiffLine("ctx", old_no, new_no, " ", highlight_line(body, current.path, deadline)))
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
                continue
            # Counts disagreed with the text: leave the hunk and reparse this line as a header.
            old_left = new_left = 0

        if line.startswith("diff --git "):
            current = DiffFile(path=_path_from_git_header(line))
            files.append(current)
            continue
        if line.startswith("--- "):
            # A bare patch (no `diff --git` line) starts a file at `---`; after a
            # per-file header it is that file's own header and changes nothing.
            if current is None or any(ln.kind != "meta" for ln in current.lines):
                current = DiffFile(path=_strip_ab(line[4:]))
                files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("+++ "):
            target = line[4:]
            if target != "/dev/null":
                current.path = _strip_ab(target)
            continue
        if line.startswith("new file mode"):
            current.status = "added"
            continue
        if line.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if line.startswith("rename from "):
            current.status = "renamed"
            continue
        if line.startswith("rename to "):
            current.path = line[len("rename to ") :]
            continue
        if line.startswith(("Binary files ", "GIT binary patch")):
            current.binary = True
            meta(line, current)
            continue
        match = _HUNK.match(line)
        if match:
            old_no = int(match.group(1))
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_no = int(match.group(3))
            new_left = int(match.group(4)) if match.group(4) is not None else 1
            current.lines.append(DiffLine("hunk", None, None, " ", Markup(escape(line))))
            continue
        # index, mode, similarity ...: not shown (the file header already says what matters)

    return ParsedDiff(
        files=files,
        added=sum(f.added for f in files),
        deleted=sum(f.deleted for f in files),
        truncated_lines=dropped,
    )
