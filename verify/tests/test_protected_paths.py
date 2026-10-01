"""`is_protected_path`: the paths the agent's edit tool must refuse.

It exists so the tool and the scorer cannot disagree. A path the tool allows and
`disqualifying_paths` rejects is a finished patch thrown away; one the tool
refuses and the scorer allows is a fix the agent was never able to make. So the
tests here are mostly *consistency* tests against the scorer's own functions and
constants, rather than a second hand-written list that could drift from them.
"""

import pytest

from verify.protocol import SuiteResult
from verify.scoring import (
    _CONFIG_FILES,
    disqualifying_paths,
    is_protected_path,
    is_test_path,
)

PROTECTED = [
    "tests/test_a.py",
    "pkg/test_b.py",
    "pkg/b_test.py",
    "pkg/tests.py",
    "conftest.py",
    "tests/conftest.py",
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    ".coveragerc",
    ".gitattributes",
    "sitecustomize.py",
    # Fixtures that define no test of their own: the cheapest fake fixes there are.
    "tests/helpers.py",
    "tests/__snapshots__/render.ambr",
    "tests/cassettes/api.yaml",
]

NOT_PROTECTED = [
    "src/app.py",
    "pkg/models.py",
    "myapp/models.py",
    "README.md",
    "docs/index.rst",
    "setup.py",
    "src/contest.py",  # near-miss on `conftest.py`
]


@pytest.mark.parametrize("path", PROTECTED)
def test_test_and_config_paths_are_protected(path):
    assert is_protected_path(path) is True


@pytest.mark.parametrize("path", NOT_PROTECTED)
def test_ordinary_source_is_not_protected(path):
    assert is_protected_path(path) is False


@pytest.mark.parametrize("name", sorted(_CONFIG_FILES))
def test_every_config_basename_is_protected_at_any_depth(name):
    """Reads the scorer's own set, so a name added there is covered here for free."""
    assert is_protected_path(name) is True
    assert is_protected_path(f"deep/er/{name}") is True


@pytest.mark.parametrize("path", [".gitattributes", ".coveragerc"])
def test_a_leading_dot_slash_does_not_escape(path):
    """The seam `_normalise` exists for: `lstrip("./")` once turned these into
    `gitattributes` and `coveragerc`, which matched nothing."""
    assert is_protected_path(f"./{path}") is True


def test_a_shipped_module_under_a_test_named_directory_follows_is_test_path():
    """`django/test/client.py` is a shipped module, not a test. `is_test_path`
    flags it anyway (the heuristic cannot know), and the tool inherits that.

    Asserted as consistency with `is_test_path` rather than as a value, so
    retuning the heuristic updates this without anyone editing the test.
    """
    path = "django/test/client.py"

    assert is_protected_path(path) is is_test_path(path)


def test_it_is_stricter_than_the_scorer_exactly_where_the_scorer_has_evidence():
    """The one deliberate difference. `disqualifying_paths` spares a file that
    existed at baseline and that pytest never collected; at edit time there is no
    such evidence, so the tool refuses. Documented here so it is a decision and
    not an accident someone later "fixes" by loosening the tool."""
    path = "django/test/client.py"
    baseline = SuiteResult(collected_files=("tests/test_x.py",))

    assert disqualifying_paths([path], baseline, baseline_files=(path,)) == ()
    assert is_protected_path(path) is True


@pytest.mark.parametrize("path", PROTECTED)
def test_a_protected_path_is_one_the_scorer_disqualifies_without_evidence(path):
    """With no baseline to consult, scorer and tool agree on every protected path."""
    assert disqualifying_paths([path], SuiteResult()) == (path,)


@pytest.mark.parametrize("path", NOT_PROTECTED)
def test_an_unprotected_path_is_one_the_scorer_lets_through(path):
    assert disqualifying_paths([path], SuiteResult()) == ()
