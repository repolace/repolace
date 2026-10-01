"""The shared fakes, tested as the real thing they stand in for.

A fake is a second implementation of a contract, and the failure mode of a fake
is quiet: it drifts from the Protocol and every test built on it keeps passing
against a seam the production code does not have. So some of these tests compare
the fake's signatures against the Protocol and against `DockerBackend`, and the
rest pin the behaviours other packages' tests will lean on without re-reading
this file.
"""

import dataclasses
import inspect
from pathlib import Path

import pytest

from repolace_shared.git.workspace import TaskWorkspace
from verify.backends.docker import DockerBackend
from verify.errors import EnvironmentBuildFailed, SandboxUnavailable
from verify.protocol import EnvironmentRef, RepoSpec, SandboxBackend, ScriptResult, SuiteResult
from verify.stage import Workspace
from verify.testing import DEFAULT_SUITE_RESULT, EXPORT_REFUSAL, FakeBackend, FakeWorkspace

pytestmark = pytest.mark.anyio

SPEC = RepoSpec(key="a/b")
ENV = EnvironmentRef("fake", "image-k")


async def run_tests(backend: FakeBackend, tmp_path: Path, name: str = "c") -> SuiteResult:
    return await backend.run_tests(ENV, tmp_path, tmp_path, SPEC, container_name=name)


async def run_script(backend: FakeBackend, tmp_path: Path, name: str = "c") -> ScriptResult:
    return await backend.run_script(
        ENV, tmp_path, tmp_path / "s.py", SPEC, container_name=name, timeout_seconds=5.0
    )


def parameter_names(function) -> list[str]:
    return list(inspect.signature(function).parameters)


#: What each record carried before it became a snapshot too. Additive only.
ORIGINAL_RUN_KEYS = ("image", "source", "results", "container")
ORIGINAL_SCRIPT_KEYS = ("image", "source", "script", "container", "timeout")


def original(record: dict, keys: tuple[str, ...]) -> dict:
    return {key: record[key] for key in keys}


def make_tree(root: Path, files: dict[str, bytes]) -> Path:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


class TestSignaturesMatchTheContract:
    @pytest.mark.parametrize("method", ["prepare", "run_tests", "run_script"])
    @pytest.mark.parametrize("implementation", [FakeBackend, DockerBackend])
    def test_a_backend_has_the_protocols_parameters(self, implementation, method):
        assert parameter_names(getattr(implementation, method)) == parameter_names(
            getattr(SandboxBackend, method)
        )

    @pytest.mark.parametrize("method", ["export_tree", "results_dir", "discard"])
    def test_the_fake_workspace_has_the_protocols_parameters(self, method):
        assert parameter_names(getattr(FakeWorkspace, method)) == parameter_names(
            getattr(Workspace, method)
        )

    def test_the_fakes_checkpoint_takes_what_the_real_ones_does(self):
        """It is passed as `ToolContext.checkpoint`, so a different signature would
        only fail against the real workspace."""
        assert parameter_names(FakeWorkspace.record_attempt) == parameter_names(TaskWorkspace.record_attempt)


