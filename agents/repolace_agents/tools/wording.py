"""Sentences the tools share with other parts of the agent, so they cannot drift apart.

`TESTS_NOT_SHOWN` is the one phrasing for "there are tests you cannot see". The graph's prompt
and its feedback say it with `repolace_agents.render.TESTS_NOT_SHOWN`; a tool description that
said the same thing another way ("the final check", "the full test suite") would tell the model
two different stories about what is hidden.

TODO: import it from `repolace_agents.render` instead of repeating it, once the graph stream's
`render` module is on this base. It is duplicated here because that module is not importable yet;
the sentence is copied exactly, and a test in the graph stream can hold the two equal.
"""

TESTS_NOT_SHOWN = "Some tests are not shown to you."
