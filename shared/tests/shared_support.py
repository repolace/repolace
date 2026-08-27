"""Helpers for building real git repositories in tests.

Separate from conftest.py because these are imported by name, not injected as
fixtures: `write` in particular is used a dozen times per module, and threading
it through every signature would bury the assertions.
"""

import subprocess
from pathlib import Path

# A test repo has no committer identity and may inherit a global signing
# requirement, either of which fails the commit outright.
AUTHOR_ARGS = ("-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *AUTHOR_ARGS, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def git_check_ref(name: str) -> bool:
    """Ask git itself whether a branch name is legal, rather than restating its rules here."""
    result = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True, text=True)
    return result.returncode == 0
