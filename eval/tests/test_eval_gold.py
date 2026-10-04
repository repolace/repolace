"""`repolace-eval gold`: which instances survive two runs of the reference fix.

Every rejection reason has a test built from synthetic database rows, because the
point of the analysis is what it refuses: an instance whose curated test already
passes at baseline lets a no-op patch score PASSED, and one whose baseline moves
between two runs of the same commit makes every later score ambiguous.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from eval_exec_support import add_repo, add_task, make_instance, write_instances
from harness.gold import (
    TARGETED_P2P_FRACTION,
    main,
    GoldRun,
    build_parser,
    render_validation,
    suite_timeout_seconds,
    validate_gold,
    validate_instance,
)
from harness.enqueue import DEFAULT_WALL_CLOCK_SECONDS, task_wall_clock_bound
from verify.protocol import SuiteResult
from repolace_shared.db.models import TaskOutcome, TaskStatus, TaskTestRun

F2P = "t.py::test_new"
P2P = "t.py::test_old"
RUNS = ("gold-1", "gold-2")


def instance(**overrides):
    return make_instance(
        "a",
        fail_to_pass=(F2P,),
        pass_to_pass=(P2P, "other.py::test_x"),
        test_files={"t.py": "def test_new():\n    assert True\n"},
        **overrides,
    )


GOOD_BASELINE = {"failed": [F2P], "passed": [P2P, "other.py::test_x"], "collected_files": ["t.py", "other.py"], "duration_seconds": 40.0}
GOOD_ATTEMPT = {"passed": [F2P, P2P, "other.py::test_x"], "collected_files": ["t.py", "other.py"], "duration_seconds": 45.0}


async def add_gold_run(
    session, repo, run_id, instance_id="a", *, outcome=TaskOutcome.PASSED, status=TaskStatus.COMPLETED,
    baseline=GOOD_BASELINE, attempt=GOOD_ATTEMPT, score_reason=None, error_message=None, **task_fields,
):
    task = await add_task(
        session, repo, eval_run_id=run_id, instance_id=instance_id, run_index=0, status=status,
        outcome=outcome, score_reason=score_reason, error_message=error_message, **task_fields,
    )
    for number, fields in ((0, baseline), (1, attempt)):
        if fields is not None:
            session.add(TaskTestRun(task_id=task.id, attempt=number, commit_sha="c" * 40, **fields))
    await session.commit()
    return task


async def verdict_for(factory, spec=None, runs=RUNS):
    spec = spec or instance()
    (verdict,) = await validate_gold(factory, {spec.instance_id: spec}, runs)
    return verdict


@pytest.mark.anyio
@pytest.mark.db
class TestRejections:
    async def seed(self, session, **second):
        repo = await add_repo(session, "repolace/bench-a")
        await add_gold_run(session, repo, "gold-1")
        await add_gold_run(session, repo, "gold-2", **second)
        return repo

    async def test_an_instance_that_passes_both_runs_cleanly_is_accepted(self, db_session, db_session_factory):
        await self.seed(db_session)

        verdict = await verdict_for(db_session_factory)

        assert verdict.accepted and verdict.reasons == ()
        assert verdict.baseline_seconds == (40.0, 40.0) and verdict.gold_seconds == (45.0, 45.0)
        assert verdict.proposal is None

    async def test_a_run_that_did_not_pass_is_rejected_with_its_score_reason(self, db_session, db_session_factory):
        await self.seed(db_session, outcome=TaskOutcome.FAILED, score_reason="regressions: t.py::test_old")

        verdict = await verdict_for(db_session_factory)

        assert not verdict.accepted
        assert any("gold-2" in r and "outcome is failed" in r and "regressions: t.py::test_old" in r for r in verdict.reasons)

    async def test_a_run_that_has_not_finished_is_rejected(self, db_session, db_session_factory):
        await self.seed(db_session, outcome=None, status=TaskStatus.RUNNING, attempt=None)

        verdict = await verdict_for(db_session_factory)

        assert any("gold-2" in r and "outcome is none" in r and "running" in r for r in verdict.reasons)

    async def test_passed_with_a_test_edit_is_not_passed(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1")
        # The CHECK constraint wants a justification whenever the outcome is a test edit.
        await add_gold_run(
            db_session, repo, "gold-2", outcome=TaskOutcome.PASSED_WITH_TEST_EDIT,
            test_edit_justification="the reference fix edits a test",
        )

        verdict = await verdict_for(db_session_factory)

        assert any("passed_with_test_edit" in r for r in verdict.reasons)

    async def test_a_missing_run_is_rejected(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1")

        verdict = await verdict_for(db_session_factory)

        assert any("gold-2" in r and "no task row" in r for r in verdict.reasons)

    async def test_a_missing_baseline_is_rejected(self, db_session, db_session_factory):
        await self.seed(db_session, baseline=None)

        verdict = await verdict_for(db_session_factory)

        assert any("gold-2" in r and "no baseline" in r for r in verdict.reasons)

    async def test_an_unscoreable_baseline_is_rejected_with_its_error(self, db_session, db_session_factory):
        await self.seed(db_session, baseline={**GOOD_BASELINE, "error": "verify: suite exceeded its 1800s deadline"})

        verdict = await verdict_for(db_session_factory)

        assert any("unscoreable" in r and "1800s deadline" in r for r in verdict.reasons)

    async def test_a_baseline_that_differs_between_the_runs_is_flaky(self, db_session, db_session_factory):
        flaky = {**GOOD_BASELINE, "passed": [P2P], "failed": [F2P, "other.py::test_x"]}
        await self.seed(db_session, baseline=flaky)

        verdict = await verdict_for(db_session_factory)

        flaky_reasons = [r for r in verdict.reasons if "flaky" in r]
        assert flaky_reasons and "other.py::test_x" in flaky_reasons[0]
        assert "passed differs" in flaky_reasons[0] or any("failed differs" in r for r in flaky_reasons)

    async def test_identical_baselines_are_not_flaky_even_if_listed_in_another_order(self, db_session, db_session_factory):
        reordered = {**GOOD_BASELINE, "passed": ["other.py::test_x", P2P]}
        await self.seed(db_session, baseline=reordered)

        verdict = await verdict_for(db_session_factory)

        assert not any("flaky" in r for r in verdict.reasons)

    async def test_a_curated_test_that_already_passes_at_baseline_is_rejected(self, db_session, db_session_factory):
        green = {**GOOD_BASELINE, "failed": [], "passed": [F2P, P2P, "other.py::test_x"]}
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1", baseline=green)
        await add_gold_run(db_session, repo, "gold-2", baseline=green)

        verdict = await verdict_for(db_session_factory)

        reasons = [r for r in verdict.reasons if "not red" in r]
        assert len(reasons) == 1 and F2P in reasons[0] and "changes nothing" in reasons[0]

    async def test_an_ordinary_skip_is_not_red(self, db_session, db_session_factory):
        skipped = {**GOOD_BASELINE, "failed": [], "skipped": [F2P]}
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1", baseline=skipped)
        await add_gold_run(db_session, repo, "gold-2", baseline=skipped)

        verdict = await verdict_for(db_session_factory)

        assert any("not red" in r and F2P in r for r in verdict.reasons)

    async def test_an_expected_failure_counts_as_red(self, db_session, db_session_factory):
        xfail = {**GOOD_BASELINE, "failed": [], "xfailed": [F2P]}
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1", baseline=xfail)
        await add_gold_run(db_session, repo, "gold-2", baseline=xfail)

        verdict = await verdict_for(db_session_factory)

        assert verdict.accepted

    async def test_an_id_the_baseline_never_collected_is_reported_as_a_format_mismatch(self, db_session, db_session_factory):
        wrong = {**GOOD_BASELINE, "failed": []}  # F2P is in no bucket and no module failed to import
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1", baseline=wrong)
        await add_gold_run(db_session, repo, "gold-2", baseline=wrong)

        verdict = await verdict_for(db_session_factory)

        assert any("not collected at baseline" in r and "format mismatch" in r and F2P in r for r in verdict.reasons)
        assert not any("not red" in r for r in verdict.reasons), "an uncollected id must not be reported twice"

    async def test_an_id_in_a_module_that_failed_to_import_is_red_not_uncollected(self, db_session, db_session_factory):
        broken = {**GOOD_BASELINE, "failed": [], "collect_failures": ["t.py"]}
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1", baseline=broken)
        await add_gold_run(db_session, repo, "gold-2", baseline=broken)

        verdict = await verdict_for(db_session_factory)

        assert verdict.accepted

    async def test_a_sibling_module_prefix_does_not_excuse_an_uncollected_id(self, db_session, db_session_factory):
        # `t.py` failing to import must not cover `t.py_other::...` or `tt.py::...`
        spec = instance()
        sibling = {**GOOD_BASELINE, "failed": [], "collect_failures": ["tt.py"]}
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1", baseline=sibling)
        await add_gold_run(db_session, repo, "gold-2", baseline=sibling)

        verdict = await verdict_for(db_session_factory, spec)

        assert any("not collected" in r for r in verdict.reasons)

    async def test_every_reason_is_reported_not_just_the_first(self, db_session, db_session_factory):
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1", outcome=TaskOutcome.FAILED, baseline={**GOOD_BASELINE, "failed": []})
        await add_gold_run(db_session, repo, "gold-2", outcome=TaskOutcome.FAILED, baseline={**GOOD_BASELINE, "failed": []})

        verdict = await verdict_for(db_session_factory)

        assert len(verdict.reasons) >= 3


@pytest.mark.anyio
@pytest.mark.db
class TestTimingAndProposal:
    async def seed(self, session, baseline_seconds, attempt_seconds=10.0):
        repo = await add_repo(session, "repolace/bench-a")
        for run in RUNS:
            await add_gold_run(
                session, repo, run,
                baseline={**GOOD_BASELINE, "duration_seconds": baseline_seconds},
                attempt={**GOOD_ATTEMPT, "duration_seconds": attempt_seconds},
            )

    async def test_a_slow_suite_gets_a_proposal_naming_the_fix_files_and_the_coverage_it_keeps(self, db_session, db_session_factory):
        await self.seed(db_session, 1000.0)  # default timeout 1800: 1000 >= 0.5 * 1800

        verdict = await verdict_for(db_session_factory)

        assert verdict.proposal is not None
        assert verdict.proposal.test_targets == ("t.py",)
        assert (verdict.proposal.covered_pass_to_pass, verdict.proposal.total_pass_to_pass) == (1, 2)
        assert verdict.accepted, "a proposal is advice; it does not reject the instance"

    async def test_a_fast_suite_gets_no_proposal(self, db_session, db_session_factory):
        await self.seed(db_session, 100.0)

        assert (await verdict_for(db_session_factory)).proposal is None

    async def test_the_instances_own_timeout_is_the_yardstick(self, db_session, db_session_factory):
        await self.seed(db_session, 60.0)
        spec = instance(spec={"base_image": "python:3.9-slim", "timeout_seconds": 100})

        assert suite_timeout_seconds(spec) == 100.0
        assert (await verdict_for(db_session_factory, spec)).proposal is not None

    async def test_an_instance_already_targeted_is_not_proposed_again(self, db_session, db_session_factory):
        await self.seed(db_session, 1500.0)
        spec = instance(targeted_p2p=True, spec={"base_image": "python:3.9-slim", "test_targets": ["t.py"]})

        assert (await verdict_for(db_session_factory, spec)).proposal is None

    async def test_the_slowest_run_decides(self, db_session, db_session_factory):
        await self.seed(db_session, 50.0, attempt_seconds=1200.0)

        assert (await verdict_for(db_session_factory)).proposal is not None

    def test_the_threshold_is_half_the_timeout(self):
        assert TARGETED_P2P_FRACTION == 0.5


class TestRunnerWallClockProposal:
    """The proposal also weighs the runner's per-task wall clock, not only the suite timeout.

    A task the runner kills is a harness error and leaves `passed / admissible`; a suite that
    is comfortably inside its own timeout can still make the whole task outlast the runner.
    Pure: built from `GoldRun`s directly, no database.
    """

    @staticmethod
    def runs(seconds: float) -> dict[str, GoldRun]:
        baseline = SuiteResult(failed=(F2P,), passed=(P2P, "other.py::test_x"), collected_files=("t.py", "other.py"), duration_seconds=seconds)
        attempt = SuiteResult(passed=(F2P, P2P, "other.py::test_x"), collected_files=("t.py", "other.py"), duration_seconds=seconds)
        return {run: GoldRun(run, TaskStatus.COMPLETED, TaskOutcome.PASSED, None, None, baseline, (attempt,)) for run in RUNS}

    def test_a_suite_inside_its_own_timeout_that_would_outlast_the_runner_is_proposed(self):
        # 600s is a third of a 1800s timeout, so the timeout rule alone says nothing; padded to
        # 1200s per run, the task needs task_wall_clock_bound(1200) seconds.
        runner = task_wall_clock_bound(1200.0) - 1.0

        verdict = validate_instance(instance(), self.runs(600.0), timeout_seconds=1800.0, runner_timeout_seconds=runner)

        assert verdict.proposal is not None
        assert "runner" in verdict.proposal.reason
        assert "of the 1800s timeout" not in verdict.proposal.reason, "the suite-timeout rule must not be what fired"
        assert verdict.accepted, "a proposal is advice; it does not reject the instance"

    def test_the_same_suite_under_a_runner_limit_that_covers_it_gets_none(self):
        runner = task_wall_clock_bound(1200.0)

        verdict = validate_instance(instance(), self.runs(600.0), timeout_seconds=1800.0, runner_timeout_seconds=runner)

        assert verdict.proposal is None

    def test_the_padding_stops_at_the_suites_own_timeout(self):
        # A long timeout padded past itself would overstate the task; the sandbox stops the run there.
        runner = task_wall_clock_bound(1000.0)

        verdict = validate_instance(instance(), self.runs(990.0), timeout_seconds=1000.0, runner_timeout_seconds=runner)

        assert verdict.proposal is not None and "runner" not in verdict.proposal.reason

    def test_an_instance_with_a_longer_suite_timeout_is_flagged_under_the_default_runner_limit(self):
        spec = instance(spec={"base_image": "python:3.9-slim", "timeout_seconds": 4000})

        verdict = validate_instance(spec, self.runs(1900.0), timeout_seconds=suite_timeout_seconds(spec))

        assert verdict.proposal is not None and "runner" in verdict.proposal.reason

    def test_the_default_runner_limit_is_the_runners(self):
        assert build_parser().parse_args([]).runner_timeout_seconds == DEFAULT_WALL_CLOCK_SECONDS

    @pytest.mark.parametrize("seconds", ["0", "-1", "inf", "nan"])
    def test_a_nonsense_runner_limit_is_a_usage_error(self, seconds, capsys):
        assert main(["--runner-timeout-seconds", seconds]) == 2
        assert "--runner-timeout-seconds" in capsys.readouterr().err


class TestRendering:
    def verdicts(self):
        from harness.gold import InstanceVerdict, Proposal

        return [
            InstanceVerdict("good", (), (40.0,), (45.0,), None),
            InstanceVerdict("bad", ("gold-1: outcome is failed | `x` <script>alert(1)</script>\nsecond line",), (), (), None),
            InstanceVerdict("slow", (), (1000.0,), (1100.0,), Proposal(("t.py",), 1, 2, "longest suite run 1100s is at least 50%")),
        ]

    def test_reasons_cannot_break_out_of_their_table_cell(self):
        text = render_validation(self.verdicts(), RUNS)

        assert "<script>" not in text and "&lt;script&gt;" in text
        row = next(line for line in text.splitlines() if line.startswith("| `bad`"))
        assert row.count("|") - row.count("\\|") == 3  # leading, middle, trailing
        assert "`x`" not in row and "\n" not in row

    def test_the_report_says_it_never_edits_instances_and_lists_each_section(self):
        text = render_validation(self.verdicts(), RUNS)

        assert "never edits an instance file" in text
        assert "## Rejected" in text and "## Accepted" in text and "## targeted_p2p proposals" in text
        assert "3 instance(s): 2 accepted, 1 rejected" in text
        assert "### `slow`" in text and "would still cover 1 of 2" in text

    def test_rendering_is_deterministic(self):
        assert render_validation(self.verdicts(), RUNS) == render_validation(self.verdicts(), RUNS)


@pytest.mark.anyio
@pytest.mark.db
class TestMain:
    def seam(self, factory):
        @asynccontextmanager
        async def go():
            yield factory

        return go

    def digests(self, directory: Path) -> dict[str, str]:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.glob("*.json")}

    async def test_writes_validation_md_and_never_touches_instance_files(self, db_session, db_session_factory, tmp_path, capsys):
        directory = tmp_path / "instances"
        directory.mkdir()
        write_instances(directory, instance())
        before = self.digests(directory)
        repo = await add_repo(db_session, "repolace/bench-a")
        for run in RUNS:
            await add_gold_run(db_session, repo, run)

        code = await asyncio.to_thread(main, ["--instances-dir", str(directory)], session_factory=self.seam(db_session_factory))

        assert code == 0
        assert "1 accepted, 0 rejected" in capsys.readouterr().out
        text = (directory / "VALIDATION.md").read_text()
        assert "`a`" in text and "Written by `repolace-eval gold`" in text
        assert self.digests(directory) == before

    async def test_exit_one_when_an_instance_is_rejected(self, db_session, db_session_factory, tmp_path, capsys):
        directory = tmp_path / "instances"
        directory.mkdir()
        write_instances(directory, instance())
        repo = await add_repo(db_session, "repolace/bench-a")
        await add_gold_run(db_session, repo, "gold-1")  # gold-2 is missing

        code = await asyncio.to_thread(main, ["--instances-dir", str(directory)], session_factory=self.seam(db_session_factory))

        assert code == 1
        assert "rejected: a" in capsys.readouterr().err
        assert "no task row" in (directory / "VALIDATION.md").read_text()

    async def test_a_custom_output_path_is_honoured(self, db_session, db_session_factory, tmp_path):
        directory = tmp_path / "instances"
        directory.mkdir()
        write_instances(directory, instance())
        out = tmp_path / "elsewhere" / "report.md"

        await asyncio.to_thread(
            main, ["--instances-dir", str(directory), "--output", str(out)], session_factory=self.seam(db_session_factory)
        )

        assert out.exists() and not (directory / "VALIDATION.md").exists()


class TestUsage:
    def forbidden(self):
        @asynccontextmanager
        async def go():
            raise AssertionError("the database must not be opened for a usage error")
            yield  # pragma: no cover

        return go

    @pytest.mark.parametrize("runs", ["gold-1", "gold-1,gold-1", "gold-1,gold-2,gold-3", "gold-1,../x", ""])
    def test_exactly_two_distinct_valid_run_ids_are_required(self, tmp_path, runs, capsys):
        directory = tmp_path / "instances"
        directory.mkdir()
        write_instances(directory, instance())

        code = main(["--instances-dir", str(directory), "--runs", runs], session_factory=self.forbidden())

        assert code == 2

    def test_an_empty_instances_directory_is_refused(self, tmp_path, capsys):
        directory = tmp_path / "instances"
        directory.mkdir()

        assert main(["--instances-dir", str(directory)], session_factory=self.forbidden()) == 2
        assert "no instances" in capsys.readouterr().err
