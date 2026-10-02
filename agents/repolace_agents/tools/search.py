"""The two tools that find code: `search_code` (the index) and `grep` (the checkout).

`grep` is the command-injection surface of the toolbox. It runs `git grep` on the
host with a pattern and a path the model chose, so the argument vector is built to
one rule: **a model-supplied string is never placed before `--`, and is never an
option.** The pattern is the operand of `-e`; the path goes after `--`; and
`--literal-pathspecs` makes git read that path as a file name, not as pathspec
magic (`:(top)`, `:(exclude)`, `:(glob)`) or a tree-ish (`origin/main`).

The second hazard is resource use, not injection. `-E` is glibc's regex engine,
which is **not** safe against a hostile pattern: nested counted repetition
(`((a{1,200}){1,200}){1,200}b`, 28 characters) needs gigabytes to compile, and a
backreference makes matching exponential in CPU. Only `-F` is safe. So the child
runs under `RLIMIT_AS` and `RLIMIT_CPU` (`gitproc.run_limited_git`), single-threaded
so that limit is deterministic and one call cannot burn every core, and the
patterns known to cause it are refused up front as a second, cheaper line.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

from repolace_shared.git import GitTimeoutError

from repolace_agents.tools.base import ToolContext, ToolError, ToolOutcome, ToolSpec
from repolace_agents.tools.files import NOTICE_RESERVE, fit_count, notice_reserve, path_property
from repolace_agents.tools.gitproc import run_limited_git
from repolace_agents.tools.paths import (
    confine,
    escape_controls,
    is_git_name,
    printable,
    relative_posix,
    require_text,
    shown,
)

if TYPE_CHECKING:  # `contracts` imports the tools package, so a runtime import here would be a cycle
    from repolace_agents.contracts import SearchHit

#: Wall-clock bound on one `git grep`.
GREP_TIMEOUT_SECONDS = 20.0

#: Address-space and CPU ceilings for the `git grep` child. 512 MiB is far above what a
#: real search needs (single-threaded, a few tens of MiB) and far below what a counted-
#: repetition pattern wants (measured ~5 GB resident after 12 s), so a hostile pattern
#: dies at the limit instead of taking the worker host down with it.
GREP_MAX_MEMORY_BYTES = 512 * 1024 * 1024
GREP_MAX_CPU_SECONDS = 20

#: Bytes of `git grep` output read back before the process is killed. `run_git` buffers
#: everything git prints, which for `-e .` over a large repository is gigabytes.
GREP_MAX_OUTPUT_BYTES = 1024 * 1024

#: A counted repetition with a bound at or above this is refused in a regex pattern.
MAX_REPEAT_BOUND = 100

_BACKREFERENCE = re.compile(r"\\[1-9]")
_REPEAT_BOUND = re.compile(r"\{(\d*)(?:,(\d*))?\}")

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
        query = args["query"]
        require_text("query", query)
        # The real retrieval raises on a blank query and hands a NUL to Postgres, which
        # rejects it; both would end the run for what is the model's mistake.
        if not query.strip() or "\0" in query:
            raise ToolError("query must contain visible text")
        hits = list(await self._ctx.search(query, limit))[:limit]
        if not hits:
            return ToolOutcome("no matches in the index; try different words, or use grep for an exact string")

        cap = self._ctx.limits.max_output_chars
        budget = max(0, cap - notice_reserve(cap))
        blocks: list[str] = []
        used = 0
        for hit in hits:
            block = self._block(hit, MAX_SNIPPET_LINES)
            if not blocks and len(block) > budget:
                # Even one hit is too long (a tiny output cap): keep it, with fewer snippet lines.
                lines = MAX_SNIPPET_LINES
                while lines > 0 and len(block) > budget:
                    lines -= 1
                    block = self._block(hit, lines)
            if used + len(block) + 2 > budget:
                break
            blocks.append(block)
            used += len(block) + 2
        text = "\n\n".join(blocks)
        if len(blocks) < len(hits):
            text += (
                f"\n\n[{len(hits) - len(blocks)} more hit(s) cut at the output limit; ask for a smaller "
                f"limit or make the query more specific]"
            )
        return ToolOutcome(text)

    @staticmethod
    def _block(hit: SearchHit, snippet_lines: int) -> str:
        header = f"{hit.file_path}:{hit.start_line}-{hit.end_line} {hit.symbol} ({hit.chunk_type})"
        snippet = [_clip(line, MAX_SNIPPET_LINE_CHARS) for line in hit.snippet.split("\n")[:snippet_lines]]
        return "\n".join([header, *(f"    {line}" for line in snippet)])


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
        fixed = args.get("fixed_string", False)
        if not fixed:
            _refuse_expensive_regex(pattern)

        resolved = confine(ctx.checkout, args.get("path", "."), write=False)
        if not os.path.lexists(resolved):
            raise ToolError(f"{shown(args.get('path', '.'))} does not exist")
        pathspec = relative_posix(ctx.checkout, resolved)

        # `--threads=1`: no worker threads, so the address-space limit below is the whole
        # story (each thread would add its own stack and malloc arena) and one call cannot
        # spend every core.
        argv = ["--literal-pathspecs", "grep", "--threads=1", "-z", "-n", "-I", "--no-textconv", "--no-color"]
        argv.append("-F" if fixed else "-E")
        if args.get("case_insensitive", False):
            argv.append("-i")
        # Everything model-supplied is below this line: the pattern as the operand of -e,
        # the path after `--`. `-z` makes git print `path NUL line NUL text`, so a file name
        # containing ':' still parses.
        argv += ["-e", pattern, "--", pathspec]

        try:
            result = await run_limited_git(
                *argv,
                cwd=ctx.checkout,
                timeout=GREP_TIMEOUT_SECONDS,
                max_output_bytes=GREP_MAX_OUTPUT_BYTES,
                max_memory_bytes=GREP_MAX_MEMORY_BYTES,
                max_cpu_seconds=GREP_MAX_CPU_SECONDS,
            )
        except GitTimeoutError:
            raise ToolError(
                f"grep ran longer than {GREP_TIMEOUT_SECONDS:g}s; narrow it with path or glob, or use a "
                f"more specific pattern"
            ) from None

        if not result.output_cut and result.returncode not in (0, 1):
            if result.returncode < 0:
                raise ToolError(
                    "grep was stopped for using too much memory or CPU time; use fixed_string=true or a "
                    "simpler pattern"
                )
            # 128 is an invalid regular expression -- but it is also what a broken repository
            # would give, and git's message tells the model which. (Exit 1 is "no matches".)
            stderr = result.stderr.decode("utf-8", errors="replace")
            first_line = (stderr.splitlines() or ["(no message)"])[0]
            raise ToolError(f"grep failed: {_clip(first_line, 300)}")

        output = result.stdout.decode("utf-8", errors="replace")
        if result.output_cut:
            output = output[: output.rfind("\n") + 1]  # drop the record the cut landed in

        matches = []
        for file_path, line_number, text in _records(output):
            # `read_file` and `list_dir` refuse the `.git*` family, so grep must not show its content.
            if any(is_git_name(part) for part in file_path.split("/")):
                continue
            if glob is not None and not _glob_matches(glob, file_path):
                continue
            matches.append(f"{escape_controls(file_path)}:{line_number}:{_clip(text, MAX_MATCH_LINE_CHARS)}")

        if not matches:
            scope = f" matching glob {glob!r}" if glob is not None else ""
            return ToolOutcome(f"no matches for {shown(pattern)!r} in tracked files under {printable(pathspec)}{scope}")
        # Budgeted in characters, with room kept for up to two notices: a count limit does not
        # bound size, and the box would cut the notices off the end.
        cap = ctx.limits.max_output_chars
        budget = max(0, cap - min(2 * NOTICE_RESERVE, cap // 2))
        shown_count = max(1, fit_count(matches[:max_results], budget))
        body = "\n".join(matches[:shown_count])
        if shown_count < len(matches):
            body += (
                f"\n[showing the first {shown_count} of {len(matches)} matches; narrow with path or "
                f"glob, or use a more specific pattern]"
            )
        if result.output_cut:
            body += (
                f"\n[grep output was cut at {GREP_MAX_OUTPUT_BYTES} bytes, so these results are "
                f"incomplete; narrow with path or glob, or use a more specific pattern]"
            )
        return ToolOutcome(body)


def _refuse_expensive_regex(pattern: str) -> None:
    """Refuse the pattern shapes that make glibc's engine blow up, in a regex (not `-F`).

    A defence in depth behind the resource limits, and the clearer failure: the model is
    told what to change instead of getting a killed process. It over-refuses on purpose
    -- `\\1` after an escaped backslash, a `{200}` that was meant literally -- because a
    refused pattern costs one retry and an accepted one can cost the host.
    """
    if _BACKREFERENCE.search(pattern):
        raise ToolError(
            "the pattern uses a backreference (\\1 to \\9), which can make grep take exponential "
            "time; use fixed_string=true or a simpler pattern"
        )
    for match in _REPEAT_BOUND.finditer(pattern):
        if any(int(bound) >= MAX_REPEAT_BOUND for bound in match.groups() if bound):
            raise ToolError(
                f"the pattern repeats something {MAX_REPEAT_BOUND} or more times with a {{n,m}} bound, "
                f"which can make grep use unbounded memory; use fixed_string=true or a smaller bound"
            )


def _records(output: str) -> Iterator[tuple[str, str, str]]:
    """`(path, line number, text)` from `git grep -z -n` output: `path NUL line NUL text NL`.

    Parsed by position -- the path runs to the first NUL, the line number to the next, the
    text to the next newline -- and not by splitting on newlines first. A file name may
    itself contain a newline, and splitting first would let a repository forge a record
    that attributes text to any path it likes.
    """
    position = 0
    while position < len(output):
        path_end = output.find("\0", position)
        if path_end < 0:
            return
        number_end = output.find("\0", path_end + 1)
        if number_end < 0:
            return
        text_end = output.find("\n", number_end + 1)
        if text_end < 0:
            text_end = len(output)
        yield output[position:path_end], output[path_end + 1 : number_end], output[number_end + 1 : text_end]
        position = text_end + 1


def _glob_matches(glob: str, file_path: str) -> bool:
    """`fnmatch` on the whole path, and on the file name too when the glob has no '/'.

    Done here rather than as a pathspec because `--literal-pathspecs` -- which is what
    keeps a model-chosen path from being read as magic -- also switches glob matching
    off in git.
    """
    if fnmatch.fnmatchcase(file_path, glob):
        return True
    return "/" not in glob and fnmatch.fnmatchcase(file_path.rsplit("/", 1)[-1], glob)
