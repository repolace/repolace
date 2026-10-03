"""The PR gate, as a matrix.

Every row is one decision a real task can reach. The rows that matter most are
the refusals: a gate that opens a PR it should not is a repolace PR on someone's
repository with nothing behind it, and one that withholds a good one is only a
missed opportunity -- so the tests are weighted toward "closed".
"""

import itertools

import pytest

from repolace_shared.db.models import TaskOutcome
from verify.scoring import Score, Verdict

from repolace_agents.contracts import AgentResult, StopReason
from repolace_pipeline.gate import PrDecision, pr_decision


def result(stop_reason: StopReason = StopReason.SUBMITTED) -> AgentResult:
    return AgentResult(stop_reason=stop_reason, summary="did it", attempts=1, steps=3, last_attempt=None)


def scored(outcome: TaskOutcome | None) -> Score:
    return Score(outcome=outcome, reason="because", inadmissible=outcome is None)


OK = Verdict(ok=True, reason="no evidence of harm")
HARM = Verdict(ok=False, reason="2 pass-to-pass regression(s): tests/test_a.py::test_one", regressions=("t",))


def decide(**overrides) -> PrDecision:
    fields = dict(
        benchmark=False,
        has_change=True,
        scored=scored(None),
        verdict=OK,
        result=result(),
        open_pr_on_failure=False,
        open_pr_allowed=True,
    )
    return pr_decision(**{**fields, **overrides})


class TestSharedRules:
    def test_a_switched_off_run_never_opens_one(self):
        for benchmark, on_failure in itertools.product((True, False), repeat=2):
            decision = decide(
                benchmark=benchmark,
                scored=scored(TaskOutcome.PASSED),
                open_pr_on_failure=on_failure,
                open_pr_allowed=False,
            )

            assert decision.open is False
            assert "switched off" in decision.reason

    def test_no_change_never_opens_one_even_when_the_task_asked_for_a_pr_on_failure(self):
        for benchmark in (True, False):
            decision = decide(
                benchmark=benchmark,
                has_change=False,
                scored=scored(TaskOutcome.PASSED),
                open_pr_on_failure=True,
            )

            assert decision.open is False
            assert "no change" in decision.reason

    def test_switched_off_wins_over_no_change_in_the_reason(self):
        assert "switched off" in decide(open_pr_allowed=False, has_change=False).reason

    def test_a_decision_is_immutable(self):
        with pytest.raises(Exception):
            decide().open = False  # type: ignore[misc]


class TestBenchmarkMode:
    def test_passed_opens_one(self):
        decision = decide(benchmark=True, scored=scored(TaskOutcome.PASSED))

        assert decision.open is True
        assert "passed" in decision.reason

    def test_passed_opens_one_even_when_the_agent_did_not_submit(self):
        """Outcome comes from the last scored attempt, not from how the agent stopped."""
        decision = decide(
            benchmark=True, scored=scored(TaskOutcome.PASSED), result=result(StopReason.STEP_CAP), verdict=HARM
        )

        assert decision.open is True

    @pytest.mark.parametrize("outcome", [TaskOutcome.FAILED, TaskOutcome.PASSED_WITH_TEST_EDIT, None])
    def test_anything_but_passed_stays_closed(self, outcome):
        decision = decide(benchmark=True, scored=scored(outcome))

        assert decision.open is False

    def test_a_submitted_clean_agent_does_not_open_one_when_the_benchmark_says_failed(self):
        """The product rule must not leak into benchmark mode: here the score is the truth."""
        decision = decide(benchmark=True, scored=scored(TaskOutcome.FAILED), verdict=OK, result=result())

        assert decision.open is False

    @pytest.mark.parametrize("outcome", [TaskOutcome.FAILED, None])
    def test_the_failure_flag_opens_one_anyway_and_says_so(self, outcome):
        decision = decide(benchmark=True, scored=scored(outcome), open_pr_on_failure=True)

        assert decision.open is True
        assert "asked for a PR on failure" in decision.reason

    def test_the_reason_never_carries_the_scorers_sentence(self):
        """`Score.reason` names test ids, which in benchmark mode are the oracle."""
        failed = Score(outcome=TaskOutcome.FAILED, reason="1 expected test still failing: tests/test_hidden.py::test_x")

        for on_failure in (True, False):
            decision = decide(benchmark=True, scored=failed, open_pr_on_failure=on_failure)

            assert "test_hidden" not in decision.reason


class TestProductMode:
    def test_submitted_and_clean_opens_one(self):
        decision = decide(result=result(StopReason.SUBMITTED), verdict=OK)

        assert decision.open is True
        assert "submitted" in decision.reason

    def test_the_score_is_irrelevant_in_product_mode(self):
        """`score()` is inadmissible for a live issue, which is the normal case."""
        assert decide(scored=scored(None)).open is True
        assert decide(scored=scored(TaskOutcome.FAILED)).open is True

    @pytest.mark.parametrize(
        "stop",
        [s for s in StopReason if s is not StopReason.SUBMITTED],
    )
    def test_an_agent_that_did_not_submit_gets_no_pr_even_with_a_clean_verdict(self, stop):
        decision = decide(result=result(stop), verdict=OK)

        assert decision.open is False
        assert stop.value in decision.reason

    def test_a_submitted_agent_whose_change_did_harm_gets_no_pr(self):
        decision = decide(result=result(), verdict=HARM)

        assert decision.open is False
        assert "regression" in decision.reason, "the person reading the result needs to know why"

    def test_the_failure_flag_opens_one_for_an_agent_that_did_not_submit(self):
        decision = decide(result=result(StopReason.STEP_CAP), verdict=OK, open_pr_on_failure=True)

        assert decision.open is True
        assert "did not submit" in decision.reason

    def test_the_failure_flag_opens_one_for_a_change_that_did_harm_and_says_so(self):
        decision = decide(verdict=HARM, open_pr_on_failure=True)

        assert decision.open is True
        assert "flagged" in decision.reason

    def test_a_clean_submitted_change_does_not_need_the_flag_and_does_not_mention_it(self):
        decision = decide(open_pr_on_failure=True)

        assert decision.open is True
        assert "no evidence of harm" in decision.reason, "the flag must not mask the real reason"
