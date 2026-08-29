"""Unit tests for the scoring rule.

This is the benchmark's definition, so these tests are the specification. Each
one below corresponds to a way the rule could be gamed or could be wrong; two
expert audits produced the list, and several are cases where the earlier design
would have returned PASSED or silently excluded a task.

What is deliberately NOT tested, because it cannot be: that a PASSED verdict
means the issue was really fixed. A patch that satisfies the assertion from the
source side -- special-casing the input, returning the literal the test wants --
is indistinguishable here. That needs curated ground truth and a human reading
the diff.
"""

import pytest

from repolace_shared.db.models import TaskOutcome
from verify.protocol import SuiteResult
from verify.scoring import (
    collection_fixed,
    disqualifying_paths,
    fail_to_pass,
    is_test_path,
    neutralized,
    regressions,
    score,
)


def suite(**overrides) -> SuiteResult:
    fields = {"passed": (), "failed": (), "skipped": (), "xfailed": (), "did_not_run": (),
              "collect_failures": (), "collected_files": (), "conftests": (),
              "fingerprint": {"rootdir": "/repo", "ini": {}, "plugins": []}}
    return SuiteResult(**{**fields, **overrides})


class TestIsTestPath:
    @pytest.mark.parametrize("path", [
        "tests/test_x.py", "src/tests/helper.py", "pkg/conftest.py",
        "myapp/tests.py",                       # Django: matches no test_*.py pattern
        "tests/data/expected.json",             # data under a test dir
        "__snapshots__/render.ambr",            # syrupy
        "cassettes/api.yaml",                   # vcrpy
        "pyproject.toml", "tox.ini", "setup.cfg", "pytest.ini", ".coveragerc",
        "sitecustomize.py",                     # imported before pytest exists
        ".gitattributes",                       # changes checked-out bytes
        "latest_test.py",
    ])
    def test_disqualifying(self, path):
        assert is_test_path(path) is True

    @pytest.mark.parametrize("path", [
        "src/contest.py",          # contains "test" but is not one
        "src/latest_.py",          # ends with "test" before the underscore
        "src/testing_utils.py",    # "testi" is not "test_"
        "src/app.py",
        "README.md",
    ])
    def test_not_disqualifying(self, path):
        assert is_test_path(path) is False


class TestDisqualifyingPaths:
    def test_a_file_pytest_collected_from_is_disqualified(self):
        baseline = suite(collected_files=("myapp/checks.py",))

        assert disqualifying_paths(["myapp/checks.py"], baseline) == ("myapp/checks.py",)

    def test_a_shipped_module_named_test_is_not_disqualified(self):
        """django/test/client.py is a shipped module, not a test.

        The heuristic alone flags it and would fail honest work. Existence at
        baseline plus absence from the collected set is what exonerates it.
        """
        baseline = suite(collected_files=("tests/test_client.py",))

        assert disqualifying_paths(
            ["django/test/client.py"], baseline,
            baseline_files=("django/test/client.py", "tests/test_client.py"),
        ) == ()

    def test_without_a_baseline_file_list_the_heuristic_stays_conservative(self):
        """No evidence to exonerate with, so it errs toward disqualifying --
        a false negative costs a point, a false positive costs the claim."""
        baseline = suite(collected_files=("tests/test_client.py",))

        assert disqualifying_paths(["django/test/client.py"], baseline) == ("django/test/client.py",)

    def test_a_config_file_is_disqualified_even_though_it_existed_at_baseline(self):
        """pyproject.toml is never collected, but relaxing filterwarnings in it
        turns a real failure into a real pass."""
        baseline = suite(collected_files=("tests/test_x.py",))

        assert disqualifying_paths(
            ["pyproject.toml"], baseline, baseline_files=("pyproject.toml",)
        ) == ("pyproject.toml",)

    def test_a_newly_added_test_file_is_disqualified(self):
        """It cannot appear in the baseline's collected set, so the heuristic
        is still needed alongside it."""
        baseline = suite(collected_files=("tests/test_old.py",))

        assert disqualifying_paths(["tests/test_new.py"], baseline) == ("tests/test_new.py",)

    def test_a_golden_file_beside_collected_tests_is_disqualified(self):
        """Editing the expected output is the cheapest possible fake fix."""
        baseline = suite(collected_files=("tests/test_render.py",))

        assert disqualifying_paths(["tests/expected.json"], baseline) == ("tests/expected.json",)


