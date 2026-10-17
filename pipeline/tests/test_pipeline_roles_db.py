"""The Phase 2 roles inside `run_task`: the real roles graph, toolbox, gateway client, scorer and git.

What the unit tests in `agents/` cannot show is the join: that `LLMAgent(roles=True)` through the
real pipeline completes a task the scorer agrees with (`_check_against_scorer` would fail it
otherwise), that every role's call is recorded on its own stage, that the reviewer reads the real
diff of the scored commit, that the debugger's `rewind` moves the real clone, and that the pull
request names the editor's model when the planner runs on another. Faked, as in
`test_pipeline_integration_db.py`: the provider (scripted `litellm.acompletion`), the sandbox and
GitHub.
"""

from decimal import Decimal

import pytest
from pipeline_llm_support import (
    BROKEN,
    COLLECTED,
    DOCSTRING,
    FIXED,
    GREEN_AFTER,
    MODEL_NAME,
    OTHER,
    RED_BASELINE,
    VISIBLE,
    ScriptedProvider,
    call,
    edit_docstring,
    llm_rows,
    make_client,
    reply,
)
from pipeline_support import FakeGithubClient, local_workspace_factory, reload, seed_task
from repolace_gateway.config import parse_config
from repolace_gateway.recorder import CallRecord, Recorder
from repolace_gateway.redaction import Redactor
from repolace_pipeline.agent_runner import LLMAgent
from repolace_pipeline.finalize import first_model
from repolace_pipeline.run import run_task
from repolace_shared.db.models import TaskOutcome, TaskStatus
from verify.protocol import SuiteResult
from verify.testing import FakeBackend

pytestmark = [pytest.mark.anyio, pytest.mark.db]

CHEAP_MODEL = "anthropic/test-cheap"
PLAN = "Say in parse_config's docstring that an empty file gives an empty dict."
#: OTHER passed at the baseline and fails here: a visible regression, so the attempt is red.
RED_ATTEMPT = SuiteResult(passed=(), failed=(OTHER, VISIBLE), collected_files=COLLECTED)


def roles_config(plan_model: str = "main"):
    """The integration suite's config, plus a second priced model the `plan` stage can be pointed at."""
    price = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}
    raw = {
        "gateway": {"max_attempts": 2, "base_delay_seconds": 0.01, "max_delay_seconds": 0.02, "request_timeout_seconds": 30.0},
        "stages": {
            "agent": {"model": "main", "cache": True, "max_tokens": 4096},
            "cheap": {"model": "main", "max_tokens": 256},
        },
        "models": {
            "main": {"provider": "anthropic", "litellm_model": MODEL_NAME, "supports_prompt_caching": True, "price": price},
            "cheap": {"provider": "anthropic", "litellm_model": CHEAP_MODEL, "supports_prompt_caching": True, "price": price},
        },
    }
    if plan_model != "main":
        raw["stages"]["plan"] = {"model": plan_model, "cache": True, "max_tokens": 4096}
    return parse_config(raw)


async def run_roles(factory, origin_url, task, provider, backend, *, config=None):
    github = FakeGithubClient()
    result = await run_task(
        task.id,
        factory,
        github,
        backend,
        agent=LLMAgent(roles=True),
        llm=make_client(factory, provider, config=config or roles_config()),
        workspace_factory=local_workspace_factory(origin_url),
        embedder_warmup=lambda: None,
    )
    return result, github


def user_text(request) -> str:
    """The text of a request's first user message (the gateway may have split it into cache blocks)."""
    content = next(m for m in request["messages"] if m["role"] == "user")["content"]
    return content if isinstance(content, str) else "".join(block.get("text", "") for block in content)


def clean_script():
    return [
        reply(call("submit_plan", plan=PLAN)),
        reply(call("read_file", path="src/app.py")),
        reply(edit_docstring(DOCSTRING, FIXED)),
        reply(call("submit", summary="Document that an empty file gives an empty dict.")),
        reply(call("submit_review", verdict="approve")),
    ]


