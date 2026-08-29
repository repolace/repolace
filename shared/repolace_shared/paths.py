"""Confining a path to a tree, for the paths this system does not choose itself.

Three kinds of path reach the filesystem from somewhere the repository controls:
a `file_path` stored on a `CodeChunk` row, a name emitted by `git diff`, and --
once the real Editor exists -- a path the model wrote. All three are untrusted
input to a filesystem call, and `root / candidate` does not confine any of them.
"""

from pathlib import Path


class PathEscapesRoot(ValueError):
    """A path resolved outside the tree it was supposed to stay inside."""


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` inside ``root``, or refuse.

    ``root / candidate`` is not containment, and all three ways it fails are
    reachable here:

    * **pathlib discards ``root`` entirely when ``candidate`` is absolute.**
      ``Path("/repo") / "/etc/passwd"`` *is* ``/etc/passwd`` -- no exception, no
      ``..``, nothing that looks wrong in a log line.
    * ``..`` walks out lexically.
    * **a symlinked component walks out with no suspicious characters at all**,
      which is the one a string check cannot see. git tracks symlinks as mode
      ``120000`` and clones them back verbatim, so committing
      ``settings.py -> /home/worker/.env`` is the whole attack.

    So the check is on the *resolved* path, and a symlink is refused rather than
    followed. Following one that happens to land inside ``root`` today would
    still be trusting the repository to choose where a later read or write goes.

    Returns the resolved path, which is what callers should then open --
    resolving and then reopening by the original name would be a TOCTOU gap.
    """
    root = root.resolve()
    if Path(candidate).is_absolute():
        raise PathEscapesRoot(f"refusing an absolute path: {candidate}")

    target = root / candidate
    if target.is_symlink():
        raise PathEscapesRoot(f"refusing to follow a symlink: {candidate}")

    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        # Catches `..` and a symlinked *intermediate* component alike, because
        # resolve() walks the whole chain.
        raise PathEscapesRoot(f"path resolves outside the tree: {candidate}")
    return resolved
