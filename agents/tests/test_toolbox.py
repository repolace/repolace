"""`ToolBox`: the one place a tool call is checked and run.

The line these tests hold is between a model-visible failure and a bug. The first
becomes an `is_error` outcome the model can read and correct; the second must
propagate, because turning a bug into a polite tool result would let a broken
tool pass for a model that is simply bad at using it.
"""

import asyncio
import re

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


class TestUnenforcedKeywordsAreRefused:
    """A schema that advertises a bound nothing checks is sent to the provider as
    if it bound the model, and the tool author believes the argument is protected.
    So `ToolBox(...)` refuses it, loudly, when it is built."""

    @staticmethod
    def tool_with(prop, name="x", **kw) -> FunctionTool:
        return FunctionTool("t", parameters=object_schema({name: prop}, **kw))

    @pytest.mark.parametrize(
        ("keyword", "value"),
        [
            ("pattern", "^[a-z]+$"),
            ("format", "uri"),
            ("oneOf", [{"type": "string"}]),
            ("anyOf", [{"type": "string"}]),
            ("allOf", [{"type": "string"}]),
            ("not", {"type": "null"}),
            ("$ref", "#/definitions/x"),
            ("const", "x"),
            ("contentMediaType", "text/plain"),
        ],
    )
    def test_a_validation_keyword_it_does_not_enforce_is_refused(self, keyword, value):
        bad = self.tool_with({"type": "string", keyword: value})

        with pytest.raises(ValueError, match=rf"property 'x' uses '{re.escape(keyword)}'"):
            box(bad)

    def test_the_message_says_what_to_do_instead(self):
        with pytest.raises(ValueError, match="Check it inside the tool and drop it from the schema"):
            box(self.tool_with({"type": "string", "pattern": "^a"}))

    def test_unique_items_is_refused(self):
        with pytest.raises(ValueError, match="uniqueItems"):
            box(self.tool_with({"type": "array", "uniqueItems": True}))

    def test_nested_properties_are_refused(self):
        """Only one level is checked: an object's own fields would be advertised and not enforced."""
        nested = {"type": "object", "properties": {"inner": {"type": "string"}}, "required": ["inner"]}

        with pytest.raises(ValueError, match="'properties', 'required'"):
            box(self.tool_with(nested))

    def test_a_bare_object_property_is_fine(self):
        """Shape-unchecked, but it advertises nothing it does not check."""
        box(self.tool_with({"type": "object"}))

    def test_an_array_of_arrays_is_checked_only_as_far_as_each_element_being_an_array(self):
        box(self.tool_with({"type": "array", "items": {"type": "array"}}))
        with pytest.raises(ValueError, match="'items'"):
            box(self.tool_with({"type": "array", "items": {"type": "array", "items": {"type": "string"}}}))

    @pytest.mark.parametrize("union", [["string", "null"], ("string", "null"), ["string"]])
    def test_a_union_type_is_a_value_error_not_a_type_error(self, union):
        """`{"type": ["string", "null"]}` is valid JSON Schema, and used to raise a
        raw `TypeError: unhashable type: 'list'` from deep inside `_check_spec`."""
        with pytest.raises(ValueError, match="unions"):
            box(self.tool_with({"type": union}))

    def test_a_property_with_no_type_is_refused(self):
        """Nothing about it would be checked at all."""
        with pytest.raises(ValueError, match="declares no 'type'"):
            box(self.tool_with({"description": "anything"}))

    @pytest.mark.parametrize("bad_type", [7, {"a": 1}, "", "str"])
    def test_a_type_that_is_not_a_supported_name_is_refused(self, bad_type):
        with pytest.raises(ValueError, match="unsupported type"):
            box(self.tool_with({"type": bad_type}))

    @pytest.mark.parametrize(
        ("declared", "keyword", "value"),
        [
            ("integer", "minLength", 1),
            ("integer", "maxItems", 3),
            ("string", "minimum", 1),
            ("string", "maxItems", 3),
            ("array", "maxLength", 3),
            ("array", "enum", [[1]]),
            ("boolean", "minimum", 0),
            ("object", "enum", [{}]),
        ],
    )
    def test_a_real_keyword_on_the_wrong_type_is_refused(self, declared, keyword, value):
        """JSON Schema ignores it; here that would be a bound that is advertised and
        never applies."""
        with pytest.raises(ValueError, match=rf"'{keyword}', which the toolbox does not enforce for type '{declared}'"):
            box(self.tool_with({"type": declared, keyword: value}))

    def test_a_misspelt_keyword_is_refused_not_ignored(self):
        with pytest.raises(ValueError, match="maxLenght"):
            box(self.tool_with({"type": "string", "maxLenght": 5}))

    @pytest.mark.parametrize("keyword", ["oneOf", "anyOf", "allOf", "patternProperties", "minProperties", "$ref"])
    def test_a_top_level_keyword_it_does_not_enforce_is_refused(self, keyword):
        schema = {**object_schema({"x": {"type": "string"}}), keyword: []}

        with pytest.raises(ValueError, match=rf"the parameters object uses '{re.escape(keyword)}'"):
            box(FunctionTool("t", parameters=schema))

    @pytest.mark.parametrize("value", [{"type": "string"}, "false", 0, None])
    def test_additional_properties_must_be_a_boolean(self, value):
        schema = {**object_schema(), "additionalProperties": value}

        with pytest.raises(ValueError, match="additionalProperties"):
            box(FunctionTool("t", parameters=schema))

    def test_the_top_level_type_must_be_object(self):
        with pytest.raises(ValueError, match="type 'object'"):
            box(FunctionTool("t", parameters={**object_schema(), "type": "array"}))

    def test_required_must_be_a_list(self):
        """A bare string would iterate as characters and fail confusingly."""
        with pytest.raises(ValueError, match="'required' must be a list"):
            box(FunctionTool("t", parameters={**object_schema({"path": {"type": "string"}}), "required": "path"}))

    @pytest.mark.parametrize(
        "prop",
        [
            {"type": "string", "enum": []},
            {"type": "string", "enum": "ab"},
            {"type": "string", "enum": ["a", 1]},
            {"type": "integer", "enum": [1, "2"]},
            {"type": "integer", "minimum": "1"},
            {"type": "integer", "maximum": True},
            {"type": "number", "minimum": float("nan")},
            {"type": "string", "minLength": -1},
            {"type": "string", "maxLength": 2.5},
            {"type": "array", "maxItems": True},
            {"type": "integer", "minimum": 5, "maximum": 4},
            {"type": "string", "minLength": 9, "maxLength": 3},
            {"type": "array", "minItems": 4, "maxItems": 1},
        ],
    )
    def test_a_bound_that_is_itself_malformed_is_refused(self, prop):
        with pytest.raises(ValueError):
            box(self.tool_with(prop))

    def test_items_must_be_a_schema(self):
        with pytest.raises(ValueError, match="items"):
            box(self.tool_with({"type": "array", "items": "string"}))

    def test_a_keyword_inside_items_is_checked_like_one_on_a_property(self):
        with pytest.raises(ValueError, match="items uses 'pattern'"):
            box(self.tool_with({"type": "array", "items": {"type": "string", "pattern": "^a"}}))

    def test_every_enforced_keyword_is_accepted(self):
        box(
            FunctionTool(
                "t",
                parameters=object_schema(
                    {
                        "s": {"type": "string", "minLength": 1, "maxLength": 10, "enum": ["a", "b"], "description": "d"},
                        "i": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3, "title": "I"},
                        "n": {"type": "number", "minimum": 0.5, "maximum": 2.5, "examples": [1]},
                        "b": {"type": "boolean", "enum": [True]},
                        "a": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 5,
                            "items": {"type": "string", "minLength": 1, "maxLength": 300},
                        },
                        "o": {"type": "object", "description": "free-form"},
                    },
                    required=["s"],
                ),
            )
        )

    def test_the_error_names_the_tool(self):
        bad = FunctionTool("run_tests", parameters=object_schema({"x": {"type": "string", "pattern": "a"}}))

        with pytest.raises(ValueError, match="tool 'run_tests'"):
            box(bad)


