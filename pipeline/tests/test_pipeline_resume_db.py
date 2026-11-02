"""A task stopped by unavailability is paused with a checkpoint and continued by the same pipeline.

The real graph, the real gateway client (over a scripted provider), real git and the real database: the
first run is stopped by the provider refusing every call, the second is `run_task` again on the same row.
What it proves is in the database and on the remote, not in the script: the edit made before the pause is
in the final diff though the second run never made it; the baseline is not recorded twice; cost and calls
add up across both runs; and every way a resume must be refused ends the row FAILED, not paused or run.
"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from litellm import exceptions as lx
from pipeline_llm_support import (
    COLLECTED,
    DOCSTRING,
    FIXED,
    GREEN_AFTER,
    OTHER,
    RED_BASELINE,
    VISIBLE,
    ScriptedProvider,
    call,
    edit_docstring,
    llm_rows,
    reply,
    run_real,
)
from pipeline_support import FakeGithubClient, ScriptedAgent, local_workspace_factory, push_upstream, reload, seed_task
from repolace_agents.checkpoint import CheckpointFenced
from repolace_gateway.budget import TaskBudget
from repolace_gateway.errors import KIND_OTHER, LLMCallError
from repolace_pipeline import run as run_module
from repolace_pipeline.checkpoints import load_checkpoint, pack, save_checkpoint
from repolace_pipeline.errors import TaskNotClaimable
from repolace_pipeline.finalize import write_failure_minimal, write_paused
from repolace_pipeline.run import _claim, run_task
from repolace_pipeline.testruns import record_test_run
from repolace_shared.db.models import Task, TaskCheckpoint, TaskOutcome, TaskStatus, TaskTestRun
from repolace_shared.tasks_recovery import RESUME_REFUSED_PREFIX, reopen_refused
from sqlalchemy import delete, func, update
from verify.errors import SandboxUnavailable
from verify.protocol import SuiteResult
from verify.testing import FakeBackend

pytestmark = [pytest.mark.anyio, pytest.mark.db, pytest.mark.usefixtures("embedder")]


def refusal() -> lx.RateLimitError:
    return lx.RateLimitError(message="quota exceeded", llm_provider="anthropic", model="test-main")


def stopped_after_one_edit():
    """read, edit, then the provider refuses (the test config retries twice, so two refusals)."""
    return [
        reply(call("read_file", path="src/app.py")),
        reply(edit_docstring(DOCSTRING, FIXED)),
        refusal(),
        refusal(),
    ]


async def pause_one(db_session, db_session_factory, origin_url, *, backend=None, **kwargs):
    task = await seed_task(db_session)
    provider = ScriptedProvider(stopped_after_one_edit())
    backend = backend or FakeBackend(results=[RED_BASELINE])
    result, _ = await run_real(db_session_factory, origin_url, task, provider, backend, **kwargs)
    return task, result


class TestThePause:
    async def test_a_provider_that_refuses_past_the_retries_pauses_the_task_with_a_checkpoint(
        self, db_session, db_session_factory, origin_url
    ):
        task, result = await pause_one(db_session, db_session_factory, origin_url)

        row, _runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PAUSED
        assert "provider quota exhausted" in row.paused_reason and result.paused_reason == row.paused_reason
        assert (row.stop_class, row.error_message, row.completed_at, row.outcome) == ("unavailable", None, None, None)
        assert row.resume_count == 0
        stored = await load_checkpoint(db_session_factory, task.id)
        assert stored is not None and stored.seq >= 3, "a checkpoint was written before each step"
        assert stored.delta_patch, "the edit made before the stop is in the checkpoint"
        assert FIXED.encode() in stored.delta_patch

    async def test_the_checkpoint_stores_no_suite_result_only_the_scored_attempts_number_sha_and_flag(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        script = [
            reply(call("read_file", path="src/app.py")),
            reply(edit_docstring(DOCSTRING, FIXED)),
            reply(call("submit", summary="First try.")),
            refusal(),
            refusal(),
        ]
        # The scored attempt breaks a test that passed at the baseline: a visible regression, so the agent retries.
        regressed = SuiteResult(passed=(), failed=(OTHER, VISIBLE), collected_files=COLLECTED)
        backend = FakeBackend(results=[RED_BASELINE, regressed])

        await run_real(db_session_factory, origin_url, task, ScriptedProvider(script), backend)

        async with db_session_factory() as session:
            row = await session.get(TaskCheckpoint, task.id)
        state = row.agent_state
        assert set(state["last_scored"]) == {"attempt", "commit_sha", "infrastructure_error"}
        assert state["attempt"] == 1
        assert not {"result", "baseline", "passed", "failed", "stdout_tail"} & set(state), "no suite result field"

    async def test_a_design_stop_is_not_paused_it_completes_as_the_agents(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        script = [
            reply(call("read_file", path="src/app.py")),
            lx.BadRequestError(message="messages must not be empty", llm_provider="anthropic", model="test-main"),
        ]

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider(script), FakeBackend(results=[RED_BASELINE])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is not TaskStatus.PAUSED
        assert row.agent_stop_reason == "llm_error" and row.stop_class is None and row.paused_reason is None

    async def test_an_unavailability_stop_with_nothing_saved_fails_as_before_with_its_class(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)

        async def provider_down(deps):
            raise LLMCallError("down", stage="agent", model="m", retries=3, kind=KIND_OTHER, transient=True)

        result = await run_task(
            task.id, db_session_factory, _github(), FakeBackend(results=[RED_BASELINE]),
            agent=ScriptedAgent(provider_down), open_pr=False,
            workspace_factory=_workspaces(origin_url), embedder_warmup=lambda: None,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.FAILED
        assert row.stop_class == "unavailable" and row.error_message


class TestTheResume:
    async def test_the_edit_made_before_the_pause_is_in_the_final_diff_and_the_baseline_is_not_recorded_twice(
        self, db_session, db_session_factory, origin_url
    ):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        first_calls = len(await llm_rows(db_session_factory, task.id))
        provider = ScriptedProvider([reply(call("submit", summary="Document the empty file case."))])
        backend = FakeBackend(results=[RED_BASELINE, GREEN_AFTER])  # one to build the environment, one scored

        result, github = await run_real(db_session_factory, origin_url, task, provider, backend)

        row, runs = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED
        assert row.outcome is TaskOutcome.PASSED
        assert (row.resume_count, row.paused_reason, row.stop_class) == (1, None, None)
        assert FIXED in row.patch_diff, "the second run never edited anything; the first run's edit came back"
        assert [r.attempt for r in runs] == [0, 1], "the baseline row from the first run was kept, not duplicated"
        assert len(github.pull_requests) == 1
        assert len(await llm_rows(db_session_factory, task.id)) == first_calls + 1

    async def test_the_model_is_shown_the_transcript_it_had_and_the_same_delimiters(
        self, db_session, db_session_factory, origin_url
    ):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        first_request = (await llm_rows(db_session_factory, task.id))[0].request["messages"]
        provider = ScriptedProvider([reply(call("submit", summary="Done."))])

        await run_real(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, GREEN_AFTER])
        )

        sent = provider.calls[0]["messages"]
        roles = [m["role"] for m in sent]
        assert roles[:2] == ["system", "user"] and roles.count("system") == 1, "localize did not run again"
        assert roles[2:6] == ["assistant", "tool", "assistant", "tool"], "the two completed steps are in the history"
        assert _text(sent[0]["content"]) == _text(first_request[0]["content"]), "the same nonce, so the same delimiters"

    async def test_the_budget_is_rebuilt_from_what_the_first_run_spent(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        budget = TaskBudget()
        provider = ScriptedProvider([reply(call("submit", summary="Done."))])

        await run_real(
            db_session_factory, origin_url, task, provider,
            FakeBackend(results=[RED_BASELINE, GREEN_AFTER]), budget=budget,
        )

        rows = await llm_rows(db_session_factory, task.id)
        priced = [r.cost_usd for r in rows if r.cost_usd is not None]
        assert budget.calls == len(rows), "every call of both runs, failed ones included"
        assert budget.spent_usd == sum(priced, Decimal(0)) and budget.spent_usd > 0

    async def test_the_cost_on_the_row_is_the_sum_over_both_runs(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        provider = ScriptedProvider([reply(call("submit", summary="Done."))])

        await run_real(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, GREEN_AFTER])
        )

        row, _ = await reload(db_session_factory, task.id)
        rows = await llm_rows(db_session_factory, task.id)
        assert row.cost_usd == sum((r.cost_usd for r in rows if r.cost_usd is not None), Decimal(0))

    async def test_a_second_pause_counts_a_second_resume(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        provider = ScriptedProvider([refusal(), refusal()])

        again, _ = await run_real(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert again.status is row.status is TaskStatus.PAUSED and row.resume_count == 1


class TestARefusedResume:
    """A refusal fails the row cleanly, keeps its checkpoint and does not spend a resume: the way back is open."""

    async def refuse_by_moving_the_base(self, db_session, db_session_factory, origin_url, tmp_path):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        push_upstream(origin_url, tmp_path / "pusher", {"README.md": "# moved\n"})
        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE])
        )
        return task, result

    async def test_a_target_branch_that_moved_while_paused_fails_the_task_instead_of_measuring_another_tree(
        self, db_session, db_session_factory, origin_url, tmp_path
    ):
        task, result = await self.refuse_by_moving_the_base(db_session, db_session_factory, origin_url, tmp_path)

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.FAILED
        assert row.error_message.startswith(RESUME_REFUSED_PREFIX) and "moved" in row.error_message
        assert row.stop_class == "design"

    async def test_the_checkpoint_survives_a_refusal_and_the_resume_it_cost_is_handed_back(
        self, db_session, db_session_factory, origin_url, tmp_path
    ):
        task, _ = await self.refuse_by_moving_the_base(db_session, db_session_factory, origin_url, tmp_path)

        row, _ = await reload(db_session_factory, task.id)
        stored = await load_checkpoint(db_session_factory, task.id)
        assert stored is not None and FIXED.encode() in stored.delta_patch, "the saved work is still there"
        assert row.resume_count == 0, "the claim counted a resume; the refusal handed it back"

    async def test_reverting_the_cause_and_reopening_lets_the_same_checkpoint_resume(
        self, db_session, db_session_factory, origin_url
    ):
        task, _ = await pause_one(db_session, db_session_factory, origin_url, code_version="a" * 40)
        refused, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE]),
            code_version="b" * 40,
        )
        assert refused.status is TaskStatus.FAILED
        assert await reopen_refused(db_session_factory, task.id) is True

        provider = ScriptedProvider([reply(call("submit", summary="Done."))])
        result, _ = await run_real(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, GREEN_AFTER]),
            code_version="a" * 40,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PR_OPENED and FIXED in row.patch_diff
        assert row.resume_count == 1, "only the resume that ran counts"

    async def test_different_code_is_refused(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url, code_version="a" * 40)

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE]),
            code_version="b" * 40,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.FAILED
        assert "different code or settings" in row.error_message and "code was" in row.error_message
        assert await load_checkpoint(db_session_factory, task.id) is not None

    async def test_a_checkpoint_older_than_the_scored_attempts_is_refused_and_kept(
        self, db_session, db_session_factory, origin_url
    ):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        # An attempt was scored and recorded after the last snapshot: the window the design names.
        await record_test_run(db_session_factory, task.id, 1, "c" * 40, GREEN_AFTER)

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.FAILED and "older than the scored attempts" in row.error_message
        assert row.error_message.startswith(RESUME_REFUSED_PREFIX) and row.resume_count == 0
        assert await load_checkpoint(db_session_factory, task.id) is not None

    async def test_a_missing_baseline_row_is_refused_and_kept(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        async with db_session_factory() as session:
            await session.execute(delete(TaskTestRun).where(TaskTestRun.task_id == task.id))
            await session.commit()

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.FAILED and row.error_message.startswith(RESUME_REFUSED_PREFIX)
        assert "baseline" in row.error_message and await load_checkpoint(db_session_factory, task.id) is not None

    async def test_a_patch_that_does_not_apply_is_refused_and_kept(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        async with db_session_factory() as session:
            await session.execute(
                update(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id).values(delta_patch=pack(b"not a patch\n"))
            )
            await session.commit()

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.FAILED and "could not be restored" in row.error_message
        assert await load_checkpoint(db_session_factory, task.id) is not None

    async def test_a_paused_row_without_a_checkpoint_fails_rather_than_starting_over(
        self, db_session, db_session_factory, origin_url
    ):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        async with db_session_factory() as session:
            await session.delete(await session.get(TaskCheckpoint, task.id))
            await session.commit()

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.FAILED and "no checkpoint" in row.error_message


class TestOnlyAnUnfinishedAgentIsPaused:
    """Fix 1: a pause must be one a resume can honour."""

    async def test_a_checkpoint_that_misses_a_scored_attempt_is_not_a_pause(
        self, db_session, db_session_factory, origin_url, monkeypatch
    ):
        """Attempt 1 was scored and recorded, but the snapshot after it never landed: a resume would be refused."""
        real = run_module.CheckpointRecorder

        class LosesTheSnapshotsAfterAScoredAttempt(real):
            async def __call__(self, snapshot):
                if snapshot.attempt >= 1:
                    raise RuntimeError("the database blipped while saving")
                await super().__call__(snapshot)

        monkeypatch.setattr(run_module, "CheckpointRecorder", LosesTheSnapshotsAfterAScoredAttempt)
        task = await seed_task(db_session)
        regressed = SuiteResult(passed=(), failed=(OTHER, VISIBLE), collected_files=COLLECTED)
        script = [
            reply(edit_docstring(DOCSTRING, FIXED)),
            reply(call("submit", summary="First try.")),
            refusal(),
            refusal(),
        ]

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider(script), FakeBackend(results=[RED_BASELINE, regressed])
        )

        row, runs = await reload(db_session_factory, task.id)
        assert [r.attempt for r in runs] == [0, 1], "the attempt really was scored and recorded"
        assert result.status is row.status is TaskStatus.FAILED, "so the pause is refused up front, not failed at resume"
        assert row.stop_class == "unavailable"

    async def test_a_failure_after_the_agent_finished_is_not_a_pause(self, db_session, db_session_factory, origin_url):
        """The PR step hits a GitHub outage after the agent is done: resuming would run the agent a second time."""
        task = await seed_task(db_session)
        github = FakeGithubClient(pull_request_error=httpx.ConnectError("github is down"))
        provider = ScriptedProvider(
            [reply(edit_docstring(DOCSTRING, FIXED)), reply(call("submit", summary="Done."))]
        )

        result, _ = await run_real(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, GREEN_AFTER]),
            github=github,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert await load_checkpoint(db_session_factory, task.id) is not None, "a checkpoint exists from the agent run"
        assert result.status is row.status is TaskStatus.FAILED
        assert row.stop_class == "unavailable" and row.paused_reason is None

    async def test_a_pause_keeps_the_pull_request_the_row_already_has(self, db_session, db_session_factory):
        task = await seed_task(db_session, status=TaskStatus.RUNNING, claimed_by="host:1")
        task_id = task.id
        async with db_session_factory() as session:
            session.add(TaskCheckpoint(task_id=task_id, agent_state={}, base_sha="b" * 40, provenance={}))
            await session.commit()

        async with db_session_factory() as session:
            assert await write_paused(
                session, task_id, paused_reason="x", pr_number=7, pr_url="https://github.test/pull/7"
            )
            await session.commit()

        row, _ = await reload(db_session_factory, task_id)
        assert (row.pr_number, row.pr_url) == (7, "https://github.test/pull/7")


class FlakyPreflight(FakeGithubClient):
    """The installation lookup fails once with a GitHub 5xx, then answers."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def get_installation(self, installation_id: int):
        if self.failures:
            self.failures -= 1
            request = httpx.Request("GET", "https://api.github.com/app/installations/1")
            raise httpx.HTTPStatusError("bad gateway", request=request, response=httpx.Response(502, request=request))
        return await super().get_installation(installation_id)


