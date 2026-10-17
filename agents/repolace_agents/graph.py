"""The agent graph: `localize -> agent -> verify -> (router) -> agent | END`.

A hybrid, deliberately: the outer graph is fixed and small (the stages are
known), and the agent node is a bounded tool loop (what to do inside a stage is
not). LangGraph earns its place in the outer part -- the retry edge is a
conditional edge, and the recursion guard is a property of the graph rather than
of a counter we remember to check.

**What each node may and may not do, since the rules are easy to break.**

* `localize` makes no model call. It builds the two opening messages from the
  deps and the baseline *as `feedback.baseline_summary` renders it*.
* `agent` is the only node that calls the model, through `_run_loop` (the bounded
  tool loop every model-calling node shares). It stops on `submit`, on the
  step cap, on a budget, or on a provider failure -- and for the last two it does
  **not** append the response that crossed the line: its tool calls cannot be
  executed, so appending it would leave an assistant message whose `tool_calls`
  have no answers, and the provider rejects that on the next call.
  `UnpricedModelError`, `MissingProviderKey`, `NoTaskScope` and everything else
  propagate: they mean repolace is broken or misconfigured, and a task that fails
  loudly is worth more than one that quietly reports a stop reason.
* `verify` is skipped after a budget or LLM stop (the loop was cut short, so
  there is nothing worth scoring and no budget to act on the result). Otherwise it
  calls `verify_attempt`, and **it is the only node that touches
  `deps.baseline` or `AttemptRecord.result`, and only to hand them to
  `feedback.visible_feedback`.** Nothing else in this package may read either
  into a prompt.
* the router retries only on a *fresh*, not-clean verification with attempts to
  spare and no infrastructure fault.

Dependencies travel in `RunContext`, not in the state: a state is what a
checkpointer would persist, and an LLM client is not.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from repolace_gateway.budget import BudgetExceeded, BudgetLimit, current_scope
from repolace_gateway.errors import LLMCallError, NoTaskScope

from repolace_agents.contracts import AgentDeps, AgentLimits, AgentResult, StopReason
from repolace_agents.feedback import baseline_summary, render_feedback, visible_feedback
from repolace_agents.prompts import (
    EMPTY_TOOL_OUTPUT,
    SKIPPED_BUDGET,
    SUBMIT,
    TOO_MANY_CALLS,
    build_localize_message,
    build_system_prompt,
    elided,
    nudge_for,
    skipped_after,
)
from repolace_agents.render import escape_invisible, require_nonce, sanitize_text
from repolace_agents.role_prompts import build_briefing, role_step_caps
from repolace_agents.state import AgentState
from repolace_agents.tools.base import ToolBox

_BUDGET_STOPS = {
    BudgetLimit.USD: StopReason.BUDGET_USD,
    BudgetLimit.CALLS: StopReason.BUDGET_CALLS,
    BudgetLimit.WALL_TIME: StopReason.BUDGET_WALL,
}

#: The loop was cut short, so the tree may be half-edited: nothing is scored.
_SKIP_VERIFY = frozenset(
    {StopReason.BUDGET_USD, StopReason.BUDGET_CALLS, StopReason.BUDGET_WALL, StopReason.LLM_ERROR}
)

#: Reasons after which the router never retries, whatever `feedback_clean` says.
_NO_RETRY = _SKIP_VERIFY | {StopReason.NO_CHANGE, StopReason.MAX_ATTEMPTS}

_ELIDED = re.compile(r"\[output from attempt \d+ elided\]")

#: How many tool calls one model reply may have executed; the rest are answered, not run.
#: A module constant rather than an `AgentLimits` field because `AgentLimits` is a frozen
#: contract (and `ToolLimits` lives in the frozen `tools/base.py`); it belongs in
#: `AgentLimits` and moves there with the next contract change. Without it one reply
#: could carry thousands of calls -- the reviewer's 3,000 were all executed -- each
#: `run_python` up to 120 s and each `run_tests` up to 300 s, while the wall-clock
#: budget is only consulted inside the next model call.
MAX_TOOL_CALLS_PER_REPLY = 8

#: A transcript larger than this has its older tool results elided before the next model
#: call. About 100k tokens of the model's context, with room left for the reply: past that
#: the next request fails with a context-window error, which is `LLM_ERROR`, which skips
#: verify, so the work done would never be scored.
MAX_TRANSCRIPT_CHARS = 400_000
#: Elision spares the newest few tool results: they are what the model is reasoning about.
KEEP_RECENT_TOOL_RESULTS = 6

#: The result's summary is cut here. The prompt asks for three sentences and the tool
#: allows 4,000 characters; this is the bound every consumer (a database column, a pull
#: request body) can rely on.
MAX_SUMMARY_CHARS = 2000


class RoleGit(Protocol):
    """The three git operations the roles graph makes on the checkout, and nothing else.

    `GitRepo` (`repolace_shared.git`) in production -- the same calls the pipeline's own
    `review_diff` and `rewind_to` make, through the same pinned wrapper -- and a fake in a test.
    The single-agent graph makes none of them.
    """

    async def head_sha(self) -> str: ...

    async def diff_from(self, base_sha: str) -> str: ...

    async def reset_hard(self, sha: str) -> None: ...


@dataclass(frozen=True)
class RunContext:
    """What every node is given besides the state. Built once per run."""

    deps: AgentDeps
    #: The per-task delimiter nonce. Random in production; fixed in a test so it
    #: can be asserted on.
    nonce: str
    #: Roles only: the checkout's git, for the reviewer's diff and the debugger's `rewind`.
    git: RoleGit | None = None
    #: Roles only: HEAD when the run began, which is the base commit (the pipeline starts the
    #: agent on a fresh branch at the base). What the reviewer's diff is taken against.
    base_sha: str | None = None


#: Super-steps the single-agent graph needs: `localize` once, then `agent` and `verify` per attempt.
OPENING_NODES = 1
NODES_PER_ATTEMPT = 2


def recursion_limit_for(limits: AgentLimits) -> int:
    """The graph's recursion limit: twice the super-steps it can need, plus six.

    A guard, not a constraint. The single-agent graph needs `2 * max_attempts + 1` super-steps
    (localize, then agent and verify per attempt) and the tool loop lives inside one node, so it
    never approaches its limit of `4 * max_attempts + 8`. The roles graph needs at most
    `3 * max_attempts + 2` (localize and the planner, then an editor or the debugger, verify and
    the reviewer per attempt: `roles.OPENING_NODES`, `roles.NODES_PER_ATTEMPT`), so its limit is
    `6 * max_attempts + 10`. LangGraph's own default is 10007 -- effectively unlimited -- so
    without an explicit value a routing bug would spin through a model call per step until the
    budget ended it. This makes that a `GraphRecursionError` within a few dozen steps, which
    propagates: the task is FAILED, because it means repolace broke.
    """
    opening, per_attempt = OPENING_NODES, NODES_PER_ATTEMPT
    if limits.roles:
        # Imported here: `roles` builds on this module, so a top-level import would be a cycle.
        from repolace_agents import roles

        opening, per_attempt = roles.OPENING_NODES, roles.NODES_PER_ATTEMPT
    return 2 * (per_attempt * limits.max_attempts + opening) + 6


def overlay_mode(deps: AgentDeps) -> bool:
    """Is the oracle in play? **Fails closed:** either signal turns the filter's strict mode on.

    `issue.instance_id is not None` is what `IssueContext` documents as benchmark
    mode. `hidden_paths` non-empty is the other. Keying on the second alone would
    mean an overlay that happened to be empty silently opened the oracle.
    """
    return deps.issue.instance_id is not None or bool(deps.hidden_paths)


# --- message helpers ---------------------------------------------------------


def _assistant_turn(response: Any) -> dict[str, Any]:
    """The assistant message to append: the gateway's own, never empty, with valid arguments.

    An assistant message with no content and no tool calls is rejected by some
    providers on the *next* call, which would turn one empty reply into a failed
    task. A placeholder is cheaper than that.

    **A call whose arguments did not parse is echoed back with `"{}"`**, matched by
    index. The gateway keeps `raw_arguments` verbatim so that a valid call round-trips
    exactly, but replaying unparseable text (truncated JSON, single quotes, two
    concatenated objects, a bad escape) makes LiteLLM's message converter raise a
    non-transient error while building the next request. That becomes `LLM_ERROR`,
    which skips verify, so a single truncated call would cost the whole attempt. The
    model is still told what it got wrong: the tool result for that call carries the
    parse error, which `ToolBox.dispatch` builds from `ToolCall.parse_error`.
    """
    message = dict(response.message)
    if not response.tool_calls and not message.get("content"):
        message["content"] = "(empty reply)"
    raw_calls = message.get("tool_calls")
    if raw_calls:
        parsed = response.tool_calls
        message["tool_calls"] = [
            {**raw, "function": {**raw["function"], "arguments": "{}"}}
            if index < len(parsed) and parsed[index].parse_error
            else raw
            for index, raw in enumerate(raw_calls)
        ]
    return message


def _elide_tool_outputs(
    messages: Sequence[Mapping[str, Any]], attempt: int, *, keep_last: int = 0
) -> tuple[Mapping[str, Any], ...]:
    """Replace the content of tool results not already elided with a placeholder.

    Called on entry to a retry, when every non-elided tool result belongs to the
    attempt that just finished -- earlier ones were elided on their own retry -- so
    `attempt` names them correctly without tracking which message came from which
    attempt. The assistant messages and their `tool_calls` are left alone: only
    the *content* goes, so each call still has its answer and the list stays valid
    for the provider. That bounds the context across attempts, and the cache
    prefix is rebuilt once per attempt rather than never matching.

    `keep_last` spares the newest tool results, for the mid-attempt pass that keeps
    one long attempt inside the context window (see `MAX_TRANSCRIPT_CHARS`).
    """
    marker = elided(attempt)
    tool_positions = [i for i, message in enumerate(messages) if message.get("role") == "tool"]
    # `max(..., 0)`: with fewer results than `keep_last` a negative start would slice from the
    # end and spare the wrong ones.
    spared = set(tool_positions[max(len(tool_positions) - keep_last, 0):]) if keep_last > 0 else set()
    out: list[Mapping[str, Any]] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if (
            message.get("role") == "tool"
            and index not in spared
            and not (isinstance(content, str) and _ELIDED.fullmatch(content))
        ):
            out.append({**message, "content": marker})
        else:
            out.append(message)
    return tuple(out)


def _transcript_chars(messages: Sequence[Mapping[str, Any]]) -> int:
    """The characters a request would carry: every message's content and every call's arguments."""
    total = 0
    for message in messages:
        content = message.get("content")
        total += len(content) if isinstance(content, str) else 0
        for call in message.get("tool_calls") or ():
            total += len(call.get("function", {}).get("arguments") or "")
    return total


def _raise_if_budget_reached() -> None:
    """The gateway's budget check, between tool calls rather than only inside a model call.

    A reply's tool calls can each run for minutes, and the wall-clock budget is
    otherwise only looked at when the next model call starts. Outside a
    `task_scope` there is no budget to check and the model client itself raises
    `NoTaskScope` on the first call, so this stays quiet rather than raise it twice.
    """
    try:
        scope = current_scope()
    except NoTaskScope:
        return
    scope.budget.raise_if_reached()


# --- nodes -------------------------------------------------------------------


async def localize(state: AgentState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Build the opening messages. No model call, and no oracle: see the module docstring."""
    ctx = runtime.context
    deps = ctx.deps
    system = build_system_prompt(deps.limits, ctx.nonce)
    # The baseline reaches the prompt only through this call.
    baseline_text = baseline_summary(
        deps.baseline, deps.hidden_paths, overlay_mode=overlay_mode(deps), nonce=ctx.nonce
    )
    user = build_localize_message(
        deps.issue, deps.repo_overview, deps.retrieved, baseline_text, deps.limits, ctx.nonce
    )
    return {"messages": ({"role": "system", "content": system}, {"role": "user", "content": user})}