BOUNDED = object_schema(
    {
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        "mode": {"type": "string", "enum": ["fast", "slow"]},
        "paths": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {"type": "string", "minLength": 1, "maxLength": 20},
        },
        "ratio": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string", "minLength": 2, "maxLength": 10},
        "level": {"type": "integer", "enum": [1, 2, 3]},
        "matrix": {"type": "array", "items": {"type": "array"}},
    },
    required=[],
)


class TestDeclaredBoundsAreEnforced:
    """The schema is sent to the provider as a promise to the model; `dispatch` is
    where it is made true. Each case here used to reach the tool verbatim."""

    @staticmethod
    async def call(**arguments):
        tool = FunctionTool("t", parameters=BOUNDED)
        outcome = await box(tool).dispatch(FakeToolCall("t", arguments))
        return outcome, tool

    async def test_the_hostile_inputs_from_the_review_are_all_refused_at_once(self):
        """`limit=10**9, mode='zzz', paths=[1, ['-x'], {'a': 1}]` was accepted whole."""
        outcome, tool = await self.call(limit=10**9, mode="zzz", paths=[1, ["-x"], {"a": 1}])

        assert outcome.is_error
        assert tool.calls == []
        assert "argument 'limit' must be at most 10, got 1000000000" in outcome.content
        assert "argument 'mode' must be one of 'fast', 'slow'; got 'zzz'" in outcome.content
        assert "argument 'paths'[0] must be string, got integer" in outcome.content
        assert "argument 'paths'[1] must be string, got array" in outcome.content
        assert "argument 'paths'[2] must be string, got object" in outcome.content

    @pytest.mark.parametrize("limit", [1, 5, 10])
    async def test_a_value_at_or_inside_the_bounds_is_accepted(self, limit):
        outcome, tool = await self.call(limit=limit)

        assert not outcome.is_error and len(tool.calls) == 1

    @pytest.mark.parametrize(
        ("limit", "message"),
        [(0, "must be at least 1, got 0"), (-5, "must be at least 1, got -5"), (11, "must be at most 10, got 11")],
    )
    async def test_an_integer_outside_the_bounds_is_refused(self, limit, message):
        outcome, tool = await self.call(limit=limit)

        assert outcome.is_error and f"argument 'limit' {message}" in outcome.content
        assert tool.calls == []

    @pytest.mark.parametrize("ratio", [0, 0.5, 1, 1.0])
    async def test_a_number_inside_the_bounds_is_accepted(self, ratio):
        assert not (await self.call(ratio=ratio))[0].is_error

    @pytest.mark.parametrize("ratio", [-0.1, 1.5, 10**9])
    async def test_a_number_outside_the_bounds_is_refused(self, ratio):
        outcome, _ = await self.call(ratio=ratio)

        assert outcome.is_error and "argument 'ratio' must be at" in outcome.content

    @pytest.mark.parametrize("ratio", [float("nan"), float("inf"), float("-inf")])
    async def test_a_non_finite_number_is_refused_because_it_sails_through_every_bound(self, ratio):
        """NaN compares false against both `minimum` and `maximum`."""
        outcome, tool = await self.call(ratio=ratio)

        assert outcome.is_error and "must be a finite number" in outcome.content
        assert tool.calls == []

    @pytest.mark.parametrize("mode", ["fast", "slow"])
    async def test_an_enum_member_is_accepted(self, mode):
        assert not (await self.call(mode=mode))[0].is_error

    @pytest.mark.parametrize("mode", ["zzz", "", "FAST", "fast "])
    async def test_anything_else_is_refused_with_the_options_listed(self, mode):
        outcome, _ = await self.call(mode=mode)

        assert outcome.is_error and "must be one of 'fast', 'slow'" in outcome.content

    async def test_an_integer_enum_is_checked_by_value(self):
        assert not (await self.call(level=2))[0].is_error
        outcome, _ = await self.call(level=4)
        assert outcome.is_error and "argument 'level' must be one of 1, 2, 3; got 4" in outcome.content

    async def test_a_huge_value_is_not_echoed_back_in_full(self):
        outcome, _ = await self.call(mode="z" * 100_000)

        assert outcome.is_error and len(outcome.content) < 400

    @pytest.mark.parametrize("summary", ["ab", "x" * 10])
    async def test_a_string_at_the_length_bounds_is_accepted(self, summary):
        assert not (await self.call(summary=summary))[0].is_error

    @pytest.mark.parametrize(
        ("summary", "message"),
        [("", "at least 2 characters, got 0"), ("a", "at least 2 characters, got 1"), ("x" * 11, "at most 10 characters, got 11")],
    )
    async def test_a_string_outside_the_length_bounds_is_refused(self, summary, message):
        outcome, _ = await self.call(summary=summary)

        assert outcome.is_error and f"argument 'summary' must be {message}" in outcome.content

    async def test_length_is_counted_in_characters_not_bytes(self):
        assert not (await self.call(summary="\u00e9" * 10))[0].is_error
        assert (await self.call(summary="\u00e9" * 11))[0].is_error

    async def test_an_array_at_the_item_bounds_is_accepted(self):
        assert not (await self.call(paths=["a"]))[0].is_error
        assert not (await self.call(paths=["a"] * 5))[0].is_error

    async def test_too_few_and_too_many_items_are_refused(self):
        few, _ = await self.call(paths=[])
        many, _ = await self.call(paths=["a"] * 6)

        assert few.is_error and "argument 'paths' must have at least 1 item(s), got 0" in few.content
        assert many.is_error and "argument 'paths' must have at most 5 item(s), got 6" in many.content

    async def test_the_element_that_is_wrong_is_named_by_position(self):
        outcome, _ = await self.call(paths=["ok", 3, "also ok"])

        assert outcome.is_error
        assert "argument 'paths'[1] must be string, got integer" in outcome.content
        assert "[0]" not in outcome.content and "[2]" not in outcome.content

    async def test_element_bounds_apply_to_each_element(self):
        outcome, _ = await self.call(paths=["ok", "", "x" * 21])

        assert "argument 'paths'[1] must be at least 1 characters, got 0" in outcome.content
        assert "argument 'paths'[2] must be at most 20 characters, got 21" in outcome.content

    async def test_a_null_element_is_refused_and_told_to_leave_it_out(self):
        outcome, _ = await self.call(paths=[None])

        assert "argument 'paths'[0] must be string, got null" in outcome.content

    async def test_the_elements_of_a_huge_bad_array_cost_a_few_sentences_not_the_budget(self):
        outcome, _ = await self.call(paths=list(range(10_000)))

        assert outcome.is_error
        # five named, then a count; plus the maxItems problem
        assert outcome.content.count("must be string") == 5
        assert "more problem(s) in argument 'paths'" in outcome.content
        assert len(outcome.content) < 1000

    async def test_an_array_of_arrays_is_checked_only_for_being_arrays(self):
        assert not (await self.call(matrix=[[1], ["a", None], []]))[0].is_error
        outcome, _ = await self.call(matrix=[[1], "row"])
        assert "argument 'matrix'[1] must be array, got string" in outcome.content

    async def test_a_tuple_is_an_array(self):
        assert not (await self.call(paths=("a", "b")))[0].is_error

    async def test_null_for_an_optional_argument_says_to_omit_it(self):
        """Models routinely send null for an optional argument; "got null" alone does
        not tell them to leave it out."""
        outcome, _ = await self.call(limit=None)

        assert "argument 'limit' must be integer, got null" in outcome.content
        assert "omit an optional argument rather than sending null" in outcome.content

    async def test_bounds_are_not_applied_to_a_value_of_the_wrong_type(self):
        """Only the type problem is reported; "got 'x', must be at most 10" would be nonsense."""
        outcome, _ = await self.call(limit="999")

        assert outcome.content.count("argument 'limit'") == 1
        assert "must be integer, got string" in outcome.content

    async def test_a_bool_is_not_inside_an_integer_range(self):
        outcome, _ = await self.call(limit=True)

        assert "must be integer, got boolean" in outcome.content

    async def test_every_problem_across_every_argument_is_reported_at_once(self):
        outcome, _ = await self.call(limit=0, mode="x", summary="", ratio=2, paths=[])

        for fragment in ("'limit' must be at least 1", "'mode' must be one of", "'summary' must be at least 2",
                         "'ratio' must be at most 1", "'paths' must have at least 1"):
            assert fragment in outcome.content

    async def test_the_tool_is_never_called_when_a_bound_fails(self):
        _, tool = await self.call(limit=11)

        assert tool.calls == []

    async def test_an_unknown_argument_is_not_bounds_checked_when_additional_properties_allows_it(self):
        tool = FunctionTool("t", parameters=object_schema({"a": {"type": "string"}}, additional_properties=True))

        outcome = await box(tool).dispatch(FakeToolCall("t", {"a": "x", "extra": 10**9}))

        assert not outcome.is_error


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
    """The cap INCLUDES the trailer: callers (a prompt budget, a column width) are
    promised `len(content) <= max_output_chars`, and it used to be cap + 22."""

    @staticmethod
    async def run(content, cap=64):
        async def handler(args):
            return ToolOutcome(content)

        return await box(edit_tool(handler), max_output_chars=cap).dispatch(
            FakeToolCall("edit_file", {"path": "a"})
        )

    async def test_a_short_outcome_is_untouched(self):
        async def handler(args):
            return ToolOutcome("x" * 8000)

        outcome = await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a"}))

        assert outcome.content == "x" * 8000

    async def test_exactly_at_the_cap_is_untouched_and_one_over_is_cut(self):
        assert (await self.run("x" * 64)).content == "x" * 64
        assert (await self.run("x" * 65)).content != "x" * 65

    async def test_a_long_outcome_is_cut_to_the_cap_trailer_included(self):
        async def handler(args):
            return ToolOutcome("x" * 8000 + "tail" * 100)

        outcome = await box(edit_tool(handler)).dispatch(FakeToolCall("edit_file", {"path": "a"}))

        assert len(outcome.content) <= 8000
        match = re.fullmatch(r"(x+)\n\[truncated (\d+) chars\]", outcome.content)
        assert match, outcome.content[-60:]
        # the number in the trailer is the number actually dropped
        assert int(match.group(2)) == 8400 - len(match.group(1))

    @pytest.mark.parametrize("cap", [64, 65, 100, 1000, 8000])
    @pytest.mark.parametrize("excess", [1, 2, 9, 10, 11, 99, 100, 101, 999, 1000, 12_345, 10**6])
    async def test_the_cap_holds_across_every_digit_boundary_of_the_dropped_count(self, cap, excess):
        """The trailer's own length depends on how many digits the dropped count
        has, so the kept length and the count have to be solved together."""
        content = "y" * (cap + excess)

        outcome = await self.run(content, cap)

        assert len(outcome.content) <= cap
        match = re.fullmatch(r"(y*)\n\[truncated (\d+) chars\]", outcome.content)
        assert match
        assert int(match.group(2)) == len(content) - len(match.group(1))

    async def test_it_keeps_every_character_it_can(self):
        """Not merely under the cap: within one character of it, or the trailer's
        digit count would be wasting content the model could have had."""
        outcome = await self.run("z" * 10_000, cap=500)

        assert 495 <= len(outcome.content) <= 500

    async def test_the_cap_is_configurable(self):
        outcome = await self.run("abcdefghij" * 20, cap=100)

        assert len(outcome.content) <= 100
        assert outcome.content.startswith("abcdefghij")
        assert "[truncated" in outcome.content

    async def test_multibyte_text_is_counted_in_characters_and_never_split(self):
        outcome = await self.run("\u00e9\u4e2d" * 200, cap=100)

        assert len(outcome.content) <= 100
        assert outcome.content.encode("utf-8")  # still valid text

    async def test_truncation_keeps_the_other_fields(self):
        async def handler(args):
            return ToolOutcome("x" * 200, submitted=True, summary="s")

        outcome = await box(FunctionTool("submit", handler), max_output_chars=64).dispatch(FakeToolCall("submit"))

        assert outcome.submitted and outcome.summary == "s" and not outcome.is_error

    async def test_an_error_message_is_capped_too(self):
        async def handler(args):
            raise ToolError("e" * 200)

        outcome = await box(edit_tool(handler), max_output_chars=64).dispatch(
            FakeToolCall("edit_file", {"path": "a"})
        )

        assert outcome.is_error and len(outcome.content) <= 64 and "[truncated" in outcome.content

    async def test_an_unknown_tool_message_is_capped_too(self):
        outcome = await box(max_output_chars=100).dispatch(FakeToolCall("x" * 1000))

        assert outcome.is_error and len(outcome.content) <= 100 and "[truncated" in outcome.content

    async def test_the_unknown_tool_name_the_model_chose_cannot_flood_the_prompt(self):
        """The name is model output echoed straight back, so it is capped like any
        other content rather than trusted to be short."""
        outcome = await box().dispatch(FakeToolCall("n" * 100_000))

        assert len(outcome.content) <= 8000

    @pytest.mark.parametrize("cap", [0, -1, -3, 1, 10, 63])
    def test_a_cap_too_small_to_hold_the_trailer_is_refused(self, cap):
        """Non-positive would slice from the end (a negative cap used to keep the
        *tail*); a tiny positive cap cannot fit the trailer that is counted inside it."""
        with pytest.raises(ValueError, match="max_output_chars"):
            ToolBox([], max_output_chars=cap)

    @pytest.mark.parametrize("cap", [8.5, "8000", None, True])
    def test_a_cap_that_is_not_an_integer_is_refused(self, cap):
        with pytest.raises(ValueError, match="max_output_chars"):
            ToolBox([], max_output_chars=cap)  # type: ignore[arg-type]

    def test_the_smallest_accepted_cap_is_accepted(self):
        assert ToolBox([], max_output_chars=64).max_output_chars == 64


class TestToolOutcomeInvariants:
    def test_an_error_cannot_also_be_a_submission(self):
        """The loop stops on `submitted`; an error that says so ends the run on a
        call that, by its own account, did not do what was asked."""
        with pytest.raises(ValueError, match="both an error and a submission"):
            ToolOutcome("nope", is_error=True, submitted=True)

    def test_each_alone_is_fine(self):
        assert ToolOutcome("x", is_error=True).is_error
        assert ToolOutcome("x", submitted=True).submitted

    async def test_a_tool_that_builds_the_contradiction_fails_loudly_at_the_tool(self):
        """Raised from the dataclass inside the tool: a bug, so it propagates."""

        async def handler(args):
            return ToolOutcome("x", is_error=True, submitted=True)

        with pytest.raises(ValueError):
            await box(FunctionTool("submit", handler)).dispatch(FakeToolCall("submit"))
