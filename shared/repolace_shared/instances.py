"""One benchmark instance, as data.

An instance is a real GitHub issue with ground truth: the commit it was filed
against, the tests the fixing PR added (the **overlay**), which of them must go
from failing to passing, and the reference fix. This module is the on-disk
format and nothing else -- loading, validating, writing -- so the pipeline can
read an instance by `tasks.instance_id` without depending on `eval/`, and the
harness can produce one without depending on the pipeline. That is why it lives
here and not under `eval/`.

**Trust boundary.** Instance files are trusted operator data: the operator
selected the instances and ran the preparation that wrote them. They are
validated anyway, and the path keys of `test_files` and `gold_files` hardest,
because those keys end up as paths on the host filesystem -- the overlay writes
them into an export, the gold runner writes them into a checkout -- and a path is
the one kind of data where "trusted, but wrong" is already an arbitrary file
write. The text *inside* the files is a different matter: `problem_statement` is
GitHub issue text, which anyone could have written, so it reaches a prompt only
as delimited untrusted data like any other issue body.

**An id that came from a database row goes through `load_instance_by_id`.**
`tasks.instance_id` is a DB value, and `directory / f"{id}.json"` built by hand is
exactly the "path from a database row" pattern CLAUDE.md requires
`resolve_within` for: a row holding `../../x` would point `load_instance` at any
JSON file on the host. `instance_path` validates the id with the same rule the
files are held to and confines the result; `load_instance(path)` is for a path
the operator typed.

**What must never cross.** `gold_files` is the reference fix. It exists for gold
validation (apply it, run the real stack, require PASSED) and must never reach an
agent, a prompt, or a tool result; `test_files` is the oracle's tests, and
reaches the *sandbox* only. Neither is in `problem_statement`, and nothing here
renders either one.

**Why `spec` is a raw mapping.** It holds `RepoSpec` fields, but `shared` cannot
import `verify` (the dependency points the other way). It is stored as the plain
JSON object `verify.spec.spec_from_mapping(key, ...)` consumes -- the fields
*without* `key`, which that function takes separately and refuses inside the
mapping. **The key is the `instance_id`** (it feeds the image tag, so one instance
is one environment), supplied by the caller and never stored in the mapping.

**`spec` is frozen all the way down, so hand `spec_from_mapping` `plain_spec()`.**
Nested lists are tuples and nested mappings are read-only, so
`spec["install"].append(...)` cannot edit a loaded instance. `spec_from_mapping`
accepts only `list` and `dict`, so the conversion back happens at the edge:
`plain_spec()` returns a fresh, mutable, JSON-shaped copy each call. Passing
`.spec` straight in is a `SpecError`, loudly.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any

from repolace_shared.paths import PathEscapesRoot, resolve_within

SCHEMA_VERSION = 1

#: What an instance id may look like. It is a file name (`<id>.json`), a Docker
#: container-name component, a branch name (`bench/<id>`) and part of a GitHub
#: repository name, so it is held to the intersection of what all four accept.
#: Every SWE-bench id (`psf__requests-2317`, `scikit-learn__scikit-learn-10297`)
#: fits. The character class alone is not enough for the branch name: `a..b`,
#: `x.` and `x.lock` all match it and `git check-ref-format` rejects every one,
#: so `_check_instance_id` adds those rules (and `.git`, which GitHub strips from
#: the end of a repository name).
_INSTANCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

#: A full commit sha. A branch name or tag pins an instance to a ref that moves,
#: which silently makes the benchmark non-reproducible, and a value starting with
#: `-` (`--upload-pack=x`) would reach git's argv as an option.
_BASE_COMMIT = re.compile(r"[0-9a-f]{40}")

#: `tasks.issue_number` is a 32-bit `Integer` column. A larger value loads, then
#: fails at enqueue with a driver overflow, far from where it was written.
_MAX_ISSUE_NUMBER = 2**31 - 1

#: Nesting allowed inside `spec`. `RepoSpec` needs two levels (a table of strings,
#: a list of strings); a bound turns a pathological file into an `InstanceError`
#: rather than a `RecursionError` from wherever the recursion happens to run out.
_MAX_SPEC_DEPTH = 16

#: Longest `issue_title`. What the task row and the retrieval query both use.
_TITLE_MAX_CHARS = 200


class InstanceError(ValueError):
    """An instance file is unreadable, malformed, or unsafe to use."""


def _freeze(value: object, where: str, depth: int = 0) -> object:
    """Mappings become read-only, lists become tuples, recursively.

    Anything else passes through untouched, including an object `json` cannot
    hold: that is `dump_instance`'s error to raise, with the file's name on it.
    """
    if depth > _MAX_SPEC_DEPTH:
        raise InstanceError(f"{where} is nested more than {_MAX_SPEC_DEPTH} levels deep")
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item, where, depth + 1) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, where, depth + 1) for item in value)
    return value


@dataclass(frozen=True)
class InstanceSpec:
    #: "psf__requests-2317". Also the file stem, so the directory is its own index.
    instance_id: str
    #: The upstream repository, "psf/requests". Identification only: it is never
    #: rendered into a pull request, because a `#`-reference or a URL there would
    #: resolve to an unrelated object in the bench repository, or notify upstream.
    repo: str
    #: A full 40-hex commit sha, never a branch or tag: a moving ref makes the
    #: benchmark non-reproducible.
    base_commit: str
    version: str
    #: UNTRUSTED. GitHub issue text; reaches a prompt only as delimited data.
    problem_statement: str
    #: Synthetic: the trailing integer of `instance_id`. The bench repository has
    #: no such issue; the number only keys the task row. 1..2**31-1.
    issue_number: int
    #: Node ids that must go from failing to passing. Never empty: an empty list
    #: makes `score(expected_fail_to_pass=())` vacuously PASSED for any patch that
    #: breaks nothing, which is a pass nobody could defend. Every entry is a
    #: non-empty string; a bare `str` is refused rather than split into characters.
    fail_to_pass: tuple[str, ...]
    #: Reference only. `score()` computes regressions itself from the baseline
    #: run; this list is what the upstream project says should keep passing.
    pass_to_pass: tuple[str, ...]
    #: The overlay: repo-relative path -> the file's **full** text after the fix
    #: PR's test changes. Full text, not a diff -- instance preparation applies
    #: SWE-bench's `test_patch` once, so nothing at run time parses a patch.
    #: Text, so a non-UTF-8 test file cannot be represented and excludes the
    #: instance at preparation. **Never empty**: its keys are `hidden_paths`, and an
    #: empty set there silently turns the feedback filter and the stdout
    #: suppression off. `repr=False`: the oracle's tests must not print into a log.
    test_files: Mapping[str, str] = field(repr=False)
    #: The reference fix's files, same shape. Gold validation only; see above.
    #: `repr=False`, because a stray log line must not print the gold patch.
    gold_files: Mapping[str, str] = field(repr=False)
    #: `RepoSpec` fields as a raw mapping, without `key`, frozen all the way down.
    #: See the module docstring, and use `plain_spec()` to hand it to `verify`.
    spec: Mapping[str, object]
    #: Whether pass-to-pass was run over a targeted subset because the full suite
    #: exceeds the timeout. Flagged on every result rather than hidden: a headline
    #: that quietly ran fewer tests is not comparable with one that did not. Must
    #: agree with `spec["test_targets"]`, which is what makes it so.
    targeted_p2p: bool = False
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Frozen only stops rebinding. The overlay is the oracle's tests and `spec`
        # decides how they are run, so a caller must not be able to edit either
        # through a reference it kept -- and a list handed in for a tuple field
        # would make two equal-looking specs compare unequal.
        for name in ("fail_to_pass", "pass_to_pass"):
            value = getattr(self, name)
            # `tuple("a::b")` is `('a', ':', ':', 'b')`: a bare string would
            # silently become one-character test ids that no suite can match.
            if not isinstance(value, (list, tuple)):
                raise InstanceError(
                    f"{name} must be a list or tuple of strings, got {type(value).__name__}"
                )
            object.__setattr__(self, name, tuple(value))
        for name in ("test_files", "gold_files"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise InstanceError(f"{name} must be a mapping of paths to text, got {type(value).__name__}")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        if not isinstance(self.spec, Mapping):
            raise InstanceError(f"spec must be a mapping, got {type(self.spec).__name__}")
        object.__setattr__(self, "spec", _freeze(self.spec, "spec"))

    def __hash__(self) -> int:
        # A frozen dataclass generates `__hash__` from every field, and the frozen
        # mappings are unhashable. The identity fields are enough and agree with
        # `==`: equal specs have equal ids, commits and versions.
        return hash((self.instance_id, self.base_commit, self.schema_version))

    def __reduce__(self) -> tuple[Any, ...]:
        # `MappingProxyType` supports neither pickle nor deepcopy, so without this
        # a spec could not cross a process boundary or sit in deep-copied graph
        # state. Round-trips through the plain document, which re-freezes on load.
        return (_rebuild, (_document(self),))

    @property
    def issue_title(self) -> str:
        """The first non-empty line of `problem_statement`, at most 200 characters.

        One definition, so enqueue (which writes `tasks.issue_title`) and the
        retrieval eval (which builds its query from the title) cannot derive two
        different titles from one statement. Empty only if the statement has no
        non-blank line at all.
        """
        for line in self.problem_statement.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:_TITLE_MAX_CHARS].rstrip()
        return ""

    def plain_spec(self) -> dict[str, Any]:
        """`spec` as a fresh, mutable, JSON-shaped dict, for `verify.spec.spec_from_mapping`.

        `spec` itself is frozen all the way down (tuples, read-only mappings) and
        `spec_from_mapping` accepts only `list` and `dict`, so the conversion back
        is done here, at the edge, once per call. Mutating the result cannot reach
        this instance.
        """
        return _plain(self.spec)  # type: ignore[return-value]

    def overlay_bytes(self) -> dict[str, bytes]:
        """`test_files` as the bytes the sandbox will be given.

        UTF-8, which `load_instance` has already confirmed every value encodes to.
        A fresh dict each call: this is what `Verifier(overlay=...)` stores, and it
        must not share state with the spec.
        """
        return {path: text.encode("utf-8") for path, text in self.test_files.items()}


_FIELD_NAMES = tuple(f.name for f in fields(InstanceSpec))
#: Optional in a file because the dataclass has a default; everything else must be
#: present. `schema_version` is *not* in this set: a file with no version cannot be
#: assumed to be this one.
_OPTIONAL_FIELDS = frozenset({"targeted_p2p"})


# --- validation --------------------------------------------------------------


def _validate_repo_path(path: object, where: str) -> None:
    """Refuse any key that is not a plain relative path inside a tree.

    Every one of these is a way to make a later `root / key` land somewhere the
    operator did not mean, and none needs an attacker -- a preparation bug is
    enough. The `.git` check is on every component, case-insensitively: a `.git`
    entry is executable configuration to the host's git (hooks, fsmonitor, config),
    and the sandbox is never supposed to receive one.
    """
    if not isinstance(path, str) or not path:
        raise InstanceError(f"{where}: path keys must be non-empty strings, got {path!r}")
    if "\x00" in path:
        raise InstanceError(f"{where}: path contains a NUL byte: {path!r}")
    if "\\" in path:
        raise InstanceError(f"{where}: path contains a backslash: {path!r}")
    if path.startswith("/"):
        raise InstanceError(f"{where}: absolute path: {path!r}")
    for component in path.split("/"):
        if component == "":
            raise InstanceError(f"{where}: empty path component (doubled or trailing slash): {path!r}")
        if component in (".", ".."):
            raise InstanceError(f"{where}: path has a {component!r} component: {path!r}")
        if component.lower() == ".git":
            raise InstanceError(f"{where}: path has a .git component: {path!r}")


def _check_instance_id(instance_id: object, where: str) -> None:
    """The rule an id is held to, by its file, its container, its branch and its repo name.

    Shared by `_validate` and `instance_path`, so an id accepted when a file is
    written is the id accepted when a database row is read back.
    """
    if not isinstance(instance_id, str) or not _INSTANCE_ID.fullmatch(instance_id):
        raise InstanceError(
            f"{where}: instance_id {instance_id!r} must start with a letter or digit and use "
            f"only letters, digits, '_', '.', '-'"
        )
    # Each of these matches the character class and is refused by git as a branch
    # name (`bench/<id>`), or by GitHub as a repository name (`bench-<id>`).
    lowered = instance_id.lower()
    if ".." in instance_id:
        reason = "contains '..'"
    elif instance_id.endswith("."):
        reason = "ends with '.'"
    elif lowered.endswith(".lock"):
        reason = "ends with '.lock'"
    elif lowered.endswith(".git"):
        reason = "ends with '.git'"
    else:
        return
    raise InstanceError(
        f"{where}: instance_id {instance_id!r} {reason}, which git refuses in a branch name "
        f"or GitHub strips from a repository name"
    )


def _validate(spec: InstanceSpec, where: str) -> None:
    """The rules that hold for a spec however it was built -- from a file or in code.

    Shared by `load_instance` and `dump_instance`, so the harness cannot write a
    file the pipeline would then refuse to read.
    """
    if spec.schema_version != SCHEMA_VERSION:
        raise InstanceError(
            f"{where}: schema_version is {spec.schema_version!r}, this code reads {SCHEMA_VERSION}"
        )
    _check_instance_id(spec.instance_id, where)
    if not spec.repo:
        raise InstanceError(f"{where}: repo is empty")
    if not isinstance(spec.base_commit, str) or not _BASE_COMMIT.fullmatch(spec.base_commit):
        raise InstanceError(
            f"{where}: base_commit {spec.base_commit!r} must be a full 40-character lowercase hex sha "
            f"(a branch or tag pins the instance to a ref that moves, and a leading '-' would reach "
            f"git as an option)"
        )
    if isinstance(spec.issue_number, bool) or not isinstance(spec.issue_number, int):
        raise InstanceError(f"{where}: issue_number must be an integer, got {spec.issue_number!r}")
    if not 1 <= spec.issue_number <= _MAX_ISSUE_NUMBER:
        raise InstanceError(
            f"{where}: issue_number must be between 1 and {_MAX_ISSUE_NUMBER} (a 32-bit column), "
            f"got {spec.issue_number}"
        )
    if not spec.fail_to_pass:
        raise InstanceError(
            f"{where}: fail_to_pass is empty, so any patch that breaks nothing would score PASSED"
        )
    for name in ("fail_to_pass", "pass_to_pass"):
        for item in getattr(spec, name):
            if not isinstance(item, str) or not item.strip():
                raise InstanceError(f"{where}: {name} entries must be non-empty strings, got {item!r}")
    if not spec.test_files:
        raise InstanceError(
            f"{where}: test_files is empty. Its keys are the hidden paths, and an empty set there "
            f"silently turns the feedback filter and the stdout suppression off"
        )
    # `targeted_p2p` records that pass-to-pass ran over a targeted subset, and the
    # subset *is* `spec.test_targets`. Either alone is a result flagged wrongly:
    # "targeted" with nothing targeted, or a narrowed run reported as the full one.
    has_targets = bool(spec.spec.get("test_targets"))
    if spec.targeted_p2p != has_targets:
        raise InstanceError(
            f"{where}: targeted_p2p is {spec.targeted_p2p} but spec.test_targets is "
            f"{'set' if has_targets else 'empty or absent'}; the two must agree"
        )

    for field_name in ("test_files", "gold_files"):
        for path, text in getattr(spec, field_name).items():
            _validate_repo_path(path, f"{where}: {field_name}")
            try:
                text.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise InstanceError(
                    f"{where}: {field_name}[{path!r}] is not encodable as UTF-8: {exc.reason}"
                ) from exc


def _expect(data: Mapping[str, Any], name: str, kind: type, where: str) -> Any:
    value = data[name]
    # bool is an int subclass; a `true` where a number belongs is a typo, not a number.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise InstanceError(f"{where}: {name} must be {kind.__name__}, got {value!r}")
    return value


def _expect_strings(data: Mapping[str, Any], name: str, where: str) -> tuple[str, ...]:
    value = data[name]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InstanceError(f"{where}: {name} must be a list of strings, got {value!r}")
    return tuple(value)


def _expect_text_map(data: Mapping[str, Any], name: str, where: str) -> dict[str, str]:
    value = data[name]
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise InstanceError(f"{where}: {name} must be an object of strings to strings")
    return dict(value)


def _from_mapping(data: object, where: str) -> InstanceSpec:
    if not isinstance(data, dict):
        raise InstanceError(f"{where}: top level must be a JSON object, got {type(data).__name__}")

    unknown = sorted(set(data) - set(_FIELD_NAMES))
    if unknown:
        raise InstanceError(
            f"{where}: unknown key(s) {', '.join(unknown)}; known keys are {', '.join(sorted(_FIELD_NAMES))}"
        )
    missing = sorted(set(_FIELD_NAMES) - set(data) - _OPTIONAL_FIELDS)
    if missing:
        raise InstanceError(f"{where}: missing key(s) {', '.join(missing)}")

    # Checked before anything else is read: a file written for another schema may
    # have fields this code would misread rather than reject.
    version = data["schema_version"]
    # An int exactly: `1.0 == 1` and `True == 1`, and neither is a version number.
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise InstanceError(f"{where}: schema_version is {version!r}, this code reads {SCHEMA_VERSION}")

    values: dict[str, Any] = {
        "instance_id": _expect(data, "instance_id", str, where),
        "repo": _expect(data, "repo", str, where),
        "base_commit": _expect(data, "base_commit", str, where),
        "version": _expect(data, "version", str, where),
        "problem_statement": _expect(data, "problem_statement", str, where),
        "issue_number": _expect(data, "issue_number", int, where),
        "fail_to_pass": _expect_strings(data, "fail_to_pass", where),
        "pass_to_pass": _expect_strings(data, "pass_to_pass", where),
        "test_files": _expect_text_map(data, "test_files", where),
        "gold_files": _expect_text_map(data, "gold_files", where),
        "spec": _expect(data, "spec", dict, where),
        "schema_version": version,
    }
    if "targeted_p2p" in data:
        values["targeted_p2p"] = _expect(data, "targeted_p2p", bool, where)
    try:
        return InstanceSpec(**values)
    except InstanceError as exc:
        # Raised from `__post_init__`, which knows the field but not the file.
        raise InstanceError(f"{where}: {exc}") from exc


# --- reading and writing -----------------------------------------------------


def load_instance(path: Path) -> InstanceSpec:
    """Read and validate one instance file.

    Rejects, each with an error naming the file, and **always as an
    `InstanceError`** -- never a raw `ValueError`, `RecursionError` or
    `OSError`: unreadable or non-JSON input (including an integer too long for
    Python to parse and nesting too deep to recurse into), a top level that is
    not an object, **unknown keys** (a typo that silently stopped applying would
    surface later as an unexplained unscoreable instance), missing keys, a
    **wrong `schema_version`**, wrong types, an empty `fail_to_pass` or
    `test_files`, a `base_commit` that is not a full sha, an `issue_number`
    outside 1..2**31-1, an instance id git would refuse as a branch name, a
    `targeted_p2p` that disagrees with `spec.test_targets`, and any `test_files`
    / `gold_files` key that is absolute, contains a `..` or `.` component, a
    backslash, an empty component, a NUL byte, or a `.git` component.

    For an id that came from a database row use `load_instance_by_id`, which
    confines the path first.
    """
    where = str(path)
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InstanceError(f"{where}: cannot read: {type(exc).__name__}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # `JSONDecodeError` is a `ValueError`, and so is the error Python raises
        # for an integer literal past its digit limit; `RecursionError` is what
        # deeply nested input costs. All of them are "not a valid instance file".
        raise InstanceError(f"{where}: not valid JSON: {type(exc).__name__}: {exc}") from exc

    spec = _from_mapping(data, where)
    _validate(spec, where)
    return spec


def instance_path(directory: Path, instance_id: str) -> Path:
    """Where `instance_id`'s file is inside `directory`, or an `InstanceError`.

    The id is checked with the rule files are held to, then the path is confined
    with `repolace_shared.paths.resolve_within`: a symlink is refused rather than
    followed, and nothing resolves outside `directory`. Use it for any id that
    came from a database row (`tasks.instance_id`), never `directory / f"{id}.json"`.

    Does not check that the file exists; `load_instance` reports that with the
    path in the message.
    """
    _check_instance_id(instance_id, "instance id")
    try:
        return resolve_within(Path(directory), f"{instance_id}.json")
    except PathEscapesRoot as exc:
        raise InstanceError(f"{directory}: {exc}") from exc


def load_instance_by_id(directory: Path, instance_id: str) -> InstanceSpec:
    """The instance named `instance_id` from `directory`, validated.

    `instance_path` then `load_instance`, plus one more check: the file's own
    `instance_id` must be the one asked for, as `load_instances` requires of the
    stem, so a file that says it is some other instance cannot be loaded under
    this one's name.
    """
    path = instance_path(directory, instance_id)
    spec = load_instance(path)
    if spec.instance_id != instance_id:
        raise InstanceError(f"{path}: contains instance {spec.instance_id!r}, expected {instance_id!r}")
    return spec


def load_instances(directory: Path) -> dict[str, InstanceSpec]:
    """Every instance in a directory, keyed by `instance_id`.

    Only `*.json` files are read: the directory also holds the gold patches, the
    manifest and the validation report, none of which are instances. Everything
    that *is* `.json` must be one -- a stray `notes.json` fails the load rather than
    being skipped, so an instance that failed to parse cannot silently vanish from
    the set.

    **The file stem must equal the instance id.** That makes the directory its own
    index with no second source of truth to disagree with it, and it is also what
    rules out two files claiming one id (the second would be a stem mismatch), so
    there is no separate duplicate check.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise InstanceError(f"{directory}: not a directory")

    instances: dict[str, InstanceSpec] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json" or not path.is_file():
            continue
        spec = load_instance(path)
        if path.stem != spec.instance_id:
            raise InstanceError(
                f"{path}: file name must be '{spec.instance_id}.json' to match its instance_id"
            )
        instances[spec.instance_id] = spec
    return instances


