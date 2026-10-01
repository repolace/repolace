"""The seam between the pipeline and the agent.

Everything the agent is given is in `AgentDeps`; everything it hands back is an
`AgentResult`. The pipeline builds the first and reads the second, and neither
side imports the other -- which is what lets `run_task` take any `AgentRunner`:
the real LLM graph, the stub editor that keeps the plumbing smoke test, and the
gold runner that applies the reference fix to validate an instance.

Imports stay cheap on purpose: no `litellm`, no `repolace_gateway.client` (which
pulls `litellm` and FastAPI), no `rag`/`retrieval`, no `torch` or
`sentence_transformers`. That is *not* "no gateway": `repolace_gateway.budget`
and `repolace_gateway.errors` are light and the graph legitimately imports them.
It is also not "no SQLAlchemy": `repolace_agents.tools.base` imports
`verify.scoring` for `is_protected_path`, and that loads
`repolace_shared.db.models` (SQLAlchemy and pgvector, but not torch).
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
    #: Set only for a benchmark task, and **`instance_id is not None` is what
    #: means benchmark mode.** Anything that decides whether to hide the oracle --
    #: the feedback filter above all -- must key on this and *fail closed*: when
    #: it is set, filter, even if `AgentDeps.hidden_paths` happens to be empty.
    #: Keying on `hidden_paths` alone makes an empty overlay silently turn the
    #: protection off, which is the failure that leaks the answer.
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
    #: The **unfiltered** result, exactly as recorded in `task_test_runs`, and
    #: **oracle-bearing**: in benchmark mode it contains the hidden tests' node
    #: ids, their outcomes and their collect failures. The feedback shown to the
    #: model is derived from it by a filter that hides all of that; this is what
    #: scoring reads. **Nothing outside that filter may interpolate it into a
    #: prompt, a tool result or a log line the model can later read** -- not
    #: `result.failed`, not `result.stdout_tail`, not `repr(result)`.
    result: SuiteResult
    #: The sandbox or host failed, not the patch. Same meaning as the flag
    #: `score` takes: not a verdict on the agent, a signal to retry or exclude.
    infrastructure_error: bool


class StopReason(str, enum.Enum):
    """Why the agent loop ended. The values are what `tasks.agent_stop_reason` stores.

    Duplicated from `repolace_shared.db.models.AGENT_STOP_REASONS` rather than
    imported, so this module can name a reason without depending on the ORM; a
    test holds the two equal. A reason added here without the database CHECK
    constraint fails on insert, which is the loud way to find out.
    """

    SUBMITTED = "submitted"
    STEP_CAP = "step_cap"
    BUDGET_USD = "budget_usd"
    BUDGET_CALLS = "budget_calls"
    BUDGET_WALL = "budget_wall"
    LLM_ERROR = "llm_error"
    #: The agent finished (or gave up) with no net change to the tree.
    NO_CHANGE = "no_change"
    #: Every attempt was used (`attempts == limits.max_attempts`) and the last
    #: scored one still had *visible* regressions or collection errors -- visible
    #: meaning in the feedback the model was shown, not the hidden oracle.
    #: Distinct from SUBMITTED so a report can tell "submitted clean" from "ran
    #: out of attempts still red". A clean last attempt does **not** get this
    #: reason: it keeps the agent's own (SUBMITTED, or STEP_CAP if it never
    #: submitted).
    MAX_ATTEMPTS = "max_attempts"


@dataclass(frozen=True)
class AgentResult:
    """What an agent run hands back to the pipeline.

    **`last_attempt` is the only scored state.** The pipeline scores, records and
    opens a PR from `last_attempt` and from nothing else, so everything below
    follows from that:

    * When the agent stops *without a fresh scored attempt* -- a budget or LLM
      stop, the step cap -- `HEAD` may be ahead of `last_attempt.commit_sha`
      (`run_python` and `run_tests` leave checkpoint commits that were never
      scored), or `last_attempt` may be None. **The pipeline MUST
      `workspace.rewind_to(last_attempt.commit_sha)` whenever `HEAD` differs,
      before computing `changed_files`, `review_diff`, the squash or the push**:
      otherwise the PR would carry edits that no suite ever ran against, under a
      verdict that belongs to an earlier commit.
    * With `last_attempt is None` the outcome is FAILED ("no scored attempt:
      <stop_reason>"), the PR gate sees no change, and no PR opens unless
      `open_pr_on_failure`.
    * A later attempt that stops early keeps the PREVIOUS `last_attempt`: it is
      replaced only when `verify_attempt` returns a record, never cleared.

    **`attempts` is the number of `verify_attempt` calls that returned a record**
    -- equivalently, the number of `task_test_runs` rows with `attempt >= 1` --
    not the number of attempts begun. `retry_count = max(0, attempts - 1)`.

    **`stop_reason` is one value for the whole run.** When every attempt was used
    and the last scored one still has visible regressions or collection errors it
    is `MAX_ATTEMPTS`; a clean last attempt keeps the agent's own reason
    (SUBMITTED, or STEP_CAP if it never submitted).

    `submitted` is derived from `stop_reason`, not stored beside it. It used to be
    a field that `__post_init__` forced to agree, and that raised on perfectly
    legitimate sequences -- an agent that submits and then finds no net change
    (NO_CHANGE), or one that submitted, retried, and later hit a budget -- which
    turned a normal outcome into a FAILED task. A second source of truth is
    exactly that: two columns in a report that can disagree.
    """

    stop_reason: StopReason
    #: The agent's own account of its work. Untrusted model output wherever it is
    #: later shown to a human -- quoted, never rendered as the PR's own claim.
    summary: str | None
    #: `verify_attempt` calls that returned a record. See the class docstring.
    attempts: int
    steps: int
    #: The last *scored* attempt, or None if the agent stopped before one. The
    #: only state the pipeline scores; see the class docstring for what that
    #: obliges it to do when `HEAD` has moved past it.
    last_attempt: AttemptRecord | None

    def __post_init__(self) -> None:
        # A `str` subclass, so a plain "submitted" would otherwise pass every
        # comparison here and then fail on `.value` far from where it was built.
        if not isinstance(self.stop_reason, StopReason):
            raise TypeError(
                f"stop_reason must be a StopReason, got {type(self.stop_reason).__name__} "
                f"{self.stop_reason!r}; use StopReason(...) so a typo fails here rather than "
                f"in a report column"
            )

    @property
    def submitted(self) -> bool:
        """The agent called `submit`. Derived: `stop_reason is SUBMITTED`."""
        return self.stop_reason is StopReason.SUBMITTED


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
    #: **Unfiltered and oracle-bearing**, like `AttemptRecord.result`: in
    #: benchmark mode the baseline run includes the overlay, so `baseline.failed`
    #: *is* the hidden fail-to-pass set. Only the feedback filter may read it for
    #: anything the model sees; nothing else may interpolate it into a prompt.
    baseline: SuiteResult
    #: Every tracked file at the base commit. What `disqualifying_paths` needs to
    #: tell a shipped `django/test/client.py` from a real test.
    baseline_files: tuple[str, ...]
    #: Repo-relative paths of the benchmark overlay -- the hidden tests. Empty
    #: outside benchmark mode. Node ids and collect failures from these files are
    #: the oracle, so the feedback built from a result must exclude them; the
    #: files themselves are not in `checkout`. **Do not infer benchmark mode from
    #: this being non-empty**: `issue.instance_id is not None` is that signal, and
    #: the filter must fail closed on it.
    hidden_paths: frozenset[str]
    #: Commit the tree, run the **scored** suite, record the `task_test_runs` row,
    #: and return the attempt. None means there was no net change to run. The
    #: argument is the attempt number the agent is on (1..N). A returned record
    #: becomes `AgentResult.last_attempt` and counts toward `attempts`; None does
    #: neither.
    verify_attempt: Callable[[int], Awaitable[AttemptRecord | None]]
    #: Files changed since the base commit, repo-relative.
    changed_files: Callable[[], Awaitable[list[str]]]
    limits: AgentLimits = AgentLimits()


class AgentRunner(Protocol):
    """Anything that can attempt an issue: the LLM graph, the stub, the gold runner."""

    async def __call__(self, deps: AgentDeps) -> AgentResult: ...