class TestDecisionOrder:
    def test_a_test_edit_disqualifies_even_when_the_attempt_crashed(self):
        """The incentive bug, and the cheapest fix in the whole audit.

        Under the old ordering an error check came first, so an agent that
        edited tests AND broke the harness was *excluded* from the figure rather
        than failed -- making sabotage strictly better than losing honestly.
        """
        result = score(
            suite(failed=("t::a",)),
            suite(error="verify: suite did not finish"),
            ["tests/test_x.py"],
        )

        assert result.outcome is TaskOutcome.FAILED
        assert result.inadmissible is False

    def test_an_unscoreable_baseline_is_inadmissible_not_failed(self):
        """The instrument failed before measuring; that is not the agent's verdict."""
        result = score(suite(error="verify: no report"), suite(), [])

        assert result.outcome is None and result.inadmissible is True

    def test_a_patch_that_breaks_the_suite_is_failed_not_excluded(self):
        """The image is built once, so the environment is identical across
        attempts -- a suite that now fails to collect is the patch's doing."""
        result = score(
            suite(failed=("t::a",)),
            suite(error="verify: collection failed for 1 module(s) and no test ran"),
            ["src/app.py"],
        )

        assert result.outcome is TaskOutcome.FAILED
        assert result.inadmissible is False

    def test_an_infrastructure_failure_is_inadmissible(self):
        result = score(
            suite(failed=("t::a",)),
            suite(error="verify: docker daemon unavailable"),
            ["src/app.py"],
            attempt_infrastructure_error=True,
        )

        assert result.inadmissible is True


class TestFailToPass:
    def test_a_failing_test_that_now_passes_counts(self):
        assert fail_to_pass(suite(failed=("t::a",)), suite(passed=("t::a",))) == ("t::a",)

    def test_xfail_becoming_xpass_counts(self):
        """A mature repo records a known bug as xfail, which pytest reports as a
        skip. Ignoring that transition removes the likeliest form of a
        legitimately-red test."""
        assert fail_to_pass(suite(xfailed=("t::known",)), suite(passed=("t::known",))) == ("t::known",)

    def test_an_ordinary_skip_that_starts_passing_does_not_count(self):
        """The false PASSED, reproduced and pinned.

        Baseline: `pytest.importorskip("ujson")` skips one test, nothing fails.
        Attempt: the agent adds a stub module, the test now runs and passes.
        Under the old rule that scored PASSED with nothing red at baseline, no
        test file touched, and no evidence the issue was fixed.
        """
        assert fail_to_pass(suite(skipped=("t::optional",)), suite(passed=("t::optional",))) == ()

    def test_a_test_the_agent_added_cannot_count(self):
        """It is not in the baseline, so the intersection excludes it by construction."""
        assert fail_to_pass(suite(failed=()), suite(passed=("t::new",))) == ()


class TestCollectionFixed:
    """A module that would not import is as red as a failing test, and redder:
    every test in it was lost, and none appears in `baseline.failed` because
    none ran. "Module raises ImportError at import time" is one of the
    commonest shapes a real GitHub issue takes."""

    def test_a_module_that_would_not_import_and_now_passes_counts(self):
        baseline = suite(passed=("tests/test_core.py::test_a",),
                         collect_failures=("tests/test_api.py",))
        attempt = suite(passed=("tests/test_core.py::test_a", "tests/test_api.py::test_b"))

        assert collection_fixed(baseline, attempt) == ("tests/test_api.py::test_b",)

    def test_a_prefix_does_not_match_a_sibling_module(self):
        """`::` belongs inside the prefix: without it `tests/test_api.py` also
        prefixes `tests/test_api_v2.py::test_x`, crediting a module the agent
        never touched."""
        baseline = suite(collect_failures=("tests/test_api.py",))
        attempt = suite(passed=("tests/test_api_v2.py::test_x",))

        assert collection_fixed(baseline, attempt) == ()

    def test_a_directory_level_collect_failure_is_not_credited(self):
        """Pins the documented gap. A directory id has no `.py` and its tests
        are `<dir>/<file>.py::<name>`, so matching on `<dir>/` would credit an
        entire subtree for one module starting to import. Missing them is the
        safe half; changing that should be a deliberate decision."""
        baseline = suite(collect_failures=("tests/integration",))
        attempt = suite(passed=("tests/integration/test_x.py::test_a",))

        assert collection_fixed(baseline, attempt) == ()

    def test_a_persisting_collect_failure_credits_nothing(self):
        baseline = suite(collect_failures=("tests/test_api.py",))
        attempt = suite(collect_failures=("tests/test_api.py",))

        assert collection_fixed(baseline, attempt) == ()