def _document(spec: InstanceSpec) -> dict[str, Any]:
    """The plain-JSON form of a spec: exactly the keys `_from_mapping` reads."""
    return {
        "instance_id": spec.instance_id,
        "repo": spec.repo,
        "base_commit": spec.base_commit,
        "version": spec.version,
        "problem_statement": spec.problem_statement,
        "issue_number": spec.issue_number,
        "fail_to_pass": list(spec.fail_to_pass),
        "pass_to_pass": list(spec.pass_to_pass),
        "test_files": dict(spec.test_files),
        "gold_files": dict(spec.gold_files),
        "spec": _plain(spec.spec),
        "targeted_p2p": spec.targeted_p2p,
        "schema_version": spec.schema_version,
    }


def _rebuild(document: dict[str, Any]) -> InstanceSpec:
    """Inverse of `_document`; what unpickling and deep-copying call."""
    return InstanceSpec(**document)


def dump_instance(spec: InstanceSpec, path: Path) -> None:
    """Write one instance file in a stable form.

    Sorted keys at every level, two-space indent, a trailing newline: the files
    are committed and reviewed, and a diff should show a changed fixture, not a
    reshuffled one. Validates first, with the rules `load_instance` applies, so a
    file that would not load is never written -- **including its name**: the path
    must be `<instance_id>.json`, because `load_instances` refuses a directory in
    which any file's name disagrees with its content, and one misnamed write would
    break the whole set. Always an `InstanceError` on failure, a lone surrogate in
    any field included.

    Written to a temporary file, flushed to disk and renamed, so neither an
    interrupted write nor a power loss can leave a half-file that later fails to
    parse as though the instance were broken. The result is mode 0644: `mkstemp`
    creates 0600, which `os.replace` would keep, and a committed instance file is
    meant to be readable by another uid or a bind-mounted reader.
    """
    path = Path(path)
    if path.name != f"{spec.instance_id}.json":
        raise InstanceError(
            f"{path}: file name must be '{spec.instance_id}.json' to match its instance_id, "
            f"or load_instances would refuse the whole directory"
        )
    _validate(spec, str(path))
    try:
        text = json.dumps(_document(spec), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        # `ensure_ascii=False` lets a lone surrogate through `dumps` and it only
        # fails when written, as a raw `UnicodeEncodeError` -- from *any* field,
        # not just the two `_validate` looks inside.
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InstanceError(
            f"{path}: contains text that is not encodable as UTF-8 (a lone surrogate?): {exc.reason}"
        ) from exc
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise InstanceError(f"{path}: spec is not JSON-serialisable: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _plain(value: object) -> object:
    """Unwrap read-only mappings and tuples into what `json` serialises.

    `json.dumps` handles dict and list but not `MappingProxyType`, and the frozen
    spec stores one. Recursive because a `RepoSpec` field like `extra_env` is a
    mapping itself; bounded in practice by `_MAX_SPEC_DEPTH`, which `InstanceSpec`
    enforces on construction.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "SCHEMA_VERSION",
    "InstanceError",
    "InstanceSpec",
    "dump_instance",
    "instance_path",
    "load_instance",
    "load_instance_by_id",
    "load_instances",
]
