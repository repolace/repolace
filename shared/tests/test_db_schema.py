"""Database-backed tests for the scoring schema.

The first tests in this project to touch a database. They exist because the one
defect that reached a real run was a MissingGreenlet in a database path with no
coverage, while 148 passing tests avoided the database entirely.

What is worth pinning down here is not that SQLAlchemy works -- it is that the
constraints protecting the benchmark's integrity actually reject what they are
supposed to reject. Those constraints are the only machine-checkable part of
the success criteria.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from repolace_shared.db.models import BASELINE_ATTEMPT, Task, TaskOutcome, TaskStatus, TaskTestRun

from db_support import seed_task

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class TestTaskTestRun:
    async def test_a_baseline_run_round_trips(self, db_session):
        task = await seed_task(db_session)

        db_session.add(
            TaskTestRun(
                task_id=task.id,
                attempt=BASELINE_ATTEMPT,
                commit_sha="abc123",
                passed=["tests/test_a.py::test_ok"],
                failed=["tests/test_a.py::test_broken"],
                exit_code=1,
                duration_seconds=12.5,
            )
        )
        await db_session.commit()

        run = (await db_session.execute(select(TaskTestRun))).scalar_one()
        assert run.attempt == 0
        assert run.passed == ["tests/test_a.py::test_ok"]
        assert run.failed == ["tests/test_a.py::test_broken"]

    async def test_duration_comes_back_as_a_decimal_not_a_float(self, db_session):
        """The column is Numeric(10,3); the annotation says float. The column wins."""
        task = await seed_task(db_session)
        db_session.add(
            TaskTestRun(task_id=task.id, attempt=0, commit_sha="a", duration_seconds=1.25)
        )
        await db_session.commit()

        run = (await db_session.execute(select(TaskTestRun))).scalar_one()
        assert isinstance(run.duration_seconds, Decimal)
        assert float(run.duration_seconds) == 1.25

    async def test_empty_result_sets_are_allowed(self, db_session):
        """A suite that collected nothing is a real state, distinct from NULL."""
        task = await seed_task(db_session)
        db_session.add(
            TaskTestRun(task_id=task.id, attempt=0, commit_sha="a", passed=[], failed=[],
                        error="collection failed")
        )
        await db_session.commit()

        run = (await db_session.execute(select(TaskTestRun))).scalar_one()
        assert run.passed == [] and run.error == "collection failed"

    async def test_a_none_result_set_is_silently_coerced_to_empty(self, db_session):
        """Documents a trap rather than a protection.

        The columns are NOT NULL with a server default of `{}`, so it is
        tempting to assume `passed=None` raises. It does not: SQLAlchemy omits
        an explicit None for a column that has a default, and the server fills
        in `{}`. The row stores an empty list.

        That is worse than an error, because for the scoring rule an empty
        `passed` set is indistinguishable from "the suite ran and nothing
        passed" -- which would read as a total regression. Callers must pass
        real lists; the database will not catch them.
        """
        task = await seed_task(db_session)
        db_session.add(TaskTestRun(task_id=task.id, attempt=0, commit_sha="a", passed=None))
        await db_session.commit()

        run = (await db_session.execute(select(TaskTestRun))).scalar_one()
        assert run.passed == [], "None became an empty set, not an error"

    async def test_one_row_per_attempt(self, db_session):
        """A retried attempt number is an error, not an upsert -- one row per attempt, always."""
        task = await seed_task(db_session)
        db_session.add(TaskTestRun(task_id=task.id, attempt=1, commit_sha="a"))
        await db_session.commit()

        db_session.add(TaskTestRun(task_id=task.id, attempt=1, commit_sha="b"))
        with pytest.raises(IntegrityError):
            await db_session.commit()

    async def test_a_negative_attempt_is_rejected(self, db_session):
        task = await seed_task(db_session)
        db_session.add(TaskTestRun(task_id=task.id, attempt=-1, commit_sha="a"))

        with pytest.raises(IntegrityError):
            await db_session.commit()

    async def test_deleting_a_task_takes_its_runs_with_it(self, db_session):
        task = await seed_task(db_session)
        db_session.add(TaskTestRun(task_id=task.id, attempt=0, commit_sha="a"))
        await db_session.commit()

        await db_session.delete(task)
        await db_session.commit()

        assert (await db_session.execute(select(TaskTestRun))).scalars().all() == []


class TestTestEditConstraints:
    """The one part of the success criteria that is not machine-checkable is at
    least made impossible to record silently."""

    async def test_a_test_edit_pass_without_a_justification_is_rejected(self, db_session):
        task = await seed_task(db_session)
        task.outcome = TaskOutcome.PASSED_WITH_TEST_EDIT

        with pytest.raises(IntegrityError):
            await db_session.commit()

    async def test_a_test_edit_pass_with_a_justification_is_accepted(self, db_session):
        task = await seed_task(db_session)
        task.outcome = TaskOutcome.PASSED_WITH_TEST_EDIT
        task.test_edit_justification = "the test asserted the bug it was reporting"
        await db_session.commit()

        stored = (await db_session.execute(select(Task))).scalar_one()
        assert stored.outcome is TaskOutcome.PASSED_WITH_TEST_EDIT

    async def test_half_an_approval_is_rejected(self, db_session):
        task = await seed_task(db_session)
        task.outcome = TaskOutcome.PASSED_WITH_TEST_EDIT
        task.test_edit_justification = "because"
        task.test_edit_approved_by = "rounak"  # no timestamp

        with pytest.raises(IntegrityError):
            await db_session.commit()

    async def test_sign_off_on_an_ordinary_pass_is_rejected(self, db_session):
        """Approval recorded against a plain pass means it landed on the wrong task."""
        task = await seed_task(db_session)
        task.outcome = TaskOutcome.PASSED
        task.test_edit_approved_by = "rounak"
        task.test_edit_approved_at = __import__("datetime").datetime.now(
            __import__("datetime").UTC
        )

        with pytest.raises(IntegrityError):
            await db_session.commit()


class TestTaskDefaults:
    async def test_a_new_task_is_queued_and_unscored(self, db_session):
        """Unscored is not failed. A task that has not run has no outcome yet."""
        task = await seed_task(db_session)

        assert task.status.value == "queued"
        assert task.outcome is None
        assert task.retry_count == 0
        assert task.cost_usd is None

    async def test_the_status_enum_has_no_merged_state(self, db_session):
        """`merged` was replaced by `pr_opened`: repolace's work ends when the PR exists."""
        task = await seed_task(db_session)
        task.status = "merged"

        with pytest.raises((IntegrityError, DBAPIError, LookupError, ValueError)):
            await db_session.commit()


