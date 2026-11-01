"""The pieces of a resume that need no database: the class of a stop, the patch packing, the provenance guard,
the inverse of `to_row`, and the scorer picking up where a paused task left off (against real git).
"""

import asyncio
import zlib
from decimal import Decimal

import pytest
from pipeline_support import TASK_ID, write
from repolace_agents.contracts import AgentLimits, AttemptRecord
from repolace_gateway.budget import BudgetExceeded, BudgetLimit
from repolace_gateway.errors import KIND_AUTH, KIND_OTHER, KIND_QUOTA, LLMCallError
from repolace_pipeline import checkpoints
from repolace_pipeline.attempts import AttemptScorer
from repolace_pipeline.checkpoints import (
    pack,
    provenance_for,
    provenance_mismatch,
    unpack,
)
from repolace_pipeline.stopclass import DESIGN, UNAVAILABLE, classify_exception
from repolace_pipeline.testruns import to_row, to_suite_result
from repolace_shared.git import task_workspace
from verify.errors import SandboxTimeout, SandboxUnavailable
from verify.protocol import RepoSpec, SuiteResult
from verify.stage import Verifier
from verify.testing import FakeBackend


def llm_error(kind: str, *, transient: bool = False) -> LLMCallError:
    return LLMCallError("x", stage="agent", model="m", retries=3, kind=kind, transient=transient)


class WorkerTimeout(asyncio.CancelledError):
    """Named like the worker's, which the classifier recognises by name."""


class TestTheClassOfAStop:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (llm_error(KIND_QUOTA), UNAVAILABLE),
            (llm_error(KIND_OTHER, transient=True), UNAVAILABLE),
            (llm_error(KIND_AUTH), DESIGN),
            (llm_error(KIND_OTHER), DESIGN),
            (SandboxUnavailable("the daemon is down"), UNAVAILABLE),
            (SandboxTimeout(60.0, ["pytest"]), DESIGN),
            (asyncio.CancelledError(), UNAVAILABLE),
            (KeyboardInterrupt(), UNAVAILABLE),
            (WorkerTimeout(), DESIGN),
            (BudgetExceeded(BudgetLimit.USD, spent_usd=Decimal(3), calls=1, elapsed_seconds=1.0), DESIGN),
            (ValueError("a bug"), DESIGN),
            (RuntimeError("anything else"), DESIGN),
        ],
        ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else v,
    )
    def test_each_ending_has_one_class(self, exc, expected):
        assert classify_exception(exc) == expected

    def test_the_class_is_decided_from_the_type_never_from_the_text(self):
        assert classify_exception(RuntimeError("provider quota exhausted: agent call")) == DESIGN
        assert classify_exception(llm_error(KIND_OTHER)) == DESIGN


class TestPatchPacking:
    def test_a_patch_round_trips_and_an_empty_one_stays_empty(self):
        patch = b"diff --git a/x b/x\n" * 1000
        assert unpack(pack(patch)) == patch
        assert pack(b"") == b"" and unpack(b"") == b""

    def test_a_compressed_bomb_is_refused_not_inflated(self, monkeypatch):
        monkeypatch.setattr(checkpoints, "MAX_PATCH_BYTES", 1000)
        with pytest.raises(ValueError, match="inflates past"):
            unpack(zlib.compress(b"0" * 100_000))


LIMITS = AgentLimits()
BUDGET = {"max_usd": "2.00", "max_calls": 150, "max_wall_seconds": 3600.0}


class TestProvenance:
    def make(self, **overrides):
        values = {"code": "a" * 40, "model": "vertex-gemini-3-1-pro-preview", "limits": LIMITS, "budget": BUDGET}
        return provenance_for(**{**values, **overrides})

    def test_the_same_run_matches(self):
        assert provenance_mismatch(self.make(), self.make()) is None

    @pytest.mark.parametrize(
        ("change", "named"),
        [
            ({"code": "b" * 40}, "code"),
            ({"model": "claude-sonnet-5-5"}, "model"),
            ({"limits": AgentLimits(max_attempts=2)}, "limits"),
            ({"budget": {**BUDGET, "max_usd": "4.00"}}, "budget"),
        ],
    )
    def test_a_difference_is_named(self, change, named):
        message = provenance_mismatch(self.make(), self.make(**change))
        assert message is not None and message.startswith(named)

    def test_a_value_either_side_could_not_learn_is_not_compared(self):
        assert provenance_mismatch(self.make(code=None), self.make()) is None
        assert provenance_mismatch(self.make(), self.make(model=None)) is None


