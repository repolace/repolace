"""The sandbox's flags, asserted one at a time.

Every one of these fails *open*. A misspelled `--secutiry-opt` is rejected by
the CLI and shows up immediately; a dropped `--network=none` is simply a
container with network, and nothing about the run looks any different. So the
assertions are individual and literal rather than a single comparison against a
golden argv, which would go stale and get regenerated rather than read.
"""

from pathlib import Path

import pytest

from verify.backends.docker import (
    build_argv,
    build_run_argv,
    build_script_argv,
    sanitized_docker_env,
)
from verify.config import RESERVED_ENV, DockerConfig, DockerLimits
from verify.protocol import EnvironmentRef, RepoSpec

ENV = EnvironmentRef(backend="docker", identifier="repolace-verify:a_b-cafe")
SOURCE = Path("/tmp/export-0")
RESULTS = Path("/tmp/results-0")
SCRIPT = Path("/tmp/results-script-1/_repolace_script.py")


def argv(config: DockerConfig | None = None, spec: RepoSpec | None = None) -> tuple[str, ...]:
    return build_run_argv(
        config or DockerConfig(), spec or RepoSpec(key="a/b"), ENV, SOURCE, RESULTS, "c-0"
    )


def script_argv(config: DockerConfig | None = None, spec: RepoSpec | None = None) -> tuple[str, ...]:
    return build_script_argv(
        config or DockerConfig(), spec or RepoSpec(key="a/b"), ENV, SOURCE, SCRIPT, "c-0"
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


# --- the scratch-script path ------------------------------------------------
#
# The two builders share one containment list, and the tests below are what stop
# a flag being dropped from one of them. Each is parametrised over both builders
# so a failure names the flag *and* the builder, and the structural test at the end
# pins the property the individual ones only sample: that nothing before the first
# mount differs between them.

BUILDERS = {"pytest": argv, "script": script_argv}


@pytest.fixture(params=sorted(BUILDERS))
def build(request):
    """Either builder, called as `build(config, spec)`."""
    return BUILDERS[request.param]


class TestContainmentOnBothBuilders:
    def test_the_network_is_off(self, build):
        assert "--network=none" in build()

    def test_the_network_cannot_be_turned_on_by_a_spec(self, build):
        crafted = RepoSpec(key="a/b", extra_pytest_args=("--network=bridge",), extra_env={"X": "1"})
        args = build(spec=crafted)

        assert args.count("--network=none") == 1
        assert "--network=bridge" not in args[: args.index(ENV.identifier)]

    def test_the_root_filesystem_is_read_only(self, build):
        assert "--read-only" in build()

    def test_tmp_is_a_capped_tmpfs(self, build):
        assert "--tmpfs=/tmp:rw,nosuid,nodev,size=256m" in build()

    def test_all_capabilities_are_dropped(self, build):
        assert "--cap-drop=ALL" in build()

    def test_privilege_escalation_is_blocked(self, build):
        assert "--security-opt=no-new-privileges" in build()

    def test_it_does_not_run_as_root(self, build):
        assert has_pair(build(), "--user", "65534:65534")

    def test_memory_is_capped(self, build):
        assert has_pair(build(), "--memory", "2g")

    def test_swap_equals_memory(self, build):
        args = build()

        assert has_pair(args, "--memory-swap", args[args.index("--memory") + 1])

    def test_cpu_is_capped(self, build):
        assert has_pair(build(), "--cpus", "2.0")

    def test_processes_are_capped(self, build):
        assert has_pair(build(), "--pids-limit", "512")

    def test_file_descriptors_are_capped(self, build):
        assert has_pair(build(), "--ulimit", "nofile=4096:4096")

    def test_the_log_is_capped(self, build):
        assert has_pair(build(), "--log-opt", "max-size=10m")
        assert has_pair(build(), "--log-opt", "max-file=1")

    def test_zombies_are_reaped(self, build):
        assert "--init" in build()

    def test_the_image_is_never_pulled_at_run_time(self, build):
        """A tag missing locally -- a pruned image, a typo in a spec -- would
        otherwise be fetched from a registry on the one path that must have no
        network."""
        assert "--pull=never" in build()

    def test_the_container_is_named_so_it_can_be_killed(self, build):
        """`run_process` kills the client; the container survives, so cleanup
        addresses it by name -- for a script as much as for a suite."""
        assert has_pair(build(), "--name", "c-0")

    def test_cpuset_is_passed_when_configured(self, build):
        assert has_pair(build(DockerConfig(limits=DockerLimits(cpuset_cpus="0-1"))), "--cpuset-cpus", "0-1")

    def test_cpuset_is_absent_unless_configured(self, build):
        assert "--cpuset-cpus" not in build()

    def test_the_runtime_is_passed_when_configured(self, build):
        assert has_pair(build(DockerConfig(runtime="runsc")), "--runtime", "runsc")

    def test_the_runtime_is_absent_unless_configured(self, build):
        assert "--runtime" not in build()

    def test_the_interpreter_is_the_explicit_entrypoint(self, build):
        assert has_pair(build(), "--entrypoint", "python")
        assert has_pair(build(spec=RepoSpec(key="a/b", python_executable="python3")), "--entrypoint", "python3")

    def test_home_is_writable(self, build):
        assert has_pair(build(), "-e", "HOME=/tmp")

    def test_the_working_directory_is_the_repo(self, build):
        assert has_pair(build(), "-w", "/repo")

    def test_extra_env_is_passed(self, build):
        assert has_pair(build(spec=RepoSpec(key="a/b", extra_env={"TZ": "UTC"})), "-e", "TZ=UTC")


class TestTheTwoBuildersShareOneContainmentList:
    """The property the per-flag tests above only sample."""

    @pytest.mark.parametrize(
        "config",
        [
            DockerConfig(),
            DockerConfig(runtime="runsc"),
            DockerConfig(limits=DockerLimits(cpuset_cpus="0-1", memory="1g", memory_swap="1g")),
        ],
        ids=["default", "runtime", "cpuset-and-memory"],
    )
    def test_everything_before_the_first_mount_is_identical(self, config):
        """A flag added to one path and not the other makes these differ. Compared
        up to the first `-v`, which is where the shared list ends. Both are given
        the same container name, which is the one per-run value in the prefix."""
        pytest_args, script_args = argv(config), script_argv(config)

        pytest_prefix = pytest_args[: pytest_args.index("-v")]
        script_prefix = script_args[: script_args.index("-v")]

        assert pytest_prefix == script_prefix
        assert "--network=none" in pytest_prefix  # not vacuous: the prefix has substance

    def test_a_spec_cannot_change_the_shared_part(self):
        """The shared part reads only the config -- a spec field that reached it
        would be a per-repository switch on a containment control."""
        hostile = RepoSpec(
            key="a/b", repo_readonly=True, keep_addopts=True, disable_plugin_autoload=True,
            extra_env={"A": "1"}, test_targets=("t",), extra_pytest_args=("--x",),
        )
        plain = RepoSpec(key="a/b")

        for builder in (argv, script_argv):
            assert builder(spec=hostile)[: builder(spec=hostile).index("-v")] == builder(spec=plain)[
                : builder(spec=plain).index("-v")
            ]


class TestScriptMounts:
    def test_the_source_is_read_only(self):
        assert has_pair(script_argv(), "-v", f"{SOURCE}:/repo:ro")

    def test_the_source_is_read_only_even_when_the_spec_says_writable(self):
        """Unconditional: a spec is per-repository data, and a protection data can
        switch off is only as trustworthy as the data."""
        spec = RepoSpec(key="a/b", repo_readonly=False)

        assert has_pair(script_argv(spec=spec), "-v", f"{SOURCE}:/repo:ro")
        assert not has_pair(script_argv(spec=spec), "-v", f"{SOURCE}:/repo")

    def test_the_script_is_mounted_read_only_at_a_fixed_path_outside_the_tree(self):
        assert has_pair(script_argv(), "-v", f"{SCRIPT}:/scratch/_repolace_script.py:ro")

    def test_there_are_exactly_two_mounts(self):
        args = script_argv()

        assert sum(1 for a in args if a == "-v") == 2

    def test_nothing_is_mounted_for_results(self):
        args = script_argv()

        assert not any(":/results" in a for a in args)
        assert not any("REPOLACE_REPORT_PATH" in a for a in args)

    def test_the_pytest_path_still_mounts_its_results(self):
        """The contrast that gives the previous test meaning."""
        assert has_pair(argv(), "-v", f"{RESULTS}:/results")


class TestScriptCommand:
    def test_the_command_after_the_image_is_exactly_the_script(self):
        args = script_argv()

        assert args[args.index(ENV.identifier) + 1 :] == ("/scratch/_repolace_script.py",)

    def test_it_is_not_run_under_pytest(self):
        args = script_argv()

        assert "pytest" not in args and "-m" not in args and "_repolace_report" not in args

    def test_the_source_root_is_importable(self):
        """A script's `sys.path[0]` is `/scratch`, so a flat-layout package would
        otherwise not import."""
        assert has_pair(script_argv(), "-e", "PYTHONPATH=/repo")

    def test_the_pytest_plugin_directory_is_not_on_the_path(self):
        assert not any("/opt/repolace" in a for a in script_argv())

    def test_a_specs_test_targets_and_pytest_args_do_not_reach_the_script(self):
        spec = RepoSpec(key="a/b", test_targets=("tests",), extra_pytest_args=("-x",))
        args = script_argv(spec=spec)

        assert "tests" not in args and "-x" not in args

    def test_the_image_is_the_environment_that_was_prepared(self):
        assert ENV.identifier in script_argv()

    def test_the_image_comes_after_every_flag(self):
        """Anything after the image is an argument to the interpreter, so a flag
        placed there would be silently ignored rather than rejected."""
        args = script_argv()
        flags = [i for i, a in enumerate(args) if a in {"-v", "-e", "-w", "--user", "--memory"}]

        assert max(flags) < args.index(ENV.identifier)


class TestRootdirIsPinned:
    def test_it_follows_the_plugin_and_precedes_everything_else_pytest_is_given(self):
        args = argv(spec=RepoSpec(key="a/b", test_targets=("tests",), extra_pytest_args=("-x",)))
        plugin = args.index("_repolace_report")

        assert args[plugin - 1] == "-p"
        assert args[plugin + 1] == "--rootdir=/repo"
        assert args.index("--rootdir=/repo") < args.index("tests")

    @pytest.mark.parametrize("keep_addopts", [True, False])
    @pytest.mark.parametrize("targets", [(), ("tests",), ("tests/test_a.py::test_one",)])
    def test_it_is_always_there_not_only_when_targets_are_overridden(self, keep_addopts, targets):
        """One node-id space for the baseline, every attempt and every probe."""
        spec = RepoSpec(key="a/b", keep_addopts=keep_addopts, test_targets=targets)

        assert argv(spec=spec).count("--rootdir=/repo") == 1

    def test_the_script_path_has_no_pytest_to_pin(self):
        assert not any(a.startswith("--rootdir") for a in script_argv())


class TestScriptNameIsOneNoRepositoryHas:
    def test_the_script_is_not_called_main(self):
        """`sys.path[0]` is the script's directory, ahead of `PYTHONPATH`, so a
        script called `main.py` would shadow a repository's own `main.py`."""
        args = script_argv()

        assert not any(a.endswith("main.py") or ":/scratch/main" in a for a in args)
        assert args[-1] == "/scratch/_repolace_script.py"


class TestTheRunNonce:
    """The container is told to *forward* the nonce (`-e REPOLACE_RUN_NONCE`, no value); the
    value rides in the docker CLI's environment, never its argv, which `ps` shows to every
    user and which is logged when a run times out."""

    @staticmethod
    def forwarding(config=None, spec=None) -> tuple[str, ...]:
        return build_run_argv(
            config or DockerConfig(), spec or RepoSpec(key="a/b"), ENV, SOURCE, RESULTS, "c-0",
            forward_nonce=True,
        )

    def test_the_name_is_forwarded(self):
        assert has_pair(self.forwarding(), "-e", "REPOLACE_RUN_NONCE")

    def test_no_value_is_in_the_argv(self):
        """An `-e NAME=VALUE` spelling would carry it; the bare name is the point."""
        args = self.forwarding()

        assert not any(a.startswith("REPOLACE_RUN_NONCE=") for a in args)
        assert "REPOLACE_RUN_NONCE" in args
        assert args[args.index("REPOLACE_RUN_NONCE") - 1] == "-e"

    def test_it_comes_before_the_image(self):
        args = self.forwarding()

        assert args.index("REPOLACE_RUN_NONCE") < args.index(ENV.identifier)

    def test_it_is_absent_unless_asked_for(self):
        assert not any("REPOLACE_RUN_NONCE" in a for a in argv())

    def test_a_script_has_no_report_and_so_no_nonce(self):
        assert not any("REPOLACE_RUN_NONCE" in a for a in script_argv())

    def test_the_argv_is_otherwise_unchanged_by_it(self):
        stamped = self.forwarding()
        position = stamped.index("REPOLACE_RUN_NONCE")

        assert stamped[: position - 1] + stamped[position + 1 :] == argv()

    def test_the_old_keyword_is_gone(self):
        """A caller still passing the value would put it back in argv; make that an error."""
        with pytest.raises(TypeError):
            build_run_argv(
                DockerConfig(), RepoSpec(key="a/b"), ENV, SOURCE, RESULTS, "c-0", nonce="abc123"  # type: ignore[call-arg]
            )


class TestReservedEnvironment:
    """The sandbox sets these for itself, and a spec may not name them: each carries a
    decision, and an override would fail open as a quietly different run."""

    @pytest.mark.parametrize("name", sorted(RESERVED_ENV))
    def test_a_spec_cannot_set_it_on_either_builder(self, build, name):
        spec = RepoSpec(key="a/b", extra_env={name: "/elsewhere"})

        with pytest.raises(ValueError, match=name):
            build(spec=spec)

    def test_the_error_names_every_offender_and_the_spec(self, build):
        spec = RepoSpec(key="acme/x", extra_env={"HOME": "/h", "PYTHONPATH": "/p", "TZ": "UTC"})

        with pytest.raises(ValueError) as excinfo:
            build(spec=spec)

        assert "acme/x" in str(excinfo.value)
        assert "HOME" in str(excinfo.value) and "PYTHONPATH" in str(excinfo.value)
        assert "TZ" not in str(excinfo.value)

    def test_an_ordinary_variable_is_still_fine(self, build):
        assert has_pair(build(spec=RepoSpec(key="a/b", extra_env={"TZ": "UTC"})), "-e", "TZ=UTC")

    def test_the_reserved_set_is_the_four_the_sandbox_owns(self):
        assert RESERVED_ENV == {"PYTHONPATH", "HOME", "REPOLACE_REPORT_PATH", "REPOLACE_RUN_NONCE"}

    def test_the_reserved_names_are_still_set_by_the_sandbox_itself(self):
        args = build_run_argv(
            DockerConfig(), RepoSpec(key="a/b"), ENV, SOURCE, RESULTS, "c-0", forward_nonce=True
        )

        for pair in ("HOME=/tmp", "PYTHONPATH=/opt/repolace", "REPOLACE_REPORT_PATH=/results/report.jsonl",
                     "REPOLACE_RUN_NONCE"):
            assert has_pair(args, "-e", pair), pair


class TestCollectionErrorsDoNotAbortTheSession:
    """Without the flag pytest stops at the first module that fails to import: exit 2,
    nothing run, every visible result lost. `test_collection_errors_real_pytest.py`
    shows the consequence against real pytest; these pin that the argv carries it."""

    FLAG = "--continue-on-collection-errors"

    def test_it_is_always_there(self):
        assert self.FLAG in argv()

    def test_it_comes_before_the_targets_and_the_extra_args(self):
        args = argv(spec=RepoSpec(key="a/b", test_targets=("tests",), extra_pytest_args=("-x",)))

        assert args.index(self.FLAG) < args.index("tests") < args.index("-x")
        assert args[-2:] == ("tests", "-x")

    @pytest.mark.parametrize("keep_addopts", [True, False])
    @pytest.mark.parametrize("autoload", [True, False])
    def test_no_spec_field_removes_it(self, keep_addopts, autoload):
        spec = RepoSpec(
            key="a/b", keep_addopts=keep_addopts, disable_plugin_autoload=autoload,
            repo_readonly=True, test_targets=("tests",), extra_pytest_args=("-p", "no:x"),
            extra_env={"PYTEST_ADDOPTS": "-x"},
        )

        assert argv(spec=spec).count(self.FLAG) == 1

    def test_it_is_not_given_twice_and_is_not_a_value_of_another_flag(self):
        args = argv()

        assert args.count(self.FLAG) == 1
        assert args[args.index(self.FLAG) - 1] not in {"-o", "-p", "--rootdir"}

    def test_it_is_after_the_plugin_and_the_rootdir(self):
        args = argv()

        assert args.index("_repolace_report") < args.index("--rootdir=/repo") < args.index(self.FLAG)

    def test_a_script_is_not_pytest_and_gets_no_pytest_flag(self):
        assert self.FLAG not in script_argv()


# --- an ADDED flag must fail too -------------------------------------------------
#
# Everything above asserts that a control is present, and a presence check passes
# when something is *added*: `--privileged` or `--network=host` appended before the
# image would leave every test green. The script path is where the agent gets a
# shell, so these pin the whole set of flags, written out literally so that changing
# it is a conscious edit of this file rather than a regenerated golden.

PYTEST_FLAGS_BEFORE_THE_IMAGE = [
    "--rm", "--pull=never", "--init", "--name", "--network=none", "--read-only",
    "--tmpfs=/tmp:rw,nosuid,nodev,size=256m", "--cap-drop=ALL", "--security-opt=no-new-privileges",
    "--user", "--memory", "--memory-swap", "--cpus", "--pids-limit", "--ulimit",
    "--log-opt", "--log-opt", "-v", "-v", "-w", "-e", "-e", "-e", "-e", "--entrypoint",
]

SCRIPT_FLAGS_BEFORE_THE_IMAGE = [
    "--rm", "--pull=never", "--init", "--name", "--network=none", "--read-only",
    "--tmpfs=/tmp:rw,nosuid,nodev,size=256m", "--cap-drop=ALL", "--security-opt=no-new-privileges",
    "--user", "--memory", "--memory-swap", "--cpus", "--pids-limit", "--ulimit",
    "--log-opt", "--log-opt", "-v", "-v", "-w", "-e", "-e", "--entrypoint",
]

PYTEST_ENV_BEFORE_THE_IMAGE = [
    "REPOLACE_REPORT_PATH=/results/report.jsonl", "REPOLACE_RUN_NONCE", "HOME=/tmp", "PYTHONPATH=/opt/repolace",
]
SCRIPT_ENV_BEFORE_THE_IMAGE = ["HOME=/tmp", "PYTHONPATH=/repo"]


def pytest_argv_with_nonce(config=None, spec=None) -> tuple[str, ...]:
    return build_run_argv(
        config or DockerConfig(), spec or RepoSpec(key="a/b"), ENV, SOURCE, RESULTS, "c-0",
        forward_nonce=True,
    )


def before_the_image(args: tuple[str, ...]) -> tuple[str, ...]:
    return args[: args.index(ENV.identifier)]


def option_tokens(args: tuple[str, ...]) -> list[str]:
    return [t for t in before_the_image(args) if t.startswith("-")]


def values_after(args: tuple[str, ...], flag: str) -> list[str]:
    before = before_the_image(args)
    return [b for a, b in zip(before, before[1:]) if a == flag]


BUILDERS_WITH_ALLOWLISTS = {
    "pytest": (pytest_argv_with_nonce, PYTEST_FLAGS_BEFORE_THE_IMAGE, PYTEST_ENV_BEFORE_THE_IMAGE,
               [f"{SOURCE}:/repo", f"{RESULTS}:/results"]),
    "script": (script_argv, SCRIPT_FLAGS_BEFORE_THE_IMAGE, SCRIPT_ENV_BEFORE_THE_IMAGE,
               [f"{SOURCE}:/repo:ro", f"{SCRIPT}:/scratch/_repolace_script.py:ro"]),
}


@pytest.fixture(params=sorted(BUILDERS_WITH_ALLOWLISTS))
def allowlisted(request):
    return BUILDERS_WITH_ALLOWLISTS[request.param]


class TestTheFlagsAreExactlyTheAllowlist:
    def test_the_option_tokens_before_the_image(self, allowlisted):
        build, flags, _env, _mounts = allowlisted

        assert option_tokens(build()) == flags

    def test_the_environment_the_container_is_given(self, allowlisted):
        build, _flags, env, _mounts = allowlisted

        assert values_after(build(), "-e") == env

    def test_the_mounts_the_container_is_given(self, allowlisted):
        build, _flags, _env, mounts = allowlisted

        assert values_after(build(), "-v") == mounts

    def test_an_added_flag_would_be_seen(self, allowlisted):
        """The property the allowlist exists for: appending a dangerous flag changes the
        token list, so the comparison above would fail."""
        build, flags, _env, _mounts = allowlisted
        args = build()
        tampered = args[: args.index(ENV.identifier)] + ("--privileged",) + args[args.index(ENV.identifier) :]

        assert option_tokens(tampered) != flags

    def test_a_config_option_does_not_add_flags_it_was_not_asked_for(self, allowlisted):
        build, flags, _env, _mounts = allowlisted

        assert option_tokens(build(DockerConfig(limits=DockerLimits(memory="1g", memory_swap="1g")))) == flags

    def test_the_two_optional_flags_are_the_only_ones_a_config_can_add(self, allowlisted):
        build, flags, _env, _mounts = allowlisted
        config = DockerConfig(runtime="runsc", limits=DockerLimits(cpuset_cpus="0-1"))

        assert sorted(set(option_tokens(build(config))) - set(flags)) == ["--cpuset-cpus", "--runtime"]


DENIED_FLAGS = (
    "--privileged", "--cap-add", "--pid", "--ipc", "--uts", "--device", "--userns", "--cgroupns",
    "--volumes-from", "--add-host", "--mount", "--sysctl", "--env-file", "--group-add", "--net",
    "--security-opt-override", "--storage-opt", "--gpus", "--publish", "-p", "-P", "--link",
)


def denied_tokens(args: tuple[str, ...]) -> list[str]:
    """Anything before the image that grants a capability the sandbox must never have."""
    found = []
    tokens = before_the_image(args)
    for index, token in enumerate(tokens):
        name = token.split("=", 1)[0]
        if name in DENIED_FLAGS:
            found.append(token)
        if name == "--network" and token != "--network=none":
            found.append(token)
        if name == "--security-opt" and token != "--security-opt=no-new-privileges":
            found.append(token)
        if "docker.sock" in token or token in {"/", "/var/run", "/run"} or token.startswith("/var/run/"):
            found.append(token)
        if token in {"--network", "--security-opt"}:  # the separate-value spelling
            found.append(token)
        if token == "-v" and not (tokens[index + 1].startswith(str(SOURCE)) or tokens[index + 1].startswith(str(RESULTS)) or tokens[index + 1].startswith(str(SCRIPT))):
            found.append(tokens[index + 1])
    return found


class TestNothingDangerousIsGranted:
    def test_the_defaults(self, build):
        assert denied_tokens(build()) == []

    def test_with_every_optional_setting_on(self, build):
        config = DockerConfig(runtime="runsc", limits=DockerLimits(cpuset_cpus="0-1"))
        spec = RepoSpec(
            key="a/b", repo_readonly=True, keep_addopts=True, disable_plugin_autoload=True,
            extra_env={"TZ": "UTC"}, test_targets=("tests",), extra_pytest_args=("-x",),
        )

        assert denied_tokens(build(config, spec)) == []

    @pytest.mark.parametrize(
        "added",
        [
            ("--privileged",), ("--cap-add=ALL",), ("--cap-add", "SYS_ADMIN"), ("--network=host",),
            ("--network", "host"), ("--net=host",), ("--pid=host",), ("--ipc=host",), ("--uts=host",),
            ("--device", "/dev/kmsg"), ("--userns=host",), ("--security-opt", "seccomp=unconfined"),
            ("--security-opt=seccomp=unconfined",), ("--security-opt=apparmor=unconfined",),
            ("-v", "/var/run/docker.sock:/var/run/docker.sock"), ("-v", "/:/host"),
            ("--mount", "type=bind,src=/,dst=/host"), ("--volumes-from", "other"),
            ("--add-host", "x:1.1.1.1"), ("--env-file", "/etc/environment"), ("-p", "80:80"),
        ],
        ids=lambda added: " ".join(added),
    )
    def test_the_detector_catches_each_dangerous_addition(self, build, added):
        """The denylist is only worth having if it fires. Insert each before the image
        and require that it is found."""
        args = build()
        index = args.index(ENV.identifier)
        tampered = args[:index] + added + args[index:]

        assert denied_tokens(tampered) != []


class TestAHostileSpecCannotChangeTheFlags:
    """`test_the_network_cannot_be_turned_on_by_a_spec` above puts its hostile value in
    `extra_pytest_args`, which lands after the image (or is unused), so it proves nothing.
    These put the value where a spec really can place it."""

    HOSTILE = "--network=host"

    @staticmethod
    def without_pair(args: tuple[str, ...], flag: str, value_prefix: str) -> tuple[str, ...]:
        """`args` with the one `flag value` pair whose value starts with `value_prefix` removed."""
        for index, (a, b) in enumerate(zip(args, args[1:])):
            if a == flag and b.startswith(value_prefix):
                return args[:index] + args[index + 2 :]
        raise AssertionError(f"no {flag} {value_prefix!r} in {args}")

    def test_a_hostile_env_value_adds_only_its_own_pair(self, build):
        plain = build()
        hostile = build(spec=RepoSpec(key="a/b", extra_env={"X": self.HOSTILE}))

        assert self.without_pair(hostile, "-e", "X=") == plain
        assert denied_tokens(hostile) == []

    def test_a_hostile_env_name_is_refused_outright(self, build):
        """Once only emitted harmlessly as the value of `-e`; a name that is not an
        identifier is now refused, which is stricter (see `TestEnvironmentNamesAreNames`)."""
        with pytest.raises(ValueError, match="names must match"):
            build(spec=RepoSpec(key="a/b", extra_env={"--privileged": "1"}))

    def test_a_hostile_interpreter_is_only_ever_the_entrypoints_value(self, build):
        """`--entrypoint --network=host image` hands docker an entrypoint, not a flag."""
        plain = build()
        hostile = build(spec=RepoSpec(key="a/b", python_executable=self.HOSTILE))
        position = hostile.index("--entrypoint")

        assert hostile[position + 1] == self.HOSTILE
        assert hostile[:position + 1] + ("python",) + hostile[position + 2 :] == plain

    def test_hostile_targets_and_pytest_args_are_after_the_image(self, build):
        plain = build()
        hostile = build(
            spec=RepoSpec(key="a/b", test_targets=(self.HOSTILE, "--privileged"),
                          extra_pytest_args=("--cap-add=ALL", "--network=host"))
        )

        assert before_the_image(hostile) == before_the_image(plain)
        assert denied_tokens(hostile) == []

    def test_a_hostile_base_image_changes_nothing_in_the_argv(self, build):
        """The image a run starts from is the prepared environment's tag, never the spec's."""
        assert build(spec=RepoSpec(key="a/b", base_image=self.HOSTILE)) == build()

    def test_a_hostile_spec_key_changes_nothing_in_the_argv(self, build):
        assert build(spec=RepoSpec(key="--privileged")) == build()

    def test_with_every_string_field_hostile_at_once_only_the_env_pair_and_the_entrypoint_differ(
        self, build
    ):
        plain = build()
        spec = RepoSpec(
            key=self.HOSTILE, base_image=self.HOSTILE, install=(self.HOSTILE,),
            system_packages=(self.HOSTILE,), python_executable=self.HOSTILE,
            test_targets=(self.HOSTILE,), extra_pytest_args=(self.HOSTILE,),
            extra_env={"X": self.HOSTILE},
        )
        hostile = build(spec=spec)

        stripped = self.without_pair(hostile, "-e", "X=")
        position = stripped.index("--entrypoint")
        stripped = stripped[: position + 1] + ("python",) + stripped[position + 2 :]

        assert before_the_image(stripped) == before_the_image(plain)
        # The interpreter value is a token like any other to `denied_tokens`, which cannot
        # tell it is docker's entrypoint argument, so check what is left once it is replaced.
        assert denied_tokens(stripped) == []


class TestEnvironmentNamesAreNames:
    """`-e NAME=VALUE` splits on the first `=`, so a *name* containing one is a way past
    an exact-key reserved check: `HOME=/evil` emits `-e HOME=/evil=v`, which docker applies
    after the sandbox's own `-e HOME=/tmp` and which wins."""

    PAYLOADS = ["HOME=/evil", "REPOLACE_RUN_NONCE=known#", "PYTHONPATH=/x:", "A=B", "REPOLACE_REPORT_PATH=/x"]

    @pytest.mark.parametrize("name", PAYLOADS)
    def test_a_name_that_smuggles_an_assignment_is_refused_on_either_builder(self, build, name):
        with pytest.raises(ValueError, match="names must match"):
            build(spec=RepoSpec(key="a/b", extra_env={name: "v"}))

    @pytest.mark.parametrize(
        "name", ["", "1A", "A B", "A-B", "A.B", "A\n", "A\nB", "HOME ", " HOME", "ÄB", "A\x00", "A=", "=A", "$X"]
    )
    def test_anything_that_is_not_a_plain_identifier_is_refused(self, build, name):
        with pytest.raises(ValueError):
            build(spec=RepoSpec(key="a/b", extra_env={name: "v"}))

    @pytest.mark.parametrize("name", ["TZ", "_X", "a1", "LC_ALL", "MY_VAR_2", "__"])
    def test_a_plain_identifier_is_accepted(self, build, name):
        assert has_pair(build(spec=RepoSpec(key="a/b", extra_env={name: "v"})), "-e", f"{name}=v")

    def test_the_error_names_every_offender(self, build):
        with pytest.raises(ValueError) as excinfo:
            build(spec=RepoSpec(key="acme/x", extra_env={"HOME=/e": "1", "A B": "2", "TZ": "UTC"}))

        message = str(excinfo.value)
        assert "acme/x" in message and "HOME=/e" in message and "A B" in message
        assert "'TZ'" not in message

    def test_the_payload_never_reaches_the_argv(self, build):
        """Not merely refused in a message: nothing is built."""
        with pytest.raises(ValueError):
            build(spec=RepoSpec(key="a/b", extra_env={"HOME=/evil": "v"}))

    def test_the_trailing_newline_trap(self, build):
        """`$` matches before a final newline; the check is a fullmatch, not that."""
        with pytest.raises(ValueError):
            build(spec=RepoSpec(key="a/b", extra_env={"HOME\n": "v"}))
