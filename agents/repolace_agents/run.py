"""The entry point the pipeline calls to run the real agent."""

import secrets

from repolace_agents.contracts import AgentDeps, AgentResult, AgentRunner
from repolace_agents.graph import run_graph


async def run_agent(deps: AgentDeps) -> AgentResult:
    """Attempt the issue in `deps.checkout` with the LLM graph. Contract: `AgentRunner`.

    The delimiter nonce is generated here, per call, and takes no parameter: one a
    caller could choose is one an attacker could predict. Needs `deps.llm` and
    `deps.tools` (the stub and gold runners are the ones that run without), and
    must be called inside the gateway's `task_scope`, which the pipeline binds.
    """
    if deps.resume_from is not None:
        # The one run that does not draw a fresh nonce: the snapshot's transcript already contains the
        # delimiters written with it, and rewriting them would change what the model has been told. The
        # nonce was never chosen by a caller; it is this same run's own, kept in its checkpoint.
        return await run_graph(deps, nonce=deps.resume_from.nonce)
    return await run_graph(deps, nonce=secrets.token_hex(8))


#: Fails type-checking, not runtime, if `run_agent` ever stops being a valid runner.
_runner: AgentRunner = run_agent
