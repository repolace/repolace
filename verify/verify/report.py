"""Turn the sandbox's JSONL into a SuiteResult.

All the subtlety of "did this test pass" lives here rather than in the plugin,
so it is a pure function over strings and can be re-derived from stored reports
when the rule changes.

The distinction this module exists to protect: `error` set means the run is
**unscoreable** -- we never found out -- which is categorically different from
every test having failed. Conflating them lets infrastructure flakiness deflate
the benchmark number while looking like agent failure.
"""

import errno
import json
import os
import re
import stat
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

#: What replaces a character that has no business in a stored string.
_REPLACEMENT = "\ufffd"

#: NUL and every C0 control character except `\n` and `\t`, plus lone surrogates.
#: Everything parsed here was written by code the host does not trust, and it ends
#: up in Postgres TEXT, ARRAY and JSONB columns: a NUL is rejected by all three, and
#: a lone surrogate cannot be encoded as UTF-8 at all. Either would fail the whole
#: `record_baseline` stage with an error that names neither, which reads as an
#: instrument failure rather than as a hostile report. (ESC and the rest of C0 are
#: also what a terminal would act on if the text were ever echoed to one.)
_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\ud800-\udfff]")

#: Fixed text for a report that is not a regular file. Names no path: it reaches the
#: model through a probe, and a host path is how a probe learns where a sibling
#: run's report lives.
_NOT_REGULAR = (
    "report is not a regular file (a symlink, pipe, device or directory was found "
    "where the report belongs)"
)

#: Fixed text for a report that was not written by this run.
_WRONG_RUN = "report was not written by this run (its run nonce is missing or wrong)"


class _MissingReport(Exception):
    """No report at all, or an empty one. Carries nothing: the caller knows the exit code."""


def _clean(value):
    """Replace unsafe characters in every string of a parsed JSON value, keys included."""
    if isinstance(value, str):
        return _UNSAFE_CHARS.sub(_REPLACEMENT, value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {_clean(key): _clean(item) for key, item in value.items()}
    return value


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


def _open_report(path: Path) -> int:
    """Open the report without following a symlink or blocking on a pipe.

    The sandbox owns the directory the report lives in, so whatever is at this path
    is whatever the code under test left there. `O_NOFOLLOW` is what stops a link to
    *another run's* report -- the baseline's, written with the hidden tests
    overlaid -- being parsed as this one's and handed back to the agent. (The report
    sits one component below the mount root and a hard link cannot cross bind
    mounts, so a symlink is the only way to point this path elsewhere.)
    `O_NONBLOCK` because opening a FIFO for reading otherwise waits for a writer
    that will never come.
    """
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)


def _read_records(path: Path) -> tuple[list[dict], str | None]:
    """Parse the file line-wise, tolerating exactly one truncated trailing line.

    Streamed rather than slurped, and size-capped first: the report is written
    by untrusted code, and the container's memory limit does not constrain this
    process. `read_text` on an arbitrarily large file would OOM the worker
    rather than the sandbox.

    The file is opened once and every check is made on the descriptor, not the
    path: a symlink, FIFO, device or directory is refused, and the size cap is read
    off `fstat`, so the thing checked is the thing read. Raises `_MissingReport`
    for no file or an empty one; every other refusal is a returned reason, so the
    caller never has to tell them apart by exception type.

    Per-line flushing means a killed run leaves at most one partial line. A
    malformed line anywhere else is real corruption and must not be silently
    skipped -- tolerating it would let a forged report hide behind a deliberate
    syntax error.
    """
    try:
        descriptor = _open_report(path)
    except FileNotFoundError:
        raise _MissingReport from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # O_NOFOLLOW met a symlink
            return [], _NOT_REGULAR
        raise

    handle = None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return [], _NOT_REGULAR
        if info.st_size == 0:
            raise _MissingReport
        if info.st_size > MAX_REPORT_BYTES:
            return [], f"report is {info.st_size} bytes, above the {MAX_REPORT_BYTES} cap"
        handle = os.fdopen(descriptor, "r", encoding="utf-8", errors="replace")
    finally:
        # Every early return above lands here with the descriptor still ours; only
        # a successful fdopen hands it to `handle`, whose `with` closes it below.
        if handle is None:
            os.close(descriptor)

    records: list[dict] = []
    pending: tuple[int, str] | None = None
    with handle:  # owns the descriptor from here
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
            records.append(_clean(record))

    if pending is not None:
        log.warning("verify.report.truncated_tail", line=pending[0])
    return records, None


def _foreign_run(records: list[dict], nonce: str) -> bool:
    """Whether this report fails to prove it was written by the run that expects it.

    The plugin stamps the per-run nonce into its `start` and `session` records, and
    the nonce was handed to the container as an environment variable only the host
    and that container know. A report from any *other* run -- planted by a symlink,
    or left over in a reused directory -- carries a different one, so it is refused
    even if the symlink protection above is ever regressed.

    A `start` record must be present (the plugin always writes one first), and
    every `start` and `session` record must match. This is integrity against
    substitution, **not** authentication of the code under test: that code shares
    the container, and so the environment, with the plugin.
    """
    stamped = [r for r in records if r["kind"] in ("start", "session")]
    if not any(r["kind"] == "start" for r in stamped):
        return True
    return any(r.get("nonce") != nonce for r in stamped)


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


