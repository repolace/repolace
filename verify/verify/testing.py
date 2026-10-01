"""In-memory stand-ins for the sandbox seam, shared by every package's tests.

Lives in the package rather than under `verify/tests/` because `agents/` and
`pipeline/` need the same fakes, and one test directory cannot import another's
helpers (pytest's prepend import mode puts each on `sys.path` by bare name, which
is exactly the shadowing the `<package>_support.py` convention exists to dodge).
Three copies of a fake backend would drift, and a fake that disagrees with the
real `SandboxBackend` is how a green suite ends up testing a seam that is not
there.

Dependency-free on purpose -- the standard library and `verify.protocol` only, so
importing it never pulls in structlog, Docker, or SQLAlchemy.
"""

import shutil
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from verify.protocol import EnvironmentRef, RepoSpec, ScriptResult, SuiteResult

#: What `run_tests` returns when no results were scripted -- the same single
#: passing test the original fake in `test_stage.py` returned, so moving it here
#: changed no assertion.
DEFAULT_SUITE_RESULT = SuiteResult(passed=("t::a",))


class FakeBackend:
    """A `SandboxBackend` that records every call and returns what it was told to.

    With nothing scripted, every `run_tests` returns `DEFAULT_SUITE_RESULT` and
    every `run_script` returns a clean exit. Once results are scripted they come
    back strictly in order, and running **past the end of the script is an
    `AssertionError`** rather than a recycled last answer: a test that triggers
    one more sandbox run than it planned for has found a bug (a probe that
    recorded a row, a retry that should not have happened), and a fake that
    quietly kept answering would hide it.

    Recorded for assertions:

    * `prepared` -- the cache key of each `prepare` call.
    * `runs` -- one dict per `run_tests`: `image`, `source`, `results`,
      `container`.
    * `scripts` -- one dict per `run_script`: `image`, `source`, `script`,
      `container`, `timeout`.
    """

    def __init__(
        self,
        results: Iterable[SuiteResult] = (),
        scripts: Iterable[ScriptResult] = (),
    ) -> None:
        scripted_results = list(results)
        scripted_scripts = list(scripts)
        # Whether anything was scripted is decided once, at construction: an
        # empty queue after use means "exhausted", an empty one at the start
        # means "use the default", and they must not be confused.
        self._results_scripted = bool(scripted_results)
        self._scripts_scripted = bool(scripted_scripts)
        self._results: deque[SuiteResult] = deque(scripted_results)
        self._script_results: deque[ScriptResult] = deque(scripted_scripts)
        self.prepared: list[str] = []
        self.runs: list[dict[str, Any]] = []
        self.scripts: list[dict[str, Any]] = []

    async def prepare(self, spec: RepoSpec, source_dir: Path, cache_key: str) -> EnvironmentRef:
        self.prepared.append(cache_key)
        return EnvironmentRef("fake", f"image-{cache_key}")

    async def run_tests(
        self,
        env: EnvironmentRef,
        source_dir: Path,
        results_dir: Path,
        spec: RepoSpec,
        *,
        container_name: str,
    ) -> SuiteResult:
        self.runs.append(
            {
                "image": env.identifier,
                "source": source_dir,
                "results": results_dir,
                "container": container_name,
            }
        )
        if not self._results_scripted:
            return DEFAULT_SUITE_RESULT
        if not self._results:
            raise AssertionError("more runs than scripted")
        return self._results.popleft()

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
        self.scripts.append(
            {
                "image": env.identifier,
                "source": source_dir,
                "script": script_path,
                "container": container_name,
                "timeout": timeout_seconds,
            }
        )
        if not self._scripts_scripted:
            return ScriptResult(exit_code=0)
        if not self._script_results:
            raise AssertionError("more scripts than scripted")
        return self._script_results.popleft()


class FakeWorkspace:
    """The `Workspace` Protocol over plain directories under `root`.

    Each label gets its own `export-<label>` and `results-<label>`, which is the
    isolation the real workspace provides and what the label-isolation tests
    assert. `discard` really deletes them, so a test can check that an unscored
    run cleaned up after itself rather than just that it called something.

    Recorded for assertions: `exported` (every label passed to `export_tree`, in
    order) and `discarded` (every label passed to `discard`).
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.exported: list[int | str] = []
        self.discarded: list[int | str] = []

    def _export_path(self, attempt: int | str) -> Path:
        return self.root / f"export-{attempt}"

    def _results_path(self, attempt: int | str) -> Path:
        return self.root / f"results-{attempt}"

    async def export_tree(self, attempt: int | str) -> Path:
        self.exported.append(attempt)
        path = self._export_path(attempt)
        path.mkdir(exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='x'\n")
        return path

    async def results_dir(self, attempt: int | str) -> Path:
        path = self._results_path(attempt)
        path.mkdir(exist_ok=True)
        return path

    async def discard(self, attempt: int | str) -> None:
        self.discarded.append(attempt)
        for path in (self._export_path(attempt), self._results_path(attempt)):
            shutil.rmtree(path, ignore_errors=True)
