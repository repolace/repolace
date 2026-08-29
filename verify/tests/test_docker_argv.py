"""The sandbox's flags, asserted one at a time.

Every one of these fails *open*. A misspelled `--secutiry-opt` is rejected by
the CLI and shows up immediately; a dropped `--network=none` is simply a
container with network, and nothing about the run looks any different. So the
assertions are individual and literal rather than a single comparison against a
golden argv, which would go stale and get regenerated rather than read.
"""

from pathlib import Path

import pytest

from verify.backends.docker import build_argv, build_run_argv, sanitized_docker_env
from verify.config import DockerConfig, DockerLimits
from verify.protocol import EnvironmentRef, RepoSpec

ENV = EnvironmentRef(backend="docker", identifier="repolace-verify:a_b-cafe")
SOURCE = Path("/tmp/export-0")
RESULTS = Path("/tmp/results-0")


def argv(config: DockerConfig | None = None, spec: RepoSpec | None = None) -> tuple[str, ...]:
    return build_run_argv(
        config or DockerConfig(), spec or RepoSpec(key="a/b"), ENV, SOURCE, RESULTS, "c-0"
    )


def has_pair(args: tuple[str, ...], flag: str, value: str) -> bool:
    """A `--flag value` pair, in that order and adjacent."""
    return any(a == flag and b == value for a, b in zip(args, args[1:]))


class TestContainment:
    def test_the_network_is_off(self):
        assert "--network=none" in argv()

    def test_the_network_cannot_be_turned_on_by_a_spec(self):
        """There is deliberately no field for it. A repo whose suite needs the
        network is a repo whose suite can exfiltrate, and the install step is
        where network belongs."""
        crafted = RepoSpec(key="a/b", extra_pytest_args=("--network=bridge",))

        assert argv(spec=crafted).count("--network=none") == 1
        assert "--network=bridge" not in argv(spec=crafted)[: argv(spec=crafted).index(ENV.identifier)]

    def test_the_root_filesystem_is_read_only(self):
        assert "--read-only" in argv()

    def test_all_capabilities_are_dropped(self):
        assert "--cap-drop=ALL" in argv()

    def test_privilege_escalation_is_blocked(self):
        assert "--security-opt=no-new-privileges" in argv()

    def test_it_does_not_run_as_root(self):
        assert has_pair(argv(), "--user", "65534:65534")

    def test_tmp_exists_and_is_capped(self):
        """pytest's own tmp_path lives there, so it has to exist under --read-only."""
        assert "--tmpfs=/tmp:rw,nosuid,nodev,size=256m" in argv()


class TestLimits:
    def test_memory_is_capped(self):
        assert has_pair(argv(), "--memory", "2g")

    def test_swap_equals_memory_so_the_cap_cannot_be_escaped(self):
        args = argv()
        memory = args[args.index("--memory") + 1]

        assert has_pair(args, "--memory-swap", memory)

    def test_cpu_is_capped(self):
        assert has_pair(argv(), "--cpus", "2.0")

    def test_processes_are_capped(self):
        assert has_pair(argv(), "--pids-limit", "512")

    def test_file_descriptors_are_capped(self):
        assert has_pair(argv(), "--ulimit", "nofile=4096:4096")

    def test_the_daemons_json_log_is_capped(self):
        """It is on the host disk and unbounded by default -- a verbose suite
        would otherwise fill the volume Postgres lives on."""
        assert has_pair(argv(), "--log-opt", "max-size=10m")
        assert has_pair(argv(), "--log-opt", "max-file=1")

    def test_cpuset_is_absent_unless_configured(self):
        assert "--cpuset-cpus" not in argv()

    def test_cpuset_is_passed_when_configured(self):
        config = DockerConfig(limits=DockerLimits(cpuset_cpus="0-1"))

        assert has_pair(argv(config), "--cpuset-cpus", "0-1")

    def test_zombies_are_reaped(self):
        """Without an init, a suite that spawns background processes leaves
        zombies holding the pid cgroup until the cap trips."""
        assert "--init" in argv()


class TestRuntime:
    def test_gvisor_is_off_by_default(self):
        assert "--runtime" not in argv()

    def test_gvisor_is_one_flag_when_asked_for(self):
        """The point of the semantic seam: stronger isolation is a config change
        rather than a new backend."""
        assert has_pair(argv(DockerConfig(runtime="runsc")), "--runtime", "runsc")


class TestMounts:
    def test_the_source_is_mounted_writable_by_default(self):
        assert has_pair(argv(), "-v", f"{SOURCE}:/repo")

    def test_the_source_can_be_mounted_read_only(self):
        spec = RepoSpec(key="a/b", repo_readonly=True)

        assert has_pair(argv(spec=spec), "-v", f"{SOURCE}:/repo:ro")

    def test_the_results_go_somewhere_the_suite_does_not_own(self):
        """Separate from the export, so a suite that scribbles over its working
        directory cannot destroy the report that says what it did."""
        assert has_pair(argv(), "-v", f"{RESULTS}:/results")

    def test_the_working_directory_is_the_repo(self):
        assert has_pair(argv(), "-w", "/repo")


