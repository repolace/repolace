"""`harness.report.load_rows` and `build_report` against real tables.

Under `-m db`, for the reason `gateway/tests/test_recorder_db.py` gives: a join that
sums LLM spend, picks a first model and counts test runs is exactly the kind of SQL
a mock cannot check, and it is the one place the report touches the database.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from eval_support import T0, add_call, add_eval_task, add_test_run, run_manifest, seed_bench_repo
from harness.report import ReportError, build_report, load_rows
from repolace_shared.db.models import Task, TaskOutcome, TaskStatus

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class TestLoadRows:
    async def test_a_task_is_joined_with_its_spend_first_model_and_test_runs(self, db_session):
        repo = await seed_bench_repo(db_session)
        task = await add_eval_task(
            db_session, repo, outcome=TaskOutcome.PASSED, agent_stop_reason="submitted",
            score_reason="3 fail-to-pass", patch_diff="diff --git a/x b/x\n", started_at=T0,
            completed_at=T0 + timedelta(seconds=90),
        )
        # Inserted out of order on purpose: the first model is the earliest by created_at.
        add_call(db_session, task, model="openai/later", cost="0.30", input_tokens=300, cached=100, output=30,
                 created_at=T0 + timedelta(seconds=20))
        add_call(db_session, task, model="anthropic/first", cost="0.10", input_tokens=100, cached=0, output=10,
                 created_at=T0 + timedelta(seconds=1))
        add_call(db_session, task, model="anthropic/first", cost=None, input_tokens=None, cached=None, output=None,
                 created_at=T0 + timedelta(seconds=30))
        add_test_run(db_session, task, 0)
        add_test_run(db_session, task, 1)
        add_test_run(db_session, task, 2, error="attempt unscoreable: timeout")
        await db_session.commit()

        (row,) = await load_rows(db_session, ["run-a"])

        assert (row.eval_run_id, row.instance_id, row.run_index) == ("run-a", "psf__requests-1001", 0)
        assert (row.status, row.outcome) == (TaskStatus.COMPLETED, TaskOutcome.PASSED)
        assert (row.agent_stop_reason, row.score_reason) == ("submitted", "3 fail-to-pass")
        assert row.model == "anthropic/first"
        assert row.llm_calls == 3 and row.unpriced_calls == 1
        assert row.cost_usd == Decimal("0.40000000")
        assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (400, 100, 40)
        assert row.attempts == 2  # attempts 1 and 2; the baseline is attempt 0
        assert row.last_run_error == "attempt unscoreable: timeout"
        assert row.patch_diff == "diff --git a/x b/x\n"
        assert (row.completed_at - row.started_at).total_seconds() == 90
        assert row.test_edit_approved is False

    async def test_a_task_with_no_calls_or_runs_has_no_cost_and_no_model(self, db_session):
        repo = await seed_bench_repo(db_session)
        await add_eval_task(db_session, repo, status=TaskStatus.FAILED, error_message="boom")

        (row,) = await load_rows(db_session, ["run-a"])

        assert row.cost_usd is None and row.model is None
        assert (row.llm_calls, row.unpriced_calls, row.attempts) == (0, 0, 0)
        assert (row.input_tokens, row.cached_input_tokens, row.output_tokens) == (0, 0, 0)
        assert row.last_run_error is None
        assert row.error_message == "boom"

    async def test_a_baseline_only_task_has_no_attempts(self, db_session):
        repo = await seed_bench_repo(db_session)
        task = await add_eval_task(db_session, repo)
        add_test_run(db_session, task, 0, error="baseline unscoreable")
        await db_session.commit()

        (row,) = await load_rows(db_session, ["run-a"])

        assert row.attempts == 0 and row.last_run_error == "baseline unscoreable"

    async def test_only_the_requested_runs_are_loaded_in_a_stable_order(self, db_session):
        repo = await seed_bench_repo(db_session)
        await add_eval_task(db_session, repo, instance_id="b-2", eval_run_id="run-a", run_index=1)
        await add_eval_task(db_session, repo, instance_id="a-1", eval_run_id="run-a", run_index=0)
        await add_eval_task(db_session, repo, instance_id="a-1", eval_run_id="run-b", run_index=0)
        await add_eval_task(db_session, repo, instance_id="c-3", eval_run_id="run-other", run_index=0)

        rows = await load_rows(db_session, ["run-b", "run-a"])

        assert [(r.eval_run_id, r.instance_id, r.run_index) for r in rows] == [
            ("run-a", "a-1", 0), ("run-a", "b-2", 1), ("run-b", "a-1", 0),
        ]

    async def test_a_product_task_is_never_a_benchmark_row(self, db_session):
        repo = await seed_bench_repo(db_session)
        db_session.add(Task(
            repo_id=repo.id, issue_number=1, issue_title="t", issue_url="u", target_branch="main",
            status=TaskStatus.COMPLETED,
        ))
        await add_eval_task(db_session, repo)
        await db_session.commit()

        assert len(await load_rows(db_session, ["run-a"])) == 1

    async def test_a_signed_off_test_edit_is_flagged_but_is_still_the_same_outcome(self, db_session):
        repo = await seed_bench_repo(db_session)
        await add_eval_task(
            db_session, repo, outcome=TaskOutcome.PASSED_WITH_TEST_EDIT, test_edit_justification="the test was wrong",
            test_edit_approved_by="maintainer", test_edit_approved_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        (row,) = await load_rows(db_session, ["run-a"])

        assert row.outcome is TaskOutcome.PASSED_WITH_TEST_EDIT and row.test_edit_approved is True

    async def test_a_mistyped_run_id_is_an_error_not_a_short_report(self, db_session):
        repo = await seed_bench_repo(db_session)
        await add_eval_task(db_session, repo)

        with pytest.raises(ReportError, match="no tasks for eval run id\\(s\\): run-typo"):
            await load_rows(db_session, ["run-a", "run-typo"])

    async def test_no_run_ids_is_an_error(self, db_session):
        with pytest.raises(ReportError, match="no eval run ids"):
            await load_rows(db_session, [])


class TestBuildReport:
    async def test_the_headline_comes_out_of_the_tables(self, db_session):
        repo = await seed_bench_repo(db_session)
        outcomes = [TaskOutcome.PASSED, TaskOutcome.PASSED, TaskOutcome.FAILED]
        for index, outcome in enumerate(outcomes):
            task = await add_eval_task(db_session, repo, instance_id=f"inst-{index}", outcome=outcome,
                                       started_at=T0, completed_at=T0 + timedelta(seconds=10))
            add_call(db_session, task, cost="0.25")
        await add_eval_task(db_session, repo, instance_id="inst-3", status=TaskStatus.FAILED)
        await add_eval_task(db_session, repo, instance_id="inst-4", outcome=None)
        await db_session.commit()

        report, rows = await build_report(db_session, ["run-a"])

        assert len(rows) == 5
        overall = report.overall
        assert (overall.counts["passed"], overall.counts["failed"]) == (2, 1)
        assert (overall.counts["harness_error"], overall.counts["inadmissible"]) == (1, 1)
        # No manifest: the headline is over the five rows that exist, and says so.
        (headline,) = report.headlines
        assert (headline.passed, headline.planned) == (2, 5) and "UNANCHORED" in headline.flags
        assert (headline.secondary.passed, headline.secondary.admissible) == (2, 3)
        assert report.cost_usd.total == pytest.approx(0.75)

    async def test_a_manifest_anchors_the_headline_to_the_planned_grid_not_to_the_rows(self, db_session):
        repo = await seed_bench_repo(db_session)
        for index in range(3):
            await add_eval_task(db_session, repo, instance_id=f"inst-{index}", outcome=TaskOutcome.PASSED)
        await db_session.commit()
        manifest = run_manifest(5, runs=1)  # inst-3 and inst-4 were planned and never enqueued

        report, _ = await build_report(db_session, ["run-a"], manifests={"run-a": manifest})
        assert report.headlines[0].withheld.startswith("headline withheld: 2 of 5 planned rows missing/unfinished")

        partial, _ = await build_report(db_session, ["run-a"], manifests={"run-a": manifest}, allow_partial=True)
        assert (partial.headlines[0].passed, partial.headlines[0].planned) == (3, 5)
        assert "PARTIAL (3 of 5)" in partial.headlines[0].flags

    async def test_gold_runs_are_reported_apart_from_the_agent_runs(self, db_session):
        repo = await seed_bench_repo(db_session)
        await add_eval_task(db_session, repo, instance_id="inst-0", outcome=TaskOutcome.FAILED)
        for run in ("gold-1", "gold-2"):
            await add_eval_task(db_session, repo, instance_id="inst-0", eval_run_id=run, outcome=TaskOutcome.PASSED)

        report, _ = await build_report(db_session, ["run-a"], ["gold-1", "gold-2"])

        assert report.overall.rows == 1
        assert [(g.instance_id, g.status) for g in report.gold.instances] == [("inst-0", "validated")]
        assert report.gold.validated == 1

    async def test_a_gold_run_given_as_an_agent_run_is_refused(self, db_session):
        repo = await seed_bench_repo(db_session)
        await add_eval_task(db_session, repo, eval_run_id="gold-1")

        with pytest.raises(ReportError, match="start with 'gold-': pass them with --gold-run"):
            await build_report(db_session, ["gold-1"])

    async def test_an_agent_run_given_as_a_gold_run_is_refused(self, db_session):
        repo = await seed_bench_repo(db_session)
        await add_eval_task(db_session, repo)

        with pytest.raises(ReportError, match="would be counted as agent runs"):
            await build_report(db_session, ["run-a"], ["run-a"])
