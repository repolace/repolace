"""End-to-end against a real daemon.

Skipped when none answers -- and *failed* when REPOLACE_TEST_DOCKER_REQUIRED=1,
because the containment assertions below are precisely the ones that must never
pass vacuously. A green build that proved the network was off without ever
starting a container would be worse than no test at all.

These are the only tests in the project that execute the thing the whole Verify
design exists to contain, so they check two separate claims: that the pass/fail
data comes back correctly, and that the sandbox is actually a sandbox.
"""

import os
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from repolace_shared.git.workspace import task_workspace
from verify.backends.docker import DockerBackend
from verify.config import DockerConfig
from verify.dockerfile import image_cache_key
from verify.protocol import RepoSpec
from verify.scoring import score
from verify.spec import install_commands
from verify.stage import BASELINE_ATTEMPT, Verifier, container_name

pytestmark = [pytest.mark.docker, pytest.mark.anyio]

#: Matches `export_index_to`'s dir_mode. The sandbox runs as an unprivileged uid
#: that is not ours, and it has to be able to create entries in these
#: directories -- the report above all, which is the only thing that comes out.
SANDBOX_DIR_MODE = 0o777

PASSING_AND_FAILING = textwrap.dedent(
    """
    def test_passes():
        assert True

    def test_fails():
        assert 1 == 2

    def test_skipped():
        import pytest
        pytest.skip("not today")
    """
)

BOTH_PASSING = PASSING_AND_FAILING.replace("assert 1 == 2", "assert 1 == 1")


def make_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, SANDBOX_DIR_MODE)
    return path


def make_source(root: Path, name: str, body: str) -> Path:
    source = make_dir(root / name)
    (source / "test_sample.py").write_text(body)
    return source


@pytest.fixture
def backend():
    # A short run timeout: these suites take under a second, and a hung daemon
    # should fail the test rather than the session.
    return DockerBackend(DockerConfig(run_timeout_seconds=180.0))


@pytest.fixture
def spec():
    return RepoSpec(key="repolace/docker-backend-test")


@pytest.fixture
async def environment(backend, spec, tmp_path):
    """One image, built from the baseline tree, reused by every test here.

    Which is also the production rule: the environment is built once and shared
    by the baseline and every attempt, so a suite that starts failing can only
    be the patch's doing.
    """
    source = make_source(tmp_path, "build", PASSING_AND_FAILING)
    install = install_commands(spec, source)
    return await backend.prepare(spec, source, image_cache_key(spec, install, source))


async def run(backend, spec, environment, tmp_path, name, body):
    source = make_source(tmp_path, name, body)
    results = make_dir(tmp_path / f"results-{name}")
    return await backend.run_tests(
        environment,
        source,
        results,
        spec,
        container_name=f"repolace-test-{uuid.uuid4().hex[:12]}",
    )


class TestResults:
    async def test_the_pass_fail_sets_come_back(self, backend, spec, environment, tmp_path):
        result = await run(backend, spec, environment, tmp_path, "base", PASSING_AND_FAILING)

        assert result.scoreable, result.error
        assert "test_sample.py::test_passes" in result.passed
        assert "test_sample.py::test_fails" in result.failed
        assert "test_sample.py::test_skipped" in result.skipped

    async def test_an_ordinary_skip_is_not_recorded_as_an_xfail(
        self, backend, spec, environment, tmp_path
    ):
        """The distinction a false PASSED once turned on: an `importorskip` that
        started passing is evidence of nothing, an xfail going green is not."""
        result = await run(backend, spec, environment, tmp_path, "skips", PASSING_AND_FAILING)

        assert result.xfailed == ()

    async def test_pytest_collected_the_file_it_says_it_did(
        self, backend, spec, environment, tmp_path
    ):
        """`disqualifying_paths` joins on this, and a path heuristic cannot
        replace it."""
        result = await run(backend, spec, environment, tmp_path, "collect", PASSING_AND_FAILING)

        assert "test_sample.py" in result.collected_files

    async def test_a_fixed_suite_scores_as_a_pass(self, backend, spec, environment, tmp_path):
        """The whole point of the stage, end to end: a red test at baseline, the
        same test green afterwards, nothing else changed."""
        baseline = await run(backend, spec, environment, tmp_path, "b0", PASSING_AND_FAILING)
        attempt = await run(backend, spec, environment, tmp_path, "b1", BOTH_PASSING)

        scored = score(baseline, attempt, ["test_sample.py"], baseline_files=("test_sample.py",))

        # The diff touches the only file there is, which *is* the test file --
        # so the honest outcome is a disqualification, and asserting it is how
        # this test proves the criteria are enforced rather than decorative.
        assert scored.disqualified == ("test_sample.py",)

        scored_source_only = score(
            baseline, attempt, ["sample.py"], baseline_files=("test_sample.py", "sample.py")
        )
        assert scored_source_only.outcome is not None
        assert scored_source_only.outcome.value == "passed"
        assert scored_source_only.fail_to_pass == ("test_sample.py::test_fails",)

    async def test_the_fingerprint_is_stable_across_two_runs_of_the_same_image(
        self, backend, spec, environment, tmp_path
    ):
        """`score` refuses to compare two runs whose fingerprints differ, so an
        unstable one would make every task unscoreable."""
        first = await run(backend, spec, environment, tmp_path, "f0", PASSING_AND_FAILING)
        second = await run(backend, spec, environment, tmp_path, "f1", PASSING_AND_FAILING)

        assert first.fingerprint == second.fingerprint


