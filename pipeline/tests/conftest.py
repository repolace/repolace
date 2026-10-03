"""Fixtures for the pipeline tests: a real git remote and the shared fake embedder.

Nothing about git is mocked. `run_task` clones, commits, squashes and pushes, so a test that
stubbed git would assert that argument lists are assembled and nothing about whether the
branch reaches the remote -- which is the step the whole task exists to reach.
"""

from pathlib import Path

import pytest
from retrieval.testing import FakeEmbedder, install_fake_embedder

from pipeline_support import make_bare_origin, make_source_repo


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    return make_source_repo(tmp_path / "source")


@pytest.fixture
def origin(tmp_path: Path, source_repo: Path) -> Path:
    return make_bare_origin(source_repo, tmp_path / "origin.git")


@pytest.fixture
def origin_url(origin: Path) -> str:
    return f"file://{origin}"


@pytest.fixture
def embedder(monkeypatch) -> FakeEmbedder:
    """The shared fake, installed where indexing and retrieval look it up. Never the real model."""
    return install_fake_embedder(monkeypatch)
