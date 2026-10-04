"""Spies on the joins: which arguments the pipeline passes, and which entry points it calls.

A type check cannot tell a wired function from one that is passed and ignored, and the review of the
pipeline found exactly that kind of wiring surviving mutation. So each test here replaces the callee with
a recorder and pins the arguments: change the call and the test fails, not just a different number.
"""

import unicodedata

import pytest
from langgraph.errors import GraphRecursionError
from retrieval.query import build_query
from verify.testing import FakeBackend

import repolace_pipeline.agent_context as agent_context_module
import repolace_pipeline.agent_runner as agent_runner_module
import repolace_pipeline.run as run_module
from repolace_agents.contracts import AgentResult, StopReason
from repolace_agents.tools import build_toolbox
from repolace_agents.tools.base import ToolContext
from repolace_gateway.budget import BudgetExceeded, BudgetLimit
from repolace_gateway.errors import LLMCallError, MissingProviderKey, NoTaskScope, UnpricedModelError
from repolace_pipeline.agent_runner import LLMAgent

from pipeline_llm_support import RED_BASELINE
from pipeline_support import (
    FakeGithubClient,
    NeverCalledLLM,
    ScriptedAgent,
    agent_result,
    local_workspace_factory,
    make_deps,
    reload,
    seed_task,
)

TRACEBACK_BODY = """Calling parse_config on an empty file blows up.

Traceback (most recent call last):
  File "src/app.py", line 3, in parse_config
    return dict(line.strip().split("=", 1) for line in handle if "=" in line)
ValueError: dictionary update sequence element #0 has length 1; 2 is required

I think render_report is fine. See also ConfigParser.read_file and the LEGACY_FORMAT flag.
"""


class RecordingSearch:
    """Wraps the real `hybrid_search`, remembering how it was called."""

    def __init__(self, real):
        self.real = real
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return await self.real(*args, **kwargs)


@pytest.fixture
def spy_search(monkeypatch):
    spy = RecordingSearch(run_module.hybrid_search)
    monkeypatch.setattr(run_module, "hybrid_search", spy)
    return spy


async def run_no_op_agent(factory, origin_url, task, **kwargs):
    async def script(deps):
        return agent_result(StopReason.NO_CHANGE, None)

    return await run_module.run_task(
        task.id, factory, FakeGithubClient(), FakeBackend(results=[RED_BASELINE]),
        agent=ScriptedAgent(script), workspace_factory=local_workspace_factory(origin_url),
        embedder_warmup=lambda: None, **kwargs,
    )


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.usefixtures("embedder")
class TestRetrievalUsesTheQueryBuilder:
    async def test_the_two_arms_get_the_two_strings_build_query_makes_and_the_repos_strategy(
        self, db_session, db_session_factory, origin_url, spy_search
    ):
        title = "parse_config crashes on an empty config file"
        task = await seed_task(db_session, issue_title=title, issue_body=TRACEBACK_BODY)
        expected = build_query(title, TRACEBACK_BODY)
        assert expected.keyword and expected.keyword != expected.semantic, "the fixture must tell the arms apart"

        await run_no_op_agent(db_session_factory, origin_url, task, embedding_strategy="head_tail")

        (args, kwargs), *_ = spy_search.calls
        assert args[2] == expected.semantic, "the semantic arm embeds the title plus the head of the body"
        assert kwargs["keyword_query"] == expected.keyword, "the keyword arm gets identifiers, not prose"
        assert kwargs["query_strategy"] == "head_tail"
        assert kwargs["limit"] == run_module.RETRIEVE_LIMIT
        assert "ValueError" in expected.keyword.split() or "parse_config" in expected.keyword.split()

    async def test_the_issue_body_is_bounded_and_stripped_before_it_reaches_the_query(
        self, db_session, db_session_factory, origin_url, spy_search
    ):
        """The body is anyone's text. A 100 kB body full of control characters must not become a 100 kB query."""
        body = ("parse_config \x07\x1b[31m" + "x" * 200 + "\n") * 500
        task = await seed_task(db_session, issue_body=body)

        await run_no_op_agent(db_session_factory, origin_url, task)

        (args, kwargs), *_ = spy_search.calls
        semantic = args[2]
        assert len(semantic) <= 256 + 1 + 1500, "title cap plus the body head `build_query` allows"
        assert not any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in semantic)
        assert len(kwargs["keyword_query"].split()) <= 64

    async def test_a_missing_title_falls_back_to_the_issue_number_rather_than_an_empty_query(
        self, db_session, db_session_factory, origin_url, spy_search
    ):
        task = await seed_task(db_session, issue_title="   ", issue_body=None)

        result = await run_no_op_agent(db_session_factory, origin_url, task)

        (args, _), *_ = spy_search.calls
        assert args[2] == "issue 7", "hybrid_search refuses an empty query, and that must not fail the task"
        assert result.error_message is None

    async def test_retrieval_and_the_search_tool_use_the_strategy_the_index_was_built_with(
        self, db_session, db_session_factory, origin_url, spy_search, monkeypatch
    ):
        async def stored(session_factory, repo_id):
            return "windows"

        tool_spy = RecordingSearch(agent_context_module.hybrid_search)
        monkeypatch.setattr(agent_context_module, "hybrid_search", tool_spy)
        monkeypatch.setattr(run_module, "_stored_strategy", stored)
        task = await seed_task(db_session)

        async def script(deps):
            await deps.tools.dispatch(_search_call("parse_config"))
            return agent_result(StopReason.NO_CHANGE, None)

        await run_module.run_task(
            task.id, db_session_factory, FakeGithubClient(), FakeBackend(results=[RED_BASELINE]),
            agent=ScriptedAgent(script), llm=NeverCalledLLM(), workspace_factory=local_workspace_factory(origin_url),
            embedder_warmup=lambda: None,
        )

        assert spy_search.calls[0][1]["query_strategy"] == "windows"
        assert tool_spy.calls and tool_spy.calls[0][1]["query_strategy"] == "windows"


