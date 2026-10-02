"""Builders for the toolbox tests: a real git checkout and a `ToolContext` over fakes.

`<area>_support.py`, globally unique, for the reason `agents_support.py` gives.

Nothing about the filesystem or git is faked: the tools exist to confine real
paths and run real `git grep`, and a test that stubbed either would only assert
that we assemble arguments. What *is* faked is everything behind the pipeline's
callables -- the index, the sandbox -- by `FakeWorkspace` and scripted results.
"""

import os
import subprocess
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repolace_agents.contracts import SearchHit
from repolace_agents.tools import ToolBox, ToolContext, ToolOutcome, build_toolbox
from verify.protocol import ScriptResult, SuiteResult
from verify.testing import FakeWorkspace

from agents_support import FakeToolCall

#: A small Python project: source, a test, and the files the write guard protects.
DEFAULT_FILES: Mapping[str, str | bytes] = {
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
    "src/pkg/util.py": "VALUE = 1\n",
    "tests/test_core.py": "from pkg.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "conftest.py": "",
    "pyproject.toml": "[project]\nname = 'pkg'\n",
    "README.md": "# pkg\n",
    ".gitignore": "*.log\nbuild/\n",
}


def git(cwd: Path, *args: str) -> str:
    """Run real git with no user or system config, so a developer's settings cannot change a result."""
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )
    return result.stdout


def make_checkout(root: Path, files: Mapping[str, str | bytes] | None = None, *, commit: bool = True) -> Path:
    """A real git repository at `root/checkout` holding `files` (default: `DEFAULT_FILES`)."""
    checkout = root / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q", "--initial-branch=main", ".")
    for rel, content in (DEFAULT_FILES if files is None else files).items():
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    if commit:
        git(checkout, "add", "-A")
        git(checkout, "commit", "-q", "-m", "init")
    return checkout


def snapshot(checkout: Path) -> dict[str, bytes | None]:
    """`{relative path: bytes}` for every file under `checkout` outside `.git`, `None` for a directory.

    Directories are included because a refused `create_file` that had already made
    its parent directories would leave a changed tree with no changed file.
    Symlinks are recorded by target, not followed.
    """
    entries: dict[str, bytes | None] = {}
    for current, directories, names in os.walk(checkout):
        directories[:] = [name for name in directories if name != ".git"]
        for name in directories:
            path = Path(current) / name
            entries[path.relative_to(checkout).as_posix()] = None
        for name in names:
            path = Path(current) / name
            rel = path.relative_to(checkout).as_posix()
            entries[rel] = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
    return entries


def hit(
    file_path: str = "src/pkg/core.py",
    start: int = 1,
    end: int = 2,
    symbol: str = "add",
    snippet: str = "def add(a, b):\n    return a + b",
    chunk_type: str = "function",
) -> SearchHit:
    return SearchHit(file_path, start, end, symbol, chunk_type, 0.9, snippet)


@dataclass
class Harness:
    """A real checkout, a `ToolBox` over it, and a record of what the callables were asked."""

    checkout: Path
    workspace: FakeWorkspace
    box: ToolBox
    ctx: ToolContext
    #: Every checkpoint message and every sandbox call, in the order they happened.
    events: list[str] = field(default_factory=list)
    subset_calls: list[list[str]] = field(default_factory=list)
    script_calls: list[tuple[str, float]] = field(default_factory=list)
    searches: list[tuple[str, int]] = field(default_factory=list)

    async def call(self, name: str, **arguments: Any) -> ToolOutcome:
        return await self.box.dispatch(FakeToolCall(name, arguments))


def make_harness(
    root: Path,
    *,
    files: Mapping[str, str | bytes] | None = None,
    subset_results: Sequence[SuiteResult | BaseException] = (),
    script_results: Sequence[ScriptResult | BaseException] = (),
    hits: Sequence[SearchHit] = (),
    **ctx_overrides: Any,
) -> Harness:
    """A harness over a fresh checkout. `ctx_overrides` replace `ToolContext` fields
    (`run_subset=None`, `limits=...`, `is_protected=...`).

    `run_subset` and `run_script` first export the workspace, which -- like the real
    one -- refuses a tree that differs from HEAD, so a tool that forgets to
    checkpoint raises here instead of passing.
    """
    checkout = make_checkout(root, files)
    workspace = FakeWorkspace(root / "workspace")
    (root / "workspace").mkdir()
    events: list[str] = []
    subset_calls: list[list[str]] = []
    script_calls: list[tuple[str, float]] = []
    searches: list[tuple[str, int]] = []
    suites = deque(subset_results)
    scripts = deque(script_results)

    async def checkpoint(message: str) -> str | None:
        events.append(f"checkpoint: {message}")
        return await workspace.record_attempt(message)

    async def search(query: str, limit: int) -> Sequence[SearchHit]:
        searches.append((query, limit))
        return list(hits)

    async def run_subset(targets: Sequence[str]) -> SuiteResult:
        events.append("run_subset")
        subset_calls.append(list(targets))
        await workspace.export_tree("probe-1")
        item = suites.popleft() if suites else SuiteResult(passed=("t::a",))
        if isinstance(item, BaseException):
            raise item
        return item

    async def run_script(code: str, timeout: float) -> ScriptResult:
        events.append("run_script")
        script_calls.append((code, timeout))
        await workspace.export_tree("script-1")
        item = scripts.popleft() if scripts else ScriptResult(exit_code=0, stdout="ok\n")
        if isinstance(item, BaseException):
            raise item
        return item

    fields: dict[str, Any] = {
        "checkout": checkout,
        "checkpoint": checkpoint,
        "search": search,
        "run_subset": run_subset,
        "run_script": run_script,
    }
    fields.update(ctx_overrides)
    ctx = ToolContext(**fields)
    return Harness(checkout, workspace, build_toolbox(ctx), ctx, events, subset_calls, script_calls, searches)

