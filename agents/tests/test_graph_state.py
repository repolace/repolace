"""A frozen, all-defaulted dataclass is a valid LangGraph state.

The graph's design rests on this and it is not what the documentation leads with
(a `TypedDict`), so it is pinned before anything is built on it: if a LangGraph
upgrade stops constructing state as `schema(**input)`, or starts requiring
fields without defaults, this fails here rather than inside a graph run.
"""

import dataclasses
import typing

import pytest
from langgraph.graph import END, START, StateGraph

from repolace_agents.state import AgentState

pytestmark = pytest.mark.anyio


async def test_a_frozen_all_defaulted_state_round_trips_through_ainvoke():
    async def touch(state: AgentState):
        assert isinstance(state, AgentState)
        return {"steps": state.steps + 1}

    builder = StateGraph(AgentState)
    builder.add_node("touch", touch)
    builder.add_edge(START, "touch")
    builder.add_edge("touch", END)

    out = await builder.compile().ainvoke(AgentState())

    assert out == {**dataclasses.asdict(AgentState()), "steps": 1}


def test_every_field_has_a_default():
    """The first node entry is `schema(**input)`; a field with no default raises there."""
    for field in dataclasses.fields(AgentState):
        assert field.default is not dataclasses.MISSING or field.default_factory is not dataclasses.MISSING, field.name


def test_the_annotations_resolve_at_runtime():
    """LangGraph reads them to build its channels; a `TYPE_CHECKING`-only name would be an obscure error inside it."""
    assert set(typing.get_type_hints(AgentState)) == {f.name for f in dataclasses.fields(AgentState)}


def test_submitted_is_not_a_field():
    """`AgentResult.submitted` is derived from `stop_reason`; a second copy here could disagree with it."""
    assert "submitted" not in {f.name for f in dataclasses.fields(AgentState)}
