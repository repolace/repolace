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

**What must never cross.** `gold_files` is the reference fix. It exists for gold
validation (apply it, run the real stack, require PASSED) and must never reach an
agent, a prompt, or a tool result; `test_files` is the oracle's tests, and
reaches the *sandbox* only. Neither is in `problem_statement`, and nothing here
renders either one.

**Why `spec` is a raw mapping.** It holds `RepoSpec` fields, but `shared` cannot
import `verify` (the dependency points the other way). It is stored as the plain
JSON object `verify.spec.spec_from_mapping(key, spec)` consumes -- the fields
*without* `key`, which that function takes separately and refuses inside the
mapping. The key is the bench repository's name, derived where the repository is
known, not stored per instance.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = 1

#: What an instance id may look like. It is a file name (`<id>.json`), a Docker
#: container-name component, a branch name (`bench/<id>`) and part of a GitHub
#: repository name, so it is held to the intersection of what all four accept.
#: Every SWE-bench id (`psf__requests-2317`, `scikit-learn__scikit-learn-10297`)
#: fits.
_INSTANCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class InstanceError(ValueError):
    """An instance file is unreadable, malformed, or unsafe to use."""


@dataclass(frozen=True)
class InstanceSpec:
    #: "psf__requests-2317". Also the file stem, so the directory is its own index.
    instance_id: str
    #: The upstream repository, "psf/requests". Identification only: it is never
    #: rendered into a pull request, because a `#`-reference or a URL there would
    #: resolve to an unrelated object in the bench repository, or notify upstream.
    repo: str
    base_commit: str
    version: str
    #: UNTRUSTED. GitHub issue text; reaches a prompt only as delimited data.
    problem_statement: str
    #: Synthetic: the trailing integer of `instance_id`. The bench repository has
    #: no such issue; the number only keys the task row.
    issue_number: int
    #: Node ids that must go from failing to passing. Never empty: an empty list
    #: makes `score(expected_fail_to_pass=())` vacuously PASSED for any patch that
    #: breaks nothing, which is a pass nobody could defend.
    fail_to_pass: tuple[str, ...]
    #: Reference only. `score()` computes regressions itself from the baseline
    #: run; this list is what the upstream project says should keep passing.
    pass_to_pass: tuple[str, ...]
    #: The overlay: repo-relative path -> the file's **full** text after the fix
    #: PR's test changes. Full text, not a diff -- instance preparation applies
    #: SWE-bench's `test_patch` once, so nothing at run time parses a patch.
    #: Text, so a non-UTF-8 test file cannot be represented and excludes the
    #: instance at preparation.
    test_files: Mapping[str, str]
    #: The reference fix's files, same shape. Gold validation only; see above.
    gold_files: Mapping[str, str]
    #: `RepoSpec` fields as a raw mapping, without `key`. See the module docstring.
    spec: Mapping[str, object]
    #: Whether pass-to-pass was run over a targeted subset because the full suite
    #: exceeds the timeout. Flagged on every result rather than hidden: a headline
    #: that quietly ran fewer tests is not comparable with one that did not.
    targeted_p2p: bool = False
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Frozen only stops rebinding. The overlay is the oracle's tests and `spec`
        # decides how they are run, so a caller must not be able to edit either
        # through a reference it kept -- and a list handed in for a tuple field
        # would make two equal-looking specs compare unequal.
        for name in ("test_files", "gold_files", "spec"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        for name in ("fail_to_pass", "pass_to_pass"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

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


def _validate(spec: InstanceSpec, where: str) -> None:
    """The rules that hold for a spec however it was built -- from a file or in code.

    Shared by `load_instance` and `dump_instance`, so the harness cannot write a
    file the pipeline would then refuse to read.
    """
    if spec.schema_version != SCHEMA_VERSION:
        raise InstanceError(
            f"{where}: schema_version is {spec.schema_version!r}, this code reads {SCHEMA_VERSION}"
        )
    if not _INSTANCE_ID.fullmatch(spec.instance_id):
        raise InstanceError(
            f"{where}: instance_id {spec.instance_id!r} must start with a letter or digit and use "
            f"only letters, digits, '_', '.', '-'"
        )
    if not spec.repo:
        raise InstanceError(f"{where}: repo is empty")
    if not spec.base_commit:
        raise InstanceError(f"{where}: base_commit is empty")
    if spec.issue_number < 1:
        raise InstanceError(f"{where}: issue_number must be positive, got {spec.issue_number}")
    if not spec.fail_to_pass:
        raise InstanceError(
            f"{where}: fail_to_pass is empty, so any patch that breaks nothing would score PASSED"
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
    return InstanceSpec(**values)


# --- reading and writing -----------------------------------------------------


def load_instance(path: Path) -> InstanceSpec:
    """Read and validate one instance file.

    Rejects, each with an error naming the file: unreadable or non-JSON input, a
    top level that is not an object, **unknown keys** (a typo that silently stopped
    applying would surface later as an unexplained unscoreable instance), missing
    keys, a **wrong `schema_version`**, wrong types, an empty `fail_to_pass`, and
    any `test_files` / `gold_files` key that is absolute, contains a `..` or `.`
    component, a backslash, an empty component, a NUL byte, or a `.git` component.
    """
    where = str(path)
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InstanceError(f"{where}: cannot read: {type(exc).__name__}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstanceError(f"{where}: not valid JSON: {exc}") from exc

    spec = _from_mapping(data, where)
    _validate(spec, where)
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


def dump_instance(spec: InstanceSpec, path: Path) -> None:
    """Write one instance file in a stable form.

    Sorted keys at every level, two-space indent, a trailing newline: the files
    are committed and reviewed, and a diff should show a changed fixture, not a
    reshuffled one. Validates first, with the rules `load_instance` applies, so a
    file that would not load is never written. Written to a temporary file and
    renamed, so an interrupted write cannot leave a half-file that later fails to
    parse as though the instance were broken.
    """
    path = Path(path)
    _validate(spec, str(path))
    document = {
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
    try:
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise InstanceError(f"{path}: spec is not JSON-serialisable: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _plain(value: object) -> object:
    """Unwrap read-only mappings and tuples into what `json` serialises.

    `json.dumps` handles dict and list but not `MappingProxyType`, and the frozen
    spec stores one. Recursive because a `RepoSpec` field like `extra_env` is a
    mapping itself.
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
    "load_instance",
    "load_instances",
]
