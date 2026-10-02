"""The four tools that read or change files: `read_file`, `list_dir`, `edit_file`, `create_file`.

Every path goes through `confine` and the tools open what it returns. Everything
a refusal depends on is checked *before* the first byte is written, so a refused
call leaves the checkout exactly as it was -- a test pins that for every refusal.

Parameter bounds (lengths, ranges) live in each schema and are enforced by
`ToolBox`; the code here owns only what a schema cannot say: that the file
exists, is text, matches exactly once.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from repolace_shared.git import GitCommandError, run_git

from repolace_agents.tools.base import ToolContext, ToolError, ToolOutcome, ToolSpec
from repolace_agents.tools.paths import MAX_PATH_CHARS, confine, is_git_name, printable, relative_posix

#: More than this in one listing is a wall of text the model cannot use; it is
#: told to list a subdirectory instead.
MAX_LIST_ENTRIES = 300

#: Lines of context shown either side of an edit.
EDIT_CONTEXT_LINES = 3

#: `git check-ignore` is a lookup, not a scan; this only bounds a wedged git.
CHECK_IGNORE_TIMEOUT_SECONDS = 20.0


def path_property(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": MAX_PATH_CHARS, "description": description}


def read_regular_file(resolved: Path, shown_path: str, max_bytes: int) -> bytes:
    """The bytes of a regular text file within the size limit, or a `ToolError`.

    Reads at most `max_bytes + 1` rather than trusting `st_size`, so a file that
    grew between the two cannot defeat the cap. A NUL byte means binary: it
    cannot be shown to the model or edited as text without corrupting it.
    """
    try:
        info = resolved.lstat()
    except (FileNotFoundError, NotADirectoryError):
        raise ToolError(f"{shown_path} does not exist; use list_dir or grep to find the right path") from None
    if stat.S_ISDIR(info.st_mode):
        raise ToolError(f"{shown_path} is a directory; use list_dir to see what is in it")
    if not stat.S_ISREG(info.st_mode):
        # A FIFO or device would block or misbehave on open; a checkout holds neither.
        raise ToolError(f"{shown_path} is not a regular file")

    too_big = (
        f"{shown_path} is over the {max_bytes}-byte limit for these tools; use grep to find the "
        f"part you need"
    )
    if info.st_size > max_bytes:
        raise ToolError(too_big)
    with resolved.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ToolError(too_big)
    if b"\0" in data:
        raise ToolError(f"{shown_path} looks like a binary file (it contains NUL bytes) and cannot be read or edited")
    return data


def numbered(lines: list[str], first_number: int) -> str:
    return "\n".join(f"{first_number + offset:>6}\t{line}" for offset, line in enumerate(lines))


def split_lines(text: str) -> list[str]:
    """Lines as an editor numbers them: split on `\\n` only, trailing newline not a line.

    `str.splitlines` also splits on form feed, vertical tab and U+2028, which
    would number a line differently from `grep -n`, `git` and the model's own
    `old_string`.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


