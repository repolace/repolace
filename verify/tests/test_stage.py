"""The Verify stage's ordering rules.

One rule carries the whole measurement: **the environment is built once, from
the base commit, and reused for every attempt.** If an attempt could rebuild it,
a suite that started failing might be the patch's doing or might be a dependency
that resolved differently, and `score` has no way to tell those apart -- which
is the exact ambiguity the baseline exists to remove.
"""

import asyncio
import re
import stat
import uuid

import pytest

from verify.errors import EnvironmentBuildFailed, SandboxUnavailable
from verify.overlay import OverlayError
from verify.protocol import RepoSpec, ScriptResult, SuiteResult
from verify.stage import BASELINE_ATTEMPT, Verifier, VerifierNotReady, container_name
from verify.testing import FakeBackend, FakeWorkspace

pytestmark = pytest.mark.anyio

HIDDEN = {"tests/test_hidden.py": b"def test_it():\n    assert False\n", "tests/data/x.json": b"{}"}


@pytest.fixture
def workspace(tmp_path):
    return FakeWorkspace(tmp_path)


class TestEnvironmentReuse:
    async def test_the_image_is_built_once_and_reused(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert len(verifier.backend.prepared) == 1

    async def test_both_attempts_run_in_the_same_image(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert len({run["image"] for run in verifier.backend.runs}) == 1

    async def test_prepared_reports_whether_an_environment_exists(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())
        assert not verifier.prepared

        await verifier.run(workspace, BASELINE_ATTEMPT)

        assert verifier.prepared


class TestPerAttemptIsolation:
    async def test_each_attempt_gets_its_own_export_and_results_directory(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert workspace.exported == [BASELINE_ATTEMPT, 1]
        assert len({run["source"] for run in verifier.backend.runs}) == 2
        assert len({run["results"] for run in verifier.backend.runs}) == 2

    async def test_each_attempt_gets_its_own_container_name(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert len({run["container"] for run in verifier.backend.runs}) == 2

    async def test_a_second_baseline_is_refused(self, workspace):
        """It would mean the caller looped in a way that invalidates every
        comparison downstream of it."""
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())
        await verifier.run(workspace, BASELINE_ATTEMPT)

        with pytest.raises(RuntimeError, match="baseline"):
            await verifier.run(workspace, BASELINE_ATTEMPT)


class TestContainerName:
    def test_two_tasks_cannot_collide(self):
        """Collision matters more than it looks: the timeout path removes the
        container by name, so a shared name has one task killing another's suite."""
        a, b = uuid.uuid4(), uuid.uuid4()

        assert container_name(a, 0) != container_name(b, 0)

    def test_it_is_a_legal_docker_name(self):
        import re

        name = container_name(uuid.uuid4(), 1)

        assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name)


async def ready(workspace, **kwargs) -> Verifier:
    """A Verifier whose baseline has run, which is what a probe or a script needs."""
    verifier = Verifier(kwargs.pop("backend", None) or FakeBackend(), kwargs.pop("spec", RepoSpec(key="a/b")), uuid.uuid4(), **kwargs)
    await verifier.run(workspace, BASELINE_ATTEMPT)
    return verifier


class TestTheOverlay:
    """Applied to scored runs only, and only after the image is built from a clean export."""

    async def test_the_environment_is_prepared_from_a_tree_without_it(self, workspace):
        """If the hidden tests were in the export the image is built from, they
        would be baked into a layer the cache shares between tasks."""
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), overlay=HIDDEN)

        await verifier.run(workspace, BASELINE_ATTEMPT)

        prepared_from = verifier.backend.prepare_calls[0]["snapshot"]
        assert set(prepared_from).isdisjoint(HIDDEN)
        assert "pyproject.toml" in prepared_from

    async def test_the_suite_runs_against_a_tree_with_it(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), overlay=HIDDEN)

        await verifier.run(workspace, BASELINE_ATTEMPT)

        ran_against = verifier.backend.runs[0]["snapshot"]
        assert {path: ran_against[path] for path in HIDDEN} == HIDDEN
        assert "pyproject.toml" in ran_against

    async def test_every_scored_run_carries_it_not_only_the_baseline(self, workspace):
        """Baseline and attempts must see the same tests, or fail-to-pass compares
        two different suites."""
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), overlay=HIDDEN)

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)
        await verifier.run(workspace, 2)

        assert len(verifier.backend.runs) == 3
        for run in verifier.backend.runs:
            assert set(HIDDEN) <= set(run["snapshot"])

    async def test_the_image_is_still_built_once(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), overlay=HIDDEN)

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert len(verifier.backend.prepared) == 1

    async def test_the_cache_key_is_computed_before_the_overlay_lands(self, tmp_path):
        """Hidden tests are not dependency manifests, so an ordering mistake would
        not move the key -- unless the overlay holds one. It does here, deliberately
        (instance preparation excludes such instances; this is the probe for the
        order): a key that differs means it was computed from the overlaid tree."""
        keys = []
        for index, overlay in enumerate((None, {"requirements.txt": b"evil-package==1\n"})):
            root = tmp_path / str(index)
            root.mkdir()
            verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), overlay=overlay)
            await verifier.run(FakeWorkspace(root), BASELINE_ATTEMPT)
            keys.append(verifier.backend.prepared[0])

        assert keys[0] == keys[1]

    async def test_without_an_overlay_the_export_is_untouched(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)

        assert set(verifier.backend.runs[0]["snapshot"]) == {"pyproject.toml"}

    async def test_a_refused_overlay_never_reaches_the_sandbox(self, workspace):
        """Running the suite without the hidden tests would score the task against
        the wrong tests -- a confidently wrong number, so it is an error."""
        verifier = Verifier(
            FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), overlay={"../escape.py": b"x"}
        )

        with pytest.raises(OverlayError, match="escape"):
            await verifier.run(workspace, BASELINE_ATTEMPT)

        assert verifier.backend.runs == []

    async def test_a_dirty_tree_is_refused_before_anything_is_prepared_or_overlaid(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4(), overlay=HIDDEN)
        workspace.set_dirty(True)

        with pytest.raises(RuntimeError, match="refusing to export"):
            await verifier.run(workspace, BASELINE_ATTEMPT)

        assert verifier.backend.prepared == [] and verifier.backend.runs == []


