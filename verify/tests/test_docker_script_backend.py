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
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
import structlog

from verify.backends import docker as docker_module
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

    def make(run_body: str, *, rm_exit: int = 0, rm_body: str | None = None) -> tuple[str, Path]:
        path = tmp_path / "docker"
        path.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> {log}\n'
            'case "$1" in\n'
            f"  rm) {rm_body if rm_body is not None else f'exit {rm_exit}'} ;;\n"
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
        tmp_path / "results-script-1" / "_repolace_script.py",
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
            tmp_path / "export-script-1", tmp_path / "results-script-1" / "_repolace_script.py", NAME,
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
    """`error` is for the runtime failing. Exit 125 alone is not proof of that: a
    script can exit 125 too, so it also has to read like docker's own message."""

    DAEMON_DOWN = 'echo "docker: Cannot connect to the Docker daemon at unix:///x.sock" >&2; exit 125'

    async def test_exit_125_with_dockers_own_message_is_a_runtime_failure(self, fake_docker, tmp_path):
        binary, _log = fake_docker(self.DAEMON_DOWN)

        result = await run(backend_for(binary), tmp_path)

        assert result.timed_out is False
        assert "Cannot connect to the Docker daemon" in result.error
        assert result.error.startswith("container runtime unavailable")

    async def test_the_real_exit_code_and_output_are_kept_even_then(self, fake_docker, tmp_path):
        """A forged runtime failure must not hide what the script really printed, or
        teach the agent to stop using its sandbox."""
        binary, _log = fake_docker(self.DAEMON_DOWN)

        result = await run(backend_for(binary), tmp_path)

        assert result.exit_code == 125
        assert "Cannot connect to the Docker daemon" in result.stderr

    async def test_the_error_says_it_may_be_the_scripts_own_exit(self, fake_docker, tmp_path):
        binary, _log = fake_docker(self.DAEMON_DOWN)

        result = await run(backend_for(binary), tmp_path)

        assert "exit 125 is also what a script that exits 125 produces" in result.error
        assert "may be a script exit, not a runtime failure" in result.error

    async def test_a_script_that_just_exits_125_is_an_ordinary_exit(self, fake_docker, tmp_path):
        binary, _log = fake_docker('echo "the real output"; echo "some error" >&2; exit 125')

        result = await run(backend_for(binary), tmp_path)

        assert result.exit_code == 125 and result.error is None
        assert result.stdout.strip() == "the real output" and result.stderr.strip() == "some error"

    async def test_a_daemon_error_response_is_recognised_without_the_docker_prefix(self, fake_docker, tmp_path):
        binary, _log = fake_docker('echo "Error response from daemon: no such image" >&2; exit 125')

        result = await run(backend_for(binary), tmp_path)

        assert "no such image" in result.error

    async def test_only_exit_125_is_ever_a_runtime_failure(self, fake_docker, tmp_path):
        """Docker-shaped stderr on any other exit is just a script that printed it."""
        binary, _log = fake_docker('echo "docker: Cannot connect to the Docker daemon" >&2; exit 1')

        result = await run(backend_for(binary), tmp_path)

        assert result.exit_code == 1 and result.error is None

    async def test_the_runtime_message_is_redacted_everywhere_it_appears(self, fake_docker, tmp_path):
        secret = "ghp_" + "a" * 36
        binary, _log = fake_docker(f'echo "docker: auth failed: {secret}" >&2; exit 125')

        result = await run(backend_for(binary), tmp_path)

        assert secret not in result.error
        assert secret not in result.stderr

    async def test_host_paths_in_dockers_message_are_replaced(self, fake_docker, tmp_path):
        """The task root is under a random directory a probe has no other way to
        learn; docker's own errors quote bind-mount sources."""
        export = tmp_path / "export-script-1"
        script = tmp_path / "results-script-1" / "_repolace_script.py"
        binary, _log = fake_docker(
            f'echo "docker: Error response from daemon: invalid mount config: bind source path '
            f'does not exist: {script}" >&2; '
            f'echo "also {export} and {tmp_path}" >&2; exit 125'
        )

        result = await run(backend_for(binary), tmp_path)

        for text in (result.error, result.stderr, result.stdout):
            assert str(tmp_path) not in text and tmp_path.name not in text
        assert "<script>" in result.stderr and "<export>" in result.stderr
        assert "<workspace>" in result.stderr

    async def test_an_ordinary_scripts_output_is_not_rewritten(self, fake_docker, tmp_path):
        """Only docker's own message is scrubbed; a script's output is its own."""
        binary, _log = fake_docker(f'echo "{tmp_path}"; exit 0')

        result = await run(backend_for(binary), tmp_path)

        assert result.stdout.strip() == str(tmp_path)

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


