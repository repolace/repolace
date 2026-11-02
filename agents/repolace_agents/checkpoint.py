"""The JSON form of an `AgentSnapshot`, and the one place that reads one back.

A checkpoint is database content, so reading one is parsing untrusted input: every field is checked for
its type, an unknown version is refused, and the snapshot refuses a transcript that is not a list of
role-bearing mappings. Nothing here imports the oracle's types: `ScoredMark` carries a number, a sha and a
flag, and the unfiltered `AttemptRecord.result` is rebuilt by the pipeline from `task_test_runs`.

`FIELDS` is the complete list of what is written. A test holds it equal to the dataclass's fields, so a field
added to `AgentSnapshot` without a decision about the checkpoint fails there.
"""

from collections.abc import Mapping
from typing import Any

from repolace_agents.contracts import AgentSnapshot, ScoredMark

VERSION = 1
FIELDS = (
    "nonce",
    "messages",
    "attempt",
    "steps",
    "attempt_steps",
    "summary",
    "scored_summary",
    "feedback_clean",
    "last_scored",
)


class CheckpointFenced(Exception):
    """The writer refused to save: this process no longer owns the task.

    Raised by the pipeline's checkpoint writer when the row is not `running` under this process's claim (it was
    paused and claimed again, or finished, while this run was stalled). Unlike any other save failure the graph
    does **not** swallow it: a run that has lost its task must stop, or two runs would work on one task.
    """


class CheckpointError(ValueError):
    """A stored checkpoint that cannot be read back as one. The task is not resumed from it."""


def snapshot_to_json(snapshot: AgentSnapshot) -> dict[str, Any]:
    last = snapshot.last_scored
    return {
        "version": VERSION,
        "nonce": snapshot.nonce,
        "messages": [dict(message) for message in snapshot.messages],
        "attempt": snapshot.attempt,
        "steps": snapshot.steps,
        "attempt_steps": snapshot.attempt_steps,
        "summary": snapshot.summary,
        "scored_summary": snapshot.scored_summary,
        "feedback_clean": snapshot.feedback_clean,
        "last_scored": None
        if last is None
        else {
            "attempt": last.attempt,
            "commit_sha": last.commit_sha,
            "infrastructure_error": last.infrastructure_error,
        },
    }


def _need(data: Mapping[str, Any], key: str, kind: type, *, nullable: bool = False) -> Any:
    value = data.get(key)
    if value is None and nullable:
        return None
    # `bool` is an `int`: a count that is `True` is corrupt, not 1.
    if (isinstance(value, bool) and kind is int) or not isinstance(value, kind):
        raise CheckpointError(f"checkpoint field {key!r} is {type(value).__name__}, expected {kind.__name__}")
    return value


def snapshot_from_json(data: Any) -> AgentSnapshot:
    if not isinstance(data, Mapping):
        raise CheckpointError("a checkpoint is a JSON object")
    if data.get("version") != VERSION:
        raise CheckpointError(f"checkpoint version {data.get('version')!r} is not {VERSION}")
    messages = _need(data, "messages", list)
    for message in messages:
        if not isinstance(message, Mapping) or not isinstance(message.get("role"), str):
            raise CheckpointError("a checkpoint transcript entry has no role")
    last = _need(data, "last_scored", Mapping, nullable=True)
    scored = (
        None
        if last is None
        else ScoredMark(
            attempt=_need(last, "attempt", int),
            commit_sha=_need(last, "commit_sha", str),
            infrastructure_error=_need(last, "infrastructure_error", bool),
        )
    )
    return AgentSnapshot(
        nonce=_need(data, "nonce", str),
        messages=tuple(dict(message) for message in messages),
        attempt=_need(data, "attempt", int),
        steps=_need(data, "steps", int),
        attempt_steps=_need(data, "attempt_steps", int),
        summary=_need(data, "summary", str, nullable=True),
        scored_summary=_need(data, "scored_summary", str, nullable=True),
        feedback_clean=_need(data, "feedback_clean", bool, nullable=True),
        last_scored=scored,
    )
