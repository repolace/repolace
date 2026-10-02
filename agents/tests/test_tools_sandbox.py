"""`run_python` and `run_tests`: ordering, argument safety, and what reaches the model.

The ordering test is the one that matters most. The sandbox export refuses a tree
that differs from HEAD, and `FakeWorkspace` refuses the same way, so a tool that
forgot to checkpoint raises inside the fake instead of passing.
"""

import dataclasses

import pytest
from verify.protocol import EnvironmentRef, RepoSpec, ScriptResult, SuiteResult
from verify.stage import VerifierNotReady
from verify.testing import FakeBackend

from repolace_agents.tools import ToolLimits, build_toolbox
from repolace_shared.git import GitCommandError

from agents_support import FakeToolCall
from tools_support import make_harness, snapshot

pytestmark = pytest.mark.anyio

TARGET_REFUSALS = [
    "-p evil",
    "-p",
    "--rootdir=/",
    "-x",
    "--co",
    "-",
    "../x.py",
    "tests/../../x.py",
    "a b",
    "tests/test_core.py -x",
    "tests/test_core.py\n",
    "tests/test_core.py;rm -rf /",
    "$(id)",
    "`id`",
    "tests/test_core.py'",
    'tests/"x.py',
    "tests/nope.py",
    "nope",
    "/etc/passwd",
    ".git/config",
    ".gitattributes",
    "::test_add",
    "tests/test_core.py::test add",
    "@a.txt",
    "@tests/test_core.py",
]


