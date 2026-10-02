"""Sentences the tools share with other parts of the agent, so they cannot drift apart.

`TESTS_NOT_SHOWN` is the one phrasing for "there are tests you cannot see". The graph's prompt
and its feedback say it with `repolace_agents.render.TESTS_NOT_SHOWN`; a tool description that
said the same thing another way ("the final check", "the full test suite") would tell the model
two different stories about what is hidden.

It is repeated here rather than imported because `render` imports `contracts`, which imports
this package: importing `render` from a tool is a circular import. The copy is held equal to
`render`'s by `agents/tests/test_tools_wording_matches_render.py`, so editing one and forgetting
the other fails a test instead of telling the model two stories.
"""

TESTS_NOT_SHOWN = "Some tests are not shown to you."
