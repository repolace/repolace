"""The Docker implementation of `SandboxBackend`.

Two phases with opposite postures, and the split is the whole security design:

* `prepare` builds an image **with network**, because installing a repository's
  dependencies needs one. Nothing under test runs here that the repository's own
  build did not already ask for.
* `run_tests` runs the suite with **no network at all**, a read-only root, every
  capability dropped, an unprivileged uid, and hard memory/CPU/PID caps. This is
  where untrusted code executes -- the repository's `conftest.py` runs at
  collection, before any patch is involved, so registering a hostile repository
  is the whole attack.

What crosses back out is data only: the pass/fail sets and a stdout tail, parsed
from JSONL on the host. Never a file the host then executes, and never anything
written into `.git` -- which is not here at all, because `export_tree` omits it.
"""

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import structlog

from repolace_shared.process import ProcessResult, run_process
from verify.config import (
    PLUGIN_DIR,
    PLUGIN_MODULE,
    REPORT_PATH,
    RESULTS_DIR,
    WORKDIR,
    DockerConfig,
)
from verify.dockerfile import (
    CONTEXT_PLUGIN_PATH,
    CONTEXT_SOURCE_DIR,
    image_tag,
    plugin_source,
    render_dockerfile,
)
from verify.errors import EnvironmentBuildFailed, SandboxUnavailable
from verify.protocol import EnvironmentRef, RepoSpec, SuiteResult
from verify.report import parse_report

log = structlog.get_logger()

BACKEND_NAME = "docker"

#: How long `docker version` gets to answer before the daemon counts as absent.
PROBE_TIMEOUT_SECONDS = 20.0

#: How long the cleanup `docker rm -f` gets. Short on purpose: it runs on a path
#: that is already going wrong, and blocking there would compound the problem.
REMOVE_TIMEOUT_SECONDS = 30.0

#: Environment handed to the `docker` CLI. An allowlist for the same reason
#: `sanitized_git_env` is one: this process holds the GitHub App private key,
#: the database URL and the broker URL, and a subprocess inherits all of it by
#: default. The App key is the worst of them -- it mints installation tokens for
#: every installation, and rotating a token does not revoke it.
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "USER",
        "LOGNAME",
        # How the CLI finds the daemon. Rootless Docker lives on a per-user
        # socket under XDG_RUNTIME_DIR, so dropping these would silently fall
        # back to the rootful socket -- which is the posture CLAUDE.md rejects.
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
        "XDG_RUNTIME_DIR",
        # Build-time only: a registry pull may need the operator's proxy.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


def sanitized_docker_env() -> dict[str, str]:
    """The CLI's environment, built by allowlist rather than by subtraction.

    Subtracting means every secret added to the service environment later is
    inherited by default and only stops being inherited if someone remembers to
    come back here. An allowlist fails the safe way.
    """
    return {name: value for name, value in os.environ.items() if name in _ENV_ALLOWLIST}