class TestRunPython:
    async def test_the_tree_is_checkpointed_before_the_script_runs(self, tmp_path):
        h = make_harness(tmp_path)
        h.workspace.set_dirty(True)  # an edit the sandbox export would refuse

        out = await h.call("run_python", code="print(1)")

        assert not out.is_error
        assert h.events == ["checkpoint: checkpoint: before script", "run_script"]
        assert h.workspace.checkpoints == ["checkpoint: before script"]

    async def test_the_code_and_the_default_timeout_reach_the_sandbox(self, tmp_path):
        h = make_harness(tmp_path)

        await h.call("run_python", code="print('hi')")
        await h.call("run_python", code="x = 1", timeout_seconds=7)

        assert h.script_calls == [("print('hi')", 60.0), ("x = 1", 7)]

    async def test_code_that_cannot_be_written_to_a_file_is_refused_before_the_checkpoint(self, tmp_path):
        h = make_harness(tmp_path)

        out = await h.call("run_python", code="\ud800")

        assert out.is_error and "not valid text" in out.content
        assert h.events == []

    async def test_a_timeout_above_the_maximum_is_refused_by_the_schema(self, tmp_path):
        h = make_harness(tmp_path)

        out = await h.call("run_python", code="x", timeout_seconds=121)

        assert out.is_error and "at most 120" in out.content
        assert h.events == []

    async def test_the_timeout_is_clamped_to_the_limit_even_when_the_default_exceeds_it(self, tmp_path):
        h = make_harness(tmp_path, limits=ToolLimits(default_script_timeout=60.0, max_script_timeout=30.0))

        await h.call("run_python", code="x")

        assert h.script_calls == [("x", 30.0)]

    async def test_it_reports_exit_code_and_both_streams(self, tmp_path):
        h = make_harness(tmp_path, script_results=[ScriptResult(exit_code=1, stdout="out\n", stderr="Traceback\nKeyError: 'a'\n")])

        out = await h.call("run_python", code="x")

        assert not out.is_error
        assert out.content.startswith("exit code: 1\n--- stdout ---\nout\n")
        assert out.content.endswith("--- stderr ---\nTraceback\nKeyError: 'a'\n")

    async def test_a_nonzero_exit_is_information_not_an_error(self, tmp_path):
        h = make_harness(tmp_path, script_results=[ScriptResult(exit_code=2)])

        assert not (await h.call("run_python", code="x")).is_error

    async def test_empty_streams_are_labelled_so_silence_is_visible(self, tmp_path):
        h = make_harness(tmp_path, script_results=[ScriptResult(exit_code=0)])

        out = await h.call("run_python", code="x")

        assert out.content.count("(empty)") == 2

    async def test_a_timeout_and_a_capture_cut_are_stated(self, tmp_path):
        h = make_harness(tmp_path, script_results=[ScriptResult(exit_code=None, timed_out=True, truncated=True)])

        out = await h.call("run_python", code="x", timeout_seconds=5)

        assert "exit code: none" in out.content
        assert "killed after the 5s timeout" in out.content and "capture limit" in out.content

    async def test_the_end_of_a_long_traceback_survives_the_output_cap(self, tmp_path):
        # The box cuts from the end, and the end is the traceback.
        h = make_harness(tmp_path, script_results=[ScriptResult(exit_code=1, stdout="o" * 20_000, stderr="e" * 20_000 + "\nKeyError: boom")])

        out = await h.call("run_python", code="x")

        assert len(out.content) <= 8000 and "[truncated" not in out.content
        assert out.content.endswith("KeyError: boom")
        assert out.content.index("--- stdout ---") < out.content.index("--- stderr ---")

    async def test_each_stream_is_capped_at_four_thousand_characters(self, tmp_path):
        h = make_harness(tmp_path, limits=ToolLimits(max_output_chars=100_000), script_results=[ScriptResult(exit_code=0, stdout="Z" * 20_000, stderr="Q" * 20_000)])

        out = await h.call("run_python", code="x")

        assert 3000 < out.content.count("Z") <= 4000 and 3000 < out.content.count("Q") <= 4000

    async def test_no_sandbox_is_an_error_and_nothing_is_committed(self, tmp_path):
        h = make_harness(tmp_path, run_script=None)

        out = await h.call("run_python", code="x")

        assert out.is_error and out.content.startswith("the sandbox is unavailable:")
        assert h.events == []

    async def test_a_runtime_failure_is_an_error_not_a_crash(self, tmp_path):
        h = make_harness(tmp_path, script_results=[ScriptResult(exit_code=None, error="container runtime unavailable: no daemon")])

        out = await h.call("run_python", code="x")

        assert out.is_error and out.content.startswith("the sandbox is unavailable: container runtime unavailable")

    async def test_a_verifier_that_is_not_ready_is_an_error_not_a_crash(self, tmp_path):
        h = make_harness(tmp_path, script_results=[VerifierNotReady("no environment prepared yet")])

        out = await h.call("run_python", code="x")

        assert out.is_error and out.content.startswith("the sandbox is unavailable:")

    async def test_a_long_runtime_error_is_clipped(self, tmp_path):
        h = make_harness(tmp_path, script_results=[ScriptResult(exit_code=None, error="boom " * 500)])

        out = await h.call("run_python", code="x")

        assert len(out.content) < 400

    async def test_anything_else_the_sandbox_raises_is_a_bug_and_propagates(self, tmp_path):
        h = make_harness(tmp_path, script_results=[RuntimeError("a bug")])

        with pytest.raises(RuntimeError, match="a bug"):
            await h.call("run_python", code="x")

    async def test_a_script_cannot_change_the_checkout(self, tmp_path):
        h = make_harness(tmp_path)
        before = snapshot(h.checkout)

        await h.call("run_python", code="open('/repo/x.py', 'w').write('boom')")

        assert snapshot(h.checkout) == before

    async def test_the_description_states_the_sandbox_contract(self, tmp_path):
        description = make_harness(tmp_path).box.schemas()[6]["function"]["description"]

        for fact in ("NO network", "/scratch/main.py", "/repo", "read-only", "Nothing the script writes persists"):
            assert fact in description


