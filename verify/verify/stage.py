"""The seam the pipeline actually calls.

`run.py` should be able to run a suite without knowing that containers exist --
that is the point of `SandboxBackend` being a Protocol. Everything
container-shaped stops here: the pipeline sees `Verifier.run(workspace, attempt)`
and a `SuiteResult`.

Also the one place that knows the ordering rule the whole measurement rests on:
**the environment is built once, from the base commit, and reused for every
attempt.** If an attempt could rebuild it, a suite that started failing might be
the patch's doing or might be a dependency that resolved differently, and
`score` would have no way to tell -- which is precisely the ambiguity the
baseline exists to remove.
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

import structlog

from verify.dockerfile import image_cache_key
from verify.protocol import EnvironmentRef, RepoSpec, SandboxBackend, ScriptResult, SuiteResult
from verify.spec import install_commands

log = structlog.get_logger()

#: Docker requires `[a-zA-Z0-9][a-zA-Z0-9_.-]*`. A task id is a hex UUID and an
#: attempt is an int, so this only ever bites on a hand-built prefix.
_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]")

#: Mirrors `repolace_shared.db.models.BASELINE_ATTEMPT`. Duplicated rather
#: than imported: `verify` has no business pulling SQLAlchemy and pgvector
#: into a package whose whole point is to be swappable for a remote executor.
BASELINE_ATTEMPT = 0

#: What keys a run's export directory, results directory and container name.
#:
#: An **int** is a scored attempt -- 0 the baseline, 1..N the edit attempts --
#: and is the only kind that is ever recorded. A **str** is an unscored probe or
#: scratch run ("probe-3", "script-7"): its own keyspace, so a probe can never
#: collide with an attempt on disk, in the daemon, or in the database, and never
#: needs an attempt number it does not have. `container_name` enforces that the
#: two cannot be confused (a str label must start with a letter).
RunLabel = int | str

#: A str label: starts with a letter, so it can never read as an attempt number
#: (`"1"` would otherwise produce the same directory and container name as the
#: int 1), and limited to what Docker accepts in a name so nothing is silently
#: rewritten into a collision. At most 64 characters, because the label becomes a
#: directory name (`export-<label>`, `results-<label>`) and the filesystem's limit
#: is 255 bytes per component, not Docker's -- an over-long label would pass
#: `container_name` and then fail at `mkdir` with `ENAMETOOLONG`.
_LABEL = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}")


class Workspace(Protocol):
    """The slice of `TaskWorkspace` this module needs.

    Structural rather than an import, so `verify` does not depend on the git
    package's concrete class and the tests can pass a directory pair.
    """

    async def export_tree(self, attempt: RunLabel) -> Path: ...

    async def results_dir(self, attempt: RunLabel) -> Path: ...

    async def discard(self, attempt: RunLabel) -> None:
        """Delete that label's export and results directories, best effort.

        For unscored runs: an agent loop of 40 steps would otherwise leave 40
        full copies of the tree on disk, and a stale file in one scratch run's
        directory must not be visible to the next. Best effort because it runs
        on the way out of a path that may already be failing -- a leftover
        directory is a disk leak worth logging, not an exception worth raising.
        """
        ...


class VerifierNotReady(RuntimeError):
    """A probe or script was requested before the baseline built the environment.

    `run_subset` and `run_script` reuse the environment the baseline prepared and
    never build one themselves: a probe that could trigger the build would
    prepare the image from a tree the agent had already edited, which is exactly
    the ambiguity "built once, from the base commit" exists to remove. So when
    there is no environment -- the baseline has not run, or its build failed --
    the answer is this error, which the tools turn into a plain "sandbox
    unavailable" message for the model rather than letting it escape the loop.
    """


def container_name(task_id: uuid.UUID, run: RunLabel) -> str:
    """Unique per task and run, so two concurrent tasks cannot collide.

    Collision matters more than it looks: `run_tests` removes the container by
    name on the timeout path, and a shared name would have one task killing
    another task's running suite.

    An int gives exactly the name it always did. A str label is validated rather
    than sanitised: rewriting `probe/3` into `probe-3` would let two distinct
    labels share one name, which is the collision this function exists to rule
    out -- so a bad label is an error at the call that made it.
    """
    if isinstance(run, str) and not _LABEL.fullmatch(run):
        raise ValueError(
            f"run label {run!r} must start with a letter, use only letters, digits, '_', '.', '-', "
            f"and be at most 64 characters"
        )
    return _NAME_UNSAFE.sub("-", f"repolace-{task_id.hex[:12]}-{run}")


class Verifier:
    """One task's Verify stage: build the environment once, then run per attempt.

    Three kinds of run, with deliberately different rules. **Scored** runs
    (`run`) are keyed by an int attempt, include the overlay, are recorded by
    the caller, and guard the baseline. **Probes** (`run_subset`) and **scripts**
    (`run_script`) are the agent's own tools: unscored, label-keyed, never
    recorded, and never carrying the overlay.

    `overlay` is the hidden-test files of a benchmark instance, as complete
    file bytes keyed by repo-relative path. It is applied to every *scored* run
    -- baseline included, so the baseline and every attempt see the same tests --
    and only after the environment is built, outside the image cache key, so
    the hidden tests are never baked into a layer the cache shares. It is `None`
    for a live issue.
    """

    def __init__(
        self,
        backend: SandboxBackend,
        spec: RepoSpec,
        task_id: uuid.UUID,
        *,
        overlay: Mapping[str, bytes] | None = None,
    ) -> None:
        self.backend = backend
        self.spec = spec
        self.task_id = task_id
        # A read-only copy: the keys feed `hidden_paths`, which the feedback
        # filter trusts for the whole task, so a caller mutating the dict it
        # passed in must not be able to change what counts as hidden.
        self.overlay: Mapping[str, bytes] = MappingProxyType(dict(overlay or {}))
        self._env: EnvironmentRef | None = None

    @property
    def prepared(self) -> bool:
        return self._env is not None

    @property
    def hidden_paths(self) -> frozenset[str]:
        """The overlay's paths: files whose contents the agent must never see.

        Empty when there is no overlay. The agent loop uses it to filter the
        feedback it shows the model: node ids and collect failures from these
        files *are* the oracle. (The files themselves are never in the agent's
        checkout -- the overlay goes onto the export, not the working tree -- so
        the toolbox has nothing to refuse reading.)
        """
        return frozenset(self.overlay)

    async def run(self, workspace: Workspace, attempt: int) -> SuiteResult:
        """Export the tree, prepare the environment if needed, run the suite.

        The export happens first and unconditionally, because `export_tree`
        refuses when the working tree does not match HEAD. That refusal is the
        precondition the whole stage rests on: without it the sandbox would test
        HEAD's content while the results got attributed to an unstaged edit --
        a confidently wrong measurement, which is the worst shape this can fail
        in.

        With a non-empty overlay this raises `NotImplementedError` until stream
        A applies it. Loudly, and before anything is exported: running the suite
        without the hidden tests would score the task against the wrong tests,
        and that is a confidently wrong number, not an error anyone would see.
        """
        if self.overlay:
            raise NotImplementedError(
                "applying the hidden-test overlay lands in stream A: sandbox"
            )

        source_dir = await workspace.export_tree(attempt)
        results_dir = await workspace.results_dir(attempt)

        if self._env is None:
            install = install_commands(self.spec, source_dir)
            cache_key = image_cache_key(self.spec, install, source_dir)
            self._env = await self.backend.prepare(self.spec, source_dir, cache_key)
            log.info(
                "verify.environment.ready",
                attempt=attempt,
                identifier=self._env.identifier,
                cache_key=cache_key,
            )
        elif attempt == BASELINE_ATTEMPT:
            # Defensive, and worth an exception rather than a log line: a second
            # baseline would mean the caller looped in a way that invalidates
            # every comparison downstream of it.
            raise RuntimeError("the baseline has already run for this task")

        return await self.backend.run_tests(
            self._env,
            source_dir,
            results_dir,
            self.spec,
            container_name=container_name(self.task_id, attempt),
        )

    async def run_subset(
        self,
        workspace: Workspace,
        targets: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> SuiteResult:
        """Run some of the visible tests, for the agent's `run_tests` tool.

        A probe, not a measurement. The rules below are what keep it from
        corrupting the one that is, and every one of them fails silently:

        * **Never includes the overlay.** It would hand the agent the hidden
          fail-to-pass tests -- the oracle -- through the one tool built to show
          it test output.
        * **Never recorded.** The result goes back to the tool, not into
          `task_test_runs`, and carries no attempt number.
        * **Never trips the baseline guard** -- it is not attempt 0, and it must
          not make a later real attempt look like a second baseline.
        * **A fresh label per call**, `"probe-<n>"`, where `<n>` comes from a
          per-`Verifier` counter that only ever increases: never reused, not even
          after the previous probe was discarded, so its export, results
          directory and container name collide with nothing. (`script-<n>` for
          `run_script` has its own counter.) The label is at most 64 characters
          and starts with a letter, which `container_name` enforces.
        * **Discards its directories when done** -- `workspace.discard(label)`,
          in a `finally`, cancellation included -- so a 40-step loop leaves
          bounded disk behind and one probe's files cannot reach the next.
        * Reuses the prepared environment and never builds one: raises
          `VerifierNotReady` when there is none.

        **What comes back, and what does not.** A `SandboxError` from the backend
        -- `SandboxUnavailable`, `SandboxTimeout`, `EnvironmentBuildFailed` -- is
        **returned** as `SuiteResult(error=<redacted message>)`, never raised, as
        `pipeline._verify` does for a scored run: the tool reports an `error` like
        any other result, and has exactly one thing to catch.
        **`VerifierNotReady` is the only exception** this raises for a sandbox
        reason. Anything that is not a `SandboxError` -- an `OSError` from the
        workspace, a bug -- propagates, as it should.

        `timeout_seconds=None` means the spec's own `timeout_seconds`, or the
        backend's default when that is None too. A number overrides both for this
        call alone.

        **`targets` and the timeout reach the backend only through the spec:**
        `dataclasses.replace(self.spec, test_targets=tuple(targets),
        timeout_seconds=...)` is what `SandboxBackend.run_tests` receives. The
        Protocol has no `targets` or `timeout` parameter on `run_tests` and does
        not gain one, so a backend reads them from `spec.test_targets` and
        `spec.timeout_seconds`.

        The calling tool commits a checkpoint first, because `export_tree`
        refuses a tree that differs from HEAD -- which keeps this stage git-free.
        `targets` are pytest node ids or paths, validated by the caller; the
        rootdir is pinned the same as for scored runs so node ids mean the same
        thing in both.
        """
        raise NotImplementedError("Verifier.run_subset lands in stream A: sandbox")

    async def run_script(
        self,
        workspace: Workspace,
        code: str,
        *,
        timeout_seconds: float,
    ) -> ScriptResult:
        """Run a scratch script against the current tree, for `run_python`.

        Same rules as `run_subset` -- never the overlay, never recorded, never
        the baseline guard, a fresh label per call (`"script-<n>"`, from its own
        per-`Verifier` counter), directories discarded in a `finally`, a
        `SandboxError` **returned** as `ScriptResult(error=<redacted message>)`
        rather than raised, `VerifierNotReady` the only exception -- plus these:

        * **Where the script is written.** `<workspace.results_dir(label)>/main.py`,
          mode 0644. That is outside both trees: not in the checkout, which
          `git add -A` would sweep into the next checkpoint commit and the PR, and
          not in the export, which is mounted read-only and which the host might
          later diff. It is also the one host-side directory `workspace.discard`
          removes, so the script cannot outlive the call -- `Workspace` has no
          scratch directory of its own, so do not reach for `tempfile`, whose
          output `discard` never sees.
        * **`timeout_seconds` is required**, not defaulted: the tool owns the
          policy, and a script with no limit holds a container and its memory cap
          for as long as the model likes. Unlike a probe's, it reaches the
          backend as `SandboxBackend.run_script`'s own `timeout_seconds` argument,
          which the Protocol already has.

        Raises `VerifierNotReady` when no environment has been prepared.
        """
        raise NotImplementedError("Verifier.run_script lands in stream A: sandbox")
