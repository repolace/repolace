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

import os
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

#: The sentence `TaskWorkspace.export_tree` raises when the tree is dirty, kept
#: word for word: a tool whose test asserts on it should read the same on the
#: real thing. A test compares it with the real source.
EXPORT_REFUSAL = (
    "refusing to export: the working tree does not match HEAD, so the exported "
    "tree would not be the commit the results get attributed to"
)


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    """`{relative posix path: bytes}` of every regular file under `root`, right now.

    Symlinks are skipped rather than followed (a fake that read through one could
    capture a file outside the tree), and a path that is not a directory gives an
    empty snapshot, which is what a test passing a made-up `source_dir` means.
    """
    if not root.is_dir():
        return {}
    snapshot: dict[str, bytes] = {}
    for current, _directories, names in os.walk(root):
        for name in names:
            path = Path(current) / name
            if path.is_symlink():
                continue
            snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


class FakeBackend:
    """A `SandboxBackend` that records every call and returns what it was told to.

    With nothing scripted, every `run_tests` returns `DEFAULT_SUITE_RESULT` and
    every `run_script` returns a clean exit. Once results are scripted they come
    back strictly in order, and running **past the end of the script is an
    `AssertionError`** rather than a recycled last answer: a test that triggers
    one more sandbox run than it planned for has found a bug (a probe that
    recorded a row, a retry that should not have happened), and a fake that
    quietly kept answering would hide it.

    **A scripted item may be an exception instance**, which is raised when that
    item is reached. That is how the failure paths become testable: the real
    backend raises `EnvironmentBuildFailed` and `SandboxUnavailable`, and nothing
    else would let a test see what the pipeline does with them. `prepare_error`
    does the same for `prepare`, which is what makes `VerifierNotReady` reachable.

    Recorded for assertions -- **at call time**, because the evidence is deleted
    afterwards: `Verifier.run_subset` discards its directories in a `finally`, so
    by the time a test looks the files are gone. Every key is additive; none was
    ever removed.

    * `prepared` -- the cache key of each `prepare` call.
    * `prepare_calls` -- one dict per `prepare`: `cache_key`, `source`,
      `snapshot`, `spec`. The snapshot is how a test proves the image is built
      from a tree *without* the hidden-test overlay.
    * `runs` -- one dict per `run_tests`: `image`, `source`, `results`,
      `container`, plus `snapshot` (`{relative path: bytes}` of `source_dir` when
      the call was made) and `spec` (so `test_targets` and `timeout_seconds` are
      assertable).
    * `scripts` -- one dict per `run_script`: `image`, `source`, `script`,
      `container`, `timeout`, plus `snapshot`, `spec` and `script_text` (what the
      script file said when the call was made, or None if there was no file).

    **Where this differs from `DockerBackend`, and a test must not rely on:**

    * It runs nothing. The scripted result is returned whatever `source_dir`
      holds; no pytest, no report parsing, `results_dir` is never written to.
    * No containment is applied or checkable: no network cut, no read-only mount,
      no caps. The `timeout` and `spec.timeout_seconds` are recorded and **never
      enforced**, so a timeout path needs a scripted `ScriptResult(timed_out=True)`.
    * `container_name` is not validated or uniqueness-checked. A real daemon
      refuses a second container with a live name; this accepts a repeat.
    * `prepare` does not reuse a cached image by key: every call returns a fresh
      `EnvironmentRef("fake", "image-<key>")` and nothing is built.
    * It raises only what it was scripted to raise. The real backend can raise
      `SandboxError` subclasses from any call.
    * Exhausting a script is an `AssertionError`, which is an `Exception`: an
      `except Exception` between the fake and the test would turn the intended
      loud failure into whatever that handler does.
    * The snapshot reads the whole directory, so point it at small test trees;
      pass `snapshot=False` to skip it for a large one.
    """

    def __init__(
        self,
        results: Iterable[SuiteResult | BaseException] = (),
        scripts: Iterable[ScriptResult | BaseException] = (),
        *,
        prepare_error: Exception | None = None,
        snapshot: bool = True,
    ) -> None:
        scripted_results = list(results)
        scripted_scripts = list(scripts)
        # Whether anything was scripted is decided once, at construction: an
        # empty queue after use means "exhausted", an empty one at the start
        # means "use the default", and they must not be confused.
        self._results_scripted = bool(scripted_results)
        self._scripts_scripted = bool(scripted_scripts)
        self._results: deque[SuiteResult | BaseException] = deque(scripted_results)
        self._script_results: deque[ScriptResult | BaseException] = deque(scripted_scripts)
        self._prepare_error = prepare_error
        self._snapshot = snapshot
        self.prepared: list[str] = []
        self.prepare_calls: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.scripts: list[dict[str, Any]] = []

    def _snap(self, source_dir: Path) -> dict[str, bytes]:
        return _snapshot_tree(source_dir) if self._snapshot else {}

    @staticmethod
    def _next(queue: deque, scripted: bool, default: Any, exhausted: str) -> Any:
        if not scripted:
            return default
        if not queue:
            raise AssertionError(exhausted)
        item = queue.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    async def prepare(self, spec: RepoSpec, source_dir: Path, cache_key: str) -> EnvironmentRef:
        # Recorded before the scripted failure, so a test can see the build was
        # attempted and what it was attempted against.
        self.prepared.append(cache_key)
        self.prepare_calls.append(
            {"cache_key": cache_key, "source": source_dir, "snapshot": self._snap(source_dir), "spec": spec}
        )
        if self._prepare_error is not None:
            raise self._prepare_error
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
                "snapshot": self._snap(source_dir),
                "spec": spec,
            }
        )
        return self._next(self._results, self._results_scripted, DEFAULT_SUITE_RESULT, "more runs than scripted")

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
                "snapshot": self._snap(source_dir),
                "spec": spec,
                "script_text": (
                    script_path.read_text(encoding="utf-8", errors="replace") if script_path.is_file() else None
                ),
            }
        )
        return self._next(
            self._script_results, self._scripts_scripted, ScriptResult(exit_code=0), "more scripts than scripted"
        )