class TestContainment:
    async def test_the_suite_cannot_reach_the_network(
        self, backend, spec, environment, tmp_path
    ):
        """The exfiltration path. Network is permitted during the build and
        nowhere else, so this must fail rather than error out the run."""
        body = textwrap.dedent(
            """
            import socket

            def test_network():
                socket.create_connection(("1.1.1.1", 80), timeout=5)
            """
        )
        result = await run(backend, spec, environment, tmp_path, "net", body)

        assert result.scoreable, result.error
        assert result.failed == ("test_sample.py::test_network",)

    async def test_nothing_outside_the_mounts_and_tmp_is_writable(
        self, backend, spec, environment, tmp_path
    ):
        """`--read-only` plus an unprivileged uid. /tmp has to stay writable --
        pytest's own tmp_path lives there -- and /repo is the export, which the
        suite legitimately owns."""
        body = textwrap.dedent(
            """
            import pytest

            @pytest.mark.parametrize("path", ["/probe", "/etc/probe", "/usr/probe"])
            def test_readonly(path):
                with pytest.raises(OSError):
                    open(path, "w").write("x")

            def test_tmp_is_writable(tmp_path):
                (tmp_path / "x").write_text("x")
            """
        )
        result = await run(backend, spec, environment, tmp_path, "ro", body)

        assert result.scoreable, result.error
        assert result.failed == ()
        assert len(result.passed) == 4

    async def test_it_does_not_run_as_root(self, backend, spec, environment, tmp_path):
        body = textwrap.dedent(
            """
            import os

            def test_uid():
                assert os.getuid() == 65534
            """
        )
        result = await run(backend, spec, environment, tmp_path, "uid", body)

        assert result.scoreable, result.error
        assert result.failed == ()


class TestEnvironmentBuild:
    async def test_a_second_prepare_reuses_the_image(self, backend, spec, tmp_path):
        """Rebuilding per attempt would mean the baseline and the attempt ran in
        different environments -- the ambiguity the baseline exists to remove."""
        source = make_source(tmp_path, "reuse", PASSING_AND_FAILING)
        key = image_cache_key(spec, install_commands(spec, source), source)

        first = await backend.prepare(spec, source, key)
        second = await backend.prepare(spec, source, key)

        assert first.identifier == second.identifier

    async def test_an_impossible_install_fails_as_a_build_not_as_a_run(self, backend, tmp_path):
        """`EnvironmentBuildFailed` names the repo. It is expected to be the most
        common failure on a new repo, and CLAUDE.md says so."""
        from verify.errors import EnvironmentBuildFailed

        spec = RepoSpec(
            key="repolace/broken",
            install=("pip install repolace-package-that-does-not-exist-9f3a",),
        )
        source = make_source(tmp_path, "broken", PASSING_AND_FAILING)

        with pytest.raises(EnvironmentBuildFailed, match="repolace/broken"):
            await backend.prepare(spec, source, "brokenkey")


# --- scratch scripts and probes ----------------------------------------------
#
# The agent gets a shell-shaped tool, so these are the assertions that decide
# whether it is safe to hand it one. Same rule as above: they must never pass
# vacuously, which is why each one asserts a *positive* observation made from
# inside the container (an errno, a uid, a cgroup file) rather than the absence
# of an exception.

SCRIPT_FILE_MODE = 0o644


def write_script(tmp_path: Path, name: str, code: str) -> Path:
    directory = make_dir(tmp_path / f"results-script-{name}")
    script = directory / "main.py"
    script.write_text(textwrap.dedent(code))
    script.chmod(SCRIPT_FILE_MODE)
    return script