class TestScriptTimeoutValue:
    @pytest.mark.parametrize("value", [0, -1, -0.5, float("nan"), float("inf")])
    async def test_a_non_positive_or_non_finite_timeout_is_refused(self, fake_docker, tmp_path, value):
        """`0` is falsy: `timeout or default` would have run it for 30 minutes."""
        binary, log = fake_docker("exit 0")

        with pytest.raises(ValueError, match="positive"):
            await run(backend_for(binary), tmp_path, timeout=value)

        assert calls(log) == []

    async def test_a_positive_timeout_is_used(self, fake_docker, tmp_path):
        binary, _log = fake_docker("sleep 30")

        started = time.monotonic()
        result = await run(backend_for(binary), tmp_path, timeout=0.5)

        assert result.timed_out and time.monotonic() - started < 10

    async def test_none_means_the_configured_default(self, fake_docker, tmp_path):
        binary, _log = fake_docker("echo done")

        result = await run(backend_for(binary), tmp_path, timeout=None)

        assert result.exit_code == 0


class TestContainerRemoval:
    """Removal by name is the only thing that stops a runaway container outliving its
    timeout, so a removal that fails for any real reason has to be loud and retried."""

    @pytest.fixture(autouse=True)
    def quick_retry(self, monkeypatch):
        monkeypatch.setattr(docker_module, "REMOVE_RETRY_DELAY_SECONDS", 0.01)

    @staticmethod
    def removals(log: Path) -> list[str]:
        return [line for line in calls(log) if line.startswith("rm ")]

    @staticmethod
    def warnings(logs) -> list[dict]:
        return [e for e in logs if e["event"] == "verify.docker.remove.failed"]

    async def test_no_such_container_is_the_harmless_miss(self, fake_docker, tmp_path):
        binary, log = fake_docker("sleep 30", rm_body='echo "Error: No such container: x" >&2; exit 1')

        with structlog.testing.capture_logs() as logs:
            result = await run(backend_for(binary), tmp_path, timeout=0.5)

        assert result.timed_out
        assert len(self.removals(log)) == 1  # not retried
        assert self.warnings(logs) == []

    async def test_any_other_failure_is_a_warning_and_is_retried_once(self, fake_docker, tmp_path):
        binary, log = fake_docker("sleep 30", rm_body='echo "Error: permission denied" >&2; exit 1')

        with structlog.testing.capture_logs() as logs:
            result = await run(backend_for(binary), tmp_path, timeout=0.5)

        assert result.timed_out and result.error is None  # still not an error for the script
        assert len(self.removals(log)) == 2
        failures = self.warnings(logs)
        assert [e["attempt"] for e in failures] == [1, 2]
        assert all(e["log_level"] == "warning" and e["container"] == NAME for e in failures)
        assert "permission denied" in failures[0]["detail"]

    async def test_a_retry_that_succeeds_ends_quietly(self, fake_docker, tmp_path):
        state = tmp_path / "tried-once"
        binary, log = fake_docker(
            "sleep 30",
            rm_body=f'if [ -f {state} ]; then exit 0; else touch {state}; echo "boom" >&2; exit 1; fi',
        )

        with structlog.testing.capture_logs() as logs:
            await run(backend_for(binary), tmp_path, timeout=0.5)

        assert len(self.removals(log)) == 2
        assert [e["attempt"] for e in self.warnings(logs)] == [1]

    async def test_a_successful_first_removal_is_not_repeated(self, fake_docker, tmp_path):
        binary, log = fake_docker("sleep 30", rm_exit=0)

        await run(backend_for(binary), tmp_path, timeout=0.5)

        assert len(self.removals(log)) == 1

    async def test_the_logged_detail_is_redacted(self, fake_docker, tmp_path):
        secret = "ghp_" + "a" * 36
        binary, _log = fake_docker("sleep 30", rm_body=f'echo "denied {secret}" >&2; exit 1')

        with structlog.testing.capture_logs() as logs:
            await run(backend_for(binary), tmp_path, timeout=0.5)

        assert secret not in repr(self.warnings(logs))


