"""Turn the sandbox's JSONL into a SuiteResult.

All the subtlety of "did this test pass" lives here rather than in the plugin,
so it is a pure function over strings and can be re-derived from stored reports
when the rule changes.

The distinction this module exists to protect: `error` set means the run is
**unscoreable** -- we never found out -- which is categorically different from
every test having failed. Conflating them lets infrastructure flakiness deflate
the benchmark number while looking like agent failure.
"""

import json
from collections import defaultdict
from pathlib import Path

import structlog

from repolace_shared.git import redact
from repolace_shared.process import ProcessResult
from verify.protocol import SuiteResult

log = structlog.get_logger()

SCHEMA_VERSION = 1

#: Beyond this the run is refused rather than truncated: a clipped `passed`
#: array corrupts pass-to-pass toward false regressions, and a wrong number that
#: looks like a result is worse than no number.
MAX_TEST_IDS = 20_000

#: pytest exit codes that still describe a complete, scoreable session.
#: 0 all passed, 1 tests failed, 6 max-warnings (the session finished first).
#: Deliberately excludes 5 (NO_TESTS_COLLECTED): a suite we could not find is
#: unscoreable, not "everything failed".
_SCOREABLE_EXITS = frozenset({0, 1, 6})

_EXIT_NAMES = {
    0: "OK", 1: "TESTS_FAILED", 2: "INTERRUPTED", 3: "INTERNAL_ERROR",
    4: "USAGE_ERROR", 5: "NO_TESTS_COLLECTED", 6: "MAX_WARNINGS_ERROR",
}

_STDOUT_TAIL = 2000


def _verdict(reports: list[dict]) -> str:
    """Aggregate every phase of one node into a single verdict.

    Order matters and is the whole point:

    * `failed` first catches a setup error (which emits no `call` report at all,
      so a call-keyed parser loses it) and a teardown failure on a test whose
      call phase passed (which a call-keyed parser would record as a pass).
    * `skipped` second covers module-level skips and xfail, which pytest reports
      as skipped carrying a `wasxfail` attribute.
    * requiring a `call` phase for `passed` is what leaves `did_not_run`
      reachable rather than a dead branch.
    """
    if any(r["outcome"] == "failed" for r in reports):
        return "failed"
    if any(r["outcome"] == "skipped" for r in reports):
        return "skipped"
    if any(r["when"] == "call" and r["outcome"] == "passed" for r in reports):
        return "passed"
    return "did_not_run"


def _read_records(path: Path) -> tuple[list[dict], str | None]:
    """Parse the file, tolerating exactly one truncated trailing line.

    Per-line flushing means a killed run leaves at most one partial line. A
    malformed line anywhere else is real corruption and must not be silently
    skipped.
    """
    records: list[dict] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if number == len(lines):
                log.warning("verify.report.truncated_tail", line=number)
                continue
            return records, f"corrupt report at line {number}"
    return records, None


def parse_report(path: Path, process: ProcessResult, elapsed: float) -> SuiteResult:
    """Build a SuiteResult. Never raises for a bad report -- it sets `error`."""
    tail = redact(process.stdout.decode("utf-8", errors="replace"))[-_STDOUT_TAIL:]
    exit_code = process.returncode
    base = {"exit_code": exit_code, "duration_seconds": round(elapsed, 3), "stdout_tail": tail}

    def unscoreable(reason: str, **fields) -> SuiteResult:
        return SuiteResult(**base, **fields, error=redact(f"verify: {reason}"))

    if process.timed_out:
        # run_process discards output on the kill path, so there is no tail.
        return unscoreable(f"suite exceeded its {elapsed:.0f}s deadline and was killed")
    if exit_code == 137:
        return unscoreable("container killed (exit 137); most likely the memory limit")
    if not path.is_file() or path.stat().st_size == 0:
        # Exit 4 (usage error) and an early internal error both run before any
        # hook fires, so no report exists at all. Naming that here saves the
        # next person a baffling debugging session.
        return unscoreable(
            f"no test report was written (exit {exit_code}); "
            f"a usage error or a startup failure runs before any plugin hook"
        )

    records, corruption = _read_records(path)
    if corruption:
        return unscoreable(corruption)

    versions = {r.get("v") for r in records if r["kind"] in ("start", "session")}
    if versions - {SCHEMA_VERSION, None}:
        return unscoreable(f"report schema {sorted(versions)}, expected {SCHEMA_VERSION}")

    if not any(r["kind"] == "session" for r in records):
        # The one thing exit codes cannot express: a collection error under
        # --continue-on-collection-errors exits 1, exactly like a test failure.
        return unscoreable(f"suite did not finish (exit {exit_code}); report has no session record")

    by_node: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record["kind"] == "test":
            by_node[record["nodeid"]].append(record)

    collect_failures = tuple(sorted(r["nodeid"] for r in records if r["kind"] == "collect"))

    buckets: dict[str, list[str]] = defaultdict(list)
    for nodeid, reports in by_node.items():
        buckets[_verdict(reports)].append(nodeid)

    passed = tuple(sorted(buckets["passed"]))
    failed = tuple(sorted(buckets["failed"]))
    sets = {
        "passed": passed,
        "failed": failed,
        "skipped": tuple(sorted(buckets["skipped"])),
        "did_not_run": tuple(sorted(buckets["did_not_run"])),
        "collect_failures": collect_failures,
    }

    if len(by_node) > MAX_TEST_IDS:
        return unscoreable(f"{len(by_node)} test ids exceeds the {MAX_TEST_IDS} cap", **sets)
    if exit_code not in _SCOREABLE_EXITS:
        name = _EXIT_NAMES.get(exit_code or -1, "UNKNOWN")
        return unscoreable(f"pytest exited {exit_code} ({name})", **sets)
    if collect_failures and not passed and not failed:
        return unscoreable(
            f"collection failed for {len(collect_failures)} module(s) and no test ran", **sets
        )

    # A collection failure with tests still running is deliberately NOT an
    # error. At baseline those tests are in neither set; at an attempt their
    # disappearance shows up as a pass-to-pass regression, which is the right
    # answer and falls out of set difference for free.
    return SuiteResult(**base, **sets, error=None)
