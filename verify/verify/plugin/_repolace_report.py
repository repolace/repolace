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

#: ini options that change what runs or whether it passes, without touching a
#: test file. `-o addopts=` clears only addopts, so these are recorded and the
#: host requires them identical between baseline and attempt -- otherwise an
#: agent relaxes `filterwarnings` in pyproject.toml and a real failure becomes a
#: real pass with a source-shaped diff.
_WATCHED_INI = (
    "addopts", "filterwarnings", "xfail_strict", "testpaths", "norecursedirs",
    "python_files", "python_functions", "python_classes", "minversion",
    "required_plugins", "usefixtures", "markers",
)

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
            "pytest": _pytest_version(),
            # rootdir as data rather than as an argv convention: node ids are
            # relative to it, so a shift silently renames every test.
            "rootdir": str(config.rootpath),
            "inipath": str(config.inipath) if config.inipath else None,
            "ini": _watched_ini(config),
            "plugins": _plugin_names(config),
        })
    except Exception:
        _recorder = None


def _watched_ini(config):
    values = {}
    for name in _WATCHED_INI:
        try:
            value = config.getini(name)
        except (ValueError, KeyError):
            continue
        values[name] = [str(v) for v in value] if isinstance(value, (list, tuple)) else str(value)
    return values


def _plugin_names(config):
    """Registered plugin names, with unnameable registrations counted, not named.

    PYTEST_DISABLE_PLUGIN_AUTOLOAD stops entry-point autoload, but a conftest
    can still import an installed plugin and register it by hand -- which is how
    `pytest-randomly` reorders a suite that was supposed to be deterministic.

    Two kinds of registration have no stable name, and the host compares this
    list between the baseline and every attempt:

    * A conftest is registered under its **path**, which moves with rootdir.
    * pytest names a plugin `getattr(plugin, "__name__", None) or str(id(plugin))`,
      so anything registered as a plain object is named by its **memory
      address**. `PytestPluginManager` registers itself exactly that way, on
      every run, so this list held a fresh random string every time.

    The second one was found by running the sandbox for the first time, and it
    was not survivable: `fingerprint_changed` would have reported "plugins
    changed between baseline and attempt" for **every task on every
    repository**, and `score` returns FAILED on that before it looks at a single
    test result. A benchmark of uniform, confident, spurious failures -- which
    no unit test on either side of the boundary could have caught, because both
    sides were individually behaving as written.

    Counted rather than dropped: the count is stable for a given pytest, so an
    *extra* anonymous plugin -- a conftest registering some object of its own --
    still shows up as a difference, which is the signal worth keeping.
    """
    try:
        names = [name for name, _ in config.pluginmanager.list_name_plugin()]
    except Exception:
        return []
    stable = []
    anonymous = 0
    for name in names:
        if not name or name.startswith("/"):
            continue
        if name.isdigit():
            # str(id(plugin)). A real __name__ is an identifier and cannot be
            # all digits, so this cannot swallow a genuine plugin.
            anonymous += 1
            continue
        stable.append(name)
    stable.sort()
    if anonymous:
        stable.append("<anonymous:%d>" % anonymous)
    return stable


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


def pytest_collection_finish(session):
    """The files pytest actually collected tests from, and the conftests it loaded.

    Authoritative for this repository, which a path heuristic cannot be: it
    neither misses `tests.py` nor wrongly disqualifies a shipped module like
    `django/test/client.py`. The host unions it with the heuristic, which is
    still needed to catch test files the baseline never saw.
    """
    if _recorder is None:
        return
    try:
        files = set()
        for item in session.items:
            location = getattr(item, "location", None)
            if location and location[0]:
                files.add(str(location[0]))
        conftests = set()
        for plugin in session.config.pluginmanager.get_plugins():
            path = getattr(plugin, "__file__", None) or ""
            if os.path.basename(path) == "conftest.py":
                conftests.add(path)
        _recorder.write({
            "kind": "files",
            "collected": sorted(files),
            "conftests": sorted(conftests),
        })
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