FAKE_CONTAINER = """#!{python}
import json, os, sys

MODE = {mode!r}
SIBLING = {sibling!r}
CALLS = {calls!r}

args = sys.argv[1:]
if not args or args[0] != "run":
    sys.exit(0)

results = None
for previous, current in zip(args, args[1:]):
    if previous == "-v" and current.endswith(":/results"):
        results = current[: -len(":/results")]

# Like `docker run -e NAME` with no value: forward the variable from the CLI's own
# environment, and only if the argv asked for it.
forwarded = "REPOLACE_RUN_NONCE" in args
nonce = os.environ.get("REPOLACE_RUN_NONCE") if forwarded else None
with open(CALLS, "w") as handle:
    json.dump(
        {{
            "argv": args,
            "nonce_in_cli_env": os.environ.get("REPOLACE_RUN_NONCE"),
            "docker_host": os.environ.get("DOCKER_HOST"),
            "leaked_secret": os.environ.get("GITHUB_APP_PRIVATE_KEY_BASE64"),
        }},
        handle,
    )

report = os.path.join(results, "report.jsonl")
if MODE == "symlink":
    os.symlink(SIBLING, report)
    sys.exit(1)

stamp = {{"stale": "0000", "none": None}}.get(MODE, nonce)
records = [
    {{"kind": "start", "v": 1, "rootdir": "/repo", "ini": {{}}, "plugins": []}},
    {{"kind": "test", "nodeid": "t.py::ok", "when": "call", "outcome": "passed"}},
    {{"kind": "session", "v": 1, "exitstatus": 0}},
]
with open(report, "w") as handle:
    for record in records:
        if stamp is not None and record["kind"] in ("start", "session"):
            record["nonce"] = stamp
        handle.write(json.dumps(record) + "\\n")
sys.exit(0)
"""


@pytest.fixture
def fake_container(tmp_path):
    """A stand-in `docker` that behaves like a container running the plugin.

    It reads the results directory and the run nonce out of the argv it was given --
    exactly what the real container is handed -- and writes a report there stamped
    with that nonce, or misbehaves as `mode` says: a `stale` nonce (another run's),
    `none` at all, or a `symlink` planted in place of the report.
    """
    import sys

    def make(mode: str = "honest", sibling: Path | None = None) -> str:
        path = tmp_path / "docker"
        path.write_text(
            FAKE_CONTAINER.format(
                python=sys.executable, mode=mode, sibling=str(sibling or ""),
                calls=str(tmp_path / "container-calls.json"),
            )
        )
        path.chmod(0o755)
        return str(path)

    return make


async def run_suite_through(backend, tmp_path, *, name="results-0"):
    results = tmp_path / name
    results.mkdir(exist_ok=True)
    result = await backend.run_tests(
        ENV, tmp_path / "export-0", results, RepoSpec(key="a/b"), container_name=NAME
    )
    return result, results


