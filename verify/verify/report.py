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

#: Hard cap on the report file itself, checked before anything is read.
#: MAX_TEST_IDS bounds ids *after* parsing, which is too late -- the container's
#: memory limit does not apply to the host process doing the reading, so an
#: oversized report would OOM the worker rather than the sandbox. Generous:
#: 20k tests x 3 phases x ~400 bytes is roughly 24 MB.
MAX_REPORT_BYTES = 64 * 1024 * 1024

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
    * `xfailed` before `skipped`, and read off the *skipping* record rather than
      off the node. Two properties of pytest make that scoping necessary rather
      than fussy. `xfail` is **per-phase** -- pytest sets `wasxfail` on the
      `call` report, so setup and teardown of the same node carry False and an
      `all()` would never fire. And it is **not exclusive to skips** -- a
      non-strict xpass is `outcome: passed` with `xfail: True`, and that node is
      a genuine pass that must stay in `passed`, not a silenced one. Reading the
      flag only off the record that carried the skip handles both without a
      special case. (A strict xpass carries no `wasxfail` at all and arrives as
      `failed`, which the first rule takes.)

      A `@pytest.mark.skip` layered on an xfail-marked test produces a skip
      *without* `wasxfail`, so it classifies as an ordinary skip -- correct, and
      deliberately so: an agent that adds a skip mark must stay in the
      disqualifying bucket.
    * `skipped` covers module-level skips and every ordinary skip.
    * requiring a `call` phase for `passed` is what leaves `did_not_run`
      reachable rather than a dead branch.
    """
    if any(r["outcome"] == "failed" for r in reports):
        return "failed"
    skips = [r for r in reports if r["outcome"] == "skipped"]
    if skips:
        return "xfailed" if any(r.get("xfail") for r in skips) else "skipped"
    if any(r["when"] == "call" and r["outcome"] == "passed" for r in reports):
        return "passed"
    return "did_not_run"


def _read_records(path: Path) -> tuple[list[dict], str | None]:
    """Parse the file line-wise, tolerating exactly one truncated trailing line.

    Streamed rather than slurped, and size-capped first: the report is written
    by untrusted code, and the container's memory limit does not constrain this
    process. `read_text` on an arbitrarily large file would OOM the worker
    rather than the sandbox.

    Per-line flushing means a killed run leaves at most one partial line. A
    malformed line anywhere else is real corruption and must not be silently
    skipped -- tolerating it would let a forged report hide behind a deliberate
    syntax error.
    """
    size = path.stat().st_size
    if size > MAX_REPORT_BYTES:
        return [], f"report is {size} bytes, above the {MAX_REPORT_BYTES} cap"

    records: list[dict] = []
    pending: tuple[int, str] | None = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if pending is not None:
                # The previous bad line was not the last one after all.
                return records, f"corrupt report at line {pending[0]}"
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                pending = (number, line)
                continue
            if not isinstance(record, dict) or not isinstance(record.get("kind"), str):
                # Well-formed JSON that is not a record: a bare `3`, `null`,
                # `[]`, or an object with no `kind`. Treated exactly like
                # corruption rather than skipped, for the same reason the
                # docstring gives above -- a forged report must not be able to
                # hide behind a bare scalar any more than behind a syntax error.
                return records, f"line {number} is not a report record"
            records.append(record)

    if pending is not None:
        log.warning("verify.report.truncated_tail", line=pending[0])
    return records, None


#: Required key -> expected type, per record kind. A record of a *known* kind
#: that does not match is refused rather than skipped: this file is written by
#: untrusted code, and a record we cannot read is a report we cannot trust.
#: Validating once here is what lets everything downstream read these keys by
#: bracket, so the reading code stays about pytest semantics rather than about
#: defensive access -- scattering `.get()` would turn crashes into wrong answers,
#: which is strictly worse.
#:
#: An unknown `kind` is ignored entirely, so the plugin can add a record type
#: without breaking a host that predates it.
_REQUIRED: dict[str, dict[str, type]] = {
    "test": {"nodeid": str, "when": str, "outcome": str},
    "collect": {"nodeid": str, "outcome": str},
    "session": {"v": int, "exitstatus": int},
    "start": {"v": int},
    "files": {"collected": list, "conftests": list},
}

#: Present-or-absent, but typed when present. `xfail` decides which bucket a
#: skipped node lands in, and therefore PASSED from FAILED, so a truthy `"no"`
#: must not slip through as True.
_OPTIONAL: dict[str, dict[str, type]] = {"test": {"xfail": bool}}


def _malformed(record: dict) -> str | None:
    """Why this record cannot be read, or None if it can."""
    kind = record["kind"]
    for key, expected in _REQUIRED.get(kind, {}).items():
        value = record.get(key)
        # bool is a subclass of int, so `True` would otherwise satisfy an int
        # check and arrive as exitstatus 1.
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            return f"{kind} record has a missing or malformed {key!r}"
    for key, expected in _OPTIONAL.get(kind, {}).items():
        if key in record and not isinstance(record[key], expected):
            return f"{kind} record has a malformed {key!r}"
    return None


def _exitstatus_disagrees(
    exitstatus: int | None, failed: tuple[str, ...], collect_failures: tuple[str, ...]
) -> str | None:
    """Compare pytest's own verdict with the one derived from the records.

    Only the two unambiguous codes are checked. Exit 6 (max warnings) can
    accompany either, and anything else has already been refused above.
    """
    trouble = bool(failed) or bool(collect_failures)
    if exitstatus == 0 and trouble:
        return f"report claims {len(failed)} failures but pytest exited 0"
    if exitstatus == 1 and not trouble:
        return "pytest exited 1 but the report contains no failure"
    return None


def parse_report(path: Path, process: ProcessResult, elapsed: float) -> SuiteResult:
    """Build a SuiteResult. Never raises for a bad report -- it sets `error`.

    The explicit checks in `_parse_report` are what *should* catch a bad report,
    and each one names what it caught. This wrapper exists because the promise
    in that first sentence is otherwise a claim rather than a property: the file
    is written by untrusted code inside the sandbox, and one unanticipated shape
    reaching this far would raise into the pipeline and turn a scoreable task
    into a crash. It also covers the OSError paths -- `stat` and `open` racing a
    file the sandbox is still deleting.

    Logged at error level deliberately. A bug here must be visible as a bug, not
    disappear into the unscoreable bucket alongside ordinary flakiness.
    """
    try:
        return _parse_report(path, process, elapsed)
    except Exception as exc:
        log.error("verify.report.unexpected", path=str(path), exc_info=True)
        return SuiteResult(
            exit_code=process.returncode,
            duration_seconds=round(elapsed, 3),
            error=redact(f"verify: report could not be parsed ({type(exc).__name__}: {exc})"),
        )


def _parse_report(path: Path, process: ProcessResult, elapsed: float) -> SuiteResult:
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

    for record in records:
        problem = _malformed(record)
        if problem:
            return unscoreable(problem)

    # Every start/session record is now guaranteed an int `v`, so this cannot
    # raise on a mixed None/int set -- and a record missing `v` entirely is
    # refused above rather than passing the gate as None.
    versions = {r["v"] for r in records if r["kind"] in ("start", "session")}
    if versions - {SCHEMA_VERSION}:
        return unscoreable(f"report schema {sorted(versions)}, expected {SCHEMA_VERSION}")

    sessions = [r for r in records if r["kind"] == "session"]
    if not sessions:
        # The one thing exit codes cannot express: a collection error under
        # --continue-on-collection-errors exits 1, exactly like a test failure.
        return unscoreable(f"suite did not finish (exit {exit_code}); report has no session record")

    by_node: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record["kind"] == "test":
            by_node[record["nodeid"]].append(record)

    # Deduped, and empty ids dropped. The plugin filters collect reports on
    # `outcome`, not on nodeid, so a *failing* session-level report -- whose
    # nodeid is the empty string -- reaches us; and it emits one record per
    # failing collector without deduping, so a module that fails at two levels
    # appears twice. Both are fixed here rather than in the plugin: the plugin
    # runs inside the sandbox, and the host must not depend on its filtering.
    collect_failures = tuple(sorted(
        {r["nodeid"] for r in records if r["kind"] == "collect" and r["nodeid"]}
    ))

    files = next((r for r in records if r["kind"] == "files"), {})
    # The lists are guaranteed lists, but not lists *of strings*, and sorted()
    # raises on mixed types. Filtering rather than refusing: a stray member is
    # not evidence the run is untrustworthy, unlike a malformed record.
    collected_files = tuple(sorted(p for p in files.get("collected", ()) if isinstance(p, str)))
    conftests = tuple(sorted(p for p in files.get("conftests", ()) if isinstance(p, str)))
    start = next((r for r in records if r["kind"] == "start"), {})
    fingerprint = {
        "rootdir": start.get("rootdir"),
        "ini": start.get("ini", {}),
        "plugins": start.get("plugins", []),
    }

    buckets: dict[str, list[str]] = defaultdict(list)
    for nodeid, reports in by_node.items():
        buckets[_verdict(reports)].append(nodeid)

    passed = tuple(sorted(buckets["passed"]))
    failed = tuple(sorted(buckets["failed"]))
    sets = {
        "passed": passed,
        "failed": failed,
        "skipped": tuple(sorted(buckets["skipped"])),
        "xfailed": tuple(sorted(buckets["xfailed"])),
        "did_not_run": tuple(sorted(buckets["did_not_run"])),
        "collect_failures": collect_failures,
        "collected_files": collected_files,
        "conftests": conftests,
        "fingerprint": fingerprint,
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

    mismatch = _exitstatus_disagrees(sessions[-1]["exitstatus"], failed, collect_failures)
    if mismatch:
        # pytest's own exit status is produced by the process, the sets by the
        # records. They should agree, and a disagreement means the report does
        # not describe the run that happened.
        #
        # This is a cheap consistency check, NOT a security control. The code
        # under test shares an interpreter with the plugin that writes these
        # records, so a determined patch can rewrite both. It catches the
        # accidental and the careless; see the module docstring.
        return unscoreable(mismatch, **sets)

    # A collection failure with tests still running is deliberately NOT an
    # error. At baseline those tests are in neither set; at an attempt their
    # disappearance shows up as a pass-to-pass regression, which is the right
    # answer and falls out of set difference for free.
    return SuiteResult(**base, **sets, error=None)
