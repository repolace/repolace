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

from collections.abc import Mapping
from pathlib import Path


def apply_overlay(source_dir: Path, files: Mapping[str, bytes], *, dir_mode: int) -> None:
    """Write `files` into `source_dir`, replacing whatever is at those paths.

    The keys come from instance data -- trusted operator input -- but they end up
    as paths on the host filesystem, so they are validated as if they were not.
    The contract an implementation must hold:

    * **Every path goes through `repolace_shared.paths.resolve_within`**, rooted
      at `source_dir`. Nothing here may build `source_dir / key` by hand: pathlib
      discards the root when the key is absolute.
    * **Refused, with an error that names the offending path** and writes
      *nothing* (validate every key before touching the disk, so a bad entry
      cannot leave a half-applied overlay): an absolute path, any `..` component,
      any **`.git` component** at any depth (a `.git` entry is executable
      configuration to the host's git; the sandbox never receives one), and any
      path whose existing parent is a **symlink**.
    * **Parent directories are created with `dir_mode`** -- chmodded explicitly,
      because `mkdir`'s mode argument is masked by the umask. This is the same
      lesson as the export's directory modes: a sandbox running as an
      unprivileged uid that cannot create a `__pycache__` beside its own tests
      produces an unscoreable run, which silently drops the instance from the
      benchmark instead of failing visibly.
    * **Files are written `0644`**, replacing an existing file of the same path
      in full -- never appended to or patched.
    * It touches only the paths it was given. An overlay that deleted or renamed
      anything is excluded at instance preparation, so there is nothing here to
      handle.
    """
    raise NotImplementedError("apply_overlay lands in stream A: sandbox")