class FakeWorkspace:
    """The `Workspace` Protocol over plain directories under `root`.

    Each label gets its own `export-<label>` and `results-<label>`, which is the
    isolation the real workspace provides and what the label-isolation tests
    assert. `discard` really deletes them, so a test can check that an unscored
    run cleaned up after itself rather than just that it called something.

    **The tree can be dirty.** `set_dirty(True)` marks it as differing from HEAD,
    and `export_tree` then raises the same `RuntimeError` the real one does until
    `record_attempt` (the checkpoint) or `set_dirty(False)` clears it. That is the
    ordering a tool is most likely to get wrong -- checkpoint *before* handing the
    tree to the sandbox -- and a fake that never refused would let a tool that
    forgets it pass every test and fail on the first real task. A new workspace
    starts clean.

    Recorded for assertions: `exported` (every label passed to a *successful*
    `export_tree`, in order), `discarded` (every label passed to `discard`) and
    `checkpoints` (every message passed to `record_attempt`).

    **Where this differs from `TaskWorkspace`, and a test must not rely on:**

    * The export is a stub tree -- one `pyproject.toml` -- not the tracked tree at
      HEAD. There is no git, so no refusal of a symlink (mode 120000) or a
      submodule (160000), and no byte-identical export.
    * Directory modes are whatever the umask leaves; the real one chmods `0o777`
      all the way down and `results_dir` too. A sandbox-uid permission problem
      cannot show up here.
    * Dirtiness is only what `set_dirty` / `record_attempt` say. The real one
      compares the actual tree and index with HEAD.
    * A label may be any `int` or `str`, including `"1"`, which aliases the int
      `1` (the same `export-1` directory), and `"../x"`. `TaskWorkspace` refuses a
      str label that does not start with a letter or uses anything but letters,
      digits and `_.-` (it deletes the tree under the name); `container_name`
      refuses the same labels before a `Verifier` ever passes one here. This fake
      does neither, so a test of label hygiene belongs on the real workspace.
    * Re-exporting a label, or asking again for its results directory, is allowed
      here: it overwrites in place and keeps stale files. `TaskWorkspace` **raises**
      `RuntimeError("... already exists; labels are single-use")` for both, because
      whatever the sandbox left in a reused directory (a symlink, say) is not ours to
      write through. Only `discard` clears a label in either. A test of reuse belongs
      on the real workspace; this fake is deliberately not stricter, so a stream's
      existing tests that export one label twice keep working.
    * `discard` is `rmtree(ignore_errors=True)` on files this process owns, so it
      always works. The real one will meet files the sandbox's subuid created and
      the host cannot delete, so bounded disk over a 40-step loop is not
      guaranteed by the contract, only by the real implementation's best effort.
    * `TaskWorkspace.discard` removes on a worker thread and logs rather than
      raises when a path will not delete; this one is a plain `rmtree`.
    """

    def __init__(self, root: Path, *, dirty: bool = False) -> None:
        self.root = root
        self.exported: list[int | str] = []
        self.discarded: list[int | str] = []
        self.checkpoints: list[str] = []
        self._dirty = dirty

    @property
    def dirty(self) -> bool:
        return self._dirty

    def set_dirty(self, dirty: bool = True) -> None:
        """Mark the tree as differing (or not) from HEAD, as an edit would."""
        self._dirty = dirty

    async def record_attempt(self, message: str) -> str | None:
        """The checkpoint commit: clears the dirty flag, as `TaskWorkspace.record_attempt` does.

        Returns a fake sha, or None when there was nothing to commit -- the real
        one returns None for a clean tree too. Pass it as `ToolContext.checkpoint`.
        """
        self.checkpoints.append(message)
        was_dirty, self._dirty = self._dirty, False
        return f"fake-checkpoint-{len(self.checkpoints)}" if was_dirty else None

    def _export_path(self, attempt: int | str) -> Path:
        return self.root / f"export-{attempt}"

    def _results_path(self, attempt: int | str) -> Path:
        return self.root / f"results-{attempt}"

    async def export_tree(self, attempt: int | str) -> Path:
        if self._dirty:
            raise RuntimeError(EXPORT_REFUSAL)
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
