"""Saving and loading a task's checkpoint: the one row in `task_checkpoints`, replaced as the task moves.

`CheckpointRecorder` is the `AgentDeps.checkpoint` the pipeline hands the graph. It does the part the graph
cannot: committing the working tree, taking the two patches (`TaskWorkspace.capture`), and writing them with
the snapshot, the active seconds and the run's provenance in one upsert. Patches are zlib-compressed.

**What is in a checkpoint is decided in `agents/checkpoint.py` and here, and nothing in it names the oracle**:
the snapshot carries a number, a sha and a flag for the last scored attempt, and the suite result a resume
needs comes back from `task_test_runs`.

`provenance_mismatch` is the guard a resume applies before it continues anything: a run continued under
different code, a different model or different limits would be measuring neither.
"""

import subprocess
import uuid
import zlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog
from repolace_agents.checkpoint import snapshot_to_json
from repolace_agents.contracts import AgentLimits, AgentSnapshot
from repolace_shared.db.models import TaskCheckpoint
from repolace_shared.git import TaskWorkspace
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

SessionFactory = async_sessionmaker[AsyncSession]

#: Patches are code diffs and compress well; level 6 is zlib's default trade.
_LEVEL = 6
#: A patch is compressed text, but a corrupt or hostile row must not inflate without bound.
MAX_PATCH_BYTES = 256 * 1024 * 1024


def pack(patch: bytes) -> bytes:
    return zlib.compress(patch, _LEVEL) if patch else b""


def unpack(blob: bytes) -> bytes:
    if not blob:
        return b""
    inflater = zlib.decompressobj()
    out = inflater.decompress(blob, MAX_PATCH_BYTES)
    if inflater.unconsumed_tail:
        raise ValueError(f"a checkpoint patch inflates past {MAX_PATCH_BYTES} bytes")
    return out


def code_version() -> str | None:
    """The git sha of the checkout this code runs from, or None if git cannot say (an installed copy, no git).

    Saved in each checkpoint so a resume refuses to continue a run under other code. Read once per process.
    """
    here = Path(__file__).resolve().parent
    try:
        done = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and len(sha) == 40 else None


def provenance_for(*, code: str | None, model: str | None, limits: AgentLimits, budget: Mapping[str, Any]) -> dict:
    """What identifies the run a checkpoint belongs to. `None` for a value the caller could not learn."""
    return {"code": code, "model": model, "limits": asdict(limits), "budget": dict(budget)}


def provenance_mismatch(stored: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    """A sentence naming what differs, or None. A key either side lacks (None) is not compared."""
    for key in sorted(set(stored) | set(current)):
        before, now = stored.get(key), current.get(key)
        if before is None or now is None:
            continue
        if before != now:
            return f"{key} was {before!r} when the task paused and is {now!r} now"
    return None


@dataclass(frozen=True)
class StoredCheckpoint:
    task_id: uuid.UUID
    seq: int
    agent_state: dict
    base_sha: str
    scored_patch: bytes
    delta_patch: bytes
    agent_seconds_used: float
    provenance: dict


async def save_checkpoint(
    session_factory: SessionFactory,
    task_id: uuid.UUID,
    *,
    agent_state: dict,
    base_sha: str,
    scored_patch: bytes,
    delta_patch: bytes,
    agent_seconds_used: float,
    provenance: dict,
) -> None:
    """Replace the task's checkpoint. Its own session, like every write that must outlive a failing task."""
    values = {
        "agent_state": agent_state,
        "base_sha": base_sha,
        "scored_patch": pack(scored_patch),
        "delta_patch": pack(delta_patch),
        "agent_seconds_used": agent_seconds_used,
        "provenance": provenance,
    }
    statement = insert(TaskCheckpoint).values(task_id=task_id, seq=1, **values)
    statement = statement.on_conflict_do_update(
        index_elements=[TaskCheckpoint.task_id],
        set_={**values, "seq": TaskCheckpoint.seq + 1, "updated_at": func.now()},
    )
    async with session_factory() as session:
        await session.execute(statement)
        await session.commit()


async def load_checkpoint(session_factory: SessionFactory, task_id: uuid.UUID) -> StoredCheckpoint | None:
    async with session_factory() as session:
        row = await session.get(TaskCheckpoint, task_id)
        if row is None:
            return None
        return StoredCheckpoint(
            task_id=row.task_id,
            seq=row.seq,
            agent_state=row.agent_state,
            base_sha=row.base_sha,
            scored_patch=unpack(row.scored_patch),
            delta_patch=unpack(row.delta_patch),
            agent_seconds_used=row.agent_seconds_used,
            provenance=row.provenance,
        )


class CheckpointRecorder:
    """The graph's `AgentDeps.checkpoint` for one task: commit, capture, save.

    `last_scored_sha` and `seconds_used` are callables, not values, because both change while the agent
    runs: the scorer's last record, and the budget's elapsed time (which already includes the seconds a
    resumed run carried in).
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        task_id: uuid.UUID,
        workspace: TaskWorkspace,
        *,
        last_scored_sha: Callable[[], str | None],
        seconds_used: Callable[[], float],
        provenance: dict,
    ) -> None:
        self._session_factory = session_factory
        self._task_id = task_id
        self._workspace = workspace
        self._last_scored_sha = last_scored_sha
        self._seconds_used = seconds_used
        self._provenance = provenance

    async def __call__(self, snapshot: AgentSnapshot) -> None:
        scored_patch, delta_patch = await self._workspace.capture(self._last_scored_sha())
        await save_checkpoint(
            self._session_factory,
            self._task_id,
            agent_state=snapshot_to_json(snapshot),
            base_sha=self._workspace.base_sha,
            scored_patch=scored_patch,
            delta_patch=delta_patch,
            agent_seconds_used=self._seconds_used(),
            provenance=self._provenance,
        )
        log.info(
            "pipeline.checkpoint.saved",
            attempt=snapshot.attempt,
            steps=snapshot.steps,
            patch_bytes=len(scored_patch) + len(delta_patch),
        )