class TestCheckpointFailure:
    async def test_a_tree_that_cannot_be_snapshotted_is_a_tool_error_not_a_crash(self, tmp_path):
        # Whatever else breaks a checkpoint, the model sees a message and the run goes on.
        async def failing_checkpoint(message):
            raise GitCommandError(["add", "--all"], 128, "error: invalid path 'git~1/hooks/pc'")

        h = make_harness(tmp_path, checkpoint=failing_checkpoint)

        for name, args in (("run_python", {"code": "print(1)"}), ("run_tests", {"targets": ["tests/test_core.py"]})):
            out = await h.call(name, **args)
            assert out.is_error and out.content.startswith("could not snapshot the working tree:")
            assert "invalid path" in out.content
        assert h.script_calls == [] and h.subset_calls == []

    async def test_a_failure_that_is_not_git_is_a_bug_and_propagates(self, tmp_path):
        async def broken_checkpoint(message):
            raise RuntimeError("a bug")

        h = make_harness(tmp_path, checkpoint=broken_checkpoint)

        with pytest.raises(RuntimeError, match="a bug"):
            await h.call("run_python", code="x")


class TestRunTests:
    async def test_the_tree_is_checkpointed_before_the_probe_runs(self, tmp_path):
        h = make_harness(tmp_path)
        h.workspace.set_dirty(True)

        out = await h.call("run_tests", targets=["tests/test_core.py"])

        assert not out.is_error
        assert h.events == ["checkpoint: checkpoint: before test probe", "run_subset"]

    async def test_the_targets_reach_the_sandbox_unchanged_and_without_a_timeout(self, tmp_path):
        h = make_harness(tmp_path)

        await h.call("run_tests", targets=["tests/test_core.py::test_add", "tests"])

        assert h.subset_calls == [["tests/test_core.py::test_add", "tests"]]

    @pytest.mark.parametrize(
        ("target", "reaches_the_sandbox_as"),
        [
            ("tests/test_core.py", "tests/test_core.py"),
            ("tests/test_core.py::test_add", "tests/test_core.py::test_add"),
            ("tests/test_core.py::test_add[param-1]", "tests/test_core.py::test_add[param-1]"),
            ("tests/test_core.py::TestX::test_y[a,b=c@d+e]", "tests/test_core.py::TestX::test_y[a,b=c@d+e]"),
            ("tests", "tests"),
            ("tests/", "tests"),
            (".", "."),
            ("./tests/test_core.py::test_add", "tests/test_core.py::test_add"),
            ("tests/./test_core.py", "tests/test_core.py"),
        ],
    )
    async def test_a_plain_target_is_accepted_and_reaches_the_sandbox_normalised(self, tmp_path, target, reaches_the_sandbox_as):
        h = make_harness(tmp_path)

        out = await h.call("run_tests", targets=[target])

        assert not out.is_error, out.content
        assert h.subset_calls == [[reaches_the_sandbox_as]]

    @pytest.mark.parametrize("target", TARGET_REFUSALS)
    async def test_a_hostile_target_is_refused_and_nothing_runs(self, tmp_path, target):
        h = make_harness(tmp_path)

        out = await h.call("run_tests", targets=["tests/test_core.py", target])

        assert out.is_error
        assert h.subset_calls == [] and h.events == []

    @pytest.mark.parametrize("name", ["--collect-only", "-x", "-pevil"])
    async def test_an_existing_file_named_like_an_option_is_still_refused_as_a_target(self, tmp_path, name):
        # The existence check alone would pass this: the file is really there.
        h = make_harness(tmp_path)
        (h.checkout / name).write_text("x\n")

        out = await h.call("run_tests", targets=[name])

        assert out.is_error and "starts with '-'" in out.content
        assert h.subset_calls == [] and h.events == []

    async def test_an_argfile_target_is_refused_even_when_the_file_exists(self, tmp_path):
        # pytest expands `@file` from a file the agent can write, which injects -o, -W, --junitxml=...
        h = make_harness(tmp_path)
        (h.checkout / "a.txt").write_text("-o\naddopts=-p evil\n")
        (h.checkout / "@a.txt").write_text("-v\n")  # lexists('@a.txt') is true: only the character rule stops it

        out = await h.call("run_tests", targets=["@a.txt"])

        assert out.is_error and "must start with a letter, digit, '.' or '_'" in out.content
        assert h.subset_calls == [] and h.events == []

    @pytest.mark.parametrize("target", ["./-x", "./@a.txt", "tests/../x", "./--collect-only::t"])
    async def test_a_target_that_only_becomes_an_option_once_normalised_is_refused(self, tmp_path, target):
        # `./-x` passes the character check as typed; as `-x` it would reach pytest as an option.
        h = make_harness(tmp_path)
        for name in ("-x", "@a.txt", "--collect-only"):
            (h.checkout / name).write_text("x\n")

        out = await h.call("run_tests", targets=[target])

        assert out.is_error
        assert h.subset_calls == [] and h.events == []

    @pytest.mark.parametrize("target", ["-p evil", "--rootdir=/", "-x"])
    async def test_an_option_is_refused_with_the_reason(self, tmp_path, target):
        out = await make_harness(tmp_path).call("run_tests", targets=[target])

        assert "starts with '-'" in out.content and "option" in out.content

    async def test_a_symlinked_target_is_refused(self, tmp_path):
        import os

        h = make_harness(tmp_path)
        os.symlink(tmp_path / "workspace", h.checkout / "tests/linked")

        assert (await h.call("run_tests", targets=["tests/linked"])).is_error
        assert h.subset_calls == []

    @pytest.mark.parametrize(
        ("targets", "message"),
        [([], "at least 1"), (["tests"] * 21, "at most 20"), ([1], "must be string"), (["x" * 301], "at most 300"), ([""], "at least 1"), ("tests", "must be array")],
    )
    async def test_the_schema_bounds_hold(self, tmp_path, targets, message):
        h = make_harness(tmp_path)

        out = await h.call("run_tests", targets=targets)

        assert out.is_error and message in out.content
        assert h.subset_calls == []

    async def test_it_reports_counts_failing_ids_and_the_output_tail(self, tmp_path):
        result = SuiteResult(
            passed=("t::a", "t::b"),
            failed=("tests/test_x.py::test_one", "tests/test_x.py::test_two"),
            skipped=("t::s",),
            xfailed=("t::x",),
            collect_failures=("tests/test_broken.py",),
            exit_code=1,
            stdout_tail="E   AssertionError: 1 != 2\n=== 2 failed ===",
        )
        h = make_harness(tmp_path, subset_results=[result])

        out = await h.call("run_tests", targets=["tests"])

        assert out.content.splitlines()[0] == (
            "2 passed, 2 failed, 1 skipped, 1 xfailed, 0 did not run, 1 collection error(s) (pytest exit code 1)"
        )
        assert "failed (2):\n  tests/test_x.py::test_one\n  tests/test_x.py::test_two" in out.content
        assert "collection errors (1):\n  tests/test_broken.py" in out.content
        assert out.content.endswith("E   AssertionError: 1 != 2\n=== 2 failed ===")

    async def test_at_most_fifty_failing_ids_are_listed(self, tmp_path):
        failed = tuple(f"tests/test_x.py::test_{n:03}" for n in range(80))
        h = make_harness(tmp_path, limits=ToolLimits(max_output_chars=100_000), subset_results=[SuiteResult(failed=failed)])

        out = await h.call("run_tests", targets=["tests"])

        assert out.content.count("tests/test_x.py::test_") == 50
        assert "... and 30 more" in out.content

    async def test_a_long_tail_and_many_ids_still_fit_with_the_end_of_the_output_intact(self, tmp_path):
        failed = tuple(f"tests/test_x.py::test_{'n' * 100}_{n:03}" for n in range(50))
        result = SuiteResult(failed=failed, collect_failures=failed, stdout_tail="o" * 20_000 + "\nFINAL LINE")
        h = make_harness(tmp_path, subset_results=[result])

        out = await h.call("run_tests", targets=["tests"])

        assert len(out.content) <= 8000 and "[truncated" not in out.content
        assert out.content.endswith("FINAL LINE")
        assert "more" in out.content  # the id lists were shortened to make room

    async def test_the_output_tail_is_at_most_six_thousand_characters(self, tmp_path):
        h = make_harness(tmp_path, limits=ToolLimits(max_output_chars=100_000), subset_results=[SuiteResult(stdout_tail="Z" * 20_000)])

        out = await h.call("run_tests", targets=["tests"])

        assert 5000 < out.content.count("Z") <= 6000

    async def test_no_sandbox_is_an_error_and_nothing_is_committed(self, tmp_path):
        h = make_harness(tmp_path, run_subset=None)

        out = await h.call("run_tests", targets=["tests"])

        assert out.is_error and out.content.startswith("the sandbox is unavailable:")
        assert h.events == []

    async def test_a_probe_that_errored_is_an_error_result_not_a_crash(self, tmp_path):
        h = make_harness(tmp_path, subset_results=[SuiteResult(error="sandbox exceeded 300.0s running pytest...")])

        out = await h.call("run_tests", targets=["tests"])

        assert out.is_error and out.content.startswith("the sandbox is unavailable: sandbox exceeded")
        assert "narrower targets" in out.content

    async def test_a_verifier_that_is_not_ready_is_an_error_not_a_crash(self, tmp_path):
        h = make_harness(tmp_path, subset_results=[VerifierNotReady("baseline has not run")])

        out = await h.call("run_tests", targets=["tests"])

        assert out.is_error and out.content.startswith("the sandbox is unavailable:")

    async def test_anything_else_the_sandbox_raises_is_a_bug_and_propagates(self, tmp_path):
        h = make_harness(tmp_path, subset_results=[RuntimeError("a bug")])

        with pytest.raises(RuntimeError, match="a bug"):
            await h.call("run_tests", targets=["tests"])

    async def test_a_forgotten_checkpoint_would_be_caught_by_the_fake(self, tmp_path):
        # Proves the harness itself: without `checkpoint`, the dirty tree refuses export.
        h = make_harness(tmp_path)
        h.workspace.set_dirty(True)

        with pytest.raises(RuntimeError, match="refusing to export"):
            await h.ctx.run_subset(["tests"])


