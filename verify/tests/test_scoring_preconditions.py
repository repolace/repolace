"""`_preconditions`: the one place "can these two runs be compared at all" is decided.

`score()` (the benchmark) and `agent_verdict()` (the PR gate) both start with the
same five questions. They used to be two copies, and two copies are two chances for
the gate and the benchmark to disagree about what a run means. The equivalence
tests in `test_agent_verdict.py` pin that they agree today; the tests here pin
*why*: both are built on one helper, so they cannot drift by someone editing one.
"""

import pytest

from repolace_shared.db.models import TaskOutcome
from verify import scoring
from verify.scoring import Score, _preconditions, agent_verdict, score

from verify_support import suite

A = "tests/test_a.py::test_one"
B = "tests/test_a.py::test_two"
DRIFT = {"rootdir": "/other", "ini": {}, "plugins": []}


def pre(baseline, attempt, changed=("src/app.py",), baseline_files=None, infra=False):
    return _preconditions(baseline, attempt, list(changed), baseline_files, infra)


class TestWhatItDecides:
    def test_clean_runs_pass_every_precondition(self):
        assert pre(suite(passed=(A,)), suite(passed=(A,))) is None

    def test_it_does_not_look_at_test_sets(self):
        """A regression is not a precondition failure: that is the caller's next step."""
        assert pre(suite(passed=(A, B)), suite(passed=(A,))) is None

    def test_a_test_edit_fails_the_task_outright(self):
        result = pre(suite(passed=(A,)), suite(passed=(A,)), changed=["tests/test_a.py"])

        assert result.outcome == TaskOutcome.FAILED and not result.inadmissible
        assert result.disqualified == ("tests/test_a.py",)
        assert result.reason == "diff touches test or config files: tests/test_a.py"

    def test_an_unusable_baseline_is_inadmissible(self):
        result = pre(suite(error="no image"), suite(passed=(A,)))

        assert result.outcome is None and result.inadmissible
        assert result.reason == "baseline unscoreable: no image"

    def test_an_infrastructure_failure_is_inadmissible(self):
        result = pre(suite(passed=(A,)), suite(error="daemon"), infra=True)

        assert result.outcome is None and result.inadmissible
        assert result.reason == "infrastructure failure: daemon"

    def test_an_unscoreable_attempt_is_the_patchs_failure(self):
        result = pre(suite(passed=(A,)), suite(error="boom"))

        assert result.outcome == TaskOutcome.FAILED and not result.inadmissible
        assert result.reason == "attempt unscoreable: boom"

    def test_a_drifted_fingerprint_fails_the_task(self):
        result = pre(suite(passed=(A,)), suite(passed=(A,), fingerprint=DRIFT))

        assert result.outcome == TaskOutcome.FAILED
        assert result.reason == "rootdir changed between baseline and attempt"

    def test_a_shipped_module_is_spared_given_the_baseline_files(self):
        base = suite(passed=(A,), collected_files=("tests/test_client.py",))

        spared = pre(
            base, suite(passed=(A,)), changed=["django/test/client.py"],
            baseline_files=("django/test/client.py", "tests/test_client.py"),
        )

        assert spared is None
        assert pre(base, suite(passed=(A,)), changed=["django/test/client.py"]) is not None


class TestOrder:
    """First match wins; a later problem must never mask an earlier one's reason."""

    def test_disqualified_beats_everything(self):
        result = pre(suite(error="b"), suite(error="a", fingerprint=DRIFT), changed=["tests/t.py"], infra=True)

        assert result.reason.startswith("diff touches")

    def test_baseline_error_beats_infrastructure_attempt_error_and_drift(self):
        result = pre(suite(error="b"), suite(error="a", fingerprint=DRIFT), infra=True)

        assert result.reason == "baseline unscoreable: b"

    def test_infrastructure_beats_attempt_error_and_drift(self):
        result = pre(suite(), suite(error="a", fingerprint=DRIFT), infra=True)

        assert result.reason == "infrastructure failure: a"

    def test_attempt_error_beats_drift(self):
        result = pre(suite(), suite(error="a", fingerprint=DRIFT))

        assert result.reason == "attempt unscoreable: a"


