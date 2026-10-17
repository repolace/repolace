"""The three tools only the Phase 2 roles have: `submit_plan`, `submit_review` and `rewind`.

Every other role tool is one of the nine, taken from the run's own box with `ToolBox.subset`, so
a role's reads and writes are bounded by exactly the guards the single agent's are. These three
end a role's loop or move the tree, and each is as narrow as its job:

* `submit_plan` and `submit_review` carry only text and one enumerated verdict. The text is the
  model's own and stays untrusted: the next role reads it inside a `plan` or `review` block.
* `rewind` takes an attempt **number**, never a commit: it resolves the number against the commits
  the scorer recorded for this run and refuses anything else, so the debugger can return to a
  state that was actually scored but cannot point the tree at an arbitrary commit. The reset itself
  is a callable the graph supplies (`GitRepo.reset_hard`, the same call the pipeline's own
  `rewind_to` makes), which keeps git out of this module and a fake in reach of a test.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from repolace_agents.tools.base import ToolError, ToolOutcome, ToolSpec
from repolace_agents.tools.paths import require_text

#: The tools' names: the planner's and the reviewer's exit tools, and the debugger's reset.
SUBMIT_PLAN = "submit_plan"
SUBMIT_REVIEW = "submit_review"
REWIND = "rewind"

MAX_PLAN_CHARS = 8000
MAX_COMMENTS_CHARS = 4000

APPROVE = "approve"
REQUEST_CHANGES = "request_changes"
VERDICTS = (APPROVE, REQUEST_CHANGES)

#: Control characters other than tab, newline and carriage return, as `submit` refuses them:
#: the text is shown to another model and may later reach a human's terminal.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _checked_text(name: str, value: str, *, blank_message: str) -> str:
    require_text(name, value)
    if _CONTROL_CHARS.search(value):
        raise ToolError(f"the {name} may not contain control characters (newlines and tabs are fine)")
    if not value.strip():
        raise ToolError(blank_message)
    return value


class SubmitPlan:
    """Ends the planner's loop. The plan is handed to the editor as data."""

    def __init__(self) -> None:
        self.spec = ToolSpec(
            name=SUBMIT_PLAN,
            description=(
                "Call this when you have found the cause, with the plan the editor should follow: which source "
                "files and functions to change, what to change and why, and how to check it. It ends your work."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan": {"type": "string", "minLength": 1, "maxLength": MAX_PLAN_CHARS, "description": "The plan."},
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        plan = _checked_text("plan", args["plan"], blank_message="the plan is blank; say what to change and why")
        return ToolOutcome(content="plan submitted", submitted=True, summary=plan)


class SubmitReview:
    """Ends the reviewer's loop with a verdict. Holds the verdict for the node that ran the loop.

    A fresh instance per review: the node reads `verdict` and `comments` after the loop, and an
    instance carried over from an earlier review would answer for a review that did not happen.
    """

    def __init__(self) -> None:
        self.verdict: str | None = None
        self.comments: str = ""
        self.spec = ToolSpec(
            name=SUBMIT_REVIEW,
            description=(
                "Call this with your verdict on the change. `approve` if it fixes the cause of the issue and does "
                "nothing else; `request_changes`, with comments saying what is wrong and what to do instead, if "
                "it does not. A request for changes gives the editor one more attempt. It ends your work."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": list(VERDICTS), "description": "approve or request_changes"},
                    "comments": {
                        "type": "string",
                        "maxLength": MAX_COMMENTS_CHARS,
                        "description": "What is wrong and what to do instead. Required for request_changes.",
                    },
                },
                "required": ["verdict"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        verdict = args["verdict"]
        comments = args.get("comments", "")
        require_text("comments", comments)
        if _CONTROL_CHARS.search(comments):
            raise ToolError("the comments may not contain control characters (newlines and tabs are fine)")
        if verdict == REQUEST_CHANGES and not comments.strip():
            raise ToolError("a request for changes needs comments saying what is wrong and what to do instead")
        self.verdict, self.comments = verdict, comments
        return ToolOutcome(content="review submitted", submitted=True, summary=verdict)


class Rewind:
    """Resets the working tree to a scored attempt's commit. The debugger's alone."""

    def __init__(self, scored: Mapping[int, str], reset: Callable[[str], Awaitable[None]]) -> None:
        #: Attempt number -> the commit the scorer ran that attempt's suite against. Copied, so a
        #: later attempt cannot change which commits this instance accepts.
        self._scored = dict(scored)
        self._reset = reset
        self.spec = ToolSpec(
            name=REWIND,
            description=(
                "Reset the working tree to exactly the committed state of an earlier scored attempt, discarding "
                "every edit made after it. Takes the attempt's number; only attempts whose tests were run can be "
                "rewound to."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "attempt": {"type": "integer", "minimum": 1, "description": "The number of a scored attempt."},
                },
                "required": ["attempt"],
                "additionalProperties": False,
            },
        )

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        number = args["attempt"]
        sha = self._scored.get(number)
        if sha is None:
            available = ", ".join(str(n) for n in sorted(self._scored)) or "none yet"
            raise ToolError(
                f"attempt {number} was not scored, so there is nothing to rewind to; the scored attempts are: {available}"
            )
        await self._reset(sha)
        return ToolOutcome(
            f"The working tree is now exactly as it was at scored attempt {number}; every edit made after it is gone."
        )
