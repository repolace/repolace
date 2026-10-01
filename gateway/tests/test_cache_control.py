"""Anthropic cache breakpoints, as a pure function.

This is where cost is won or lost in the agent loop. Without a breakpoint on the
conversation, a 40-step loop re-sends its whole growing history at the full input
rate on every step; with one, each step reads the previous steps from the cache.

The properties pinned here are the ones that fail *quietly*: a marker that
accumulates on the caller's history (rejected by Anthropic at five breakpoints,
long after the edit that caused it), or one that lands somewhere LiteLLM ignores
(no error, just no caching, and a bill that is several times larger).
"""

import copy
import json

from repolace_gateway.client import with_cache_control

from gateway_support import MESSAGES, TOOLS

EPHEMERAL = {"type": "ephemeral"}


def count_markers(*parts) -> int:
    """How many `cache_control` keys appear anywhere in the given structures."""
    return json.dumps(parts).count('"cache_control"')


def history(turns: int) -> list[dict]:
    """A realistic agent history: system, issue, then `turns` of (tool call, tool result)."""
    messages = [dict(m) for m in MESSAGES]
    for n in range(turns):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"call_{n}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"call_{n}", "content": f"file contents {n}"})
    return messages


class TestWhereTheMarkersGo:
    def test_a_string_system_prompt_becomes_a_marked_block(self):
        messages, _ = with_cache_control(MESSAGES, TOOLS)
        assert messages[0]["content"] == [{"type": "text", "text": "You fix bugs.", "cache_control": EPHEMERAL}]

    def test_the_last_tool_carries_a_marker_and_the_others_do_not(self):
        _, tools = with_cache_control(MESSAGES, TOOLS)
        assert tools[-1]["cache_control"] == EPHEMERAL
        assert all("cache_control" not in tool for tool in tools[:-1])

    def test_the_last_user_message_is_the_rolling_breakpoint(self):
        messages, _ = with_cache_control(MESSAGES, TOOLS)
        assert messages[1]["content"][-1]["cache_control"] == EPHEMERAL

    def test_a_tool_result_is_marked_on_the_message_not_a_content_block(self):
        """LiteLLM reads a tool result's marker from the message dict; a block-level one is ignored."""
        messages, _ = with_cache_control(history(2), TOOLS)
        last = messages[-1]
        assert last["role"] == "tool"
        assert last["cache_control"] == EPHEMERAL
        assert last["content"] == "file contents 1"  # still a plain string

    def test_only_the_newest_conversation_message_is_marked(self):
        messages, _ = with_cache_control(history(4), TOOLS)
        marked_tool_messages = [m for m in messages if m["role"] == "tool" and "cache_control" in m]
        assert marked_tool_messages == [messages[-1]]

    def test_the_rolling_marker_walks_back_past_a_trailing_assistant_turn(self):
        messages = [*MESSAGES, {"role": "assistant", "content": "thinking"}]
        marked, _ = with_cache_control(messages, TOOLS)
        assert "cache_control" in json.dumps(marked[1])
        assert "cache_control" not in json.dumps(marked[2])

    def test_a_list_content_marks_only_the_last_block(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        ]
        marked, _ = with_cache_control(messages, None)
        blocks = marked[1]["content"]
        assert "cache_control" not in blocks[0]
        assert blocks[1]["cache_control"] == EPHEMERAL


class TestTheBudgetOfBreakpoints:
    def test_three_markers_in_a_fresh_conversation(self):
        """Tools, system and the user turn: inside Anthropic's limit of four."""
        messages, tools = with_cache_control(MESSAGES, TOOLS)
        assert count_markers(messages, tools) == 3

    def test_still_three_after_many_turns(self):
        messages, tools = with_cache_control(history(30), TOOLS)
        assert count_markers(messages, tools) == 3

    def test_without_tools_there_is_no_tool_marker(self):
        messages, tools = with_cache_control(MESSAGES, None)
        assert tools is None
        assert count_markers(messages) == 2


class TestTheCallersHistoryIsLeftAlone:
    def test_the_inputs_are_not_mutated(self):
        messages, tools = history(3), copy.deepcopy(TOOLS)
        before = copy.deepcopy((messages, tools))
        with_cache_control(messages, tools)
        assert (messages, tools) == before

    def test_applying_it_every_turn_does_not_accumulate_markers(self):
        """The failure this guards: markers written into the loop's own history, one per step."""
        messages = history(1)
        for _ in range(10):
            marked, tools = with_cache_control(messages, TOOLS)
            assert count_markers(marked, tools) == 3
            messages.append({"role": "assistant", "content": None, "tool_calls": []})
            messages.append({"role": "tool", "tool_call_id": "x", "content": "more"})
        assert count_markers(messages) == 0


class TestWhatCannotBeMarked:
    def test_empty_content_is_left_unmarked_rather_than_sent_as_an_empty_block(self):
        """Anthropic rejects a cache breakpoint on an empty text block."""
        messages = [{"role": "system", "content": ""}, {"role": "user", "content": ""}]
        marked, _ = with_cache_control(messages, None)
        assert count_markers(marked) == 0

    def test_an_empty_tool_result_is_left_unmarked(self):
        messages = [*MESSAGES, {"role": "tool", "tool_call_id": "c", "content": ""}]
        marked, _ = with_cache_control(messages, None)
        assert "cache_control" not in marked[-1]

    def test_no_messages_at_all_is_not_an_error(self):
        assert with_cache_control([], None) == ([], None)
