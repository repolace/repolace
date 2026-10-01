"""Entry point for `repolace-eval`.

A placeholder so the `[project.scripts]` target resolves from the moment the
dependency set is locked. Every subcommand (`select`, `gold`, `fork`, `enqueue`,
`run`, `report`) lands with the eval streams; none of them exists yet, and an
exit code of 2 -- the conventional "bad invocation" code -- makes a script that
calls one fail loudly rather than succeed having done nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # no subcommands exist yet, so there is nothing to parse
    print(
        "repolace-eval: no subcommands yet; they land with the eval streams "
        "(harness data and harness execution).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
