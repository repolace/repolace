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

import asyncio
import dataclasses
import itertools
import math
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

import structlog

from repolace_shared.git import redact
from verify.config import SCRIPT_PATH
from verify.dockerfile import image_cache_key
from verify.errors import SandboxError
from verify.overlay import apply_overlay, validate_overlay_paths
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

#: Directory mode for what the overlay creates. Matches the export's
#: (`TaskWorkspace`'s `_SANDBOX_DIR_MODE`, which this package does not import):
#: the sandbox runs as an unprivileged uid that is not ours and needs to create
#: entries beside its own tests.
_OVERLAY_DIR_MODE = 0o777

#: A scratch script is written here, mode 0644: readable by the sandbox uid, which
#: is not the owner.
_SCRIPT_FILE_MODE = 0o644

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


def _check_timeout(timeout_seconds: float | None) -> None:
    """`None` means the default; anything else must be a positive finite number.

    Not left to the backend's `x or default`: `0` is falsy and would run for the
    backend's 30-minute default instead of failing.
    """
    if timeout_seconds is None:
        return
    if not (math.isfinite(timeout_seconds) and timeout_seconds > 0):
        raise ValueError(f"timeout_seconds must be a positive number, got {timeout_seconds!r}")


def _check_targets(targets: Sequence[str]) -> tuple[str, ...]:
    """Refuse what would be read as something other than a list of test paths.

    A bare `str` is a `Sequence[str]`, so `tuple("tests/a.py")` would silently
    become one-character arguments. A leading `-` would be read by pytest as an
    option, and a leading `@` as a request to read more arguments from a file. The
    toolbox already refuses these, so reaching here with one is a bug in the caller --
    hence an error, not a result.
    """
    if isinstance(targets, str):
        raise ValueError(f"targets must be a sequence of strings, not the single string {targets!r}")
    checked = tuple(targets)
    for target in checked:
        if not isinstance(target, str) or not target:
            raise ValueError(f"a test target must be a non-empty string, got {target!r}")
        if target.startswith("-"):
            raise ValueError(f"a test target may not start with '-', got {target!r}")
        if target.startswith("@"):
            # pytest expands `@file` arguments by reading the named file for more
            # arguments, even after `--`, so this is an option in disguise.
            raise ValueError(f"a test target may not start with '@', got {target!r}")
    return checked


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
    for a live issue. Its keys are validated here, at construction, so a bad one
    fails before any build rather than after a forty-minute one.

    **The hidden tests are not hidden from the code under test.** During a scored
    run they sit in the container's `/repo`, readable by anything the suite
    imports; the protection is the feedback filter withholding their ids and the
    stdout tail from the model, not the sandbox. Do not assume otherwise when
    adding a feature that shows a scored run's output to the agent.

    **The workspace's parent directory must be quota-limited in production**
    (`task_workspace(parent_dir=...)` on a size- and inode-limited filesystem).
    A probe can write as much as it likes into its own export, a bind mount no
    container limit covers, and `discard` can leave behind entries the host cannot
    delete (files owned by the sandbox's subuid).
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
        validate_overlay_paths(self.overlay)
        self._env: EnvironmentRef | None = None
        # Only ever advanced, so a label is never reused -- not even after the run
        # that held it was discarded. `export-<label>` is written in place and the
        # plugin appends to `report.jsonl`, so a reused label would mix two runs.
        self._probe_numbers = itertools.count(1)
        self._script_numbers = itertools.count(1)

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

        **The overlay goes on after the environment is prepared, never before.**
        The image is built from the first export (`COPY source/ /repo/`) and is
        shared through a cache whose key does not cover the tests, so an overlay
        applied earlier would bake the hidden tests into a layer every later build
        reuses. Applied here it lands only in this run's export directory -- the one
        the sandbox mounts -- and on every scored run, baseline included, so the
        baseline and each attempt see the same tests.
        """
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

        if self.overlay:
            await asyncio.to_thread(
                apply_overlay, source_dir, self.overlay, dir_mode=_OVERLAY_DIR_MODE
            )

        return await self.backend.run_tests(
            self._env,
            source_dir,
            results_dir,
            self.spec,
            container_name=container_name(self.task_id, attempt),
        )

    def _require_environment(self, what: str) -> EnvironmentRef:
        if self._env is None:
            raise VerifierNotReady(
                f"cannot {what}: no environment has been prepared (the baseline has not "
                f"run, or its build failed)"
            )
        return self._env

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
        call alone, and must be positive and finite: `ValueError` otherwise, never
        a silent fall back to the default.

        **`targets` is validated here as well as by the tool**: a bare `str`, a
        non-string, an empty string or anything starting with `-` is a `ValueError`
        (a violation is a bug in the caller, not a sandbox result). An **empty
        sequence is a whole-suite probe** -- every visible test, still without the
        overlay -- and is allowed on purpose.

        **`targets` and the timeout reach the backend only through the spec:**
        `dataclasses.replace(self.spec, test_targets=tuple(targets),
        timeout_seconds=...)` is what `SandboxBackend.run_tests` receives. The
        Protocol has no `targets` or `timeout` parameter on `run_tests` and does
        not gain one, so a backend reads them from `spec.test_targets` and
        `spec.timeout_seconds`.

        The calling tool commits a checkpoint first, because `export_tree`
        refuses a tree that differs from HEAD -- which keeps this stage git-free.
        `targets` are pytest node ids or paths; the rootdir is pinned the same as
        for scored runs so node ids mean the same thing in both.
        """
        checked_targets = _check_targets(targets)
        _check_timeout(timeout_seconds)
        env = self._require_environment("run a test subset")
        label = f"probe-{next(self._probe_numbers)}"
        # Targets and the timeout travel in the spec, the only channel the
        # Protocol gives them. `None` leaves the spec's own timeout (and so the
        # backend's default) in force.
        spec = dataclasses.replace(
            self.spec,
            test_targets=checked_targets,
            timeout_seconds=self.spec.timeout_seconds if timeout_seconds is None else timeout_seconds,
        )
        name = container_name(self.task_id, label)

        try:
            source_dir = await workspace.export_tree(label)
            results_dir = await workspace.results_dir(label)
            return await self.backend.run_tests(
                env, source_dir, results_dir, spec, container_name=name
            )
        except SandboxError as exc:
            # Redacted again here, not only in the exception's constructor: this
            # text goes to the model, and a subclass that overrides `__str__`
            # would otherwise bypass the constructor's redaction.
            return SuiteResult(error=redact(str(exc)))
        finally:
            await asyncio.shield(workspace.discard(label))

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

        * **Where the script is written.** `<workspace.results_dir(label)>/_repolace_script.py`,
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

        **A script that times out returns no output**, however much it printed:
        the backend's process runner discards it on the kill path. Say so to the
        model rather than presenting an empty result as "it printed nothing".

        `timeout_seconds` must be positive and finite (`ValueError` otherwise).

        Raises `VerifierNotReady` when no environment has been prepared.
        """
        _check_timeout(timeout_seconds)
        if timeout_seconds is None:
            raise ValueError("timeout_seconds is required: the tool owns the policy")
        env = self._require_environment("run a script")
        label = f"script-{next(self._script_numbers)}"
        name = container_name(self.task_id, label)

        try:
            source_dir = await workspace.export_tree(label)
            results_dir = await workspace.results_dir(label)
            script_path = results_dir / Path(SCRIPT_PATH).name
            await asyncio.to_thread(_write_script, script_path, code)
            return await self.backend.run_script(
                env,
                source_dir,
                script_path,
                self.spec,
                container_name=name,
                timeout_seconds=timeout_seconds,
            )
        except SandboxError as exc:
            return ScriptResult(exit_code=None, error=redact(str(exc)))
        finally:
            await asyncio.shield(workspace.discard(label))


def _write_script(path: Path, code: str) -> None:
    """The model's code as bytes on disk, readable by a uid that is not ours.

    `errors="replace"` because the text comes from a model and a lone surrogate
    would otherwise raise out of a tool call. The mode is set explicitly, since
    `open`'s is masked by the umask and an unreadable script is a run that fails
    with a permissions error the model would try to debug in its own code.
    """
    path.write_bytes(code.encode("utf-8", errors="replace"))
    os.chmod(path, _SCRIPT_FILE_MODE)
