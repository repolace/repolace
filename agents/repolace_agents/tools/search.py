"""The two tools that find code: `search_code` (the index) and `grep` (the checkout).

`grep` is the command-injection surface of the toolbox. It runs `git grep` on the
host with a pattern and a path the model chose, so the argument vector is built to
one rule: **a model-supplied string is never placed before `--`, and is never an
option.** The pattern is the operand of `-e`; the path goes after `--`; and
`--literal-pathspecs` makes git read that path as a file name, not as pathspec
magic (`:(top)`, `:(exclude)`, `:(glob)`) or a tree-ish (`origin/main`).
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from typing import Any

from repolace_shared.git import GitCommandError, GitTimeoutError, run_git

from repolace_agents.tools.base import ToolContext, ToolError, ToolOutcome, ToolSpec
from repolace_agents.tools.files import path_property
from repolace_agents.tools.paths import confine, printable, relative_posix, require_text, shown

#: Wall-clock bound on one `git grep`. It also bounds how much output git can
#: produce, and so how much the host buffers, which is the one resource the
#: model-chosen pattern can spend.
GREP_TIMEOUT_SECONDS = 20.0

#: A match in a minified file can be a megabyte on one line.
MAX_MATCH_LINE_CHARS = 300

#: Snippet lines shown per `search_code` hit.
MAX_SNIPPET_LINES = 30
MAX_SNIPPET_LINE_CHARS = 200

DEFAULT_GREP_RESULTS = 50
DEFAULT_SEARCH_LIMIT = 8


def _clip(line: str, limit: int) -> str:
    return line if len(line) <= limit else f"{line[:limit]}..."


class SearchCode:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.spec = ToolSpec(
            name="search_code",
            description=(
                "Semantic search over the repository: describe what you are looking for and get the "
                "most relevant functions, methods and classes, best first, each with its file, line "
                "range and the start of its code. The index reflects the repository at the base "
                "commit, not your edits: use read_file for the current content of a file, and grep "
                "for an exact name or string."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "What to look for, in words or as an identifier."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": f"Maximum hits (default {DEFAULT_SEARCH_LIMIT})."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        limit = args.get("limit", DEFAULT_SEARCH_LIMIT)
        require_text("query", args["query"])
        hits = list(await self._ctx.search(args["query"], limit))[:limit]
        if not hits:
            return ToolOutcome("no matches in the index; try different words, or use grep for an exact string")

        blocks = []
        for hit in hits:
            header = f"{hit.file_path}:{hit.start_line}-{hit.end_line} {hit.symbol} ({hit.chunk_type})"
            snippet = [_clip(line, MAX_SNIPPET_LINE_CHARS) for line in hit.snippet.split("\n")[:MAX_SNIPPET_LINES]]
            blocks.append("\n".join([header, *(f"    {line}" for line in snippet)]))
        return ToolOutcome("\n\n".join(blocks))


class Grep:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.spec = ToolSpec(
            name="grep",
            description=(
                "Search file contents with git grep: TRACKED files only, so a file you just created "
                "is not searched until the next run_python or run_tests commits it. The pattern is a "
                "POSIX extended regular expression, or a plain string with fixed_string. Output is "
                "path:line:text. Narrow with path (a file or directory) and glob (for example '*.py'; "
                "'*' matches across directories, and a glob with no '/' is also matched against the "
                "file name alone). Binary files are skipped."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "minLength": 1, "maxLength": 200, "description": "What to search for (single line)."},
                    "path": path_property("File or directory to search, relative to the repository root (default '.')."),
                    "glob": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Only files whose path matches, e.g. '*.py'."},
                    "fixed_string": {"type": "boolean", "description": "Treat the pattern as a literal string, not a regex."},
                    "case_insensitive": {"type": "boolean", "description": "Ignore case."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": ctx.limits.max_grep_results, "description": f"Maximum matches shown (default {DEFAULT_GREP_RESULTS})."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        ctx = self._ctx
        pattern = args["pattern"]
        glob = args.get("glob")
        max_results = args.get("max_results", min(DEFAULT_GREP_RESULTS, ctx.limits.max_grep_results))

        # NUL cannot be passed to a subprocess at all (it would raise ValueError, which
        # reads as a bug); a newline would make git treat the pattern as several.
        if "\0" in pattern or "\n" in pattern:
            raise ToolError("the pattern must be a single line with no NUL characters")
        require_text("pattern", pattern)

        resolved = confine(ctx.checkout, args.get("path", "."), write=False)
        if not os.path.lexists(resolved):
            raise ToolError(f"{shown(args.get('path', '.'))} does not exist")
        pathspec = relative_posix(ctx.checkout, resolved)

        argv = ["--literal-pathspecs", "grep", "-z", "-n", "-I", "--no-textconv", "--no-color"]
        argv.append("-F" if args.get("fixed_string", False) else "-E")
        if args.get("case_insensitive", False):
            argv.append("-i")
        # Everything model-supplied is below this line: the pattern as the operand of -e,
        # the path after `--`. `-z` makes git print `path NUL line NUL text`, so a file name
        # containing ':' still parses.
        argv += ["-e", pattern, "--", pathspec]

        try:
            stdout = await run_git(*argv, cwd=ctx.checkout, timeout=GREP_TIMEOUT_SECONDS)
        except GitTimeoutError:
            raise ToolError(
                f"grep ran longer than {GREP_TIMEOUT_SECONDS:g}s; narrow it with path or glob, or use a "
                f"more specific pattern"
            ) from None
        except GitCommandError as exc:
            if exc.returncode == 1:
                # `git grep` exits 1 for "no matches", and `run_git` raises on any
                # non-zero exit. This is the answer, not a failure.
                stdout = ""
            else:
                # 128 is an invalid regular expression -- but it is also what a broken
                # repository would give, and git's message tells the model which.
                first_line = (exc.stderr.splitlines() or ["(no message)"])[0]
                raise ToolError(f"grep failed: {_clip(first_line, 300)}") from None

        matches = []
        # Split on "\n" only: matched text can hold a form feed or U+2028, which
        # `splitlines` would cut in two.
        for record in stdout.split("\n"):
            parts = record.split("\0", 2)
            if len(parts) != 3:
                continue
            file_path, line_number, text = parts
            if glob is not None and not _glob_matches(glob, file_path):
                continue
            matches.append(f"{file_path}:{line_number}:{_clip(text, MAX_MATCH_LINE_CHARS)}")

        if not matches:
            scope = f" matching glob {glob!r}" if glob is not None else ""
            return ToolOutcome(f"no matches for {shown(pattern)!r} in tracked files under {printable(pathspec)}{scope}")
        body = "\n".join(matches[:max_results])
        if len(matches) > max_results:
            body += (
                f"\n[showing the first {max_results} of {len(matches)} matches; narrow with path or "
                f"glob, or use a more specific pattern]"
            )
        return ToolOutcome(body)


def _glob_matches(glob: str, file_path: str) -> bool:
    """`fnmatch` on the whole path, and on the file name too when the glob has no '/'.

    Done here rather than as a pathspec because `--literal-pathspecs` -- which is what
    keeps a model-chosen path from being read as magic -- also switches glob matching
    off in git.
    """
    if fnmatch.fnmatchcase(file_path, glob):
        return True
    return "/" not in glob and fnmatch.fnmatchcase(file_path.rsplit("/", 1)[-1], glob)
