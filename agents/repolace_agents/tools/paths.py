"""The one place a model-chosen path becomes a filesystem path.

Every path a tool touches was written by a language model, and the model reads
text that anyone can file on a public repository. So a path here is untrusted
input to a filesystem call, and this is the *only* function that turns one into a
`Path`. The tools never join a checkout and a string themselves; they call
`confine` and open what it returns.

Confinement is deliberately small -- `resolve_within` for "inside the tree, no
symlinks", then three refusals of our own, each with the reason beside it. The
reasons matter more than the code: this is a confused-deputy surface (CLAUDE.md,
"Danger 2"), where nothing escapes and the *host* later acts on something the
agent wrote.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from repolace_shared.paths import PathEscapesRoot, resolve_within

from repolace_agents.tools.base import ToolError

#: A longer string is not a path anyone meant, and filesystems refuse a component
#: over 255 bytes with an `OSError` that would otherwise escape as a "bug".
MAX_PATH_CHARS = 500

#: Why a protected write is refused, in the words the model needs: the rule, and
#: what to do instead. Without the second half a model that cannot add a test
#: tends to try the same write under a different name.
PROTECTED_WRITE_REFUSAL = (
    "{path} is a test or configuration file, which is read-only: test files, conftest.py, "
    "pytest/tox/setup config and CI workflows cannot be edited, and adding new test files is not "
    "allowed. Fix the source code instead, and reproduce behaviour with run_python scratch scripts"
)

_GIT_REFUSAL = (
    "{path} is off limits: any path component starting with '.git' (.git, .gitignore, "
    ".gitattributes, .gitmodules, .github, ...) is configuration that git or the hosting platform "
    "acts on, so these tools neither read nor change it"
)


def printable(text: str) -> str:
    """`text` with anything UTF-8 cannot encode written as an escape (`\\ud800`, `\\udcff`).

    A lone surrogate reaches here two ways: a model string (JSON allows one) and a
    file name that is not valid UTF-8 (Python decodes those with surrogate escapes,
    and a repository may hold one). Echoed raw into a tool result, either would make
    the next provider request fail to encode, ending the run.
    """
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def shown(text: str, limit: int = 80) -> str:
    """A bounded, encodable echo of something the model sent, for an error message."""
    return printable(text if len(text) <= limit else f"{text[:limit]}... ({len(text)} chars)")


def require_text(name: str, text: str) -> None:
    """Refuse a model string that cannot be encoded as UTF-8 (a lone surrogate such as `\\ud800`).

    JSON permits one, Python holds one happily, and the first thing that encodes it
    -- a subprocess argument, a script file, a database parameter -- raises
    `UnicodeEncodeError`. That would escape as a bug and end the run for a mistake
    that is the model's, so every string that leaves the process is checked here.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError(f"{name} is not valid text (it holds a lone surrogate character)") from None


def is_git_name(component: str) -> bool:
    """Is this path component part of the `.git*` family?

    The whole family, not just `.git`: git reads `.gitattributes` (a `diff=` or
    `filter=` selector pairs with a driver command), `.gitmodules` and `.gitignore`
    as configuration, and `.github/` is CI that runs with the repository's
    secrets. Case-folded because a case-insensitive filesystem would otherwise
    let `.GIT/hooks/post-commit` through. A hook written there runs on the *host*
    at the next commit, which is the one thing the sandbox cannot contain.
    """
    return component.lower().startswith(".git")


def relative_posix(checkout: Path, resolved: Path) -> str:
    """`resolved` relative to the checkout, as the repo-relative path git and the scorer use."""
    return resolved.relative_to(checkout.resolve()).as_posix()


def confine(
    checkout: Path,
    path: str,
    *,
    write: bool,
    is_protected: Callable[[str], bool] | None = None,
) -> Path:
    """Resolve a model-supplied path inside `checkout`, or raise a `ToolError`.

    Always refused, for reads too:

    * an absolute path, `..`, and any symlink or symlinked parent that leaves the
      tree -- `resolve_within`, which refuses a symlink rather than following it;
    * a symlink anywhere in the path, even one that lands back inside the tree;
    * anything in the `.git*` family (see `is_git_name`), checked on the string
      the model sent *and* on the resolved location, so a path that merely spells
      `.git` and one that reaches it another way are both refused.

    Refused when `write=True`, additionally:

    * the repository root itself (not a file);
    * `is_protected(rel)` -- `ToolContext.is_protected`, never `is_protected_path`
      directly, so the pipeline's baseline-aware closure applies. It is judged on
      the **resolved** repo-relative path, so `./tests/x.py`, `src/../tests/x.py`
      and an in-tree symlink onto `tests/` all read the same as `tests/x.py`.

    `is_protected` is required, not defaulted, when `write=True`: a default would
    be the weaker baseline-blind guard, and a tool author who forgot to pass the
    real one would get no error, only a quietly weaker check.

    Returns the resolved path, which is what the caller must open. No file is
    created, touched or even required to exist here.
    """
    if write and is_protected is None:
        raise TypeError("confine(write=True) requires is_protected=ctx.is_protected")

    if not path or "\0" in path or len(path) > MAX_PATH_CHARS:
        # NUL makes `lstat` raise ValueError and an overlong component raises
        # OSError(ENAMETOOLONG); both are the model's mistake, not a bug.
        raise ToolError(
            f"not a usable path: {shown(path)!r}; give a non-empty path of at most "
            f"{MAX_PATH_CHARS} characters, relative to the repository root"
        )

    if any(is_git_name(part) for part in path.split("/")):
        raise ToolError(_GIT_REFUSAL.format(path=shown(path)))

    try:
        resolved = resolve_within(checkout, path)
    except PathEscapesRoot as exc:
        raise ToolError(f"{shown(str(exc), 200)}; use a path inside the repository, relative to its root") from None
    except (OSError, ValueError):
        raise ToolError(f"not a usable path: {shown(path)!r}") from None

    # `resolve_within` refuses a symlink as the last component and any symlink whose
    # target is outside the tree; a symlinked *directory* in the middle that lands
    # back inside is followed. That would let the repository choose where a later read
    # or write goes -- the reason `resolve_within` refuses at all -- so refuse these too.
    cursor = checkout.resolve()
    for part in path.split("/"):
        if part in ("", "."):
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            raise ToolError(f"refusing to follow a symlink: {shown(path)}")

    rel = relative_posix(checkout, resolved)
    if any(is_git_name(part) for part in rel.split("/")):
        raise ToolError(_GIT_REFUSAL.format(path=shown(path)))

    if write:
        if rel == ".":
            raise ToolError("that path is the repository root; name a file inside it")
        assert is_protected is not None
        if is_protected(rel):
            raise ToolError(PROTECTED_WRITE_REFUSAL.format(path=printable(rel)))
    return resolved
