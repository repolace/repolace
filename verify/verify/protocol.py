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
    #: Empty is the safest default: with no ini file pytest derives rootdir from
    #: the argv, so passing nothing keeps node IDs stable between baseline and
    #: attempts. See `dockerfile`/`backends.docker` for why that matters.
    test_targets: tuple[str, ...] = ()
    extra_pytest_args: tuple[str, ...] = ()
    #: Drop `-o addopts=` for a repo that genuinely needs its own addopts.
    keep_addopts: bool = False
    repo_readonly: bool = False
    timeout_seconds: float | None = None
    extra_env: Mapping[str, str] = field(default_factory=dict)


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
    skipped: tuple[str, ...] = ()
    did_not_run: tuple[str, ...] = ()
    collect_failures: tuple[str, ...] = ()
    exit_code: int | None = None
    duration_seconds: float | None = None
    stdout_tail: str = ""
    error: str | None = None

    @property
    def scoreable(self) -> bool:
        return self.error is None


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
