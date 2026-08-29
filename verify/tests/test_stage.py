"""The Verify stage's ordering rules.

One rule carries the whole measurement: **the environment is built once, from
the base commit, and reused for every attempt.** If an attempt could rebuild it,
a suite that started failing might be the patch's doing or might be a dependency
that resolved differently, and `score` has no way to tell those apart -- which
is the exact ambiguity the baseline exists to remove.
"""

import uuid
from pathlib import Path

import pytest

from verify.protocol import EnvironmentRef, RepoSpec, SuiteResult
from verify.stage import BASELINE_ATTEMPT, Verifier, container_name

pytestmark = pytest.mark.anyio


class FakeBackend:
    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.runs: list[dict] = []

    async def prepare(self, spec: RepoSpec, source_dir: Path, cache_key: str) -> EnvironmentRef:
        self.prepared.append(cache_key)
        return EnvironmentRef(backend="fake", identifier=f"image-{cache_key}")

    async def run_tests(self, env, source_dir, results_dir, spec, *, container_name):
        self.runs.append(
            {
                "image": env.identifier,
                "source": source_dir,
                "results": results_dir,
                "container": container_name,
            }
        )
        return SuiteResult(passed=("t::a",))


class FakeWorkspace:
    """Just the two methods `Verifier` needs, which is why `Workspace` is a Protocol."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.exported: list[int] = []

    async def export_tree(self, attempt: int) -> Path:
        self.exported.append(attempt)
        path = self.root / f"export-{attempt}"
        path.mkdir(exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='x'\n")
        return path

    async def results_dir(self, attempt: int) -> Path:
        path = self.root / f"results-{attempt}"
        path.mkdir(exist_ok=True)
        return path


@pytest.fixture
def workspace(tmp_path):
    return FakeWorkspace(tmp_path)


class TestEnvironmentReuse:
    async def test_the_image_is_built_once_and_reused(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert len(verifier.backend.prepared) == 1

    async def test_both_attempts_run_in_the_same_image(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert len({run["image"] for run in verifier.backend.runs}) == 1

    async def test_prepared_reports_whether_an_environment_exists(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())
        assert not verifier.prepared

        await verifier.run(workspace, BASELINE_ATTEMPT)

        assert verifier.prepared


class TestPerAttemptIsolation:
    async def test_each_attempt_gets_its_own_export_and_results_directory(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert workspace.exported == [BASELINE_ATTEMPT, 1]
        assert len({run["source"] for run in verifier.backend.runs}) == 2
        assert len({run["results"] for run in verifier.backend.runs}) == 2

    async def test_each_attempt_gets_its_own_container_name(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())

        await verifier.run(workspace, BASELINE_ATTEMPT)
        await verifier.run(workspace, 1)

        assert len({run["container"] for run in verifier.backend.runs}) == 2

    async def test_a_second_baseline_is_refused(self, workspace):
        """It would mean the caller looped in a way that invalidates every
        comparison downstream of it."""
        verifier = Verifier(FakeBackend(), RepoSpec(key="a/b"), uuid.uuid4())
        await verifier.run(workspace, BASELINE_ATTEMPT)

        with pytest.raises(RuntimeError, match="baseline"):
            await verifier.run(workspace, BASELINE_ATTEMPT)


class TestContainerName:
    def test_two_tasks_cannot_collide(self):
        """Collision matters more than it looks: the timeout path removes the
        container by name, so a shared name has one task killing another's suite."""
        a, b = uuid.uuid4(), uuid.uuid4()

        assert container_name(a, 0) != container_name(b, 0)

    def test_it_is_a_legal_docker_name(self):
        import re

        name = container_name(uuid.uuid4(), 1)

        assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name)
