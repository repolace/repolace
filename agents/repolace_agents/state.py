"""The graph's state: what flows between nodes, and nothing else.

Two rules shape it.

**Every field has a default.** LangGraph builds the state for each node as
`schema(**input)` -- a fresh instance per node, from whatever channels have been
written -- so a field without a default raises on the very first node entry,
before any of our code runs. It is frozen because a node that mutated its
argument in place would be changing a value LangGraph still holds; nodes return
partial-update dicts instead.

**Nothing here is a second source of truth.** `AgentResult.submitted` is derived
from `stop_reason`, so there is no `submitted` field to disagree with it, and
"how many attempts were scored" is `attempt` rather than a parallel counter.

Dependencies (the LLM client, the toolbox, `verify_attempt`) are deliberately not
here. State is what a checkpointer would persist, and a client or a database
callable is not that; they travel in the run context.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from repolace_agents.contracts import AttemptRecord, StopReason


@dataclass(frozen=True)
class AgentState:
    #: The whole conversation, system message first, in the provider's shape. A
    #: tuple so a node cannot append to the one LangGraph holds; the agent node
    #: builds a new one. Replaced wholesale on every update (no reducer): the
    #: elision pass rewrites earlier entries, which an append-only reducer cannot.
    messages: tuple[Mapping[str, Any], ...] = ()
    #: Attempts that were **scored** -- `verify_attempt` returned a record. This is
    #: `AgentResult.attempts`, and the attempt now running is `attempt + 1`. It is
    #: not "attempts begun": an attempt that ends on a budget stop is never scored
    #: and does not count, which is what keeps it equal to the number of
    #: `task_test_runs` rows with `attempt >= 1`.
    attempt: int = 0
    #: Model calls across the whole run.
    steps: int = 0
    #: Model calls in the attempt now running; reset on entry to each attempt.
    attempt_steps: int = 0
    #: The agent's own account, from the `submit` of the attempt that just ran.
    #: Reset on entry to each attempt, so a retry that never submits does not
    #: carry the previous attempt's claim into a result it no longer describes.
    summary: str | None = None
    #: The summary of the attempt that produced `last_attempt`, set when
    #: `verify_attempt` returns a record. **This, not `summary`, is what the result
    #: reports whenever something was scored**, because the pipeline pushes
    #: `last_attempt` and a summary describing some other patch would be quoted in
    #: its pull request: a later attempt that dies on a budget has an empty
    #: `summary` of its own, and one that finished clean without submitting must
    #: not inherit the claim of an attempt it repaired.
    scored_summary: str | None = None
    #: Why the agent loop ended. One value for the whole run -- see
    #: `AgentResult`. None until the agent node has run, and reset on entry to
    #: each attempt: a stale SUBMITTED from attempt 1 must not survive into an
    #: attempt 2 that ends some other way.
    stop_reason: StopReason | None = None
    #: The last attempt `verify_attempt` returned a record for. **Replaced only by
    #: a record, never cleared**: an attempt that stops early (budget, LLM error)
    #: or finds no net change keeps the previous one, because that is the only
    #: scored state the pipeline may act on.
    last_attempt: AttemptRecord | None = None
    #: Whether the *fresh* verification of the attempt just run was clean. None
    #: when there was no fresh record (verify skipped, or it found no change),
    #: which is what stops the router reading a stale answer from an earlier
    #: attempt. Reset on entry to each attempt.
    feedback_clean: bool | None = None
