"""`expected_not_red`: curated fail-to-pass ids that were never red at baseline.

`score(expected_fail_to_pass=...)` checks that every expected test passes after
the patch and never that it was failing before. If one already passes in our
environment, every other condition is met by a patch that changes nothing -- so
a comment edit scores PASSED, and the verdict is well-formed enough that nothing
would notice. This is the function that notices.
"""

import inspect

import pytest

from repolace_shared.db.models import TaskOutcome
from verify.scoring import expected_not_red, score

from verify_support import suite

GATED = "tests/test_api.py::test_gated"
OTHER = "tests/test_api.py::test_other"


class TestRed:
    def test_a_failed_test_is_red(self):
        assert expected_not_red(suite(failed=(GATED,)), [GATED]) == ()

    def test_an_xfailed_test_is_red(self):
        """A known bug the repository wrote down: red, and going green is the
        likeliest honest form of fail-to-pass."""
        assert expected_not_red(suite(xfailed=(GATED,)), [GATED]) == ()

    def test_a_test_in_a_module_that_would_not_import_is_red(self):
        """None of its tests ran, so none is in `failed` -- but the module was red."""
        baseline = suite(collect_failures=("tests/test_api.py",))

        assert expected_not_red(baseline, [GATED]) == ()

    def test_every_expected_test_red_returns_nothing(self):
        baseline = suite(failed=(GATED,), xfailed=(OTHER,))

        assert expected_not_red(baseline, [GATED, OTHER]) == ()


class TestNotRed:
    def test_an_already_passing_test_is_reported(self):
        assert expected_not_red(suite(passed=(GATED,)), [GATED]) == (GATED,)

    def test_an_ordinary_skip_is_not_red(self):
        """An `importorskip` that starts passing is evidence of nothing, which is the
        reason `xfailed` is a separate bucket from `skipped`."""
        assert expected_not_red(suite(skipped=(GATED,)), [GATED]) == (GATED,)

    def test_a_test_absent_from_the_baseline_is_reported(self):
        """A test the baseline never ran cannot have failed there. With the overlay
        applied it should have been collected, so absence is itself a defect."""
        assert expected_not_red(suite(passed=(OTHER,)), [GATED]) == (GATED,)

    def test_a_test_that_did_not_run_is_reported(self):
        assert expected_not_red(suite(did_not_run=(GATED,)), [GATED]) == (GATED,)

    def test_only_the_offenders_are_returned(self):
        baseline = suite(failed=(GATED,), passed=(OTHER,))

        assert expected_not_red(baseline, [GATED, OTHER]) == (OTHER,)

    def test_the_result_is_sorted_and_deduplicated(self):
        baseline = suite(passed=(GATED, OTHER))

        assert expected_not_red(baseline, [OTHER, GATED, OTHER]) == (GATED, OTHER)


class TestCollectFailureCoverage:
    def test_a_sibling_module_is_not_covered(self):
        """`::` is part of the prefix: `tests/test_api.py` must not credit
        `tests/test_api_v2.py::...`, a module that imported fine."""
        baseline = suite(collect_failures=("tests/test_api.py",), passed=("tests/test_api_v2.py::test_x",))

        assert expected_not_red(baseline, ["tests/test_api_v2.py::test_x"]) == ("tests/test_api_v2.py::test_x",)

    def test_a_directory_shaped_failure_covers_nothing(self):
        """Same limit `collection_fixed` documents: matching on `<dir>/` would credit
        a whole subtree for one module, so the safe half is to cover nothing."""
        baseline = suite(collect_failures=("tests",))

        assert expected_not_red(baseline, [GATED]) == (GATED,)

    def test_it_uses_the_same_prefixes_as_the_scorer(self):
        """The three functions must agree about what a collection error covers, or an
        id can count as red here and credit nothing there."""
        from verify.scoring import collection_fixed

        baseline = suite(collect_failures=("tests/test_api.py",))
        attempt = suite(passed=(GATED,))

        assert collection_fixed(baseline, attempt) == (GATED,)
        assert expected_not_red(baseline, [GATED]) == ()


class TestEmptyAndShapes:
    def test_an_empty_expected_list_returns_nothing(self):
        assert expected_not_red(suite(passed=(GATED,)), []) == ()
        assert expected_not_red(suite(), ()) == ()

    def test_it_accepts_a_tuple_a_list_and_returns_a_tuple(self):
        baseline = suite(passed=(GATED,))

        assert expected_not_red(baseline, (GATED,)) == (GATED,)
        assert isinstance(expected_not_red(baseline, [GATED]), tuple)

    def test_it_does_not_look_at_the_attempt_at_all(self):
        """The signature takes only the baseline: what the agent did cannot excuse a
        test that was never red."""
        assert list(inspect.signature(expected_not_red).parameters) == ["baseline", "expected"]


class TestTheHoleItCloses:
    def test_score_credits_a_noop_when_the_expected_test_already_passed(self):
        """The defect, demonstrated against `score()` itself: the expected test
        passes before and after, a non-test file is touched, and the task is
        PASSED. `expected_not_red` is how a caller learns it was never evidence."""
        baseline = suite(passed=(GATED,))
        attempt = suite(passed=(GATED,))

        scored = score(baseline, attempt, ["src/app.py"], expected_fail_to_pass=(GATED,))

        assert scored.outcome == TaskOutcome.PASSED
        assert expected_not_red(baseline, [GATED]) == (GATED,)

    def test_an_honest_fix_is_not_flagged(self):
        baseline = suite(passed=(OTHER,), failed=(GATED,))
        attempt = suite(passed=(OTHER, GATED))

        scored = score(baseline, attempt, ["src/app.py"], expected_fail_to_pass=(GATED,))

        assert scored.outcome == TaskOutcome.PASSED
        assert expected_not_red(baseline, [GATED]) == ()

    @pytest.mark.parametrize("bucket", ["passed", "skipped", "did_not_run"])
    def test_each_non_red_bucket_is_flagged(self, bucket):
        assert expected_not_red(suite(**{bucket: (GATED,)}), [GATED]) == (GATED,)
