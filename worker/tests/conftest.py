"""Fixtures for the worker tests.

The task-body tests reuse the pipeline tests' seams (a real local git remote, `FakeGithubClient`,
`ScriptedAgent`, the shared fake embedder). `pipeline_support` is not an installed module, so its
directory is put on `sys.path` here, the same way pytest's prepend import mode does for it when the
pipeline tests are collected.
"""

import os
import sys
from pathlib import Path

import pytest

_PIPELINE_TESTS = Path(__file__).resolve().parents[2] / "pipeline" / "tests"
if str(_PIPELINE_TESTS) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_TESTS))

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from pipeline_support import make_bare_origin, make_source_repo  # noqa: E402 -- needs the sys.path entry above
from retrieval.testing import FakeEmbedder, install_fake_embedder  # noqa: E402 -- same


@pytest.fixture
def origin_url(tmp_path: Path) -> str:
    source = make_source_repo(tmp_path / "source")
    return f"file://{make_bare_origin(source, tmp_path / 'origin.git')}"


@pytest.fixture
def embedder(monkeypatch) -> FakeEmbedder:
    return install_fake_embedder(monkeypatch)
