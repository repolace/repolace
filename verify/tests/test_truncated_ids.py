"""Curated ids that SWE-bench cut at a space are read as prefixes of the real ids.

`test_x[a: str = None-Optional[str]]` is stored upstream as `test_x[a:` (the id was split on
whitespace). Matching exactly, such an instance can never score PASSED however good the fix.
"""

from repolace_shared.db.models import TaskOutcome
from verify.scoring import expand_truncated_ids, expected_not_red, score

from verify_support import suite

FULL_A = "tests/t.py::test_x[a: str = None-Optional[str]]"
FULL_B = "tests/t.py::test_x[b: int = 1-int]"
CUT_A = "tests/t.py::test_x[a:"
PLAIN = "tests/t.py::test_plain"


class TestExpansion:
    def test_a_cut_id_stands_for_the_ids_that_continue_it(self):
        assert expand_truncated_ids([CUT_A], [FULL_A, FULL_B]) == (FULL_A,)

    def test_every_continuation_is_included(self):
        other = "tests/t.py::test_x[a: float = 1.0-float]"

        assert expand_truncated_ids([CUT_A], [FULL_A, other, FULL_B]) == tuple(sorted((FULL_A, other)))

    def test_the_continuation_must_start_after_a_space(self):
        """`[a:` is not a prefix of `[a:b]`, which is a different parameter."""
        assert expand_truncated_ids([CUT_A], ["tests/t.py::test_x[a:b]"]) == (CUT_A,)

    def test_a_cut_id_with_no_continuation_is_kept_so_it_still_reads_as_missing(self):
        assert expand_truncated_ids([CUT_A], [PLAIN]) == (CUT_A,)

    def test_balanced_ids_are_untouched(self):
        assert expand_truncated_ids([PLAIN, "tests/t.py::test_y[1-2]"], [FULL_A]) == tuple(
            sorted((PLAIN, "tests/t.py::test_y[1-2]"))
        )


class TestScoring:
    def test_a_fix_whose_cut_id_now_passes_scores_passed(self):
        baseline = suite(failed=(FULL_A,), passed=(PLAIN,))
        attempt = suite(passed=(FULL_A, PLAIN))

        result = score(baseline, attempt, ["src/app.py"], expected_fail_to_pass=(CUT_A,))

        assert result.outcome == TaskOutcome.PASSED

    def test_one_continuation_still_failing_is_a_failure(self):
        other = "tests/t.py::test_x[a: float = 1.0-float]"
        baseline = suite(failed=(FULL_A, other), passed=(PLAIN,))
        attempt = suite(passed=(FULL_A, PLAIN), failed=(other,))

        result = score(baseline, attempt, ["src/app.py"], expected_fail_to_pass=(CUT_A,))

        assert result.outcome == TaskOutcome.FAILED

    def test_a_cut_id_the_run_never_produced_is_not_credited(self):
        baseline = suite(failed=(PLAIN,))
        attempt = suite(passed=(PLAIN,))

        result = score(baseline, attempt, ["src/app.py"], expected_fail_to_pass=(CUT_A,))

        assert result.outcome == TaskOutcome.FAILED


class TestNotRed:
    def test_a_cut_id_whose_continuation_failed_at_baseline_is_red(self):
        assert expected_not_red(suite(failed=(FULL_A,)), [CUT_A]) == ()

    def test_a_cut_id_whose_continuation_already_passed_is_not_red(self):
        assert expected_not_red(suite(passed=(FULL_A,)), [CUT_A]) == (FULL_A,)
