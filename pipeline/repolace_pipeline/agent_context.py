"""What the pipeline hands the agent's tools, and what it holds back.

The tools (`repolace_agents.tools`) never see a session, a `Verifier` or a git
repository: the pipeline closes over those and passes functions, so a tool can do
only what its function does. This module builds the three functions that need a
decision, and the one callable that decides what the agent may write.

**Everything here exists because a probe, a refusal or a search result is a way
for the agent to learn something it must not.** In benchmark mode the hidden
tests are the answer key, and the agent has five ways to ask a question of the
system: it can run tests, it can write files, it can search, it can read, and it
can run a script. Reading and scripts are bounded elsewhere (the tools' read
guard, the sandbox); the other three are decided here.

* A **probe** never includes the overlay (`Verifier.run_subset` guarantees it), so
  its result cannot name a hidden test today. It is filtered anyway
  (`visible_probe`): the tool renders ids and output verbatim, and a future change
  to the verifier that let one hidden path through would otherwise reach the model
  with nothing between.
* A **write refusal** says "that file is read-only", which for a path the agent has
  never seen is a statement that the file exists. `protected_check` is therefore
  built from the baseline *minus* the overlay's paths, so a hidden test file is as
  writable as any other path and the existence of one is not leaked. The cost is
  real and accepted: an agent that writes over a hidden collected file wastes an
  attempt, because the scorer (which reads the unfiltered baseline) disqualifies it.
* A **search** reads the working tree through the same guard as `read_file`.
"""

import asyncio
import posixpath
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repolace_shared.git import TaskWorkspace
from retrieval.retrieve import hybrid_search
from verify.config import WORKDIR
from verify.protocol import ScriptResult, SuiteResult
from verify.scoring import is_protected_path
from verify.stage import Verifier

from repolace_agents.contracts import SearchHit
from repolace_agents.tools.base import ToolContext, ToolLimits
from repolace_pipeline.context import RetrievedChunk, search_hit

log = structlog.get_logger()

#: The message prefixes `verify.errors` gives each kind of sandbox failure, which the
#: tools use to choose a sentence for the model. Only these survive a probe's `error`:
#: the rest of the text is a build-log tail or a command line, which came from a
#: process that ran repository code.
_ERROR_PREFIXES = (
    "container runtime unavailable",
    "environment build failed",
    "sandbox exceeded",
)
_UNCATEGORISED_ERROR = "sandbox run failed"


# --- the hidden-path rule ----------------------------------------------------
#
# The same rule as `repolace_agents.feedback` (which keeps its copy private): an id is
# hidden when its path part *is* a hidden path or lies *under* one. Restated here, not
# imported, because it is one `posixpath` call and a private import across packages is
# the worse coupling -- and pinned by a test that compares the two over a grid of paths,
# so a change to either fails loudly rather than drifting.


def _normalise(path: str) -> str:
    """One spelling for a path, so `./a//b.py` and `a/b.py` are the same one.

    Leading slashes are dropped too: an id that arrived absolute must still match
    the repo-relative path it names. That over-hides rather than under-hides, which
    is the safe direction for a filter.
    """
    return posixpath.normpath(path.replace("\\", "/")).lstrip("/") or "."


def _hidden_set(hidden_paths: Collection[str]) -> frozenset[str]:
    return frozenset(_normalise(path) for path in hidden_paths)


def _path_is_hidden(path: str, hidden: frozenset[str]) -> bool:
    if not hidden:
        return False
    normalised = _normalise(path)
    if normalised in hidden:
        return True
    parts = normalised.split("/")
    return any("/".join(parts[:i]) in hidden for i in range(1, len(parts)))


def _id_is_hidden(nodeid: str, hidden: frozenset[str]) -> bool:
    return _path_is_hidden(nodeid.split("::", 1)[0], hidden)


def _in_repo(path: str) -> str:
    """A path the sandbox reported, relative to the repository root.

    pytest reports a conftest by absolute container path (`/repo/tests/conftest.py`)
    and a test by a root-relative one. Without this a hidden conftest would be
    compared as `repo/tests/conftest.py` and never match.
    """
    return path.removeprefix(f"{WORKDIR}/")


# --- probes ------------------------------------------------------------------


def _error_category(error: str) -> str:
    lowered = error.lstrip().lower()
    for prefix in _ERROR_PREFIXES:
        if lowered.startswith(prefix):
            return prefix
    return _UNCATEGORISED_ERROR