class TestEnvironment:
    def test_the_plugin_knows_where_to_write(self):
        assert has_pair(argv(), "-e", "REPOLACE_REPORT_PATH=/results/report.jsonl")

    def test_home_is_writable(self):
        """Under --read-only, anything writing a dotfile into a nonexistent HOME
        fails in a way that reads as a test failure."""
        assert has_pair(argv(), "-e", "HOME=/tmp")

    def test_the_plugin_is_importable(self):
        assert has_pair(argv(), "-e", "PYTHONPATH=/opt/repolace")

    def test_plugin_autoload_is_left_alone_by_default(self):
        """Disabling it makes a run reproducible and breaks any suite built on
        pytest-django or pytest-asyncio -- unscoreable, for a reason that has
        nothing to do with the agent."""
        assert not any("PYTEST_DISABLE_PLUGIN_AUTOLOAD" in a for a in argv())

    def test_plugin_autoload_can_be_disabled_per_repo(self):
        spec = RepoSpec(key="a/b", disable_plugin_autoload=True)

        assert has_pair(argv(spec=spec), "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1")

    def test_extra_env_is_passed(self):
        spec = RepoSpec(key="a/b", extra_env={"TZ": "UTC"})

        assert has_pair(argv(spec=spec), "-e", "TZ=UTC")


class TestCommand:
    def test_the_entrypoint_is_explicit(self):
        """So a base image carrying its own ENTRYPOINT cannot turn the pytest
        invocation into arguments for something else."""
        assert has_pair(argv(), "--entrypoint", "python")

    def test_the_interpreter_is_configurable_for_a_non_python_base_image(self):
        spec = RepoSpec(key="a/b", python_executable="python3")

        assert has_pair(argv(spec=spec), "--entrypoint", "python3")

    def test_everything_after_the_image_is_the_command(self):
        args = argv()
        command = args[args.index(ENV.identifier) + 1 :]

        assert command[:2] == ("-m", "pytest")

    def test_the_plugin_is_loaded(self):
        assert has_pair(argv(), "-p", "_repolace_report")

    def test_the_pytest_cache_goes_to_the_tmpfs_not_the_export(self):
        """Left at its default it lands in the bind-mounted export, owned by the
        sandbox uid, and the host can no longer delete its own temp tree."""
        assert has_pair(argv(), "-o", "cache_dir=/tmp/.pytest_cache")

    def test_the_cache_redirect_survives_keep_addopts(self):
        """It is a cleanup property, not an addopts one -- a repo that needs its
        own addopts still must not leak a directory per task."""
        assert has_pair(argv(spec=RepoSpec(key="a/b", keep_addopts=True)), "-o",
                        "cache_dir=/tmp/.pytest_cache")

    def test_addopts_are_cleared_by_default(self):
        """A repo's own addopts -- a coverage gate, `-x`, `--strict` -- would
        otherwise decide the exit code."""
        assert has_pair(argv(), "-o", "addopts=")

    def test_addopts_survive_when_the_repo_needs_them(self):
        assert not has_pair(argv(spec=RepoSpec(key="a/b", keep_addopts=True)), "-o", "addopts=")

    def test_test_targets_and_extra_args_come_last_and_in_order(self):
        spec = RepoSpec(key="a/b", test_targets=("tests",), extra_pytest_args=("-x",))
        args = argv(spec=spec)

        assert args[-2:] == ("tests", "-x")

    def test_the_container_is_named_so_it_can_be_killed(self):
        """`run_process` kills the client; the container is a child of the
        daemon and survives, so cleanup addresses it by name."""
        assert has_pair(argv(), "--name", "c-0")


class TestBuild:
    def test_the_build_tags_and_points_at_the_context(self, tmp_path):
        args = build_argv(DockerConfig(), "repolace-verify:a_b-cafe", tmp_path)

        assert has_pair(args, "--tag", "repolace-verify:a_b-cafe")
        assert has_pair(args, "--file", str(tmp_path / "Dockerfile"))
        assert args[-1] == str(tmp_path)


class TestEnvironmentAllowlist:
    def test_the_app_private_key_is_not_inherited(self, monkeypatch):
        """The worst credential in the process: it mints installation tokens for
        every installation, and rotating a token does not revoke it."""
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", "secret")
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@host/db")

        env = sanitized_docker_env()

        assert "GITHUB_APP_PRIVATE_KEY_BASE64" not in env
        assert "DATABASE_URL" not in env

    def test_the_rootless_socket_is_inherited(self, monkeypatch):
        """Dropping DOCKER_HOST would silently fall back to the rootful socket,
        which is the posture CLAUDE.md rejects."""
        monkeypatch.setenv("DOCKER_HOST", "unix:///run/user/1000/docker.sock")

        assert sanitized_docker_env()["DOCKER_HOST"] == "unix:///run/user/1000/docker.sock"