class TestAnOutageBeforeTheAgentOnAResume:
    """Fix 3: the stages a resume re-runs can fail transiently too; the saved work must wait, not be lost."""

    async def paused_row(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        row, _ = await reload(db_session_factory, task.id)
        assert row.resume_count == 0
        return task

    async def test_a_github_5xx_at_preflight_re_pauses_and_costs_no_resume(
        self, db_session, db_session_factory, origin_url
    ):
        task = await self.paused_row(db_session, db_session_factory, origin_url)

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[]), github=FlakyPreflight()
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PAUSED
        assert "502" in row.paused_reason and row.stop_class == "unavailable"
        assert row.resume_count == 0, "a resume that never reached the agent is not counted"
        assert await load_checkpoint(db_session_factory, task.id) is not None

    async def test_a_sandbox_that_never_answered_the_extra_baseline_re_pauses(
        self, db_session, db_session_factory, origin_url
    ):
        task = await self.paused_row(db_session, db_session_factory, origin_url)

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]),
            FakeBackend(results=[SandboxUnavailable("the docker daemon is down")]),
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.PAUSED
        assert "container runtime unavailable" in row.paused_reason and row.resume_count == 0

    async def test_the_same_task_then_resumes_when_the_outage_is_over(self, db_session, db_session_factory, origin_url):
        task = await self.paused_row(db_session, db_session_factory, origin_url)
        await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[]), github=FlakyPreflight()
        )

        provider = ScriptedProvider([reply(call("submit", summary="Done."))])
        result, _ = await run_real(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, GREEN_AFTER])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.PR_OPENED and FIXED in row.patch_diff and row.resume_count == 1

    async def test_the_pause_does_not_postpone_its_own_expiry(self, db_session, db_session_factory, origin_url):
        task = await self.paused_row(db_session, db_session_factory, origin_url)
        long_ago = datetime.now(UTC) - timedelta(hours=30)
        async with db_session_factory() as session:
            await session.execute(update(Task).where(Task.id == task.id).values(updated_at=long_ago))
            await session.commit()

        await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[]), github=FlakyPreflight()
        )

        row, _ = await reload(db_session_factory, task.id)
        assert row.updated_at == long_ago, "no progress, so the age the expiry reads is untouched"
        assert row.paused_at > long_ago, "but the claim gate restarted"


