"""A task stopped by unavailability is paused with a checkpoint and continued by the same pipeline.

The real graph, the real gateway client (over a scripted provider), real git and the real database: the
first run is stopped by the provider refusing every call, the second is `run_task` again on the same row.
What it proves is in the database and on the remote, not in the script: the edit made before the pause is
in the final diff though the second run never made it; the baseline is not recorded twice; cost and calls
add up across both runs; and every way a resume must be refused ends the row FAILED, not paused or run.
"""

import asyncio
from decimal import Decimal

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
from repolace_gateway.budget import TaskBudget
from repolace_gateway.errors import KIND_OTHER, LLMCallError
from repolace_pipeline.checkpoints import load_checkpoint
from repolace_pipeline.errors import TaskNotClaimable
from repolace_pipeline.run import _claim, run_task
from repolace_shared.db.models import TaskCheckpoint, TaskOutcome, TaskStatus
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
    async def test_a_target_branch_that_moved_while_paused_fails_the_task_instead_of_measuring_another_tree(
        self, db_session, db_session_factory, origin_url, tmp_path
    ):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        push_upstream(origin_url, tmp_path / "pusher", {"README.md": "# moved\n"})

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is row.status is TaskStatus.FAILED
        assert row.error_message.startswith("resume:") and "moved" in row.error_message
        assert row.stop_class == "design"

    async def test_different_code_is_refused(self, db_session, db_session_factory, origin_url):
        task, _ = await pause_one(db_session, db_session_factory, origin_url, code_version="a" * 40)

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE]),
            code_version="b" * 40,
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.FAILED
        assert "different code or settings" in row.error_message and "code was" in row.error_message

    async def test_a_checkpoint_older_than_the_scored_attempts_is_refused(
        self, db_session, db_session_factory, origin_url
    ):
        task, _ = await pause_one(db_session, db_session_factory, origin_url)
        # An attempt was scored and recorded after the last snapshot: the window the design names.
        from repolace_pipeline.testruns import record_test_run

        await record_test_run(db_session_factory, task.id, 1, "c" * 40, GREEN_AFTER)

        result, _ = await run_real(
            db_session_factory, origin_url, task, ScriptedProvider([]), FakeBackend(results=[RED_BASELINE])
        )

        row, _ = await reload(db_session_factory, task.id)
        assert result.status is TaskStatus.FAILED and "older than the scored attempts" in row.error_message

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