SENTINEL = Score(outcome=TaskOutcome.FAILED, reason="sentinel from _preconditions", disqualified=("x.py",))


class TestBothCallersAreBuiltOnIt:
    """The property the equivalence tests only sample. Replace the helper and see
    what each caller returns: if either re-implemented a check inline, that check
    would not follow the replacement."""

    def test_score_returns_what_it_returns(self, monkeypatch):
        monkeypatch.setattr(scoring, "_preconditions", lambda *args, **kwargs: SENTINEL)

        assert score(suite(passed=(A,)), suite(passed=(A,)), ["src/app.py"]) is SENTINEL

    def test_agent_verdict_reports_the_same_reason_and_disqualified_set(self, monkeypatch):
        monkeypatch.setattr(scoring, "_preconditions", lambda *args, **kwargs: SENTINEL)

        verdict = agent_verdict(suite(passed=(A,)), suite(passed=(A,)), ["src/app.py"])

        assert verdict.ok is False
        assert verdict.reason == SENTINEL.reason
        assert verdict.disqualified == SENTINEL.disqualified

    def test_when_it_finds_nothing_both_go_on_to_compare_test_sets(self, monkeypatch):
        monkeypatch.setattr(scoring, "_preconditions", lambda *args, **kwargs: None)
        base = suite(passed=(A, B), failed=())

        assert agent_verdict(base, suite(passed=(A,)), ["src/app.py"]).regressions == (B,)
        assert score(base, suite(passed=(A,)), ["src/app.py"]).inadmissible  # nothing red at baseline

    def test_both_hand_it_the_same_arguments(self, monkeypatch):
        calls = []

        def spy(*args, **kwargs):
            # Positional and keyword spellings of the same call must compare equal, so
            # a caller switching to keyword arguments is not a spurious failure.
            names = ("baseline", "attempt", "changed_files", "baseline_files", "attempt_infrastructure_error")
            calls.append(dict(zip(names, args)) | kwargs)
            return None

        monkeypatch.setattr(scoring, "_preconditions", spy)
        base, attempt = suite(passed=(A,)), suite(passed=(A,))
        files = ("src/app.py", "tests/test_a.py")

        score(base, attempt, files, baseline_files=("src/app.py",), attempt_infrastructure_error=True)
        agent_verdict(base, attempt, files, baseline_files=("src/app.py",), attempt_infrastructure_error=True)

        assert calls[0] == calls[1] == {
            "baseline": base, "attempt": attempt, "changed_files": files,
            "baseline_files": ("src/app.py",), "attempt_infrastructure_error": True,
        }


class TestEveryPreconditionFailsBothCallers:
    """The equivalence test the brief names, over the helper's own scenarios: for
    each, `agent_verdict` is not ok AND `score` reports the same verdict."""

    SCENARIOS = {
        "disqualified": dict(baseline=suite(passed=(A,)), attempt=suite(passed=(A,)), changed=["tests/test_a.py"]),
        "baseline_error": dict(baseline=suite(error="no image"), attempt=suite(passed=(A,))),
        "infrastructure": dict(baseline=suite(passed=(A,)), attempt=suite(error="daemon"), infra=True),
        "attempt_error": dict(baseline=suite(passed=(A,)), attempt=suite(error="boom")),
        "drift": dict(baseline=suite(passed=(A,)), attempt=suite(passed=(A,), fingerprint=DRIFT)),
    }

    @pytest.mark.parametrize("name", SCENARIOS)
    def test_verdict_is_not_ok_and_score_carries_the_same_reason(self, name):
        s = self.SCENARIOS[name]
        changed = s.get("changed", ["src/app.py"])
        infra = s.get("infra", False)

        scored = score(s["baseline"], s["attempt"], changed, attempt_infrastructure_error=infra)
        verdict = agent_verdict(s["baseline"], s["attempt"], changed, attempt_infrastructure_error=infra)
        helper = pre(s["baseline"], s["attempt"], changed, infra=infra)

        assert verdict.ok is False
        assert verdict.reason == scored.reason == helper.reason
        assert scored == helper
        assert verdict.disqualified == scored.disqualified