@dataclass(frozen=True)
class LoopOutcome:
    """What one bounded tool loop hands back to the node that ran it."""

    #: The conversation as the loop left it, ready to store in the state.
    messages: tuple[Mapping[str, Any], ...]
    #: Model calls made. A call that crossed a budget is counted (it was paid for) but not appended.
    calls: int
    stop: StopReason
    #: What the exit tool carried (`submit`'s summary), or None if it was never called.
    summary: str | None


async def _run_loop(
    ctx: RunContext,
    messages: Sequence[Mapping[str, Any]],
    toolbox: ToolBox,
    stage: str,
    step_cap: int,
    exit_tool: str,
    *,
    attempt: int,
) -> LoopOutcome:
    """The bounded tool loop: call the model on `stage`, run what it asks for, until something ends it.

    It ends on the exit tool (`exit_tool` names it, for the wording of the nudge and of a call
    made after it), on `step_cap` model calls, on a budget, or on a provider failure -- and for
    the last two the response that crossed the line is **not** appended (see the module
    docstring). `attempt` is what each call is recorded under. Every node that calls the model
    calls it through here, so every one of them is held to the same bounds: at most
    `MAX_TOOL_CALLS_PER_REPLY` executed per reply, the budget checked between tool calls,
    mid-attempt elision past `MAX_TRANSCRIPT_CHARS`, and every call answered.
    """
    llm = ctx.deps.llm
    assert llm is not None  # run_graph refuses a None before the graph is built

    messages = list(messages)
    schemas = toolbox.schemas()
    calls = 0
    summary: str | None = None

    while True:
        if calls >= step_cap:
            stop = StopReason.STEP_CAP
            break
        if _transcript_chars(messages) > MAX_TRANSCRIPT_CHARS:
            messages = list(_elide_tool_outputs(messages, attempt, keep_last=KEEP_RECENT_TOOL_RESULTS))
        try:
            # A tuple, so the client and this loop cannot alias one mutable list.
            response = await llm.complete(stage, tuple(messages), schemas, attempt=attempt)
        except BudgetExceeded as exc:
            if exc.response is not None:
                # The call happened and was paid for, so it is a step; but it is
                # not appended -- its tool calls cannot be run.
                calls += 1
            stop = _BUDGET_STOPS[exc.limit]
            break
        except LLMCallError as exc:
            if exc.provider_refused:
                # The provider rejected our key or our quota is spent: that says nothing about the
                # agent, so it is not a stop reason. It propagates and fails the task (a harness error).
                raise
            stop = StopReason.LLM_ERROR
            break

        calls += 1
        messages.append(_assistant_turn(response))

        if not response.tool_calls:
            messages.append({"role": "user", "content": nudge_for(exit_tool)})
            continue

        submitted = False
        budget_stop: StopReason | None = None
        for index, call in enumerate(response.tool_calls):
            # Every call is answered, whatever happens to it, or the next request is invalid.
            if submitted:
                # The exit tool is the model's last word, so a call after it is not run.
                messages.append({"role": "tool", "tool_call_id": call.id, "content": skipped_after(exit_tool)})
                continue
            if budget_stop is not None:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": SKIPPED_BUDGET})
                continue
            if index >= MAX_TOOL_CALLS_PER_REPLY:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": TOO_MANY_CALLS})
                continue
            try:
                _raise_if_budget_reached()
            except BudgetExceeded as exc:
                budget_stop = _BUDGET_STOPS[exc.limit]
                messages.append({"role": "tool", "tool_call_id": call.id, "content": SKIPPED_BUDGET})
                continue
            outcome = await toolbox.dispatch(call)
            # Tool output is untrusted too (file contents, search hits, test output): invisible
            # characters are escaped, not deleted, so it still matches what `edit_file` is given.
            # Empty content is replaced because some providers reject an empty tool message.
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

    return LoopOutcome(messages=tuple(messages), calls=calls, stop=stop, summary=summary)


