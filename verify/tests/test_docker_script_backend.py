"""`DockerBackend.run_script` against a stand-in `docker` executable.

Not the real thing -- `test_docker_backend.py` is, and needs a daemon. This file
covers the logic around the container that does not depend on one: how a result is
shaped from what the CLI did, and above all that **the container is removed by
name when the deadline fires or the task is cancelled**, because killing the
`docker run` client leaves the container alive, holding its memory cgroup and a
bind mount of a directory the workspace is about to delete.

A real child process, as `shared/tests/test_process.py` does: the behaviour worth
defending is what happens to an actual process under a deadline.
"""

import asyncio
import time
import uuid
from pathlib import Path

import pytest

from verify.backends.docker import DockerBackend, build_script_argv
from verify.config import DockerConfig
from verify.protocol import EnvironmentRef, RepoSpec
from verify.stage import container_name

pytestmark = pytest.mark.anyio

ENV = EnvironmentRef(backend="docker", identifier="repolace-verify:a_b-cafe")
NAME = container_name(uuid.UUID("2f8a1c4e-0000-4000-8000-000000000001"), "script-1")


@pytest.fixture
def fake_docker(tmp_path):
    """An executable that logs its argv and behaves as told for `docker run`.

    The log path is baked into the script because the backend hands the CLI an
    allowlisted environment, so a variable set here would never arrive.
    """
    log = tmp_path / "calls.log"

    def make(run_body: str, *, rm_exit: int = 0) -> tuple[str, Path]:
        path = tmp_path / "docker"
        path.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> {log}\n'
            'case "$1" in\n'
            f"  rm) exit {rm_exit} ;;\n"
            f"  run) {run_body} ;;\n"
            "esac\n"
        )
        path.chmod(0o755)
        return str(path), log

    return make


def backend_for(binary: str, **config) -> DockerBackend:
    return DockerBackend(DockerConfig(docker_binary=binary, **config))


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


async def run(backend, tmp_path, *, timeout=10.0, name=NAME):
    return await backend.run_script(
        ENV,
        tmp_path / "export-script-1",
        tmp_path / "results-script-1" / "main.py",
        RepoSpec(key="a/b"),
        container_name=name,
        timeout_seconds=timeout,
    )


class TestAnOrdinaryRun:
    async def test_output_and_exit_status_are_the_scripts(self, fake_docker, tmp_path):
        binary, _log = fake_docker("echo hello; echo oops >&2; exit 3")

        result = await run(backend_for(binary), tmp_path)

        assert result.exit_code == 3
        assert result.stdout.strip() == "hello" and result.stderr.strip() == "oops"
        assert result.error is None and result.timed_out is False and result.truncated is False

    async def test_a_non_zero_exit_is_the_agents_to_read_not_a_runtime_error(self, fake_docker, tmp_path):
        """126 and 127 are the contained command failing to start, 1 is a traceback:
        all of them are facts about the script, so none may become `error`."""
        for code in (1, 2, 126, 127, 137):
            binary, _log = fake_docker(f"exit {code}")

            result = await run(backend_for(binary), tmp_path)

            assert result.exit_code == code and result.error is None, code

    async def test_it_gives_the_cli_exactly_the_argv_the_builder_builds(self, fake_docker, tmp_path):
        binary, log = fake_docker("exit 0")
        config = DockerConfig(docker_binary=binary)
        backend = DockerBackend(config)

        await run(backend, tmp_path)

        expected = build_script_argv(
            config, RepoSpec(key="a/b"), ENV,
            tmp_path / "export-script-1", tmp_path / "results-script-1" / "main.py", NAME,
        )
        assert calls(log) == [" ".join(expected)]

    async def test_it_does_not_ask_the_daemon_anything_else(self, fake_docker, tmp_path):
        """No `version` probe and no `image inspect`: the environment was prepared
        by the baseline, and a script run is one `docker run`."""
        binary, log = fake_docker("exit 0")

        await run(backend_for(binary), tmp_path)

        assert [line.split()[0] for line in calls(log)] == ["run"]

    async def test_a_clean_exit_does_not_issue_a_spurious_remove(self, fake_docker, tmp_path):
        """`--rm` already did it; a `docker rm` per run is noise in the daemon's log."""
        binary, log = fake_docker("echo done")

        await run(backend_for(binary), tmp_path)

        assert not any(line.startswith("rm ") for line in calls(log))

    async def test_the_duration_is_measured(self, fake_docker, tmp_path):
        binary, _log = fake_docker("sleep 0.2")

        result = await run(backend_for(binary), tmp_path)

        assert result.duration_seconds is not None and result.duration_seconds >= 0.2

    async def test_output_that_is_not_utf8_is_replaced_not_fatal(self, fake_docker, tmp_path):
        binary, _log = fake_docker(r"printf '\377\376ok'")

        result = await run(backend_for(binary), tmp_path)

        assert result.stdout.endswith("ok") and "�" in result.stdout

    async def test_output_past_the_capture_limit_is_flagged_truncated(self, fake_docker, tmp_path):
        binary, _log = fake_docker("head -c 20000 /dev/zero | tr '\\000' x")

        result = await run(backend_for(binary, capture_limit=1024), tmp_path)

        assert result.truncated is True
        assert len(result.stdout) == 1024
        assert result.exit_code == 0


