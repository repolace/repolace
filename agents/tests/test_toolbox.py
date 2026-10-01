"""`ToolBox`: the one place a tool call is checked and run.

The line these tests hold is between a model-visible failure and a bug. The first
becomes an `is_error` outcome the model can read and correct; the second must
propagate, because turning a bug into a polite tool result would let a broken
tool pass for a model that is simply bad at using it.
"""

import asyncio

import pytest

from repolace_agents.tools.base import ToolBox, ToolError, ToolLimits, ToolOutcome, ToolSpec
from agents_support import FakeToolCall, FunctionTool, object_schema

pytestmark = pytest.mark.anyio

EDIT_SCHEMA = object_schema(
    {
        "path": {"type": "string"},
        "limit": {"type": "integer"},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "items": {"type": "array"},
        "options": {"type": "object"},
        "note": {"type": "string"},
    },
    required=["path"],
)


def box(*tools, **kw) -> ToolBox:
    return ToolBox(list(tools), **kw)


def edit_tool(handler=None) -> FunctionTool:
    return FunctionTool("edit_file", handler, parameters=EDIT_SCHEMA)


class TestSpecSchema:
    def test_it_has_the_function_calling_shape(self):
        spec = ToolSpec("read_file", "Read a file.", object_schema({"path": {"type": "string"}}, ["path"]))

        assert spec.schema() == {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }

    def test_the_schema_is_a_copy_the_caller_may_mutate(self):
        spec = ToolSpec("t", "d", object_schema({"path": {"type": "string"}}))
        schema = spec.schema()

        schema["function"]["parameters"]["properties"]["path"]["type"] = "integer"
        schema["function"]["parameters"]["properties"]["injected"] = {}

        assert spec.parameters["properties"] == {"path": {"type": "string"}}

    def test_the_spec_is_immutable(self):
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            ToolSpec("t", "d", {}).name = "other"  # type: ignore[misc]


class TestConstruction:
    def test_duplicate_names_are_rejected(self):
        with pytest.raises(ValueError, match="duplicate tool name.*edit_file"):
            box(edit_tool(), edit_tool())

    def test_schemas_are_listed_in_the_order_given(self):
        toolbox = box(FunctionTool("b_tool"), FunctionTool("a_tool"))

        assert [s["function"]["name"] for s in toolbox.schemas()] == ["b_tool", "a_tool"]

    def test_an_empty_box_is_valid(self):
        assert box().schemas() == []

    def test_a_property_type_the_box_cannot_enforce_is_refused_up_front(self):
        """Otherwise it would go unchecked on every call, and the author would
        believe the schema protected an argument it did not."""
        bad = FunctionTool("t", parameters=object_schema({"x": {"type": "str"}}))

        with pytest.raises(ValueError, match="unsupported type 'str'"):
            box(bad)

    def test_a_required_property_that_is_not_declared_is_refused(self):
        bad = FunctionTool("t", parameters=object_schema({"x": {"type": "string"}}, required=["y"]))

        with pytest.raises(ValueError, match="required property 'y'"):
            box(bad)

    def test_the_default_output_cap_is_the_planned_limit(self):
        assert box().max_output_chars == ToolLimits().max_output_chars == 8000


class TestDispatchRunsTheTool:
    async def test_valid_arguments_reach_the_tool_and_its_outcome_comes_back(self):
        seen = {}

        async def handler(args):
            seen.update(args)
            return ToolOutcome("edited")

        tool = edit_tool(handler)

        outcome = await box(tool).dispatch(FakeToolCall("edit_file", {"path": "a.py", "limit": 3}))

        assert outcome == ToolOutcome("edited")
        assert seen == {"path": "a.py", "limit": 3}

    async def test_a_submission_outcome_passes_through_intact(self):
        async def handler(args):
            return ToolOutcome("submitted", submitted=True, summary="fixed the parser")

        outcome = await box(FunctionTool("submit", handler)).dispatch(FakeToolCall("submit"))

        assert outcome.submitted and outcome.summary == "fixed the parser"

    async def test_it_works_with_the_gateways_real_tool_call(self):
        """`ToolCall` is duck-typed here, so this is what keeps the duck honest."""
        from repolace_gateway.client import ToolCall

        call = ToolCall(id="c1", name="edit_file", arguments={"path": "a.py"}, raw_arguments='{"path":"a.py"}')

        outcome = await box(edit_tool()).dispatch(call)

        assert not outcome.is_error

    async def test_a_gateway_tool_call_with_a_parse_error_is_an_error_outcome(self):
        from repolace_gateway.client import ToolCall

        call = ToolCall(
            id="c1", name="edit_file", arguments={}, raw_arguments="{oops", parse_error="Expecting property name"
        )

        outcome = await box(edit_tool()).dispatch(call)

        assert outcome.is_error and "Expecting property name" in outcome.content


