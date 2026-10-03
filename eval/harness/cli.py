"""Entry point for `repolace-eval <subcommand>`.

A dispatcher and nothing else. Each subcommand lives in its own module and is
imported **only when it is chosen**: `repolace-eval --help` must work on a machine
where `torch` is not installed or a module has not been merged yet, and `run`
should not pay for the retrieval eval's embedding model. A module that cannot be
imported produces one line on stderr and exit code 2 -- the conventional "bad
invocation" code -- never a traceback, so a script that calls an unavailable
subcommand fails loudly instead of succeeding having done nothing.

Exit codes are the subcommand's own; this file adds only 2 (unknown or
unavailable subcommand).
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Sequence
from types import ModuleType

#: subcommand -> (module that implements it, one line for `--help`). Every module
#: exposes `main(argv) -> int`.
SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "select": ("harness.select_instances", "choose benchmark instances from SWE-bench Verified (network)"),
    "fork": ("harness.bench_repos", "create the private bench-<instance_id> repositories and push base commits (GitHub token)"),
    "gold": ("harness.gold", "analyse two gold runs and write VALIDATION.md"),
    "enqueue": ("harness.enqueue", "enqueue benchmark tasks for a run and write its manifest"),
    "run": ("harness.runner", "run a run's queued tasks, K at a time"),
    "report": ("harness.report", "write the benchmark report from the database"),
    "retrieval": ("harness.retrieval_eval", "evaluate retrieval strategies (loads the embedding model)"),
}

EXIT_UNAVAILABLE = 2


def _usage() -> str:
    width = max(len(name) for name in SUBCOMMANDS)
    lines = ["usage: repolace-eval <subcommand> [args...]", "", "subcommands:"]
    lines += [f"  {name.ljust(width)}  {summary}" for name, (_module, summary) in SUBCOMMANDS.items()]
    lines += ["", "`repolace-eval <subcommand> --help` describes one."]
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> int:
    """Dispatch to a subcommand. `importer` is a test seam."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(_usage(), file=sys.stderr)
        return EXIT_UNAVAILABLE
    name, rest = args[0], args[1:]
    if name in ("-h", "--help", "help"):
        print(_usage())
        return 0
    if name not in SUBCOMMANDS:
        print(f"repolace-eval: unknown subcommand {name!r}; choose one of {', '.join(SUBCOMMANDS)}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    module_name = SUBCOMMANDS[name][0]
    try:
        module = importer(module_name)
    except ImportError as exc:
        missing = exc.name or str(exc)
        reason = (
            f"module {module_name} is not available (not merged or not installed)"
            if missing in (module_name, module_name.split(".")[0])
            else f"importing {module_name} failed: it needs {missing}"
        )
        print(f"repolace-eval: subcommand {name!r} cannot run: {reason}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    handler = getattr(module, "main", None)
    if not callable(handler):
        print(f"repolace-eval: subcommand {name!r} cannot run: {module_name} has no main()", file=sys.stderr)
        return EXIT_UNAVAILABLE
    result = handler(rest)
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
