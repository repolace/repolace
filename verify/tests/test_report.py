"""Unit tests for the JSONL parser.

These are where the benchmark's correctness actually lives. Three of the cases
below correspond to real pytest behaviours that a naive parser gets wrong in a
way that produces plausible-looking numbers rather than an error:

* a fixture error emits no `call` report at all;
* a teardown failure attaches to a node whose `call` phase passed;
* a collection failure makes tests vanish rather than fail.

The `error` assertions matter as much as the verdicts: `error` means the run is
unscoreable, and conflating that with "everything failed" lets infrastructure
flakiness quietly deflate the headline figure.
"""

from pathlib import Path

import pytest

from repolace_shared.process import ProcessResult
from verify.report import MAX_TEST_IDS, parse_report

from verify_support import collect_record, jsonl, report, session_record, start_record


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "report.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


def run(returncode: int = 0, **kwargs) -> ProcessResult:
    fields = {"returncode": returncode, "stdout": b"", "stderr": b""}
    return ProcessResult(**{**fields, **kwargs})


def parse(tmp_path: Path, text: str, returncode: int = 0, **process_kwargs):
    return parse_report(write(tmp_path, text), run(returncode, **process_kwargs), elapsed=1.0)


class TestVerdicts:
    def test_a_plain_pass(self, tmp_path):
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::ok", "setup", "passed"),
            report("t.py::ok", "call", "passed"),
            report("t.py::ok", "teardown", "passed"),
            session_record(0),
        ))

        assert result.passed == ("t.py::ok",)
        assert result.error is None

    def test_a_fixture_error_counts_as_failed(self, tmp_path):
        """Emits setup+teardown and NO call report. A call-keyed parser loses it entirely."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::broken", "setup", "failed", crash="RuntimeError: fixture died"),
            report("t.py::broken", "teardown", "passed"),
            session_record(1),
        ), returncode=1)

        assert result.failed == ("t.py::broken",)
        assert result.passed == ()

    def test_a_teardown_failure_after_a_passing_call_counts_as_failed(self, tmp_path):
        """pytest reports this as ERROR. Reading the call report would call it passed."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::leaky", "call", "passed"),
            report("t.py::leaky", "teardown", "failed", crash="RuntimeError: socket open"),
            session_record(1),
        ), returncode=1)

        assert result.failed == ("t.py::leaky",)
        assert result.passed == ()

    def test_xfail_is_its_own_bucket_not_a_skip(self, tmp_path):
        """pytest reports xfail as skipped carrying wasxfail, so the two are
        indistinguishable by outcome alone -- and the scoring rule treats them
        oppositely, an xfail being red at baseline and a skip not."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::known_bug", "call", "skipped", xfail=True),
            session_record(0),
        ))

        assert result.xfailed == ("t.py::known_bug",)
        assert result.skipped == ()
        assert result.passed == () and result.failed == ()

    def test_a_non_strict_xpass_is_a_pass_not_an_xfail(self, tmp_path):
        """pytest sets wasxfail on a *passed* record for a non-strict xpass, so
        `xfail` is not exclusive to skips. A rule reading the flag off the node
        rather than off the skipping record would file a genuine pass as
        silenced -- and `neutralized` counts xfailed as silenced."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::fixed_bug", "call", "passed", xfail=True),
            session_record(0),
        ))

        assert result.passed == ("t.py::fixed_bug",)
        assert result.xfailed == () and result.skipped == ()

    def test_setup_and_teardown_phases_do_not_dilute_the_xfail_flag(self, tmp_path):
        """wasxfail rides on the call report only, so the sibling phases carry
        False. An `all()` over the node's records would never fire."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::known_bug", "setup", "passed", xfail=False),
            report("t.py::known_bug", "call", "skipped", xfail=True),
            report("t.py::known_bug", "teardown", "passed", xfail=False),
            session_record(0),
        ))

        assert result.xfailed == ("t.py::known_bug",)
        assert result.skipped == ()

    def test_a_skip_mark_on_an_xfail_test_is_an_ordinary_skip(self, tmp_path):
        """The skip short-circuits before xfail evaluation, so no wasxfail is
        set. Correct to file as an ordinary skip: an agent that adds a skip mark
        must not thereby move a test into the fail-to-pass candidate set."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::known_bug", "setup", "skipped", xfail=False),
            session_record(0),
        ))

        assert result.skipped == ("t.py::known_bug",)
        assert result.xfailed == ()

    def test_strict_xpass_is_failed(self, tmp_path):
        """Reported as failed with longrepr a plain str, so it falls out of the failed rule."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::surprise", "call", "failed", longrepr_type="str"),
            session_record(1),
        ), returncode=1)

        assert result.failed == ("t.py::surprise",)

    def test_a_module_level_skip_is_skipped(self, tmp_path):
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::needs_pg", "setup", "skipped"),
            report("t.py::needs_pg", "teardown", "passed"),
            session_record(0),
        ))

        assert result.skipped == ("t.py::needs_pg",)

    def test_node_ids_survive_verbatim(self, tmp_path):
        """`::`, spaces and tabs all appear in real parametrised ids.

        The id is the join key for the entire scoring rule, so any parsing or
        normalisation here would corrupt every comparison.
        """
        awkward = "t.py::test_x[a::b]"
        spaced = "t.py::test_y[sp ace]"
        tabbed = "t.py::test_z[tab\\there]"
        result = parse(tmp_path, jsonl(
            start_record(),
            *[report(n, "call", "passed") for n in (awkward, spaced, tabbed)],
            session_record(0),
        ))

        assert set(result.passed) == {awkward, spaced, tabbed}


class TestUnscoreable:
    def test_a_timeout_is_unscoreable_not_failed(self, tmp_path):
        result = parse(tmp_path, jsonl(start_record()), returncode=-9, timed_out=True)

        assert result.error is not None and "deadline" in result.error
        assert result.failed == ()

    def test_an_oom_kill_is_unscoreable(self, tmp_path):
        result = parse(tmp_path, jsonl(start_record(), session_record(137)), returncode=137)

        assert result.error is not None and "137" in result.error

    def test_a_missing_report_names_the_usage_error_case(self, tmp_path):
        """Exit 4 runs before any hook, so no file exists. Without this the
        operator sees 'no report' and no explanation."""
        result = parse_report(tmp_path / "absent.jsonl", run(4), elapsed=1.0)

        assert result.error is not None
        assert "no test report" in result.error and "usage error" in result.error

    def test_an_empty_report_is_unscoreable(self, tmp_path):
        result = parse(tmp_path, "", returncode=4)

        assert result.error is not None

    def test_a_missing_session_record_is_unscoreable(self, tmp_path):
        """The one thing exit codes cannot express: a collection error under
        --continue-on-collection-errors exits 1, exactly like a test failure."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::ok", "call", "passed"),
        ), returncode=1)

        assert result.error is not None and "did not finish" in result.error

    def test_a_truncated_final_line_is_tolerated(self, tmp_path):
        """Per-line flushing leaves at most one partial trailing line."""
        text = jsonl(start_record(), report("t.py::ok", "call", "passed"), session_record(0))
        result = parse(tmp_path, text + '{"kind": "test", "nodei')

        assert result.error is None
        assert result.passed == ("t.py::ok",)

    def test_corruption_before_the_last_line_is_not_tolerated(self, tmp_path):
        text = (
            jsonl(start_record())
            + "{not json at all\n"
            + jsonl(report("t.py::ok", "call", "passed"), session_record(0))
        )
        result = parse(tmp_path, text)

        assert result.error is not None and "corrupt report" in result.error

    def test_no_tests_collected_is_unscoreable(self, tmp_path):
        """A suite we could not find is 'we never found out', not 'everything failed'."""
        result = parse(tmp_path, jsonl(start_record(), session_record(5)), returncode=5)

        assert result.error is not None and "NO_TESTS_COLLECTED" in result.error

    def test_max_warnings_is_still_scoreable(self, tmp_path):
        """Exit 6 fires after a complete session; every report was written."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::ok", "call", "passed"),
            session_record(6),
        ), returncode=6)

        assert result.error is None
        assert result.passed == ("t.py::ok",)

    def test_too_many_ids_errors_rather_than_truncating(self, tmp_path):
        """A clipped `passed` array corrupts pass-to-pass toward false regressions."""
        records = [start_record()]
        records += [report(f"t.py::t{i}", "call", "passed") for i in range(MAX_TEST_IDS + 1)]
        records.append(session_record(0))
        result = parse(tmp_path, jsonl(*records))

        assert result.error is not None and "exceeds" in result.error

    def test_an_unknown_schema_version_is_refused(self, tmp_path):
        result = parse(tmp_path, jsonl({"kind": "start", "v": 99}, {"kind": "session", "v": 99}))

        assert result.error is not None and "schema" in result.error


class TestCollectFailures:
    def test_a_collect_failure_with_tests_still_running_is_not_an_error(self, tmp_path):
        """One broken optional import must not make the whole task unscoreable.

        The missing tests show up later as pass-to-pass regressions, which is
        the right answer and needs no special handling.
        """
        result = parse(tmp_path, jsonl(
            start_record(),
            collect_record("tests/test_optional.py"),
            report("t.py::ok", "call", "passed"),
            session_record(1),
        ), returncode=1)

        assert result.error is None
        assert result.collect_failures == ("tests/test_optional.py",)
        assert result.passed == ("t.py::ok",)

    def test_a_collect_failure_with_no_tests_at_all_is_unscoreable(self, tmp_path):
        result = parse(tmp_path, jsonl(
            start_record(),
            collect_record("tests/test_everything.py"),
            session_record(1),
        ), returncode=1)

        assert result.error is not None and "collection failed" in result.error


class TestForgeryDetection:
    """A cheap consistency check, not a security control.

    The code under test shares an interpreter with the plugin that writes these
    records. A patch that monkeypatches the recorder can rewrite the exit status
    alongside the outcomes and defeat everything here. That is not a gap to be
    closed -- there is no in-sandbox measurement of untrusted code that is
    forgery-proof. What these catch is the accidental and the careless.
    """

    def test_a_report_claiming_no_failures_while_pytest_failed_is_refused(self, tmp_path):
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::a", "call", "passed"),
            session_record(1),
        ), returncode=1)

        assert result.error is not None and "no failure" in result.error

    def test_a_report_claiming_failures_while_pytest_passed_is_refused(self, tmp_path):
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::a", "call", "failed"),
            session_record(0),
        ))

        assert result.error is not None and "exited 0" in result.error

    def test_max_warnings_is_exempt_from_the_cross_check(self, tmp_path):
        """Exit 6 can accompany a passing or a failing session."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::a", "call", "passed"),
            session_record(6),
        ), returncode=6)

        assert result.error is None


class TestOversizedReport:
    def test_a_report_above_the_cap_is_refused_without_being_read(self, tmp_path, monkeypatch):
        """The container's memory limit does not constrain this process.

        Reading an arbitrarily large report would OOM the worker rather than the
        sandbox, so the size is checked before anything is parsed.
        """
        from verify import report as report_module

        monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", 64)
        result = parse(tmp_path, jsonl(
            start_record(),
            *[report(f"t.py::t{i}", "call", "passed") for i in range(50)],
            session_record(0),
        ))

        assert result.error is not None and "above the" in result.error