@pytest.mark.usefixtures("embedder")
class TestARolesRunThroughThePipeline:
    async def test_plan_edit_review_completes_and_opens_the_pr(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        provider = ScriptedProvider(clean_script())

        result, github = await run_roles(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, GREEN_AFTER])
        )
        row, runs = await reload(db_session_factory, task.id)

        assert result.status is row.status is TaskStatus.PR_OPENED and row.error_message is None
        assert row.outcome is TaskOutcome.PASSED and row.agent_stop_reason == "submitted"
        assert [r.attempt for r in runs] == [0, 1] and provider.unused == 0
        calls = await llm_rows(db_session_factory, task.id)
        assert [c.stage for c in calls] == ["plan", "agent", "agent", "agent", "review"]
        assert row.cost_usd == sum((c.cost_usd for c in calls), Decimal(0))
        assert len(github.pull_requests) == 1

    async def test_the_reviewer_reads_the_scored_diff_and_the_editor_the_plan(self, db_session, db_session_factory, origin_url):
        task = await seed_task(db_session)
        provider = ScriptedProvider(clean_script())

        await run_roles(db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, GREEN_AFTER]))
        row, _ = await reload(db_session_factory, task.id)

        assert PLAN in provider.request_text(1), "the editor's first request carries the plan"
        opening = user_text(provider.calls[4])
        assert "diff --git a/src/app.py b/src/app.py" in opening and f"+    {FIXED}" in opening
        tools = [schema["function"]["name"] for schema in provider.calls[4]["tools"]]
        assert tools == ["search_code", "read_file", "grep", "list_dir", "submit_review"], "the reviewer is read-only"
        assert row.patch_diff and FIXED in row.patch_diff

    async def test_a_red_attempt_goes_to_the_debugger_whose_rewind_moves_the_real_clone(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        rewind = call("rewind", attempt=1)
        provider = ScriptedProvider([
            reply(call("submit_plan", plan=PLAN)),
            reply(edit_docstring(DOCSTRING, BROKEN)),
            reply(call("submit", summary="first try")),
            reply(rewind),
            reply(edit_docstring(BROKEN, FIXED)),
            reply(call("submit", summary="Document that an empty file gives an empty dict.")),
            reply(call("submit_review", verdict="approve")),
        ])

        result, _ = await run_roles(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, RED_ATTEMPT, GREEN_AFTER])
        )
        row, runs = await reload(db_session_factory, task.id)

        assert result.status is TaskStatus.PR_OPENED and row.error_message is None, "the scorer agreed with the agent"
        assert [r.attempt for r in runs] == [0, 1, 2] and row.retry_count == 1
        assert "now exactly as it was at scored attempt 1" in provider.result_of(rewind)
        assert row.outcome is TaskOutcome.PASSED and FIXED in row.patch_diff and BROKEN not in row.patch_diff
        calls = await llm_rows(db_session_factory, task.id)
        assert [c.stage for c in calls] == ["plan", "agent", "agent", "agent", "agent", "agent", "review"]
        assert "rewind" in provider.request_text(3) and "rewind" not in provider.request_text(1)

    async def test_the_pr_names_the_editors_model_when_the_planner_runs_on_another(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        provider = ScriptedProvider(clean_script())

        _, github = await run_roles(
            db_session_factory, origin_url, task, provider, FakeBackend(results=[RED_BASELINE, GREEN_AFTER]),
            config=roles_config(plan_model="cheap"),
        )

        calls = await llm_rows(db_session_factory, task.id)
        assert (calls[0].stage, calls[0].model) == ("plan", CHEAP_MODEL), "the planner ran first, on the other model"
        (pull,) = github.pull_requests
        assert f"| model | `{MODEL_NAME}` |" in pull["body"] and CHEAP_MODEL not in pull["body"]


async def record(factory, task_id, *, stage: str, model: str) -> None:
    await Recorder(factory, Redactor()).record(
        CallRecord(task_id=task_id, stage=stage, model=model, provider="anthropic", cost_usd=Decimal("0.1"))
    )


class TestFirstModelIsTheEditors:
    async def test_a_planner_call_before_the_editor_does_not_name_the_model(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        await record(db_session_factory, task.id, stage="plan", model="planner-model")
        await record(db_session_factory, task.id, stage="agent", model="editor-model")
        await record(db_session_factory, task.id, stage="review", model="reviewer-model")

        assert await first_model(db_session_factory, task.id) == "editor-model"

    async def test_without_an_agent_stage_call_it_is_the_first_call_of_any_stage(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        await record(db_session_factory, task.id, stage="plan", model="planner-model")
        await record(db_session_factory, task.id, stage="review", model="reviewer-model")

        assert await first_model(db_session_factory, task.id) == "planner-model"

    async def test_with_only_agent_calls_it_is_the_first_one_as_before(self, db_session, db_session_factory):
        task = await seed_task(db_session)
        await record(db_session_factory, task.id, stage="agent", model="first")
        await record(db_session_factory, task.id, stage="agent", model="second")

        assert await first_model(db_session_factory, task.id) == "first"