def _search_call(query: str):
    from pipeline_support import FakeToolCall

    return FakeToolCall(name="search_code", arguments={"query": query})


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.usefixtures("embedder")
class TestTheRealRunnerIsReachedThroughRunAgent:
    async def test_run_task_calls_run_agent_once_with_the_deps_it_built(
        self, db_session, db_session_factory, origin_url, monkeypatch
    ):
        seen = []

        async def spy(deps):
            seen.append(deps)
            return AgentResult(stop_reason=StopReason.NO_CHANGE, summary=None, attempts=0, steps=0, last_attempt=None)

        monkeypatch.setattr(agent_runner_module, "run_agent", spy)
        task = await seed_task(db_session)
        llm = NeverCalledLLM()

        result = await run_module.run_task(
            task.id, db_session_factory, FakeGithubClient(), FakeBackend(results=[RED_BASELINE]),
            agent=LLMAgent(), llm=llm, workspace_factory=local_workspace_factory(origin_url),
            embedder_warmup=lambda: None,
        )

        row, _ = await reload(db_session_factory, task.id)
        (deps,) = seen
        assert deps.llm is llm and deps.tools is not None, "the model client and the nine tools"
        assert deps.tools.names[-1] == "submit"
        assert result.stop_reason == "no_change" and row.agent_stop_reason == "no_change"

    async def test_there_is_no_path_to_the_graph_that_chooses_its_own_nonce(self):
        """`run_graph` takes the nonce as a parameter. The runner module must not import it at all."""
        assert not hasattr(agent_runner_module, "run_graph")
        assert agent_runner_module.run_agent.__module__ == "repolace_agents.run"


class _Recording:
    """An `LLMClientLike` that records the opening messages, then fails like a provider that is down."""

    def __init__(self):
        self.first_messages = []

    async def complete(self, stage, messages, tools=None, **kwargs):
        self.first_messages.append(list(messages))
        raise LLMCallError("provider down", stage=stage, model="m", retries=0)


async def _nothing(*args, **kwargs):
    return None


@pytest.mark.anyio
class TestEveryRunDrawsAFreshNonce:
    async def test_two_runs_of_the_same_deps_get_different_system_prompts(self, tmp_path):
        """The delimiters that fence untrusted text carry the nonce, so a fixed one is a predictable closing tag."""
        box = build_toolbox(
            ToolContext(
                checkout=tmp_path, checkpoint=_nothing, search=_nothing, run_subset=None, run_script=None
            )
        )
        prompts = []
        for _ in range(2):
            llm = _Recording()
            result = await LLMAgent()(make_deps(tmp_path, llm=llm, tools=box))
            assert result.stop_reason is StopReason.LLM_ERROR
            prompts.append(llm.first_messages[0][0]["content"])

        assert prompts[0] != prompts[1]


class TestWhichExceptionsAreTheAgentsFault:
    @pytest.mark.parametrize(
        ("exc", "stop"),
        [
            (LLMCallError("down", stage="agent", model="m", retries=1), StopReason.LLM_ERROR),
            (BudgetExceeded(BudgetLimit.USD, spent_usd=1, calls=1, elapsed_seconds=1.0), StopReason.BUDGET_USD),
            (BudgetExceeded(BudgetLimit.CALLS, spent_usd=1, calls=1, elapsed_seconds=1.0), StopReason.BUDGET_CALLS),
            (BudgetExceeded(BudgetLimit.WALL_TIME, spent_usd=1, calls=1, elapsed_seconds=1.0), StopReason.BUDGET_WALL),
        ],
    )
    def test_agent_caused_exceptions_map_to_a_stop(self, exc, stop):
        assert run_module._agent_stop_from(exc) is stop

    @pytest.mark.parametrize(
        "exc",
        [
            UnpricedModelError("no price"),
            MissingProviderKey("no key"),
            NoTaskScope("outside a scope"),
            RuntimeError("a bug"),
            # Only a routing bug in repolace can reach the graph's recursion limit; see `_agent_stop_from`.
            GraphRecursionError("limit"),
            ValueError("LLMAgent needs a model client"),
        ],
    )
    def test_everything_that_means_the_measurement_or_repolace_is_broken_is_not_a_stop(self, exc):
        """These fail the task, loudly: a price missing mid-run must not read as the agent running out of steps."""
        assert run_module._agent_stop_from(exc) is None
