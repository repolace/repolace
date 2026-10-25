"""The early-edit nudge (experimental variant): a prompt paragraph, and a reminder added once.

Sweep 4 found the agent editing late (first edit near step 23 in the step-cap failures, 7 to 9 in
passes). The system prompt now asks for a first edit by `early_edit_step`, and if the first attempt
reaches that step with no `edit_file`/`create_file` call, one reminder is appended. Nothing here may
depend on a result, only on the agent's own calls; with the limit off the transcript is today's.
"""

import dataclasses

import pytest
from agents_support import (
    ScriptedLLM,
    ScriptedVerifier,
    attempt_record,
    make_deps,
    reply,
    scripted_toolbox,
    submit_reply,
    suite,
    tool_call,
)
from repolace_agents.contracts import AgentLimits, StopReason
from repolace_agents.graph import run_graph
from repolace_agents.prompts import build_system_prompt, early_edit_reminder

pytestmark = pytest.mark.anyio

NONCE = "n0nce1234"
A, B = "tests/test_a.py::test_one", "tests/test_a.py::test_two"
STEP = 4
LIMITS = AgentLimits(max_attempts=3, max_steps_per_attempt=8, early_edit_step=STEP)


def read():
    return reply(None, tool_call("read_file", {"path": "src/a.py"}))


def edit():
    return reply(None, tool_call("edit_file", {"path": "src/a.py", "new": "x = 1"}))


def clean(n: int):
    return attempt_record(n, suite(passed=[A, B]))


def red(n: int):
    return attempt_record(n, suite(passed=[A]))


async def run(llm, verifier, limits=LIMITS):
    deps = make_deps(llm=llm, tools=scripted_toolbox()[0], verify_attempt=verifier, baseline=suite(passed=[A, B]), limits=limits)
    return await run_graph(deps, nonce=NONCE)


def reminders(llm) -> list[tuple[int, str]]:
    """(index of the call that first saw it, text) for each reminder user message in the transcripts."""
    seen: list[tuple[int, str]] = []
    marker = "have not edited a file yet"
    for index, call in enumerate(llm.calls):
        count = sum(1 for m in call.messages if m["role"] == "user" and marker in str(m["content"]))
        if count and not any(i == index - 1 for i, _ in seen) and (index == 0 or sum(1 for m in llm.calls[index - 1].messages if m["role"] == "user" and marker in str(m["content"])) == 0):
            seen.append((index, next(str(m["content"]) for m in call.messages if m["role"] == "user" and marker in str(m["content"]))))
    return seen


async def test_a_reminder_is_added_once_when_no_edit_was_made_by_the_step():
    llm = ScriptedLLM([read() for _ in range(STEP)] + [edit(), submit_reply()])

    result = await run(llm, ScriptedVerifier([clean(1)]))

    found = reminders(llm)
    assert [index for index, _ in found] == [STEP]
    assert early_edit_reminder(STEP, LIMITS.max_steps_per_attempt) == found[0][1]
    # one reminder in total, never repeated on later calls
    assert sum(1 for m in llm.calls[-1].messages if m["role"] == "user" and "have not edited a file yet" in str(m["content"])) == 1
    assert result.stop_reason is StopReason.SUBMITTED


async def test_no_reminder_when_the_agent_already_edited():
    llm = ScriptedLLM([read(), edit()] + [read() for _ in range(STEP)] + [submit_reply()])

    await run(llm, ScriptedVerifier([clean(1)]))

    assert reminders(llm) == []


async def test_a_reply_with_no_tool_call_still_counts_as_a_step():
    llm = ScriptedLLM([reply("thinking") for _ in range(STEP)] + [edit(), submit_reply()])

    await run(llm, ScriptedVerifier([clean(1)]))

    assert [index for index, _ in reminders(llm)] == [STEP]


async def test_no_reminder_in_a_retry_attempt():
    llm = ScriptedLLM([edit(), submit_reply("one")] + [read() for _ in range(STEP)] + [submit_reply("two")])

    await run(llm, ScriptedVerifier([red(1), clean(2)]))

    assert reminders(llm) == []


@pytest.mark.parametrize("off", [None, 0])
async def test_with_the_limit_off_the_transcript_is_unchanged(off):
    limits = dataclasses.replace(LIMITS, early_edit_step=off)
    script = [read() for _ in range(STEP + 1)] + [edit(), submit_reply()]
    llm = ScriptedLLM(script)

    await run(llm, ScriptedVerifier([clean(1)]), limits)

    assert reminders(llm) == []
    assert "Make your first edit early" not in llm.calls[0].messages[0]["content"]


def test_the_system_prompt_carries_the_paragraph_with_the_step_and_stays_pure():
    on = build_system_prompt(LIMITS, NONCE)
    assert f"by about step {STEP}" in on and "Make your first edit early" in on
    assert build_system_prompt(LIMITS, NONCE) == on
    off = build_system_prompt(dataclasses.replace(LIMITS, early_edit_step=None), NONCE)
    assert "Make your first edit early" not in off


async def test_the_reminder_depends_only_on_the_agents_own_calls_not_on_results():
    """The same script gives the same transcript whichever way the (hidden) verification would go."""
    seen = []
    for verdicts in ([clean(1)], [red(1), clean(2)]):
        llm = ScriptedLLM([read() for _ in range(STEP)] + [edit(), submit_reply()] + ([submit_reply("two")] if len(verdicts) > 1 else []))
        await run(llm, ScriptedVerifier(verdicts))
        seen.append([m["content"] for m in llm.calls[STEP].messages if m["role"] == "user"][-1])
    assert seen[0] == seen[1]