async def localize_roles(state: AgentState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """The roles graph's opening: the briefing every role is shown first. No model call.

    Here rather than in `roles` for the reason `localize` is here: the baseline reaches a
    prompt only through `feedback.baseline_summary`, and this module is the one allowed to hand
    it over. The briefing carries the same issue, repository and retrieval blocks as the
    single agent's localize message (`role_prompts.build_briefing`).
    """
    ctx = runtime.context
    deps = ctx.deps
    baseline_text = baseline_summary(
        deps.baseline, deps.hidden_paths, overlay_mode=overlay_mode(deps), nonce=ctx.nonce
    )
    briefing = build_briefing(deps.issue, deps.repo_overview, deps.retrieved, baseline_text, deps.limits, ctx.nonce)
    return {"briefing": briefing}


async def agent(state: AgentState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Run one attempt: the bounded tool loop. See the module docstring for how it stops."""
    deps = runtime.context.deps
    assert deps.llm is not None and deps.tools is not None  # run_graph refuses a None before the graph is built

    messages = _elide_tool_outputs(state.messages, state.attempt) if state.attempt else state.messages
    loop = await _run_loop(
        runtime.context,
        messages,
        deps.tools,
        "agent",
        deps.limits.max_steps_per_attempt,
        SUBMIT,
        attempt=state.attempt + 1,
    )
    return {
        "messages": loop.messages,
        "steps": state.steps + loop.calls,
        "attempt_steps": loop.calls,
        "summary": loop.summary,
        "stop_reason": loop.stop,
        "feedback_clean": None,
    }


async def verify(state: AgentState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    """Score the attempt and decide what the agent is told. The only reader of the oracle."""
    ctx = runtime.context
    deps = ctx.deps

    if state.stop_reason in _SKIP_VERIFY:
        return {}

    attempt_no = state.attempt + 1
    record = await deps.verify_attempt(attempt_no)
    if record is None:
        # No net change. On attempt 1 that is "the agent did nothing"; on a later one
        # it is "nothing since the last scored attempt", and `last_attempt` is left
        # as it was -- it is only ever replaced by a record.
        return {"stop_reason": StopReason.NO_CHANGE, "feedback_clean": None}

    # An errored scored run (`SuiteResult.error`, not an infrastructure fault) is
    # never clean, so the policy is a function of that one bit alone: retry while
    # attempts remain, `MAX_ATTEMPTS` otherwise. In overlay mode `feedback` words
    # every such run identically, so what the hidden tests can steer here is that
    # bit and nothing finer (see the residual-risk note in `feedback.py`).
    changed = await deps.changed_files()
    feedback = visible_feedback(
        deps.baseline,
        record.result,
        changed,
        deps.baseline_files,
        deps.hidden_paths,
        overlay_mode=overlay_mode(deps),
        infrastructure_error=record.infrastructure_error,
    )
    update: dict[str, Any] = {
        "attempt": attempt_no,
        "last_attempt": record,
        "scored_summary": state.summary,
        "feedback_clean": feedback.clean,
    }
    if feedback.clean or record.infrastructure_error:
        return update
    if attempt_no >= deps.limits.max_attempts:
        # Every attempt used and the last scored one still red.
        update["stop_reason"] = StopReason.MAX_ATTEMPTS
        return update
    update["messages"] = state.messages + (
        {"role": "user", "content": render_feedback(feedback, nonce=ctx.nonce)},
    )
    return update


def route(state: AgentState, runtime: Runtime[RunContext]) -> Literal["agent", "__end__"]:
    """Back to the agent only on a fresh, not-clean verification with attempts left.

    `feedback_clean is False` is only ever set by a verification of *this*
    attempt (the agent node resets it and a skipped verify leaves it None), so
    `last_attempt` is the record it came from and its infrastructure flag is
    current.
    """
    limits = runtime.context.deps.limits
    if state.stop_reason in _NO_RETRY:
        return END
    if state.feedback_clean is not False:
        return END
    if state.attempt >= limits.max_attempts:
        return END
    if state.last_attempt is None or state.last_attempt.infrastructure_error:
        return END
    return "agent"


def build_graph(*, roles: bool = False):
    """The compiled graph. Cheap; built per run so nothing is shared between tasks.

    With `roles` off -- the default, and the only graph the pipeline runs unless the runner
    asks -- this is the single agent, `localize -> agent -> verify -> route`, exactly as it was
    before the role split existed. `roles=True` is the Phase 2 split (`repolace_agents.roles`).
    """
    if roles:
        # Imported here: `roles` builds on this module, so a top-level import would be a cycle.
        from repolace_agents.roles import build_roles_graph

        return build_roles_graph()
    builder = StateGraph(AgentState, context_schema=RunContext)
    builder.add_node("localize", localize)
    builder.add_node("agent", agent)
    builder.add_node("verify", verify)
    builder.add_edge(START, "localize")
    builder.add_edge("localize", "agent")
    builder.add_edge("agent", "verify")
    builder.add_conditional_edges("verify", route, {"agent": "agent", END: END})
    return builder.compile()


def _clean_summary(summary: str | None) -> str | None:
    """The agent's summary with NUL, control, format and surrogate characters removed, and capped.

    Done once, here, so every consumer gets text that is safe to store and to print: a
    NUL breaks a Postgres text insert, and a bidi override or a tag-block character is
    how a line is made to read differently from what it says. **Markdown, @mentions, `#N`
    references, links and images are deliberately NOT touched** -- the summary is still
    the model's own words, and neutralising those is the pull-request writer's job, since
    only it knows where the text ends up. An empty result is None: "no summary".
    """
    if summary is None:
        return None
    cleaned = sanitize_text(summary)[:MAX_SUMMARY_CHARS].strip()
    return cleaned or None


def result_from_state(state: AgentState) -> AgentResult:
    """Map the final state to the contract's result.

    `summary` is the scored attempt's whenever something was scored -- see
    `AgentState.scored_summary` -- and the agent's own latest account otherwise
    (an agent that found nothing to change may say so, and that is worth keeping).
    It is sanitised and capped here (see `_clean_summary`).
    """
    if state.stop_reason is None:
        raise RuntimeError("the graph ended without a stop reason; the agent node did not run")
    return AgentResult(
        stop_reason=state.stop_reason,
        summary=_clean_summary(state.scored_summary if state.last_attempt is not None else state.summary),
        attempts=state.attempt,
        steps=state.steps,
        last_attempt=state.last_attempt,
    )


async def run_graph(deps: AgentDeps, *, nonce: str, git: RoleGit | None = None) -> AgentResult:
    """Run the graph for one task. `run_agent` calls this with a random nonce.

    **The pipeline must call `run_agent`, never this.** The nonce is what keeps an
    attacker from writing the real closing delimiter, so it must be unpredictable;
    `run_agent` draws it from `secrets.token_hex(8)` on every call and takes no
    argument for it, because one a caller could choose is one an attacker could
    predict. This function exists separately only so a test can fix the nonce and
    assert on it. It refuses an empty or malformed one.

    With `deps.limits.roles` it runs the Phase 2 role split instead (`repolace_agents.roles`),
    reading the checkout's HEAD once first as the base the reviewer diffs against. `git` is a
    test seam like the nonce: `run_agent` never passes one, so production uses the checkout's
    own repository.
    """
    require_nonce(nonce)
    if deps.llm is None or deps.tools is None:
        raise ValueError("the LLM graph needs deps.llm and deps.tools; only the stub and gold runners run without them")
    if deps.limits.max_attempts < 1 or deps.limits.max_steps_per_attempt < 1:
        raise ValueError(f"max_attempts and max_steps_per_attempt must be at least 1, got {deps.limits}")
    if not deps.limits.roles:
        final = await build_graph().ainvoke(
            AgentState(),
            config={"recursion_limit": recursion_limit_for(deps.limits)},
            context=RunContext(deps=deps, nonce=nonce),
        )
        return result_from_state(AgentState(**final))

    role_step_caps(deps.limits)  # a per-attempt cap too small to split is refused before anything runs
    if git is None:
        # The production path (`run_agent` passes no git): the checkout's own repository, through
        # the shared wrapper and its config pins. Imported here so the single agent never loads it.
        from repolace_shared.git.repo import GitRepo

        git = GitRepo(path=deps.checkout)
    context = RunContext(deps=deps, nonce=nonce, git=git, base_sha=await git.head_sha())
    final = await build_graph(roles=True).ainvoke(
        AgentState(), config={"recursion_limit": recursion_limit_for(deps.limits)}, context=context
    )
    return result_from_state(AgentState(**final))
