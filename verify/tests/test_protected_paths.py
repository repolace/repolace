"""`is_protected_path`: the paths the agent's edit tool must refuse.

It exists so the tool and the scorer cannot disagree. A path the tool allows and
`disqualifying_paths` rejects is a finished patch thrown away; one the tool
refuses and the scorer allows is a fix the agent was never able to make. So the
tests here are mostly *consistency* tests against the scorer's own functions and
constants, rather than a second hand-written list that could drift from them.
"""

import pytest

from verify import scoring
from verify.protocol import SuiteResult
from verify.scoring import (
    _CONFIG_FILES,
    _normalise,
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

#: Protected by the tool and deliberately NOT disqualified by the scorer: CI is
#: not test infrastructure, but agent-authored CI on a pull request is a
#: security problem on a real repository.
PROTECTED_BY_THE_TOOL_ONLY = [
    ".github/workflows/ci.yml",
    ".github/dependabot.yml",
    ".github/CODEOWNERS",
    "./.github/workflows/ci.yml",
    ".GitHub/workflows/ci.yml",  # a filesystem that folds case must not let it through
]

NOT_PROTECTED = [
    "src/app.py",
    "pkg/models.py",
    "myapp/models.py",
    "README.md",
    "docs/index.rst",
    "setup.py",
    "src/contest.py",  # near-miss on `conftest.py`
    "github/workflows/ci.yml",  # no leading dot: GitHub does not read it
    ".githubrc",  # starts with `.github` but is not the directory
    "docs/.github/notes.md",  # GitHub only reads the repository-root directory
]

#: What a repository with a custom `python_files` / `--doctest-modules` setup
#: reports. None of these look like tests to the path heuristic.
COLLECTED = ("checks/check_foo.py", "src/pkg/docs.py")
CONFTESTS = ("conftest.py", "checks/conftest.py")


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
    assert _normalise(f"./{path}") == path
    assert _normalise(path) == path  # the dotfile's own leading dot survives


def test_a_leading_dot_slash_does_not_escape_the_baseline_evidence():
    """The half `is_test_path` cannot cover. `PurePosixPath("./x").parts` already
    drops the `./`, so the heuristic passes whether or not `_normalise` runs; the
    collected-file lookup is a plain string-set membership and is what the
    normalisation is actually *for*. Removing the call from `is_protected_path`
    leaves every heuristic test green and fails this one."""
    assert is_protected_path("./checks/check_foo.py", collected_files=COLLECTED) is True
    assert is_protected_path("./checks/data.json", collected_files=COLLECTED) is True
    assert is_protected_path("./checks/check_foo.py") is False  # no evidence, no heuristic match


def test_the_baselines_own_paths_may_carry_a_dot_slash_too():
    assert is_protected_path("checks/check_foo.py", collected_files=("./checks/check_foo.py",)) is True


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


@pytest.mark.parametrize("path", PROTECTED_BY_THE_TOOL_ONLY)
def test_ci_configuration_is_protected(path):
    assert is_protected_path(path) is True


@pytest.mark.parametrize("path", PROTECTED_BY_THE_TOOL_ONLY)
def test_the_scorer_does_not_disqualify_ci_so_the_tool_is_stricter_there(path):
    """Pinned so the asymmetry stays a decision. If the scorer ever learns to
    disqualify `.github/`, this fails and the docstring has to be updated."""
    assert disqualifying_paths([path], SuiteResult()) == ()


@pytest.mark.parametrize("path", PROTECTED)
def test_a_protected_path_is_one_the_scorer_disqualifies_without_evidence(path):
    """With no baseline to consult, scorer and tool agree on every protected path."""
    assert disqualifying_paths([path], SuiteResult()) == (path,)


@pytest.mark.parametrize("path", NOT_PROTECTED)
def test_an_unprotected_path_is_one_the_scorer_lets_through(path):
    assert disqualifying_paths([path], SuiteResult()) == ()


class TestBaselineEvidence:
    """The reverse direction: the scorer is stricter than the heuristic, and the
    tool must not be weaker than the scorer.

    `disqualifying_paths` reads what pytest actually collected. With a custom
    `python_files` or `--doctest-modules`, a file the path heuristic has never
    heard of is a test -- and a tool that cannot see that lets the agent edit it,
    then watches the scorer discard the whole patch.
    """

    BASELINE = SuiteResult(collected_files=COLLECTED, conftests=CONFTESTS)

    @pytest.mark.parametrize(
        "path", ["checks/check_foo.py", "src/pkg/docs.py", "checks/data.json", "checks/expected.yaml"]
    )
    def test_the_scorer_disqualifies_what_the_heuristic_misses(self, path):
        """The premise, asserted rather than assumed."""
        assert is_test_path(path) is False
        assert disqualifying_paths([path], self.BASELINE) == (path,)

    @pytest.mark.parametrize(
        "path", ["checks/check_foo.py", "src/pkg/docs.py", "checks/data.json", "checks/expected.yaml"]
    )
    def test_the_tool_protects_them_given_the_baseline(self, path):
        assert is_protected_path(path, collected_files=COLLECTED, conftests=CONFTESTS) is True

    @pytest.mark.parametrize("path", ["checks/check_foo.py", "src/pkg/docs.py", "checks/data.json"])
    def test_the_tool_cannot_know_without_the_baseline(self, path):
        """The weaker default, pinned so it is a known limit and not a surprise: it
        is why the pipeline must hand the toolbox a baseline-aware closure."""
        assert is_protected_path(path) is False

    def test_an_unrelated_file_stays_editable(self):
        for path in ("src/pkg/models.py", "checks/helper.py", "src/other/docs.py"):
            assert is_protected_path(path, collected_files=COLLECTED, conftests=CONFTESTS) is False

    def test_a_data_file_beside_collected_tests_is_protected_but_a_source_file_is_not(self):
        """The scorer's fixture-directory rule, which exempts `.py` so a helper
        module next to a collected file is not swept in by directory alone."""
        assert is_protected_path("checks/golden.json", collected_files=COLLECTED) is True
        assert is_protected_path("checks/helper.py", collected_files=COLLECTED) is False

    def test_only_keyword_arguments(self):
        """Positional evidence would be too easy to swap: `collected_files` and
        `conftests` are both sets of paths."""
        with pytest.raises(TypeError):
            is_protected_path("a.py", COLLECTED, CONFTESTS)  # type: ignore[misc]


PATHS_FOR_THE_GRID = [
    *PROTECTED,
    *NOT_PROTECTED,
    "checks/check_foo.py",
    "checks/data.json",
    "checks/helper.py",
    "src/pkg/docs.py",
    "src/pkg/models.py",
    "django/test/client.py",
    "tests/__init__.py",
    "docs/conf.py",
    "./checks/check_foo.py",
    "pkg/sub/conftest.py",
]

BASELINES = {
    "nothing collected": SuiteResult(),
    "custom python_files": SuiteResult(collected_files=COLLECTED, conftests=CONFTESTS),
    "ordinary layout": SuiteResult(
        collected_files=("tests/test_a.py", "tests/unit/test_b.py"), conftests=("tests/conftest.py",)
    ),
    "tests at the root": SuiteResult(collected_files=("test_root.py",), conftests=("conftest.py",)),
    "dot-slash paths": SuiteResult(collected_files=("./checks/check_foo.py",), conftests=("./conftest.py",)),
}


@pytest.mark.parametrize("name", BASELINES)
@pytest.mark.parametrize("path", PATHS_FOR_THE_GRID)
def test_the_tool_is_never_weaker_than_the_scorer(name, path):
    """The invariant in the docstring, over a grid: any path `disqualifying_paths`
    rejects (without `baseline_files`) is protected once the tool is given the
    same baseline sets. If the scorer learns a new way to reject a path, this is
    what notices that the tool has not."""
    baseline = BASELINES[name]
    rejected = bool(disqualifying_paths([path], baseline))

    protected = is_protected_path(
        path, collected_files=baseline.collected_files, conftests=baseline.conftests
    )

    assert protected or not rejected


def test_the_basename_floor_holds_when_the_heuristic_is_narrowed(monkeypatch):
    """The second clause in `is_protected_path` is "not redundancy to tidy away",
    and until now no test said so: every `PROTECTED` entry is also caught by
    `is_test_path`, so deleting the clause left the suite green.

    Patching `is_test_path` to match nothing is the situation the clause exists
    for -- the heuristic being tuned -- and `conftest.py` and `pyproject.toml`
    must stay unwritable regardless.
    """
    monkeypatch.setattr(scoring, "is_test_path", lambda path: False)

    for name in sorted(_CONFIG_FILES):
        assert is_protected_path(name) is True, name
        assert is_protected_path(f"deep/er/{name}") is True, name
    # and the narrowed heuristic really is narrowed, or this proves nothing
    assert scoring.is_test_path("tests/test_a.py") is False
    assert is_protected_path("tests/test_a.py") is False