class TestTheInverseOfToRow:
    def test_every_field_of_a_result_comes_back(self):
        result = SuiteResult(
            passed=("a::1",), failed=("a::2",), skipped=("a::3",), xfailed=("a::4",), did_not_run=("a::5",),
            collect_failures=("a.py",), collected_files=("a.py",), conftests=("conftest.py",),
            fingerprint={"rootdir": "/r", "ini": {"x": 1}}, exit_code=1, duration_seconds=12.5,
            stdout_tail="tail", error=None,
        )

        assert to_suite_result(to_row(TASK_ID, 2, "a" * 40, result)) == result

    def test_an_unscoreable_run_keeps_its_error(self):
        result = SuiteResult(error="container runtime unavailable")
        assert to_suite_result(to_row(TASK_ID, 1, "a" * 40, result)).error == "container runtime unavailable"


@pytest.mark.anyio
class TestTheScorerPicksUpWhereItStopped:
    @pytest.fixture
    async def workspace(self, origin_url):
        async with task_workspace("acme", "sample", "main", clone_url=origin_url) as ws:
            await ws.start_agent_branch(7, TASK_ID)
            yield ws

    async def scorer(self, workspace, results):
        backend = FakeBackend(results=results)
        verifier = Verifier(backend, RepoSpec(key="acme/sample"), TASK_ID)
        await verifier.run(workspace, 0)
        recorded: list[tuple[int, str]] = []

        async def record(attempt, sha, result):
            recorded.append((attempt, sha))

        return AttemptScorer(verifier=verifier, workspace=workspace, record=record), recorded

    async def test_a_restored_scorer_continues_numbering_and_compares_against_the_restored_tree(self, workspace):
        scorer, recorded = await self.scorer(workspace, [SuiteResult(passed=("t::a",)), SuiteResult(passed=("t::a", "t::b"))])
        write(workspace.path / "src" / "app.py", "VALUE = 'scored'\n")
        await workspace.record_attempt("restored: last scored attempt")
        sha = await workspace.repo.head_sha()
        last = AttemptRecord(1, sha, SuiteResult(passed=("t::a",)), False)

        scorer.restore(last, 1)

        assert scorer.attempts == 1 and scorer.next_attempt == 2 and scorer.last_record == last
        # Nothing changed since the restored scored tree: nothing to score.
        assert await scorer.verify_attempt(2) is None
        write(workspace.path / "src" / "app.py", "VALUE = 'more'\n")
        record = await scorer.verify_attempt(2)
        assert record is not None and record.attempt == 2 and recorded == [(2, record.commit_sha)]
        assert scorer.attempts == 2

    async def test_attempt_one_cannot_be_scored_again_after_a_restore(self, workspace):
        scorer, _ = await self.scorer(workspace, [SuiteResult(passed=("t::a",))])
        scorer.restore(AttemptRecord(1, await workspace.repo.head_sha(), SuiteResult(), False), 1)

        with pytest.raises(ValueError, match="already scored"):
            await scorer.verify_attempt(1)

    async def test_restore_is_only_for_a_scorer_that_has_scored_nothing(self, workspace):
        scorer, _ = await self.scorer(workspace, [SuiteResult(passed=("t::a",)), SuiteResult(passed=("t::a",))])
        write(workspace.path / "src" / "app.py", "VALUE = 1\n")
        await scorer.verify_attempt(1)

        with pytest.raises(RuntimeError, match="scored nothing yet"):
            scorer.restore(None, 0)

    async def test_the_record_and_the_count_must_agree(self, workspace):
        scorer, _ = await self.scorer(workspace, [SuiteResult(passed=("t::a",))])
        with pytest.raises(ValueError, match="scored attempt"):
            scorer.restore(None, 1)
