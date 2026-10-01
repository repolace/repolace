"""The entry point the pipeline calls to run the real agent."""

from repolace_agents.contracts import AgentDeps, AgentResult, AgentRunner


async def run_agent(deps: AgentDeps) -> AgentResult:
    """Attempt the issue in `deps.checkout` with the LLM graph. Contract: `AgentRunner`."""
    raise NotImplementedError("run_agent lands in stream F: graph")


#: Fails type-checking, not runtime, if `run_agent` ever stops being a valid runner.
_runner: AgentRunner = run_agent