def visible_probe(result: SuiteResult, hidden_paths: Collection[str]) -> SuiteResult:
    """A probe's result with nothing from a hidden path in it, built field by field.

    Starts from an empty `SuiteResult` rather than copying one, so a field added to
    `SuiteResult` later is *dropped* until someone decides it is safe, not forwarded
    until someone notices. The fingerprint is not carried: a probe is not compared
    with a baseline.

    * ids, collection failures, collected files and conftests under a hidden path go;
    * `error` is reduced to its category (see `_ERROR_PREFIXES`);
    * the output tail is kept -- a traceback is what a probe is *for* -- unless it
      names a hidden path, in which case all of it goes. A probe cannot produce such a
      tail today, because the overlay is not in its export; if one ever appears the
      safe reading is that the tail came from a run that had the answer key, and a
      partly redacted traceback is not worth the risk of a path spelled another way.
    """
    hidden = _hidden_set(hidden_paths)

    def keep(ids: Sequence[str]) -> tuple[str, ...]:
        return tuple(i for i in ids if not _id_is_hidden(i, hidden))

    tail = result.stdout_tail
    if hidden and any(path in tail for path in hidden):
        log.warning("pipeline.probe.tail_names_hidden_path")
        tail = ""
    return SuiteResult(
        passed=keep(result.passed),
        failed=keep(result.failed),
        skipped=keep(result.skipped),
        xfailed=keep(result.xfailed),
        did_not_run=keep(result.did_not_run),
        collect_failures=keep(result.collect_failures),
        collected_files=tuple(p for p in result.collected_files if not _path_is_hidden(_in_repo(p), hidden)),
        conftests=tuple(p for p in result.conftests if not _path_is_hidden(_in_repo(p), hidden)),
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
        stdout_tail=tail,
        error=_error_category(result.error) if result.error else None,
    )


# --- writes ------------------------------------------------------------------


def protected_check(baseline: SuiteResult, hidden_paths: Collection[str]) -> Callable[[str], bool]:
    """`ToolContext.is_protected`, built from the baseline without the hidden overlay.

    Aware of what pytest collected at the base commit -- without which a test file
    collected through a custom `python_files` is editable and the scorer then discards
    the whole patch -- but only of the *visible* part of it. See the module docstring
    for why: built from the raw baseline, which in benchmark mode includes the overlay,
    a refusal becomes an existence oracle for a hidden path.
    """
    hidden = _hidden_set(hidden_paths)
    files = tuple(p for p in baseline.collected_files if not _path_is_hidden(_in_repo(p), hidden))
    conftests = tuple(p for p in baseline.conftests if not _path_is_hidden(_in_repo(p), hidden))

    def is_protected(path: str) -> bool:
        return is_protected_path(path, collected_files=files, conftests=conftests)

    return is_protected


# --- search ------------------------------------------------------------------


def make_search(
    session_factory: async_sessionmaker[AsyncSession],
    repo_id: uuid.UUID,
    checkout: Path,
    *,
    strategy: str,
    max_lines: int,
) -> Callable[[str, int], Awaitable[Sequence[SearchHit]]]:
    """`ToolContext.search`: retrieval on a short-lived session of its own, per call.

    A session shared with the pipeline's own writes would be held open across a
    minutes-long agent loop (idle in a transaction, holding a snapshot), so each
    search opens one and closes it before returning. The chunks are projected to
    plain data *inside* that session -- touching a deferred column afterwards is the
    `MissingGreenlet` this codebase has already met once -- and the snippets are read
    outside it, on a thread, since file reads must not stall the event loop.
    """

    async def search(query: str, limit: int) -> Sequence[SearchHit]:
        if not query.strip():
            return ()
        async with session_factory() as db:
            results = await hybrid_search(db, repo_id, query, limit=limit, query_strategy=strategy)
            chunks = [RetrievedChunk.from_result(result) for result in results]
        return await asyncio.to_thread(lambda: [search_hit(c, checkout, max_lines) for c in chunks])

    return search


# --- the context -------------------------------------------------------------


def build_tool_context(
    *,
    workspace: TaskWorkspace,
    verifier: Verifier,
    search: Callable[[str, int], Awaitable[Sequence[SearchHit]]],
    hidden_paths: Collection[str],
    baseline: SuiteResult,
    limits: ToolLimits = ToolLimits(),
) -> ToolContext:
    """The agent's `ToolContext`, built from the task's own resources.

    * `checkpoint` is `workspace.record_attempt`: `run_python` and `run_tests`
      commit the tree before handing it to the sandbox, because the export refuses a
      tree that differs from HEAD. Those commits are not scored, which is why the
      pipeline rewinds to the last scored attempt before it reads the result.
    * `run_subset` applies `limits.max_probe_seconds` itself -- the callable the tool
      receives takes only the targets, so the bound has to be in the closure -- and
      returns the *filtered* result.
    * `run_script` passes the tool's own timeout straight through.
    """
    hidden = frozenset(hidden_paths)

    async def run_subset(targets: Sequence[str]) -> SuiteResult:
        result = await verifier.run_subset(workspace, targets, timeout_seconds=limits.max_probe_seconds)
        return visible_probe(result, hidden)

    async def run_script(code: str, timeout_seconds: float) -> ScriptResult:
        return await verifier.run_script(workspace, code, timeout_seconds=timeout_seconds)

    return ToolContext(
        checkout=workspace.path,
        checkpoint=workspace.record_attempt,
        search=search,
        run_subset=run_subset,
        run_script=run_script,
        limits=limits,
        is_protected=protected_check(baseline, hidden),
    )
