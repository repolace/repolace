"""Builders for verify tests.

`<package>_support.py`, globally unique, because pytest's prepend import mode
puts every test directory on sys.path and two files with the same name would
silently resolve to whichever was collected first.
"""

import json

from verify.protocol import RepoSpec, SuiteResult


def spec(**overrides) -> RepoSpec:
    fields = {"key": "acme/sample"}
    return RepoSpec(**{**fields, **overrides})


def result(**overrides) -> SuiteResult:
    return SuiteResult(**overrides)


def jsonl(*records: dict) -> str:
    """Render records the way the plugin does: one compact ASCII object per line."""
    return "".join(json.dumps(r, ensure_ascii=True, sort_keys=True) + "\n" for r in records)


def report(nodeid: str, when: str, outcome: str, **extra) -> dict:
    """Named `report`, not `test_record`: pytest collects test_* functions out of
    the importing module's namespace, so a helper with that prefix becomes a
    spurious test with unfillable fixture arguments."""
    return {
        "kind": "test",
        "nodeid": nodeid,
        "when": when,
        "outcome": outcome,
        "xfail": extra.pop("xfail", False),
        "duration": extra.pop("duration", 0.001),
        "crash": extra.pop("crash", None),
        "longrepr_type": extra.pop("longrepr_type", "NoneType"),
        **extra,
    }


def session_record(exitstatus: int = 0, **extra) -> dict:
    return {"kind": "session", "v": 1, "exitstatus": exitstatus, **extra}


def start_record(**extra) -> dict:
    return {"kind": "start", "v": 1, "pytest": "9.1.1", **extra}


def collect_record(nodeid: str, **extra) -> dict:
    return {"kind": "collect", "nodeid": nodeid, "outcome": "failed",
            "longrepr": extra.pop("longrepr", "ImportError: no module named x"), **extra}
