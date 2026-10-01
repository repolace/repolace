"""The shared fakes, tested as the real thing they stand in for.

A fake is a second implementation of a contract, and the failure mode of a fake
is quiet: it drifts from the Protocol and every test built on it keeps passing
against a seam the production code does not have. So some of these tests compare
the fake's signatures against the Protocol and against `DockerBackend`, and the
rest pin the behaviours other packages' tests will lean on without re-reading
this file.
"""

import inspect
from pathlib import Path

import pytest

from verify.backends.docker import DockerBackend
from verify.protocol import EnvironmentRef, RepoSpec, SandboxBackend, ScriptResult, SuiteResult
from verify.stage import Workspace
from verify.testing import DEFAULT_SUITE_RESULT, FakeBackend, FakeWorkspace

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

        assert backend.runs == [
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
        assert backend.scripts == [
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