def build_run_argv(
    config: DockerConfig,
    spec: RepoSpec,
    env: EnvironmentRef,
    source_dir: Path,
    results_dir: Path,
    container_name: str,
) -> tuple[str, ...]:
    """Every flag the sandbox runs with, as one pure function.

    Pure, and tested flag by flag, because these are the containment controls
    rather than decoration. Each of them fails *open*: a misspelled
    `--secutiry-opt` is rejected by the CLI, but a dropped `--network=none` is
    simply a container with network, and nothing about the run looks different.
    """
    limits = config.limits
    argv: list[str] = [
        "run",
        "--rm",
        # tini as pid 1. A suite that spawns background processes otherwise
        # leaves zombies that keep the pid cgroup populated until the cap trips.
        "--init",
        "--name",
        container_name,
        # Danger 1, the containment half: nothing under test can reach the
        # network, so nothing it reads can leave. Unconditional -- there is no
        # spec field to weaken it, deliberately.
        "--network=none",
        "--read-only",
        # /tmp has to exist and be writable: pytest's own `tmp_path` lives
        # there. nodev/nosuid rather than noexec -- noexec is real hardening but
        # breaks any suite that writes a script and runs it, and an unscoreable
        # instance costs the benchmark more than this flag buys against an
        # attacker who can already execute /repo.
        f"--tmpfs=/tmp:rw,nosuid,nodev,size={limits.tmpfs_size}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        config.user,
        "--memory",
        limits.memory,
        # Equal to --memory. If swap were larger the container could exceed the
        # memory cap by swapping, and exit 137 -- which `parse_report` reads as
        # "killed, most likely the memory limit" -- would never arrive.
        "--memory-swap",
        limits.memory_swap,
        "--cpus",
        limits.cpus,
        "--pids-limit",
        str(limits.pids_limit),
        "--ulimit",
        f"nofile={limits.nofile}:{limits.nofile}",
        "--log-opt",
        f"max-size={limits.log_max_size}",
        "--log-opt",
        f"max-file={limits.log_max_file}",
    ]

    if limits.cpuset_cpus:
        # For a suite that shards on os.cpu_count(), which reports host CPUs
        # whatever --cpus says.
        argv += ["--cpuset-cpus", limits.cpuset_cpus]

    if config.runtime:
        # gVisor, when it is installed. One flag, because the semantic seam is
        # what makes stronger isolation a config change rather than a new backend.
        argv += ["--runtime", config.runtime]

    mount_suffix = ":ro" if spec.repo_readonly else ""
    argv += [
        "-v",
        f"{source_dir}:{WORKDIR}{mount_suffix}",
        # Outside the source tree, so a suite that scribbles over its own
        # working directory cannot destroy the report that says what it did.
        "-v",
        f"{results_dir}:{RESULTS_DIR}",
        "-w",
        WORKDIR,
        "-e",
        f"REPOLACE_REPORT_PATH={REPORT_PATH}",
        # Under --read-only, anything writing a dotfile into a nonexistent HOME
        # fails in a way that reads as a test failure rather than as a
        # configuration problem.
        "-e",
        "HOME=/tmp",
        "-e",
        f"PYTHONPATH={PLUGIN_DIR}",
    ]

    if spec.disable_plugin_autoload:
        argv += ["-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1"]

    for name, value in sorted(spec.extra_env.items()):
        argv += ["-e", f"{name}={value}"]

    # Explicit, so a base image carrying its own ENTRYPOINT cannot turn the
    # pytest invocation into arguments for something else.
    argv += ["--entrypoint", spec.python_executable, env.identifier, "-m", "pytest"]
    argv += ["-p", PLUGIN_MODULE]
    # Into the tmpfs, which dies with the container. Left at its default the
    # cache lands in /repo -- the bind-mounted export -- created by uid 65534
    # with mode 0755. The host cannot then delete inside a directory it does not
    # own, `task_workspace`'s cleanup fails, and every task leaks its temp tree.
    # `-o cache_dir` rather than `-p no:cacheprovider` so the `cache` fixture and
    # `--lf` keep working for a repo that uses them; `cache_dir` is not one of
    # the fingerprinted ini options, so this cannot look like configuration drift.
    argv += ["-o", "cache_dir=/tmp/.pytest_cache"]

    if not spec.keep_addopts:
        # Clears a repo's own addopts -- coverage gates, `-x`, `--strict` --
        # which would otherwise decide the exit code. The value is fingerprinted
        # by the plugin either way, so a change between baseline and attempt is
        # caught rather than silently changing what "passing" means.
        argv += ["-o", "addopts="]

    argv += list(spec.test_targets)
    argv += list(spec.extra_pytest_args)
    return tuple(argv)


def build_argv(config: DockerConfig, tag: str, context_dir: Path) -> tuple[str, ...]:
    """The image build. Network is permitted here and nowhere else."""
    return (
        "build",
        "--tag",
        tag,
        "--file",
        str(context_dir / "Dockerfile"),
        str(context_dir),
    )


