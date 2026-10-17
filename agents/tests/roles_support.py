"""Builders for the Phase 2 roles tests: a nine-tool fake box, a fake git, scripted role replies.

`<area>_support.py`, globally unique, for the reason `agents_support.py` gives. The roles take
slices of the run's box by name (`ToolBox.subset`), so the four-tool `scripted_toolbox` is not
enough here: this box has all nine names the real one has, over the same `FunctionTool` fakes.
"""

from collections.abc import Callable, Mapping
from typing import Any

from agents_support import (
    FunctionTool,
    ScriptedLLM,
    ScriptedVerifier,
    make_deps,
    object_schema,
    reply,
    suite,
    tool_call,
)
from repolace_agents.contracts import AgentLimits, AgentResult
from repolace_agents.graph import run_graph
from repolace_agents.tools.base import ToolBox, ToolOutcome

NONCE = "n0nce1234"
A, B = "tests/test_a.py::test_one", "tests/test_a.py::test_two"
#: Enough steps that every role has more than one: 4 to plan, 2 to review, 10 to edit.
ROLE_LIMITS = AgentLimits(max_attempts=3, max_steps_per_attempt=16, roles=True)
#: One attempt: a clean result goes to no reviewer and a red one to no debugger, so a test of the
#: planner or the editor alone needs no reply for either.
ONE_ATTEMPT = AgentLimits(max_attempts=1, max_steps_per_attempt=16, roles=True)
NINE = ("search_code", "read_file", "grep", "list_dir", "edit_file", "create_file", "run_python", "run_tests", "submit")
BASE_SHA = "b" * 40
DIFF = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n"


def role_toolbox(result_for: Callable[[str, Mapping[str, Any]], str] | None = None):
    """A real `ToolBox` over nine fake tools named like the real ones. Returns the box and the tools by name."""
    result_for = result_for or (lambda name, args: f"{name} output for {dict(args)}")
    text = {"type": "string"}
    properties: dict[str, dict[str, Any]] = {
        "search_code": {"query": text},
        "read_file": {"path": text},
        "grep": {"pattern": text},
        "list_dir": {"path": text},
        "edit_file": {"path": text, "new": text},
        "create_file": {"path": text, "content": text},
        "run_python": {"code": text},
        "run_tests": {"targets": {"type": "array", "items": text}},
        "submit": {"summary": {"type": "string", "maxLength": 4000}},
    }

    def make(name: str) -> FunctionTool:
        async def handler(args: Mapping[str, Any]) -> ToolOutcome:
            if name == "submit":
                return ToolOutcome("submitted", submitted=True, summary=args["summary"])
            return ToolOutcome(result_for(name, args))

        return FunctionTool(name, handler, parameters=object_schema(properties[name]), description=f"fake {name}")

    tools = {name: make(name) for name in NINE}
    return ToolBox(list(tools.values())), tools


class FakeGit:
    """The three git calls the roles graph makes, recorded. `head_sha` is read once, before the planner."""

    def __init__(self, head: str = BASE_SHA, diff: str = DIFF) -> None:
        self.head, self.diff = head, diff
        self.diffs_from: list[str] = []
        self.resets: list[str] = []
        self.head_reads = 0

    async def head_sha(self) -> str:
        self.head_reads += 1
        return self.head

    async def diff_from(self, base_sha: str) -> str:
        self.diffs_from.append(base_sha)
        return self.diff

    async def reset_hard(self, sha: str) -> None:
        self.resets.append(sha)


def plan_reply(plan: str = "Guard the empty case in parse() in src/a.py.", *, id: str | None = None):
    return reply(None, tool_call("submit_plan", {"plan": plan}, id=id))


def review_reply(verdict: str = "approve", comments: str | None = None, *, id: str | None = None):
    args = {"verdict": verdict} if comments is None else {"verdict": verdict, "comments": comments}
    return reply(None, tool_call("submit_review", args, id=id))


def roles_deps(llm: ScriptedLLM, verifier: ScriptedVerifier, *, tools: ToolBox | None = None, **overrides: Any):
    fields = {
        "llm": llm,
        "tools": tools if tools is not None else role_toolbox()[0],
        "verify_attempt": verifier,
        "limits": ROLE_LIMITS,
        "baseline": suite(passed=[A, B]),
        "repo_overview": "src/\n  a.py",
    }
    return make_deps(**{**fields, **overrides})


async def run_roles(llm: ScriptedLLM, verifier: ScriptedVerifier, *, git: FakeGit | None = None, **overrides: Any) -> AgentResult:
    return await run_graph(roles_deps(llm, verifier, **overrides), nonce=NONCE, git=git if git is not None else FakeGit())


def stages(llm: ScriptedLLM) -> list[str]:
    return [call.stage for call in llm.calls]


def tool_names(call) -> list[str]:
    return [schema["function"]["name"] for schema in call.tools or ()]
