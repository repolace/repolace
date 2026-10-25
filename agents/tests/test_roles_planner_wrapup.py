"""The planner's hand-over step: its last model call is held back so it can always submit a plan.

sweep-r1 (the role split as first built) ran seven tasks to the end and in every one the planner
used its whole step cap exploring and never called `submit_plan`, so the editor started with no
plan. The planner now explores for `plan - 1` steps and, if it has not submitted, is asked once more
with only `submit_plan` available. The cap is still `plan` model calls in all.
"""

from decimal import Decimal

import pytest
from agents_support import ScriptedLLM, ScriptedVerifier, attempt_record, reply, submit_reply, suite, tool_call
from repolace_agents.contracts import StopReason
from repolace_agents.role_prompts import role_step_caps
from repolace_gateway.budget import BudgetExceeded, BudgetLimit
from roles_support import NONCE, ONE_ATTEMPT, ROLE_LIMITS, A, B, plan_reply, run_roles, stages, tool_names

pytestmark = pytest.mark.anyio

CAPS = role_step_caps(ROLE_LIMITS)


def clean(n: int):
    return attempt_record(n, suite(passed=[A, B]))


def read():
    return reply(None, tool_call("read_file", {"path": "src/a.py"}))


def explore(n: int):
    return [read() for _ in range(n)]


async def run_one_attempt(llm, verifier):
    return await run_roles(llm, verifier, limits=ONE_ATTEMPT)


async def test_a_planner_that_explores_to_its_cap_hands_over_a_plan_on_its_last_step():
    llm = ScriptedLLM([*explore(CAPS.plan - 1), plan_reply("WRAPUP-PLAN fix parse()"), submit_reply()])

    result = await run_one_attempt(llm, ScriptedVerifier([clean(1)]))

    assert stages(llm) == ["plan"] * CAPS.plan + ["agent"]
    assert f"<plan-{NONCE}>\nWRAPUP-PLAN fix parse()\n</plan-{NONCE}>" in llm.calls[CAPS.plan].messages[1]["content"]
    assert result.stop_reason is StopReason.SUBMITTED and result.attempts == 1


async def test_the_hand_over_call_offers_only_submit_plan_and_says_why():
    llm = ScriptedLLM([*explore(CAPS.plan - 1), plan_reply(), submit_reply()])

    await run_one_attempt(llm, ScriptedVerifier([clean(1)]))

    last = llm.calls[CAPS.plan - 1]
    assert tool_names(last) == ["submit_plan"]
    assert last.messages[-1]["role"] == "user" and "exploration steps are used up" in last.messages[-1]["content"]


async def test_the_planner_never_makes_more_than_its_cap_of_calls():
    llm = ScriptedLLM([*explore(CAPS.plan + 3), submit_reply()])

    await run_one_attempt(llm, ScriptedVerifier([clean(1)]))

    assert stages(llm).count("plan") == CAPS.plan


async def test_a_planner_that_submits_early_gets_no_hand_over_call():
    llm = ScriptedLLM([read(), plan_reply(), submit_reply()])

    await run_one_attempt(llm, ScriptedVerifier([clean(1)]))

    assert stages(llm) == ["plan", "plan", "agent"]


async def test_a_hand_over_that_does_not_submit_leaves_the_editor_without_a_plan():
    text_only = reply("I have not decided yet")
    llm = ScriptedLLM([*explore(CAPS.plan - 1), text_only, submit_reply()])

    result = await run_one_attempt(llm, ScriptedVerifier([clean(1)]))

    assert "did not produce a plan" in llm.calls[CAPS.plan].messages[1]["content"]
    assert result.stop_reason is StopReason.SUBMITTED


async def test_a_budget_stop_during_the_hand_over_ends_the_run_before_any_edit():
    exceeded = BudgetExceeded(BudgetLimit.USD, spent_usd=Decimal("2.5"), calls=3, elapsed_seconds=1.0)
    verifier = ScriptedVerifier([])
    llm = ScriptedLLM([*explore(CAPS.plan - 1), exceeded])

    result = await run_one_attempt(llm, verifier)

    assert result.stop_reason is StopReason.BUDGET_USD and result.attempts == 0 and verifier.calls == []
    assert "agent" not in stages(llm)