class TestModelVisibleFailures:
    """Each of these is the model's mistake, so each is an `is_error` outcome whose
    message says what to do differently -- and the tool is never called."""

    async def test_an_unknown_tool_lists_the_real_ones(self):
        tool = edit_tool()

        outcome = await box(tool, FunctionTool("grep")).dispatch(FakeToolCall("rm_rf"))

        assert outcome.is_error
        assert "unknown tool 'rm_rf'" in outcome.content
        assert "edit_file" in outcome.content and "grep" in outcome.content
        assert tool.calls == []

    async def test_a_parse_error_says_what_the_model_got_wrong(self):
        tool = edit_tool()
        call = FakeToolCall("edit_file", {}, parse_error="Unterminated string at char 40")

        outcome = await box(tool).dispatch(call)

        assert outcome.is_error
        assert "Unterminated string at char 40" in outcome.content
        assert "JSON object" in outcome.content
        assert tool.calls == []

    async def test_a_missing_required_argument_is_named(self):
        tool = edit_tool()

        outcome = await box(tool).dispatch(FakeToolCall("edit_file", {"limit": 1}))

        assert outcome.is_error
        assert "missing required argument(s): path" in outcome.content
        assert "edit_file" in outcome.content
        assert tool.calls == []

    async def test_an_unknown_argument_is_named_with_what_is_accepted(self):
        tool = edit_tool()

        outcome = await box(tool).dispatch(FakeToolCall("edit_file", {"path": "a.py", "mode": "x"}))

        assert outcome.is_error
        assert "unexpected argument(s): mode" in outcome.content
        assert "path" in outcome.content  # what it does accept
        assert tool.calls == []

    async def test_unknown_arguments_are_allowed_when_additional_properties_is_not_false(self):
        for additional in (True, None):
            tool = FunctionTool(
                "t", parameters=object_schema({"a": {"type": "string"}}, additional_properties=additional)
            )

            outcome = await box(tool).dispatch(FakeToolCall("t", {"a": "x", "extra": 1}))

            assert not outcome.is_error

    @pytest.mark.parametrize(
        ("argument", "value", "expected", "got"),
        [
            ("path", 5, "string", "integer"),
            ("path", None, "string", "null"),
            ("path", ["a.py"], "string", "array"),
            ("limit", "3", "integer", "string"),
            ("limit", 3.5, "integer", "number"),
            ("limit", True, "integer", "boolean"),  # bool is an int in Python, not in JSON
            ("ratio", True, "number", "boolean"),
            ("ratio", "0.5", "number", "string"),
            ("flag", 1, "boolean", "integer"),
            ("flag", "true", "boolean", "string"),
            ("items", "a,b", "array", "string"),
            ("items", {"a": 1}, "array", "object"),
            ("options", ["a"], "object", "array"),
        ],
    )
    async def test_a_wrong_type_is_named_with_what_was_sent(self, argument, value, expected, got):
        tool = edit_tool()
        arguments = {"path": "a.py", argument: value}

        outcome = await box(tool).dispatch(FakeToolCall("edit_file", arguments))

        assert outcome.is_error
        assert f"argument {argument!r} must be {expected}, got {got}" in outcome.content
        assert tool.calls == []

    @pytest.mark.parametrize(
        "arguments",
        [
            {"path": "a.py", "limit": 3},
            {"path": "a.py", "ratio": 3},  # an integer is a valid number
            {"path": "a.py", "ratio": 0.5},
            {"path": "a.py", "flag": False},
            {"path": "a.py", "items": []},
            {"path": "a.py", "items": ("a", "b")},
            {"path": "a.py", "options": {}},
            {"path": ""},  # an empty string is still a string
        ],
    )
    async def test_correct_types_are_accepted(self, arguments):
        outcome = await box(edit_tool()).dispatch(FakeToolCall("edit_file", arguments))

        assert not outcome.is_error

    async def test_every_problem_is_reported_at_once(self):
        """One mistake per call would spend a step on each, and steps are capped."""
        outcome = await box(edit_tool()).dispatch(
            FakeToolCall("edit_file", {"limit": "3", "mode": "x"})
        )

        assert "missing required argument(s): path" in outcome.content
        assert "unexpected argument(s): mode" in outcome.content
        assert "argument 'limit' must be integer, got string" in outcome.content

    async def test_arguments_that_are_not_an_object_are_an_error_outcome(self):
        tool = edit_tool()
        call = FakeToolCall("edit_file", ["path", "a.py"])  # type: ignore[arg-type]

        outcome = await box(tool).dispatch(call)

        assert outcome.is_error and "JSON object" in outcome.content
        assert tool.calls == []

    async def test_a_tool_error_becomes_an_error_outcome_with_its_message(self):
        async def handler(args):
            raise ToolError("edit_file: 'old' matches 3 places; include more context to make it unique")

        outcome = await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a.py"}))

        assert outcome.is_error
        assert outcome.content == "edit_file: 'old' matches 3 places; include more context to make it unique"

    async def test_a_tool_error_with_no_message_still_says_something(self):
        async def handler(args):
            raise ToolError()

        outcome = await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a.py"}))

        assert outcome.is_error and "edit_file" in outcome.content


