"""`agent_verdict`: the PR gate for a task that has no ground truth.

Written against the real function, not a stub, because streams E and F both need
a verdict to test against and E must not wait on stream A for one.

The questions it answers are the ones `score()` cannot: with the suite green and
the bug simply uncovered -- the normal case for a live issue -- did the patch
break anything, or make something stop objecting? So most of what is pinned here
is that it does NOT demand a fail-to-pass, and that each way of doing harm is
seen.
"""

import pytest

from repolace_shared.db.models import TaskOutcome
from verify.scoring import Verdict, agent_verdict, score

from verify_support import suite

A = "tests/test_a.py::test_one"
B = "tests/test_a.py::test_two"
C = "tests/test_b.py::test_three"


def verdict(baseline, attempt, changed=("src/app.py",), **kw) -> Verdict:
    return agent_verdict(baseline, attempt, list(changed), **kw)


class TestGreen:
    def test_an_unchanged_suite_is_ok(self):
        base = suite(passed=(A, B))

        result = verdict(base, suite(passed=(A, B)))

        assert result.ok is True
        assert result.regressions == () and result.neutralized == ()
        assert result.new_collect_failures == () and result.disqualified == ()

    def test_it_does_not_require_a_fail_to_pass(self):
        """The case `score()` is inadmissible for and the normal case for a live
        issue: nothing red at baseline, nothing newly green."""
        base = suite(passed=(A, B))

        assert verdict(base, suite(passed=(A, B))).ok is True
        assert score(base, suite(passed=(A, B)), ["src/app.py"]).inadmissible is True

    def test_a_baseline_failure_that_stays_failing_is_not_harm(self):
        """Not fixed, not silenced, not a regression: nothing the patch did."""
        base = suite(passed=(A,), failed=(B,))

        assert verdict(base, suite(passed=(A,), failed=(B,))).ok is True

    def test_a_baseline_failure_that_is_fixed_is_ok(self):
        base = suite(passed=(A,), failed=(B,))

        assert verdict(base, suite(passed=(A, B))).ok is True

    def test_a_baseline_collect_failure_that_persists_is_ok(self):
        """Not new, so not this patch's doing."""
        base = suite(passed=(A,), collect_failures=("tests/test_broken.py",))

        assert verdict(base, suite(passed=(A,), collect_failures=("tests/test_broken.py",))).ok is True

    def test_the_reason_says_what_ok_means(self):
        """`ok` is "no evidence of harm", and the sentence must not read as "fixed"."""
        reason = verdict(suite(passed=(A,)), suite(passed=(A,))).reason

        assert "not evidence the issue is fixed" in reason

    def test_a_test_gained_is_fine(self):
        assert verdict(suite(passed=(A,)), suite(passed=(A, B))).ok is True


class TestRegression:
    def test_a_passing_test_that_now_fails(self):
        result = verdict(suite(passed=(A, B)), suite(passed=(A,), failed=(B,)))

        assert result.ok is False
        assert result.regressions == (B,)
        assert "pass-to-pass regression" in result.reason

    def test_a_deleted_test_counts(self):
        """Absent from `attempt.failed` and from `attempt.passed`: the way to make an
        inconvenient test stop objecting."""
        result = verdict(suite(passed=(A, B)), suite(passed=(A,)))

        assert result.ok is False and result.regressions == (B,)

    def test_a_test_turned_into_a_skip_counts(self):
        result = verdict(suite(passed=(A, B)), suite(passed=(A,), skipped=(B,)))

        assert result.ok is False and result.regressions == (B,)

    def test_the_regressions_are_sorted(self):
        result = verdict(suite(passed=(C, B, A)), suite())

        assert result.regressions == (A, B, C)

    def test_the_reason_names_at_most_three(self):
        many = tuple(f"tests/test_x.py::test_{i}" for i in range(6))

        result = verdict(suite(passed=many), suite())

        assert "6 pass-to-pass regression" in result.reason
        assert result.reason.count("tests/test_x.py::") == 3
        assert len(result.regressions) == 6


class TestNewCollectFailure:
    def test_a_module_that_stopped_importing(self):
        base = suite(passed=(A, B), collect_failures=())
        attempt = suite(passed=(), collect_failures=("tests/test_a.py",))

        result = verdict(base, attempt)

        assert result.ok is False
        assert result.new_collect_failures == ("tests/test_a.py",)
        assert "no longer collect" in result.reason

    def test_it_is_a_set_difference_not_the_whole_attempt_list(self):
        base = suite(passed=(A,), collect_failures=("tests/test_old.py",))
        attempt = suite(passed=(A,), collect_failures=("tests/test_old.py", "tests/test_new.py"))

        assert verdict(base, attempt).new_collect_failures == ("tests/test_new.py",)

    def test_it_is_seen_even_when_no_baseline_test_is_lost(self):
        """A module that stops importing and had no passing tests at baseline is
        in nobody's `regressions`; its own set is what catches it."""
        base = suite(passed=(A,))
        attempt = suite(passed=(A,), collect_failures=("tests/test_extra.py",))

        result = verdict(base, attempt)

        assert result.ok is False
        assert result.regressions == ()
        assert result.new_collect_failures == ("tests/test_extra.py",)

    def test_the_new_failures_are_sorted(self):
        attempt = suite(collect_failures=("tests/test_z.py", "tests/test_a.py"))

        assert verdict(suite(), attempt).new_collect_failures == ("tests/test_a.py", "tests/test_z.py")