class ReadFile:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.spec = ToolSpec(
            name="read_file",
            description=(
                "Read a text file from the repository, with line numbers. Shows the file as it is "
                f"now, including your own edits. At most {ctx.limits.max_read_lines} lines per call: "
                "pass start_line (and end_line) to read further. Binary files and files over "
                f"{ctx.limits.max_file_bytes} bytes are refused. Paths are relative to the "
                "repository root; anything starting with '.git' is off limits."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_property("File path relative to the repository root."),
                    "start_line": {"type": "integer", "minimum": 1, "description": "First line to show (default 1)."},
                    "end_line": {"type": "integer", "minimum": 1, "description": "Last line to show (inclusive)."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        ctx = self._ctx
        resolved = confine(ctx.checkout, args["path"], write=False)
        rel = relative_posix(ctx.checkout, resolved)
        name = printable(rel)  # for display only: `rel` is what the filesystem and git are given
        data = read_regular_file(resolved, name, ctx.limits.max_file_bytes)

        lines = split_lines(data.decode("utf-8", errors="replace"))
        total = len(lines)
        if total == 0:
            return ToolOutcome(f"{name} is empty")

        start = args.get("start_line", 1)
        end = args.get("end_line")
        if start > total:
            raise ToolError(f"{name} has only {total} line(s); start_line {start} is past the end")
        if end is not None and end < start:
            raise ToolError(f"end_line ({end}) is before start_line ({start})")

        capped_end = start + ctx.limits.max_read_lines - 1
        last = min(total, capped_end, end if end is not None else total)
        out = [f"{name} (lines {start}-{last} of {total})", numbered(lines[start - 1 : last], start)]
        if last < total and (end is None or end > last):
            out.append(f"[{total - last} more line(s); call read_file again with start_line={last + 1} to continue]")
        return ToolOutcome("\n".join(out))


class ListDir:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.spec = ToolSpec(
            name="list_dir",
            description=(
                "List the entries of a directory. Directories end with '/', symlinks with '@' (a "
                "symlink is never followed). depth 1 lists the directory itself; 2 and 3 include "
                f"subdirectories. At most {MAX_LIST_ENTRIES} entries; entries starting with '.git' "
                "are not shown."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_property("Directory relative to the repository root (default '.')."),
                    "depth": {"type": "integer", "minimum": 1, "maximum": 3, "description": "Levels to list (default 1)."},
                },
                "required": [],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        ctx = self._ctx
        requested = args.get("path", ".")
        resolved = confine(ctx.checkout, requested, write=False)
        rel = relative_posix(ctx.checkout, resolved)
        name = printable(rel)  # for display only: `rel` is what the filesystem and git are given
        try:
            info = resolved.lstat()
        except (FileNotFoundError, NotADirectoryError):
            raise ToolError(f"{name} does not exist") from None
        if not stat.S_ISDIR(info.st_mode):
            raise ToolError(f"{name} is a file, not a directory; use read_file")

        entries: list[str] = []
        self._walk(resolved, "", args.get("depth", 1), entries)
        shown_entries = entries[:MAX_LIST_ENTRIES]
        header = f"{name}/" if rel != "." else "./"
        if not shown_entries:
            return ToolOutcome(f"{header} is empty")
        body = "\n".join(shown_entries)
        if len(entries) > MAX_LIST_ENTRIES:
            body += f"\n[more than {MAX_LIST_ENTRIES} entries; list a subdirectory or use a lower depth]"
        return ToolOutcome(f"{header}\n{body}")

    def _walk(self, directory: Path, prefix: str, depth: int, out: list[str]) -> None:
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda entry: entry.name)
        for child in children:
            if len(out) > MAX_LIST_ENTRIES:
                return  # one past the cap is enough to know there is more
            if is_git_name(child.name):
                continue
            # `follow_symlinks=False` on both: a symlink is listed, never entered,
            # so a listing cannot be walked out of the tree.
            is_dir = child.is_dir(follow_symlinks=False)
            suffix = "/" if is_dir else "@" if child.is_symlink() else ""
            out.append(printable(f"{prefix}{child.name}{suffix}"))
            if is_dir and depth > 1:
                self._walk(Path(child.path), f"{prefix}{child.name}/", depth - 1, out)


class EditFile:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        limit = ctx.limits.max_edit_chars
        self.spec = ToolSpec(
            name="edit_file",
            description=(
                "Replace text in an existing file. old_string must match the file exactly "
                "(whitespace and indentation included) and must appear exactly once unless "
                "replace_all is true; include enough surrounding lines to make it unique. Test files, "
                "conftest.py and pytest/tox/setup configuration are read-only, as is anything "
                "starting with '.git'. Returns the edited region with a few lines of context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_property("File path relative to the repository root."),
                    "old_string": {"type": "string", "minLength": 1, "maxLength": limit, "description": "Exact text to replace."},
                    "new_string": {"type": "string", "maxLength": limit, "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        ctx = self._ctx
        old, new = args["old_string"], args["new_string"]
        replace_all = args.get("replace_all", False)

        resolved = confine(ctx.checkout, args["path"], write=True, is_protected=ctx.is_protected)
        rel = relative_posix(ctx.checkout, resolved)
        name = printable(rel)  # for display only: `rel` is what the filesystem and git are given
        if old == new:
            raise ToolError("old_string and new_string are identical; nothing to change")
        if "\0" in new:
            raise ToolError("new_string contains a NUL byte; files edited with these tools must be text")

        data = read_regular_file(resolved, name, ctx.limits.max_file_bytes)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{name} is not valid UTF-8, so it cannot be edited safely") from None

        count = text.count(old)
        if count == 0:
            raise ToolError(
                f"old_string was not found in {name}; it must match exactly, including whitespace and "
                f"indentation. Use read_file to see the current content"
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string appears {count} times in {name}; add surrounding lines to make it unique, "
                f"or set replace_all to true to change every occurrence"
            )

        replacements = count if replace_all else 1
        try:
            new_size = len(new.encode("utf-8"))
        except UnicodeEncodeError:
            raise ToolError("new_string is not valid text (it holds a lone surrogate)") from None
        # `replace_all` with a one-character old_string and a 20k-character new_string
        # would grow a file by gigabytes. The size is computed *before* the result is
        # built, so the refusal costs nothing, and it is held to the cap a file must
        # fit under to be read at all.
        projected = len(data) + replacements * (new_size - len(old.encode("utf-8")))
        if projected > ctx.limits.max_file_bytes:
            raise ToolError(
                f"the edit would make {name} larger than the {ctx.limits.max_file_bytes}-byte limit; "
                f"change less text per call"
            )

        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        resolved.write_bytes(new_text.encode("utf-8"))

        first_line = new_text.count("\n", 0, text.find(old)) + 1
        # A trailing newline ends the last replaced line rather than starting another.
        last_line = first_line + max(0, new.count("\n") - (1 if new.endswith("\n") else 0))
        lines = split_lines(new_text)
        window_start = max(1, first_line - EDIT_CONTEXT_LINES)
        window_end = min(len(lines), last_line + EDIT_CONTEXT_LINES)
        summary = f"edited {name}" + (f": replaced {replacements} occurrences; context shown for the first" if replacements > 1 else "")
        return ToolOutcome(f"{summary}\n{numbered(lines[window_start - 1 : window_end], window_start)}")


class CreateFile:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.spec = ToolSpec(
            name="create_file",
            description=(
                "Create a new file, with any missing parent directories. Refuses to overwrite: use "
                "edit_file for an existing file. Adding test files is not allowed, and neither is "
                "anything starting with '.git'. A path matched by .gitignore is created but never "
                "committed or run, and the result says so."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_property("New file path relative to the repository root."),
                    "content": {"type": "string", "maxLength": ctx.limits.max_create_chars, "description": "Full file content."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        ctx = self._ctx
        content = args["content"]

        resolved = confine(ctx.checkout, args["path"], write=True, is_protected=ctx.is_protected)
        rel = relative_posix(ctx.checkout, resolved)
        name = printable(rel)  # for display only: `rel` is what the filesystem and git are given
        if os.path.lexists(resolved):
            raise ToolError(f"{name} already exists; use edit_file to change it")
        if "\0" in content:
            raise ToolError("content contains a NUL byte; files created with these tools must be text")
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            raise ToolError("content is not valid text (it holds a lone surrogate)") from None

        # Asked before anything is written, so a git failure leaves the tree untouched.
        ignored = await self._is_ignored(rel)

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            raise ToolError(f"a parent of {name} is a file, not a directory") from None
        try:
            # O_EXCL: the existence check above is advisory; this is the one that
            # cannot race. O_NOFOLLOW: never write through a symlink at the leaf.
            descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            raise ToolError(f"{name} already exists; use edit_file to change it") from None
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)

        message = f"created {name} ({len(split_lines(content))} lines)"
        if ignored:
            message += (
                f"\nWARNING: {name} matches a .gitignore rule, so it will never be committed, never "
                f"reach the sandbox and never appear in the patch. Put the code somewhere that is "
                f"not ignored"
            )
        return ToolOutcome(message)

    async def _is_ignored(self, rel: str) -> bool:
        # The path is a model-chosen string, so it goes after `--` and can never be an
        # option. `check-ignore` refuses `--literal-pathspecs`, so the other half of the
        # `grep` rule is done by hand: a leading `./` means the string cannot begin with
        # `:`, which is how pathspec magic is spelled.
        try:
            await run_git(
                "-c", "safe.bareRepository=explicit",
                "check-ignore", "-q", "--", f"./{rel}",
                cwd=self._ctx.checkout, timeout=CHECK_IGNORE_TIMEOUT_SECONDS,
            )
        except GitCommandError as exc:
            if exc.returncode == 1:  # git's "not ignored"
                return False
            # 128 is what git says for a path inside a submodule (a tracked gitlink is an
            # empty directory in the clone, and a repository author controls that). Not
            # something the model can fix and not a repolace bug, so it is told, not crashed.
            raise ToolError(
                "cannot create files at that location: git cannot check it, for example because it "
                "is inside a submodule"
            ) from None
        return True
