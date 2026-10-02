"""Resolving a ``RepoSpec``, and guessing one when nobody wrote it down.

Two jobs, deliberately together: what a curated spec file says, and what to do
for a repository nobody has curated yet. CLAUDE.md calls dependency
installation "the part that will actually cost the time", and the honest shape
of that cost is a heuristic that works for most repositories plus a file where
the exceptions get recorded one at a time.

The heuristic's commands are *returned*, not run, so they land in the generated
Dockerfile where a failed build shows exactly what was attempted. A backend that
installed dependencies itself would put that reasoning inside a container log.
"""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import fields
from pathlib import Path

import structlog

from verify.config import ENV_NAME, RESERVED_ENV
from verify.errors import SpecError
from verify.protocol import RepoSpec

log = structlog.get_logger()

#: Read from the export, so a repository cannot point the build at a manifest
#: outside its own tree. Order is the order they are tried.
_REQUIREMENT_NAMES = (
    "requirements-test.txt",
    "requirements_test.txt",
    "test-requirements.txt",
    "requirements-dev.txt",
    "requirements.txt",
)

#: ``key`` is the table name in the spec file, never a field inside it, so a
#: table cannot disagree with its own heading.
_NOT_SETTABLE = frozenset({"key"})


def _coerce(name: str, value: object, spec_key: str) -> object:
    """Turn TOML's types into the dataclass's, refusing anything that does not fit.

    Refusing rather than coercing loosely is the whole point of ``SpecError``:
    a field that silently stopped applying surfaces weeks later as an
    unexplained unscoreable run, with nothing pointing back at the typo.
    """
    if name in ("install", "system_packages", "test_targets", "extra_pytest_args"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SpecError(f"{spec_key}.{name} must be a list of strings, got {value!r}")
        return tuple(value)
    if name in ("keep_addopts", "repo_readonly", "disable_plugin_autoload"):
        if not isinstance(value, bool):
            raise SpecError(f"{spec_key}.{name} must be true or false, got {value!r}")
        return value
    if name == "timeout_seconds":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpecError(f"{spec_key}.{name} must be a number, got {value!r}")
        if value <= 0:
            raise SpecError(f"{spec_key}.{name} must be positive, got {value!r}")
        return float(value)
    if name == "extra_env":
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise SpecError(f"{spec_key}.{name} must be a table of strings, got {value!r}")
        malformed = sorted(k for k in value if not ENV_NAME.fullmatch(k))
        if malformed:
            # `HOME=/evil` is not a name: docker splits it into HOME and wins over the
            # sandbox's own, which the reserved check below cannot see.
            raise SpecError(
                f"{spec_key}.{name} has invalid variable name(s) "
                f"{', '.join(repr(k) for k in malformed)}; names must match [A-Za-z_][A-Za-z0-9_]*"
            )
        reserved = sorted(set(value) & RESERVED_ENV)
        if reserved:
            # The backend refuses these too; failing here names the spec file entry
            # when it is loaded, instead of the first task that happens to use it.
            raise SpecError(
                f"{spec_key}.{name} may not set {', '.join(reserved)}; the sandbox reserves them"
            )
        return dict(value)
    if not isinstance(value, str):
        raise SpecError(f"{spec_key}.{name} must be a string, got {value!r}")
    return value


def spec_from_mapping(key: str, raw: Mapping[str, object]) -> RepoSpec:
    """Build one ``RepoSpec``, rejecting any field the dataclass does not have.

    The allowed set comes from ``dataclasses.fields`` rather than a literal
    list, so adding a field to ``RepoSpec`` cannot leave this behind rejecting
    it as unknown.
    """
    allowed = {f.name for f in fields(RepoSpec)} - _NOT_SETTABLE
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SpecError(
            f"{key}: unknown field(s) {', '.join(unknown)}; "
            f"known fields are {', '.join(sorted(allowed))}"
        )
    values = {name: _coerce(name, value, key) for name, value in raw.items()}
    return RepoSpec(key=key, **values)


def load_specs(path: Path) -> dict[str, RepoSpec]:
    """Read the curated spec file. A missing file is fine; a broken one is not.

    Missing means "nothing curated yet", which is the state every repository
    starts in and not an error. Malformed means somebody wrote a spec that is
    not being applied, which is exactly the silent failure ``SpecError`` exists
    to prevent.
    """
    if not path.is_file():
        log.info("verify.spec.none", path=str(path))
        return {}
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SpecError(f"{path}: {type(exc).__name__}: {exc}") from exc

    table = document.get("specs", document)
    if not isinstance(table, dict):
        raise SpecError(f"{path}: top-level 'specs' must be a table")

    specs = {}
    for key, raw in table.items():
        if not isinstance(raw, dict):
            raise SpecError(f"{path}: entry {key!r} must be a table, got {raw!r}")
        specs[key] = spec_from_mapping(key, raw)
    log.info("verify.spec.loaded", path=str(path), count=len(specs))
    return specs


def resolve_spec(specs: Mapping[str, RepoSpec], key: str) -> RepoSpec:
    """The curated spec for ``key``, or a default one carrying the same key.

    Falling back rather than raising is deliberate: an uncurated repository
    should get a run and a diagnosable build failure, not a refusal that looks
    like the repository is unsupported.
    """
    found = specs.get(key)
    if found is not None:
        return found
    log.info("verify.spec.default", key=key)
    return RepoSpec(key=key)


def heuristic_install(source_dir: Path) -> tuple[str, ...]:
    """Guess how to install a repository, for a spec that gave no ``install``.

    Ordered widest-net-last. The editable install comes first because it is what
    makes the repository importable under its own name -- without it a suite
    that does ``import mypackage`` fails at collection, which ``parse_report``
    correctly calls unscoreable and which would then look like a broken sandbox
    rather than a missing install step.

    ``pytest`` is installed unconditionally at the end. A repository that pins
    its own version has already had it installed by one of the earlier commands,
    and pip leaves a satisfied requirement alone; one that assumes pytest is
    simply present on the machine -- which is common -- would otherwise fail
    with a usage error carrying no clue about why.
    """
    commands: list[str] = []
    has_project = (source_dir / "pyproject.toml").is_file() or (source_dir / "setup.py").is_file()

    for name in _REQUIREMENT_NAMES:
        if (source_dir / name).is_file():
            # Quoted: a filename is repository-controlled and reaches a shell.
            commands.append(f"pip install -r '{name}'")

    if has_project:
        commands.append("pip install -e .")

    commands.append("pip install pytest")
    log.info("verify.spec.heuristic", commands=commands, project=has_project)
    return tuple(commands)


def install_commands(spec: RepoSpec, source_dir: Path) -> tuple[str, ...]:
    """What the image build should run. The spec wins; the heuristic fills in."""
    return spec.install or heuristic_install(source_dir)
