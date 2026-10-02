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

import os
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


def parse(tmp_path: Path, text: str, returncode: int = 0, *, nonce: str | None = None, **process_kwargs):
    return parse_report(
        write(tmp_path, text), run(returncode, **process_kwargs), elapsed=1.0, nonce=nonce
    )


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
        result = parse(tmp_path, jsonl(
            {"kind": "start", "v": 99},
            # exitstatus supplied so shape validation passes and the *version*
            # gate is what refuses this, which is the thing under test.
            {"kind": "session", "v": 99, "exitstatus": 1},
        ))

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


class TestCollectFailureIds:
    def test_a_session_level_collect_report_is_not_a_node_id(self, tmp_path):
        """The plugin filters collect reports on `outcome`, not on nodeid, so a
        *failing* session-level report -- whose nodeid is the empty string --
        reaches the host. Left in, it inflates the failure count and prefixes
        nothing."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::ok", "call", "passed"),
            collect_record(""),
            session_record(1),
        ))

        assert result.collect_failures == ()

    def test_duplicate_collect_records_for_one_module_are_deduped(self, tmp_path):
        """The plugin emits one record per failing collector and does not
        dedupe, so a module failing at two levels appears twice."""
        result = parse(tmp_path, jsonl(
            start_record(),
            report("t.py::ok", "call", "passed"),
            collect_record("tests/broken.py"),
            collect_record("tests/broken.py"),
            session_record(1),
        ))

        assert result.collect_failures == ("tests/broken.py",)


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


class TestMalformedRecords:
    """The report is written by code sharing an interpreter with the repository
    under test, so its *shape* is attacker-controlled even though it is not a
    trust boundary in the usual sense. A conftest appending one line at
    collection time is enough.

    The point is not to prevent scoring manipulation -- `error` already scores
    FAILED. It is that the one function whose entire job is turning untrusted
    bytes into a trustworthy SuiteResult must honour its documented contract:
    set `error`, never raise. Honest plugin/pytest version skew produces the
    same shapes.
    """

    def good(self, *extra):
        return (start_record(), report("t.py::ok", "call", "passed"), *extra, session_record(0))

    def test_a_bare_json_scalar_line_is_refused(self, tmp_path):
        result = parse(tmp_path, jsonl(*self.good()) + "3\n")

        assert result.error is not None and "not a report record" in result.error

    def test_a_json_array_line_is_refused(self, tmp_path):
        result = parse(tmp_path, jsonl(*self.good()) + "[1, 2]\n")

        assert result.error is not None

    def test_a_json_null_line_is_refused(self, tmp_path):
        result = parse(tmp_path, jsonl(*self.good()) + "null\n")

        assert result.error is not None

    def test_a_record_without_a_kind_is_refused(self, tmp_path):
        result = parse(tmp_path, jsonl(*self.good({"hello": "world"})))

        assert result.error is not None and "not a report record" in result.error

    def test_a_test_record_without_a_nodeid_is_refused(self, tmp_path):
        result = parse(tmp_path, jsonl(*self.good({"kind": "test", "when": "call", "outcome": "passed"})))

        assert result.error is not None and "nodeid" in result.error

    def test_a_numeric_nodeid_is_refused(self, tmp_path):
        """Would corrupt the by_node join silently rather than crash loudly."""
        result = parse(tmp_path, jsonl(*self.good(
            {"kind": "test", "nodeid": 7, "when": "call", "outcome": "passed"}
        )))

        assert result.error is not None and "nodeid" in result.error

    def test_a_session_record_with_a_string_exitstatus_is_refused(self, tmp_path):
        """`"0"` compares unequal to 0, so the exit-status cross-check silently
        took the wrong branch instead of catching a forged report."""
        result = parse(tmp_path, jsonl(
            start_record(), report("t.py::ok", "call", "passed"),
            {"kind": "session", "v": 1, "exitstatus": "0"},
        ))

        assert result.error is not None and "exitstatus" in result.error

    def test_a_boolean_where_an_integer_belongs_is_refused(self, tmp_path):
        """bool subclasses int, so True would otherwise arrive as exitstatus 1."""
        result = parse(tmp_path, jsonl(
            start_record(), report("t.py::ok", "call", "passed"),
            {"kind": "session", "v": 1, "exitstatus": True},
        ))

        assert result.error is not None and "exitstatus" in result.error

    def test_a_non_boolean_xfail_is_refused(self, tmp_path):
        """`xfail` decides which bucket a skip lands in, and therefore PASSED
        from FAILED. A truthy `"no"` must not slip through as True."""
        result = parse(tmp_path, jsonl(*self.good(
            {"kind": "test", "nodeid": "t.py::x", "when": "call", "outcome": "skipped", "xfail": "no"}
        )))

        assert result.error is not None and "xfail" in result.error

    def test_a_start_record_without_a_version_is_refused(self, tmp_path):
        """The version gate used to allow None through explicitly, so a record
        with no `v` passed a check whose whole job is refusing wrong ones."""
        result = parse(tmp_path, jsonl(
            {"kind": "start"}, report("t.py::ok", "call", "passed"), session_record(0),
        ))

        assert result.error is not None and "v" in result.error

    def test_an_unknown_record_kind_is_ignored(self, tmp_path):
        """Forward compatibility, and the reason validation is per-kind rather
        than whole-file: the plugin must be able to add a record type without
        breaking a host that predates it."""
        result = parse(tmp_path, jsonl(*self.good({"kind": "future", "whatever": [1, 2]})))

        assert result.error is None
        assert result.passed == ("t.py::ok",)

    def test_a_files_record_with_non_string_members_does_not_crash_the_sort(self, tmp_path):
        """sorted() raises on mixed types. A stray member is not evidence the
        run is untrustworthy, so these are filtered rather than refused."""
        result = parse(tmp_path, jsonl(*self.good(
            {"kind": "files", "collected": ["a.py", 7], "conftests": ["conftest.py"]}
        )))

        assert result.error is None
        assert result.collected_files == ("a.py",)

    def test_an_unreadable_report_is_unscoreable(self, tmp_path, monkeypatch):
        """stat and open can race the sandbox still deleting the file."""
        path = write(tmp_path, jsonl(*self.good()))

        def boom(path):
            raise OSError("device disappeared")

        monkeypatch.setattr("verify.report._open_report", boom)
        result = parse_report(path, run(0), elapsed=1.0)

        assert result.error is not None and "could not be parsed" in result.error

    def test_an_unexpected_error_is_reported_as_unscoreable_not_raised(self, tmp_path, monkeypatch):
        """Without this the outer guard is untested code, and the contract in
        parse_report's docstring stays a claim rather than a property."""
        monkeypatch.setattr(
            "verify.report._read_records", lambda path: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        path = write(tmp_path, jsonl(*self.good()))

        result = parse_report(path, run(0), elapsed=1.0)

        assert result.error is not None
        assert "RuntimeError" in result.error and "boom" in result.error


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


NONCE = "9f2c0de1" * 4
OTHER_NONCE = "0a1b2c3d" * 4


def stamped(*records: dict, nonce: str | None = NONCE) -> str:
    """A report whose start and session records carry `nonce`, as the plugin writes them."""
    out = []
    for record in records:
        if nonce is not None and record["kind"] in ("start", "session"):
            record = {**record, "nonce": nonce}
        out.append(record)
    return jsonl(*out)


def good_report(**kw) -> str:
    return stamped(start_record(), report("t.py::ok", "call", "passed"), session_record(0), **kw)


class TestTheReportIsNeverFollowed:
    """The report path is in a directory the sandbox owns, so whatever is at it is
    whatever the code under test left there. A probe that points it at another run's
    report would be handed that run's results -- including the hidden tests'."""

    def baseline_report(self, tmp_path) -> Path:
        """Another run's valid report, with hidden-test ids in it, in a sibling directory."""
        path = tmp_path / "results-0" / "report.jsonl"
        path.parent.mkdir()
        path.write_text(
            stamped(
                start_record(),
                {"kind": "files", "collected": ["tests/test_hidden_oracle.py"], "conftests": []},
                report("tests/test_hidden_oracle.py::test_secret_fix", "call", "failed"),
                session_record(1),
                nonce=OTHER_NONCE,
            )
        )
        return path

    def results(self, tmp_path) -> Path:
        directory = tmp_path / "results-probe-1"
        directory.mkdir()
        return directory / "report.jsonl"

    def test_a_symlink_to_another_runs_report_is_refused(self, tmp_path):
        target = self.baseline_report(tmp_path)
        link = self.results(tmp_path)
        link.symlink_to(target)

        result = parse_report(link, run(1), elapsed=1.0)

        assert result.error is not None and "not a regular file" in result.error
        assert result.failed == () and result.passed == () and result.collected_files == ()

    def test_none_of_the_other_runs_ids_reach_the_result(self, tmp_path):
        target = self.baseline_report(tmp_path)
        link = self.results(tmp_path)
        link.symlink_to(target)

        result = parse_report(link, run(1), elapsed=1.0)

        assert "hidden_oracle" not in repr(result)

    def test_a_symlink_to_a_valid_report_is_refused_even_without_a_nonce(self, tmp_path):
        """The symlink rule stands on its own; the nonce is the second layer."""
        link = self.results(tmp_path)
        target = tmp_path / "other.jsonl"
        target.write_text(jsonl(start_record(), report("t.py::ok", "call", "passed"), session_record(0)))
        link.symlink_to(target)

        assert "not a regular file" in parse_report(link, run(0), elapsed=1.0).error

    def test_a_symlink_to_a_non_report_file_is_refused(self, tmp_path):
        secret = tmp_path / "host_secret.env"
        secret.write_text("DATABASE_URL=postgres://user:pw@host/db\n")
        link = self.results(tmp_path)
        link.symlink_to(secret)

        result = parse_report(link, run(0), elapsed=1.0)

        assert "not a regular file" in result.error and "pw@host" not in repr(result)

    def test_a_dangling_symlink_is_refused_not_reported_missing(self, tmp_path):
        link = self.results(tmp_path)
        link.symlink_to(tmp_path / "nowhere.jsonl")

        assert "not a regular file" in parse_report(link, run(0), elapsed=1.0).error

    def test_a_symlink_to_a_directory_is_refused(self, tmp_path):
        link = self.results(tmp_path)
        link.symlink_to(tmp_path)

        assert "not a regular file" in parse_report(link, run(0), elapsed=1.0).error

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
    def test_a_fifo_is_refused_and_does_not_block(self, tmp_path):
        """Opened for reading without O_NONBLOCK this would wait forever for a writer."""
        fifo = self.results(tmp_path)
        os.mkfifo(fifo)

        result = parse_report(fifo, run(0), elapsed=1.0)

        assert "not a regular file" in result.error

    def test_a_directory_at_the_report_path_is_refused(self, tmp_path):
        directory = self.results(tmp_path)
        directory.mkdir()

        assert "not a regular file" in parse_report(directory, run(0), elapsed=1.0).error

    def test_a_plain_report_is_still_read(self, tmp_path):
        """The contrast that makes the refusals meaningful."""
        path = self.results(tmp_path)
        path.write_text(good_report())

        assert parse_report(path, run(0), elapsed=1.0, nonce=NONCE).error is None

    @pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
    def test_the_descriptor_is_closed_on_every_path(self, tmp_path):
        """A leak per refused report would exhaust descriptors over a long benchmark.
        Counted rather than probed by number: a closed descriptor's number is reused
        by the very next open."""
        paths = []
        for name, text in {"good": good_report(), "empty": "", "bad": "{not json\n{also not\n"}.items():
            path = tmp_path / f"{name}.jsonl"
            path.write_text(text)
            paths.append(path)
        directory = tmp_path / "dir"
        directory.mkdir()
        link = tmp_path / "link.jsonl"
        link.symlink_to(paths[0])
        too_big = tmp_path / "big.jsonl"
        too_big.write_text(good_report())

        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            for path in (*paths, directory, link, tmp_path / "absent.jsonl"):
                parse_report(path, run(0), elapsed=1.0, nonce=NONCE)
        after = len(os.listdir("/proc/self/fd"))

        assert after == before

    @pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
    def test_the_descriptor_is_closed_when_the_size_cap_refuses(self, tmp_path, monkeypatch):
        from verify import report as report_module

        monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", 8)
        path = tmp_path / "big.jsonl"
        path.write_text(good_report())

        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            assert "above the" in parse_report(path, run(0), elapsed=1.0).error
        assert len(os.listdir("/proc/self/fd")) == before


class TestTheRunNonce:
    def test_the_right_nonce_is_accepted(self, tmp_path):
        result = parse(tmp_path, good_report(), nonce=NONCE)

        assert result.error is None and result.passed == ("t.py::ok",)

    def test_a_wrong_nonce_is_refused(self, tmp_path):
        result = parse(tmp_path, good_report(nonce=OTHER_NONCE), nonce=NONCE)

        assert result.error is not None and "not written by this run" in result.error
        assert result.passed == ()

    def test_a_missing_nonce_is_refused(self, tmp_path):
        result = parse(tmp_path, good_report(nonce=None), nonce=NONCE)

        assert "not written by this run" in result.error

    def test_a_report_with_no_start_record_is_refused(self, tmp_path):
        """A forged report cannot skip the record that proves whose it is."""
        text = stamped(report("t.py::ok", "call", "passed"), session_record(0))

        assert "not written by this run" in parse(tmp_path, text, nonce=NONCE).error

    def test_one_record_with_the_wrong_nonce_is_enough_to_refuse(self, tmp_path):
        """A report spliced from two runs: right start, foreign session."""
        text = jsonl(
            {**start_record(), "nonce": NONCE},
            report("t.py::ok", "call", "passed"),
            {**session_record(0), "nonce": OTHER_NONCE},
        )

        assert "not written by this run" in parse(tmp_path, text, nonce=NONCE).error

    def test_a_non_string_nonce_is_refused(self, tmp_path):
        text = jsonl({**start_record(), "nonce": 12345}, session_record(0))

        assert "not written by this run" in parse(tmp_path, text, nonce=NONCE).error

    def test_the_refusal_leaks_nothing_from_the_foreign_report(self, tmp_path):
        text = stamped(
            start_record(), report("tests/test_hidden_oracle.py::test_secret_fix", "call", "failed"),
            session_record(1), nonce=OTHER_NONCE,
        )

        result = parse(tmp_path, text, returncode=1, nonce=NONCE)

        assert "hidden_oracle" not in repr(result)
        assert result.failed == ()

    def test_no_nonce_expected_means_no_check(self, tmp_path):
        """Reports produced outside a container (the plugin under the host's pytest)."""
        assert parse(tmp_path, good_report(nonce=OTHER_NONCE)).error is None
        assert parse(tmp_path, good_report(nonce=None)).error is None

    def test_a_missing_session_is_still_reported_as_unfinished_not_as_foreign(self, tmp_path):
        """The diagnostic a killed run needs must survive the nonce check."""
        text = stamped(start_record(), report("t.py::ok", "call", "passed"))

        assert "did not finish" in parse(tmp_path, text, nonce=NONCE).error


class TestNoHostPathInAnError:
    """Every message here can reach the model through a probe, and the results
    directory sits under a random per-task root that a probe cannot otherwise learn."""

    def assert_no_path(self, result, tmp_path):
        text = repr(result)
        assert str(tmp_path) not in text, text
        assert tmp_path.name not in text, text

    def test_an_unreadable_report(self, tmp_path):
        """What a probe gets by `chmod 000`-ing its own report."""
        path = write(tmp_path, good_report())
        path.chmod(0)
        try:
            result = parse_report(path, run(0), elapsed=1.0, nonce=NONCE)
        finally:
            path.chmod(0o644)
        if os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")

        assert result.error is not None and "could not be parsed" in result.error
        assert "PermissionError" in result.error
        self.assert_no_path(result, tmp_path)

    def test_an_oserror_names_its_errno_not_its_file(self, tmp_path, monkeypatch):
        path = write(tmp_path, good_report())

        def boom(path):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr("verify.report._open_report", boom)
        result = parse_report(path, run(0), elapsed=1.0)

        assert "Permission denied" in result.error
        self.assert_no_path(result, tmp_path)

    def test_a_non_os_error_that_formatted_the_path_in_is_scrubbed(self, tmp_path, monkeypatch):
        path = write(tmp_path, good_report())

        def boom(path):
            raise RuntimeError(f"cannot handle {path} or {path.parent}")

        monkeypatch.setattr("verify.report._read_records", boom)
        result = parse_report(path, run(0), elapsed=1.0)

        assert "RuntimeError" in result.error and "cannot handle" in result.error
        self.assert_no_path(result, tmp_path)

    def test_every_refusal_message(self, tmp_path):
        link = tmp_path / "link.jsonl"
        link.symlink_to(tmp_path / "x")
        for result in (
            parse_report(link, run(0), elapsed=1.0),
            parse_report(tmp_path / "absent.jsonl", run(4), elapsed=1.0),
            parse(tmp_path, good_report(nonce=OTHER_NONCE), nonce=NONCE),
            parse(tmp_path, "{garbage\n{more garbage\n"),
        ):
            assert result.error is not None
            self.assert_no_path(result, tmp_path)


class TestStoredStringsAreSafeForPostgres:
    """The report is written by hostile code and stored in TEXT, ARRAY and JSONB
    columns, none of which accept a NUL, and none of which can encode a lone
    surrogate. One would fail the whole baseline stage as an unexplained error."""

    BAD = "a\x00b\x01c\x1b[2Jd\ud800e"
    CLEAN = "a\ufffdb\ufffdc\ufffd[2Jd\ufffde"

    def test_nul_and_control_characters_in_node_ids_are_replaced(self, tmp_path):
        text = jsonl(
            start_record(), report(f"t.py::{self.BAD}", "call", "passed"), session_record(0)
        )

        result = parse(tmp_path, text)

        assert result.passed == (f"t.py::{self.CLEAN}",)

    def test_the_stdout_tail(self, tmp_path):
        """Bytes, so a lone surrogate cannot be in it: undecodable input already arrives
        as U+FFFD. The control characters are what this has to catch."""
        result = parse(tmp_path, good_report(), stdout=b"xa\x00b\x01c\x1b[2Jdy")

        assert result.stdout_tail == "xa\ufffdb\ufffdc\ufffd[2Jdy"

    def test_a_newline_and_a_tab_survive(self, tmp_path):
        result = parse(tmp_path, good_report(), stdout=b"line one\n\tindented\n")

        assert result.stdout_tail == "line one\n\tindented\n"

    def test_collect_failures(self, tmp_path):
        text = jsonl(
            start_record(), report("t.py::ok", "call", "passed"),
            collect_record(f"bad{self.BAD}.py"), session_record(1),
        )

        assert parse(tmp_path, text, returncode=1).collect_failures == (f"bad{self.CLEAN}.py",)

    def test_every_string_in_the_fingerprint_keys_and_values_recursively(self, tmp_path):
        text = jsonl(
            start_record(
                rootdir=f"/repo{self.BAD}",
                ini={f"k{self.BAD}": [f"v{self.BAD}", {f"n{self.BAD}": f"w{self.BAD}"}]},
                plugins=[f"p{self.BAD}"],
            ),
            report("t.py::ok", "call", "passed"),
            session_record(0),
        )

        fingerprint = parse(tmp_path, text).fingerprint

        assert fingerprint == {
            "rootdir": f"/repo{self.CLEAN}",
            "ini": {f"k{self.CLEAN}": [f"v{self.CLEAN}", {f"n{self.CLEAN}": f"w{self.CLEAN}"}]},
            "plugins": [f"p{self.CLEAN}"],
        }

    def test_collected_files_and_conftests(self, tmp_path):
        text = jsonl(
            start_record(),
            {"kind": "files", "collected": [f"a{self.BAD}.py"], "conftests": [f"c{self.BAD}.py"]},
            report("t.py::ok", "call", "passed"),
            session_record(0),
        )

        result = parse(tmp_path, text)

        assert result.collected_files == (f"a{self.CLEAN}.py",)
        assert result.conftests == (f"c{self.CLEAN}.py",)

    def test_nothing_unsafe_survives_anywhere_in_the_result(self, tmp_path):
        text = jsonl(
            start_record(rootdir=self.BAD), report(f"t.py::{self.BAD}", "call", "failed"),
            collect_record(self.BAD), session_record(1),
        )

        result = parse(tmp_path, text, returncode=1, stdout=b"a\x00b\x01c\x1bd")

        assert not any(ch in repr(result) for ch in ("\x00", "\x01", "\x1b", "\\x00", "\\x1b"))
        assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in repr(result))
