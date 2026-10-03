"""Plain projections of retrieval results.

``hybrid_search`` returns live ORM ``CodeChunk`` rows with ``embedding`` and
``content_tsv`` deferred. Touching either outside a greenlet context raises
``MissingGreenlet``, and the rows stay bound to the session they were loaded
from -- so passing them further into the pipeline couples every later stage to
a session lifetime and to which columns happen to be loaded.

Projecting at the call site cuts both. Nothing downstream of retrieval ever
sees a ``CodeChunk``, so no downstream code *can* trip the deferred-column
hazard, whatever it does with what it is given.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog

from repolace_agents.contracts import SearchHit
from repolace_agents.tools.base import ToolError
from repolace_agents.tools.paths import confine
from retrieval.ranking import RRFResult

log = structlog.get_logger()

#: A snippet is bounded in characters as well as lines. `AgentLimits` caps lines, and a
#: line is not a unit of size: one minified or generated line is megabytes.
MAX_SNIPPET_CHARS = 4000
#: What `repo_overview` may add to the opening prompt: names only, no contents.
MAX_OVERVIEW_CHARS = 3000
OVERVIEW_DEPTH = 2
#: Room kept for the "... and N more entries" line, so the cap holds for the whole text.
_OVERVIEW_TRAILER_RESERVE = len("... and 99999999 more entries") + 1


@dataclass(frozen=True)
class RetrievedChunk:
    """One retrieval hit, detached from the ORM and from the session."""

    file_path: str
    start_line: int
    end_line: int
    chunk_type: str
    symbol_name: str
    class_name: str | None
    rrf_score: float
    semantic_rank: int | None
    keyword_rank: int | None

    @classmethod
    def from_result(cls, result: RRFResult) -> "RetrievedChunk":
        chunk = result.chunk
        return cls(
            file_path=chunk.file_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            # ChunkType subclasses str, but .value keeps the plain string out of
            # anything that later formats or compares it as an enum.
            chunk_type=chunk.chunk_type.value,
            symbol_name=chunk.symbol_name,
            class_name=chunk.class_name,
            rrf_score=result.rrf_score,
            semantic_rank=result.semantic_rank,
            keyword_rank=result.keyword_rank,
        )

    @property
    def location(self) -> str:
        """`path:start-end`, the form used in logs, the edit and the PR body."""
        return f"{self.file_path}:{self.start_line}-{self.end_line}"

    @property
    def qualified_symbol(self) -> str:
        return f"{self.class_name}.{self.symbol_name}" if self.class_name else self.symbol_name


def search_hit(
    chunk: RetrievedChunk,
    checkout: Path,
    max_lines: int,
    *,
    max_chars: int = MAX_SNIPPET_CHARS,
) -> SearchHit:
    """One retrieval hit as the agent sees it, with its snippet read from the checkout.

    Read from the working tree rather than taken from the index: the index holds the
    chunk as it was embedded, which may predate an edit, and the agent should read
    what is on disk.

    **The path is a database value and the file is the repository's**, so it goes
    through the same read guard the agent's own `read_file` uses (`confine`): an
    absolute path, a `..`, a symlink (even one that lands back inside the tree) and
    anything in the `.git*` family are refused rather than followed. A repo that
    commits `settings.py -> /home/worker/.env` must not have that file reach a prompt
    through the retrieval stage, which runs before the agent has read anything.

    **A refusal is an empty snippet, never an exception.** This runs inside a tool
    call and before the agent starts; raising would turn a data problem (an indexed
    file a later commit deleted, a `.github/` script the tools will not open) into a
    failed task, and the hit is still useful without its text. The location and
    symbol are kept, and the reason is logged.
    """
    if max_lines < 1:
        raise ValueError(f"max_lines must be at least 1, got {max_lines}")
    return SearchHit(
        file_path=chunk.file_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        symbol=chunk.qualified_symbol,
        chunk_type=chunk.chunk_type,
        score=chunk.rrf_score,
        snippet=_read_snippet(checkout, chunk, max_lines, max_chars),
    )


def _read_snippet(checkout: Path, chunk: RetrievedChunk, max_lines: int, max_chars: int) -> str:
    try:
        path = confine(checkout, chunk.file_path, write=False)
    except ToolError as exc:
        log.warning("pipeline.context.snippet_refused", file=chunk.file_path[:200], reason=str(exc)[:200])
        return ""

    first = max(chunk.start_line, 1)
    last = min(chunk.end_line, first + max_lines - 1)
    taken: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                if number > last:
                    break
                if number >= first:
                    taken.append(line)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        # The index can outlive a file that a later commit deleted.
        return ""
    return "".join(taken)[:max_chars]


def repo_overview(paths: Sequence[str], *, max_chars: int = MAX_OVERVIEW_CHARS) -> str:
    """A tracked-path listing to depth two, from the base commit's file list. No contents.

    `src/` and `src/app/` appear as directories and `src/app/core.py` does not appear
    at all (its directory does): the point is the shape of the repository, so the agent
    can ask for the right directory, not an inventory a `list_dir` call would give.
    Built from `baseline_files()`, i.e. the commit, so it can never include the hidden
    overlay and does not change as the agent edits.

    Bounded in characters, and the bound is applied to *entries*: the listing stops
    before an entry that would not fit and says how many it left out, so it never ends
    mid-name. Control characters in a name become `?`: a tracked file may be named with a
    newline in it, and a name must stay one line of this listing.
    """
    if max_chars < 2 * _OVERVIEW_TRAILER_RESERVE:
        raise ValueError(f"max_chars must leave room for the trailer, got {max_chars}")
    entries: set[str] = set()
    for path in paths:
        parts = [part for part in path.split("/") if part]
        if not parts:
            continue
        for depth in range(1, min(len(parts), OVERVIEW_DEPTH) + 1):
            is_directory = depth < len(parts)
            entries.add("/".join(parts[:depth]) + ("/" if is_directory else ""))

    cleaned = ["".join("?" if ord(ch) < 0x20 or ord(ch) == 0x7F else ch for ch in entry) for entry in sorted(entries)]
    full = "\n".join(cleaned)
    if len(full) <= max_chars:
        return full

    budget = max_chars - _OVERVIEW_TRAILER_RESERVE
    lines: list[str] = []
    used = 0
    for line in cleaned:
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    lines.append(f"... and {len(cleaned) - len(lines)} more entries")
    return "\n".join(lines)
