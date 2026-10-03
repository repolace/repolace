"""The run manifest: what a benchmark run PLANNED, written before any task runs.

`eval/runs/<eval_run_id>/manifest.json`. The report anchors its headline to it: the
expected grid is `instance_ids x range(runs_per_instance)`, and the database is
checked against that grid, not the other way round. Without a manifest the only
denominator is whatever rows happen to exist, which is chosen after the results --
an instance never enqueued, or dropped from a run, is invisible, and a half-finished
sweep reads as a finished one. The writer (the eval runner) and the reader (the
report) share this module, so the format has exactly one definition.

Keys, all required, no others::

    eval_run_id         str   the run's id; also the directory name
    created_at          str   ISO 8601 timestamp
    git_sha             str   the pipeline commit the run used
    model               str   the headline model id
    stage_models        map   stage name -> model id
    limits              map   limit name -> JSON scalar (budget, wall clock, ...)
    runs_per_instance   int   >= 1
    instance_ids        list  distinct, valid instance ids
    agent               str   "llm", "gold" or "stub"

Unknown and missing keys are errors, not defaults: a manifest that silently stopped
applying (a typo'd `instance_ids`) would turn the planned grid into the observed one
and the anchoring into theatre.

The loader raises `ManifestError`; `harness.report` re-raises it as `ReportError`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from repolace_shared.instances import InstanceError, instance_path
from repolace_shared.paths import PathEscapesRoot, resolve_within

MANIFEST_FILENAME = "manifest.json"
AGENTS = ("llm", "gold", "stub")

_KEYS = frozenset({
    "eval_run_id", "created_at", "git_sha", "model", "stage_models", "limits",
    "runs_per_instance", "instance_ids", "agent",
})
#: A run id is a directory name and appears in a file name; hold it to the same
#: shape an instance id has, minus nothing a shell would treat specially.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_JSON_SCALAR = (str, int, float, bool, type(None))


class ManifestError(ValueError):
    """A manifest is missing, unreadable, malformed, or names another run."""


@dataclass(frozen=True)
class RunManifest:
    eval_run_id: str
    created_at: str
    git_sha: str
    model: str
    stage_models: Mapping[str, str]
    limits: Mapping[str, Any]
    runs_per_instance: int
    instance_ids: tuple[str, ...]
    agent: str

    def planned_pairs(self) -> frozenset[tuple[str, int]]:
        """The expected grid: every `(instance_id, run_index)` the run was meant to produce."""
        return frozenset((i, r) for i in self.instance_ids for r in range(self.runs_per_instance))


def check_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise ManifestError(
            f"eval_run_id {run_id!r} must start with a letter or digit and use only letters, digits, '_', '.', '-'"
        )
    return run_id


def manifest_path(runs_dir: Path, eval_run_id: str) -> Path:
    """Where `eval_run_id`'s manifest is under `runs_dir`, confined to it (a symlink is refused)."""
    check_run_id(eval_run_id)
    try:
        return resolve_within(Path(runs_dir), f"{eval_run_id}/{MANIFEST_FILENAME}")
    except PathEscapesRoot as exc:
        raise ManifestError(f"{runs_dir}: {exc}") from exc


def _string_map(value: object, name: str, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ManifestError(f"{where}: {name} must be an object of strings to strings")
    return dict(value)


def _from_mapping(data: object, where: str) -> RunManifest:
    if not isinstance(data, dict):
        raise ManifestError(f"{where}: top level must be a JSON object, got {type(data).__name__}")
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise ManifestError(f"{where}: unknown key(s) {', '.join(unknown)}; known keys are {', '.join(sorted(_KEYS))}")
    missing = sorted(_KEYS - set(data))
    if missing:
        raise ManifestError(f"{where}: missing key(s) {', '.join(missing)}")

    try:
        run_id = check_run_id(data["eval_run_id"])
    except ManifestError as exc:
        raise ManifestError(f"{where}: {exc}") from exc

    created = data["created_at"]
    if not isinstance(created, str):
        raise ManifestError(f"{where}: created_at must be an ISO 8601 string")
    try:
        datetime.fromisoformat(created)
    except ValueError as exc:
        raise ManifestError(f"{where}: created_at {created!r} is not an ISO 8601 timestamp") from exc

    for name in ("git_sha", "model"):
        value = data[name]
        if not isinstance(value, str) or not value.strip() or re.search(r"\s", value):
            raise ManifestError(f"{where}: {name} must be a non-empty string without whitespace, got {value!r}")

    limits = data["limits"]
    if not isinstance(limits, dict) or not all(isinstance(k, str) for k in limits) or not all(
        isinstance(v, _JSON_SCALAR) for v in limits.values()
    ):
        raise ManifestError(f"{where}: limits must be an object of names to JSON scalars")

    runs = data["runs_per_instance"]
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise ManifestError(f"{where}: runs_per_instance must be an integer >= 1, got {runs!r}")

    ids = data["instance_ids"]
    if not isinstance(ids, list) or not ids:
        raise ManifestError(f"{where}: instance_ids must be a non-empty list")
    for instance_id in ids:
        try:
            # The same rule instance files and the pipeline hold an id to.
            instance_path(Path("."), instance_id)
        except InstanceError as exc:
            raise ManifestError(f"{where}: {exc}") from exc
    if len(set(ids)) != len(ids):
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        raise ManifestError(f"{where}: instance_ids repeats {', '.join(repeated[:5])}; a repeated id would double its weight")

    agent = data["agent"]
    if agent not in AGENTS:
        raise ManifestError(f"{where}: agent must be one of {', '.join(AGENTS)}, got {agent!r}")

    return RunManifest(
        eval_run_id=run_id, created_at=created, git_sha=data["git_sha"], model=data["model"],
        stage_models=_string_map(data["stage_models"], "stage_models", where), limits=dict(limits),
        runs_per_instance=runs, instance_ids=tuple(ids), agent=agent,
    )


def load_manifest(path: Path) -> RunManifest:
    """Read and validate one manifest file.

    The run id inside must equal the directory it sits in: a manifest copied under
    another run's directory would otherwise anchor that run to the wrong grid.
    """
    where = str(path)
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"{where}: cannot read: {type(exc).__name__}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ManifestError(f"{where}: not valid JSON: {type(exc).__name__}: {exc}") from exc
    manifest = _from_mapping(data, where)
    directory = Path(path).parent.name
    if manifest.eval_run_id != directory:
        raise ManifestError(
            f"{where}: eval_run_id is {manifest.eval_run_id!r} but the manifest sits in {directory!r}"
        )
    return manifest


def _document(manifest: RunManifest) -> dict[str, Any]:
    return {
        "eval_run_id": manifest.eval_run_id, "created_at": manifest.created_at, "git_sha": manifest.git_sha,
        "model": manifest.model, "stage_models": dict(manifest.stage_models), "limits": dict(manifest.limits),
        "runs_per_instance": manifest.runs_per_instance, "instance_ids": list(manifest.instance_ids),
        "agent": manifest.agent,
    }


def dump_manifest(manifest: RunManifest, runs_dir: Path) -> Path:
    """Write `<runs_dir>/<eval_run_id>/manifest.json` atomically and return its path.

    Validated by round-tripping the document through the loader's own checks first,
    so the writer cannot produce a file the reader would refuse.
    """
    document = _document(manifest)
    _from_mapping(json.loads(json.dumps(document)), str(runs_dir / manifest.eval_run_id))
    directory = Path(runs_dir) / check_run_id(manifest.eval_run_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / MANIFEST_FILENAME
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{MANIFEST_FILENAME}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target
