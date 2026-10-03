"""When an edit attempt is scored, and when it is not.

Real git, real `Verifier`, shared fake sandbox: the rules are about commits and trees, which
only a repository can express. `record` is a list, so none of this needs a database -- the
`task_test_runs` write is covered by the end-to-end tests.
"""

import pytest
from repolace_shared.git import task_workspace
from verify.errors import EnvironmentBuildFailed, SandboxTimeout, SandboxUnavailable
from verify.protocol import RepoSpec, SuiteResult
from verify.stage import Verifier
from verify.testing import FakeBackend

from repolace_pipeline.attempts import AttemptScorer

from pipeline_support import TASK_ID, git, write

pytestmark = pytest.mark.anyio

BASELINE = SuiteResult(passed=("t::a",))
PASSING = SuiteResult(passed=("t::a", "t::b"))


class Recorded:
    def __init__(self) -> None:
        self.rows: list[tuple[int, str, SuiteResult]] = []

    async def __call__(self, attempt: int, sha: str, result: SuiteResult) -> None:
        self.rows.append((attempt, sha, result))


@pytest.fixture
async def workspace(origin_url):
    async with task_workspace("acme", "sample", "main", clone_url=origin_url) as ws:
        await ws.start_agent_branch(7, TASK_ID)
        yield ws


async def scorer_for(workspace, backend, *, overlay=None):
    verifier = Verifier(backend, RepoSpec(key="acme/sample"), TASK_ID, overlay=overlay)
    await verifier.run(workspace, 0)
    recorded = Recorded()
    return AttemptScorer(verifier=verifier, workspace=workspace, record=recorded), recorded, verifier


def edit(workspace, name: str, text: str) -> None:
    write(workspace.path / name, text)


class TestWhatIsScored:
    async def test_a_changed_tree_is_committed_scored_and_recorded(self, workspace):
        backend = FakeBackend(results=[BASELINE, PASSING])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")

        record = await scorer.verify_attempt(1)

        head = git(workspace.path, "rev-parse", "HEAD")
        assert record is not None
        assert (record.attempt, record.commit_sha, record.infrastructure_error) == (1, head, False)
        assert record.result == PASSING
        assert recorded.rows == [(1, head, PASSING)]

    async def test_the_suite_ran_against_the_committed_edit_in_a_run_of_its_own(self, workspace):
        backend = FakeBackend(results=[BASELINE, PASSING])
        scorer, _, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")

        await scorer.verify_attempt(1)

        assert len(backend.runs) == 2
        assert backend.runs[1]["snapshot"]["src/app.py"] == b"X = 1\n"
        assert backend.runs[1]["container"].endswith("-1")

    async def test_the_unfiltered_result_is_what_is_recorded_and_returned(self, workspace):
        raw = SuiteResult(passed=("t::a",), failed=("tests/test_hidden.py::test_f2p",), stdout_tail="raw output")
        scorer, recorded, _ = await scorer_for(workspace, FakeBackend(results=[BASELINE, raw]))
        edit(workspace, "src/app.py", "X = 1\n")

        record = await scorer.verify_attempt(1)

        assert record.result is raw
        assert recorded.rows[0][2] is raw

    async def test_a_checkpoint_commit_made_before_the_call_is_the_state_that_is_scored(self, workspace):
        """`run_python` commits the tree first, so `record_attempt` finds nothing to commit -- and the edit
        is still new, and the sha at HEAD is what ran."""
        backend = FakeBackend(results=[BASELINE, PASSING])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")
        checkpoint = await workspace.record_attempt("checkpoint: before script")

        record = await scorer.verify_attempt(1)

        assert record is not None
        assert record.commit_sha == checkpoint
        assert recorded.rows[0][1] == checkpoint


