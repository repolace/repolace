"""Rendering the image that a repository's suite runs in, and naming it.

Pure functions over plain data: no daemon, no filesystem writes, no subprocess.
That is what lets the interesting half -- which commands run, and when the image
has to be rebuilt -- be tested without Docker installed at all.

The image holds the *environment*, never the tree under test. Source is copied
in at build time because an editable install needs something to point at, and
then bind-mounted over at run time, one export per attempt. Both live at
``/repo`` so the editable install keeps resolving across the swap. This is
CLAUDE.md's "install dependencies once into an image, then start a fresh
container per run from that image": reusing one live container across attempts
is faster and lets a stale ``.pyc`` or a mutated fixture database leak between
them, which corrupts the benchmark quietly.
"""

import hashlib
import shlex
from collections.abc import Sequence
from pathlib import Path

from verify.config import PLUGIN_DIR, PLUGIN_MODULE, WORKDIR
from verify.errors import SpecError
from verify.protocol import RepoSpec

#: Where the build context puts each piece. Referenced by both the rendered
#: Dockerfile and the backend that assembles the context, so they cannot drift.
CONTEXT_SOURCE_DIR = "source"
CONTEXT_PLUGIN_PATH = f"plugin/{PLUGIN_MODULE}.py"

#: Top-level files whose contents decide what gets installed. Hashed into the
#: image tag, so an image is reused across attempts and across tasks but rebuilt
#: the moment a dependency changes. Top level only, deliberately: it is what
#: `heuristic_install` reads, and walking the tree would make the key depend on
#: the agent's own edits and rebuild the image on every attempt.
MANIFEST_NAMES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "poetry.lock",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "constraints.txt",
    "requirements.txt",
    "requirements-test.txt",
    "requirements_test.txt",
    "test-requirements.txt",
    "requirements-dev.txt",
)

#: Enough of the digest to make a collision implausible while keeping the tag
#: readable in `docker images` output.
_KEY_LENGTH = 16

#: A tag component may hold letters, digits, and `_.-`, and may not lead with a
#: separator. Repository keys are `owner/name`, which is none of those things.
_TAG_SAFE = str.maketrans({"/": "_", ":": "_", "@": "_", " ": "_"})


def _check_command(command: str, spec_key: str) -> str:
    """A command has to be one Dockerfile line, or it silently becomes two.

    A newline inside a `RUN` turns the remainder into a fresh instruction --
    usually a syntax error, but not always, and "not always" is the case worth
    refusing rather than discovering.
    """
    if "\n" in command or "\r" in command:
        raise SpecError(f"{spec_key}: install command spans multiple lines: {command!r}")
    if not command.strip():
        raise SpecError(f"{spec_key}: empty install command")
    return command.strip()


def render_dockerfile(spec: RepoSpec, install: Sequence[str]) -> str:
    """The Dockerfile text for one repository's environment."""
    lines = [
        f"FROM {spec.base_image}",
        # PIP_ROOT_USER_ACTION because the build runs as root and pip's warning
        # about it is pure noise in a build log somebody reads only when it
        # failed. PYTHONDONTWRITEBYTECODE so the run leaves no `__pycache__`
        # owned by the sandbox uid in the export -- the host has to delete that
        # tree afterwards and cannot chmod files it does not own.
        "ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \\",
        "    PIP_ROOT_USER_ACTION=ignore \\",
        "    PYTHONUNBUFFERED=1 \\",
        "    PYTHONDONTWRITEBYTECODE=1",
    ]

    if spec.system_packages:
        packages = " ".join(shlex.quote(p) for p in spec.system_packages)
        lines += [
            # One RUN, and the lists deleted in it: a separate RUN would keep
            # the 40 MB apt cache in its own layer whatever the next line does.
            "RUN apt-get update \\",
            f" && apt-get install -y --no-install-recommends {packages} \\",
            " && rm -rf /var/lib/apt/lists/*",
        ]

    # Before the source, so an edit to the tree does not invalidate this layer.
    lines.append(f"COPY {CONTEXT_PLUGIN_PATH} {PLUGIN_DIR}/{PLUGIN_MODULE}.py")

    lines += [
        f"WORKDIR {WORKDIR}",
        f"COPY {CONTEXT_SOURCE_DIR}/ {WORKDIR}/",
    ]
    lines += [f"RUN {_check_command(command, spec.key)}" for command in install]

    # No CMD. The pytest invocation belongs in `build_run_argv`, where every
    # flag is inspectable and asserted individually, rather than baked into an
    # image where a change means a rebuild and a diff nobody reads.
    return "\n".join(lines) + "\n"


def _manifest_digest(source_dir: Path) -> list[tuple[str, str]]:
    """Hash each dependency manifest that exists. Missing ones are recorded as absent.

    Recorded rather than skipped: a repository that *deletes* its
    `requirements.txt` has changed what gets installed just as much as one that
    edits it, and a key built only from present files would not notice.
    """
    digests = []
    for name in MANIFEST_NAMES:
        candidate = source_dir / name
        try:
            digests.append((name, hashlib.sha256(candidate.read_bytes()).hexdigest()))
        except OSError:
            digests.append((name, "-"))
    return digests


def image_cache_key(spec: RepoSpec, install: Sequence[str], source_dir: Path) -> str:
    """What makes two builds the same build.

    Covers the rendered Dockerfile (so the base image, the system packages and
    the install commands are all in it), the plugin's own bytes (it is COPYed
    in, so a change to it must not be served from a stale image), and the
    dependency manifests.

    Deliberately *not* covering the rest of the tree. The agent edits source on
    every attempt; keying on it would rebuild the image per attempt and throw
    away the one property that makes attempts comparable -- an identical
    environment, which is what lets `score` blame a newly failing suite on the
    patch rather than on the install step.
    """
    digest = hashlib.sha256()
    digest.update(render_dockerfile(spec, install).encode("utf-8"))
    digest.update(plugin_source().read_bytes())
    for name, value in _manifest_digest(source_dir):
        digest.update(f"{name}={value}\n".encode("utf-8"))
    return digest.hexdigest()[:_KEY_LENGTH]


def image_tag(prefix: str, spec: RepoSpec, cache_key: str) -> str:
    """`prefix:owner_name-<key>`. Readable in `docker images`, unique per environment."""
    return f"{prefix}:{spec.key.translate(_TAG_SAFE)}-{cache_key}"


def plugin_source() -> Path:
    """Where the plugin lives on this machine.

    Resolved through the package rather than a path relative to this file, so it
    keeps working from an installed wheel as well as from the workspace
    checkout. Never imported -- importing it would register its hooks into
    repolace's own test run, which its module docstring warns about.
    """
    from verify import plugin

    return Path(plugin.__file__).parent / f"{PLUGIN_MODULE}.py"
