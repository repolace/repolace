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
from pathlib import Path
from typing import Protocol

import structlog

from verify.dockerfile import image_cache_key
from verify.protocol import EnvironmentRef, RepoSpec, SandboxBackend, SuiteResult
from verify.spec import install_commands

log = structlog.get_logger()

#: Docker requires `[a-zA-Z0-9][a-zA-Z0-9_.-]*`. A task id is a hex UUID and an
#: attempt is an int, so this only ever bites on a hand-built prefix.
_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]")

#: Mirrors `repolace_shared.db.models.BASELINE_ATTEMPT`. Duplicated rather
#: than imported: `verify` has no business pulling SQLAlchemy and pgvector
#: into a package whose whole point is to be swappable for a remote executor.
BASELINE_ATTEMPT = 0


class Workspace(Protocol):
    """The slice of `TaskWorkspace` this module needs.

    Structural rather than an import, so `verify` does not depend on the git
    package's concrete class and the tests can pass a directory pair.
    """

    async def export_tree(self, attempt: int) -> Path: ...

    async def results_dir(self, attempt: int) -> Path: ...


def container_name(task_id: uuid.UUID, attempt: int) -> str:
    """Unique per task and attempt, so two concurrent tasks cannot collide.

    Collision matters more than it looks: `run_tests` removes the container by
    name on the timeout path, and a shared name would have one task killing
    another task's running suite.
    """
    return _NAME_UNSAFE.sub("-", f"repolace-{task_id.hex[:12]}-{attempt}")


class Verifier:
    """One task's Verify stage: build the environment once, then run per attempt."""

    def __init__(self, backend: SandboxBackend, spec: RepoSpec, task_id: uuid.UUID) -> None:
        self.backend = backend
        self.spec = spec
        self.task_id = task_id
        self._env: EnvironmentRef | None = None

    @property
    def prepared(self) -> bool:
        return self._env is not None

    async def run(self, workspace: Workspace, attempt: int) -> SuiteResult:
        """Export the tree, prepare the environment if needed, run the suite.

        The export happens first and unconditionally, because `export_tree`
        refuses when the working tree does not match HEAD. That refusal is the
        precondition the whole stage rests on: without it the sandbox would test
        HEAD's content while the results got attributed to an unstaged edit --
        a confidently wrong measurement, which is the worst shape this can fail
        in.
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

        return await self.backend.run_tests(
            self._env,
            source_dir,
            results_dir,
            self.spec,
            container_name=container_name(self.task_id, attempt),
        )
