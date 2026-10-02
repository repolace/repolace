"""pytest's other config files are test-edit territory too.

pytest reads `pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`, `tox.ini`,
`setup.cfg` and `pyproject.toml`, and a file earlier in its search order silently
replaces one later in it: verified with pytest 9.1.1, a `pytest.toml` takes precedence
over the repository's own `pytest.ini` ("ignoring pytest config in pytest.ini"). So an
agent could swap the whole config -- `filterwarnings`, `python_files`, `testpaths`,
`addopts` -- by *adding* a file, with a diff that touches no protected path. The
fingerprint watches only twelve ini keys, so it would not notice either.

Each of the three names is checked through every route the no-test-edits rule has:
the edit tool's `is_protected_path`, `disqualifying_paths`, and `score()` itself.
The pre-existing tests in `test_scoring.py` and `test_protected_paths.py` are
untouched; `test_protected_paths.py` already iterates `_CONFIG_FILES`, so it covers
these names too.
"""

import pytest

from repolace_shared.db.models import TaskOutcome
from verify.scoring import (
    _CONFIG_FILES,
    agent_verdict,
    disqualifying_paths,
    is_protected_path,
    is_test_path,
    score,
)

from verify_support import suite

NEW_NAMES = (".pytest.ini", "pytest.toml", ".pytest.toml")
A = "tests/test_a.py::test_one"
B = "tests/test_a.py::test_two"

#: At the root, where pytest looks first, and one level down, where a nested rootdir would.
LOCATIONS = ("{name}", "sub/{name}", "a/b/c/{name}")


def paths():
    return [pattern.format(name=name) for name in NEW_NAMES for pattern in LOCATIONS]


def test_the_set_holds_every_config_file_pytest_reads():
    assert {"pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg", "pyproject.toml"} <= _CONFIG_FILES


@pytest.mark.parametrize("path", paths())
class TestEachRoute:
    def test_the_heuristic_calls_it_test_infrastructure(self, path):
        assert is_test_path(path) is True

    def test_the_edit_tool_refuses_it(self, path):
        assert is_protected_path(path) is True

    def test_it_is_protected_with_no_baseline_evidence_and_with_the_dot_slash_prefix(self, path):
        """`_normalise` must not eat the leading dot of `.pytest.ini`."""
        assert is_protected_path(f"./{path}") is True

    def test_it_is_disqualified(self, path):
        assert disqualifying_paths([path], suite(passed=(A,))) == (path,)

    def test_it_is_disqualified_even_though_it_existed_at_baseline(self, path):
        """The exemption for a shipped module that merely looks like a test must not
        reach a config file: relaxing it is the whole attack."""
        assert disqualifying_paths([path], suite(passed=(A,)), baseline_files=(path, "src/app.py")) == (path,)

    def test_score_fails_the_task_before_looking_at_any_run(self, path):
        scored = score(suite(failed=(A,)), suite(passed=(A,)), [path, "src/app.py"])

        assert scored.outcome == TaskOutcome.FAILED
        assert scored.disqualified == (path,)
        assert "diff touches test or config files" in scored.reason

    def test_score_fails_it_even_when_the_run_would_otherwise_pass(self, path):
        """A real fail-to-pass and no regression -- PASSED but for the config edit."""
        baseline, attempt = suite(passed=(A,), failed=(B,)), suite(passed=(A, B))

        assert score(baseline, attempt, ["src/app.py"]).outcome == TaskOutcome.PASSED
        assert score(baseline, attempt, ["src/app.py", path]).outcome == TaskOutcome.FAILED

    def test_a_crashed_harness_cannot_launder_it(self, path):
        """Disqualification is a property of the diff, so it comes before any run is read."""
        scored = score(suite(error="no image"), suite(error="boom"), [path], attempt_infrastructure_error=True)

        assert scored.outcome == TaskOutcome.FAILED and not scored.inadmissible

    def test_the_pr_gate_refuses_it_too(self, path):
        verdict = agent_verdict(suite(passed=(A,)), suite(passed=(A,)), [path])

        assert verdict.ok is False and verdict.disqualified == (path,)


class TestTheNamesAreNotMatchedLooselyOrNotAtAll:
    @pytest.mark.parametrize(
        "path", ["pytest.toml.bak", "my_pytest.toml", "src/pytest_toml.py", "docs/pytest.tomlx", "pytest.tom"]
    )
    def test_only_the_exact_basename_counts(self, path):
        assert not is_test_path(path)
        assert disqualifying_paths([path], suite(passed=(A,))) == ()

    @pytest.mark.parametrize("name", NEW_NAMES)
    def test_a_directory_with_that_name_does_not_protect_what_is_inside(self, name):
        """Basename, not path component: a source file under a directory called
        `pytest.toml` is not config."""
        assert not is_test_path(f"{name}/module.py")