class TestRegressions:
    def test_a_test_that_now_fails_counts(self):
        assert regressions(suite(passed=("t::a",)), suite(failed=("t::a",))) == ("t::a",)

    def test_a_deleted_test_counts(self):
        """Set difference against `passed`, so a vanished test is caught."""
        assert regressions(suite(passed=("t::a",)), suite()) == ("t::a",)

    def test_a_test_turned_into_a_skip_counts(self):
        """Otherwise skipping is a way to silence an inconvenient test."""
        assert regressions(suite(passed=("t::a",)), suite(skipped=("t::a",))) == ("t::a",)


    def test_a_passing_test_turned_into_an_xfail_counts(self):
        """A set difference against `passed`, so it does not matter which
        non-passing bucket the test landed in."""
        assert regressions(suite(passed=("t::a",)), suite(xfailed=("t::a",))) == ("t::a",)


class TestNeutralized:
    def test_silencing_a_baseline_failure_is_caught(self):
        """Previously-failing tests were the unprotected set: not a regression,
        no fail-to-pass, no cost. Turning one into a skip was free."""
        assert neutralized(suite(failed=("t::a",)), suite(skipped=("t::a",))) == ("t::a",)

    def test_a_baseline_failure_that_vanishes_is_caught(self):
        assert neutralized(suite(failed=("t::a",)), suite()) == ("t::a",)

    def test_it_disqualifies_the_attempt(self):
        result = score(
            suite(failed=("t::a", "t::b")),
            suite(passed=("t::a",), skipped=("t::b",)),
            ["src/app.py"],
        )

        assert result.outcome is TaskOutcome.FAILED
        assert "silenced" in result.reason


    def test_marking_a_baseline_failure_xfail_is_caught(self):
        """The hole that separating the buckets could have opened. Marking a red
        test `@pytest.mark.xfail` is the cheapest possible silencing, and it now
        lands in `xfailed` rather than `skipped`."""
        assert neutralized(suite(failed=("t::a",)), suite(xfailed=("t::a",))) == ("t::a",)


class TestFingerprint:
    def test_relaxing_an_ini_option_is_caught(self):
        """`-o addopts=` clears only addopts. Relaxing `filterwarnings` in
        pyproject.toml turns a real failure into a real pass with a
        source-shaped diff, so the config is compared instead."""
        baseline = suite(failed=("t::a",), fingerprint={"rootdir": "/repo",
                                                        "ini": {"filterwarnings": ["error"]},
                                                        "plugins": []})
        attempt = suite(passed=("t::a",), fingerprint={"rootdir": "/repo",
                                                       "ini": {"filterwarnings": []},
                                                       "plugins": []})

        result = score(baseline, attempt, ["src/app.py"])

        assert result.outcome is TaskOutcome.FAILED and "ini changed" in result.reason

    def test_a_conftest_registered_plugin_is_caught(self):
        baseline = suite(failed=("t::a",), fingerprint={"rootdir": "/r", "ini": {}, "plugins": []})
        attempt = suite(passed=("t::a",),
                        fingerprint={"rootdir": "/r", "ini": {}, "plugins": ["randomly"]})

        assert score(baseline, attempt, ["src/app.py"]).outcome is TaskOutcome.FAILED


