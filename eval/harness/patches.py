"""Reading a unified git diff: which files it touches, and which OLD lines it changes.

Pure, and deliberately strict. `select_instances` decides whether an instance is
admissible from this module's answer (a test patch that deletes or renames a file
is refused, a gold patch that touches a test path is refused), so a parser that
silently skips a block it did not understand would let a bad instance through
with a clean-looking reason. Anything it cannot read is a `PatchError`.

**The diff is parsed as a state machine, not grepped.** A hunk body is consumed
by the counts in its `@@` header, so a context or removed line that happens to
read `--- a/x`, `+++ b/y` or even `diff --git ...` (a patch of a patch file, a
changelog quoting a diff) is content, never a new file. Splitting is on `\\n`
only: `str.splitlines()` also breaks on form feeds and `\\x85`, both of which
occur inside real Python source and would shift every count after them.

**Paths are read from the lines git writes them most exactly on.** `rename
from/to` first, then `---`/`+++`, and the `diff --git` line only when nothing
else names the file (a binary or mode-only block). That line is ambiguous for an
unquoted name containing a space (git does not quote spaces there), which is
why it comes last.

**What an old-side range is.** `old_side_hunks` reports the *changed* old lines,
not the hunk header's span: the header includes three lines of context on each
side, and a range that wide would overlap the neighbouring function and make
"the gold patch touches this chunk" true of code the patch never changed -- which
inflates a retrieval recall number. A run of removed lines is the inclusive range
of those lines. A pure insertion removes nothing, so it is anchored to the old
line it *follows* (a single-line range; line 1 for an insertion at the top of the
file): inserting a statement at the end of a function belongs to that function.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ADDED = "added"
MODIFIED = "modified"
DELETED = "deleted"
RENAMED = "renamed"
BINARY = "binary"


class PatchError(ValueError):
    """The text is not a diff this module can read without guessing."""


@dataclass(frozen=True)
class FileChange:
    #: The path after the change; for a deleted file, the path it had.
    path: str
    #: One of `added`, `modified`, `deleted`, `renamed`, `binary`. A binary file is
    #: `binary` whatever was done to it: nothing downstream can use its bytes as text.
    status: str
    #: Only for a rename (and a copy, which is `added`): where the content came from.
    old_path: str | None = None


@dataclass
class _FilePatch:
    change: FileChange
    old_ranges: list[tuple[int, int]] = field(default_factory=list)


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_C_ESCAPES = {
    "a": 0x07, "b": 0x08, "f": 0x0C, "n": 0x0A, "r": 0x0D, "t": 0x09, "v": 0x0B,
    "\\": 0x5C, '"': 0x22,
}


def _unquote(token: str) -> str:
    """Undo git's C-style path quoting: `"caf\\303\\251.py"` is `café.py`.

    Octal escapes are *bytes* (git quotes the UTF-8 encoding, one escape per
    byte), so they are collected and decoded together. A sequence that is not
    valid UTF-8 is refused: decoding it loosely would name a different file.
    """
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        raise PatchError(f"malformed quoted path: {token!r}")
    inner = token[1:-1]
    out = bytearray()
    i = 0
    while i < len(inner):
        char = inner[i]
        if char != "\\":
            out += char.encode("utf-8")
            i += 1
            continue
        i += 1
        if i >= len(inner):
            raise PatchError(f"quoted path ends in a lone backslash: {token!r}")
        escape = inner[i]
        if escape in _C_ESCAPES:
            out.append(_C_ESCAPES[escape])
            i += 1
        elif escape in "01234567":
            digits = inner[i : i + 3]
            if len(digits) != 3 or any(d not in "01234567" for d in digits):
                raise PatchError(f"bad octal escape in quoted path: {token!r}")
            value = int(digits, 8)
            if value > 0xFF:
                raise PatchError(f"octal escape out of range in quoted path: {token!r}")
            out.append(value)
            i += 3
        else:
            raise PatchError(f"unknown escape \\{escape} in quoted path: {token!r}")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"quoted path is not valid UTF-8: {token!r}") from exc


def _quoted_prefix(text: str) -> tuple[str, str]:
    """Split a leading `"..."` token off `text`, honouring `\\"` inside it."""
    i = 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return text[: i + 1], text[i + 1 :]
        i += 1
    raise PatchError(f"unterminated quoted path: {text!r}")


def _path_value(raw: str) -> str:
    """A path as it stands on a `rename`/`copy`/`---`/`+++` line (quoted or bare)."""
    return _unquote(raw) if raw.startswith('"') else raw


def _strip_side_prefix(path: str, prefix: str, where: str) -> str:
    # A diff written with `--no-prefix` cannot be told from one with a directory
    # literally named `a`, so it is refused rather than guessed at.
    if not path.startswith(prefix):
        raise PatchError(f"{where}: path {path!r} lacks the {prefix!r} prefix a git diff carries")
    return path[len(prefix) :]


def _header_paths(rest: str) -> tuple[str, str]:
    """The two paths on a `diff --git` line, prefixes stripped.

    The last resort (see the module docstring). For a block that is not a rename
    both sides are the same path, which resolves the unquoted-space ambiguity
    exactly: `a/<P> b/<P>` has length `2*len(P) + 5`.
    """
    if rest.startswith('"'):
        a_token, remainder = _quoted_prefix(rest)
        a_raw = _unquote(a_token)
        b_raw = _path_value(remainder.lstrip(" "))
    else:
        quoted_b = rest.find(' "b/')
        if quoted_b != -1 and rest.endswith('"'):
            a_raw, b_raw = rest[:quoted_b], _unquote(rest[quoted_b + 1 :])
        else:
            length = len(rest) - 5
            if length >= 0 and length % 2 == 0:
                half = length // 2
                candidate = rest[2 : 2 + half]
                if rest == f"a/{candidate} b/{candidate}":
                    return candidate, candidate
            split = rest.find(" b/")
            if split == -1:
                raise PatchError(f"cannot read the paths on 'diff --git {rest}'")
            a_raw, b_raw = rest[:split], rest[split + 1 :]
    where = f"diff --git {rest}"
    return _strip_side_prefix(a_raw, "a/", where), _strip_side_prefix(b_raw, "b/", where)


def _file_header_path(line: str, marker: str, prefix: str) -> str | None:
    """The path on a `--- `/`+++ ` line, or None for `/dev/null`."""
    raw = line[len(marker) :].rstrip("\r")
    if raw.startswith('"'):
        # git appends a TAB after a name containing whitespace even when it also
        # quotes it, so the token ends at the closing quote, not at the line end.
        raw = _quoted_prefix(raw)[0]
    else:
        # A name containing whitespace is followed by a TAB (and sometimes a
        # timestamp); a literal tab inside a name would have been quoted.
        raw = raw.split("\t", 1)[0]
    if raw == "/dev/null":
        return None
    return _strip_side_prefix(_path_value(raw), prefix, line)


def _parse_hunks(lines: list[str], start: int, ranges: list[tuple[int, int]]) -> int:
    """Consume consecutive hunks, appending old-side ranges; return the next index."""
    i = start
    n = len(lines)
    while i < n:
        match = _HUNK_HEADER.match(lines[i])
        if match is None:
            break
        old_start = int(match[1])
        old_left = 1 if match[2] is None else int(match[2])
        new_left = 1 if match[4] is None else int(match[4])
        i += 1
        # git writes `-N,0` for "after line N" (and `-0,0` for a new file), so the
        # next old line is N + 1, not N.
        next_old = old_start if old_left > 0 else old_start + 1

        run_open = False
        run_anchor = 0
        run_first: int | None = None
        run_last: int | None = None

        def close_run() -> None:
            nonlocal run_open, run_first, run_last
            if not run_open:
                return
            if run_first is not None and run_last is not None:
                ranges.append((run_first, run_last))
            else:
                anchor = max(run_anchor, 1)
                ranges.append((anchor, anchor))
            run_open, run_first, run_last = False, None, None

        while old_left > 0 or new_left > 0:
            if i >= n:
                raise PatchError(f"hunk at old line {old_start} is truncated: the diff ends inside it")
            line = lines[i]
            tag = line[:1]
            i += 1
            if tag == "\\":
                # "\ No newline at end of file": about the line above, never a line.
                continue
            if tag == "-":
                if old_left <= 0:
                    raise PatchError(f"hunk at old line {old_start} has more '-' lines than its header says")
                if not run_open:
                    run_open, run_anchor = True, next_old - 1
                if run_first is None:
                    run_first = next_old
                run_last = next_old
                next_old += 1
                old_left -= 1
            elif tag == "+":
                if new_left <= 0:
                    raise PatchError(f"hunk at old line {old_start} has more '+' lines than its header says")
                if not run_open:
                    run_open, run_anchor = True, next_old - 1
                new_left -= 1
            elif tag in (" ", ""):
                # A bare empty line is a context line whose single space was
                # stripped (an editor, a mail gateway); it still counts as one.
                if old_left <= 0 or new_left <= 0:
                    raise PatchError(f"hunk at old line {old_start} has more context than its header says")
                close_run()
                next_old += 1
                old_left -= 1
                new_left -= 1
            else:
                raise PatchError(f"unexpected line inside the hunk at old line {old_start}: {line[:60]!r}")
        close_run()
        while i < n and lines[i].startswith("\\"):
            i += 1
    return i


def _parse_block(lines: list[str], start: int) -> tuple[_FilePatch, int]:
    header_rest = lines[start][len("diff --git ") :].rstrip("\r")
    n = len(lines)
    i = start + 1

    old_path: str | None = None
    new_path: str | None = None
    saw_old_header = saw_new_header = False
    rename_from = rename_to = copy_from = copy_to = None
    new_file = deleted_file = binary = False
    ranges: list[tuple[int, int]] = []
    in_header = True

    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            break
        if _HUNK_HEADER.match(line):
            in_header = False
            i = _parse_hunks(lines, i, ranges)
            continue
        if not in_header:
            i += 1
            continue
        clean = line.rstrip("\r")
        if clean.startswith("--- "):
            saw_old_header, old_path = True, _file_header_path(clean, "--- ", "a/")
        elif clean.startswith("+++ "):
            saw_new_header, new_path = True, _file_header_path(clean, "+++ ", "b/")
        elif clean.startswith("new file mode "):
            new_file = True
        elif clean.startswith("deleted file mode "):
            deleted_file = True
        elif clean.startswith("rename from "):
            rename_from = _path_value(clean[len("rename from ") :])
        elif clean.startswith("rename to "):
            rename_to = _path_value(clean[len("rename to ") :])
        elif clean.startswith("copy from "):
            copy_from = _path_value(clean[len("copy from ") :])
        elif clean.startswith("copy to "):
            copy_to = _path_value(clean[len("copy to ") :])
        elif clean.startswith("Binary files ") or clean == "GIT binary patch":
            binary = True
        i += 1

    if (rename_from is None) != (rename_to is None) or (copy_from is None) != (copy_to is None):
        raise PatchError(f"unpaired rename/copy header in 'diff --git {header_rest}'")

    # `--- /dev/null` / `+++ /dev/null` say the same as the mode lines; trust either.
    if saw_old_header and old_path is None:
        new_file = True
    if saw_new_header and new_path is None:
        deleted_file = True
    if new_file and deleted_file:
        raise PatchError(f"'diff --git {header_rest}' is both a new file and a deleted file")

    if rename_to is not None:
        change = FileChange(path=rename_to, status=BINARY if binary else RENAMED, old_path=rename_from)
        return _FilePatch(change, ranges if not binary else []), i
    if copy_to is not None:
        change = FileChange(path=copy_to, status=BINARY if binary else ADDED, old_path=copy_from)
        return _FilePatch(change, []), i

    if saw_old_header or saw_new_header:
        a_path, b_path = old_path, new_path
        if a_path is not None and b_path is not None and a_path != b_path:
            raise PatchError(f"paths differ without a rename header in 'diff --git {header_rest}'")
    else:
        header_a, header_b = _header_paths(header_rest)
        a_path = None if new_file else header_a
        b_path = None if deleted_file else header_b

    if binary:
        path = b_path if b_path is not None else a_path
        if path is None:
            raise PatchError(f"cannot name the binary file in 'diff --git {header_rest}'")
        return _FilePatch(FileChange(path=path, status=BINARY)), i
    if deleted_file:
        if a_path is None:
            raise PatchError(f"cannot name the deleted file in 'diff --git {header_rest}'")
        return _FilePatch(FileChange(path=a_path, status=DELETED), ranges), i
    if new_file:
        if b_path is None:
            raise PatchError(f"cannot name the new file in 'diff --git {header_rest}'")
        return _FilePatch(FileChange(path=b_path, status=ADDED)), i
    if b_path is None:
        raise PatchError(f"cannot name the file in 'diff --git {header_rest}'")
    return _FilePatch(FileChange(path=b_path, status=MODIFIED), ranges), i


def _parse(diff: str) -> list[_FilePatch]:
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    patches: list[_FilePatch] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git "):
            patch, i = _parse_block(lines, i)
            patches.append(patch)
        else:
            # A commit message or mail header before the first block.
            i += 1
    if not patches and diff.strip():
        raise PatchError("text is not a git diff: it has no 'diff --git' header")
    return patches


def files_in_patch(diff: str) -> list[FileChange]:
    """Every file a diff touches, in the order it lists them. `[]` for an empty diff."""
    return [patch.change for patch in _parse(diff)]


def old_side_hunks(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Changed OLD-side line ranges per path: 1-based, inclusive (see the module docstring).

    Keyed by the path the lines have at the base commit, which is what an index
    built from the base commit is keyed by: the old path of a rename. A file the
    diff creates has no old side and is absent, as is one whose only change is a
    mode or a binary payload.
    """
    result: dict[str, list[tuple[int, int]]] = {}
    for patch in _parse(diff):
        if not patch.old_ranges:
            continue
        key = patch.change.old_path if patch.change.status == RENAMED and patch.change.old_path else patch.change.path
        result.setdefault(key, []).extend(patch.old_ranges)
    return result
