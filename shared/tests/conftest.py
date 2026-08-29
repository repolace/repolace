"""Fixtures backed by real git repositories.

Nothing here is mocked. The module under test shells out to git, so tests that
stubbed git would only assert that we assemble argument lists -- not that the
commands do what the branch-and-PR flow needs. Local `file://` remotes make the
real thing cheap: a clone is a clone and a push is a push, just without a
network.
"""

import subprocess
from pathlib import Path

import pytest

from shared_support import git, write


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A repo with three commits on `main` and a divergent `develop` branch."""
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main", ".")

    write(repo / "README.md", "# sample\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "first")

    write(repo / "src" / "app.py", "def add(a, b):\n    return a + b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "second")

    write(repo / "src" / "app.py", "def add(a, b):\n    return a - b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "third")

    git(repo, "checkout", "-b", "develop")
    write(repo / "src" / "extra.py", "VALUE = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "develop only")
    git(repo, "checkout", "main")

    # A separate branch rather than additions to `main`, so every existing test
    # that exports or diffs `main` keeps exactly the tree it was written for.
    git(repo, "checkout", "-b", "hazards")
    write(repo / "bin" / "run.sh", "#!/bin/sh\necho hi\n")
    (repo / "bin" / "run.sh").chmod(0o755)
    write(repo / "deep" / "nested" / "mod.py", "VALUE = 2\n")
    # `text eol=crlf` is what `checkout-index` honoured and blob reads do not:
    # under the old export the sandbox ran CRLF while the commit and the PR
    # diff showed LF.
    write(repo / ".gitattributes", "*.py text eol=crlf\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "hazards")
    git(repo, "checkout", "main")

    return repo


@pytest.fixture
def repo_with_symlink(source_repo: Path):
    """Adds a committed symlink to the `hazards` branch.

    Written through `update-index --cacheinfo` rather than by creating one on
    disk, so the test does not depend on the filesystem supporting symlinks and
    the mode is unambiguously 120000.
    """

    def add(branch: str = "hazards", name: str = "link.py", target: str = "/etc/hostname") -> Path:
        git(source_repo, "checkout", branch)
        oid = git(source_repo, "hash-object", "-w", "--stdin", input_text=target)
        git(source_repo, "update-index", "--add", "--cacheinfo", f"120000,{oid},{name}")
        git(source_repo, "commit", "-m", "add symlink")
        git(source_repo, "checkout", "main")
        return source_repo

    return add


@pytest.fixture
def origin(tmp_path: Path, source_repo: Path) -> Path:
    """A bare clone standing in for the GitHub remote, so pushes have somewhere real to land."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", str(source_repo), str(bare)], check=True, capture_output=True)
    return bare


@pytest.fixture
def origin_url(origin: Path) -> str:
    return f"file://{origin}"
