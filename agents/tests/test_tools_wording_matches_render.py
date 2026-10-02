"""The tools repeat the graph's "some tests are not shown" sentence; the two must stay equal.

`tools/wording.py` cannot import it from `render` (circular: `render` -> `contracts` ->
the tools package), so equality is pinned here instead.
"""

from repolace_agents import render
from repolace_agents.tools import wording


def test_the_tools_say_the_same_sentence_as_the_graph():
    assert wording.TESTS_NOT_SHOWN == render.TESTS_NOT_SHOWN
