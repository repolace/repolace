"""The Phase 2 role split: a planner, an editor, a debugger and a reviewer, behind `AgentLimits.roles`.

**Optional, off by default, and unbenchmarked.** It exists to answer one question on the same
instances as the single agent: does splitting the work beat it? With `roles` off,
`graph.build_graph` returns the single-agent graph untouched; nothing in the pipeline or the
worker turns `roles` on, only `LLMAgent(roles=True)` (`repolace-run-task --roles`).

The graph::

    localize -> planner -> editor -> verify -> route_roles
                   |                            |-- red, attempts left   -> debugger -> verify
                   `-- budget / LLM stop: END   |-- clean, attempts left -> reviewer -> editor | END
                                                `-- anything else        -> END

**What each node may do**, extending the graph module's rules rather than relaxing them:

* `localize` (`graph.localize_roles`) makes no model call; it renders the briefing every role is
  shown, and the baseline reaches it only through `feedback.baseline_summary`.
* `planner`, `editor`, `debugger` and `reviewer` are the only nodes that call the model, each
  through `graph._run_loop` with its own tool set, gateway stage and step cap
  (`role_prompts.role_step_caps`: the caps split the per-attempt steps, they do not multiply them).
  The gateway's `task_scope` and budget are the task's, so every role spends from one budget.
* `verify` is `graph.verify` unchanged -- still the only reader of `deps.baseline` and of an
  attempt's result, and only to hand them to the feedback filter -- wrapped to remember which
  commit each scored attempt ran against, which is what `rewind` resolves its numbers with.
* **Routing never reads the oracle.** `route_roles` sees exactly what `graph.route` sees: the
  stop reason, `feedback_clean` (computed from the filtered results), the attempt count and the
  infrastructure flag. So the hidden tests can steer a roles run by the same one bit they can
  steer the single agent by, whether a scored run completed, and by nothing finer.
* The planner and the reviewer have read-only tools; the debugger's `rewind` accepts only a
  scored attempt's number. Text one role hands another (the plan, the reviewer's comments) is
  framed as data in `plan` and `review` blocks.

**The contract is the single agent's.** `AgentResult.attempts` counts `verify_attempt` records,
`last_attempt` is the last one, and every role failure maps onto the existing eight stop reasons:
a budget or provider failure in the planner or the reviewer is that `BUDGET_*` or `LLM_ERROR`; a
planner that never submits leaves the editor to start without a plan; a reviewer that never gives
a verdict leaves the change as it stands; and an editor that changes nothing after a requested
review keeps its own stop reason, since the reviewed attempt is what stands.

**Not imported by name from the graph module where the oracle tripwire would see it**
(`from repolace_agents import graph`, then `graph.verify`): this module is in
`ORACLE_BLIND_FILES`, which may not bring a name starting with `verify` into scope.
"""

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from repolace_agents import graph
from repolace_agents.prompts import SUBMIT
from repolace_agents.role_prompts import (
    READ_ONLY_TOOLS,
    SUBMIT_PLAN,
    Role,
    editor_message,
    planner_message,
    role_step_caps,
    system_prompt,
)
from repolace_agents.state import AgentState
from repolace_agents.tools.base import ToolBox
from repolace_agents.tools.role_tools import SubmitPlan

#: The gateway stage each role calls. The editor and the debugger are today's agent and use its
#: stage; `plan` and `review` are optional stages that follow `agent` unless a config names them
#: (`repolace_gateway.config.OPTIONAL_STAGE_FALLBACKS`), so a sweep's `--model` reaches every role.
PLAN_STAGE = "plan"
EDIT_STAGE = "agent"
REVIEW_STAGE = "review"

#: The node names. Distinct from every state field: LangGraph refuses a node named like a channel.
PLANNER, EDITOR, DEBUGGER, REVIEWER = "planner", "editor", "debugger", "reviewer"

#: Super-steps the roles graph needs: `localize` and `planner` once, then at most three per
#: attempt (an editor or the debugger, `verify`, the reviewer). `graph.recursion_limit_for` reads them.
OPENING_NODES = 2
NODES_PER_ATTEMPT = 3


def _system(role: Role, ctx: graph.RunContext) -> dict[str, Any]:
    return {"role": "system", "content": system_prompt(role, ctx.deps.limits, ctx.nonce)}


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _tools(ctx: graph.RunContext) -> ToolBox:
    tools = ctx.deps.tools
    assert tools is not None  # run_graph refuses a None before the graph is built
    return tools


