"""`harness.report.aggregate`: the headline arithmetic, pinned, including what it excludes.

The expectations are written as fractions (5/8, not 0.625) so a reviewer can check
them against the module docstring's definitions by hand.
"""

import json

import pytest

from eval_support import rows_for, task_row
from harness.report import (
    CONTAMINATION_CAVEAT,
    FAILED,
    HARNESS_ERROR,
    INADMISSIBLE,
    PASSED,
    PASSED_WITH_TEST_EDIT,
    UNFINISHED,
    TaskRow,
    aggregate,
    attach_instance_data,
    classify,
    describe,
    export_predictions,
    main,
    percentile,
    to_json,
    to_markdown,
)
from repolace_shared.db.models import TaskOutcome as O
from repolace_shared.db.models import TaskStatus as S
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


#: Ten instances, three runs. Per run: (passed, admissible, total) =
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
            ("inadmissible", INADMISSIBLE), ("harness", HARNESS_ERROR), ("llm_error", HARNESS_ERROR),
            ("unfinished", UNFINISHED),
        ],
    )
    def test_each_kind_has_one_bucket(self, kind, bucket):
        assert classify(task_row(**KINDS[kind])) == bucket

    @pytest.mark.parametrize("status", [S.QUEUED, S.RUNNING])
    def test_a_task_still_in_flight_is_unfinished_whatever_it_says_so_far(self, status):
        assert classify(task_row(status=status, outcome=O.PASSED, agent_stop_reason="llm_error")) == UNFINISHED

    def test_an_llm_error_stop_is_a_harness_error_even_when_the_row_scored(self):
        assert classify(task_row(outcome=O.PASSED, agent_stop_reason="llm_error")) == HARNESS_ERROR
        assert classify(task_row(outcome=O.FAILED, agent_stop_reason="llm_error")) == HARNESS_ERROR

    def test_status_failed_is_a_harness_error_even_with_an_outcome(self):
        assert classify(task_row(status=S.FAILED, outcome=O.PASSED)) == HARNESS_ERROR

    def test_a_budget_stop_is_the_agents_failure_not_the_harnesss(self):
        for reason in ("budget_usd", "budget_calls", "budget_wall", "step_cap", "max_attempts", "no_change"):
            assert classify(task_row(outcome=O.FAILED, agent_stop_reason=reason)) == FAILED

    def test_a_finished_row_with_no_outcome_is_inadmissible_not_failed(self):
        for status in (S.COMPLETED, S.PR_OPENED, S.CONFLICTING):
            assert classify(task_row(status=status, outcome=None)) == INADMISSIBLE


