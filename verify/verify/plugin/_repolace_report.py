"""A pytest plugin that records raw report events as JSONL.

Injected into the sandbox and loaded with `-p _repolace_report`. Never imported
by repolace itself -- doing so would register it into our own test runs.

Records *events*, not verdicts. Deriving "did this test pass" is genuinely
subtle: a fixture error emits no `call` report at all, a teardown failure
attaches to a node that already reported `call=passed`, xfail arrives as a skip
carrying an attribute, and strict xpass arrives as a failure whose longrepr is a
plain string. Those rules will be revised as more repositories are run, and
keeping them on the host means they are pure functions over fixture strings
instead of logic locked inside a container -- and that stored reports can be
re-derived rather than re-run. Same argument as `task_test_runs` storing raw
pass/fail sets rather than derived lists.

Written for old interpreters on purpose: it runs inside whatever environment the
target repository pins, which may be considerably older than ours. Standard
library only, no annotations, no f-strings in the hot path.
"""

import json
import os

SCHEMA_VERSION = 1
DEFAULT_REPORT_PATH = "/results/report.jsonl"

#: Enough to identify a failure; not so much that one exception fills the file.
_LONGREPR_LIMIT = 2000


class _Recorder(object):
    def __init__(self, path):
        self._handle = open(path, "a", encoding="utf-8", newline="\n")
        self._tests = 0
        self._collect_failures = 0

    def write(self, record):
        # ensure_ascii so the byte stream is unambiguous whatever locale the
        # container ends up with, and so a node ID containing a tab or a
        # non-UTF-8-decodable name still round-trips.
        self._handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self):
        if self._handle is not None:
            try:
                os.fsync(self._handle.fileno())
            except OSError:
                pass
            self._handle.close()
            self._handle = None


_recorder = None


def _crash_message(report):
    """`reprcrash` exists only when longrepr is an exception repr.

    It is None for skips (a tuple) and for strict xpass (a plain string), so an
    unguarded access raises inside the hook.
    """
    longrepr = getattr(report, "longrepr", None)
    crash = getattr(longrepr, "reprcrash", None)
    if crash is None:
        return None
    message = getattr(crash, "message", None)
    return message[:_LONGREPR_LIMIT] if message else None


def pytest_configure(config):
    global _recorder
    try:
        path = os.environ.get("REPOLACE_REPORT_PATH", DEFAULT_REPORT_PATH)
        _recorder = _Recorder(path)
        _recorder.write({
            "kind": "start",
            "v": SCHEMA_VERSION,
            "pytest": getattr(config, "_repolace_version", None) or _pytest_version(),
            "rootdir": str(config.rootpath),
        })
    except Exception:
        _recorder = None


def _pytest_version():
    try:
        import pytest
        return pytest.__version__
    except Exception:
        return None


def pytest_runtest_logreport(report):
    """Every phase of every test, unfiltered. The host decides what it means."""
    if _recorder is None:
        return
    try:
        _recorder.write({
            "kind": "test",
            "nodeid": report.nodeid,
            "when": getattr(report, "when", "collect"),
            "outcome": report.outcome,
            # hasattr, never truthiness: wasxfail is '' when no reason was given.
            "xfail": hasattr(report, "wasxfail"),
            "duration": getattr(report, "duration", None),
            "crash": _crash_message(report),
            "longrepr_type": type(getattr(report, "longrepr", None)).__name__,
        })
        _recorder._tests += 1
    except Exception:
        pass


def pytest_collectreport(report):
    """Only failures.

    A clean run emits a collect report for every directory, module and class --
    around twenty for a single directory -- plus one for the session itself,
    whose nodeid is the empty string. Writing those unfiltered would inject ""
    as a test id.
    """
    if _recorder is None or report.outcome == "passed":
        return
    try:
        longrepr = getattr(report, "longrepr", None)
        _recorder.write({
            "kind": "collect",
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "longrepr": str(longrepr)[:_LONGREPR_LIMIT] if longrepr is not None else None,
        })
        _recorder._collect_failures += 1
    except Exception:
        pass


def pytest_sessionfinish(session, exitstatus):
    """The liveness marker.

    Its absence is how the host tells "the suite ran and everything failed" from
    "the suite never finished". Nothing else in the file can express that.
    """
    if _recorder is None:
        return
    try:
        _recorder.write({
            "kind": "session",
            "v": SCHEMA_VERSION,
            "exitstatus": int(exitstatus),
            "tests": _recorder._tests,
            "collect_failures": _recorder._collect_failures,
        })
    except Exception:
        pass
    finally:
        try:
            _recorder.close()
        except Exception:
            pass


def pytest_unconfigure(config):
    if _recorder is not None:
        try:
            _recorder.close()
        except Exception:
            pass
