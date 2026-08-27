"""Fixtures for the pipeline package.

The `anyio_backend` fixture is duplicated from `shared/tests/conftest.py` on
purpose: conftest scope does not reach sibling directories, so a test dir
without its own copy cannot run async tests at all. A repo-root conftest would
serve all three test directories and is the tidier fix; a local copy is
zero-risk and is what this slice takes.
"""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Pin anyio's parametrisation to asyncio; there is no trio in this project."""
    return "asyncio"
