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
import textwrap
import uuid
from pathlib import Path

import pytest

from verify.backends.docker import DockerBackend
from verify.config import DockerConfig
from verify.dockerfile import image_cache_key
from verify.protocol import RepoSpec
from verify.scoring import score
from verify.spec import install_commands

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
