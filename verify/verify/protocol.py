"""The seam between the pipeline and whatever actually runs the tests.

Deliberately says nothing about containers. Everything here is plain data, so
swapping Docker for gVisor, a microVM, or a remote executor is a new
implementation of one Protocol rather than a change to the pipeline -- which is
what CLAUDE.md's Phase 5 replacement order assumes.

Note `run_tests` takes a *directory*, not a patch. CLAUDE.md puts patch
application on the host; handing a patch to the sandbox would mean the sandbox
needs git and a tree to reason about, which is the coupling this design exists
to avoid.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RepoSpec:
    """How to build and run one repository's suite."""

    key: str
    base_image: str = "python:3.12-slim"
    #: Shell commands run at build time, with network. Empty means "use the heuristic".
    install: tuple[str, ...] = ()
    system_packages: tuple[str, ...] = ()
    #: Used as the container's entrypoint, so a base image that ships its own
    #: ENTRYPOINT cannot swallow the pytest invocation. `python` rather than
    #: `python3` because that is what the `python:*` images provide; a repo
    #: pinned to a bare `debian`/`ubuntu` base needs `python3` here.
    python_executable: str = "python"
    #: Empty is the safest default: with no ini file pytest derives rootdir from
    #: the argv, so passing nothing keeps node IDs stable between baseline and
    #: attempts. See `dockerfile`/`backends.docker` for why that matters.
    test_targets: tuple[str, ...] = ()
    extra_pytest_args: tuple[str, ...] = ()
    #: Drop `-o addopts=` for a repo that genuinely needs its own addopts.
    keep_addopts: bool = False
    #: Off by default, and that is the safe direction. Disabling entry-point
    #: autoload makes a run reproducible, but a suite built on `pytest-django`,
    #: `pytest-asyncio` or `pytest-mock` then fails to collect -- unscoreable,
    #: for a reason that has nothing to do with the agent. Turn it on per repo
    #: when a plugin is what makes a run non-deterministic.
    disable_plugin_autoload: bool = False
    repo_readonly: bool = False
    timeout_seconds: float | None = None
    extra_env: Mapping[str, str] = field(default_factory=dict)
    #: Files the install step generates *inside the tree* (a `setuptools_scm`
    #: `_version.py`), as path -> text. Every run bind-mounts its export over the
    #: tree the image was built in, which hides whatever the install wrote there,
    #: so a package that imports such a file fails at startup. They are laid over
    #: each export after the environment is built, like the hidden-test overlay but
    #: for every run, and are not part of the image or its cache key.
    generated_files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EnvironmentRef:
    """A prepared environment. An image tag today; a VM handle or venv path later."""

    backend: str
    identifier: str
    workdir: str = "/repo"


@dataclass(frozen=True)
class SuiteResult:
    """One execution of a suite.

    `passed` and `failed` are the sets the whole scoring rule joins on. `error`
    being non-None means the run is **unscoreable** -- we never found out --
    which is a different thing from every test having failed.
    """

    passed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    #: Neither passed nor failed. Kept so "a passing test became a skip" can be
    #: seen as the regression it is, rather than silently vanishing.
    #:
    #: **Ordinary** skips only -- `@pytest.mark.skip`, `skipif`, a module-level
    #: `pytest.skip`, `importorskip` for a missing dependency. These are not red
    #: at baseline and one of them going green is evidence of nothing.
    skipped: tuple[str, ...] = ()
    #: Expected failures. pytest reports these as skips too, which is why they
    #: have to be separated here rather than by the scoring rule: an xfail is a
    #: *known bug the repository has written down*, so it is red at baseline and
    #: it going green is the likeliest honest form of fail-to-pass. Folding the
    #: two together let an `importorskip` that started passing score a task
    #: PASSED with nothing red at baseline at all.
    xfailed: tuple[str, ...] = ()
    did_not_run: tuple[str, ...] = ()
    collect_failures: tuple[str, ...] = ()
    #: Files pytest actually collected tests from, and the conftests it loaded.
    #: Authoritative for this repo in a way a path heuristic cannot be.
    collected_files: tuple[str, ...] = ()
    conftests: tuple[str, ...] = ()
    #: rootdir, watched ini options and registered plugins. Scoring requires
    #: these identical between baseline and attempt, so an agent cannot relax
    #: `filterwarnings` in pyproject.toml to turn a real failure into a real
    #: pass without touching a test file.
    fingerprint: Mapping[str, object] = field(default_factory=dict)
    exit_code: int | None = None
    duration_seconds: float | None = None
    stdout_tail: str = ""
    error: str | None = None

    @property
    def scoreable(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class ScriptResult:
    """One execution of a scratch script -- not a suite, so nothing is parsed.

    A script is whatever the agent wrote to see how the code behaves. It is not
    pytest, has no report file and no pass/fail sets, so the only honest result
    is what the process did: how it exited, and what it printed.

    `exit_code` is None when the script never produced one -- it was killed on
    the timeout, or the container could not start. `timed_out` and `error` say
    which of those it was, and they are different things:

    * **`error`** means the *runtime* failed -- the daemon is unreachable, the
      container could not be created. The script never ran, so nothing about its
      exit status is a fact about the code under test. Distinct from a non-zero
      exit, which means the script ran and failed, and is the agent's to read.
    * **`timed_out`** means it ran and was killed.

    `truncated` is True when stdout or stderr hit the capture cap, so a tool
    that shows the output to a model can say the tail is missing instead of
    presenting a clipped log as the whole story.
    """

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False
    duration_seconds: float | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        # "No exit code" has to be explained. A result with none and neither
        # `timed_out` nor `error` says nothing at all -- the script neither
        # finished, nor was killed, nor failed to start -- and a tool rendering it
        # would have to invent a sentence.
        if self.exit_code is None and not self.timed_out and not self.error:
            raise ValueError(
                "a ScriptResult with no exit_code must say why: timed_out=True or an error"
            )


class SandboxBackend(Protocol):
    async def prepare(self, spec: RepoSpec, source_dir: Path, cache_key: str) -> EnvironmentRef:
        """Build or reuse an environment. Network is permitted here and nowhere else."""
        ...

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
        ...

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
        """Run one scratch script against `source_dir`, with no network.

        Same containment as `run_tests` -- no network, read-only root, every
        capability dropped, an unprivileged uid, the same resource caps -- and an
        implementation must build both from one shared flag set so a flag cannot
        be dropped from one path and kept in the other.

        What differs, and what an implementation must hold to:

        * **The source is mounted read-only, unconditionally.** Not through a
          `RepoSpec` field: a spec is per-repository data, and a protection that
          data can switch off is only as trustworthy as the data.
        * **The script is mounted read-only at a fixed path**, outside the tree,
          so it never appears in the source the host exports or diffs.
        * **No results mount**, and no report parsing -- there is nothing to
          parse and nothing may be written back to the host.
        * `PYTHONPATH` is the source root, because a script's `sys.path[0]` is
          its own directory rather than the repository.
        * `timeout_seconds` is a hard limit; on timeout the container is removed
          by `container_name`, for the reason `run_tests` removes its own.
        """
        ...
