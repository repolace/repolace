"""A module that fails to import must not abort the session: real pytest, real plugin, real parser.

Production runs pytest with `--continue-on-collection-errors`. Without it pytest stops
the whole session at the first collection error -- exit 2, nothing run, `passed` empty,
`pytest exited 2 (INTERRUPTED)` -- and everything downstream that reasons about
collection failures (`new_collect_failures`, the baseline's own broken modules, the
feedback the agent is shown) is dead code. Worse, two attempts that differ *only* in
whether a hidden test module imports would then give the agent different feedback and
different retry decisions.

The arguments here are taken from `build_run_argv` -- the real production builder --
with only the two container paths rewritten for the host, so this tests the flags that
ship rather than a copy of them. No Docker.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from repolace_shared.db.models import TaskOutcome
from verify.backends.docker import build_run_argv
from verify.config import DockerConfig
from verify.protocol import EnvironmentRef, RepoSpec
from verify.report import parse_report
from verify.scoring import agent_verdict, score

from repolace_shared.process import ProcessResult

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "verify" / "plugin"
FLAG = "--continue-on-collection-errors"
NONCE = "feedface" * 4

VISIBLE_TEST = "tests/test_visible.py::test_f"
HIDDEN_TEST = "tests/test_hidden.py::test_g"


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def production_args(root: Path, *, with_flag: bool = True) -> list[str]:
    """The pytest part of the production argv, with the container paths made host paths."""
    argv = build_run_argv(
        DockerConfig(),
        RepoSpec(key="a/b"),
        EnvironmentRef("docker", "image"),
        Path("/src"),
        Path("/res"),
        "c-0",
        forward_nonce=True,
    )
    args = list(argv[argv.index("pytest") + 1 :])
    args = [f"--rootdir={root}" if a == "--rootdir=/repo" else a for a in args]
    args = [f"cache_dir={root}/.pytest_cache" if a == "cache_dir=/tmp/.pytest_cache" else a for a in args]
    return args if with_flag else [a for a in args if a != FLAG]


def run(root: Path, tag: str, *, with_flag: bool = True):
    report = root / f"report-{tag}.jsonl"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *production_args(root, with_flag=with_flag)],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(PLUGIN_DIR),
            "REPOLACE_REPORT_PATH": str(report),
            "REPOLACE_RUN_NONCE": NONCE,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        timeout=120,
    )
    process = ProcessResult(
        returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )
    return parse_report(report, process, elapsed=1.0, nonce=NONCE), completed


def lay_out(root: Path, *, hidden: str, visible: str | None = None, g_body: str = "return x") -> None:
    """A package with one visible test and one hidden one (here just a second module)."""
    write(root, "pkg/__init__.py", f"def f(x):\n    return x + 1\n\ndef g(x):\n    {g_body}\n")
    write(root, "tests/test_visible.py", visible or "from pkg import f\ndef test_f():\n    assert f(1) == 2\n")
    write(root, "tests/test_hidden.py", hidden)


HIDDEN_COLLECTS = "from pkg import g\ndef test_g():\n    assert g(1) == 2\n"
HIDDEN_CANNOT_IMPORT = "from pkg import renamed_symbol\ndef test_g():\n    assert renamed_symbol(1) == 2\n"


@pytest.fixture(scope="module")
def worlds(tmp_path_factory):
    """The baseline and four attempts. Each differs from the baseline in one way."""
    root = tmp_path_factory.mktemp("collection")
    lay_out(root, hidden=HIDDEN_COLLECTS)
    results = {"baseline": run(root, "baseline")}
    results["X"] = run(root, "X")  # hidden still collects and still fails

    write(root, "tests/test_hidden.py", HIDDEN_CANNOT_IMPORT)
    results["Y"] = run(root, "Y")  # hidden cannot be imported
    results["Y_control"] = run(root, "Yc", with_flag=False)

    write(root, "tests/test_hidden.py", HIDDEN_COLLECTS)
    write(root, "tests/test_visible.py", "from pkg import renamed_symbol\ndef test_f():\n    assert True\n")
    results["Z"] = run(root, "Z")  # a VISIBLE module cannot be imported

    write(root, "tests/test_visible.py", "from pkg import f\ndef test_f():\n    assert f(1) == 2\n")
    write(root, "pkg/__init__.py", "def f(x):\n    return x + 1\n\ndef g(x):\n    return x + 1\n")
    results["fixed"] = run(root, "fixed")  # the genuine fix
    return {name: parsed for name, (parsed, _process) in results.items()}, results


def parsed(worlds, name):
    return worlds[0][name]


def process(worlds, name):
    return worlds[1][name][1]


class TestTheFlagIsInTheProductionArgv:
    def test_it_is_there_and_before_the_targets(self):
        argv = build_run_argv(
            DockerConfig(),
            RepoSpec(key="a/b", test_targets=("tests",), extra_pytest_args=("-x",)),
            EnvironmentRef("docker", "image"),
            Path("/src"),
            Path("/res"),
            "c-0",
        )

        assert FLAG in argv
        assert argv.index(FLAG) < argv.index("tests")
        assert argv[-2:] == ("tests", "-x")


class TestAHiddenModuleThatCannotBeImported:
    def test_the_baseline_is_scoreable_and_has_the_failing_hidden_test(self, worlds):
        baseline = parsed(worlds, "baseline")

        assert baseline.error is None, baseline.error
        assert baseline.failed == (HIDDEN_TEST,) and baseline.passed == (VISIBLE_TEST,)

    def test_the_session_is_not_aborted(self, worlds):
        """Exit 1 (tests failed), not 2 (interrupted), and the parser accepts it."""
        assert process(worlds, "Y").returncode == 1
        assert parsed(worlds, "Y").error is None, parsed(worlds, "Y").error

    def test_the_visible_results_survive(self, worlds):
        assert parsed(worlds, "Y").passed == (VISIBLE_TEST,)

    def test_the_broken_module_is_recorded_as_a_collection_failure(self, worlds):
        result = parsed(worlds, "Y")

        assert result.collect_failures == ("tests/test_hidden.py",)
        assert result.failed == ()

    def test_without_the_flag_the_session_is_aborted_and_the_visible_results_are_lost(self, worlds):
        """The control: what production did before. This is why the flag matters."""
        control = parsed(worlds, "Y_control")

        assert process(worlds, "Y_control").returncode == 2
        assert control.passed == ()
        assert control.error is not None and "INTERRUPTED" in control.error

    def test_the_two_worlds_give_the_same_visible_results(self, worlds):
        """Whether a hidden module imports must not change what is visible."""
        assert parsed(worlds, "X").passed == parsed(worlds, "Y").passed == (VISIBLE_TEST,)

    def test_a_module_that_was_already_broken_at_baseline_does_not_make_the_instance_unscoreable(
        self, tmp_path
    ):
        lay_out(tmp_path, hidden=HIDDEN_COLLECTS)
        write(tmp_path, "tests/test_legacy.py", "import a_module_that_never_existed\n")

        with_flag, _ = run(tmp_path, "legacy")
        without_flag, _ = run(tmp_path, "legacy-control", with_flag=False)

        assert with_flag.error is None, with_flag.error
        assert with_flag.collect_failures == ("tests/test_legacy.py",)
        assert with_flag.passed == (VISIBLE_TEST,) and with_flag.failed == (HIDDEN_TEST,)
        assert without_flag.error is not None and "INTERRUPTED" in without_flag.error


class TestWhatScoringMakesOfThoseRuns:
    """The collection rules downstream of the flag are only live in production because
    of it. Run through the real `score` and `agent_verdict`."""

    def test_a_hidden_module_broken_by_the_patch_is_never_scored_passed_when_curated(self, worlds):
        scored = score(
            parsed(worlds, "baseline"), parsed(worlds, "Y"), ["pkg/__init__.py"],
            expected_fail_to_pass=(HIDDEN_TEST,),
        )

        assert scored.outcome == TaskOutcome.FAILED
        assert HIDDEN_TEST in scored.neutralized  # it vanished rather than being fixed

    def test_nor_when_uncurated(self, worlds):
        scored = score(parsed(worlds, "baseline"), parsed(worlds, "Y"), ["pkg/__init__.py"])

        assert scored.outcome == TaskOutcome.FAILED
        assert not scored.inadmissible

    def test_a_visible_module_broken_by_the_patch_is_a_regression(self, worlds):
        """Its tests leave `passed`, which is exactly what regressions measures."""
        scored = score(
            parsed(worlds, "baseline"), parsed(worlds, "Z"), ["pkg/__init__.py"],
            expected_fail_to_pass=(HIDDEN_TEST,),
        )

        assert scored.outcome == TaskOutcome.FAILED
        assert scored.regressions == (VISIBLE_TEST,)

    def test_the_pr_gate_sees_the_broken_visible_module_twice_over(self, worlds):
        verdict = agent_verdict(parsed(worlds, "baseline"), parsed(worlds, "Z"), ["pkg/__init__.py"])

        assert verdict.ok is False
        assert verdict.regressions == (VISIBLE_TEST,)
        assert verdict.new_collect_failures == ("tests/test_visible.py",)

    def test_the_pr_gate_sees_a_broken_hidden_module_as_a_new_collection_failure(self, worlds):
        verdict = agent_verdict(parsed(worlds, "baseline"), parsed(worlds, "Y"), ["pkg/__init__.py"])

        assert verdict.ok is False
        assert verdict.new_collect_failures == ("tests/test_hidden.py",)

    def test_a_genuine_fix_still_passes_in_the_same_harness(self, worlds):
        """So the tests above are not passing because everything scores FAILED."""
        scored = score(
            parsed(worlds, "baseline"), parsed(worlds, "fixed"), ["pkg/__init__.py"],
            expected_fail_to_pass=(HIDDEN_TEST,),
        )

        assert scored.outcome == TaskOutcome.PASSED, scored.reason
        assert agent_verdict(parsed(worlds, "baseline"), parsed(worlds, "fixed"), ["pkg/__init__.py"]).ok

    def test_a_failing_test_that_still_collects_is_not_a_collection_failure(self, worlds):
        result = parsed(worlds, "X")

        assert result.collect_failures == () and result.failed == (HIDDEN_TEST,)