class TestOpenPrOnFailure:
    async def test_it_defaults_to_false(self, db_session):
        """Proves migration 0008's server_default, not just the Python default.

        The insert omits the column entirely, so a missing server_default would
        surface here as a NOT NULL violation rather than as False.
        """
        task = await seed_task(db_session)

        stored = (await db_session.execute(select(Task))).scalar_one()
        assert stored.open_pr_on_failure is False

    async def test_it_can_be_set(self, db_session):
        task = await seed_task(db_session, open_pr_on_failure=True)

        stored = (await db_session.execute(select(Task))).scalar_one()
        assert stored.open_pr_on_failure is True


class TestCompletedStatus:
    async def test_a_task_can_finish_without_a_pr(self, db_session):
        """The pipeline ran to the end and the agent lost. Not a pipeline failure.

        `failed` stays reserved for "repolace itself broke", which is what keeps
        `error_message` meaning exactly one thing.
        """
        task = await seed_task(db_session)
        task.status = TaskStatus.COMPLETED
        task.outcome = TaskOutcome.FAILED
        await db_session.commit()

        stored = (await db_session.execute(select(Task))).scalar_one()
        assert stored.status is TaskStatus.COMPLETED
        assert stored.outcome is TaskOutcome.FAILED
        assert stored.error_message is None


class TestRunDetailSets:
    async def test_the_extra_sets_round_trip(self, db_session):
        """Kept because scoring asks about them: a pass turned into a skip is a
        regression, and a baseline failure silenced rather than fixed is a
        disqualification. Neither is derivable if the set was discarded."""
        task = await seed_task(db_session)
        db_session.add(
            TaskTestRun(
                task_id=task.id,
                attempt=0,
                commit_sha="abc",
                passed=["t::a"],
                failed=["t::b"],
                skipped=["t::c"],
                xfailed=["t::x"],
                did_not_run=["t::d"],
                collect_failures=["tests/broken.py"],
            )
        )
        await db_session.commit()

        run = (await db_session.execute(select(TaskTestRun))).scalar_one()
        assert run.skipped == ["t::c"]
        # Separate from `skipped` because pytest reports both the same way and
        # the scoring rule treats them oppositely -- see migration 0010.
        assert run.xfailed == ["t::x"]
        assert run.did_not_run == ["t::d"]
        assert run.collect_failures == ["tests/broken.py"]

    async def test_they_default_to_empty_not_null(self, db_session):
        """Proving migration 0009's server_default, not the Python-side default."""
        task = await seed_task(db_session)
        db_session.add(TaskTestRun(task_id=task.id, attempt=0, commit_sha="abc"))
        await db_session.commit()

        run = (await db_session.execute(select(TaskTestRun))).scalar_one()
        assert run.skipped == [] and run.did_not_run == [] and run.collect_failures == []
        assert run.xfailed == []


class TestPatchProvenance:
    async def test_a_patch_can_be_recorded_for_later_audit(self, db_session):
        """The clone is deleted when the task ends; without this a PASSED
        verdict cannot be re-examined."""
        task = await seed_task(db_session)
        task.patch_sha = "deadbeef"
        task.changed_files = ["src/config.py"]
        await db_session.commit()

        stored = (await db_session.execute(select(Task))).scalar_one()
        assert stored.patch_sha == "deadbeef"
        assert stored.changed_files == ["src/config.py"]

    async def test_null_means_never_got_that_far(self, db_session):
        """Distinct from a patch that changed no files, which is an empty list."""
        task = await seed_task(db_session)

        assert task.patch_sha is None and task.changed_files is None