class TestHeadlineArithmetic:
    def test_the_three_runs_pin_every_number(self, three_runs):
        overall = aggregate(three_runs).overall

        assert overall.instances == 10 and overall.rows == 30 and overall.runs_label == "3 runs"
        assert overall.counts == {
            PASSED: 15, FAILED: 9, PASSED_WITH_TEST_EDIT: 2, INADMISSIBLE: 2, HARNESS_ERROR: 2, UNFINISHED: 0,
        }

        admissible = overall.passed_over_admissible
        assert [(r.run_index, r.numerator, r.denominator) for r in admissible.runs] == [(0, 5, 8), (1, 6, 9), (2, 4, 9)]
        assert admissible.mean == pytest.approx((5 / 8 + 6 / 9 + 4 / 9) / 3)
        assert (admissible.minimum, admissible.maximum) == (pytest.approx(4 / 9), pytest.approx(6 / 9))
        assert (admissible.numerator, admissible.denominator) == (15, 26)

        total = overall.passed_over_total
        assert [(r.numerator, r.denominator) for r in total.runs] == [(5, 10), (6, 10), (4, 10)]
        assert total.mean == pytest.approx((5 / 10 + 6 / 10 + 4 / 10) / 3)
        assert (total.minimum, total.maximum) == (pytest.approx(0.4), pytest.approx(0.6))
        assert (total.numerator, total.denominator) == (15, 30)

    def test_the_mean_is_of_per_run_rates_not_the_pooled_rate(self):
        # Run 0: 1/1 = 100%. Run 1: 1/4 = 25%. Mean of rates 62.5%; pooled 2/5 = 40%.
        rows = sweep(0, ["passed"]) + sweep(1, ["passed", "failed", "failed", "failed"])
        admissible = aggregate(rows).overall.passed_over_admissible
        assert admissible.mean == pytest.approx(0.625)
        assert admissible.numerator / admissible.denominator == pytest.approx(0.4)

    def test_a_harness_error_leaves_the_first_rate_and_stays_in_the_second(self):
        rows = sweep(0, ["passed", "failed", "harness"])
        overall = aggregate(rows).overall
        assert overall.passed_over_admissible.mean == pytest.approx(1 / 2)
        assert overall.passed_over_total.mean == pytest.approx(1 / 3)

    def test_an_inadmissible_row_leaves_the_first_rate_and_stays_in_the_second(self):
        rows = sweep(0, ["passed", "failed", "inadmissible"])
        overall = aggregate(rows).overall
        assert overall.passed_over_admissible.mean == pytest.approx(1 / 2)
        assert overall.passed_over_total.mean == pytest.approx(1 / 3)

    def test_passed_with_test_edit_is_never_a_pass_and_stays_in_both_denominators(self):
        rows = sweep(0, ["passed", "pwte", "pwte"])
        overall = aggregate(rows).overall
        assert overall.passed_over_admissible.mean == pytest.approx(1 / 3)
        assert overall.passed_over_total.mean == pytest.approx(1 / 3)
        assert overall.counts[PASSED] == 1

    def test_a_signed_off_test_edit_is_still_not_a_pass(self):
        rows = [task_row(**KINDS["pwte"], test_edit_approved=True), task_row(instance_id="b", **KINDS["failed"])]
        report = aggregate(rows)
        assert report.overall.passed_over_admissible.mean == 0.0
        assert (report.passed_with_test_edit.approved, report.passed_with_test_edit.pending) == (1, 0)

    def test_every_finished_row_is_in_exactly_one_bucket(self, three_runs):
        counts = aggregate(three_runs).overall.counts
        assert sum(counts.values()) == 30
        total_denominator = aggregate(three_runs).overall.passed_over_total.denominator
        assert total_denominator == sum(counts[b] for b in (PASSED, FAILED, PASSED_WITH_TEST_EDIT, INADMISSIBLE, HARNESS_ERROR))

    def test_unfinished_rows_are_in_no_rate_and_the_report_says_so(self):
        rows = sweep(0, ["passed", "failed", "unfinished", "unfinished"])
        report = aggregate(rows)
        assert report.overall.passed_over_admissible.mean == pytest.approx(1 / 2)
        assert report.overall.passed_over_total.denominator == 2
        assert report.overall.counts[UNFINISHED] == 2
        assert any(w.startswith("PARTIAL: 2 row(s)") for w in report.warnings)

    def test_a_run_with_only_unfinished_rows_is_not_a_run(self):
        rows = sweep(0, ["passed", "failed"]) + sweep(1, ["unfinished", "unfinished"])
        overall = aggregate(rows).overall
        assert overall.run_indexes == (0,)
        assert overall.runs_label == "1 run"

    def test_a_run_with_no_admissible_row_is_left_out_of_the_mean_not_counted_as_zero(self):
        rows = sweep(0, ["passed", "failed"]) + sweep(1, ["harness", "harness"])
        admissible = aggregate(rows).overall.passed_over_admissible
        assert [r.rate for r in admissible.runs] == [0.5, None]
        assert admissible.mean == 0.5
        assert (admissible.minimum, admissible.maximum) == (0.5, 0.5)

    def test_no_rows_gives_no_rate_and_no_error(self):
        overall = aggregate([]).overall
        assert overall.passed_over_admissible.mean is None and overall.rows == 0

    def test_all_excluded_gives_no_admissible_rate(self):
        overall = aggregate(sweep(0, ["harness", "inadmissible"])).overall
        assert overall.passed_over_admissible.mean is None
        assert overall.passed_over_total.mean == 0.0

    def test_instances_never_admissible_are_named(self, three_runs):
        # inst-9 is a harness error twice and inadmissible once.
        assert aggregate(three_runs).instances_never_admissible == ("inst-9",)

    def test_the_warnings_name_each_exclusion(self, three_runs):
        warnings = "\n".join(aggregate(three_runs).warnings)
        assert "2 harness error row(s) are excluded" in warnings
        assert "2 inadmissible row(s)" in warnings
        assert "inst-9" in warnings