class TestFakeBackend:
    async def test_prepare_records_the_cache_key_and_names_the_image_after_it(self, tmp_path):
        backend = FakeBackend()

        env = await backend.prepare(SPEC, tmp_path, "abc123")

        assert backend.prepared == ["abc123"]
        assert env == EnvironmentRef("fake", "image-abc123")

    async def test_with_nothing_scripted_every_run_gets_the_default(self, tmp_path):
        backend = FakeBackend()

        first = await run_tests(backend, tmp_path)
        second = await run_tests(backend, tmp_path)

        assert first == second == DEFAULT_SUITE_RESULT == SuiteResult(passed=("t::a",))

    async def test_scripted_results_come_back_in_order(self, tmp_path):
        one, two = SuiteResult(passed=("a",)), SuiteResult(failed=("b",))
        backend = FakeBackend([one, two])

        assert await run_tests(backend, tmp_path) is one
        assert await run_tests(backend, tmp_path) is two

    async def test_a_generator_of_results_is_accepted(self, tmp_path):
        backend = FakeBackend(SuiteResult(passed=(str(i),)) for i in range(2))

        assert (await run_tests(backend, tmp_path)).passed == ("0",)
        assert (await run_tests(backend, tmp_path)).passed == ("1",)

    async def test_running_past_the_script_is_an_error_not_a_repeat(self, tmp_path):
        """A test that triggers one run more than it planned for has found a bug."""
        backend = FakeBackend([SuiteResult(passed=("a",))])
        await run_tests(backend, tmp_path)

        with pytest.raises(AssertionError, match="more runs than scripted"):
            await run_tests(backend, tmp_path)

    async def test_runs_are_recorded_with_what_the_stage_passed(self, tmp_path):
        backend = FakeBackend()
        source, results = tmp_path / "src", tmp_path / "res"

        await backend.run_tests(ENV, source, results, SPEC, container_name="repolace-x-1")

        # The four keys tests have always relied on, each unchanged. A projection
        # rather than `==` on the whole dict, because the record is additive.
        assert [original(run, ORIGINAL_RUN_KEYS) for run in backend.runs] == [
            {"image": "image-k", "source": source, "results": results, "container": "repolace-x-1"}
        ]

    async def test_with_no_scripts_scripted_every_script_exits_cleanly(self, tmp_path):
        backend = FakeBackend()

        assert await run_script(backend, tmp_path) == ScriptResult(exit_code=0)

    async def test_scripted_script_results_come_back_in_order(self, tmp_path):
        boom = ScriptResult(exit_code=1, stderr="Traceback")
        timeout = ScriptResult(exit_code=None, timed_out=True)
        backend = FakeBackend(scripts=[boom, timeout])

        assert await run_script(backend, tmp_path) is boom
        assert await run_script(backend, tmp_path) is timeout

    async def test_running_past_the_scripted_scripts_is_an_error(self, tmp_path):
        backend = FakeBackend(scripts=[ScriptResult(exit_code=0)])
        await run_script(backend, tmp_path)

        with pytest.raises(AssertionError, match="more scripts than scripted"):
            await run_script(backend, tmp_path)

    async def test_scripting_one_kind_does_not_script_the_other(self, tmp_path):
        backend = FakeBackend(scripts=[ScriptResult(exit_code=3)])

        assert await run_tests(backend, tmp_path) == DEFAULT_SUITE_RESULT
        assert await run_tests(backend, tmp_path) == DEFAULT_SUITE_RESULT

    async def test_scripts_are_recorded_separately_from_runs(self, tmp_path):
        backend = FakeBackend()

        await run_script(backend, tmp_path, "repolace-x-script-1")

        assert backend.runs == []
        assert [original(script, ORIGINAL_SCRIPT_KEYS) for script in backend.scripts] == [
            {
                "image": "image-k",
                "source": tmp_path,
                "script": tmp_path / "s.py",
                "container": "repolace-x-script-1",
                "timeout": 5.0,
            }
        ]


class TestFakeWorkspace:
    async def test_each_label_gets_its_own_export_and_results_directory(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)

        exports = {await workspace.export_tree(label) for label in (0, 1, "probe-1")}
        results = {await workspace.results_dir(label) for label in (0, 1, "probe-1")}

        assert len(exports) == 3 and len(results) == 3
        assert (tmp_path / "export-probe-1").is_dir() and (tmp_path / "results-probe-1").is_dir()

    async def test_the_export_looks_like_a_tree(self, tmp_path):
        export = await FakeWorkspace(tmp_path).export_tree(0)

        assert (export / "pyproject.toml").is_file()

    async def test_exports_are_recorded_in_order(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)

        await workspace.export_tree(0)
        await workspace.export_tree("probe-1")

        assert workspace.exported == [0, "probe-1"]

    async def test_discard_deletes_both_directories_and_is_recorded(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)
        export = await workspace.export_tree("probe-1")
        results = await workspace.results_dir("probe-1")
        (results / "report.jsonl").write_text("x")

        await workspace.discard("probe-1")

        assert not export.exists() and not results.exists()
        assert workspace.discarded == ["probe-1"]

    async def test_discard_leaves_other_labels_alone(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)
        kept = await workspace.export_tree(1)
        await workspace.export_tree("probe-1")

        await workspace.discard("probe-1")

        assert kept.is_dir()

    async def test_discarding_something_never_exported_is_not_an_error(self, tmp_path):
        """Best effort, as the Protocol says: it runs on a path that may be failing."""
        workspace = FakeWorkspace(tmp_path)

        await workspace.discard("probe-9")

        assert workspace.discarded == ["probe-9"]

    async def test_a_discarded_label_exports_fresh(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)
        export = await workspace.export_tree("probe-1")
        (export / "leftover.txt").write_text("stale")
        await workspace.discard("probe-1")

        again = await workspace.export_tree("probe-1")

        assert not (again / "leftover.txt").exists()