class TestNeutralized:
    def test_a_baseline_failure_turned_into_a_skip(self):
        base = suite(passed=(A,), failed=(B,))

        result = verdict(base, suite(passed=(A,), skipped=(B,)))

        assert result.ok is False
        assert result.neutralized == (B,)
        assert "silenced rather than fixed" in result.reason

    def test_a_baseline_failure_turned_into_an_xfail(self):
        """The cheapest way to make a red test stop objecting."""
        base = suite(passed=(A,), failed=(B,))

        result = verdict(base, suite(passed=(A,), xfailed=(B,)))

        assert result.ok is False and result.neutralized == (B,)

    def test_a_baseline_failure_that_vanished(self):
        base = suite(passed=(A,), failed=(B,))

        result = verdict(base, suite(passed=(A,)))

        assert result.ok is False and result.neutralized == (B,)


class TestDisqualified:
    def test_a_test_file_edit(self):
        result = verdict(suite(passed=(A,)), suite(passed=(A,)), changed=["tests/test_a.py"])

        assert result.ok is False
        assert result.disqualified == ("tests/test_a.py",)
        assert "test or config files" in result.reason

    def test_a_config_edit(self):
        result = verdict(suite(passed=(A,)), suite(passed=(A,)), changed=["pyproject.toml"])

        assert result.ok is False and result.disqualified == ("pyproject.toml",)

    def test_a_file_pytest_collected_from_is_disqualified(self):
        base = suite(passed=(A,), collected_files=("checks/check_foo.py",))

        result = verdict(base, suite(passed=(A,)), changed=["checks/check_foo.py"])

        assert result.ok is False and result.disqualified == ("checks/check_foo.py",)

    def test_a_shipped_module_is_spared_given_the_baseline_files(self):
        base = suite(passed=(A,), collected_files=("tests/test_client.py",))

        spared = verdict(
            base,
            suite(passed=(A,)),
            changed=["django/test/client.py"],
            baseline_files=("django/test/client.py", "tests/test_client.py"),
        )

        assert spared.ok is True
        assert verdict(base, suite(passed=(A,)), changed=["django/test/client.py"]).ok is False

    def test_it_needs_no_run_at_all(self):
        """A property of the diff: a harness crash cannot launder a test edit."""
        broken = suite(error="docker unreachable")

        result = verdict(suite(error="x"), broken, changed=["tests/test_a.py"], attempt_infrastructure_error=True)

        assert result.disqualified == ("tests/test_a.py",)

    def test_only_the_first_three_are_named_but_all_are_recorded(self):
        files = [f"tests/test_{i}.py" for i in range(5)]

        result = verdict(suite(), suite(), changed=files)

        assert result.reason.count("tests/test_") == 3
        assert len(result.disqualified) == 5


class TestPreconditions:
    def test_an_unusable_baseline(self):
        result = verdict(suite(error="image build failed"), suite(passed=(A,)))

        assert result.ok is False
        assert result.reason == "baseline unscoreable: image build failed"

    def test_an_infrastructure_error(self):
        result = verdict(suite(passed=(A,)), suite(error="daemon gone"), attempt_infrastructure_error=True)

        assert result.ok is False
        assert result.reason == "infrastructure failure: daemon gone"

    def test_an_unscoreable_attempt_is_the_patchs_doing(self):
        result = verdict(suite(passed=(A,)), suite(error="collection crashed"))

        assert result.ok is False
        assert result.reason == "attempt unscoreable: collection crashed"

    DRIFTS = {
        "rootdir": "/elsewhere",
        "ini": {"filterwarnings": ["ignore"]},
        "plugins": ["registered-by-a-new-conftest"],
    }

    @pytest.mark.parametrize("key", DRIFTS)
    def test_a_fingerprint_drift(self, key):
        """Relaxing `filterwarnings` in pyproject.toml, or registering a plugin from a
        new conftest, changes what a green run means without touching a test."""
        drifted = {"rootdir": "/repo", "ini": {}, "plugins": [], key: self.DRIFTS[key]}

        result = verdict(suite(passed=(A,)), suite(passed=(A,), fingerprint=drifted))

        assert result.ok is False
        assert result.reason == f"{key} changed between baseline and attempt"

    def test_a_drift_is_not_hidden_by_a_clean_diff(self):
        """Results from two different configurations are not comparable, however
        identical the pass sets look."""
        drifted = {"rootdir": "/repo", "ini": {"x": 1}, "plugins": []}

        assert verdict(suite(passed=(A,)), suite(passed=(A,), fingerprint=drifted)).ok is False


