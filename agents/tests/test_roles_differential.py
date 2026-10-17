"""With `roles` off, the graph does exactly what it did before the role split existed.

The split exists to be compared against the single agent on the same instances, so the single
agent must not have changed underneath it. The reference below is the `agent` node as it stood
before `_run_loop` was extracted from it (main at 5d8618e, `graph.py`), copied verbatim except that
module-level names are read through the graph module, so a test that patches a constant patches
both. It is assembled into the same `localize -> agent -> verify -> route` graph, and every
scenario runs through it and through `run_graph`; the requests the model receives (stage, attempt,
messages, tool schemas, keyword arguments), the result, the calls consumed and the verifier's calls
must be identical. Identical calls are identical spend: the scripted model prices each reply.
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from agents_support import (
    FunctionTool,
    ScriptedLLM,
    ScriptedVerifier,
    attempt_record,
    make_deps,
    malformed_tool_call,
    object_schema,
    reply,
    scripted_toolbox,
    suite,
    tool_call,
)
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from repolace_agents import graph as g
from repolace_agents.contracts import AgentLimits, StopReason
from repolace_agents.prompts import (
    EMPTY_TOOL_OUTPUT,
    NUDGE,
    SKIPPED_AFTER_SUBMIT,
    SKIPPED_BUDGET,
    TOO_MANY_CALLS,
)
from repolace_agents.render import escape_invisible
from repolace_agents.state import AgentState
from repolace_agents.tools.base import ToolBox, ToolOutcome
from repolace_gateway.budget import BudgetExceeded, BudgetLimit, TaskBudget, task_scope
from repolace_gateway.errors import LLMCallError

pytestmark = pytest.mark.anyio

NONCE = "n0nce1234"
A, B = "tests/test_a.py::test_one", "tests/test_a.py::test_two"
LIMITS = AgentLimits(max_attempts=3, max_steps_per_attempt=6)


async def reference_agent(state: AgentState, runtime: Runtime[g.RunContext]) -> dict[str, Any]:
    """The pre-extraction `agent` node, verbatim but for the `g.` prefixes."""
    deps = runtime.context.deps
    llm, tools = deps.llm, deps.tools
    assert llm is not None and tools is not None

    attempt_no = state.attempt + 1
    messages = list(g._elide_tool_outputs(state.messages, state.attempt) if state.attempt else state.messages)
    schemas = tools.schemas()
    steps = state.steps
    attempt_steps = 0
    summary: str | None = None

    while True:
        if attempt_steps >= deps.limits.max_steps_per_attempt:
            stop = StopReason.STEP_CAP
            break
        if g._transcript_chars(messages) > g.MAX_TRANSCRIPT_CHARS:
            messages = list(g._elide_tool_outputs(messages, attempt_no, keep_last=g.KEEP_RECENT_TOOL_RESULTS))
        try:
            response = await llm.complete("agent", tuple(messages), schemas, attempt=attempt_no)
        except BudgetExceeded as exc:
            if exc.response is not None:
                steps += 1
                attempt_steps += 1
            stop = g._BUDGET_STOPS[exc.limit]
            break
        except LLMCallError as exc:
            if exc.provider_refused:
                raise
            stop = StopReason.LLM_ERROR
            break

        steps += 1
        attempt_steps += 1
        messages.append(g._assistant_turn(response))

        if not response.tool_calls:
            messages.append({"role": "user", "content": NUDGE})
            continue

        submitted = False
        budget_stop: StopReason | None = None
        for index, call in enumerate(response.tool_calls):
            if submitted:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": SKIPPED_AFTER_SUBMIT})
                continue
            if budget_stop is not None:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": SKIPPED_BUDGET})
                continue
            if index >= g.MAX_TOOL_CALLS_PER_REPLY:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": TOO_MANY_CALLS})
                continue
            try:
                g._raise_if_budget_reached()
            except BudgetExceeded as exc:
                budget_stop = g._BUDGET_STOPS[exc.limit]
                messages.append({"role": "tool", "tool_call_id": call.id, "content": SKIPPED_BUDGET})
                continue
            outcome = await tools.dispatch(call)
            content = escape_invisible(outcome.content) or EMPTY_TOOL_OUTPUT
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if outcome.submitted:
                submitted = True
                summary = outcome.summary
        if budget_stop is not None:
            stop = budget_stop
            break
        if submitted:
            stop = StopReason.SUBMITTED
            break

    return {
        "messages": tuple(messages),
        "steps": steps,
        "attempt_steps": attempt_steps,
        "summary": summary,
        "stop_reason": stop,
        "feedback_clean": None,
    }


def reference_graph():
    builder = StateGraph(AgentState, context_schema=g.RunContext)
    builder.add_node("localize", g.localize)
    builder.add_node("agent", reference_agent)
    builder.add_node("verify", g.verify)
    builder.add_edge(START, "localize")
    builder.add_edge("localize", "agent")
    builder.add_edge("agent", "verify")
    builder.add_conditional_edges("verify", g.route, {"agent": "agent", END: END})
    return builder.compile()


async def run_reference(deps):
    final = await reference_graph().ainvoke(
        AgentState(),
        config={"recursion_limit": 4 * deps.limits.max_attempts + 8},
        context=g.RunContext(deps=deps, nonce=NONCE),
    )
    return g.result_from_state(AgentState(**final))


# --- scenarios: each builds a fresh script, so the two runs share nothing ------------------


def c(name: str, n: str, **args):
    return tool_call(name, args, id=f"c{n}")


def submit(n: str, summary: str = "Fixed it.", *before):
    return reply(None, *before, c("submit", n, summary=summary))


def clean(n: int):
    return attempt_record(n, suite(passed=[A, B]))


def red(n: int):
    return attempt_record(n, suite(passed=[A], failed=[B], stdout_tail="AssertionError: two"))


def exceeded(limit=BudgetLimit.USD, response=None):
    return BudgetExceeded(limit, spent_usd=Decimal("2.5"), calls=3, elapsed_seconds=1.0, response=response)


def big_output(name, args):
    return f"{name}:" + "x" * 3000


SCENARIOS = {
    "read_edit_submit": lambda: ([reply(None, c("read_file", "1", path="a.py")), reply(None, c("edit_file", "2", path="a.py", new="x")), submit("3")], [clean(1)], {}),
    "red_then_clean_retry_elides": lambda: ([reply(None, c("read_file", "1", path="a.py")), submit("2", "one"), reply(None, c("read_file", "3", path="b.py")), submit("4", "two")], [red(1), clean(2)], {}),
    "three_red_is_max_attempts": lambda: ([submit("1"), submit("2"), submit("3")], [red(1), red(2), red(3)], {}),
    "step_cap": lambda: ([reply(None, c("read_file", str(i), path="a.py")) for i in range(6)], [clean(1)], {}),
    "empty_reply_is_nudged": lambda: ([reply("thinking"), reply(None), submit("1")], [clean(1)], {}),
    "budget_with_response_not_appended": lambda: ([reply(None, c("read_file", "1", path="a.py")), exceeded(BudgetLimit.CALLS, response=reply(None, c("read_file", "2", path="b.py")))], [], {}),
    "budget_on_retry_keeps_attempt_one": lambda: ([submit("1", "one"), exceeded()], [red(1)], {}),
    "llm_error": lambda: ([reply(None, c("read_file", "1", path="a.py")), LLMCallError("x", stage="agent", model="m", retries=2)], [], {}),
    "too_many_calls_in_one_reply": lambda: ([reply(None, *[c("read_file", f"m{i}", path=f"f{i}") for i in range(11)]), submit("1")], [clean(1)], {}),
    "calls_after_submit_are_skipped": lambda: ([reply(None, c("submit", "1", summary="done"), c("read_file", "2", path="a.py"))], [clean(1)], {}),
    "malformed_arguments_replayed_as_empty": lambda: ([reply(None, malformed_tool_call("read_file", '{"path": "a.py"', id="cm")), submit("1")], [clean(1)], {}),
    "no_change": lambda: ([submit("1")], [None], {}),
    "no_change_after_red": lambda: ([submit("1"), submit("2")], [red(1), None], {}),
    "infrastructure_fault_is_not_retried": lambda: ([submit("1")], [attempt_record(1, suite(passed=[A], error="gone"), infrastructure_error=True)], {}),
    "mid_attempt_elision": lambda: ([reply(None, *[c("read_file", f"e{n}{i}", path="a.py") for i in range(3)]) for n in range(5)] + [submit("1")], [clean(1)], {"result_for": big_output, "limits": AgentLimits(max_attempts=3, max_steps_per_attempt=8)}),
    "benchmark_mode_red": lambda: ([submit("1"), submit("2")], [red(1), clean(2)], {"hidden_paths": frozenset({"tests/test_hidden.py"})}),
}


def deps_for(llm, verifier, extra):
    extra = dict(extra)
    toolbox = scripted_toolbox(extra.pop("result_for", None))[0]
    fields = {"llm": llm, "tools": toolbox, "verify_attempt": verifier, "baseline": suite(passed=[A, B]), "limits": LIMITS,
              "repo_overview": "src/\n  a.py"}
    return make_deps(**{**fields, **extra})


async def observe(runner, scenario, limits_override=None):
    script, verdicts, extra = SCENARIOS[scenario]()
    if limits_override is not None:
        extra = {**extra, "limits": limits_override(extra.get("limits", LIMITS))}
    llm, verifier = ScriptedLLM(script), ScriptedVerifier(verdicts)
    deps = deps_for(llm, verifier, extra)
    try:
        result = await runner(deps)
    except Exception as exc:  # the failure itself is part of what must be identical
        result = (type(exc).__name__, str(exc))
    calls = [(call.stage, call.attempt, call.messages, call.tools, call.kwargs) for call in llm.calls]
    return calls, result, llm.unused, verifier.calls


def current(deps):
    return g.run_graph(deps, nonce=NONCE)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_the_single_agent_graph_is_unchanged(scenario):
    before = await observe(run_reference, scenario)
    after = await observe(current, scenario)

    assert after == before
    assert before[0], "the scenario made at least one model call, so the comparison is not vacuous"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_an_explicit_roles_false_is_the_default(scenario):
    import dataclasses

    explicit = await observe(current, scenario, lambda limits: dataclasses.replace(limits, roles=False))

    assert explicit == await observe(run_reference, scenario)


@pytest.mark.parametrize(
    "limit, reason",
    [(BudgetLimit.WALL_TIME, StopReason.BUDGET_WALL), (BudgetLimit.USD, StopReason.BUDGET_USD), (BudgetLimit.CALLS, StopReason.BUDGET_CALLS)],
)
async def test_a_budget_that_runs_out_between_tool_calls_is_unchanged(limit, reason):
    async def once(runner):
        now = [0.0]
        budget = TaskBudget(max_usd=1, max_calls=5, max_wall_seconds=10, clock=lambda: now[0])

        async def spend(args):
            if limit is BudgetLimit.WALL_TIME:
                now[0] = 100.0
            elif limit is BudgetLimit.USD:
                budget.charge(Decimal(5))
            else:
                budget.calls = 5
            return ToolOutcome("ok")

        toolbox = ToolBox([FunctionTool("read_file", spend, parameters=object_schema({"path": {"type": "string"}}, ["path"]))])
        llm, verifier = ScriptedLLM([reply(None, *[c("read_file", str(i), path=f"f{i}") for i in range(5)])]), ScriptedVerifier([])
        deps = make_deps(llm=llm, tools=toolbox, verify_attempt=verifier, baseline=suite(passed=[A, B]), limits=LIMITS)
        with task_scope(uuid.uuid4(), budget):
            result = await runner(deps)
        return [call.messages for call in llm.calls], result, verifier.calls

    before, after = await once(run_reference), await once(current)

    assert after == before and before[1].stop_reason is reason


async def test_a_patched_transcript_cap_reaches_both_so_the_elision_scenario_compares_like_with_like(monkeypatch):
    monkeypatch.setattr(g, "MAX_TRANSCRIPT_CHARS", 5000)

    before = await observe(run_reference, "mid_attempt_elision")
    after = await observe(current, "mid_attempt_elision")

    assert after == before
    assert any("elided" in str(call[2]) for call in before[0]), "the cap was crossed, so elision ran in both"