class TestOnlyTheOwnerWrites:
    """Fix 4: a process that lost its task cannot save, finish, fail or pause it."""

    async def running(self, session, claimed_by="host:1"):
        return await seed_task(session, status=TaskStatus.RUNNING, claimed_by=claimed_by)

    async def test_a_save_by_another_claimant_is_fenced(self, db_session, db_session_factory):
        task = await self.running(db_session)
        task_id = task.id

        with pytest.raises(CheckpointFenced):
            await save_checkpoint(
                db_session_factory, task_id, claimed_by="host:2", agent_state={}, base_sha="b" * 40,
                scored_patch=b"", delta_patch=b"", agent_seconds_used=1.0, provenance={},
            )

        assert await load_checkpoint(db_session_factory, task_id) is None

    @pytest.mark.parametrize("status", [TaskStatus.PAUSED, TaskStatus.COMPLETED, TaskStatus.FAILED])
    async def test_a_save_for_a_row_that_is_no_longer_running_is_fenced(self, db_session, db_session_factory, status):
        task = await seed_task(db_session, status=status, claimed_by="host:1")

        with pytest.raises(CheckpointFenced):
            await save_checkpoint(
                db_session_factory, task.id, claimed_by="host:1", agent_state={}, base_sha="b" * 40,
                scored_patch=b"", delta_patch=b"", agent_seconds_used=1.0, provenance={},
            )

    async def test_the_owner_saves_and_a_none_patch_is_left_as_it_was(self, db_session, db_session_factory):
        task = await self.running(db_session)
        common = {"claimed_by": "host:1", "agent_state": {}, "base_sha": "b" * 40, "agent_seconds_used": 1.0, "provenance": {}}

        await save_checkpoint(db_session_factory, task.id, scored_patch=b"scored", delta_patch=b"delta", **common)
        await save_checkpoint(db_session_factory, task.id, scored_patch=None, delta_patch=None, **common)

        stored = await load_checkpoint(db_session_factory, task.id)
        assert (stored.scored_patch, stored.delta_patch, stored.seq) == (b"scored", b"delta", 2)

    async def test_a_run_that_loses_its_row_stops_at_its_next_save_and_touches_nothing(
        self, db_session, db_session_factory, origin_url, monkeypatch
    ):
        """The starved original: the reaper paused it, a resumer claimed the row, then the original woke up."""
        real = run_module.CheckpointRecorder
        task = await seed_task(db_session)

        class WakesUpAfterLosingTheRow(real):
            async def __call__(self, snapshot):
                if snapshot.steps >= 1:
                    async with db_session_factory() as session:
                        await session.execute(
                            update(Task).where(Task.id == task.id).values(claimed_by="resumer:9", heartbeat_at=func.now())
                        )
                        await session.commit()
                await super().__call__(snapshot)

        monkeypatch.setattr(run_module, "CheckpointRecorder", WakesUpAfterLosingTheRow)
        provider = ScriptedProvider(
            [reply(call("read_file", path="src/app.py")), reply(call("read_file", path="src/app.py"))]
        )

        result, _ = await run_real(db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE]))

        row, _ = await reload(db_session_factory, task.id)
        assert len(provider.calls) == 1, "it stopped at the first save after the loss, not later"
        assert result.status is TaskStatus.FAILED
        assert (row.status, row.claimed_by, row.error_message) == (TaskStatus.RUNNING, "resumer:9", None), (
            "the resumer's row is exactly as the resumer left it"
        )

    async def test_the_owners_terminal_write_is_fenced_to_its_claim(self, db_session, db_session_factory):
        task = await self.running(db_session)
        async with db_session_factory() as session:
            wrote = await write_failure_minimal(
                session, task.id, error_message="late", pr_number=None, pr_url=None, claimed_by="host:2"
            )
            await session.commit()

        row, _ = await reload(db_session_factory, task.id)
        assert wrote is False and row.status is TaskStatus.RUNNING

    async def test_a_heartbeat_from_another_claimant_does_not_keep_the_row_alive(self, db_session, db_session_factory):
        task = await self.running(db_session, claimed_by="resumer:9")
        old = datetime.now(UTC) - timedelta(hours=1)
        async with db_session_factory() as session:
            await session.execute(update(Task).where(Task.id == task.id).values(heartbeat_at=old))
            await session.commit()

        beating = asyncio.create_task(run_module._heartbeat(db_session_factory, task.id, 0.01, "stale:1"))
        await asyncio.sleep(0.2)
        beating.cancel()
        with suppress(asyncio.CancelledError):
            await beating

        row, _ = await reload(db_session_factory, task.id)
        assert row.heartbeat_at == old


