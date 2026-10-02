"""`build_toolbox`: the agent's nine tools, assembled.

Lives here rather than in `base.py` because `base.py` is a frozen contract; the
package re-exports this one (`repolace_agents.tools.build_toolbox`).

**The tool list is stable.** `run_python` and `run_tests` are always present, and
answer "the sandbox is unavailable" when the context has no sandbox callable.
Leaving them out instead would change the tool schemas -- and so the prompt prefix
the gateway caches -- between a run with a sandbox and one without, for no benefit:
a model told a tool is unavailable stops calling it just as surely.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from repolace_agents.tools.base import ToolBox, ToolContext, ToolError, ToolOutcome, ToolSpec
from repolace_agents.tools.files import CreateFile, EditFile, ListDir, ReadFile
from repolace_agents.tools.gitproc import require_prlimit
from repolace_agents.tools.paths import require_text
from repolace_agents.tools.sandbox import RunPython, RunTests
from repolace_agents.tools.search import Grep, SearchCode
from repolace_agents.tools.wording import TESTS_NOT_SHOWN

MAX_SUMMARY_CHARS = 4000

#: Control characters other than tab, newline and carriage return. An escape sequence or NUL in
#: text the model wrote reaches a human's terminal or a PR body later.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Submit:
    """Ends the run. The summary is the model's own account and stays untrusted data."""

    def __init__(self) -> None:
        self.spec = ToolSpec(
            name="submit",
            description=(
                "Call this when the issue is fixed, with a short summary of what you changed and why. "
                f"{TESTS_NOT_SHOWN} It ends your work: your change is then checked against all of the "
                "tests. Do not call it before you have checked your change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY_CHARS, "description": "What you changed and why."},
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        summary = args["summary"]
        require_text("summary", summary)  # it is stored on the task row
        if _CONTROL_CHARS.search(summary):
            raise ToolError("the summary may not contain control characters (newlines and tabs are fine)")
        if not summary.strip():
            raise ToolError("the summary is blank; say what you changed and why")
        return ToolOutcome(content="submitted", submitted=True, summary=summary)


def build_toolbox(ctx: ToolContext) -> ToolBox:
    """The agent's tools, built from a context: one `Tool` per capability.

    By name, in the order the model sees them: `search_code`, `read_file`, `grep`,
    `list_dir`, `edit_file`, `create_file`, `run_python`, `run_tests`, `submit`.
    Every limit comes from `ctx.limits`, and the box is built with its
    `max_output_chars` so the cap the tools budget against is the cap the box enforces.

    **A pipeline must pass its own `ctx.is_protected`.** The context default is the
    baseline-blind `is_protected_path`: it knows the path heuristic and not which files
    pytest collected at the base commit, so a test file collected through a custom
    `python_files` would be editable and the scorer would then discard the whole patch.
    The pipeline supplies a closure over the baseline's `collected_files` and `conftests`
    minus the benchmark overlay's paths.
    """
    require_prlimit()  # grep runs git under a memory and CPU limit; fail now, not on the first search
    return ToolBox(
        [
            SearchCode(ctx),
            ReadFile(ctx),
            Grep(ctx),
            ListDir(ctx),
            EditFile(ctx),
            CreateFile(ctx),
            RunPython(ctx),
            RunTests(ctx),
            Submit(),
        ],
        max_output_chars=ctx.limits.max_output_chars,
    )
