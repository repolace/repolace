"""Delete task clones a dead worker left behind."""

import shutil
import tempfile
import time
from pathlib import Path

import structlog

log = structlog.get_logger()

WORKSPACE_PREFIX = "repolace-task-"


def remove_stale_workspaces(
    parent_dir: Path | None, older_than_seconds: float, *, now: float | None = None
) -> list[Path]:
    """Remove `repolace-task-*` directories under `parent_dir` not modified for `older_than_seconds`.

    Older than the task time limit means no live task can own it. A failure to delete one (files
    owned by a sandbox subuid) is logged and does not stop the sweep.
    """
    parent = Path(parent_dir) if parent_dir is not None else Path(tempfile.gettempdir())
    cutoff = (time.time() if now is None else now) - older_than_seconds
    removed: list[Path] = []
    if not parent.is_dir():
        return removed
    for entry in parent.glob(f"{WORKSPACE_PREFIX}*"):
        try:
            if entry.is_symlink() or not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry)
            removed.append(entry)
        except OSError as exc:
            log.error("worker.orphans.remove_failed", path=str(entry), error=str(exc))
    if removed:
        log.info("worker.orphans.removed", count=len(removed))
    return removed