class TestWhenThereIsNothingToScore:
    async def test_no_edit_at_all_is_none_and_the_suite_is_not_run(self, workspace):
        backend = FakeBackend(results=[BASELINE])
        scorer, recorded, _ = await scorer_for(workspace, backend)

        assert await scorer.verify_attempt(1) is None

        assert len(backend.runs) == 1, "only the baseline ran"
        assert recorded.rows == []

    async def test_an_edit_reverted_to_the_base_is_none_even_though_commits_exist(self, workspace):
        """The question is the tree against the base, not whether anything was ever committed."""
        backend = FakeBackend(results=[BASELINE])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        original = (workspace.path / "src" / "app.py").read_text()
        edit(workspace, "src/app.py", "X = 1\n")
        await workspace.record_attempt("an edit")
        edit(workspace, "src/app.py", original)

        assert await scorer.verify_attempt(1) is None
        assert git(workspace.path, "rev-list", "--count", "HEAD") == "3", "the edit and its revert are both commits"
        assert len(backend.runs) == 1

    async def test_a_second_call_with_no_new_edit_is_none_against_the_last_scored_attempt(self, workspace):
        backend = FakeBackend(results=[BASELINE, PASSING])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")
        assert await scorer.verify_attempt(1) is not None

        # The tree differs from the base (so question 1 says "changed") and not from attempt 1.
        assert await scorer.verify_attempt(2) is None

        assert len(backend.runs) == 2, "no suite re-run for an unchanged tree"
        assert [row[0] for row in recorded.rows] == [1]

    async def test_a_new_commit_with_the_same_tree_is_none(self, workspace):
        """An edit and its own revert make a new commit and an old tree: nothing new to learn."""
        backend = FakeBackend(results=[BASELINE, PASSING])
        scorer, _, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")
        await scorer.verify_attempt(1)
        edit(workspace, "src/app.py", "X = 2\n")
        await workspace.record_attempt("try something")
        edit(workspace, "src/app.py", "X = 1\n")

        assert await scorer.verify_attempt(2) is None
        assert len(backend.runs) == 2

    async def test_a_none_does_not_use_up_the_attempt_number(self, workspace):
        backend = FakeBackend(results=[BASELINE, PASSING])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        assert await scorer.verify_attempt(1) is None
        edit(workspace, "src/app.py", "X = 1\n")

        record = await scorer.verify_attempt(1)

        assert record is not None and record.attempt == 1


class TestAttemptsBuildOnEachOther:
    async def test_nothing_is_rewound_between_attempts(self, workspace):
        """The prompt says earlier edits persist, so the second attempt's tree contains the first's."""
        backend = FakeBackend(results=[BASELINE, SuiteResult(passed=("t::a",), failed=("t::x",)), PASSING])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/first.py", "FIRST = 1\n")
        await scorer.verify_attempt(1)
        edit(workspace, "src/second.py", "SECOND = 1\n")

        record = await scorer.verify_attempt(2)

        snapshot = backend.runs[2]["snapshot"]
        assert snapshot["src/first.py"] == b"FIRST = 1\n"
        assert snapshot["src/second.py"] == b"SECOND = 1\n"
        assert (workspace.path / "src" / "first.py").exists()
        assert record.attempt == 2
        assert [row[0] for row in recorded.rows] == [1, 2]

    async def test_each_attempt_runs_in_its_own_export(self, workspace):
        backend = FakeBackend(results=[BASELINE, PASSING, PASSING])
        scorer, _, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/a.py", "A = 1\n")
        await scorer.verify_attempt(1)
        edit(workspace, "src/b.py", "B = 1\n")
        await scorer.verify_attempt(2)

        assert len({run["source"] for run in backend.runs}) == 3, "baseline, attempt 1 and attempt 2"
        assert backend.runs[1]["container"] != backend.runs[2]["container"]


