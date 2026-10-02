"""Putting a benchmark instance's hidden tests into an export.

A benchmark instance carries the test files its fixing PR added or changed. The
agent never sees them (they are the oracle: fail-to-pass is defined by them), and
the image never contains them (the cache is shared, so a layer holding them would
leak them to every later build). They are laid over the *export* at run time
instead -- after the environment is built, outside the cache key, and for scored
runs only.

Values are **complete file bytes**, not a diff. SWE-bench ships `test_patch` as a
unified diff; instance preparation applies it once, on the host, and stores the
resulting files, so nothing at run time has to parse or trust a patch.
"""

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from repolace_shared.paths import PathEscapesRoot, resolve_within

_FILE_MODE = 0o644


class OverlayError(ValueError):
    """An overlay entry that must not be written. Always names the offending path.

    Raised before anything touches the disk, so a refusal never leaves a
    half-applied overlay behind.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        super().__init__(f"overlay path {path!r}: {reason}")


def _check_key_string(key: object, data: object) -> list[str]:
    """The checks that need no filesystem: validate one entry, return its components.

    The keys come from instance data -- trusted operator input -- but they end up
    as paths on the host, so they are treated as if they were not. Only a
    *canonical* relative POSIX path is accepted: `./a`, `a//b` and `a/` all
    normalise to something else under pathlib, and accepting a spelling the code
    then reads differently is how a check and a use come to disagree.
    """
    if not isinstance(key, str):
        raise OverlayError(repr(key), "must be a string")
    if not key:
        raise OverlayError(key, "must be a non-empty string")
    if not isinstance(data, (bytes, bytearray)):
        # A str would be encoded by whichever default is in force, and the file
        # on disk would no longer be the bytes the instance recorded.
        raise OverlayError(key, "content must be bytes")
    if "\x00" in key or "\\" in key:
        raise OverlayError(key, "contains a NUL or a backslash")
    pure = PurePosixPath(key)
    if str(pure) != key:
        raise OverlayError(key, "is not a canonical relative path")
    if pure.is_absolute():
        raise OverlayError(key, "is absolute")
    parts = list(pure.parts)
    if not parts:
        raise OverlayError(key, "does not name a file")
    if ".." in parts:
        raise OverlayError(key, "contains a '..' component")
    # Case-folded: `.GIT` is `.git` on a case-insensitive filesystem. Refused at
    # any depth although the export has no `.git` -- an entry is executable
    # configuration to the host's git, and an overlay path is operator data that
    # lands on the host filesystem.
    if any(part.lower() == ".git" for part in parts):
        raise OverlayError(key, "has a '.git' component")
    return parts


def _check_nesting(parts_by_key: Mapping[str, list[str]]) -> None:
    """`a` and `a/b` together can never both be written, and neither exists on disk
    yet for a per-key check to trip over."""
    keys = set(parts_by_key)
    for key, parts in parts_by_key.items():
        for end in range(1, len(parts)):
            if "/".join(parts[:end]) in keys:
                raise OverlayError(key, f"{'/'.join(parts[:end])!r} is also an overlay file")


def validate_overlay_paths(files: Mapping[str, bytes]) -> None:
    """Refuse a malformed overlay without touching the filesystem. Raises `OverlayError`.

    The string-level half of `apply_overlay`'s checks, callable the moment the
    overlay is known. `Verifier` calls it at construction so a bad key fails
    *before* the environment build -- which can take forty minutes -- rather than
    after it, when the verifier would already be prepared and a retried baseline
    would then be refused as a second one. What needs the filesystem (a symlinked
    parent, an existing directory) can only be checked against an export, so
    `apply_overlay` still does that.
    """
    _check_nesting({key: _check_key_string(key, data) for key, data in files.items()})


def _check_key(source_dir: Path, key: object, data: object) -> tuple[list[str], Path]:
    """Validate one entry against the export too; return its components and resolved target."""
    parts = _check_key_string(key, data)
    assert isinstance(key, str)

    # Before `resolve_within`, so a symlinked parent is reported as what it is
    # rather than as the escape it happens to cause.
    current = source_dir
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            # Not caught by `resolve_within` when it lands back inside the tree
            # (`tests -> src`), and following it would still be letting the
            # export choose where a write goes.
            raise OverlayError(key, f"parent {part!r} is a symlink")
        if current.exists() and not current.is_dir():
            raise OverlayError(key, f"parent {part!r} is a file, not a directory")

    try:
        # Confinement as the rest of the codebase does it, and the *returned*
        # path is what gets opened: resolving and then reopening by the original
        # name would be a gap. Redundant with the checks above for a string key,
        # deliberately -- this is the function every other path in this system
        # goes through, and a future change to the string rules must not be the
        # only thing standing between a key and `/`.
        target = resolve_within(source_dir, key)
    except PathEscapesRoot as exc:
        raise OverlayError(key, str(exc)) from exc

    if target.is_dir():
        raise OverlayError(key, "is an existing directory")
    return parts, target


def _make_directories(source_dir: Path, parts: list[str], dir_mode: int) -> None:
    """Create the missing parents, each chmodded rather than trusting `mkdir`'s mode."""
    current = source_dir
    for part in parts[:-1]:
        current = current / part
        if current.exists():
            continue
        current.mkdir()
        os.chmod(current, dir_mode)


def _write_file(path: Path, data: bytes) -> None:
    # O_NOFOLLOW so that a symlink appearing at the final component between the
    # check and this write is an error rather than a write through it; O_TRUNC so
    # an existing file is replaced in full, never appended to.
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, _FILE_MODE
    )
    try:
        _write_all(descriptor, data)
        # Not the open() mode: it is masked by the umask, and an unreadable file
        # is an unscoreable run the sandbox's unprivileged uid cannot open.
        os.fchmod(descriptor, _FILE_MODE)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def apply_overlay(source_dir: Path, files: Mapping[str, bytes], *, dir_mode: int) -> None:
    """Write `files` into `source_dir`, replacing whatever is at those paths.

    The contract, and why each part exists:

    * **Every path goes through `repolace_shared.paths.resolve_within`**, rooted
      at `source_dir`. Nothing here builds `source_dir / key` and trusts it:
      pathlib discards the root when the key is absolute.
    * **Refused, naming the offending path, writing *nothing*** (every key is
      validated before the disk is touched, so a bad entry cannot leave a
      half-applied overlay): an absolute path, any `..` component, any `.git`
      component at any depth, a backslash or NUL, a non-canonical spelling, any
      path whose existing parent is a symlink or a regular file, a path that is
      an existing directory, and two keys of which one is a parent of the other.
    * **Parent directories are created with `dir_mode`**, chmodded explicitly
      because `mkdir`'s mode argument is masked by the umask. The same lesson as
      the export's directory modes: a sandbox running as an unprivileged uid that
      cannot create a `__pycache__` beside its own tests produces an unscoreable
      run, which silently drops the instance from the benchmark. Directories that
      already exist are left as they are.
    * **Files are written `0644`**, replacing an existing file in full.
    * It touches only the paths it was given. An overlay that deleted or renamed
      anything is excluded at instance preparation, so there is nothing here to
      handle.
    """
    if not files:
        return
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise OverlayError(str(source_dir), "the directory to overlay does not exist")

    checked = {key: _check_key(source_dir, key, data) for key, data in files.items()}
    _check_nesting({key: parts for key, (parts, _target) in checked.items()})

    for key, (parts, target) in checked.items():
        _make_directories(source_dir, parts, dir_mode)
        _write_file(target, bytes(files[key]))
