"""The deterministic stub editor.

Contains no model call. It exists to prove the plumbing -- clone, index,
retrieve, edit, squash, push, open a PR -- against a real repository, so a
failure anywhere in that chain has exactly one plausible cause.

The edit is driven by the *top retrieval hit*: the marker goes into the file
retrieval ranked first, and names the symbol and score that put it there. That
is deliberate. A stub that wrote to a fixed path would produce an identical,
passing run with retrieval completely broken; this one cannot. Retrieval is
load-bearing on the diff, and the diff is visible on GitHub.

Rendering is separated from applying so the interesting half is a pure function
over plain data -- no ORM rows, no filesystem, and therefore actually testable.

The words the stub puts on GitHub (title, commit message, PR body) are no longer here:
`repolace_pipeline.pr` renders them for every kind of run, so a stub PR goes through the
same sanitising and the same sections as an agent's, and says "NOT A FIX" through the
summary slot.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path

from repolace_shared.paths import PathEscapesRoot, resolve_within

from repolace_pipeline.context import RetrievedChunk

MARKER_RULE = "-" * 74
NOT_A_FIX = "repolace: NOT A FIX"


@dataclass(frozen=True)
class StubEditRequest:
    """Everything the stub renders from. Plain data, no session, no ORM."""

    task_id: uuid.UUID
    issue_number: int
    issue_title: str
    issue_url: str
    target_branch: str
    base_sha: str
    indexed_chunk_count: int
    retrieved: tuple[RetrievedChunk, ...]

    @property
    def top(self) -> RetrievedChunk:
        if not self.retrieved:
            raise ValueError("stub edit requires at least one retrieved chunk")
        return self.retrieved[0]


def _rank_note(chunk: RetrievedChunk) -> str:
    """Which arms found this chunk. `None` means that arm did not return it at all."""
    arms = [
        f"semantic={chunk.semantic_rank}" if chunk.semantic_rank is not None else "semantic=-",
        f"keyword={chunk.keyword_rank}" if chunk.keyword_rank is not None else "keyword=-",
    ]
    return f"rrf={chunk.rrf_score:.4f} " + " ".join(arms)


def render_marker(request: StubEditRequest, indent: str = "") -> str:
    """The comment block inserted above the top-ranked chunk. Pure."""
    top = request.top
    lines = [
        f"# {NOT_A_FIX} {MARKER_RULE[: max(0, 74 - len(NOT_A_FIX) - 1)]}",
        "# Written by the deterministic stub editor (repolace_pipeline.edit).",
        "# No model was involved and nothing here fixes anything. It proves only",
        "# that a task can clone, index, retrieve, edit, verify, squash, push and",
        "# open a pull request. The repo's suite did run and this diff was scored;",
        "# read tasks.outcome for the verdict, not this marker. Close this PR.",
        "#",
        f"# task   {request.task_id.hex}",
        f"# issue  #{request.issue_number} {request.issue_title}".rstrip(),
        f"# base   {request.base_sha}",
        f"# picked {top.location} {top.qualified_symbol} [{top.chunk_type}]",
        f"#        {_rank_note(top)}",
        f"#        top of {len(request.retrieved)} retrieved over "
        f"{request.indexed_chunk_count} indexed chunks",
        f"# {MARKER_RULE}",
    ]
    return "".join(f"{indent}{line}\n" for line in lines)


def apply_stub_edit(repo_path: Path, request: StubEditRequest) -> Path:
    """Insert the marker immediately above the top-ranked chunk. Returns the edited file.

    A ``#`` comment is valid Python at any indentation, and a chunk's
    ``start_line`` is always a statement boundary -- the ``def``, ``class`` or
    decorator line tree-sitter matched -- so inserting above it cannot land
    inside a string or split an expression. The marker copies that line's
    indentation so the result reads as deliberate rather than as damage.
    """
    top = request.top
    try:
        target = resolve_within(repo_path, top.file_path)
    except PathEscapesRoot as exc:
        # `repo_path / top.file_path` is not confinement: pathlib discards
        # repo_path outright for an absolute file_path, and a symlink walks out
        # with nothing in the string to notice. file_path comes from a database
        # row, and once the real Editor exists it comes from the model.
        raise ValueError(f"retrieved path is not usable in this checkout: {exc}") from exc
    if ".git" in target.relative_to(repo_path.resolve()).parts:
        # Protection the *stub* editor does not need -- its path came from a row
        # it wrote itself -- and the real Editor will. `.git` sits inside the
        # tree the editor writes to, and git treats parts of it as executable
        # configuration: an Editor that writes .git/hooks/post-commit gets host
        # code execution on the very next `record_attempt`. That is the confused
        # deputy re-entering through the editor rather than the sandbox, and
        # withholding .git from the sandbox says nothing about it.
        raise ValueError(f"refusing to edit inside .git: {top.file_path}")
    if not target.is_file():
        # Retrieval returns whatever is indexed; the index can outlive a file
        # that a later commit deleted.
        raise FileNotFoundError(f"retrieved path is not a file in this checkout: {top.file_path}")

    original = target.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    # start_line is 1-based; clamp because the index may predate an edit that
    # shortened the file.
    insert_at = min(max(top.start_line - 1, 0), len(lines))
    reference = lines[insert_at] if insert_at < len(lines) else ""
    indent = reference[: len(reference) - len(reference.lstrip())].replace("\n", "")

    lines.insert(insert_at, render_marker(request, indent=indent))
    target.write_text("".join(lines), encoding="utf-8")
    return target
