"""`RunResult`'s shape.

The new fields went in at the end, with defaults, because `run.py` builds this
positionally in places (`RunResult(task.id, TaskStatus.PR_OPENED, number, url,
...)`) and a field inserted in the middle would silently shift every one of those
arguments into the wrong slot -- with no error, since the types often agree.
"""

import dataclasses
import uuid
from decimal import Decimal

import pytest

from repolace_pipeline.run import RunResult
from repolace_shared.db.models import TaskOutcome, TaskStatus


def test_the_original_fields_keep_their_positions():
    names = [f.name for f in dataclasses.fields(RunResult)]

    assert names[:6] == ["task_id", "status", "pr_number", "pr_url", "error_message", "outcome"]
    assert names[6] == "score_reason"


def test_the_new_fields_come_last():
    names = [f.name for f in dataclasses.fields(RunResult)]

    assert names[7:] == ["cost_usd", "attempts", "submitted", "stop_reason", "pr_gate_reason"]


def test_positional_construction_still_means_what_it_did():
    task_id = uuid.uuid4()

    result = RunResult(task_id, TaskStatus.PR_OPENED, 12, "https://example.test/pull/12")

    assert (result.task_id, result.status, result.pr_number, result.pr_url) == (
        task_id, TaskStatus.PR_OPENED, 12, "https://example.test/pull/12",
    )
    assert result.error_message is None and result.outcome is None


def test_the_new_fields_default_to_nothing_measured():
    result = RunResult(uuid.uuid4(), TaskStatus.FAILED)

    assert result.cost_usd is None
    assert result.attempts == 0
    assert result.submitted is None, "no agent ran, which is not the same as an agent that did not submit"
    assert result.stop_reason is None
    assert result.pr_gate_reason is None


def test_a_finished_result_carries_them():
    result = RunResult(
        uuid.uuid4(),
        TaskStatus.COMPLETED,
        outcome=TaskOutcome.FAILED,
        score_reason="no baseline-failing test now passes",
        cost_usd=Decimal("0.4213"),
        attempts=2,
        submitted=False,
        stop_reason="step_cap",
        pr_gate_reason="the attempt regressed 3 tests",
    )

    assert result.cost_usd == Decimal("0.4213")
    assert (result.attempts, result.submitted, result.stop_reason) == (2, False, "step_cap")
    assert result.pr_gate_reason == "the attempt regressed 3 tests"


def test_it_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RunResult(uuid.uuid4(), TaskStatus.FAILED).attempts = 3  # type: ignore[misc]
