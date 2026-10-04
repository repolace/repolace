"""Fixtures for the pipeline tests: a real git remote and the shared fake embedder.

Nothing about git is mocked. `run_task` clones, commits, squashes and pushes, so a test that
stubbed git would assert that argument lists are assembled and nothing about whether the
branch reaches the remote -- which is the step the whole task exists to reach.
"""

import os
from pathlib import Path

import pytest
from retrieval.testing import FakeEmbedder, install_fake_embedder

from pipeline_support import make_bare_origin, make_source_repo

#: The integration tests build a real gateway client, which imports LiteLLM; without this it tries to
#: fetch the current price map from GitHub at import time. `repolace_gateway.client` sets the same
#: variable itself, but only when it is imported before `litellm`, and the test support imports both.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


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
