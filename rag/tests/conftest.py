"""Fixtures for indexing tests, built as real directories.

`index.py` walks a filesystem and shells out to git, so a mocked tree would
assert only that we assemble argument lists. The symlink cases in particular
cannot be expressed any other way -- a fake filesystem that models symlinks
faithfully enough to test this would be the thing under test.
"""

from pathlib import Path

import pytest

from retrieval.testing import FakeEmbedder, install_fake_embedder

from rag_support import write


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A small source tree: two Python files, one not, one ignored directory."""
    root = tmp_path / "checkout"
    write(root / "src" / "app.py", "def parse(path):\n    return path\n")
    write(root / "pkg" / "mod.py", "VALUE = 1\n")
    write(root / "README.md", "# readme\n")
    write(root / "__pycache__" / "stale.py", "SHOULD_NOT_BE_INDEXED = True\n")
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A file the checkout has no business reading, standing in for ~/.env."""
    return write(tmp_path / "outside" / "secrets.env", 'TOKEN = "host-only-secret"\n')


@pytest.fixture
def embedder(monkeypatch) -> FakeEmbedder:
    """The shared fake embedder, installed where `index.py` and `retrieve.py` look it up.

    Never the real model: nothing here may download weights or pull torch's
    inference path into a test.
    """
    return install_fake_embedder(monkeypatch)
