"""The seam between the pipeline and the agent.

Everything the agent is given is in `AgentDeps`; everything it hands back is an
`AgentResult`. The pipeline builds the first and reads the second, and neither
side imports the other -- which is what lets `run_task` take any `AgentRunner`:
the real LLM graph, the stub editor that keeps the plumbing smoke test, and the
gold runner that applies the reference fix to validate an instance.

Imports stay cheap on purpose: no `litellm`, no gateway at runtime, no `rag`.
`LLMResponse` is only a return annotation, so it is imported for type-checking
alone, and `AgentDeps` takes an `LLMClientLike` rather than the gateway's
`LLMClient` so a test needs nothing heavier than a class with one method.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from verify.protocol import SuiteResult

from repolace_agents.tools.base import ToolBox

if TYPE_CHECKING:
    from repolace_gateway.client import LLMResponse


class LLMClientLike(Protocol):
    """The one method of `repolace_gateway.client.LLMClient` the agent uses.

    Every model call goes through it, so every call is priced, budgeted and
    recorded -- the agent has no other way to reach a model.
    """

    async def complete(
        self,
        stage: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        attempt: int | None = None,
        cache: bool | None = None,
        **kw: Any,
    ) -> LLMResponse: ...


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk, as the agent sees it. Not the ORM row."""

    file_path: str
    start_line: int
    end_line: int
    symbol: str
    chunk_type: str
    score: float
    #: A short excerpt, read from the checkout rather than stored in the index:
    #: the index holds the chunk as it was embedded, which may predate an edit.
    snippet: str


@dataclass(frozen=True)
class IssueContext:
    """The issue being fixed. Everything in it is UNTRUSTED.

    Anyone can file an issue on a public repository, so `title` and `body` reach
    a prompt only as delimited data, never as instruction -- and a benchmark
    instance's problem statement is no different, since it is GitHub text too.
    """

    number: int
    title: str
    body: str | None
    #: For a benchmark instance this is the *bench* repository's URL, never the
    #: upstream issue's: an upstream link in a PR would notify the real project.
    url: str
    #: Set only for a benchmark task.
    instance_id: str | None


@dataclass(frozen=True)
class AgentLimits:
    """The bounds on one agent run, apart from the gateway's cost budget."""

    max_attempts: int = 3
    max_steps_per_attempt: int = 40
    #: The issue text is cut here before it enters a prompt.
    max_issue_chars: int = 12_000
    #: How many lines of a retrieved chunk the prompt shows.
    max_context_snippet_lines: int = 60


@dataclass(frozen=True)
class AttemptRecord:
    """One scored attempt: what was committed, and what the suite said."""

    attempt: int
    commit_sha: str
    #: The **unfiltered** result, exactly as recorded in `task_test_runs`. The
    #: feedback shown to the model is derived from it by a filter that hides the
    #: oracle; this is what scoring reads.
    result: SuiteResult
    #: The sandbox or host failed, not the patch. Same meaning as the flag
    #: `score` takes: not a verdict on the agent, a signal to retry or exclude.
    infrastructure_error: bool


class StopReason(str, enum.Enum):
    """Why the agent loop ended. The values are what `tasks.agent_stop_reason` stores.

    Duplicated from `repolace_shared.db.models.AGENT_STOP_REASONS` rather than
    imported, so naming a reason does not pull SQLAlchemy and pgvector into
    agent code; a test holds the two equal. A reason added here without the
    database CHECK constraint fails on insert, which is the loud way to find out.
    """

    SUBMITTED = "submitted"
    STEP_CAP = "step_cap"
    BUDGET_USD = "budget_usd"
    BUDGET_CALLS = "budget_calls"
    BUDGET_WALL = "budget_wall"
    LLM_ERROR = "llm_error"
    #: The agent finished (or gave up) with no net change to the tree.
    NO_CHANGE = "no_change"
    #: Every attempt was used and the last scored one still had visible
    #: regressions or collection errors. Distinct from SUBMITTED so a report can
    #: tell "submitted clean" from "ran out of attempts still red" -- see
    #: `AgentResult` for exactly when the graph reports it.
    MAX_ATTEMPTS = "max_attempts"


@dataclass(frozen=True)
class AgentResult:
    stop_reason: StopReason
    #: The agent called `submit`. Equivalent to `stop_reason is SUBMITTED`, and
    #: enforced as such: two fields that can disagree are two columns in a report
    #: that can disagree.
    submitted: bool
    #: The agent's own account of its work. Untrusted model output wherever it is
    #: later shown to a human -- quoted, never rendered as the PR's own claim.
    summary: str | None
    attempts: int
    steps: int
    #: The last scored attempt, if any. None when the agent stopped before one.
    last_attempt: AttemptRecord | None

    def __post_init__(self) -> None:
        if self.submitted != (self.stop_reason is StopReason.SUBMITTED):
            raise ValueError(
                f"submitted={self.submitted} contradicts stop_reason={self.stop_reason.value}"
            )


@dataclass(frozen=True)
class AgentDeps:
    """Everything an agent run is given. Built by the pipeline, once per task."""

    #: None for the stub and gold runners, which never call a model.
    llm: LLMClientLike | None
    tools: ToolBox | None
    #: The working tree. Edits land here; the pipeline commits them.
    checkout: Path
    issue: IssueContext
    #: What retrieval found for the issue, best first.
    retrieved: tuple[SearchHit, ...]
    repo_overview: str
    #: The baseline suite result: the reference the feedback is a delta against.
    baseline: SuiteResult
    #: Every tracked file at the base commit. What `disqualifying_paths` needs to
    #: tell a shipped `django/test/client.py` from a real test.
    baseline_files: tuple[str, ...]
    #: Repo-relative paths of the benchmark overlay -- the hidden tests. Empty
    #: outside benchmark mode. Node ids and collect failures from these files are
    #: the oracle, so the feedback built from a result must exclude them; the
    #: files themselves are not in `checkout`.
    hidden_paths: frozenset[str]
    #: Commit the tree, run the **scored** suite, record the `task_test_runs` row,
    #: and return the attempt. None means there was no net change to run. The
    #: argument is the attempt number the agent is on (1..N).
    verify_attempt: Callable[[int], Awaitable[AttemptRecord | None]]
    #: Files changed since the base commit, repo-relative.
    changed_files: Callable[[], Awaitable[list[str]]]
    limits: AgentLimits = AgentLimits()


class AgentRunner(Protocol):
    """Anything that can attempt an issue: the LLM graph, the stub, the gold runner."""

    async def __call__(self, deps: AgentDeps) -> AgentResult: ...