class TestRunSubset:
    async def test_it_needs_the_baseline_to_have_built_the_environment(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        with pytest.raises(VerifierNotReady):
            await verifier.run_subset(workspace, ["tests/test_a.py"])

        assert workspace.exported == [] and workspace.discarded == []
        assert verifier.backend.runs == [] and verifier.backend.prepared == []

    async def test_a_failed_baseline_build_leaves_it_not_ready(self, workspace):
        """The probe must not be what triggers a build from an already-edited tree."""
        backend = FakeBackend(prepare_error=EnvironmentBuildFailed("a/b", 1, "pip exploded"))
        verifier = Verifier(backend, RepoSpec(key="a/b"), uuid.uuid4())
        with pytest.raises(EnvironmentBuildFailed):
            await verifier.run(workspace, BASELINE_ATTEMPT)

        with pytest.raises(VerifierNotReady):
            await verifier.run_subset(workspace, ["tests/test_a.py"])

        assert len(backend.prepared) == 1  # the baseline's attempt, and nothing since

    async def test_labels_are_strings_and_never_repeat(self, workspace):
        verifier = await ready(workspace)

        for _ in range(4):
            await verifier.run_subset(workspace, ["tests/test_a.py"])

        probes = [label for label in workspace.exported if label != BASELINE_ATTEMPT]
        assert probes == ["probe-1", "probe-2", "probe-3", "probe-4"]
        assert all(isinstance(label, str) for label in probes)

    async def test_a_label_is_not_reused_even_though_the_previous_one_was_discarded(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["a"])
        assert not (workspace.root / "export-probe-1").exists()
        await verifier.run_subset(workspace, ["a"])

        assert workspace.exported[1:] == ["probe-1", "probe-2"]

    async def test_a_label_never_reads_as_an_attempt_number(self, workspace):
        verifier = await ready(workspace)
        await verifier.run_subset(workspace, ["a"])

        for label in workspace.exported[1:]:
            assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", label)

    async def test_it_never_builds_an_environment(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["a"])
        await verifier.run_subset(workspace, ["b"])

        assert len(verifier.backend.prepared) == 1

    async def test_it_runs_in_the_environment_the_baseline_built(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["a"])

        assert len({run["image"] for run in verifier.backend.runs}) == 1

    async def test_it_never_includes_the_overlay(self, workspace):
        """The overlay holds the hidden fail-to-pass tests. A probe that ran them
        would show the agent the oracle through the one tool built to show it test
        output."""
        verifier = await ready(workspace, overlay=HIDDEN)

        await verifier.run_subset(workspace, ["tests/test_hidden.py"])

        baseline_run, probe_run = verifier.backend.runs
        assert set(HIDDEN) <= set(baseline_run["snapshot"])  # the contrast: scored runs have it
        assert set(probe_run["snapshot"]).isdisjoint(HIDDEN)

    async def test_its_container_name_differs_from_every_attempts(self, workspace):
        verifier = await ready(workspace)
        await verifier.run(workspace, 1)

        await verifier.run_subset(workspace, ["a"])

        names = [run["container"] for run in verifier.backend.runs]
        assert len(set(names)) == 3
        assert names[2] == container_name(verifier.task_id, "probe-1")
        assert names[2].endswith("-probe-1")

    async def test_each_probe_gets_its_own_directories(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["a"])
        await verifier.run_subset(workspace, ["a"])

        _baseline, first, second = verifier.backend.runs
        assert first["source"] != second["source"] and first["results"] != second["results"]
        assert first["source"].name == "export-probe-1" and first["results"].name == "results-probe-1"

    async def test_targets_reach_the_backend_through_the_spec_and_nowhere_else(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["tests/test_a.py::test_one", "tests/test_b.py"])

        sent = verifier.backend.runs[-1]["spec"]
        assert sent.test_targets == ("tests/test_a.py::test_one", "tests/test_b.py")

    async def test_the_verifiers_own_spec_is_not_changed_by_a_probe(self, workspace):
        """A later scored run must still run what the spec says."""
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["tests/test_a.py"])
        await verifier.run(workspace, 1)

        assert verifier.spec.test_targets == ()
        assert verifier.backend.runs[-1]["spec"].test_targets == ()

    async def test_every_other_spec_field_is_carried_over(self, workspace):
        spec = RepoSpec(key="a/b", keep_addopts=True, extra_env={"TZ": "UTC"}, python_executable="python3")
        verifier = await ready(workspace, spec=spec)

        await verifier.run_subset(workspace, ["a"])

        sent = verifier.backend.runs[-1]["spec"]
        assert (sent.keep_addopts, dict(sent.extra_env), sent.python_executable, sent.key) == (
            True, {"TZ": "UTC"}, "python3", "a/b",
        )

    async def test_no_timeout_leaves_the_specs_own_in_force(self, workspace):
        verifier = await ready(workspace, spec=RepoSpec(key="a/b", timeout_seconds=900.0))

        await verifier.run_subset(workspace, ["a"])

        assert verifier.backend.runs[-1]["spec"].timeout_seconds == 900.0

    async def test_no_timeout_and_no_spec_timeout_leaves_the_backend_default(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["a"])

        assert verifier.backend.runs[-1]["spec"].timeout_seconds is None

    async def test_a_timeout_overrides_the_specs_for_this_call_only(self, workspace):
        verifier = await ready(workspace, spec=RepoSpec(key="a/b", timeout_seconds=900.0))

        await verifier.run_subset(workspace, ["a"], timeout_seconds=30.0)
        await verifier.run_subset(workspace, ["a"])

        assert [run["spec"].timeout_seconds for run in verifier.backend.runs[1:]] == [30.0, 900.0]

    async def test_the_backends_result_comes_back_unchanged(self, workspace):
        expected = SuiteResult(passed=("t::a",), failed=("t::b",), fingerprint={"rootdir": "/repo"})
        verifier = await ready(workspace, backend=FakeBackend(results=[SuiteResult(), expected]))

        result = await verifier.run_subset(workspace, ["t"])

        assert result == expected

    async def test_it_discards_its_directories_on_success(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["a"])

        assert workspace.discarded == ["probe-1"]
        assert not (workspace.root / "export-probe-1").exists()
        assert not (workspace.root / "results-probe-1").exists()

    async def test_it_discards_its_directories_when_the_backend_blows_up(self, workspace):
        verifier = await ready(workspace, backend=FakeBackend(results=[SuiteResult(), ValueError("bug")]))

        with pytest.raises(ValueError, match="bug"):
            await verifier.run_subset(workspace, ["a"])

        assert workspace.discarded == ["probe-1"]
        assert not (workspace.root / "export-probe-1").exists()

    async def test_it_discards_when_cancelled(self, workspace):
        class HangsAfterTheBaseline(FakeBackend):
            async def run_tests(self, *args, **kwargs):
                if self.runs:  # the baseline has run; this is the probe
                    await asyncio.sleep(60)
                return await super().run_tests(*args, **kwargs)

        verifier = await ready(workspace, backend=HangsAfterTheBaseline())
        task = asyncio.ensure_future(verifier.run_subset(workspace, ["a"]))
        await asyncio.sleep(0.05)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert workspace.discarded == ["probe-1"]

    async def test_it_discards_even_when_the_export_was_refused(self, workspace):
        """A dirty tree raises before the backend is called; the finally still runs."""
        verifier = await ready(workspace)
        workspace.set_dirty(True)

        with pytest.raises(RuntimeError, match="refusing to export"):
            await verifier.run_subset(workspace, ["a"])

        assert workspace.discarded == ["probe-1"]
        assert len(verifier.backend.runs) == 1  # the baseline only

    async def test_a_sandbox_failure_is_returned_not_raised(self, workspace):
        failure = SandboxUnavailable("daemon is gone")
        verifier = await ready(workspace, backend=FakeBackend(results=[SuiteResult(), failure]))

        result = await verifier.run_subset(workspace, ["a"])

        assert not result.scoreable
        assert "daemon is gone" in result.error
        assert workspace.discarded == ["probe-1"]

    @pytest.mark.parametrize(
        "failure",
        [SandboxUnavailable("x"), EnvironmentBuildFailed("a/b", 1, "log")],
        ids=["unavailable", "build-failed"],
    )
    async def test_every_sandbox_error_is_returned(self, workspace, failure):
        verifier = await ready(workspace, backend=FakeBackend(results=[SuiteResult(), failure]))

        assert (await verifier.run_subset(workspace, ["a"])).error == str(failure)

    async def test_the_returned_message_is_redacted(self, workspace):
        secret = "ghp_" + "a" * 36
        verifier = await ready(
            workspace, backend=FakeBackend(results=[SuiteResult(), SandboxUnavailable(f"auth failed: {secret}")])
        )

        result = await verifier.run_subset(workspace, ["a"])

        assert secret not in result.error

    async def test_a_dirty_tree_is_not_swallowed_as_a_sandbox_failure(self, workspace):
        """Only `SandboxError` is returned; a refusal to export is a bug in the caller
        (it forgot the checkpoint) and has to be loud."""
        verifier = await ready(workspace)
        workspace.set_dirty(True)

        with pytest.raises(RuntimeError):
            await verifier.run_subset(workspace, ["a"])

    async def test_concurrent_probes_do_not_share_a_label_or_a_container(self, workspace):
        verifier = await ready(workspace)

        await asyncio.gather(*(verifier.run_subset(workspace, [f"t{i}"]) for i in range(5)))

        probes = verifier.backend.runs[1:]
        assert len({run["container"] for run in probes}) == 5
        assert len({run["source"] for run in probes}) == 5


