"""Builders for verify tests.

`<package>_support.py`, globally unique, because pytest's prepend import mode
puts every test directory on sys.path and two files with the same name would
silently resolve to whichever was collected first.
"""

import json
from contextlib import contextmanager

import pytest

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


#: The fingerprint every `suite()` carries, so two suites built with defaults are
#: comparable. `fingerprint_changed` compares rootdir, ini and plugins, and an
#: empty fingerprint on both sides reads as "no drift" -- which would let a test
#: of the drift rule pass for the wrong reason.
STABLE_FINGERPRINT = {"rootdir": "/repo", "ini": {}, "plugins": []}


def suite(**overrides) -> SuiteResult:
    """A comparable `SuiteResult` for scoring tests: every set empty, a stable fingerprint."""
    fields = {"fingerprint": dict(STABLE_FINGERPRINT)}
    return SuiteResult(**{**fields, **overrides})


@contextmanager
def expect_stub(owner: str):
    """Assert the call inside is still a stub, and skip -- never fail -- once it is not.

    A stub test is only useful while the stub exists: it pins that the seam
    *refuses loudly* rather than returning a plausible empty value. But a plain
    `pytest.raises(NotImplementedError)` turns red the moment the stream that owns
    the stub implements it, and then forces that stream to delete a test in a
    file it does not own. So the call is made inside this block:

    * it raises `NotImplementedError` naming `owner` -- the stub, as expected; the
      error is swallowed and the test goes on to its remaining assertions (that
      the stub touched nothing, say);
    * it does anything else -- returns, or raises something else, such as
      `VerifierNotReady` from an implemented `run_subset` called before the
      baseline -- the stub has been replaced, and the test **skips** as
      "implemented".

    This is `try: <call> / except NotImplementedError: <assert> / else:
    pytest.skip("implemented")`, written once instead of at every call site, plus
    the second arm for an implementation that raises a different exception.
    """
    try:
        yield
    except NotImplementedError as exc:
        assert owner in str(exc), f"a stub must name the stream that owns it ({owner!r}), got: {exc}"
    except Exception:
        pytest.skip("implemented")
    else:
        pytest.skip("implemented")
