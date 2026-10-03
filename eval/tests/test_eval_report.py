"""`harness.report.aggregate`: the headline arithmetic, pinned, including what it excludes.

The expectations are written as fractions (5/8, not 0.625) so a reviewer can check
them against the module docstring's definitions by hand.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from eval_support import rows_for, run_manifest, task_row
from harness.report import (
    CONTAMINATION_CAVEAT,
    FAILED,
    HARNESS_ERROR,
    INADMISSIBLE,
    PASSED,
    PASSED_WITH_TEST_EDIT,
    UNFINISHED,
    ReportError,
    TaskRow,
    aggregate,
    attach_instance_data,
    classify,
    describe,
    export_predictions,
    find_duplicates,
    load_manifests,
    main,
    percentile,
    resolve_duplicates,
    to_json,
    to_markdown,
)
from repolace_shared.db.models import TaskOutcome as O
from repolace_shared.db.models import TaskStatus as S
from harness.run_manifest import dump_manifest
from repolace_shared.instances import InstanceSpec

KINDS = {
    "passed": dict(outcome=O.PASSED),
    "failed": dict(outcome=O.FAILED),
    "pwte": dict(outcome=O.PASSED_WITH_TEST_EDIT, score_reason="diff touches test or config files: tests/x.py"),
    "inadmissible": dict(outcome=None, score_reason="baseline unscoreable: container timed out"),
    "harness": dict(outcome=None, status=S.FAILED, agent_stop_reason=None, error_message="repolace broke"),
    "llm_error": dict(outcome=O.FAILED, agent_stop_reason="llm_error"),
    "unfinished": dict(outcome=None, status=S.RUNNING, agent_stop_reason=None),
}


def sweep(run_index: int, kinds: list[str], **overrides) -> list[TaskRow]:
    return [
        task_row(instance_id=f"inst-{i}", run_index=run_index, **{**KINDS[kind], **overrides})
        for i, kind in enumerate(kinds)
    ]


#: Ten instances, three runs. Per run: (passed, admissible, planned) =
#: run 0: (5, 8, 10)   run 1: (6, 9, 10)   run 2: (4, 9, 10)
RUN_0 = ["passed"] * 5 + ["failed"] * 2 + ["pwte", "inadmissible", "harness"]
RUN_1 = ["passed"] * 6 + ["failed"] * 3 + ["harness"]
RUN_2 = ["passed"] * 4 + ["failed"] * 4 + ["pwte", "inadmissible"]


@pytest.fixture
def three_runs() -> list[TaskRow]:
    return sweep(0, RUN_0) + sweep(1, RUN_1) + sweep(2, RUN_2)


class TestClassification:
    @pytest.mark.parametrize(
        "kind,bucket",
        [
            ("passed", PASSED), ("failed", FAILED), ("pwte", PASSED_WITH_TEST_EDIT),
            ("inadmissible", INADMISSIBLE), ("harness", HARNESS_ERROR), ("llm_error", FAILED),
            ("unfinished", UNFINISHED),
        ],
    )
    def test_each_kind_has_one_bucket(self, kind, bucket):
        assert classify(task_row(**KINDS[kind])) == bucket

    @pytest.mark.parametrize("status", [S.QUEUED, S.RUNNING])
    def test_a_task_still_in_flight_is_unfinished_whatever_it_says_so_far(self, status):
        assert classify(task_row(status=status, outcome=O.PASSED, agent_stop_reason="llm_error")) == UNFINISHED

    def test_an_llm_error_stop_is_counted_by_its_outcome_never_excluded(self):
        # The agent loop reports llm_error for non-transient model errors too (a context
        # overflow, an unparseable tool call), which depend on the instance. The scorer
        # already scored the row; dropping it for its stop reason would raise the rate.
        assert classify(task_row(outcome=O.PASSED, agent_stop_reason="llm_error")) == PASSED
        assert classify(task_row(outcome=O.FAILED, agent_stop_reason="llm_error")) == FAILED
        assert classify(task_row(outcome=None, agent_stop_reason="llm_error")) == INADMISSIBLE

    def test_status_failed_is_a_harness_error_even_with_an_outcome(self):
        assert classify(task_row(status=S.FAILED, outcome=O.PASSED)) == HARNESS_ERROR

    def test_a_budget_stop_is_the_agents_failure_not_the_harnesss(self):
        for reason in ("budget_usd", "budget_calls", "budget_wall", "step_cap", "max_attempts", "no_change"):
            assert classify(task_row(outcome=O.FAILED, agent_stop_reason=reason)) == FAILED

    def test_a_finished_row_with_no_outcome_is_inadmissible_not_failed(self):
        for status in (S.COMPLETED, S.PR_OPENED, S.CONFLICTING):
            assert classify(task_row(status=status, outcome=None)) == INADMISSIBLE


def headline_of(rows, manifest=None, **kwargs):
    """The one headline of a one-run report."""
    report = aggregate(rows, manifests={manifest.eval_run_id: manifest} if manifest else None, **kwargs)
    (headline,) = report.headlines
    return headline


class TestLlmErrorRows:
    def test_scored_llm_error_rows_stay_in_the_denominator(self):
        # The audit's repro: 8 passed, 6 failed, 6 llm_error scored FAILED. Dropping the
        # llm_error rows printed 8/14 = 57.1%; counting them is 8/20 = 40%.
        rows = sweep(0, ["passed"] * 8 + ["failed"] * 6 + ["llm_error"] * 6)
        headline = headline_of(rows, run_manifest(20, runs=1))
        assert (headline.passed, headline.planned, headline.rate) == (8, 20, 8 / 20)
        assert (headline.secondary.passed, headline.secondary.admissible) == (8, 20)
        overall = aggregate(rows).overall
        assert overall.counts[HARNESS_ERROR] == 0 and overall.counts[FAILED] == 12

    def test_a_passed_llm_error_row_counts_as_a_pass(self):
        rows = [task_row(instance_id="a", outcome=O.PASSED, agent_stop_reason="llm_error"), task_row(instance_id="b", outcome=O.FAILED)]
        assert headline_of(rows, run_manifest(["a", "b"], runs=1)).passed == 1

    def test_llm_error_rows_are_shown_in_their_own_column_and_a_warning(self):
        rows = sweep(0, ["passed", "failed", "llm_error", "llm_error"])
        report = aggregate(rows)
        assert report.overall.llm_errors == 2
        assert any("2 row(s) stopped on llm_error (2 scored by their outcome, 0 with no outcome)" in w for w in report.warnings)
        assert "of which llm_error" in to_markdown(report)

    def test_an_unfinished_llm_error_row_is_not_counted_as_one(self):
        rows = [task_row(status=S.RUNNING, outcome=None, agent_stop_reason="llm_error")]
        assert aggregate(rows).overall.llm_errors == 0

    def test_the_harness_error_table_shows_the_outcome_and_the_reason(self):
        row = task_row(status=S.FAILED, outcome=O.PASSED, score_reason="1 fail-to-pass", error_message="push failed")
        (ref,) = aggregate([row]).harness_errors
        assert "outcome=passed" in ref.detail and "score: 1 fail-to-pass" in ref.detail and "push failed" in ref.detail

    def test_a_harness_error_with_no_outcome_says_so(self):
        (ref,) = aggregate([task_row(**KINDS["harness"])]).harness_errors
        assert "outcome=-" in ref.detail


class TestHeadlineIsPassedOverPlanned:
    """`passed / planned`, every non-pass a non-pass. The secondary figure excludes, and says so."""

    def test_the_three_runs_pin_every_number(self, three_runs):
        headline = headline_of(three_runs, run_manifest(10, runs=3))

        assert (headline.instances, headline.runs_per_instance, headline.planned) == (10, 3, 30)
        assert (headline.passed, headline.rate) == (15, 15 / 30)
        assert headline.anchored and headline.withheld is None and headline.flags == ()
        assert [(r.run_index, r.passed, r.planned) for r in headline.per_run] == [(0, 5, 10), (1, 6, 10), (2, 4, 10)]
        assert headline.run_range == (pytest.approx(0.4), pytest.approx(0.6))
        # The secondary: harness errors (2) and inadmissible rows (2) taken out of the denominator.
        secondary = headline.secondary
        assert (secondary.passed, secondary.admissible) == (15, 8 + 9 + 9)
        assert (secondary.excluded_harness_errors, secondary.excluded_inadmissible) == (2, 2)
        assert secondary.rate == pytest.approx(15 / 26)

    def test_every_non_pass_is_a_non_pass_in_the_headline_denominator(self):
        rows = sweep(0, ["passed", "failed", "pwte", "inadmissible", "harness"])
        headline = headline_of(rows, run_manifest(5, runs=1))
        assert (headline.passed, headline.planned, headline.rate) == (1, 5, 0.2)

    def test_the_secondary_excludes_exactly_the_instrument_failures(self):
        rows = sweep(0, ["passed", "failed", "pwte", "inadmissible", "harness"])
        secondary = headline_of(rows, run_manifest(5, runs=1)).secondary
        assert (secondary.passed, secondary.admissible, secondary.rate) == (1, 3, 1 / 3)
        assert (secondary.excluded_harness_errors, secondary.excluded_inadmissible) == (1, 1)

    def test_passed_with_test_edit_is_never_a_pass_even_when_signed_off(self):
        rows = [task_row(**KINDS["pwte"], test_edit_approved=True), task_row(instance_id="b", **KINDS["failed"])]
        report = aggregate(rows, manifests={"run-a": run_manifest(["inst-0", "b"], runs=1)})
        # (the first row's instance id is "psf__requests-1001" by default: it is unplanned)
        assert report.passed_with_test_edit.approved == 1
        assert report.headlines[0].passed == 0

    def test_every_finished_row_is_in_exactly_one_bucket(self, three_runs):
        counts = aggregate(three_runs).overall.counts
        assert sum(counts.values()) == 30 and counts[PASSED] == 15 and counts[FAILED] == 9
        assert (counts[PASSED_WITH_TEST_EDIT], counts[INADMISSIBLE], counts[HARNESS_ERROR]) == (2, 2, 2)

    def test_the_markdown_leads_with_counts_then_the_percentage_then_the_interval(self, three_runs):
        text = to_markdown(aggregate(three_runs, manifests={"run-a": run_manifest(10, runs=3)}))
        assert "**Pass rate: 15 of 30 instance-runs (50%) [95% interval over 10 instances: " in text
        assert "N = 10 instances from 1 Python repository, 3 runs each, model anthropic/test-main, pipeline commit c0ffee" in text
        assert "Secondary, not the headline: passed / admissible = 15 of 26 (58%), excluding 2 harness-error and 2 inadmissible row(s) (listed)." in text
        assert "Every planned (instance, run) row is counted: 2 harness-error and 2 inadmissible row(s)" in text

    def test_the_run_to_run_range_says_it_is_not_an_interval(self, three_runs):
        text = to_markdown(aggregate(three_runs, manifests={"run-a": run_manifest(10, runs=3)}))
        assert "Run-to-run range of the 3 run rates: 40%-60%. This holds the instances fixed; it is not an interval." in text
        assert "| 0 | 5 of 10 (50%) | 50% |" in text

    def test_a_single_run_has_no_range(self):
        headline = headline_of(sweep(0, ["passed", "failed"]), run_manifest(2, runs=1))
        assert headline.run_range is None and len(headline.per_run) == 1

    def test_one_instance_has_no_interval_and_the_text_says_why(self):
        rows = [task_row(instance_id="inst-0", run_index=r) for r in range(3)]
        report = aggregate(rows, manifests={"run-a": run_manifest(1, runs=3)})
        assert report.headlines[0].interval is None
        assert "no interval: fewer than 2 instances" in to_markdown(report)

    def test_no_rows_and_no_manifest_is_no_headline_and_no_error(self):
        report = aggregate([])
        assert report.headlines == () and "No agent runs." in to_markdown(report)

    def test_repositories_come_from_the_instance_data_even_for_a_missing_row(self):
        rows = [task_row(instance_id="inst-0", repo=None)]
        headline = headline_of(
            rows, run_manifest(2, runs=1), allow_partial=True,
        )
        assert headline.repos == 0
        report = aggregate(rows, manifests={"run-a": run_manifest(2, runs=1)}, allow_partial=True,
                           instance_repos={"inst-0": "psf/requests", "inst-1": "pallets/flask"})
        assert report.headlines[0].repos == 2


UNCURATED = "3 fail-to-pass, no regressions (uncurated: not evidence this issue specifically was fixed)"
CURATED = "all 3 expected tests pass, no regressions"


class TestPassesThatProveLess:
    """A pass the instrument cannot stand behind is marked on the figure itself, and warned about first."""

    def test_uncurated_passes_mark_the_headline_and_lead_the_warnings(self):
        # The audit's repro: ten such rows printed 100% with no warning at all.
        rows = sweep(0, ["passed"] * 10, score_reason=UNCURATED)
        report = aggregate(rows, manifests={"run-a": run_manifest(10, runs=1)})
        assert "UNCURATED (10 of 10 passes)" in report.headlines[0].flags
        assert "UNCURATED (10 of 10 passes)" in report.warnings[0]
        assert "not that this issue was fixed" in report.warnings[0]
        assert "(100%) [95% interval over 10 instances: 100%-100%]** **UNCURATED (10 of 10 passes)**" in to_markdown(report)

    def test_only_the_uncurated_passes_are_counted(self):
        rows = [task_row(instance_id=f"inst-{i}", score_reason=CURATED if i % 2 else UNCURATED) for i in range(4)]
        assert "UNCURATED (2 of 4 passes)" in headline_of(rows, run_manifest(4, runs=1)).flags

    def test_a_curated_pass_is_not_marked(self):
        rows = sweep(0, ["passed"] * 3, score_reason=CURATED)
        assert headline_of(rows, run_manifest(3, runs=1)).flags == ()

    def test_an_uncurated_failure_is_not_a_pass_to_mark(self):
        rows = sweep(0, ["passed", "failed"], score_reason=UNCURATED)
        assert "UNCURATED (1 of 1 passes)" in headline_of(rows, run_manifest(2, runs=1)).flags

    def test_a_pass_that_made_no_llm_call_is_marked_and_warned_about(self):
        # The audit's repro: 20 gold-agent rows filed under a normal run id, model None,
        # zero LLM calls, printed 100%.
        rows = sweep(0, ["passed"] * 20, model=None, llm_calls=0, cost=None)
        report = aggregate(rows, manifests={"run-a": run_manifest(20, runs=1)})
        assert "NO-LLM-CALL PASSES (20 of 20)" in report.headlines[0].flags
        assert "gold or stub run" in report.warnings[0]
        assert "**NO-LLM-CALL PASSES (20 of 20)**" in to_markdown(report)

    def test_a_failure_with_no_llm_call_is_not_flagged(self):
        rows = sweep(0, ["passed", "harness"], llm_calls=0)
        assert headline_of(rows, run_manifest(2, runs=1)).flags == ("NO-LLM-CALL PASSES (1 of 1)",)
        rows = [task_row(instance_id="inst-0"), task_row(instance_id="inst-1", llm_calls=0, **KINDS["harness"])]
        assert headline_of(rows, run_manifest(2, runs=1)).flags == ()

    @pytest.mark.parametrize("agent", ["gold", "stub"])
    def test_a_manifest_that_says_the_run_was_not_an_llm_agent_is_refused(self, agent):
        with pytest.raises(ReportError, match=f"is a '{agent}' run according to its manifest"):
            aggregate(sweep(0, ["passed"]), manifests={"run-a": run_manifest(1, runs=1, agent=agent)})

    def test_an_llm_manifest_is_accepted(self):
        assert headline_of(sweep(0, ["passed"]), run_manifest(1, runs=1, agent="llm")).passed == 1


class TestInterval:
    def test_it_is_over_instances_deterministic_and_documented(self, three_runs):
        manifest = run_manifest(10, runs=3)
        first = headline_of(three_runs, manifest).interval
        again = headline_of(list(reversed(three_runs)), manifest).interval
        assert first == again
        assert (first.instances, first.resamples, first.seed, first.confidence) == (10, 10_000, 0, 0.95)
        assert first.low < 0.5 < first.high

    def test_it_is_wider_than_one_that_pretends_the_runs_are_independent(self):
        # 20 instances x 3 runs: 7 always pass, 8 never, 5 mixed (28 of 60). An interval
        # over the sixty rows would be about 24 points wide; over instances it is wider.
        passes = [3] * 7 + [1, 1, 2, 2, 1] + [0] * 8
        rows = []
        for i, k in enumerate(passes):
            for run in range(3):
                rows.append(task_row(instance_id=f"inst-{i}", run_index=run, outcome=O.PASSED if run < k else O.FAILED))
        interval = headline_of(rows, run_manifest(20, runs=3)).interval
        assert 0.30 < interval.high - interval.low < 0.46

    def test_the_wording_pins_whole_number_percentages(self):
        passes = [3] * 7 + [1, 1, 2, 2, 1] + [0] * 8
        rows = [
            task_row(instance_id=f"inst-{i}", run_index=run, outcome=O.PASSED if run < k else O.FAILED)
            for i, k in enumerate(passes) for run in range(3)
        ]
        text = to_markdown(aggregate(rows, manifests={"run-a": run_manifest(20, runs=3)}))
        assert "**Pass rate: 28 of 60 instance-runs (47%) [95% interval over 20 instances: " in text
        import re

        match = re.search(r"interval over 20 instances: (\d+)%-(\d+)%\]", text)
        assert match and 25 <= int(match[1]) <= 35 and 58 <= int(match[2]) <= 70


class TestPlannedGrid:
    def test_a_run_with_unfinished_rows_withholds_its_headline(self):
        # The audit's repro: a partial sweep whose unfinished rows are exactly the failures
        # printed 66.7% beside a top-of-page warning. Now there is no figure at all.
        rows = sweep(0, ["passed"] * 10, ) + sweep(1, ["passed"] * 10) + sweep(2, ["unfinished"] * 10)
        headline = headline_of(rows, run_manifest(10, runs=3))
        assert headline.rate is None and headline.interval is None and headline.per_run == ()
        assert headline.withheld.startswith("headline withheld: 10 of 30 planned rows missing/unfinished (0 missing, 10 unfinished)")
        assert len(headline.unfinished) == 10 and headline.missing == ()

    def test_the_markdown_prints_no_pass_rate_for_a_withheld_run(self):
        rows = sweep(0, ["passed"] * 10) + sweep(1, ["unfinished"] * 10)
        text = to_markdown(aggregate(rows, manifests={"run-a": run_manifest(10, runs=2)}))
        assert "Headline withheld: 10 of 20 planned rows missing/unfinished" in text
        assert "Pass rate:" not in text
        assert "Unfinished (10): inst-0#1" in text

    def test_a_planned_row_that_does_not_exist_withholds_it_and_is_listed(self):
        rows = sweep(0, ["passed"] * 8)  # inst-8 and inst-9 never ran
        report = aggregate(rows, manifests={"run-a": run_manifest(10, runs=1)})
        headline = report.headlines[0]
        assert headline.withheld.startswith("headline withheld: 2 of 10 planned rows missing/unfinished (2 missing, 0 unfinished)")
        assert headline.missing == ("inst-8#0", "inst-9#0")
        assert [(r.instance_id, r.run_index) for r in report.missing] == [("inst-8", 0), ("inst-9", 0)]
        assert "### Missing (2)" in to_markdown(report)

    def test_allow_partial_prints_the_figure_marked_partial_with_the_gaps_as_non_passes(self):
        rows = sweep(0, ["passed"] * 8)
        headline = headline_of(rows, run_manifest(10, runs=1), allow_partial=True)
        assert headline.withheld is None
        assert (headline.passed, headline.planned, headline.rate) == (8, 10, 0.8)
        assert "PARTIAL (8 of 10)" in headline.flags
        assert "**Pass rate: 8 of 10 instance-runs (80%)" in to_markdown(aggregate(rows, manifests={"run-a": run_manifest(10, runs=1)}, allow_partial=True))

    def test_a_manifest_with_no_rows_at_all_withholds_everything(self):
        report = aggregate([], manifests={"run-a": run_manifest(4, runs=2)})
        assert report.headlines[0].withheld.startswith("headline withheld: 8 of 8 planned rows missing/unfinished")

    def test_a_row_the_manifest_does_not_plan_is_ignored_and_named(self):
        rows = sweep(0, ["passed", "passed"]) + [task_row(instance_id="stray", outcome=O.PASSED)]
        report = aggregate(rows, manifests={"run-a": run_manifest(2, runs=1)})
        assert report.headlines[0].passed == 2 and report.headlines[0].planned == 2
        assert report.headlines[0].unplanned == ("stray#0",)
        assert any("not in the manifest's grid" in w and "stray#0" in w for w in report.warnings)

    def test_a_long_missing_list_is_truncated_in_the_markdown(self):
        text = to_markdown(aggregate([], manifests={"run-a": run_manifest(30, runs=1)}))
        assert "Missing (30): inst-0#0" in text and "... (10 more)" in text

    def test_each_run_has_its_own_headline_and_nothing_is_pooled(self):
        rows = sweep(0, ["passed", "passed"]) + [
            task_row(instance_id=f"inst-{i}", eval_run_id="run-b", model="openai/other", outcome=O.FAILED) for i in range(2)
        ]
        report = aggregate(rows, manifests={"run-a": run_manifest(2, runs=1), "run-b": run_manifest(2, runs=1, run_id="run-b", model="openai/other")})
        assert [(h.run_id, h.model, h.passed, h.planned) for h in report.headlines] == [
            ("run-a", "anthropic/test-main", 2, 2), ("run-b", "openai/other", 0, 2),
        ]


class TestMixedModelsInOneRun:
    def two_models(self) -> list[TaskRow]:
        return [
            task_row(instance_id=f"inst-{i}", model="a/x" if i < 3 else "b/y", outcome=O.PASSED) for i in range(6)
        ]

    def test_a_run_that_switched_model_has_no_pooled_headline(self):
        # The audit's repro: a model switch mid-sweep fired a warning and still printed one pooled rate.
        headline = headline_of(self.two_models(), run_manifest(6, runs=1))
        assert headline.rate is None and headline.interval is None
        assert "mixed models within the run (a/x, b/y): no pooled figure, read the By model counts" in headline.withheld

    def test_the_markdown_prints_the_per_model_counts_and_no_pass_rate(self):
        text = to_markdown(aggregate(self.two_models(), manifests={"run-a": run_manifest(6, runs=1)}))
        assert "Pass rate:" not in text
        assert "| a/x | 3 | 3 | 3 |" in text and "| b/y | 3 | 3 | 3 |" in text

    def test_a_partial_and_mixed_run_gives_both_reasons(self):
        rows = self.two_models()[:5]
        headline = headline_of(rows, run_manifest(6, runs=1))
        assert "1 of 6 planned rows missing/unfinished" in headline.withheld and "mixed models" in headline.withheld

    def test_allow_partial_does_not_override_mixed_models(self):
        headline = headline_of(self.two_models(), run_manifest(6, runs=1), allow_partial=True)
        assert headline.rate is None and "mixed models" in headline.withheld

    def test_a_row_with_no_model_is_not_a_second_model(self):
        rows = [task_row(instance_id="inst-0"), task_row(instance_id="inst-1", model=None, llm_calls=0, **KINDS["harness"])]
        assert headline_of(rows, run_manifest(2, runs=1)).withheld is None


class TestUnanchoredRuns:
    def test_no_manifest_marks_the_headline_unanchored_and_warns(self):
        report = aggregate(sweep(0, ["passed", "failed"]))
        headline = report.headlines[0]
        assert not headline.anchored and "UNANCHORED" in headline.flags
        assert (headline.passed, headline.planned) == (1, 2)
        assert any("No run manifest for run-a" in w for w in report.warnings)
        assert "**UNANCHORED**" in to_markdown(report)
        assert "pipeline commit unknown (no manifest)" in to_markdown(report)

    def test_unfinished_rows_withhold_an_unanchored_headline_too(self):
        headline = headline_of(sweep(0, ["passed", "unfinished"]))
        assert headline.withheld.startswith("headline withheld: 1 of 2 planned rows")

    def test_unequal_runs_per_instance_are_marked_and_the_pooled_count_is_the_figure(self):
        # One instance with 3 passing runs, nine instances with one failing run each:
        # an average of instance rates would say 10%; the pooled count is 3 of 12.
        rows = [task_row(instance_id="inst-0", run_index=r) for r in range(3)]
        rows += [task_row(instance_id=f"inst-{i}", run_index=0, outcome=O.FAILED) for i in range(1, 10)]
        headline = headline_of(rows)
        assert "UNEQUAL RUNS PER INSTANCE" in headline.flags
        assert (headline.passed, headline.planned, headline.rate) == (3, 12, 0.25)

    def test_a_one_row_run_does_not_weigh_like_a_nine_row_run(self):
        # The audit's repro: run 0 = 1 of 1, run 1 = 0 of 9 printed 50% beside a pooled 1 of 10.
        rows = [task_row(instance_id="inst-0", run_index=0)] + [
            task_row(instance_id=f"inst-{i}", run_index=1, outcome=O.FAILED) for i in range(9)
        ]
        headline = headline_of(rows)
        assert (headline.passed, headline.planned, headline.rate) == (1, 10, 0.1)


class TestManifestLoading:
    def test_a_manifest_that_exists_anchors_its_run(self, tmp_path):
        dump_manifest(run_manifest(3, runs=2), tmp_path)
        manifests = load_manifests(tmp_path, ["run-a"], None)
        assert manifests["run-a"].runs_per_instance == 2

    def test_a_missing_manifest_is_skipped_by_default_and_an_error_when_expected(self, tmp_path):
        assert load_manifests(tmp_path, ["run-a"], None) == {}
        with pytest.raises(ReportError, match="no manifest for run-a"):
            load_manifests(tmp_path, ["run-a"], True)

    def test_no_expect_ignores_manifests_that_exist(self, tmp_path):
        dump_manifest(run_manifest(3, runs=2), tmp_path)
        assert load_manifests(tmp_path, ["run-a"], False) == {}

    def test_a_malformed_manifest_is_an_error_never_a_silent_fallback(self, tmp_path):
        (tmp_path / "run-a").mkdir()
        (tmp_path / "run-a" / "manifest.json").write_text('{"eval_run_id": "run-a"}')
        with pytest.raises(ReportError, match="missing key"):
            load_manifests(tmp_path, ["run-a"], None)


class TestModels:
    def test_per_model_counts_are_kept_apart(self):
        rows = []
        for run in range(3):
            rows += rows_for([O.PASSED, O.FAILED], run_index=run, prefix="m", model="anthropic/main")
        rows += rows_for([O.PASSED, O.PASSED], run_index=0, prefix="m", model="openai/other", eval_run_id="run-b")
        report = aggregate(rows)

        assert (report.by_model["anthropic/main"].passed, report.by_model["anthropic/main"].finished) == (3, 6)
        assert (report.by_model["openai/other"].passed, report.by_model["openai/other"].finished) == (2, 2)

    def test_mixed_models_are_flagged_and_never_pooled_into_one_headline(self):
        rows = rows_for([O.PASSED], model="a/x") + rows_for([O.FAILED], prefix="other", model="b/y", eval_run_id="run-b")
        report = aggregate(rows)
        assert report.mixed_models
        assert any("MIXED MODELS (a/x, b/y)" in w for w in report.warnings)
        assert len(report.headlines) == 2

    def test_one_model_is_not_mixed(self):
        assert not aggregate(rows_for([O.PASSED, O.FAILED])).mixed_models

    def test_a_row_with_no_llm_call_has_its_own_column(self):
        report = aggregate([task_row(model=None, **KINDS["harness"]), task_row(instance_id="b")])
        assert set(report.by_model) == {"anthropic/test-main", "(no model call)"}
        assert not report.mixed_models

    def test_the_same_instance_and_run_twice_for_one_model_is_flagged(self):
        rows = [task_row(eval_run_id="run-a"), task_row(eval_run_id="run-b")]
        assert any("appear more than once" in w for w in aggregate(rows).warnings)


T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestDuplicateRows:
    """A re-run needs a new eval run id, so it creates a second row for the same pair."""

    def rerun_rows(self):
        first = [task_row(instance_id=f"inst-{i}", eval_run_id="run-a", created_at=T1, outcome=O.FAILED) for i in range(5)]
        again = [task_row(instance_id=f"inst-{i}", eval_run_id="run-b", created_at=T1 + timedelta(days=1)) for i in range(5)]
        return first + again

    def test_the_same_pair_in_two_runs_is_a_duplicate(self):
        assert len(find_duplicates(self.rerun_rows())) == 5

    def test_a_row_with_no_model_is_a_wildcard_so_the_pair_is_still_a_duplicate(self):
        # The audit's repro: the duplicate key included the model, so a model-less first
        # attempt beside the real-model re-run got no warning.
        rows = [task_row(eval_run_id="run-a", model=None, llm_calls=0, **KINDS["harness"]), task_row(eval_run_id="run-b")]
        assert len(find_duplicates(rows)) == 1
        assert any("appear more than once" in w for w in aggregate(rows).warnings)

    def test_two_different_models_on_the_same_pair_are_a_comparison_not_a_duplicate(self):
        rows = [task_row(eval_run_id="run-a", model="a/x"), task_row(eval_run_id="run-b", model="b/y")]
        assert find_duplicates(rows) == []

    def test_gold_rows_repeat_on_purpose_and_are_never_duplicates(self):
        rows = [task_row(eval_run_id="gold-1"), task_row(eval_run_id="gold-2")]
        assert find_duplicates(rows) == []
        kept, dropped = resolve_duplicates(rows, "latest")
        assert len(kept) == 2 and dropped == ()

    def test_the_default_refuses_and_lists_them(self):
        with pytest.raises(ReportError, match=r"5 \(instance, run_index\) pair\(s\) have more than one row.*inst-0#0 \(run-a, run-b\).*--supersede latest"):
            resolve_duplicates(self.rerun_rows())

    def test_latest_keeps_the_newest_of_each_pair_and_prints_what_it_dropped(self):
        rows = self.rerun_rows()
        kept, dropped = resolve_duplicates(rows, "latest")
        assert len(kept) == 5 and {r.eval_run_id for r in kept} == {"run-b"}
        assert [(d.instance_id, d.eval_run_id) for d in dropped] == [(f"inst-{i}", "run-a") for i in range(5)]
        assert all("superseded by the newer row in run-b" in d.detail for d in dropped)

    def test_the_audits_double_count_is_gone(self):
        # Five failed rows re-run under run-b: 10 rows for 5 pairs. Superseded, 5 rows count once.
        kept, dropped = resolve_duplicates(self.rerun_rows(), "latest")
        report = aggregate(kept, superseded=dropped)
        assert report.overall.rows == 5 and report.overall.counts[PASSED] == 5
        assert len(report.superseded) == 5
        assert any("--supersede latest dropped 5 older row(s)" in w for w in report.warnings)
        assert "### Superseded by --supersede latest (5), not counted anywhere" in to_markdown(report)

    def test_a_row_with_no_created_at_is_the_oldest(self):
        rows = [task_row(eval_run_id="run-a", created_at=None), task_row(eval_run_id="run-b", created_at=T1)]
        kept, _ = resolve_duplicates(rows, "latest")
        assert [r.eval_run_id for r in kept] == ["run-b"]

    def test_three_rows_for_a_pair_keep_only_the_newest(self):
        rows = [task_row(eval_run_id=f"run-{c}", created_at=T1 + timedelta(hours=h)) for c, h in (("a", 2), ("b", 3), ("c", 1))]
        kept, dropped = resolve_duplicates(rows, "latest")
        assert [r.eval_run_id for r in kept] == ["run-b"] and len(dropped) == 2

    def test_no_duplicates_changes_nothing_in_either_mode(self):
        rows = rows_for([O.PASSED, O.FAILED])
        for mode in ("none", "latest"):
            assert resolve_duplicates(rows, mode) == (rows, ())

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ReportError, match="unknown --supersede mode 'newest'"):
            resolve_duplicates([], "newest")


class TestPercentiles:
    def test_one_value_is_its_own_percentile(self):
        assert percentile([7.0], 95) == 7.0 and percentile([7.0], 0) == 7.0

    def test_two_values_interpolate(self):
        assert percentile([1.0, 2.0], 95) == pytest.approx(1.95)
        assert percentile([2.0, 1.0], 50) == pytest.approx(1.5)

    def test_median_of_four_is_between_the_middle_two(self):
        assert percentile([4.0, 1.0, 3.0, 2.0], 50) == 2.5

    def test_p95_of_five_lies_between_the_top_two(self):
        assert percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)

    def test_the_extremes(self):
        assert percentile([3, 1, 2], 0) == 1 and percentile([3, 1, 2], 100) == 3

    def test_nothing_has_no_percentile(self):
        assert percentile([], 95) is None

    @pytest.mark.parametrize("q", [-1, 101])
    def test_q_out_of_range(self, q):
        with pytest.raises(ValueError):
            percentile([1.0], q)

    def test_describe_reports_n_mean_median_p95_and_total(self):
        stats = describe([0.2, 0.4, 1.0])
        assert stats.n == 3
        assert stats.mean == pytest.approx(1.6 / 3)
        assert stats.median == 0.4
        assert stats.p95 == pytest.approx(0.4 + (1.0 - 0.4) * 0.9)
        assert (stats.minimum, stats.maximum, stats.total) == (0.2, 1.0, pytest.approx(1.6))

    def test_describe_of_nothing(self):
        assert describe([]).n == 0 and describe([]).mean is None


class TestCostLatencyTokens:
    def test_cost_stats_cover_finished_rows_including_harness_errors(self):
        rows = [
            task_row(instance_id="a", cost="0.20"),
            task_row(instance_id="b", cost="0.40"),
            task_row(instance_id="c", cost="1.00", **KINDS["harness"]),
        ]
        stats = aggregate(rows).cost_usd
        assert stats.n == 3 and stats.total == pytest.approx(1.6) and stats.median == 0.4

    def test_a_row_with_no_cost_is_counted_not_averaged_as_zero(self):
        report = aggregate([task_row(instance_id="a", cost="0.20"), task_row(instance_id="b", cost=None)])
        assert report.cost_usd.n == 1 and report.cost_usd.mean == 0.2
        assert report.cost_rows_without_data == 1

    def test_unfinished_spend_is_reported_separately(self):
        rows = [task_row(instance_id="a", cost="0.20"), task_row(instance_id="b", cost="0.70", **KINDS["unfinished"])]
        report = aggregate(rows)
        assert report.cost_usd.total == pytest.approx(0.2)
        assert report.unfinished_cost_usd == pytest.approx(0.7)

    def test_latency_is_completed_minus_started(self):
        stats = aggregate([task_row(instance_id="a", seconds=10), task_row(instance_id="b", seconds=30)]).latency_seconds
        assert (stats.n, stats.mean, stats.median) == (2, 20.0, 20.0)

    def test_a_row_without_both_timestamps_or_with_a_negative_span_is_skipped_and_counted(self):
        backwards = task_row(instance_id="c", seconds=-5)
        report = aggregate([task_row(instance_id="a", seconds=10), task_row(instance_id="b", seconds=None), backwards])
        assert report.latency_seconds.n == 1 and report.latency_rows_without_data == 2

    def test_token_totals_and_the_cache_read_ratio(self):
        rows = [
            task_row(instance_id="a", input_tokens=1000, cached_input_tokens=250, output_tokens=10, llm_calls=2),
            task_row(instance_id="b", input_tokens=3000, cached_input_tokens=750, output_tokens=30, llm_calls=4),
        ]
        tokens = aggregate(rows).tokens
        assert (tokens.input_tokens, tokens.cached_input_tokens, tokens.output_tokens, tokens.calls) == (4000, 1000, 40, 6)
        assert tokens.cache_read_ratio == 0.25

    def test_no_input_tokens_means_no_ratio(self):
        row = task_row(input_tokens=0, cached_input_tokens=0, llm_calls=0, cost=None)
        assert aggregate([row]).tokens.cache_read_ratio is None

    def test_unpriced_calls_are_warned_about(self):
        report = aggregate([task_row(unpriced_calls=2)])
        assert report.tokens.unpriced_calls == 2
        assert any("2 LLM call(s) carry no cost" in w for w in report.warnings)


class TestDistributions:
    def test_attempts_and_stop_reasons_count_finished_rows(self):
        rows = [
            task_row(instance_id="a", attempts=1),
            task_row(instance_id="b", attempts=1),
            task_row(instance_id="c", attempts=3, agent_stop_reason="max_attempts", outcome=O.FAILED),
            task_row(instance_id="d", attempts=0, **KINDS["harness"]),
            task_row(instance_id="e", attempts=2, **KINDS["unfinished"]),
        ]
        report = aggregate(rows)
        assert report.attempts == {0: 1, 1: 2, 3: 1}
        assert report.stop_reasons == {"(none)": 1, "max_attempts": 1, "submitted": 2}


class TestByRepoAndTargetedP2P:
    def test_outcome_by_repo(self):
        rows = [
            task_row(instance_id="a", repo="psf/requests", outcome=O.PASSED),
            task_row(instance_id="b", repo="psf/requests", outcome=O.FAILED),
            task_row(instance_id="c", repo="pallets/flask", outcome=O.PASSED),
            task_row(instance_id="d", repo=None, outcome=O.FAILED, targeted_p2p=None),
        ]
        report = aggregate(rows)
        assert (report.by_repo["psf/requests"].passed, report.by_repo["psf/requests"].finished) == (1, 2)
        assert (report.by_repo["pallets/flask"].passed, report.by_repo["pallets/flask"].finished) == (1, 1)
        assert report.by_repo["(unknown)"].counts[FAILED] == 1

    def test_targeted_pass_to_pass_rows_and_instances_are_counted(self):
        rows = [
            task_row(instance_id="a", targeted_p2p=True, run_index=0),
            task_row(instance_id="a", targeted_p2p=True, run_index=1),
            task_row(instance_id="b", targeted_p2p=False),
            task_row(instance_id="c", targeted_p2p=None),
        ]
        report = aggregate(rows)
        assert (report.targeted_p2p.rows, report.targeted_p2p.instances, report.targeted_p2p.unknown_rows) == (2, ("a",), 1)
        warnings = "\n".join(report.warnings)
        assert "2 row(s) ran pass-to-pass over a targeted subset" in warnings
        assert "1 row(s) have no instance data" in warnings

    def test_instance_data_fills_repo_and_targeted_p2p_and_unknown_ids_stay_unknown(self):
        spec = InstanceSpec(
            instance_id="a", repo="psf/requests", base_commit="a" * 40, version="2.0", problem_statement="x",
            issue_number=1, fail_to_pass=("t",), pass_to_pass=(), test_files={"tests/t.py": ""}, gold_files={},
            spec={"test_targets": ["tests"]}, targeted_p2p=True,
        )
        rows = attach_instance_data([task_row(instance_id="a", repo=None, targeted_p2p=None),
                                     task_row(instance_id="zzz", repo=None, targeted_p2p=None)], {"a": spec})
        assert (rows[0].repo, rows[0].targeted_p2p) == ("psf/requests", True)
        assert (rows[1].repo, rows[1].targeted_p2p) == (None, None)


class TestGoldValidation:
    def gold(self, instance: str, run: str, kind: str) -> TaskRow:
        return task_row(instance_id=instance, eval_run_id=run, **KINDS[kind])

    def test_gold_rows_are_in_no_agent_figure(self):
        rows = rows_for([O.PASSED, O.FAILED]) + [self.gold("inst-0", "gold-1", "passed")]
        report = aggregate(rows)
        assert report.overall.rows == 2 and report.agent_runs == ("run-a",) and report.gold_runs == ("gold-1",)

    def test_statuses(self):
        rows = [
            self.gold("ok", "gold-1", "passed"), self.gold("ok", "gold-2", "passed"),
            self.gold("once", "gold-1", "passed"),
            self.gold("flaky", "gold-1", "passed"), self.gold("flaky", "gold-2", "failed"),
            self.gold("bad", "gold-1", "failed"), self.gold("bad", "gold-2", "failed"),
            self.gold("broken", "gold-1", "harness"), self.gold("broken", "gold-2", "inadmissible"),
            self.gold("busy", "gold-1", "passed"), self.gold("busy", "gold-2", "unfinished"),
        ]
        status = {g.instance_id: g.status for g in aggregate(rows).gold.instances}
        assert status == {
            "ok": "validated", "once": "validated-once", "flaky": "flaky", "bad": "failed",
            "broken": "unscoreable", "busy": "pending",
        }

    def test_one_passing_run_is_not_validated_because_flakiness_is_unchecked(self):
        gold = aggregate([self.gold("a", "gold-1", "passed")]).gold
        assert gold.validated == 0

    def test_a_gold_pass_with_a_test_edit_does_not_validate(self):
        rows = [self.gold("a", "gold-1", "pwte"), self.gold("a", "gold-2", "pwte")]
        assert aggregate(rows).gold.instances[0].status == "failed"

    def test_the_reason_a_gold_run_did_not_pass_is_kept(self):
        rows = [self.gold("a", "gold-1", "inadmissible"), self.gold("a", "gold-2", "inadmissible")]
        assert "baseline unscoreable" in aggregate(rows).gold.instances[0].detail

    def test_agent_instances_without_validated_gold_are_named(self):
        rows = rows_for([O.PASSED, O.PASSED]) + [
            self.gold("inst-0", "gold-1", "passed"), self.gold("inst-0", "gold-2", "passed"),
        ]
        report = aggregate(rows)
        assert report.gold.agent_instances_not_validated == ("inst-1",)
        assert any("1 instance(s) in the agent runs have no validated gold" in w for w in report.warnings)

    def test_no_gold_rows_at_all_is_a_warning_not_silence(self):
        assert any("No gold-validation rows were supplied" in w for w in aggregate(rows_for([O.PASSED])).warnings)


class TestRendering:
    def test_the_markdown_states_the_counts_and_the_exclusions(self, three_runs):
        text = to_markdown(aggregate(three_runs, manifests={"run-a": run_manifest(10, runs=3)}))
        assert "### Harness errors (2)" in text
        assert "### Inadmissible (2)" in text
        assert "### passed_with_test_edit (2: 0 approved, 2 pending)" in text
        assert "every planned row that did not pass is a non-pass" in text.replace("\n", " ")
        assert "baseline unscoreable: container timed out" in text

    def test_the_markdown_carries_the_contamination_caveat_and_the_interval_magnitude(self, three_runs):
        text = to_markdown(aggregate(three_runs))
        assert CONTAMINATION_CAVEAT in text
        assert "about 30 to 40 percentage points wide" in text

    def test_a_table_cell_cannot_break_the_table(self):
        row = task_row(outcome=None, score_reason="a | b\nc")
        assert "a \\| b c" in to_markdown(aggregate([row]))

    def test_json_round_trips_the_key_numbers(self, three_runs):
        document = json.loads(to_json(aggregate(three_runs, manifests={"run-a": run_manifest(10, runs=3)})))
        assert document["overall"]["counts"]["passed"] == 15
        headline = document["headlines"][0]
        assert (headline["passed"], headline["planned"], headline["anchored"]) == (15, 30, True)
        assert headline["interval"]["instances"] == 10 and headline["secondary"]["admissible"] == 26
        assert document["instances_never_admissible"] == ["inst-9"]
        assert document["caveats"][0] == CONTAMINATION_CAVEAT

    def test_the_report_is_deterministic(self, three_runs):
        manifests = {"run-a": run_manifest(10, runs=3)}
        assert to_json(aggregate(three_runs, manifests=manifests)) == to_json(aggregate(list(reversed(three_runs)), manifests=manifests))
        assert to_markdown(aggregate(three_runs)) == to_markdown(aggregate(list(reversed(three_runs))))


class TestPredictionsExport:
    def test_one_json_line_per_instance(self):
        rows = [
            task_row(instance_id="b", patch_diff="diff --git a/b b/b\n"),
            task_row(instance_id="a", patch_diff="diff --git a/a b/a\n", model=None),
        ]
        lines = [json.loads(line) for line in export_predictions(rows).splitlines()]
        assert lines == [
            {"instance_id": "a", "model_name_or_path": "repolace", "model_patch": "diff --git a/a b/a\n"},
            {"instance_id": "b", "model_name_or_path": "anthropic/test-main", "model_patch": "diff --git a/b b/b\n"},
        ]

    def test_a_task_that_never_reached_the_agent_has_no_prediction_but_an_empty_patch_is_one(self):
        rows = [task_row(instance_id="a", patch_diff=None), task_row(instance_id="b", patch_diff="")]
        assert [json.loads(l)["instance_id"] for l in export_predictions(rows).splitlines()] == ["b"]

    def test_two_runs_of_one_instance_are_refused_not_silently_collapsed(self):
        rows = [task_row(run_index=0, patch_diff="x"), task_row(run_index=1, patch_diff="y")]
        with pytest.raises(ValueError, match="pick one run_index"):
            export_predictions(rows)

    def test_a_gold_row_is_refused_because_it_is_not_a_prediction(self):
        with pytest.raises(ValueError, match="reference patch"):
            export_predictions([task_row(eval_run_id="gold-1", patch_diff="x")])

    def test_nothing_to_export_is_an_empty_string(self):
        assert export_predictions([]) == ""


class TestCommandLine:
    def test_a_run_id_is_required(self, capsys):
        assert main([]) == 2
        assert "at least one --run" in capsys.readouterr().err

    def test_help_exits_zero(self, capsys):
        assert main(["--help"]) == 0
        out = capsys.readouterr().out
        assert "--gold-run" in out and "--allow-partial" in out and "--expect" in out and "--runs-dir" in out
        assert "--supersede" in out

    def test_an_unknown_supersede_mode_is_a_usage_error(self):
        assert main(["--run", "x", "--supersede", "oldest"]) == 2

    def test_a_bad_option_is_a_usage_error(self, capsys):
        assert main(["--nope"]) == 2

    def test_a_missing_instances_directory_is_an_error(self, tmp_path, capsys):
        assert main(["--run", "x", "--instances-dir", str(tmp_path / "nope")]) == 2
        assert "is not a directory" in capsys.readouterr().err

    def test_importing_the_report_does_not_import_the_model_stack(self):
        import subprocess
        import sys

        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, harness.report, harness.select_instances, harness.specgen, harness.metrics;"
             "print('torch' in sys.modules, 'sentence_transformers' in sys.modules)"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert out == "False False"