class DockerBackend:
    """`SandboxBackend` over the `docker` CLI.

    The CLI rather than the SDK on purpose: the argv *is* the security policy,
    so it is worth having it be a value this codebase can print, diff and assert
    on, rather than keyword arguments spread across an API call.
    """

    def __init__(self, config: DockerConfig | None = None) -> None:
        self.config = config or DockerConfig()
        self._probed = False

    async def _run(
        self, *args: str, timeout: float, env: Mapping[str, str] | None = None
    ) -> ProcessResult:
        try:
            return await run_process(
                self.config.docker_binary,
                *args,
                timeout=timeout,
                env=env if env is not None else sanitized_docker_env(),
                capture_limit=self.config.capture_limit,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise SandboxUnavailable(f"{self.config.docker_binary}: {exc}") from exc

    @staticmethod
    def _text(process: ProcessResult) -> str:
        both = process.stdout + b"\n" + process.stderr
        return both.decode("utf-8", errors="replace")

    async def _probe(self) -> None:
        """Ask the daemon whether it is there, once per backend instance.

        Before the build, so "no daemon" is `SandboxUnavailable` -- an
        instrument failure, excluded from the benchmark -- rather than
        `EnvironmentBuildFailed`, which reads as the repository's fault.
        """
        if self._probed:
            return
        process = await self._run(
            "version", "--format", "{{.Server.Version}}", timeout=PROBE_TIMEOUT_SECONDS
        )
        if process.returncode != 0 or process.timed_out:
            raise SandboxUnavailable(self._text(process))
        self._probed = True
        log.info("verify.docker.daemon", version=process.stdout.decode().strip())

    async def _image_exists(self, tag: str) -> bool:
        process = await self._run("image", "inspect", tag, timeout=PROBE_TIMEOUT_SECONDS)
        return process.returncode == 0

    async def prepare(self, spec: RepoSpec, source_dir: Path, cache_key: str) -> EnvironmentRef:
        """Build or reuse the image. Network is permitted here and nowhere else."""
        from verify.spec import install_commands

        await self._probe()
        tag = image_tag(self.config.image_prefix, spec, cache_key)
        env = EnvironmentRef(backend=BACKEND_NAME, identifier=tag, workdir=WORKDIR)

        if await self._image_exists(tag):
            # The key covers the Dockerfile, the plugin bytes and the dependency
            # manifests, so a hit means the *environment* is identical -- which
            # is the precondition that lets `score` attribute a newly failing
            # suite to the patch rather than to the install step.
            log.info("verify.docker.image.cached", tag=tag)
            return env

        install = install_commands(spec, source_dir)
        with tempfile.TemporaryDirectory(prefix="repolace-build-") as raw:
            context = Path(raw)
            await asyncio.to_thread(self._write_context, context, spec, install, source_dir)
            log.info("verify.docker.build.start", tag=tag, install=list(install))
            process = await self._run(
                *build_argv(self.config, tag, context),
                timeout=self.config.build_timeout_seconds,
            )

        if process.timed_out:
            raise EnvironmentBuildFailed(
                spec.key,
                process.returncode,
                f"build exceeded {self.config.build_timeout_seconds}s",
            )
        if process.returncode != 0:
            raise EnvironmentBuildFailed(spec.key, process.returncode, self._text(process))

        log.info("verify.docker.build.done", tag=tag)
        return env

    @staticmethod
    def _write_context(
        context: Path, spec: RepoSpec, install: Sequence[str], source_dir: Path
    ) -> None:
        """Assemble the build context on a worker thread.

        A copy rather than building straight from the export, because the
        context also has to carry the plugin, and `COPY` cannot reach outside
        it. The export is already `.git`-free and symlink-free -- `export_index_to`
        refuses modes 120000 and 160000 -- so `copytree` has nothing to follow.
        """
        shutil.copytree(source_dir, context / CONTEXT_SOURCE_DIR, symlinks=False)
        plugin_target = context / CONTEXT_PLUGIN_PATH
        plugin_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plugin_source(), plugin_target)
        (context / "Dockerfile").write_text(render_dockerfile(spec, install), encoding="utf-8")

    async def _force_remove(self, container_name: str) -> None:
        """Kill and delete the container, best effort.

        Necessary because `run_process` kills the `docker run` *client*, and the
        container is a child of the daemon rather than of that client -- so it
        survives, holding the memory cgroup and the bind mount of a directory
        the workspace is about to delete. `--rm` does not help: it fires when the
        container exits, which is exactly what is not happening here.
        """
        try:
            process = await self._run("rm", "--force", container_name, timeout=REMOVE_TIMEOUT_SECONDS)
        except SandboxUnavailable:
            return  # the daemon going away has already been reported elsewhere
        if process.returncode != 0:
            # Ordinary when the container exited normally and `--rm` won the
            # race. Logged at debug so a real leak is still findable.
            log.debug("verify.docker.remove.miss", container=container_name)

    async def run_tests(
        self,
        env: EnvironmentRef,
        source_dir: Path,
        results_dir: Path,
        spec: RepoSpec,
        *,
        container_name: str,
    ) -> SuiteResult:
        """Run the suite with no network. Source files in, pass/fail data out."""
        argv = build_run_argv(self.config, spec, env, source_dir, results_dir, container_name)
        timeout = spec.timeout_seconds or self.config.run_timeout_seconds
        log.info("verify.docker.run.start", container=container_name, image=env.identifier)

        started = time.perf_counter()
        clean = False
        try:
            process = await self._run(*argv, timeout=timeout)
            clean = not process.timed_out
        finally:
            if not clean:
                # Covers the timeout path and cancellation alike. Not run on a
                # clean exit: `--rm` has already done it, and a spurious
                # `docker rm` per run is noise in the daemon's log.
                await asyncio.shield(self._force_remove(container_name))
        elapsed = time.perf_counter() - started

        result = parse_report(Path(results_dir) / Path(REPORT_PATH).name, process, elapsed)
        log.info(
            "verify.docker.run.done",
            container=container_name,
            exit_code=result.exit_code,
            passed=len(result.passed),
            failed=len(result.failed),
            error=result.error,
            duration=result.duration_seconds,
        )
        return result
