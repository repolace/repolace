"""Projecting a SuiteResult onto its row.

Pure, and worth pinning: the columns exist so the benchmark can be *rescored*
without re-running anything, and a field dropped here is unrecoverable in
exactly the way migrations 0009, 0010 and 0011 each landed to prevent.
"""

import uuid
from dataclasses import fields

from verify.protocol import SuiteResult

from repolace_pipeline.testruns import to_row


def build(**overrides) -> SuiteResult:
    return SuiteResult(**overrides)


class TestProjection:
    def test_every_set_survives(self):
        result = build(
            passed=("t::a",),
            failed=("t::b",),
            skipped=("t::c",),
            xfailed=("t::d",),
            did_not_run=("t::e",),
            collect_failures=("t/f.py",),
        )
        row = to_row(uuid.uuid4(), 0, "abc123", result)

        assert row.passed == ["t::a"]
        assert row.failed == ["t::b"]
        assert row.skipped == ["t::c"]
        assert row.xfailed == ["t::d"]
        assert row.did_not_run == ["t::e"]
        assert row.collect_failures == ["t/f.py"]

    def test_the_scoring_inputs_survive(self):
        """`collected_files` feeds `disqualifying_paths` and `fingerprint` feeds
        `fingerprint_changed`; a run stored without them cannot be rescored."""
        result = build(
            collected_files=("tests/test_a.py",),
            conftests=("/repo/tests/conftest.py",),
            fingerprint={"rootdir": "/repo", "ini": {"addopts": ""}},
        )
        row = to_row(uuid.uuid4(), 1, "def456", result)

        assert row.collected_files == ["tests/test_a.py"]
        assert row.conftests == ["/repo/tests/conftest.py"]
        assert row.fingerprint == {"rootdir": "/repo", "ini": {"addopts": ""}}
        assert isinstance(row.fingerprint, dict)

    def test_an_unscoreable_run_keeps_its_reason_and_its_output(self):
        result = build(error="verify: suite exceeded its 1800s deadline", stdout_tail="collecting")
        row = to_row(uuid.uuid4(), 1, "def456", result)

        assert row.error == "verify: suite exceeded its 1800s deadline"
        assert row.stdout_tail == "collecting"

    def test_the_attempt_and_commit_are_recorded(self):
        """Without the sha a run cannot be tied back to the diff that produced
        it once the clone is gone."""
        task_id = uuid.uuid4()
        row = to_row(task_id, 3, "cafe123", build())

        assert (row.task_id, row.attempt, row.commit_sha) == (task_id, 3, "cafe123")

    def test_no_field_of_a_suite_result_is_silently_dropped(self):
        """A guard on the next field added to SuiteResult: if it is not stored
        and not deliberately excluded, this fails rather than losing it quietly."""
        stored = {
            "passed", "failed", "skipped", "xfailed", "did_not_run", "collect_failures",
            "collected_files", "conftests", "fingerprint", "exit_code", "duration_seconds",
            "stdout_tail", "error",
        }
        assert {f.name for f in fields(SuiteResult)} == stored