class TestTheDeadline:
    async def test_a_script_that_outlives_it_is_reported_timed_out(self, fake_docker, tmp_path):
        binary, _log = fake_docker("echo partial; sleep 30")

        started = time.monotonic()
        result = await run(backend_for(binary), tmp_path, timeout=0.5)

        assert result.timed_out is True
        assert result.exit_code is None and result.error is None
        assert time.monotonic() - started < 10

    async def test_a_timed_out_result_carries_no_output(self, fake_docker, tmp_path):
        """The kill's output says nothing about the script, so none is passed on."""
        binary, _log = fake_docker("echo partial; sleep 30")

        result = await run(backend_for(binary), tmp_path, timeout=0.5)

        assert result.stdout == "" and result.stderr == ""

    async def test_the_container_is_removed_by_name_on_timeout(self, fake_docker, tmp_path):
        """Killing the client does not stop the container; `docker rm --force
        <name>` does. The name is the whole mechanism."""
        binary, log = fake_docker("sleep 30")

        await run(backend_for(binary), tmp_path, timeout=0.5)

        assert f"rm --force {NAME}" in calls(log)

    async def test_the_removal_names_this_runs_container_and_no_other(self, fake_docker, tmp_path):
        binary, log = fake_docker("sleep 30")
        other = container_name(uuid.UUID("2f8a1c4e-0000-4000-8000-000000000002"), "script-1")

        await run(backend_for(binary), tmp_path, timeout=0.5, name=other)

        removals = [line for line in calls(log) if line.startswith("rm ")]
        assert removals == [f"rm --force {other}"]

    async def test_a_removal_that_fails_does_not_turn_the_timeout_into_an_error(self, fake_docker, tmp_path):
        binary, _log = fake_docker("sleep 30", rm_exit=1)

        result = await run(backend_for(binary), tmp_path, timeout=0.5)

        assert result.timed_out is True and result.error is None

    async def test_cancellation_removes_the_container_and_still_propagates(self, fake_docker, tmp_path):
        binary, log = fake_docker("sleep 30")
        task = asyncio.ensure_future(run(backend_for(binary), tmp_path, timeout=30.0))
        deadline = time.monotonic() + 5
        while not calls(log) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        deadline = time.monotonic() + 5
        while f"rm --force {NAME}" not in calls(log) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert f"rm --force {NAME}" in calls(log)


class TestWhenTheRuntimeFails:
    """`error` means the script never ran, which is not a non-zero exit."""

    async def test_exit_125_is_docker_itself_failing(self, fake_docker, tmp_path):
        binary, _log = fake_docker(
            'echo "docker: Cannot connect to the Docker daemon at unix:///x.sock" >&2; exit 125'
        )

        result = await run(backend_for(binary), tmp_path)

        assert result.exit_code is None and result.timed_out is False
        assert "Cannot connect to the Docker daemon" in result.error
        assert result.error.startswith("container runtime unavailable")
        assert result.stdout == "" and result.stderr == ""

    async def test_the_runtime_message_is_redacted_and_capped(self, fake_docker, tmp_path):
        secret = "ghp_" + "a" * 36
        binary, _log = fake_docker(f'echo "auth failed: {secret}" >&2; exit 125')

        result = await run(backend_for(binary), tmp_path)

        assert secret not in result.error

    async def test_a_missing_docker_binary_is_an_error_not_a_crash(self, tmp_path):
        result = await run(backend_for(str(tmp_path / "no-such-docker")), tmp_path)

        assert result.exit_code is None
        assert result.error.startswith("container runtime unavailable")

    async def test_a_binary_that_is_not_executable_is_an_error_too(self, tmp_path):
        not_executable = tmp_path / "docker"
        not_executable.write_text("#!/bin/sh\n")
        not_executable.chmod(0o644)

        result = await run(backend_for(str(not_executable)), tmp_path)

        assert result.exit_code is None and result.error is not None
