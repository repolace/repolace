"""The two agents that need no model: the plumbing stub and the gold runner.

Both are `AgentRunner`s -- they take an `AgentDeps` and return an `AgentResult` -- so
`run_task` treats them exactly as it treats the LLM graph. That is the point of having
them. The stub keeps the smoke path (`--agent stub`) running through the *same* code the
real agent uses; the gold runner validates a benchmark instance by pushing the reference
fix through the **real** pipeline, instead of through a second script that would re-implement
clone, export, overlay, verify and score and then drift from them.

Neither calls a model, so neither has an `llm` or `tools`, and neither costs anything.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import structlog

from repolace_shared.instances import InstanceSpec

from repolace_agents.contracts import AgentDeps, AgentResult, StopReason
from repolace_agents.tools.base import ToolError
from repolace_agents.tools.paths import confine
from repolace_pipeline.edit import StubEditRequest, apply_stub_edit
from repolace_pipeline.pr import PLUMBING_SUMMARY_NOTE

log = structlog.get_logger()

#: The scored attempt both runners make. Neither ever makes a second.
ONLY_ATTEMPT = 1


@dataclass(frozen=True)
class StubAgent:
    """Writes the deterministic marker above the top retrieval hit, and verifies it.

    **It never submits.** It returns `STEP_CAP` -- the reason for "the agent ended without
    calling `submit`" -- so the product-mode gate withholds the PR unless the task opted in
    with `open_pr_on_failure`. That is what the smoke path has always required (the README says
    to expect the flag), and it is the right default: the edit is a comment, so the verdict is
    a clean "no regression", and a gate that read that as "ready" would open a pull request
    on a real repository for every plumbing run. `tasks.agent_stop_reason` therefore reads
    `step_cap` for these tasks, which is a label chosen for its effect on the gate, not a
    claim that a step cap was reached.

    Built per task, because the marker names the task, the base commit, the number of
    indexed chunks and the retrieval ranks, none of which are in `AgentDeps`.
    """

    request: StubEditRequest

    async def __call__(self, deps: AgentDeps) -> AgentResult:
        edited = await asyncio.to_thread(apply_stub_edit, deps.checkout, self.request)
        log.info("pipeline.edit.applied", file=str(edited.relative_to(deps.checkout.resolve())))

        record = await deps.verify_attempt(ONLY_ATTEMPT)
        if record is None:
            # Loud, as it always was: the stub's whole contract is that it produces a change,
            # so none means repolace or the repository is wrong, not that the agent had nothing.
            raise RuntimeError(
                "the stub edit produced no committable change; check whether the repo's "
                ".gitignore covers the edited file"
            )
        return AgentResult(
            stop_reason=StopReason.STEP_CAP,
            summary=PLUMBING_SUMMARY_NOTE,
            attempts=1,
            steps=1,
            last_attempt=record,
        )


class GoldPatchRefused(ValueError):
    """A reference-fix path is one the checkout may not be written through."""


def write_gold_files(checkout: Path, files: Mapping[str, str]) -> list[str]:
    """Write the reference fix's files into `checkout`. Returns the paths written.

    The keys came from an instance file, which is trusted operator data and validated on
    load -- and is confined again here, because a key is a path on the host filesystem and
    "trusted, but wrong" is already an arbitrary file write. Each goes through the agent's
    own read guard (`confine`): an absolute path, a `..`, a symlink anywhere in the path
    and anything in the `.git*` family are refused. `.git` is the one that matters most
    -- a hook written there runs on the host at the next commit.

    **All paths are checked before any is written**, so a refusal leaves the tree exactly
    as it was rather than half-patched.

    Written as bytes: the files are full text and must land exactly as stored, with no
    newline translation.
    """
    targets: list[tuple[str, Path]] = []
    for relative in files:
        try:
            targets.append((relative, confine(checkout, relative, write=False)))
        except ToolError as exc:
            raise GoldPatchRefused(f"refusing gold file {relative!r}: {exc}") from None

    for relative, target in targets:
        if target.is_dir():
            raise GoldPatchRefused(f"refusing gold file {relative!r}: it is a directory in the checkout")
    for relative, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[relative].encode("utf-8"))
    return [relative for relative, _ in targets]


@dataclass(frozen=True)
class GoldAgent:
    """Applies the instance's reference fix and verifies it. The ceiling the benchmark can reach.

    If the gold patch does not score PASSED on an instance, the instance is dropped, not
    fixed (the plan's checkpoint 6): that is what makes it a measurement of the agent
    rather than of how well an instance was prepared.

    It returns `SUBMITTED`, as an agent that believed it was done would, and the run is
    still held to the same gate -- `--agent gold` forces `open_pr=False`, so nothing is pushed.
    """

    instance: InstanceSpec

    async def __call__(self, deps: AgentDeps) -> AgentResult:
        written = await asyncio.to_thread(write_gold_files, deps.checkout, self.instance.gold_files)
        log.info("pipeline.gold.applied", files=len(written))

        record = await deps.verify_attempt(ONLY_ATTEMPT)
        if record is None:
            # The reference fix is identical to the base commit: nothing to verify.
            return AgentResult(
                stop_reason=StopReason.NO_CHANGE, summary=None, attempts=0, steps=1, last_attempt=None
            )
        return AgentResult(
            stop_reason=StopReason.SUBMITTED,
            summary="Applied the reference fix.",
            attempts=1,
            steps=1,
            last_attempt=record,
        )