class TestModels:
    def test_a_single_run_model_is_labelled_one_run_and_the_other_three(self):
        rows = []
        for run in range(3):
            rows += rows_for([O.PASSED, O.FAILED], run_index=run, prefix="m", model="anthropic/main")
        rows += rows_for([O.PASSED, O.PASSED], run_index=0, prefix="m", model="openai/other", eval_run_id="run-b")
        report = aggregate(rows)

        assert report.by_model["anthropic/main"].runs_label == "3 runs"
        assert report.by_model["openai/other"].runs_label == "1 run"
        assert report.by_model["openai/other"].passed_over_admissible.mean == 1.0
        assert report.by_model["anthropic/main"].passed_over_admissible.mean == 0.5

    def test_pooled_models_are_flagged_and_not_a_statement_about_either(self):
        rows = rows_for([O.PASSED], model="a/x") + rows_for([O.FAILED], prefix="other", model="b/y", eval_run_id="run-b")
        report = aggregate(rows)
        assert report.mixed_models
        assert any("MIXED MODELS (a/x, b/y)" in w for w in report.warnings)
        assert "pooled across models" in to_markdown(report)

    def test_one_model_is_not_mixed(self):
        assert not aggregate(rows_for([O.PASSED, O.FAILED])).mixed_models

    def test_a_row_with_no_llm_call_has_its_own_column(self):
        report = aggregate([task_row(model=None, **KINDS["harness"]), task_row(instance_id="b")])
        assert set(report.by_model) == {"anthropic/test-main", "(no model call)"}
        assert not report.mixed_models

    def test_the_same_instance_and_run_twice_for_one_model_is_flagged(self):
        rows = [task_row(eval_run_id="run-a"), task_row(eval_run_id="run-b")]
        assert any("appear more than once" in w for w in aggregate(rows).warnings)


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
        assert report.by_repo["psf/requests"].passed_over_admissible.mean == 0.5
        assert report.by_repo["pallets/flask"].passed_over_admissible.mean == 1.0
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
    def test_the_markdown_states_the_numbers_the_counts_and_the_exclusions(self, three_runs):
        text = to_markdown(aggregate(three_runs))
        assert "**N = 10 instances**, 3 runs, 30 rows." in text
        assert "| passed / admissible | 57.9% (min 44.4%, max 66.7%; 3 runs) | 15/26 |" in text
        assert "| passed / total | 50.0% (min 40.0%, max 60.0%; 3 runs) | 15/30 |" in text
        assert "### Harness errors (2)" in text
        assert "### Inadmissible (2)" in text
        assert "### passed_with_test_edit (2: 0 approved, 2 pending)" in text
        assert "never a pass and stays in both denominators" in text.replace("\n", " ")
        assert "baseline unscoreable: container timed out" in text

    def test_the_markdown_carries_the_contamination_caveat(self, three_runs):
        assert CONTAMINATION_CAVEAT in to_markdown(aggregate(three_runs))

    def test_a_single_run_says_one_run_and_no_spread(self):
        text = to_markdown(aggregate(rows_for([O.PASSED, O.FAILED])))
        assert "50.0% (1 run, no spread)" in text

    def test_a_table_cell_cannot_break_the_table(self):
        row = task_row(outcome=None, score_reason="a | b\nc")
        assert "a \\| b c" in to_markdown(aggregate([row]))

    def test_json_round_trips_the_key_numbers(self, three_runs):
        document = json.loads(to_json(aggregate(three_runs)))
        assert document["overall"]["counts"]["passed"] == 15
        assert document["overall"]["passed_over_admissible"]["numerator"] == 15
        assert document["overall"]["passed_over_admissible"]["denominator"] == 26
        assert document["instances_never_admissible"] == ["inst-9"]
        assert document["caveats"][0] == CONTAMINATION_CAVEAT

    def test_the_report_is_deterministic(self, three_runs):
        assert to_json(aggregate(three_runs)) == to_json(aggregate(list(reversed(three_runs))))
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
        assert "--gold-run" in capsys.readouterr().out

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