async def planner(state: AgentState, runtime: Runtime[graph.RunContext]) -> dict[str, Any]:
    """Study the issue read-only and hand the editor a plan. Its own transcript, discarded after."""
    ctx = runtime.context
    caps = role_step_caps(ctx.deps.limits)
    toolbox = _tools(ctx).subset(READ_ONLY_TOOLS, extra=(SubmitPlan(),))
    messages = (_system(Role.PLANNER, ctx), _user(planner_message(state.briefing)))
    loop = await graph._run_loop(ctx, messages, toolbox, PLAN_STAGE, caps.plan, SUBMIT_PLAN, attempt=1)
    update: dict[str, Any] = {"steps": state.steps + loop.calls, "plan": loop.summary}
    if loop.stop in graph._SKIP_VERIFY:
        # The budget or the provider ended the run before any edit: nothing to score.
        update["stop_reason"] = loop.stop
    return update


def after_planner(state: AgentState, runtime: Runtime[graph.RunContext]) -> Literal["editor", "__end__"]:
    return END if state.stop_reason in graph._SKIP_VERIFY else EDITOR


async def _work(state: AgentState, runtime: Runtime[graph.RunContext], role: Role, toolbox: ToolBox) -> dict[str, Any]:
    """One editing attempt, by the editor or the debugger, on the shared work transcript.

    The work transcript is the single agent's conversation: the editor opens it with the briefing
    and the plan, and every later attempt continues it with the earlier tool outputs elided, as a
    single-agent retry does. Only its system message changes, to the role now running.
    """
    ctx = runtime.context
    caps = role_step_caps(ctx.deps.limits)
    if state.messages:
        earlier = graph._elide_tool_outputs(state.messages, state.attempt) if state.attempt else state.messages
        messages = (_system(role, ctx), *earlier[1:])
    else:
        messages = (_system(role, ctx), _user(editor_message(state.briefing, state.plan, ctx.nonce)))
    loop = await graph._run_loop(ctx, messages, toolbox, EDIT_STAGE, caps.edit, SUBMIT, attempt=state.attempt + 1)
    return {
        "messages": loop.messages,
        "steps": state.steps + loop.calls,
        "attempt_steps": loop.calls,
        "summary": loop.summary,
        "stop_reason": loop.stop,
        "feedback_clean": None,
    }


async def editor(state: AgentState, runtime: Runtime[graph.RunContext]) -> dict[str, Any]:
    """The first attempt, and any attempt a review asked for: all nine tools, as the single agent has."""
    return await _work(state, runtime, Role.EDITOR, _tools(runtime.context))


async def score(state: AgentState, runtime: Runtime[graph.RunContext]) -> dict[str, Any]:
    """`graph.verify`, plus the bookkeeping the roles need. Reads nothing of the oracle itself.

    Records `(attempt, commit)` for a fresh scored attempt, so `rewind` can resolve a number to
    the commit the scorer ran.
    """
    update = await graph.verify(state, runtime)
    scored = update.get("last_attempt")
    if scored is not None:
        update = {**update, "scored_commits": (*state.scored_commits, (scored.attempt, scored.commit_sha))}
    return update


def route_roles(state: AgentState, runtime: Runtime[graph.RunContext]) -> Literal["editor", "__end__"]:
    """Back to the editor on a fresh, red verification with attempts left; otherwise the end.

    The same inputs `graph.route` decides on, and nothing else: see the module docstring.
    """
    limits = runtime.context.deps.limits
    if state.stop_reason in graph._NO_RETRY:
        return END
    if state.feedback_clean is None:
        return END
    if state.last_attempt is None or state.last_attempt.infrastructure_error:
        return END
    if state.attempt >= limits.max_attempts or state.feedback_clean:
        return END
    return EDITOR


def build_roles_graph():
    """The compiled roles graph. Built per run, like the single-agent one."""
    builder = StateGraph(AgentState, context_schema=graph.RunContext)
    builder.add_node("localize", graph.localize_roles)
    builder.add_node(PLANNER, planner)
    builder.add_node(EDITOR, editor)
    builder.add_node("verify", score)
    builder.add_edge(START, "localize")
    builder.add_edge("localize", PLANNER)
    builder.add_conditional_edges(PLANNER, after_planner, {EDITOR: EDITOR, END: END})
    builder.add_edge(EDITOR, "verify")
    builder.add_conditional_edges("verify", route_roles, {EDITOR: EDITOR, END: END})
    return builder.compile()