class TestPrecedence:
    """First match wins, so a later problem never masks an earlier one's reason."""

    def test_disqualified_beats_a_baseline_error(self):
        result = verdict(suite(error="b"), suite(error="a"), changed=["tests/test_a.py"])

        assert result.reason.startswith("diff touches")

    def test_a_baseline_error_beats_an_infrastructure_error(self):
        result = verdict(suite(error="b"), suite(error="a"), attempt_infrastructure_error=True)

        assert result.reason == "baseline unscoreable: b"

    def test_an_infrastructure_error_beats_an_attempt_error(self):
        result = verdict(suite(), suite(error="a"), attempt_infrastructure_error=True)

        assert result.reason.startswith("infrastructure failure")

    def test_an_attempt_error_beats_a_drift(self):
        drifted = {"rootdir": "/other", "ini": {}, "plugins": []}

        result = verdict(suite(), suite(error="a", fingerprint=drifted))

        assert result.reason.startswith("attempt unscoreable")

    def test_a_drift_beats_a_regression(self):
        drifted = {"rootdir": "/other", "ini": {}, "plugins": []}

        result = verdict(suite(passed=(A,)), suite(fingerprint=drifted))

        assert result.reason == "rootdir changed between baseline and attempt"
        assert result.regressions == ()


class TestReportsEveryKindOfHarm:
    def test_all_three_are_named_in_one_reason_and_recorded(self):
        base = suite(passed=(A, B), failed=(C,))
        attempt = suite(passed=(A,), skipped=(C,), collect_failures=("tests/test_b.py",))

        result = verdict(base, attempt)

        assert result.ok is False
        assert result.regressions == (B,)
        assert result.neutralized == (C,)
        assert result.new_collect_failures == ("tests/test_b.py",)
        assert "silenced" in result.reason
        assert "regression" in result.reason
        assert "no longer collect" in result.reason


class TestAgreesWithScore:
    """The equivalence test stream A's refactor must keep green.

    The precondition prefix exists twice -- here and in `score()` -- until A
    extracts it. These pin that the two report the *same reason* for every
    precondition, which is the property a shared helper has to preserve and the
    one a divergence between the benchmark and the PR gate would break.
    """

    DRIFT = {"rootdir": "/other", "ini": {}, "plugins": []}

    SCENARIOS = {
        "disqualified": dict(baseline=suite(passed=(A,)), attempt=suite(passed=(A,)), changed=["tests/test_a.py"]),
        "baseline_error": dict(baseline=suite(error="no image"), attempt=suite(passed=(A,))),
        "infrastructure": dict(
            baseline=suite(passed=(A,)), attempt=suite(error="daemon"), infra=True
        ),
        "attempt_error": dict(baseline=suite(passed=(A,)), attempt=suite(error="boom")),
        "drift": dict(baseline=suite(passed=(A,)), attempt=suite(passed=(A,), fingerprint=DRIFT)),
    }

    @pytest.mark.parametrize("name", SCENARIOS)
    def test_the_same_reason_for_every_precondition(self, name):
        s = self.SCENARIOS[name]
        changed = s.get("changed", ["src/app.py"])
        infra = s.get("infra", False)

        scored = score(s["baseline"], s["attempt"], changed, attempt_infrastructure_error=infra)
        verdict_ = agent_verdict(s["baseline"], s["attempt"], changed, attempt_infrastructure_error=infra)

        assert verdict_.ok is False
        assert verdict_.reason == scored.reason

    def test_a_regression_fails_both_and_names_the_same_tests(self):
        """Admissible for `score()` (something was red) so it reaches the regression rule."""
        base = suite(passed=(A, B), failed=(C,))
        attempt = suite(passed=(A, C))

        scored = score(base, attempt, ["src/app.py"])

        assert scored.outcome == TaskOutcome.FAILED
        assert verdict(base, attempt).ok is False
        assert verdict(base, attempt).regressions == scored.regressions == (B,)

    def test_a_silenced_failure_fails_both_and_names_the_same_tests(self):
        base = suite(passed=(A,), failed=(B,))
        attempt = suite(passed=(A,), skipped=(B,))

        scored = score(base, attempt, ["src/app.py"])

        assert scored.outcome == TaskOutcome.FAILED
        assert verdict(base, attempt).neutralized == scored.neutralized == (B,)

    def test_the_disqualified_sets_agree(self):
        base = suite(passed=(A,))
        files = ["tests/test_a.py", "pyproject.toml", "src/app.py"]

        assert score(base, base, files).disqualified == agent_verdict(base, base, files).disqualified
