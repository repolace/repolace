"""The planner is told to write a plan a smaller editor can apply, the editor to follow it, and the debugger has its own stage."""

import pytest
from agents_support import ScriptedLLM, ScriptedVerifier, attempt_record, submit_reply, suite
from repolace_agents.contracts import AgentLimits
from repolace_agents.role_prompts import Role, role_step_caps, system_prompt
from roles_support import NONCE, ROLE_LIMITS, A, B, plan_reply, review_reply, run_roles, stages

pytestmark = pytest.mark.anyio


def test_the_planner_is_asked_for_exact_lines_a_reproduction_and_a_stop_condition():
    text = system_prompt(Role.PLANNER, ROLE_LIMITS, NONCE)

    for phrase in ("exact current lines to replace", "copied verbatim", "reproduction", "stop condition", "under about 6,000 characters"):
        assert phrase in text


def test_the_editor_applies_the_plan_in_order_and_edits_first():
    text = system_prompt(Role.EDITOR, ROLE_LIMITS, NONCE)

    assert "Apply its edits in the order given" in text and "Make the first edit straight away" in text
    assert "If there is no plan, locate the cause yourself" in text


def test_the_split_gives_the_planner_three_eighths_and_still_adds_up():
    for total in range(3, 120):
        caps = role_step_caps(AgentLimits(max_steps_per_attempt=total, roles=True))
        assert caps.plan + caps.edit + caps.review == total and min(caps.plan, caps.edit, caps.review) >= 1
    assert role_step_caps(AgentLimits(max_steps_per_attempt=40, roles=True)).plan == 15


async def test_the_debugger_calls_its_own_stage():
    llm = ScriptedLLM([plan_reply(), submit_reply("one"), submit_reply("two"), review_reply()])

    await run_roles(llm, ScriptedVerifier([attempt_record(1, suite(passed=[A])), attempt_record(2, suite(passed=[A, B]))]))

    assert stages(llm) == ["plan", "agent", "debug", "review"]
