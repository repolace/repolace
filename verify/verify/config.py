"""Every value the sandbox is launched with, in one place.

These flags are the security control, not decoration. A typo in one of them
disables it silently, which is why `build_run_argv` is a pure function and why
its test asserts each flag individually.
"""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DockerLimits:
    memory: str = "2g"
    #: Must equal `memory`. If swap is larger, a container can exceed the memory
    #: cap by swapping -- the single flag most often got wrong, and it fails
    #: open rather than closed.
    memory_swap: str = "2g"
    cpus: str = "2.0"
    #: Set when a suite shards on os.cpu_count(), which reports host CPUs
    #: regardless of --cpus.
    cpuset_cpus: str | None = None
    pids_limit: int = 512
    nofile: int = 4096
    tmpfs_size: str = "256m"
    #: The daemon's json log is on the host disk and unbounded by default. A
    #: verbose suite would otherwise fill the volume Postgres lives on.
    log_max_size: str = "10m"
    log_max_file: int = 1


@dataclass(frozen=True)
class DockerConfig:
    docker_binary: str = "docker"
    #: "runsc" adopts gVisor. One field, deliberately: the whole point of the
    #: semantic seam is that stronger isolation is a config change rather than a
    #: new backend. Untested until gVisor is actually installed.
    runtime: str | None = None
    base_image: str = "python:3.12-slim"
    image_prefix: str = "repolace-verify"
    user: str = "65534:65534"
    run_timeout_seconds: float = 1800.0
    build_timeout_seconds: float = 2400.0
    #: Bytes of container output kept. Bounds worker memory; the daemon's own
    #: log cap bounds the disk.
    capture_limit: int = 256 * 1024
    limits: DockerLimits = field(default_factory=DockerLimits)


#: Where the plugin and the report live inside the container.
PLUGIN_DIR = "/opt/repolace"
PLUGIN_MODULE = "_repolace_report"
REPORT_PATH = "/results/report.jsonl"
WORKDIR = "/repo"
RESULTS_DIR = "/results"
#: Where a scratch script is mounted. Its own directory rather than `/repo`, so
#: the script never appears in the tree the host exports or diffs. The name is
#: deliberately one no repository has: the script's directory is `sys.path[0]`,
#: ahead of `PYTHONPATH=/repo`, so a script called `main.py` would shadow -- or be
#: re-imported as -- a repository module of the same name.
SCRIPT_PATH = "/scratch/_repolace_script.py"

#: The environment variable carrying the per-run nonce into the container. Read by
#: the plugin, which stamps it into the report so the host can tell this run's
#: report from any other run's.
RUN_NONCE_ENV_VAR = "REPOLACE_RUN_NONCE"

#: Container environment the sandbox sets for itself. A spec's `extra_env` may not
#: name any of them: each one carries a decision (where the report goes, what
#: identifies the run, what is importable), and an operator typo that overrode one
#: would fail open, as an unexplained unscoreable run or a quietly different tree.
RESERVED_ENV = frozenset({"PYTHONPATH", "HOME", "REPOLACE_REPORT_PATH", RUN_NONCE_ENV_VAR})

#: What an environment variable *name* may look like. `docker run -e NAME=VALUE` splits
#: on the first `=`, so a name like `HOME=/evil` is not a name at all: it emits
#: `-e HOME=/evil=v`, which sets HOME to `/evil=v` *after* the sandbox's own `-e HOME=/tmp`
#: and wins. Exact-key comparison against `RESERVED_ENV` cannot see that, so the name
#: itself is validated, `fullmatch`ed (never `$`, which also matches before a trailing newline).
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