class TestTheProbeRendersOnlyWhatTheVerifierReturned:
    """`run_tests` prints failed ids, collection errors and the output tail verbatim, which is
    safe only because a probe never includes the hidden overlay. This pins the shape of that
    guarantee: the sandbox is called with the targets alone, on a tree without the overlay, and
    the rendering is a function of the one `SuiteResult` that came back."""

    HIDDEN = "tests/test_hidden.py::test_the_oracle"

    async def test_the_output_is_built_from_the_probe_result_alone(self, tmp_path):
        probe = SuiteResult(
            passed=("tests/test_core.py::test_ok",),
            failed=("tests/test_core.py::test_add",),
            collect_failures=("tests/test_broken.py",),
            stdout_tail="E   assert 3 == 4",
        )
        backend = FakeBackend(results=[probe])
        baseline = SuiteResult(failed=(self.HIDDEN,), stdout_tail=f"FAILED {self.HIDDEN}")  # oracle-bearing, never an input here

        h = make_harness(tmp_path)

        async def run_subset(targets):
            # Mirrors `Verifier.run_subset`: export the tree, give the backend the targets
            # through the spec, and return its result. No overlay is applied on this path.
            source = await h.workspace.export_tree("probe-1")
            results = await h.workspace.results_dir("probe-1")
            spec = dataclasses.replace(RepoSpec(key="x"), test_targets=tuple(targets))
            return await backend.run_tests(EnvironmentRef("fake", "img"), source, results, spec, container_name="probe-1")

        box = build_toolbox(dataclasses.replace(h.ctx, run_subset=run_subset))
        out = await box.dispatch(FakeToolCall("run_tests", {"targets": ["tests/test_core.py"]}))

        assert not out.is_error
        assert backend.runs[0]["spec"].test_targets == ("tests/test_core.py",)
        assert "tests/test_hidden.py" not in backend.runs[0]["snapshot"]
        for fragment in ("tests/test_core.py::test_add", "tests/test_broken.py", "E   assert 3 == 4"):
            assert fragment in out.content
        assert self.HIDDEN not in out.content and baseline.stdout_tail not in out.content
