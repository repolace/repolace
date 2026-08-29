"""Helpers for indexing tests.

Named `rag_support` rather than `support` for the reason `verify_support`
records: pytest's prepend import mode puts every test directory on `sys.path`,
so a module name here has to be unique across the whole workspace.

Deliberately not importing `shared_support`. It is reachable today by that same
accident of `sys.path`, and relying on it would couple two suites that have no
dependency on each other.
"""

import subprocess
from pathlib import Path

AUTHOR_ARGS = (
    "-c", "user.name=Test",
    "-c", "user.email=test@example.com",
    "-c", "commit.gpgsign=false",
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *AUTHOR_ARGS, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path
