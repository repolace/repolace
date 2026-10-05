"""From a SWE-bench per-repo/version environment entry to a `RepoSpec` mapping.

**This module ports no SWE-bench constants.** It defines the shape of
`eval/harness/swebench_specs.json` (a vendored snapshot the maintainer generates
once), a strict loader for it, and the pure function that turns one entry into the
mapping `verify.spec.spec_from_mapping` consumes. Nothing about any repository's
real pins lives in this file; a fixture table in the tests stands in for them.

The snapshot format (`schema_version` 1)::

    {"schema_version": 1,
     "specs": {"<owner>/<name>": {"<version>": {
         "python": "3.9",                       # required, "X.Y"
         "install": "pip install -e .",         # optional; default `pip install -e .`
         "pre_install": ["..."],                # optional, one-line shell commands
         "pip_packages": ["numpy==1.23.0"],     # optional, requirement strings
         "apt_pkgs": ["libxext6"],              # optional, system packages
         "packages": "requirements.txt"         # optional; only that value is portable
     }}}}

**Unknown keys are refused, not ignored.** SWE-bench entries carry things this
harness deliberately does not port -- `test_cmd` (our plugin runs pytest itself),
`eval_commands`, `env_patches`, `no_use_env` -- and a generator that let them
through would make "ported" mean "partly, silently". The generator below keeps
only the keys above; a fixture that adds another fails loudly at load.

Generating it (maintainer, once; needs network; `uvx` so `uv.lock` never changes).
**This snippet was written without network access and has NOT been run**: the
constant names are from memory of the `swebench` package, so adjust it on a
`KeyError`. The loader validates whatever it writes::

    uvx --from swebench python - <<'PY' > eval/harness/swebench_specs.json
    import json
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    REPOS = ["pytest-dev/pytest", "pylint-dev/pylint", "psf/requests", "pydata/xarray",
             "sphinx-doc/sphinx", "mwaskom/seaborn", "pallets/flask"]
    def entry(raw):
        out = {"python": str(raw["python"])}
        for key in ("install", "packages"):
            if raw.get(key):
                out[key] = raw[key]
        for key in ("pre_install", "pip_packages"):
            if raw.get(key):
                out[key] = list(raw[key])
        if raw.get("apt-pkgs"):
            out["apt_pkgs"] = list(raw["apt-pkgs"])
        return out
    specs = {repo: {version: entry(raw) for version, raw in MAP_REPO_VERSION_TO_SPECS[repo].items()}
             for repo in REPOS}
    print(json.dumps({"schema_version": 1, "specs": specs}, indent=1, sort_keys=True))
    PY

**Install order** (`spec_for`), chosen to reproduce SWE-bench's own: `packages`
(a requirements file), `pip_packages`, `pre_install`, `install`. Then pytest *last*:
every `pytest` requirement from `pip_packages` is held back and installed after
`install`, so nothing the project's own dependencies pull in can move the pin; and
if none was pinned a bare `pip install pytest` follows, which is a no-op when the
project already provided one (and is what makes the plugin runnable when nothing
did). A pin is never applied *before* an editable install of pytest itself, where
it would be overridden anyway.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from verify.errors import SpecError
from verify.spec import spec_from_mapping

SCHEMA_VERSION = 1

#: Where the maintainer's snapshot lives. Not shipped: see the module docstring.
DEFAULT_SPECS_PATH = Path(__file__).with_name("swebench_specs.json")

_ENTRY_KEYS = frozenset({"python", "install", "pre_install", "pip_packages", "apt_pkgs", "packages"})
_PYTHON_VERSION = re.compile(r"(\d+)\.(\d+)")
_BASE_IMAGE = re.compile(r"python:(\d+)\.(\d+)-slim")
#: The requirement's distribution name: everything before a specifier, extra or marker.
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_APT = re.compile(r"\bapt(?:-get)?\b")
_DEFAULT_INSTALL = "pip install -e ."

#: SWE-bench's `packages: requirements.txt` means a file its own tooling assembles
#: from the repository's requirement files; it is not a tracked file, and for these
#: repositories nothing named `requirements.txt` exists at the base commit
#: (`pip install -r requirements.txt` fails with "Could not open requirements file").
#: Each entry names the tracked file that carries the same test dependencies.
#: This is an approximation, and gold validation is what checks it: an environment
#: that cannot make the curated fail-to-pass tests red and then green is dropped.
_REQUIREMENT_FILES = {
    "pallets/flask": "requirements/tests.txt",
    "pylint-dev/pylint": "requirements_test_min.txt",
}

#: Requirements SWE-bench's snapshot never needed because its environments were
#: resolved when the dependency was still bundled: sphinx 3.x imports `roman`, which
#: docutils stopped vendoring, so a fresh resolve fails at startup with
#: "No module named 'roman'". Added to the pins installed before the project.
#: Keyed by repository, then by the release prefix it applies to: the 4.x instances
#: that already pass gold validation do not need it, and changing a validated
#: environment would leave it unvalidated.
_EXTRA_PIP_PACKAGES = {"sphinx-doc/sphinx": (("3.", "roman"),)}

#: pytest's own suite needs its `pytester` plugin (the `testdir` fixture), which its
#: `tox.ini` loads through `addopts = ... -p pytester`. The sandbox clears addopts so
#: a repository cannot change how the run behaves, which also drops that, and over a
#: thousand tests then error with "fixture 'testdir' not found" -- among them the
#: curated fail-to-pass ones. The plugin is asked for by name instead.
_EXTRA_PYTEST_ARGS = {"pytest-dev/pytest": ["-p", "pytester"]}

#: A plugin installed into the environment can be loaded into pytest's own *inner*
#: sessions, which its suite starts in process. The `typeguard` plugin that setuptools
#: vendors registers an ini option of type "string", which pytest before 6.0 asserts
#: against, so every inner session crashed in `pytest_addoption` and over a thousand
#: tests failed (the curated fail-to-pass ones among them). pytest's suite loads what
#: it needs by name, so entry-point autoload is switched off for it.
_DISABLE_PLUGIN_AUTOLOAD = frozenset({"pytest-dev/pytest"})

#: Distributions that derive their version from git (`setuptools_scm`). The export
#: has no `.git`, so their editable install fails with "unable to detect version"
#: unless the version is given. The value is the SWE-bench release the instance
#: belongs to, which is a fixed property of the instance and says nothing about
#: the fix. The variable is per distribution: the generic
#: `SETUPTOOLS_SCM_PRETEND_VERSION` would also reach every dependency built from
#: source during the same install.
#: Each maps to (distribution name for the variable, the file the install writes
#: the version into). That file is generated inside the tree, so it is also listed
#: as a `generated_files` entry: every run mounts its export over the tree, which
#: hides it, and pytest imports it unconditionally.
_SCM_DISTRIBUTIONS = {"pytest-dev/pytest": ("PYTEST", "src/_pytest/_version.py")}

#: Python versions whose `python:X.Y-slim` images sit on a Debian release whose apt
#: repositories have been archived, so any `apt-get` in the build fails.
_MIN_PYTHON_WITH_APT = (3, 8)


class SpecgenError(ValueError):
    """A snapshot is missing or malformed, or an entry cannot be ported without a guess."""


def _commands(value: object, name: str, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SpecgenError(f"{where}: {name} must be a list of strings, got {value!r}")
    for item in value:
        if not item.strip():
            raise SpecgenError(f"{where}: {name} holds an empty entry")
        if "\n" in item or "\r" in item:
            raise SpecgenError(f"{where}: {name} entry spans multiple lines: {item!r}")
    return list(value)


def _validate_entry(entry: object, where: str) -> dict[str, Any]:
    """The entry, checked against the snapshot schema. Returns it unchanged."""
    if not isinstance(entry, dict):
        raise SpecgenError(f"{where}: entry must be an object, got {type(entry).__name__}")
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise SpecgenError(
            f"{where}: unknown key(s) {', '.join(unknown)}; the snapshot keeps only "
            f"{', '.join(sorted(_ENTRY_KEYS))} (everything else is deliberately not ported)"
        )
    python = entry.get("python")
    if not isinstance(python, str) or not _PYTHON_VERSION.fullmatch(python):
        raise SpecgenError(f"{where}: python must be a 'X.Y' string, got {python!r}")
    if "install" in entry:
        install = entry["install"]
        if not isinstance(install, str) or not install.strip() or "\n" in install or "\r" in install:
            raise SpecgenError(f"{where}: install must be a one-line command, got {install!r}")
    for name in ("pre_install", "pip_packages", "apt_pkgs"):
        if name in entry:
            _commands(entry[name], name, where)
    for package in entry.get("apt_pkgs", []):
        if re.search(r"\s", package):
            raise SpecgenError(f"{where}: apt_pkgs entry {package!r} contains whitespace")
    if "packages" in entry and not isinstance(entry["packages"], str):
        raise SpecgenError(f"{where}: packages must be a string, got {entry['packages']!r}")
    return entry


def load_swebench_specs(path: Path | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """Read and validate the vendored snapshot: `{repo: {version: entry}}`.

    A missing file is an error with the instruction to generate it, never an
    empty table: an empty table would make every lookup an "unknown repo" and
    look like the instances were wrong rather than the setup being unfinished.
    """
    path = Path(path) if path is not None else DEFAULT_SPECS_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SpecgenError(
            f"{path}: the SWE-bench spec snapshot does not exist. Generate it once with the "
            f"snippet in the docstring of harness.specgen (it is vendored, not computed at run time)"
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecgenError(f"{path}: cannot read: {type(exc).__name__}: {exc}") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise SpecgenError(f"{path}: not valid JSON: {exc}") from exc

    if not isinstance(document, dict) or set(document) != {"schema_version", "specs"}:
        raise SpecgenError(f"{path}: top level must be an object with exactly 'schema_version' and 'specs'")
    version = document["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise SpecgenError(f"{path}: schema_version is {version!r}, this code reads {SCHEMA_VERSION}")
    specs = document["specs"]
    if not isinstance(specs, dict) or not specs:
        raise SpecgenError(f"{path}: 'specs' must be a non-empty object")

    for repo, versions in specs.items():
        if not isinstance(versions, dict) or not versions:
            raise SpecgenError(f"{path}: {repo!r} must map versions to entries")
        for release, entry in versions.items():
            _validate_entry(entry, f"{path}: {repo}@{release}")
    return specs


def _requirement_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME.match(requirement)
    return match.group(1).lower().replace("_", "-") if match else ""


def spec_for(repo: str, version: str, table: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> dict[str, Any]:
    """The `RepoSpec` mapping (no `key`) for one SWE-bench repo and version.

    Raises `SpecgenError` for an unknown repo or version, and for an entry that
    would need a guess: a conda `environment.yml`, a `pip_packages` entry that is
    really an option (`--pre`, `-r x`). The result has already been through
    `verify.spec.spec_from_mapping`, so `select_instances` cannot write a spec the
    pipeline would then refuse; it is returned as the plain JSON mapping, since
    that is what `InstanceSpec.spec` stores.
    """
    where = f"{repo}@{version}"
    versions = table.get(repo)
    if versions is None:
        raise SpecgenError(f"{where}: repo is not in the SWE-bench spec snapshot")
    raw = versions.get(version)
    if raw is None:
        known = ", ".join(sorted(versions)[:12])
        raise SpecgenError(f"{where}: version is not in the snapshot for {repo} (known: {known})")
    entry = _validate_entry(raw, where)

    packages = entry.get("packages")
    if packages not in (None, "", "requirements.txt"):
        raise SpecgenError(
            f"{where}: packages={packages!r} needs a conda environment, which a pip-based "
            f"image cannot reproduce; the instance is excluded rather than approximated"
        )

    pip_packages: list[str] = entry.get("pip_packages", [])
    for requirement in pip_packages:
        if requirement.lstrip().startswith("-"):
            raise SpecgenError(f"{where}: pip_packages entry {requirement!r} is an option, not a requirement")
    pytest_pins = [r for r in pip_packages if _requirement_name(r) == "pytest"]
    others = [r for r in pip_packages if _requirement_name(r) != "pytest"]
    others += [
        package
        for prefix, package in _EXTRA_PIP_PACKAGES.get(repo, ())
        if version.startswith(prefix) and package not in others
    ]

    commands: list[str] = []
    if packages == "requirements.txt":
        commands.append(f"pip install -r {shlex.quote(_REQUIREMENT_FILES.get(repo, 'requirements.txt'))}")
    if others:
        # Quoted: `numpy<1.24` is a shell redirect unquoted.
        commands.append("pip install " + " ".join(shlex.quote(r) for r in others))
    commands.extend(entry.get("pre_install", []))
    install = entry.get("install") or _DEFAULT_INSTALL
    scm = _SCM_DISTRIBUTIONS.get(repo)
    if scm is not None:
        install = f"SETUPTOOLS_SCM_PRETEND_VERSION_FOR_{scm[0]}={shlex.quote(version)} {install}"
    commands.append(install)
    if pytest_pins:
        commands.append("pip install " + " ".join(shlex.quote(r) for r in pytest_pins))
    else:
        commands.append("pip install pytest")

    mapping: dict[str, Any] = {"base_image": f"python:{entry['python']}-slim", "install": commands}
    if entry.get("apt_pkgs"):
        mapping["system_packages"] = list(entry["apt_pkgs"])
    if repo in _DISABLE_PLUGIN_AUTOLOAD:
        mapping["disable_plugin_autoload"] = True
    if repo in _EXTRA_PYTEST_ARGS:
        mapping["extra_pytest_args"] = list(_EXTRA_PYTEST_ARGS[repo])
    if scm is not None:
        mapping["generated_files"] = {scm[1]: f"version = {version!r}\n"}
    try:
        spec_from_mapping(where, mapping)
    except SpecError as exc:
        raise SpecgenError(f"{where}: {exc}") from exc
    return mapping


def python_version(spec: Mapping[str, Any]) -> tuple[int, int]:
    """`(3, 9)` from `base_image: python:3.9-slim`."""
    match = _BASE_IMAGE.fullmatch(str(spec.get("base_image", "")))
    if match is None:
        raise SpecgenError(f"base_image {spec.get('base_image')!r} is not a python:X.Y-slim image")
    return int(match[1]), int(match[2])


def needs_system_packages(spec: Mapping[str, Any]) -> bool:
    """Does building this environment run `apt`? Either as `system_packages` or inside a command."""
    if spec.get("system_packages"):
        return True
    return any(_APT.search(command) for command in spec.get("install", ()))


def environment_rejection(spec: Mapping[str, Any]) -> str | None:
    """Why this environment cannot be built reliably today, or None.

    `python:3.7-slim` and older sit on Debian releases whose apt repositories have
    been archived, so an `apt-get` in the build fails for reasons unrelated to the
    repository -- an instrument failure that would otherwise be found only at the
    gold run, hours later. Old Python with *no* system packages builds fine.
    """
    version = python_version(spec)
    if version < _MIN_PYTHON_WITH_APT and needs_system_packages(spec):
        return (
            f"python {version[0]}.{version[1]} needs system packages and its Debian image's "
            f"apt repositories are archived"
        )
    return None