class TestBugsPropagate:
    async def test_a_generic_exception_is_not_laundered_into_a_tool_result(self):
        async def handler(args):
            raise KeyError("a bug in the tool")

        with pytest.raises(KeyError, match="a bug in the tool"):
            await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a.py"}))

    async def test_a_subclass_of_toolerror_is_still_model_visible_but_its_parent_exceptions_are_not(self):
        class Refused(ToolError):
            pass

        async def handler(args):
            raise Refused("test files are read-only")

        outcome = await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a.py"}))

        assert outcome.is_error and outcome.content == "test files are read-only"

    async def test_cancellation_propagates(self):
        async def handler(args):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a.py"}))

    async def test_a_task_cancelled_mid_tool_actually_stops(self):
        started = asyncio.Event()

        async def handler(args):
            started.set()
            await asyncio.sleep(60)
            return ToolOutcome("never")

        task = asyncio.ensure_future(
            box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a.py"}))
        )
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_a_tool_that_returns_the_wrong_type_is_a_bug_not_a_result(self):
        async def handler(args):
            return "just a string"

        with pytest.raises(TypeError, match="not ToolOutcome"):
            await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a.py"}))


class TestTruncation:
    async def test_a_short_outcome_is_untouched(self):
        async def handler(args):
            return ToolOutcome("x" * 8000)

        outcome = await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a"}))

        assert outcome.content == "x" * 8000

    async def test_a_long_outcome_is_cut_with_a_trailer_saying_how_much(self):
        async def handler(args):
            return ToolOutcome("x" * 8000 + "tail" * 100)

        outcome = await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a"}))

        assert outcome.content == "x" * 8000 + "\n[truncated 400 chars]"

    async def test_the_cap_is_configurable(self):
        async def handler(args):
            return ToolOutcome("abcdefghij")

        outcome = await box(edit_tool(handler), max_output_chars=4).dispatch(
            FakeToolCall("edit_file", {"path": "a"})
        )

        assert outcome.content == "abcd\n[truncated 6 chars]"

    async def test_truncation_keeps_the_other_fields(self):
        async def handler(args):
            return ToolOutcome("x" * 20, submitted=True, summary="s")

        outcome = await box(FunctionTool("submit", handler), max_output_chars=5).dispatch(FakeToolCall("submit"))

        assert outcome.submitted and outcome.summary == "s" and not outcome.is_error

    async def test_an_error_message_is_capped_too(self):
        async def handler(args):
            raise ToolError("e" * 100)

        outcome = await box(edit_tool(handler), max_output_chars=10).dispatch(
            FakeToolCall("edit_file", {"path": "a"})
        )

        assert outcome.is_error and outcome.content == "e" * 10 + "\n[truncated 90 chars]"

    async def test_an_unknown_tool_message_is_capped_too(self):
        outcome = await box(max_output_chars=10).dispatch(FakeToolCall("x" * 100))

        assert outcome.is_error and "[truncated" in outcome.content

    async def test_the_unknown_tool_name_the_model_chose_cannot_flood_the_prompt(self):
        """The name is model output echoed straight back, so it is capped like any
        other content rather than trusted to be short."""
        outcome = await box().dispatch(FakeToolCall("n" * 100_000))

        assert len(outcome.content) < 8100