class TestAdmissibility:
    def test_an_all_green_baseline_is_inadmissible_not_failed(self):
        """With nothing red at base, `F0 ∩ Pn` is empty whatever the agent does.

        Scoring that FAILED would charge an instrument limitation to the agent
        and deflate the figure by an amount uncorrelated with capability.
        """
        result = score(suite(passed=("t::a",)), suite(passed=("t::a",)), ["src/app.py"])

        assert result.outcome is None and result.inadmissible is True

    def test_a_baseline_with_only_an_ordinary_skip_is_inadmissible(self):
        """Reproduced and pinned. The gate used to read `not baseline.failed and
        not baseline.skipped`, so one ordinary skip -- a platform guard, an
        `importorskip`, which essentially every real suite has -- made an
        unscoreable instance look scoreable. It then fell straight through to
        "no baseline-failing test now passes" and scored FAILED: an instrument
        limitation charged to the agent, on most real repositories.
        """
        result = score(
            suite(passed=("t::a",), skipped=("t::needs_torch",)),
            suite(passed=("t::a",), skipped=("t::needs_torch",)),
            ["src/app.py"],
        )

        assert result.outcome is None and result.inadmissible is True

    def test_a_baseline_xfail_keeps_the_instance_admissible(self):
        """The other direction. An xfail *is* red, so the gate must not close on
        it -- over-tightening here would silently exclude the instances the
        benchmark most wants to score."""
        result = score(
            suite(passed=("t::a",), xfailed=("t::known",)),
            suite(passed=("t::a", "t::known")),
            ["src/app.py"],
        )

        assert result.outcome is TaskOutcome.PASSED
        assert result.fail_to_pass == ("t::known",)

    def test_a_baseline_whose_only_red_is_a_collect_failure_is_admissible(self):
        """Nothing is in `failed` -- the tests never ran -- so the gate would
        otherwise exclude exactly the instances this shape describes."""
        result = score(
            suite(passed=("tests/test_core.py::test_a",), collect_failures=("tests/test_api.py",)),
            suite(passed=("tests/test_core.py::test_a", "tests/test_api.py::test_b")),
            ["src/api.py"],
        )

        assert result.outcome is TaskOutcome.PASSED
        assert result.fail_to_pass == ("tests/test_api.py::test_b",)

    def test_a_curated_list_makes_an_all_green_baseline_admissible(self):
        """With the fixing PR's tests injected, the baseline is green by design
        and the curated list is what defines success."""
        result = score(
            suite(passed=("t::a",)),
            suite(passed=("t::a", "t::new")),
            ["src/app.py"],
            expected_fail_to_pass=("t::new",),
        )

        assert result.outcome is TaskOutcome.PASSED


class TestCuratedGroundTruth:
    def test_all_expected_tests_must_pass_not_just_one(self):
        result = score(
            suite(passed=()),
            suite(passed=("t::one",)),
            ["src/app.py"],
            expected_fail_to_pass=("t::one", "t::two"),
        )

        assert result.outcome is TaskOutcome.FAILED and "still not passing" in result.reason

    def test_an_uncurated_pass_says_so_in_its_reason(self):
        """The weaker claim should be visible in the record, not implied."""
        result = score(suite(failed=("t::a",)), suite(passed=("t::a",)), ["src/app.py"])

        assert result.outcome is TaskOutcome.PASSED
        assert "uncurated" in result.reason


class TestOrdinaryOutcomes:
    def test_a_clean_fix_passes(self):
        result = score(
            suite(passed=("t::keep",), failed=("t::target",)),
            suite(passed=("t::keep", "t::target")),
            ["src/app.py"],
        )

        assert result.outcome is TaskOutcome.PASSED
        assert result.fail_to_pass == ("t::target",)

    def test_a_fix_that_breaks_something_else_fails(self):
        result = score(
            suite(passed=("t::keep",), failed=("t::target",)),
            suite(passed=("t::target",), failed=("t::keep",)),
            ["src/app.py"],
        )

        assert result.outcome is TaskOutcome.FAILED and "regression" in result.reason

    def test_changing_nothing_relevant_fails(self):
        result = score(
            suite(passed=("t::keep",), failed=("t::target",)),
            suite(passed=("t::keep",), failed=("t::target",)),
            ["src/app.py"],
        )

        assert result.outcome is TaskOutcome.FAILED
        assert "no baseline-failing test now passes" in result.reason