async def run_script(
    backend, spec, environment, tmp_path, name, code, *, source_body=PASSING_AND_FAILING,
    timeout=120.0, container=None,
):
    source = make_source(tmp_path, f"src-{name}", source_body)
    script = write_script(tmp_path, name, code)
    result = await backend.run_script(
        environment,
        source,
        script,
        spec,
        container_name=container or f"repolace-test-{uuid.uuid4().hex[:12]}",
        timeout_seconds=timeout,
    )
    return result, source


def containers_named(prefix: str) -> list[str]:
    """Every container, running or not, whose name starts with `prefix`."""
    listing = subprocess.run(
        [DockerConfig().docker_binary, "ps", "--all", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    )
    return listing.stdout.split()


class TestScriptContainment:
    async def test_it_does_not_run_as_root(self, backend, spec, environment, tmp_path):
        result, _ = await run_script(backend, spec, environment, tmp_path, "uid", "import os\nprint(os.getuid())\n")

        assert result.error is None, result.error
        assert result.exit_code == 0
        assert result.stdout.strip() == "65534"

    async def test_the_network_is_unreachable(self, backend, spec, environment, tmp_path):
        """Both halves: a raw connection and name resolution. The script prints what
        it saw, so a script that never ran cannot pass this."""
        code = """
            import socket
            for label, attempt in (
                ("connect", lambda: socket.create_connection(("1.1.1.1", 80), timeout=5)),
                ("resolve", lambda: socket.getaddrinfo("example.com", 80)),
            ):
                try:
                    attempt()
                    print(label, "REACHED")
                except OSError as exc:
                    print(label, "blocked", type(exc).__name__)
        """
        result, _ = await run_script(backend, spec, environment, tmp_path, "net", code)

        assert result.error is None, result.error
        assert "connect blocked" in result.stdout and "resolve blocked" in result.stdout
        assert "REACHED" not in result.stdout

    async def test_the_root_and_the_repo_are_read_only(self, backend, spec, environment, tmp_path):
        """EROFS (30) specifically -- a permissions error would mean something else
        was stopping the write, and that something may not be the mount."""
        code = """
            import errno
            for path in ("/probe", "/etc/probe", "/usr/probe", "/repo/probe", "/repo/test_sample.py"):
                try:
                    open(path, "w").write("x")
                    print(path, "WRITTEN")
                except OSError as exc:
                    print(path, errno.errorcode[exc.errno])
        """
        result, source = await run_script(backend, spec, environment, tmp_path, "ro", code)

        assert result.error is None, result.error
        for path in ("/probe", "/etc/probe", "/usr/probe", "/repo/probe", "/repo/test_sample.py"):
            assert f"{path} EROFS" in result.stdout, result.stdout
        assert "WRITTEN" not in result.stdout
        assert sorted(p.name for p in source.iterdir()) == ["test_sample.py"]
        assert (source / "test_sample.py").read_text() == PASSING_AND_FAILING

    async def test_the_source_is_read_only_even_when_the_spec_says_writable(
        self, backend, environment, tmp_path
    ):
        """The unconditional `:ro`: a spec must not be able to turn it off."""
        writable_spec = RepoSpec(key="repolace/docker-backend-test", repo_readonly=False)
        code = """
            import errno
            try:
                open("/repo/planted.py", "w").write("x")
                print("WRITTEN")
            except OSError as exc:
                print(errno.errorcode[exc.errno])
        """
        result, source = await run_script(backend, writable_spec, environment, tmp_path, "rospec", code)

        assert result.stdout.strip() == "EROFS", result.stdout
        assert not (source / "planted.py").exists()

    async def test_the_script_itself_cannot_be_rewritten(self, backend, spec, environment, tmp_path):
        code = """
            import errno
            try:
                open("/scratch/main.py", "w").write("x")
                print("WRITTEN")
            except OSError as exc:
                print(errno.errorcode[exc.errno])
        """
        result, _ = await run_script(backend, spec, environment, tmp_path, "selfro", code)

        assert result.stdout.strip() == "EROFS", result.stdout

    async def test_tmp_is_writable_because_a_real_script_needs_it(self, backend, spec, environment, tmp_path):
        code = """
            import pathlib, tempfile
            with tempfile.TemporaryDirectory() as d:
                (pathlib.Path(d) / "x").write_text("ok")
                print((pathlib.Path(d) / "x").read_text())
        """
        result, _ = await run_script(backend, spec, environment, tmp_path, "tmp", code)

        assert result.stdout.strip() == "ok", result.stdout + result.stderr

    async def test_nothing_comes_back_through_a_results_mount(self, backend, spec, environment, tmp_path):
        code = """
            import os
            print("results-exists", os.path.exists("/results"))
        """
        result, _ = await run_script(backend, spec, environment, tmp_path, "noresults", code)

        assert "results-exists False" in result.stdout, result.stdout
        assert sorted(p.name for p in (tmp_path / "results-script-noresults").iterdir()) == ["main.py"]

    async def test_every_capability_is_dropped_and_privilege_escalation_blocked(
        self, backend, spec, environment, tmp_path
    ):
        code = """
            for line in open("/proc/self/status"):
                if line.startswith(("CapEff", "CapBnd", "NoNewPrivs")):
                    print(line.strip())
        """
        result, _ = await run_script(backend, spec, environment, tmp_path, "caps", code)

        assert "CapEff:\t0000000000000000" in result.stdout, result.stdout
        assert "CapBnd:\t0000000000000000" in result.stdout
        assert "NoNewPrivs:\t1" in result.stdout

    async def test_the_resource_caps_bind(self, backend, spec, environment, tmp_path):
        """Read back from the cgroup, as CLAUDE.md records for `run_tests`. Rootless
        Docker without cgroup v2 delegation silently ignores these flags, so a green
        run here is also the check that delegation is in place."""
        code = """
            for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max"):
                print(name, open("/sys/fs/cgroup/" + name).read().strip())
        """
        result, _ = await run_script(backend, spec, environment, tmp_path, "cgroup", code)

        assert result.error is None, result.error
        assert "memory.max 2147483648" in result.stdout, result.stdout
        assert "memory.swap.max 0" in result.stdout
        assert "pids.max 512" in result.stdout
        assert "cpu.max 200000 100000" in result.stdout

    async def test_the_source_root_is_importable_through_pythonpath(
        self, backend, spec, environment, tmp_path
    ):
        """A script's `sys.path[0]` is `/scratch`, so a flat-layout package only
        imports because `PYTHONPATH=/repo`."""
        source = make_source(tmp_path, "src-flat", PASSING_AND_FAILING)
        (source / "flatpkg").mkdir()
        (source / "flatpkg" / "__init__.py").write_text("VALUE = 7\n")
        script = write_script(tmp_path, "flat", "import flatpkg\nprint(flatpkg.VALUE)\n")

        result = await backend.run_script(
            environment, source, script, spec,
            container_name=f"repolace-test-{uuid.uuid4().hex[:12]}", timeout_seconds=120.0,
        )

        assert result.stdout.strip() == "7", result.stdout + result.stderr

    async def test_the_scripts_exit_status_and_stderr_come_back(self, backend, spec, environment, tmp_path):
        code = """
            import sys
            print("to stdout")
            sys.stderr.write("to stderr")
            sys.exit(4)
        """
        result, _ = await run_script(backend, spec, environment, tmp_path, "exit", code)

        assert result.exit_code == 4 and result.error is None
        assert result.stdout.strip() == "to stdout" and result.stderr == "to stderr"

    async def test_a_traceback_is_the_scripts_failure_not_the_runtimes(self, backend, spec, environment, tmp_path):
        result, _ = await run_script(backend, spec, environment, tmp_path, "tb", "raise ValueError('boom')\n")

        assert result.exit_code == 1 and result.error is None
        assert "ValueError: boom" in result.stderr


class TestScriptTimeout:
    async def test_a_runaway_script_is_killed_and_its_container_is_gone(
        self, backend, spec, environment, tmp_path
    ):
        """Killing the `docker run` client leaves the container running, holding a
        memory cgroup and a bind mount of a directory about to be deleted. The
        backend removes it by name, so none may remain."""
        task = uuid.uuid4()
        name = container_name(task, "script-1")

        result, _ = await run_script(
            backend, spec, environment, tmp_path, "spin", "while True:\n    pass\n",
            timeout=5.0, container=name,
        )

        assert result.timed_out is True and result.exit_code is None
        assert containers_named(f"repolace-{task.hex[:12]}-script-") == []


class TestProbesAndScriptsThroughTheVerifier:
    """End to end over a real checkout: the label rules, the overlay rules and the
    cleanup, with real containers behind them."""

    @pytest.fixture
    def origin_url(self, tmp_path):
        repo = tmp_path / "origin"
        repo.mkdir()
        (repo / "test_sample.py").write_text(PASSING_AND_FAILING)
        for args in (
            ["init", "--initial-branch=main", "."],
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
             "commit", "-m", "base"],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        return f"file://{repo}"

    async def test_two_probes_never_mix_their_reports(self, backend, spec, origin_url, tmp_path):
        """Each probe has its own results directory, because the plugin opens
        `report.jsonl` in append mode and a shared one would hold both runs."""
        async with task_workspace(
            "o", "r", target_branch="main", clone_url=origin_url, parent_dir=tmp_path
        ) as workspace:
            verifier = Verifier(backend, spec, uuid.uuid4())
            await verifier.run(workspace, BASELINE_ATTEMPT)

            passing = await verifier.run_subset(workspace, ["test_sample.py::test_passes"])
            failing = await verifier.run_subset(workspace, ["test_sample.py::test_fails"])

        assert passing.scoreable, passing.error
        assert failing.scoreable, failing.error
        assert passing.passed == ("test_sample.py::test_passes",) and passing.failed == ()
        assert failing.failed == ("test_sample.py::test_fails",) and failing.passed == ()

    async def test_a_probes_node_ids_match_the_baselines(self, backend, spec, origin_url, tmp_path):
        """`--rootdir=/repo` is always pinned, so a run given one file does not root
        at that file's directory and rename every id."""
        async with task_workspace(
            "o", "r", target_branch="main", clone_url=origin_url, parent_dir=tmp_path
        ) as workspace:
            verifier = Verifier(backend, spec, uuid.uuid4())
            baseline = await verifier.run(workspace, BASELINE_ATTEMPT)
            probe = await verifier.run_subset(workspace, ["test_sample.py"])

        assert set(probe.passed) <= set(baseline.passed) and probe.passed
        assert set(probe.failed) <= set(baseline.failed) and probe.failed
        assert probe.fingerprint["rootdir"] == baseline.fingerprint["rootdir"] == "/repo"

    async def test_probes_and_scripts_leave_no_directories_and_no_containers(
        self, backend, spec, origin_url, tmp_path
    ):
        task = uuid.uuid4()
        async with task_workspace(
            "o", "r", target_branch="main", clone_url=origin_url, parent_dir=tmp_path
        ) as workspace:
            verifier = Verifier(backend, spec, task)
            await verifier.run(workspace, BASELINE_ATTEMPT)

            await verifier.run_subset(workspace, ["test_sample.py"])
            script = await verifier.run_script(workspace, "print('hi')", timeout_seconds=60.0)

            leftovers = sorted(p.name for p in workspace.root.iterdir())

        assert script.stdout.strip() == "hi", script.error
        assert leftovers == ["export-0", "repo", "results-0"]
        assert containers_named(f"repolace-{task.hex[:12]}") == []

    async def test_a_script_run_does_not_put_the_script_in_the_checkout(
        self, backend, spec, origin_url, tmp_path
    ):
        async with task_workspace(
            "o", "r", target_branch="main", clone_url=origin_url, parent_dir=tmp_path
        ) as workspace:
            verifier = Verifier(backend, spec, uuid.uuid4())
            await verifier.run(workspace, BASELINE_ATTEMPT)

            await verifier.run_script(workspace, "print('hi')", timeout_seconds=60.0)

            assert not await workspace.repo.has_changes()
            assert not (workspace.path / "main.py").exists()

    async def test_the_overlay_runs_in_scored_runs_and_is_invisible_to_probes_and_the_image(
        self, backend, spec, origin_url, tmp_path
    ):
        """The oracle must reach the scored runs and nothing else: not a probe, not
        a script, and not the image layer the cache shares between tasks."""
        hidden = {"test_hidden.py": b"def test_hidden():\n    assert False\n"}
        async with task_workspace(
            "o", "r", target_branch="main", clone_url=origin_url, parent_dir=tmp_path
        ) as workspace:
            verifier = Verifier(backend, spec, uuid.uuid4(), overlay=hidden)

            baseline = await verifier.run(workspace, BASELINE_ATTEMPT)
            # No targets: the whole visible tree, so a leaked hidden file would be collected.
            probe = await verifier.run_subset(workspace, [])
            script = await verifier.run_script(
                workspace, "import os\nprint(os.path.exists('/repo/test_hidden.py'))", timeout_seconds=60.0
            )

            image = verifier._env.identifier

        assert "test_hidden.py::test_hidden" in baseline.failed
        assert probe.scoreable, probe.error
        assert "test_sample.py::test_passes" in probe.passed  # it really ran the visible suite
        assert "test_hidden.py::test_hidden" not in probe.failed + probe.passed + probe.skipped
        assert script.stdout.strip() == "False", script.error

        in_image = subprocess.run(
            [DockerConfig().docker_binary, "run", "--rm", "--network=none", "--entrypoint", "python", image,
             "-c", "import os; print(os.path.exists('/repo/test_hidden.py'))"],
            capture_output=True, text=True, check=True,
        )
        assert in_image.stdout.strip() == "False"