class TestTheBackendRecordsEvidenceAtCallTime:
    """`Verifier.run_subset` discards its directories in a `finally`, so by the time a
    test looks the files are gone. The fake has to have looked first."""

    async def test_a_run_records_the_source_tree_as_it_was(self, tmp_path):
        source = make_tree(tmp_path / "src", {"pkg/a.py": b"A", "tests/test_a.py": b"def test(): pass\n", "x.bin": b"\x00\xff"})
        backend = FakeBackend()

        await backend.run_tests(ENV, source, tmp_path / "res", SPEC, container_name="c")

        assert backend.runs[0]["snapshot"] == {
            "pkg/a.py": b"A",
            "tests/test_a.py": b"def test(): pass\n",
            "x.bin": b"\x00\xff",
        }

    async def test_the_snapshot_survives_the_directory_being_deleted_afterwards(self, tmp_path):
        """The reason it exists."""
        import shutil

        source = make_tree(tmp_path / "src", {"a.py": b"A"})
        backend = FakeBackend()
        await backend.run_tests(ENV, source, tmp_path, SPEC, container_name="c")

        shutil.rmtree(source)

        assert backend.runs[0]["snapshot"] == {"a.py": b"A"}

    async def test_a_later_edit_does_not_change_an_earlier_snapshot(self, tmp_path):
        source = make_tree(tmp_path / "src", {"a.py": b"one"})
        backend = FakeBackend()
        await backend.run_tests(ENV, source, tmp_path, SPEC, container_name="c1")
        (source / "a.py").write_bytes(b"two")
        (source / "b.py").write_bytes(b"new")
        await backend.run_tests(ENV, source, tmp_path, SPEC, container_name="c2")

        assert backend.runs[0]["snapshot"] == {"a.py": b"one"}
        assert backend.runs[1]["snapshot"] == {"a.py": b"two", "b.py": b"new"}

    async def test_a_directory_that_does_not_exist_is_an_empty_snapshot(self, tmp_path):
        backend = FakeBackend()

        await backend.run_tests(ENV, tmp_path / "nowhere", tmp_path, SPEC, container_name="c")

        assert backend.runs[0]["snapshot"] == {}

    async def test_a_symlink_is_not_followed_out_of_the_tree(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        source = make_tree(tmp_path / "src", {"a.py": b"A"})
        (source / "link.txt").symlink_to(outside)
        backend = FakeBackend()

        await backend.run_tests(ENV, source, tmp_path, SPEC, container_name="c")

        assert backend.runs[0]["snapshot"] == {"a.py": b"A"}

    async def test_snapshotting_can_be_turned_off_for_a_large_tree(self, tmp_path):
        source = make_tree(tmp_path / "src", {"a.py": b"A"})
        backend = FakeBackend(snapshot=False)

        await backend.run_tests(ENV, source, tmp_path, SPEC, container_name="c")
        await run_script(backend, tmp_path)

        assert backend.runs[0]["snapshot"] == {} and backend.scripts[0]["snapshot"] == {}

    async def test_a_run_records_the_spec_it_was_given(self, tmp_path):
        """So `test_targets` and `timeout_seconds` -- which reach the backend only
        through the spec -- are assertable."""
        spec = dataclasses.replace(SPEC, test_targets=("tests/test_a.py::t",), timeout_seconds=30.0)
        backend = FakeBackend()

        await backend.run_tests(ENV, tmp_path, tmp_path, spec, container_name="c")

        assert backend.runs[0]["spec"] is spec
        assert backend.runs[0]["spec"].test_targets == ("tests/test_a.py::t",)
        assert backend.runs[0]["spec"].timeout_seconds == 30.0

    async def test_a_script_records_its_text_snapshot_spec_and_timeout(self, tmp_path):
        source = make_tree(tmp_path / "src", {"pkg/a.py": b"A"})
        script = tmp_path / "main.py"
        script.write_text("print('hello')\n")
        spec = dataclasses.replace(SPEC, timeout_seconds=9.0)
        backend = FakeBackend()

        await backend.run_script(ENV, source, script, spec, container_name="c", timeout_seconds=7.5)

        record = backend.scripts[0]
        assert record["script_text"] == "print('hello')\n"
        assert record["snapshot"] == {"pkg/a.py": b"A"}
        assert record["spec"] is spec
        assert record["timeout"] == 7.5

    async def test_the_script_is_not_in_the_source_snapshot_when_it_lives_outside_it(self, tmp_path):
        """What a stream proves with it: the script never lands in the tree the host exports."""
        source = make_tree(tmp_path / "src", {"a.py": b"A"})
        script = tmp_path / "scratch" / "main.py"
        script.parent.mkdir()
        script.write_text("print(1)")
        backend = FakeBackend()

        await backend.run_script(ENV, source, script, SPEC, container_name="c", timeout_seconds=5.0)

        assert "main.py" not in backend.scripts[0]["snapshot"]
        assert backend.scripts[0]["script_text"] == "print(1)"

    async def test_the_script_text_survives_the_file_being_deleted(self, tmp_path):
        script = tmp_path / "main.py"
        script.write_text("print(2)")
        backend = FakeBackend()
        await backend.run_script(ENV, tmp_path, script, SPEC, container_name="c", timeout_seconds=5.0)

        script.unlink()

        assert backend.scripts[0]["script_text"] == "print(2)"

    async def test_a_script_file_that_does_not_exist_has_no_text(self, tmp_path):
        backend = FakeBackend()

        await backend.run_script(ENV, tmp_path, tmp_path / "missing.py", SPEC, container_name="c", timeout_seconds=5.0)

        assert backend.scripts[0]["script_text"] is None

    async def test_prepare_records_the_tree_the_image_was_built_from(self, tmp_path):
        """What proves the overlay is applied AFTER the build: the image's source must
        not contain it."""
        source = make_tree(tmp_path / "src", {"pyproject.toml": b"[project]"})
        spec = dataclasses.replace(SPEC, install=("pip install .",))
        backend = FakeBackend()

        await backend.prepare(spec, source, "key1")

        assert backend.prepared == ["key1"]
        assert backend.prepare_calls == [
            {"cache_key": "key1", "source": source, "snapshot": {"pyproject.toml": b"[project]"}, "spec": spec}
        ]

    async def test_every_original_key_is_still_recorded(self, tmp_path):
        """Additive only: tests elsewhere index into these by name."""
        backend = FakeBackend()
        await run_tests(backend, tmp_path)
        await run_script(backend, tmp_path)

        assert set(ORIGINAL_RUN_KEYS) <= set(backend.runs[0])
        assert set(ORIGINAL_SCRIPT_KEYS) <= set(backend.scripts[0])


class TestTheBackendCanFail:
    """The real backend raises `EnvironmentBuildFailed` and `SandboxUnavailable`, and
    nothing else lets a test see what its caller does with them."""

    async def test_a_scripted_exception_is_raised_when_it_is_reached(self, tmp_path):
        boom = SandboxUnavailable("docker is not running")
        ok = SuiteResult(passed=("a",))
        backend = FakeBackend([ok, boom, ok])

        assert await run_tests(backend, tmp_path) is ok
        with pytest.raises(SandboxUnavailable, match="docker is not running") as raised:
            await run_tests(backend, tmp_path)
        assert raised.value is boom
        assert await run_tests(backend, tmp_path) is ok

    async def test_the_failing_call_is_still_recorded(self, tmp_path):
        backend = FakeBackend([SandboxUnavailable("down")])

        with pytest.raises(SandboxUnavailable):
            await run_tests(backend, tmp_path, "the-container")

        assert [run["container"] for run in backend.runs] == ["the-container"]

    async def test_a_script_can_fail_too(self, tmp_path):
        backend = FakeBackend(scripts=[ScriptResult(exit_code=0), SandboxUnavailable("down")])

        await run_script(backend, tmp_path)
        with pytest.raises(SandboxUnavailable):
            await run_script(backend, tmp_path)

    async def test_an_exception_counts_as_scripted_not_as_nothing_scripted(self, tmp_path):
        backend = FakeBackend([SandboxUnavailable("down")])
        with pytest.raises(SandboxUnavailable):
            await run_tests(backend, tmp_path)

        with pytest.raises(AssertionError, match="more runs than scripted"):
            await run_tests(backend, tmp_path)

    async def test_a_base_exception_instance_is_raised_too(self, tmp_path):
        class Cancelled(BaseException):
            pass

        backend = FakeBackend([Cancelled()])

        with pytest.raises(Cancelled):
            await run_tests(backend, tmp_path)

    async def test_prepare_error_is_raised_by_prepare_after_recording_the_attempt(self, tmp_path):
        failure = EnvironmentBuildFailed("a/b", 1, "pip exploded")
        backend = FakeBackend(prepare_error=failure)

        with pytest.raises(EnvironmentBuildFailed, match="pip exploded") as raised:
            await backend.prepare(SPEC, tmp_path, "k")

        assert raised.value is failure
        assert backend.prepared == ["k"], "the build was attempted, and a test can see that"

    async def test_prepare_error_is_raised_every_time(self, tmp_path):
        backend = FakeBackend(prepare_error=SandboxUnavailable("down"))

        for _ in range(2):
            with pytest.raises(SandboxUnavailable):
                await backend.prepare(SPEC, tmp_path, "k")

    async def test_without_it_prepare_succeeds(self, tmp_path):
        assert (await FakeBackend().prepare(SPEC, tmp_path, "k")) == EnvironmentRef("fake", "image-k")

    def test_the_new_arguments_are_keyword_only(self):
        """The first two positionals have always been the scripted results and scripts."""
        with pytest.raises(TypeError):
            FakeBackend([], [], SandboxUnavailable("x"))  # type: ignore[misc]


class TestTheWorkspaceCanBeDirty:
    """The real `export_tree` refuses a tree that differs from HEAD. A tool that
    forgets to checkpoint first must fail its tests, not its first real task."""

    async def test_a_dirty_tree_refuses_to_export(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)
        workspace.set_dirty(True)

        with pytest.raises(RuntimeError, match="refusing to export: the working tree does not match HEAD"):
            await workspace.export_tree("probe-1")

    async def test_a_refused_export_leaves_nothing_behind_and_is_not_recorded(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)
        workspace.set_dirty(True)

        with pytest.raises(RuntimeError):
            await workspace.export_tree(1)

        assert workspace.exported == []
        assert not (tmp_path / "export-1").exists()

    async def test_the_message_is_the_real_ones(self):
        """A tool whose test asserts on the refusal must read the same on the real
        workspace. Compared with the real source rather than copied by hand."""
        first_clause = "refusing to export: the working tree does not match HEAD"

        assert first_clause in inspect.getsource(TaskWorkspace.export_tree)
        assert EXPORT_REFUSAL.startswith(first_clause)

    async def test_a_new_workspace_is_clean(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)

        assert workspace.dirty is False
        await workspace.export_tree(0)

    async def test_it_can_start_dirty(self, tmp_path):
        with pytest.raises(RuntimeError):
            await FakeWorkspace(tmp_path, dirty=True).export_tree(0)

    async def test_setting_it_clean_again_allows_the_export(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)
        workspace.set_dirty(True)
        workspace.set_dirty(False)

        await workspace.export_tree(0)

    async def test_set_dirty_defaults_to_true(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)

        workspace.set_dirty()

        assert workspace.dirty is True

    async def test_a_checkpoint_clears_it_and_returns_a_sha(self, tmp_path):
        workspace = FakeWorkspace(tmp_path)
        workspace.set_dirty(True)

        sha = await workspace.record_attempt("run_python checkpoint")

        assert sha is not None and workspace.dirty is False
        assert workspace.checkpoints == ["run_python checkpoint"]
        await workspace.export_tree("script-1")

    async def test_a_checkpoint_of_a_clean_tree_returns_none_like_the_real_one(self, tmp_path):
        """"None when nothing changed": the tool must not read it as a failure."""
        assert await FakeWorkspace(tmp_path).record_attempt("nothing to commit") is None

    async def test_a_tool_that_forgets_to_checkpoint_fails(self, tmp_path):
        """The scenario the whole mechanism is for, spelled out."""
        workspace = FakeWorkspace(tmp_path)

        async def careless_run_python() -> Path:
            workspace.set_dirty(True)  # the agent edited a file
            return await workspace.export_tree("script-1")  # ...and the tool went straight to the sandbox

        with pytest.raises(RuntimeError, match="refusing to export"):
            await careless_run_python()

    async def test_results_and_discard_do_not_care_about_dirtiness(self, tmp_path):
        """Like the real workspace: only the export compares the tree with HEAD."""
        workspace = FakeWorkspace(tmp_path)
        workspace.set_dirty(True)

        assert (await workspace.results_dir("probe-1")).is_dir()
        await workspace.discard("probe-1")