class TestTheClaim:
    async def test_two_resumers_race_and_exactly_one_wins(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)

        async def try_claim() -> str:
            async with db_session_factory() as session:
                try:
                    await _claim(session, task.id)
                except TaskNotClaimable:
                    return "lost"
                return "won"

        outcomes = await asyncio.gather(try_claim(), try_claim())

        assert sorted(outcomes) == ["lost", "won"]
        row, _ = await reload(db_session_factory, task.id)
        assert row.status is TaskStatus.RUNNING and row.resume_count == 1 and row.paused_reason is None

    async def test_a_duplicate_message_for_a_row_that_just_paused_is_refused_and_burns_nothing(
        self, db_session, db_session_factory, origin_url
    ):
        """Fix 4: the sweeper waits its minimum age; a duplicate broker message must wait it too."""
        task, _ = await pause_one(db_session, db_session_factory, origin_url)

        async with db_session_factory() as session:
            with pytest.raises(TaskNotClaimable):
                await _claim(session, task.id, None, 300.0)

        row, _ = await reload(db_session_factory, task.id)
        assert row.status is TaskStatus.PAUSED and row.resume_count == 0

    async def test_the_same_row_is_claimable_once_it_has_waited_long_enough(
        self, db_session, db_session_factory, origin_url
    ):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        async with db_session_factory() as session:
            await session.execute(
                update(Task).where(Task.id == task.id).values(paused_at=datetime.now(UTC) - timedelta(minutes=10))
            )
            await session.commit()

        async with db_session_factory() as session:
            await _claim(session, task.id, None, 300.0)

        row, _ = await reload(db_session_factory, task.id)
        assert row.status is TaskStatus.RUNNING and row.resume_count == 1 and row.paused_at is None

    async def test_a_finished_task_is_still_not_claimable(self, db_session, db_session_factory):
        task = await seed_task(db_session, status=TaskStatus.COMPLETED)

        async with db_session_factory() as session:
            with pytest.raises(TaskNotClaimable):
                await _claim(session, task.id)


def _text(content) -> str:
    """A message's text whether it is a string or provider content blocks (cache markers make the latter)."""
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content)


def _github():
    return FakeGithubClient()


def _workspaces(origin_url):
    return local_workspace_factory(origin_url)