def _describe(exc: Exception, path: Path) -> str:
    """An exception as text that is safe to hand to the model: no host path in it.

    An `OSError` is described by its errno alone -- its message names the file, and
    the file is under a random per-task directory that a probe has no other way to
    learn. Anything else keeps its message, with the report's own path scrubbed out
    in case it was formatted in.
    """
    if isinstance(exc, OSError):
        detail = exc.strerror or errno.errorcode.get(exc.errno or 0, "OS error")
        return f"{type(exc).__name__}: {detail}"
    text = str(exc)
    # `realpath` rather than `Path.resolve`: this runs on the error path and must
    # not raise, and `realpath` does not.
    known = {str(path), str(path.parent), os.path.dirname(os.path.realpath(path))}
    for host_path in sorted(known, key=len, reverse=True):
        text = text.replace(host_path, "<results>")
    return f"{type(exc).__name__}: {text}"


def parse_report(
    path: Path, process: ProcessResult, elapsed: float, *, nonce: str | None = None
) -> SuiteResult:
    """Build a SuiteResult. Never raises for a bad report -- it sets `error`.

    `nonce` is the per-run token the sandbox was given. When it is passed, a report
    whose `start` and `session` records do not all carry it is unscoreable -- see
    `_foreign_run`. The Docker backend always passes one; it is optional only so a
    report produced outside a container (the plugin run under the host's pytest)
    can still be parsed.

    The explicit checks in `_parse_report` are what *should* catch a bad report,
    and each one names what it caught. This wrapper exists because the promise
    in that first sentence is otherwise a claim rather than a property: the file
    is written by untrusted code inside the sandbox, and one unanticipated shape
    reaching this far would raise into the pipeline and turn a scoreable task
    into a crash. It also covers the OSError paths -- `open` racing a file the
    sandbox is still deleting.

    No error text names a host path: it reaches the model through a probe, and the
    path is where a sibling run's report lives (see `_describe`).

    Logged at error level deliberately. A bug here must be visible as a bug, not
    disappear into the unscoreable bucket alongside ordinary flakiness. The log, for
    the operator, does carry the path.
    """
    try:
        return _parse_report(path, process, elapsed, nonce)
    except Exception as exc:
        log.error("verify.report.unexpected", path=str(path), exc_info=True)
        return SuiteResult(
            exit_code=process.returncode,
            duration_seconds=round(elapsed, 3),
            error=_clean(redact(f"verify: report could not be parsed ({_describe(exc, path)})")),
        )


def _parse_report(
    path: Path, process: ProcessResult, elapsed: float, nonce: str | None
) -> SuiteResult:
    tail = _clean(redact(process.stdout.decode("utf-8", errors="replace"))[-_STDOUT_TAIL:])
    exit_code = process.returncode
    base = {"exit_code": exit_code, "duration_seconds": round(elapsed, 3), "stdout_tail": tail}

    def unscoreable(reason: str, **fields) -> SuiteResult:
        # stdout and stderr are captured separately, and a run that never got as
        # far as the plugin (an import error in pytest or a conftest, a broken
        # install) says why on stderr. Keeping only stdout left such a run with
        # an empty tail and no way to diagnose it. Only an unscoreable run gets
        # it: a scored run's stderr is the code under test talking.
        error_tail = _clean(redact(process.stderr.decode("utf-8", errors="replace"))[-_STDOUT_TAIL:])
        fields_base = dict(base)
        if error_tail:
            fields_base["stdout_tail"] = f"{tail}\n--- stderr ---\n{error_tail}" if tail else f"--- stderr ---\n{error_tail}"
        return SuiteResult(**fields_base, **fields, error=redact(f"verify: {reason}"))

    if process.timed_out:
        # run_process discards output on the kill path, so there is no tail.
        return unscoreable(f"suite exceeded its {elapsed:.0f}s deadline and was killed")
    if exit_code == 137:
        return unscoreable("container killed (exit 137); most likely the memory limit")

    try:
        records, corruption = _read_records(path)
    except _MissingReport:
        # Exit 4 (usage error) and an early internal error both run before any
        # hook fires, so no report exists at all. Naming that here saves the
        # next person a baffling debugging session.
        return unscoreable(
            f"no test report was written (exit {exit_code}); "
            f"a usage error or a startup failure runs before any plugin hook"
        )
    if corruption:
        return unscoreable(corruption)

    for record in records:
        problem = _malformed(record)
        if problem:
            return unscoreable(problem)

    if nonce is not None and _foreign_run(records, nonce):
        return unscoreable(_WRONG_RUN)

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