class TestProbesDoNotDisturbTheBaselineGuard:
    async def test_a_scored_attempt_after_probes_is_not_mistaken_for_a_baseline(self, workspace):
        verifier = await ready(workspace)
        await verifier.run_subset(workspace, ["a"])
        await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)

        await verifier.run(workspace, 1)

        assert workspace.exported[-1] == 1

    async def test_the_baseline_guard_still_fires_after_probes(self, workspace):
        verifier = await ready(workspace)
        await verifier.run_subset(workspace, ["a"])

        with pytest.raises(RuntimeError, match="baseline"):
            await verifier.run(workspace, BASELINE_ATTEMPT)

    async def test_a_probe_does_not_count_as_the_baseline(self, workspace):
        """It never prepares, so it cannot stand in for the run that does."""
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        with pytest.raises(VerifierNotReady):
            await verifier.run_subset(workspace, ["a"])
        await verifier.run(workspace, BASELINE_ATTEMPT)  # still allowed: nothing ran yet

        assert verifier.prepared


class TestRunScript:
    async def test_it_needs_the_baseline_to_have_built_the_environment(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        with pytest.raises(VerifierNotReady):
            await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)

        assert workspace.exported == [] and workspace.discarded == []
        assert verifier.backend.scripts == []

    async def test_labels_are_script_n_with_their_own_counter(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_subset(workspace, ["a"])
        await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)
        await verifier.run_script(workspace, "print(2)", timeout_seconds=5.0)

        assert workspace.exported == [BASELINE_ATTEMPT, "probe-1", "script-1", "script-2"]

    async def test_the_script_is_what_the_backend_is_given(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_script(workspace, "import sys\nprint(sys.argv)\n", timeout_seconds=5.0)

        call = verifier.backend.scripts[0]
        assert call["script_text"] == "import sys\nprint(sys.argv)\n"
        assert call["script"].name == "_repolace_script.py"

    async def test_the_script_lives_in_the_results_directory_not_the_tree(self, workspace):
        """Not in the export, which is mounted read-only and which the host might
        later diff; and not in the checkout, which `git add -A` would sweep into a
        commit."""
        verifier = await ready(workspace)

        await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)

        call = verifier.backend.scripts[0]
        assert call["script"].parent.name == "results-script-1"
        assert call["script"].parent != call["source"]
        assert "_repolace_script.py" not in call["snapshot"]

    async def test_the_script_file_is_readable_by_another_uid(self, workspace):
        modes = []

        class Recording(FakeBackend):
            async def run_script(self, env, source_dir, script_path, *args, **kwargs):
                modes.append(stat.S_IMODE(script_path.stat().st_mode))
                return await super().run_script(env, source_dir, script_path, *args, **kwargs)

        verifier = await ready(workspace, backend=Recording())

        await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)

        assert modes == [0o644]

    async def test_the_script_does_not_survive_the_call(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)

        assert not (workspace.root / "results-script-1").exists()
        assert not (workspace.root / "export-script-1").exists()

    async def test_it_never_includes_the_overlay(self, workspace):
        verifier = await ready(workspace, overlay=HIDDEN)

        await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)

        assert set(verifier.backend.scripts[0]["snapshot"]).isdisjoint(HIDDEN)

    async def test_it_never_builds_an_environment(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)

        assert len(verifier.backend.prepared) == 1

    async def test_the_timeout_reaches_the_backend_as_its_own_argument(self, workspace):
        verifier = await ready(workspace, spec=RepoSpec(key="a/b", timeout_seconds=900.0))

        await verifier.run_script(workspace, "print(1)", timeout_seconds=12.5)

        call = verifier.backend.scripts[0]
        assert call["timeout"] == 12.5
        assert call["spec"] == verifier.spec  # untouched: a script has no targets to inject

    async def test_the_container_name_is_the_labels(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_script(workspace, "print(1)", timeout_seconds=5.0)

        assert verifier.backend.scripts[0]["container"] == container_name(verifier.task_id, "script-1")

    async def test_the_backends_result_comes_back_unchanged(self, workspace):
        expected = ScriptResult(exit_code=3, stdout="out", stderr="err", truncated=True)
        verifier = await ready(workspace, backend=FakeBackend(scripts=[expected]))

        assert await verifier.run_script(workspace, "x", timeout_seconds=5.0) == expected

    async def test_it_discards_on_success_and_on_failure(self, workspace):
        verifier = await ready(workspace, backend=FakeBackend(scripts=[ScriptResult(exit_code=0), ValueError("bug")]))

        await verifier.run_script(workspace, "x", timeout_seconds=5.0)
        with pytest.raises(ValueError, match="bug"):
            await verifier.run_script(workspace, "x", timeout_seconds=5.0)

        assert workspace.discarded == ["script-1", "script-2"]

    async def test_a_sandbox_failure_is_returned_not_raised(self, workspace):
        verifier = await ready(
            workspace, backend=FakeBackend(scripts=[SandboxUnavailable("daemon is gone")])
        )

        result = await verifier.run_script(workspace, "x", timeout_seconds=5.0)

        assert result.exit_code is None
        assert "daemon is gone" in result.error
        assert workspace.discarded == ["script-1"]

    async def test_a_dirty_tree_is_loud(self, workspace):
        verifier = await ready(workspace)
        workspace.set_dirty(True)

        with pytest.raises(RuntimeError, match="refusing to export"):
            await verifier.run_script(workspace, "x", timeout_seconds=5.0)

        assert verifier.backend.scripts == [] and workspace.discarded == ["script-1"]

    async def test_a_lone_surrogate_in_model_output_does_not_crash_the_tool(self, workspace):
        verifier = await ready(workspace)

        await verifier.run_script(workspace, "print('\ud800')", timeout_seconds=5.0)

        assert verifier.backend.scripts[0]["script_text"] == "print('?')"

    async def test_concurrent_scripts_do_not_share_a_label(self, workspace):
        verifier = await ready(workspace)

        await asyncio.gather(*(verifier.run_script(workspace, f"print({i})", timeout_seconds=5.0) for i in range(4)))

        scripts = verifier.backend.scripts
        assert len({call["container"] for call in scripts}) == 4
        assert sorted(call["script_text"] for call in scripts) == [f"print({i})" for i in range(4)]
