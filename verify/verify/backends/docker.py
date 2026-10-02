"""The Docker implementation of `SandboxBackend`.

Two phases with opposite postures, and the split is the whole security design
(a third command, `run_script`, is `run_tests`' containment with less access):

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
import math
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import structlog

from repolace_shared.git import redact
from repolace_shared.process import ProcessResult, run_process
from verify.config import (
    PLUGIN_DIR,
    PLUGIN_MODULE,
    REPORT_PATH,
    RESERVED_ENV,
    RESULTS_DIR,
    RUN_NONCE_ENV_VAR,
    SCRIPT_PATH,
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
from verify.protocol import EnvironmentRef, RepoSpec, ScriptResult, SuiteResult
from verify.report import parse_report

log = structlog.get_logger()

BACKEND_NAME = "docker"

#: How long `docker version` gets to answer before the daemon counts as absent.
PROBE_TIMEOUT_SECONDS = 20.0

#: How long the cleanup `docker rm -f` gets. Short on purpose: it runs on a path
#: that is already going wrong, and blocking there would compound the problem.
REMOVE_TIMEOUT_SECONDS = 30.0

#: Pause before the single retry of a failed `docker rm -f`. A daemon that just
#: failed one call is rarely ready for the identical one a millisecond later.
REMOVE_RETRY_DELAY_SECONDS = 1.0

#: `docker run`'s own exit code for "the CLI or daemon failed", as opposed to the
#: contained command's exit status (126 and 127 are the contained command).
_DOCKER_RUN_FAILED = 125


def _looks_like_docker_failure(stderr: str) -> bool:
    """Whether stderr reads like the docker CLI's own error rather than a script's."""
    return stderr.lstrip().startswith("docker:") or "Error response from daemon" in stderr


def _hide_host_paths(text: str, source_dir: Path, script_path: Path) -> str:
    """Replace the host paths of one run with fixed placeholders, longest first.

    Longest first so the workspace root does not eat the front of the longer
    paths beneath it. A path of one character or less (`/`) is skipped: replacing
    it would mangle everything.
    """
    replacements = {
        str(script_path): "<script>",
        str(script_path.parent): "<scratch>",
        str(source_dir): "<export>",
        str(source_dir.parent): "<workspace>",
    }
    for host_path in sorted(replacements, key=len, reverse=True):
        if len(host_path) > 1:
            text = text.replace(host_path, replacements[host_path])
    return text

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


def _containment_argv(
    config: DockerConfig, *, container_name: str, mounts: Sequence[str]
) -> list[str]:
    """Every flag that contains the sandbox, shared by every command that runs in it.

    One list for the pytest run and the scratch script alike, and that is the
    point: the two paths used to be able to disagree only by someone editing
    one and forgetting the other, and every flag here fails *open*. A script
    path that quietly lacked `--network=none` would be a container with network
    and an agent holding a shell in it. So a flag is added here or not at all,
    and `test_docker_argv.py` runs each containment assertion over both builders.

    Ends with the `-v` mounts, because they are the one containment decision
    that differs per command -- what is writable, and what comes back out.
    """
    limits = config.limits
    argv: list[str] = [
        "run",
        "--rm",
        # The image was built (or found) by `prepare` and is the only thing a run
        # may start. Without this, a tag that is missing locally -- a pruned image,
        # a typo in a spec -- would be silently pulled from a registry at run time,
        # which is network use on the one path that must have none.
        "--pull=never",
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

    for mount in mounts:
        argv += ["-v", mount]
    return argv


def _extra_env_argv(spec: RepoSpec) -> list[str]:
    """A spec's own environment variables. Refuses any name the sandbox reserves.

    Raised rather than reordered so they come last: an `extra_env` that named
    `PYTHONPATH` or `REPOLACE_RUN_NONCE` is a mistake in operator data, and a run
    that quietly ignored (or quietly honoured) it would be a measurement nobody
    chose. `spec.py` refuses the same names when the spec file is loaded, so this is
    the backstop for a `RepoSpec` built in code.
    """
    reserved = sorted(set(spec.extra_env) & RESERVED_ENV)
    if reserved:
        raise ValueError(
            f"spec {spec.key!r}: extra_env may not set {', '.join(reserved)}; "
            f"the sandbox reserves them"
        )
    argv: list[str] = []
    for name, value in sorted(spec.extra_env.items()):
        argv += ["-e", f"{name}={value}"]
    return argv


def _timeout(requested: float | None, default: float) -> float:
    """`None` means the default; anything else must be a positive finite number.

    Not `requested or default`: `0` is falsy, so a spec asking for a zero-second
    timeout would silently get the 30-minute default instead of an error.
    """
    if requested is None:
        return default
    if not (math.isfinite(requested) and requested > 0):
        raise ValueError(f"timeout must be a positive number of seconds, got {requested!r}")
    return requested


def build_run_argv(
    config: DockerConfig,
    spec: RepoSpec,
    env: EnvironmentRef,
    source_dir: Path,
    results_dir: Path,
    container_name: str,
    *,
    nonce: str | None = None,
) -> tuple[str, ...]:
    """Every flag the pytest run uses, as one pure function.

    Pure, and tested flag by flag, because these are the containment controls
    rather than decoration. Each of them fails *open*: a misspelled
    `--secutiry-opt` is rejected by the CLI, but a dropped `--network=none` is
    simply a container with network, and nothing about the run looks different.

    `nonce` is the per-run token `run_tests` generates and hands to the plugin
    through the environment (`REPOLACE_RUN_NONCE`), so the report it writes can be
    told from any other run's. Optional only so the argv can be built and asserted
    without one; `DockerBackend.run_tests` always passes it.
    """
    mount_suffix = ":ro" if spec.repo_readonly else ""
    argv = _containment_argv(
        config,
        container_name=container_name,
        mounts=(
            f"{source_dir}:{WORKDIR}{mount_suffix}",
            # Outside the source tree, so a suite that scribbles over its own
            # working directory cannot destroy the report that says what it did.
            f"{results_dir}:{RESULTS_DIR}",
        ),
    )
    argv += [
        "-w",
        WORKDIR,
        "-e",
        f"REPOLACE_REPORT_PATH={REPORT_PATH}",
        *(["-e", f"{RUN_NONCE_ENV_VAR}={nonce}"] if nonce is not None else []),
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

    argv += _extra_env_argv(spec)

    # Explicit, so a base image carrying its own ENTRYPOINT cannot turn the
    # pytest invocation into arguments for something else.
    argv += ["--entrypoint", spec.python_executable, env.identifier, "-m", "pytest"]
    argv += ["-p", PLUGIN_MODULE]
    # Always pinned, not only when targets are overridden. With no ini file
    # pytest derives rootdir from its arguments, so a run given `tests/test_a.py`
    # can root at `/repo/tests` and every node id lose its `tests/` prefix -- a
    # probe's ids would then match nothing the baseline recorded.
    # One rootdir means one node-id space for the baseline, every attempt and
    # every probe. The plugin fingerprints rootdir, so this is also what keeps
    # that comparison from drifting between them.
    argv += [f"--rootdir={WORKDIR}"]
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


def build_script_argv(
    config: DockerConfig,
    spec: RepoSpec,
    env: EnvironmentRef,
    source_dir: Path,
    script_path: Path,
    container_name: str,
) -> tuple[str, ...]:
    """Every flag a scratch script runs with. The same containment, less of everything else.

    What differs from the pytest run is deliberate, and each difference makes
    the script *less* able to do things, never more:

    * **The source is `:ro` unconditionally.** Not through `spec.repo_readonly`:
      a spec is per-repository data, and a protection that data can switch off
      is only as trustworthy as the data. The pytest path honours that field
      because some suites must write beside their code; a scratch script has no
      such excuse, and a writable mount here would let one script plant a file
      for the next probe to import.
    * **No `/results` mount.** Nothing is parsed and nothing is written back to
      the host. (Output still comes back, as bytes on the pipe, which the host
      reads as data.)
    * **The script is mounted `:ro` outside the tree**, so it never lands in the
      checkout `git add -A` sweeps, nor in the export.
    * `PYTHONPATH` is the source root, because a script's `sys.path[0]` is its
      own directory (`/scratch`), so a flat-layout package would otherwise not
      import. The pytest path needs no such variable: `python -m pytest` puts the
      working directory, `/repo`, on `sys.path` itself.
    * The script's file name is one no repository has. `sys.path[0]` comes ahead
      of `PYTHONPATH`, so a script called `main.py` would shadow a repository's
      own `main.py`.
    """
    argv = _containment_argv(
        config,
        container_name=container_name,
        mounts=(f"{source_dir}:{WORKDIR}:ro", f"{script_path}:{SCRIPT_PATH}:ro"),
    )
    argv += [
        "-w",
        WORKDIR,
        "-e",
        "HOME=/tmp",
        "-e",
        f"PYTHONPATH={WORKDIR}",
    ]
    argv += _extra_env_argv(spec)
    # Explicit entrypoint, for the same reason as the pytest path.
    argv += ["--entrypoint", spec.python_executable, env.identifier, SCRIPT_PATH]
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

        "No such container" is the ordinary miss (it exited and `--rm` won the
        race) and is logged quietly. Any *other* failure means a runaway script may
        be outliving its timeout, which is the very thing this exists to prevent, so
        it is a warning, and the removal is tried once more before giving up.
        """
        for attempt in (1, 2):
            try:
                process = await self._run(
                    "rm", "--force", container_name, timeout=REMOVE_TIMEOUT_SECONDS
                )
            except SandboxUnavailable:
                return  # the daemon going away has already been reported elsewhere
            if process.returncode == 0 and not process.timed_out:
                return
            detail = self._text(process)
            if "No such container" in detail:
                log.debug("verify.docker.remove.miss", container=container_name)
                return
            log.warning(
                "verify.docker.remove.failed",
                container=container_name,
                attempt=attempt,
                exit_code=process.returncode,
                timed_out=process.timed_out,
                detail=redact(detail.strip())[-300:],
            )
            if attempt == 1:
                await asyncio.sleep(REMOVE_RETRY_DELAY_SECONDS)

    async def run_tests(
        self,
        env: EnvironmentRef,
        source_dir: Path,
        results_dir: Path,
        spec: RepoSpec,
        *,
        container_name: str,
    ) -> SuiteResult:
        """Run the suite with no network. Source files in, pass/fail data out.

        A fresh nonce per run goes into the container and is required back in the
        report, so a report that did not come from *this* container -- one a probe
        planted by pointing the report path at another run's -- is refused rather
        than parsed.
        """
        nonce = secrets.token_hex(16)
        argv = build_run_argv(
            self.config, spec, env, source_dir, results_dir, container_name, nonce=nonce
        )
        timeout = _timeout(spec.timeout_seconds, self.config.run_timeout_seconds)
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

        # Off the event loop: the report is bounded at 64 MB, and parsing one that
        # size takes seconds that would otherwise stall every other task.
        result = await asyncio.to_thread(
            parse_report,
            Path(results_dir) / Path(REPORT_PATH).name,
            process,
            elapsed,
            nonce=nonce,
        )
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

    async def run_script(
        self,
        env: EnvironmentRef,
        source_dir: Path,
        script_path: Path,
        spec: RepoSpec,
        *,
        container_name: str,
        timeout_seconds: float,
    ) -> ScriptResult:
        """Run one scratch script with no network. Contract: `SandboxBackend.run_script`.

        Shares the timeout and cleanup discipline of `run_tests` -- the container
        is removed by name when the deadline fires or the task is cancelled,
        because killing the `docker run` client leaves it alive -- and nothing
        else: no report, nothing parsed, nothing read back from the container but
        its output bytes.

        **`error` is for the runtime failing**, which is not the same as the script
        failing. A non-zero exit is the agent's to read. The `docker` binary being
        unusable (`SandboxUnavailable`) means the script never ran: no exit code, no
        output. Exit 125 is the CLI's own code for "`docker run` itself failed", but
        a script can exit 125 too, so it is only called a runtime failure when
        stderr also looks like docker's own message (`docker: ...`, or `Error
        response from daemon`). Even then the real exit code, stdout and stderr are
        kept and the error text says it may be a script exit, because a forged
        runtime failure would otherwise hide the script's real output and tell the
        agent to stop using its sandbox.

        **A script that times out returns no output**, however much it printed:
        `run_process` discards it on the kill path. Known limit -- `run_process` is
        shared and unchanged here.

        Host paths (the export, the script, the workspace root) are replaced by
        placeholders in anything taken from docker's own message: the result reaches
        the model, and the task root is under a random directory it has no other
        way to learn.

        `timeout_seconds` must be positive; `ValueError` otherwise, never a
        fallback to the 30-minute default.
        """
        timeout = _timeout(timeout_seconds, self.config.run_timeout_seconds)
        argv = build_script_argv(self.config, spec, env, source_dir, script_path, container_name)
        log.info("verify.docker.script.start", container=container_name, image=env.identifier)

        started = time.perf_counter()
        clean = False
        try:
            process = await self._run(*argv, timeout=timeout)
            clean = not process.timed_out
        except SandboxUnavailable as exc:
            return ScriptResult(
                exit_code=None, error=str(exc), duration_seconds=time.perf_counter() - started
            )
        finally:
            if not clean:
                await asyncio.shield(self._force_remove(container_name))
        elapsed = time.perf_counter() - started

        stdout = process.stdout.decode("utf-8", errors="replace")
        stderr = process.stderr.decode("utf-8", errors="replace")
        if process.timed_out:
            # The kill's return code and the (discarded) output say nothing about
            # the script, so none of it is passed on.
            result = ScriptResult(exit_code=None, timed_out=True, duration_seconds=elapsed)
        elif process.returncode == _DOCKER_RUN_FAILED and _looks_like_docker_failure(stderr):
            def hide(text: str) -> str:
                # Redacted too: in this branch the text is docker's own, and a
                # registry or daemon message is the sort of thing that quotes a
                # credential back.
                return _hide_host_paths(redact(text), source_dir, script_path)

            result = ScriptResult(
                exit_code=process.returncode,
                stdout=hide(stdout),
                stderr=hide(stderr),
                truncated=process.truncated,
                duration_seconds=elapsed,
                error=(
                    f"{SandboxUnavailable(hide(stderr + chr(10) + stdout))} "
                    f"(exit {_DOCKER_RUN_FAILED} is also what a script that exits "
                    f"{_DOCKER_RUN_FAILED} produces; this may be a script exit, not a "
                    f"runtime failure)"
                ),
            )
        else:
            result = ScriptResult(
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                truncated=process.truncated,
                duration_seconds=elapsed,
            )
        log.info(
            "verify.docker.script.done",
            container=container_name,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            error=result.error,
            duration=result.duration_seconds,
        )
        return result
