"""End-to-end: the real plugin, driven by a real pytest, parsed by the real parser.

Run in a subprocess rather than via `pytest.main` in-process, because that is
the only way to exercise the actual `-p` plugin-loading path and the actual
process exit codes -- and the exit code is what several of the parser's
unscoreable rules key on.

This is the test that would catch a producer/parser schema drift, which unit
tests on either side individually cannot.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from repolace_shared.process import ProcessResult
from verify.report import parse_report

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "verify" / "plugin"

SUITE = '''
import pytest

def test_plain_pass():
    assert True

def test_plain_fail():
    assert 1 == 2, "values differ"

@pytest.fixture
def broken_fixture():
    raise RuntimeError("fixture could not start")

def test_setup_error(broken_fixture):
    assert True

@pytest.fixture
def leaky():
    yield
    raise RuntimeError("teardown blew up")

def test_teardown_error(leaky):
    assert True

@pytest.mark.skip(reason="not applicable")
def test_skipped():
    assert True

@pytest.mark.xfail(reason="known bug")
def test_xfail():
    assert False

@pytest.mark.xfail(reason="fixed but still marked")
def test_xpass():
    assert True

@pytest.mark.xfail(strict=True, reason="must fail")
def test_xpass_strict():
    assert True

@pytest.mark.parametrize("value", ["a::b", "sp ace"])
def test_parametrised(value):
    assert True
'''


def run_suite(
    tmp_path: Path, suite: str, *extra: str, nonce: str | None = None
) -> tuple[Path, ProcessResult]:
    (tmp_path / "test_sample.py").write_text(textwrap.dedent(suite))
    report = tmp_path / "report.jsonl"
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "-p", "_repolace_report",
            "-p", "no:cacheprovider", "-p", "no:randomly", "-p", "no:xdist",
            "-o", "addopts=",
            "--continue-on-collection-errors",
            "-q", *extra, str(tmp_path),
        ],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(PLUGIN_DIR),
            "REPOLACE_REPORT_PATH": str(report),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            **({"REPOLACE_RUN_NONCE": nonce} if nonce is not None else {}),
        },
        capture_output=True,
        timeout=120,
    )
    return report, ProcessResult(
        returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("suite")
    report, process = run_suite(tmp_path, SUITE)
    assert report.is_file(), f"plugin wrote nothing; stderr={process.stderr[:2000]!r}"
    return parse_report(report, process, elapsed=1.0)


class TestAgainstRealPytest:
    def test_the_run_is_scoreable(self, parsed):
        assert parsed.error is None, parsed.error

    def test_a_plain_pass_is_passed(self, parsed):
        assert any(n.endswith("::test_plain_pass") for n in parsed.passed)

    def test_a_plain_failure_is_failed(self, parsed):
        assert any(n.endswith("::test_plain_fail") for n in parsed.failed)

    def test_a_fixture_error_is_failed(self, parsed):
        """Emits setup+teardown and no call report at all."""
        assert any(n.endswith("::test_setup_error") for n in parsed.failed)
        assert not any(n.endswith("::test_setup_error") for n in parsed.passed)

    def test_a_teardown_error_is_failed_despite_the_call_passing(self, parsed):
        """The case where reading the call report would report a pass."""
        assert any(n.endswith("::test_teardown_error") for n in parsed.failed)
        assert not any(n.endswith("::test_teardown_error") for n in parsed.passed)

    def test_a_skip_is_neither_passed_nor_failed(self, parsed):
        assert any(n.endswith("::test_skipped") for n in parsed.skipped)

    def test_xfail_is_its_own_bucket(self, parsed):
        """Against real pytest: wasxfail rides on the call report, and the
        surrounding setup/teardown reports carry xfail=False."""
        assert any(n.endswith("::test_xfail") for n in parsed.xfailed)
        assert not any(n.endswith("::test_xfail") for n in parsed.skipped)

    def test_a_non_strict_xpass_is_a_pass(self, parsed):
        """It carries wasxfail on a *passed* record, so a rule keyed on the
        xfail flag alone would misfile a genuine pass as silenced."""
        assert any(n.endswith("::test_xpass") for n in parsed.passed)
        assert not any(n.endswith("::test_xpass") for n in parsed.xfailed)

    def test_strict_xpass_is_failed(self, parsed):
        assert any(n.endswith("::test_xpass_strict") for n in parsed.failed)

    def test_a_parametrised_id_containing_colons_survives(self, parsed):
        """`test_x[a::b]` is a legal node id; splitting on `::` would corrupt the join key."""
        assert any("[a::b]" in n for n in parsed.passed)

    def test_a_parametrised_id_containing_a_space_survives(self, parsed):
        assert any("[sp ace]" in n for n in parsed.passed)

    def test_the_session_marker_is_present(self, parsed):
        """Its absence is how the parser tells 'did not finish' from 'all failed'."""
        assert parsed.error is None


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    """The same suite, twice, in the *same* directory.

    The same directory on purpose: rootdir is part of the fingerprint and is
    supposed to differ when the tree does, so running in two temp dirs would
    manufacture a difference production never sees -- the sandbox mounts every
    attempt at `/repo`. The report is removed between runs because the recorder
    opens it in append mode, and a second run appending to the first would parse
    as one run with duplicate records.
    """
    tmp_path = tmp_path_factory.mktemp("stability")
    results = []
    for _ in range(2):
        report, process = run_suite(tmp_path, "def test_ok():\n    assert True\n")
        results.append(parse_report(report, process, elapsed=1.0))
        report.unlink()
    return results


class TestFingerprintStability:
    """The fingerprint must be identical across two runs of an identical environment.

    `score` refuses to compare two runs whose fingerprints differ and returns
    FAILED without looking at a single test result. So an unstable fingerprint
    is not a degraded measurement -- it is a uniform, confident, wrong one on
    every task and every repository.

    This regressed once, and neither side was misbehaving: pytest names an
    anonymously-registered plugin by `id()`, `PytestPluginManager` registers
    itself that way on every run, and the host faithfully compared the addresses.
    It took actually running the sandbox twice to see it, which is why the check
    now lives here in a subprocess test that needs no daemon.
    """

    def test_the_whole_fingerprint_is_stable(self, two_runs):
        first, second = two_runs

        assert first.fingerprint == second.fingerprint

    def test_no_plugin_name_is_a_memory_address(self, two_runs):
        """The specific defect, pinned separately so a future change that makes
        the fingerprints merely *equal* by dropping the field still fails."""
        plugins = two_runs[0].fingerprint["plugins"]

        assert plugins, "the plugin list must not be empty -- that is not a fix"
        assert not any(name.isdigit() for name in plugins), plugins

    def test_an_anonymous_registration_is_still_counted(self, two_runs):
        """Dropping them silently would hide a conftest registering its own
        object, which genuinely changes what runs."""
        plugins = two_runs[0].fingerprint["plugins"]

        assert any(name.startswith("<anonymous:") for name in plugins), plugins


class TestCollectionFailure:
    def test_a_broken_import_is_recorded_and_does_not_hide_the_rest(self, tmp_path):
        (tmp_path / "test_broken.py").write_text("import definitely_not_a_real_module_xyz\n")
        report, process = run_suite(tmp_path, "def test_ok():\n    assert True\n")

        result = parse_report(report, process, elapsed=1.0)

        assert result.collect_failures, "the broken module should be recorded"
        assert any(n.endswith("::test_ok") for n in result.passed)
        assert result.error is None, "one broken import must not make the task unscoreable"


class TestNestedSessions:
    """pytest's own suite runs pytest inside a test, in process, with this plugin loaded.

    An inner session replaced the outer recorder and dropped it unflushed, so the
    host found an empty report and called the run unscoreable. That made every
    pytest-on-pytest benchmark instance unusable.
    """

    SUITE = """
        import pytest

        def test_runs_an_inner_session(tmp_path):
            (tmp_path / "test_inner.py").write_text("def test_inner_one():\\n    assert True\\n")
            assert pytest.main(["-p", "_repolace_report", "-p", "no:cacheprovider", "-o", "addopts=", str(tmp_path)]) == 0

        def test_after_the_inner_session():
            assert True
    """

    def test_the_outer_report_survives_and_holds_only_the_outer_tests(self, tmp_path):
        report, process = run_suite(tmp_path, self.SUITE)

        result = parse_report(report, process, elapsed=1.0)

        assert result.error is None, result.error
        assert any(n.endswith("::test_runs_an_inner_session") for n in result.passed)
        assert any(n.endswith("::test_after_the_inner_session") for n in result.passed)
        assert not any("test_inner_one" in n for n in result.passed + result.failed)

    def test_the_session_marker_is_the_outer_ones(self, tmp_path):
        report, _ = run_suite(tmp_path, self.SUITE)

        records = [json.loads(line) for line in report.read_text().splitlines()]

        assert [r["kind"] for r in records].count("session") == 1
        assert [r["kind"] for r in records].count("start") == 1


class TestNoReport:
    def test_a_usage_error_produces_no_report_and_is_unscoreable(self, tmp_path):
        """Exit 4 happens before any hook fires, so the file never exists."""
        report, process = run_suite(tmp_path, "def test_ok():\n    assert True\n", "--nonexistent-flag")

        result = parse_report(report, process, elapsed=1.0)

        assert process.returncode == 4
        assert result.error is not None and "no test report" in result.error


class TestTheRunNonce:
    """The host hands the container a per-run nonce; the plugin stamps it into the
    report so a report from any other run can be told apart from this one's."""

    NONCE = "c0ffee00" * 4

    @staticmethod
    def records(report: Path) -> list[dict]:
        return [json.loads(line) for line in report.read_text().splitlines() if line.strip()]

    def test_it_lands_in_the_start_and_session_records(self, tmp_path):
        report, _process = run_suite(tmp_path, "def test_ok():\n    assert True\n", nonce=self.NONCE)

        by_kind = {r["kind"]: r for r in self.records(report)}

        assert by_kind["start"]["nonce"] == self.NONCE
        assert by_kind["session"]["nonce"] == self.NONCE

    def test_it_is_not_repeated_on_every_test_record(self, tmp_path):
        """Two records prove whose report it is; stamping each test would only grow the file."""
        report, _process = run_suite(tmp_path, "def test_ok():\n    assert True\n", nonce=self.NONCE)

        stamped = [r["kind"] for r in self.records(report) if "nonce" in r]

        assert sorted(stamped) == ["session", "start"]

    def test_the_parser_accepts_the_real_report_with_the_nonce(self, tmp_path):
        report, process = run_suite(tmp_path, "def test_ok():\n    assert True\n", nonce=self.NONCE)

        result = parse_report(report, process, elapsed=1.0, nonce=self.NONCE)

        assert result.error is None, result.error
        assert any(n.endswith("::test_ok") for n in result.passed)

    def test_the_parser_refuses_it_under_a_different_nonce(self, tmp_path):
        report, process = run_suite(tmp_path, "def test_ok():\n    assert True\n", nonce=self.NONCE)

        result = parse_report(report, process, elapsed=1.0, nonce="deadbeef" * 4)

        assert result.error is not None and "not written by this run" in result.error

    def test_a_run_without_one_writes_no_nonce_key_at_all(self, tmp_path):
        """Outside the sandbox the variable is absent, and the key must be too --
        not an empty string a parser could mistake for a match."""
        report, _process = run_suite(tmp_path, "def test_ok():\n    assert True\n")

        assert not any("nonce" in r for r in self.records(report))

    def test_the_parser_refuses_a_report_that_has_none_when_it_expects_one(self, tmp_path):
        report, process = run_suite(tmp_path, "def test_ok():\n    assert True\n")

        assert "not written by this run" in parse_report(
            report, process, elapsed=1.0, nonce=self.NONCE
        ).error