class TestSandboxFailures:
    async def test_a_daemon_that_is_not_there_is_an_infrastructure_error_and_still_recorded(self, workspace):
        backend = FakeBackend(results=[BASELINE, SandboxUnavailable("cannot connect")])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")

        record = await scorer.verify_attempt(1)

        assert record.infrastructure_error is True
        assert record.result.error and "container runtime unavailable" in record.result.error
        assert len(recorded.rows) == 1, "an attempt with no row would look like a task that never got this far"

    @pytest.mark.parametrize(
        "failure",
        [SandboxTimeout(1800, ["python", "-m", "pytest"]), EnvironmentBuildFailed("acme/sample", 1, "boom")],
    )
    async def test_a_suite_that_timed_out_or_would_not_build_is_the_patchs_doing(self, workspace, failure):
        backend = FakeBackend(results=[BASELINE, failure])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")

        record = await scorer.verify_attempt(1)

        assert record.infrastructure_error is False
        assert record.result.error
        assert len(recorded.rows) == 1

    async def test_a_bug_in_repolace_is_not_laundered_into_an_unscoreable_run(self, workspace):
        backend = FakeBackend(results=[BASELINE, OSError("disk full")])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")

        with pytest.raises(OSError, match="disk full"):
            await scorer.verify_attempt(1)

        assert recorded.rows == []


class TestGuards:
    @pytest.mark.parametrize("bad", [0, -1, True])
    async def test_an_attempt_number_below_one_is_refused_before_anything_runs(self, workspace, bad):
        backend = FakeBackend(results=[BASELINE])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")

        with pytest.raises(ValueError, match="1 or more"):
            await scorer.verify_attempt(bad)

        assert len(backend.runs) == 1 and recorded.rows == []

    async def test_a_repeated_attempt_number_is_refused_rather_than_failing_at_the_database(self, workspace):
        backend = FakeBackend(results=[BASELINE, PASSING])
        scorer, recorded, _ = await scorer_for(workspace, backend)
        edit(workspace, "src/app.py", "X = 1\n")
        await scorer.verify_attempt(1)
        edit(workspace, "src/app.py", "X = 2\n")

        with pytest.raises(ValueError, match="already scored"):
            await scorer.verify_attempt(1)

        assert len(backend.runs) == 2 and len(recorded.rows) == 1

    async def test_it_refuses_to_run_before_the_environment_was_built_from_the_base(self, workspace):
        verifier = Verifier(FakeBackend(), RepoSpec(key="acme/sample"), TASK_ID)
        scorer = AttemptScorer(verifier=verifier, workspace=workspace, record=Recorded())
        edit(workspace, "src/app.py", "X = 1\n")

        with pytest.raises(RuntimeError, match="before the environment was built"):
            await scorer.verify_attempt(1)


class TestTheOverlay:
    OVERLAY = {"tests/test_hidden.py": b"def test_f2p():\n    assert True\n"}

    async def test_the_overlay_reaches_every_scored_export_and_never_the_checkout(self, workspace):
        backend = FakeBackend(results=[BASELINE, PASSING])
        scorer, _, verifier = await scorer_for(workspace, backend, overlay=self.OVERLAY)
        edit(workspace, "src/app.py", "X = 1\n")

        await scorer.verify_attempt(1)

        assert backend.runs[0]["snapshot"]["tests/test_hidden.py"] == self.OVERLAY["tests/test_hidden.py"]
        assert backend.runs[1]["snapshot"]["tests/test_hidden.py"] == self.OVERLAY["tests/test_hidden.py"]
        assert not (workspace.path / "tests" / "test_hidden.py").exists()
        assert "tests/test_hidden.py" not in await workspace.changed_files()
        assert verifier.hidden_paths == frozenset(self.OVERLAY)

    async def test_an_overlay_alone_is_not_a_change(self, workspace):
        """The overlay lands on the export, so a tree the agent did not touch is still 'no change'."""
        backend = FakeBackend(results=[BASELINE])
        scorer, _, _ = await scorer_for(workspace, backend, overlay=self.OVERLAY)

        assert await scorer.verify_attempt(1) is None
