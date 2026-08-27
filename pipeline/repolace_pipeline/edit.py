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
"""

import uuid
from dataclasses import dataclass
from pathlib import Path

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
        "# that a task can clone, index, retrieve, edit, squash, push and open a",
        "# pull request. The Verify stage was skipped. Close this PR.",
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
    target = repo_path / top.file_path
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


def _retrieval_table(request: StubEditRequest) -> str:
    if not request.retrieved:
        # Absence must look like absence -- a blank section reads as a
        # rendering bug, which is the same reasoning as rag.index.no_python_files.
        return "_No chunks retrieved._\n"
    rows = ["| # | location | symbol | kind | score |", "|---|---|---|---|---|"]
    rows += [
        f"| {i} | `{c.location}` | `{c.qualified_symbol}` | {c.chunk_type} | {_rank_note(c)} |"
        for i, c in enumerate(request.retrieved, start=1)
    ]
    return "\n".join(rows) + "\n"


def pr_title(request: StubEditRequest) -> str:
    return f"[repolace] plumbing smoke test for issue #{request.issue_number}"


def commit_message(request: StubEditRequest) -> str:
    return (
        f"repolace stub edit for issue #{request.issue_number}\n\n"
        f"Not a fix. Written by the deterministic stub editor to verify the "
        f"task pipeline end to end. Task {request.task_id.hex}."
    )


def render_pr_body(request: StubEditRequest) -> str:
    """The PR description. Carries the full retrieved set as visible proof retrieval ran.

    Deliberately never writes `Closes #N` or `Fixes #N`: merging this would
    close a real issue that was never fixed.
    """
    top = request.top if request.retrieved else None
    return (
        f"## {NOT_A_FIX}\n\n"
        f"Generated by the deterministic stub editor (`repolace_pipeline.edit`). "
        f"**No language model was involved.** This pull request proves only that a task can "
        f"clone the repository, index it, retrieve relevant code, make an edit, squash, push a "
        f"branch and open a PR. The Verify stage was deliberately skipped, so nothing here was "
        f"tested and nothing was scored.\n\n"
        f"**Please close this pull request.**\n\n"
        f"| field | value |\n|---|---|\n"
        f"| task | `{request.task_id}` |\n"
        f"| issue | [#{request.issue_number}]({request.issue_url}) {request.issue_title} |\n"
        f"| target branch | `{request.target_branch}` |\n"
        f"| base commit | `{request.base_sha}` |\n"
        f"| indexed chunks | {request.indexed_chunk_count} |\n"
        f"| edited file | `{top.location if top else '-'}` |\n\n"
        f"### Retrieved context\n\n"
        f"The marker was inserted above the top-ranked hit below. If this table is empty or the "
        f"entries look unrelated to the issue, retrieval is the thing to look at.\n\n"
        f"{_retrieval_table(request)}"
    )