class TestRunTestsCarriesAndChecksTheNonce:
    async def test_the_nonce_it_hands_the_container_is_the_one_it_requires_back(
        self, fake_container, tmp_path
    ):
        result, _ = await run_suite_through(backend_for(fake_container("honest")), tmp_path)

        assert result.error is None, result.error
        assert result.passed == ("t.py::ok",)

    async def test_the_value_is_in_the_clis_environment_and_never_in_its_argv(self, fake_container, tmp_path):
        """Argv is readable by every user on the host and is logged when a run times out;
        a process environment is not. The argv asks docker to forward the name."""
        await run_suite_through(backend_for(fake_container("honest")), tmp_path)

        seen = json.loads((tmp_path / "container-calls.json").read_text())
        nonce = seen["nonce_in_cli_env"]

        assert isinstance(nonce, str) and len(nonce) == 32
        assert "REPOLACE_RUN_NONCE" in seen["argv"]
        assert not any(nonce in token for token in seen["argv"])
        assert not any(token.startswith("REPOLACE_RUN_NONCE=") for token in seen["argv"])

    async def test_adding_the_nonce_does_not_change_which_other_variables_the_cli_gets(
        self, fake_container, tmp_path, monkeypatch
    ):
        """The CLI's environment is the allowlisted one *plus* the nonce. Dropping the rest
        would lose `DOCKER_HOST` and fall back to the rootful socket; inheriting the rest
        would hand it the App private key."""
        monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", "not-a-real-key")

        await run_suite_through(backend_for(fake_container("honest")), tmp_path)

        seen = json.loads((tmp_path / "container-calls.json").read_text())
        assert seen["docker_host"] == "unix:///run/user/1000/docker.sock"
        assert seen["leaked_secret"] is None

    async def test_without_the_forwarding_flag_the_container_would_get_no_nonce(
        self, fake_container, tmp_path
    ):
        """The fake only forwards when asked, as docker does -- so the honest runs above
        pass *because* the argv asks, not by accident."""
        backend = backend_for(fake_container("honest"))
        real_build = docker_module.build_run_argv

        def without_forwarding(*args, **kwargs):
            kwargs["forward_nonce"] = False
            return real_build(*args, **kwargs)

        docker_module.build_run_argv = without_forwarding
        try:
            result, _ = await run_suite_through(backend, tmp_path)
        finally:
            docker_module.build_run_argv = real_build

        assert "not written by this run" in result.error

    async def test_each_run_gets_a_fresh_nonce(self, fake_container, tmp_path):
        backend = backend_for(fake_container("honest"))
        seen = []
        real_parse = docker_module.parse_report

        def spy(*args, nonce=None, **kwargs):
            seen.append(nonce)
            return real_parse(*args, nonce=nonce, **kwargs)

        docker_module.parse_report = spy
        try:
            await run_suite_through(backend, tmp_path, name="results-0")
            await run_suite_through(backend, tmp_path, name="results-1")
        finally:
            docker_module.parse_report = real_parse

        assert len(seen) == 2 and seen[0] != seen[1]
        assert all(isinstance(n, str) and len(n) == 32 for n in seen)

    async def test_a_report_stamped_by_another_run_is_refused(self, fake_container, tmp_path):
        result, _ = await run_suite_through(backend_for(fake_container("stale")), tmp_path)

        assert result.error is not None and "not written by this run" in result.error
        assert result.passed == ()

    async def test_a_report_with_no_nonce_is_refused(self, fake_container, tmp_path):
        result, _ = await run_suite_through(backend_for(fake_container("none")), tmp_path)

        assert "not written by this run" in result.error

    async def test_a_report_planted_as_a_symlink_to_a_sibling_run_is_refused(
        self, fake_container, tmp_path
    ):
        """The attack: the probe's report path points at the baseline's, which
        carries the hidden tests' results and a nonce that is not this run's."""
        baseline = tmp_path / "results-baseline"
        baseline.mkdir()
        (baseline / "report.jsonl").write_text(
            json.dumps({"kind": "start", "v": 1, "nonce": "baseline-nonce"}) + "\n"
            + json.dumps({"kind": "test", "nodeid": "hidden.py::oracle", "when": "call", "outcome": "failed"}) + "\n"
            + json.dumps({"kind": "session", "v": 1, "exitstatus": 1, "nonce": "baseline-nonce"}) + "\n"
        )
        binary = fake_container("symlink", sibling=baseline / "report.jsonl")

        result, _ = await run_suite_through(backend_for(binary), tmp_path, name="results-probe-1")

        assert result.error is not None and "not a regular file" in result.error
        assert "oracle" not in repr(result)
        assert result.failed == ()

    async def test_the_error_names_no_host_path(self, fake_container, tmp_path):
        result, _ = await run_suite_through(backend_for(fake_container("stale")), tmp_path)

        assert str(tmp_path) not in repr(result)

    async def test_the_report_is_parsed_off_the_event_loop(self, fake_container, tmp_path, monkeypatch):
        """A hostile 66 MB report took 3 seconds to parse; on the loop that stalls
        every other task for each probe."""
        threads = []
        real_parse = docker_module.parse_report

        def spy(*args, **kwargs):
            threads.append(threading.get_ident())
            return real_parse(*args, **kwargs)

        monkeypatch.setattr(docker_module, "parse_report", spy)

        await run_suite_through(backend_for(fake_container("honest")), tmp_path)

        assert threads and threads[0] != threading.get_ident()

    @pytest.mark.parametrize("value", [0, -5.0])
    async def test_a_non_positive_spec_timeout_is_refused_not_defaulted(self, fake_container, tmp_path, value):
        backend = backend_for(fake_container("honest"))
        results = tmp_path / "results-0"
        results.mkdir()

        with pytest.raises(ValueError, match="positive"):
            await backend.run_tests(
                ENV, tmp_path / "export-0", results,
                RepoSpec(key="a/b", timeout_seconds=value), container_name=NAME,
            )

    async def test_a_spec_with_no_timeout_gets_the_default(self, fake_container, tmp_path):
        result, _ = await run_suite_through(backend_for(fake_container("honest")), tmp_path)

        assert result.error is None
