"""The real `AgentRunner`: the LLM graph, called the one way it may be called.

This is deliberately thin. Everything that makes an agent run correct -- the gateway
`task_scope` and budget it runs inside, the filtered tools and write guard it is handed, the
scorer cross-check on what it returns, the translation of a budget or provider stop that escapes
it -- is built by `run_task` and applies to *any* runner. What belongs here is only the choice of
entry point.

**It calls `run_agent`, never `run_graph`.** `run_agent` draws a fresh `secrets.token_hex(8)` nonce
for the prompt's delimiters on every call and takes no argument for it, because a nonce a caller
could choose is one an attacker could predict. `run_graph` takes the nonce as a parameter so a
test can fix it, which is exactly why production code must not reach for it. The call goes through
this module's global, so a test can replace `run_agent` and see that the pipeline uses it.
"""

from dataclasses import dataclass

from repolace_agents.contracts import AgentDeps, AgentResult
from repolace_agents.run import run_agent


@dataclass(frozen=True)
class LLMAgent:
    """The model-driven runner. A class, not a bare function, so the log names it (`runner=LLMAgent`)."""

    async def __call__(self, deps: AgentDeps) -> AgentResult:
        if deps.llm is None or deps.tools is None:
            # `run_task` gives an agent tools only when it was given a model. Refused here, before
            # the graph, with the cause named: the graph's own check would say what is missing,
            # and not that the caller forgot `llm=`.
            raise ValueError("LLMAgent needs a model client and the toolbox; pass llm= to run_task")
        return await run_agent(deps)
