"""What a tool is, and the box that runs them.

The rule that shapes this module: **a model-visible failure and a bug are
different things, and only the first is allowed to become a tool result.** A
tool that is handed a path that does not exist, a pattern that matches nothing,
or arguments of the wrong type has done nothing wrong -- the *model* did, and the
useful thing is to tell it so in words it can act on. An `AttributeError` inside
a tool is repolace's bug, and turning it into a polite "tool failed" message
would let a broken tool pass for a model that is simply bad at using it -- which
is the same misattribution the benchmark's scoring exists to avoid. So
`ToolError` (and the box's own argument checks) become `is_error` outcomes;
everything else propagates.

The agent's authority is bounded by what its tools can do, not by what its prompt
says (CLAUDE.md, "PR conversation handling"). That is why the argument checks
live here, in code that runs on every call, rather than in a tool description the
model is merely asked to follow.

Imports stay cheap on purpose: no `litellm`, no gateway at runtime, no `rag`.
`ToolCall` is only ever read by attribute, so it is imported for type-checking
alone.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from verify.protocol import ScriptResult, SuiteResult

if TYPE_CHECKING:
    from repolace_agents.contracts import SearchHit
    from repolace_gateway.client import ToolCall


@dataclass(frozen=True)
class ToolSpec:
    """A tool's name, what it is for, and the JSON schema of its arguments."""

    name: str
    description: str
    #: A JSON-schema object: `{"type": "object", "properties": {...}, "required":
    #: [...], "additionalProperties": False}`. What `ToolBox.dispatch` enforces of
    #: it is deliberately minimal -- see there.
    parameters: Mapping[str, Any]

    def schema(self) -> dict[str, Any]:
        """The tool in the function-calling shape the gateway sends to a provider.

        A deep copy, because the result goes to code that is free to annotate or
        rewrite it (cache markers, provider adapters) and a mutation must not
        reach the spec every later call is built from.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(dict(self.parameters)),
            },
        }


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool hands back to the loop."""

    #: What the model reads. Capped by `ToolBox`, whatever the tool returned.
    content: str
    #: The call did not do what was asked and `content` says why. Model-visible
    #: failure only: a bug in the tool raises instead.
    is_error: bool = False
    #: Set by `submit` alone. The loop stops when it sees it.
    submitted: bool = False
    #: The model's own summary of its work, when it submitted. Untrusted model
    #: output wherever it is later shown to a human.
    summary: str | None = None


class ToolError(Exception):
    """An *expected*, model-visible failure: a bad path, no match, a refused edit.

    The message is shown to the model verbatim as an `is_error` outcome, so write
    it for the model -- say what was wrong and what would work -- and never put
    anything in it the model must not see (a host path, a credential, a hidden
    test name). Raise this for things the model got wrong. Raise anything else
    for things repolace got wrong.
    """


class Tool(Protocol):
    spec: ToolSpec

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome: ...


@dataclass(frozen=True)
class ToolLimits:
    """Every size and count a tool is held to, in one place a test can pin.

    Each one bounds either what the model can make the host do (read, write, run)
    or what comes back into the prompt (and so into the bill).
    """

    max_output_chars: int = 8000
    max_read_lines: int = 400
    max_file_bytes: int = 1_000_000
    max_edit_chars: int = 20_000
    max_create_chars: int = 100_000
    max_grep_results: int = 200
    max_targets: int = 20
    default_script_timeout: float = 60.0
    max_script_timeout: float = 120.0


@dataclass(frozen=True)
class ToolContext:
    """What the tools are built from. Everything outside the checkout is a callable.

    The tools never see a database session, a `Verifier`, or a `GitRepo`: the
    pipeline closes over those and hands in functions, so a tool can only do what
    its function does, and a test can hand in a fake.
    """

    #: The agent's working tree. Every path a tool touches resolves inside it.
    checkout: Path
    #: Commit the tree as a checkpoint (`workspace.record_attempt`) and return the
    #: sha, or None when nothing changed. Called by `run_python` and `run_tests`
    #: before they hand the tree to the sandbox, because the export refuses a tree
    #: that differs from HEAD -- and kept in the *tool*, not the verifier, so
    #: `verify` stays git-free.
    checkpoint: Callable[[str], Awaitable[str | None]]
    #: Search the index for `(query, limit)`. The pipeline supplies one that opens
    #: its **own short-lived database session per call**: a session shared with the
    #: pipeline's state writes would be held across a minutes-long agent loop.
    search: Callable[[str, int], Awaitable[Sequence[SearchHit]]]
    #: Run some of the visible tests (`Verifier.run_subset`). None when no sandbox
    #: is available, and the tool then reports that plainly instead of existing.
    run_subset: Callable[[Sequence[str]], Awaitable[SuiteResult]] | None
    #: Run a scratch script `(code, timeout_seconds)` (`Verifier.run_script`).
    run_script: Callable[[str, float], Awaitable[ScriptResult]] | None
    limits: ToolLimits = ToolLimits()


#: JSON-schema type name -> a predicate. `bool` is an `int` subclass in Python and
#: `true` is not an integer in JSON, so the numeric rows exclude it by hand.
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, (list, tuple)),
    "object": lambda v: isinstance(v, Mapping),
}


def _json_type(value: object) -> str:
    """The JSON name of a Python value, for telling the model what it sent."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _check_spec(spec: ToolSpec) -> None:
    """Refuse a spec the box could not enforce, at construction rather than per call.

    A property whose `type` the box does not know would silently go unchecked on
    every call -- the tool author believing the schema protects an argument it
    does not. Better a loud `ValueError` the first time the toolbox is built.
    """
    properties = spec.parameters.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError(f"tool {spec.name!r}: 'properties' must be an object")
    for name, prop in properties.items():
        declared = prop.get("type") if isinstance(prop, Mapping) else None
        if declared is not None and declared not in _TYPE_CHECKS:
            raise ValueError(
                f"tool {spec.name!r}: property {name!r} has unsupported type {declared!r}; "
                f"supported: {', '.join(sorted(_TYPE_CHECKS))}"
            )
    for name in spec.parameters.get("required", ()):
        if name not in properties:
            raise ValueError(f"tool {spec.name!r}: required property {name!r} is not declared")


def _argument_problems(spec: ToolSpec, arguments: Mapping[str, Any]) -> list[str]:
    """Everything wrong with `arguments`, as sentences the model can act on.

    All of it at once rather than the first problem: a model told about one
    mistake per call spends a step on each, and the step cap is per attempt.
    """
    parameters = spec.parameters
    properties: Mapping[str, Any] = parameters.get("properties", {})
    problems: list[str] = []

    missing = [name for name in parameters.get("required", ()) if name not in arguments]
    if missing:
        problems.append(f"missing required argument(s): {', '.join(missing)}")

    if parameters.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            problems.append(
                f"unexpected argument(s): {', '.join(unknown)} "
                f"(this tool accepts: {', '.join(sorted(properties)) or 'no arguments'})"
            )

    for name, value in arguments.items():
        prop = properties.get(name)
        declared = prop.get("type") if isinstance(prop, Mapping) else None
        if declared is not None and not _TYPE_CHECKS[declared](value):
            problems.append(f"argument {name!r} must be {declared}, got {_json_type(value)}")
    return problems


class ToolBox:
    """The tools of one agent run, and the one place a call is checked and run.

    `dispatch` is what makes the agent's authority a property of code. It checks
    the arguments against the tool's schema -- **only minimally**: required keys,
    no unknown keys when `additionalProperties` is false, and the six basic JSON
    types. `enum`, ranges, lengths and the element type of an array are *not*
    checked here; a tool must check anything it depends on itself, and must not
    assume the schema did.
    """

    def __init__(self, tools: Sequence[Tool], *, max_output_chars: int = ToolLimits().max_output_chars) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            name = tool.spec.name
            if name in self._tools:
                raise ValueError(f"duplicate tool name: {name!r}")
            _check_spec(tool.spec)
            self._tools[name] = tool
        self.max_output_chars = max_output_chars

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """Every tool in the order given, ready for `LLMClient.complete(tools=...)`."""
        return [tool.spec.schema() for tool in self._tools.values()]

    async def dispatch(self, call: ToolCall) -> ToolOutcome:
        """Run one tool call and return what the model should read.

        Becomes an `is_error` outcome, with a message written for the model: an
        unknown tool name, a `parse_error` on the call (the model sent something
        that was not a JSON object), arguments that fail the schema check above,
        and a `ToolError` raised by the tool.

        **Propagates, deliberately:** any other exception from a tool, and
        `asyncio.CancelledError`. A bug must not be laundered into a result the
        model then reasons about, and a cancelled task must actually stop.

        Every outcome's content is capped at `max_output_chars`, whoever produced
        it, with a `[truncated N chars]` trailer saying how much was dropped so
        the model knows the output is incomplete rather than short.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return self._cap(
                ToolOutcome(
                    f"unknown tool {call.name!r}; the available tools are: {', '.join(self._tools)}",
                    is_error=True,
                )
            )

        if call.parse_error:
            return self._cap(
                ToolOutcome(
                    f"the arguments for {call.name} were not a valid JSON object ({call.parse_error}); "
                    f"call it again with a single JSON object of arguments",
                    is_error=True,
                )
            )

        arguments = call.arguments
        if not isinstance(arguments, Mapping):
            return self._cap(
                ToolOutcome(
                    f"the arguments for {call.name} must be a JSON object, got {_json_type(arguments)}",
                    is_error=True,
                )
            )

        problems = _argument_problems(tool.spec, arguments)
        if problems:
            return self._cap(
                ToolOutcome(f"invalid arguments for {call.name}: " + "; ".join(problems), is_error=True)
            )

        try:
            outcome = await tool(arguments)
        except ToolError as exc:
            return self._cap(ToolOutcome(str(exc) or f"{call.name} failed", is_error=True))

        if not isinstance(outcome, ToolOutcome):
            # A tool that returns the wrong type is a bug in the tool, not a
            # model-visible failure -- so it is loud, like any other bug.
            raise TypeError(f"tool {call.name!r} returned {type(outcome).__name__}, not ToolOutcome")
        return self._cap(outcome)

    def _cap(self, outcome: ToolOutcome) -> ToolOutcome:
        content = outcome.content
        if len(content) <= self.max_output_chars:
            return outcome
        dropped = len(content) - self.max_output_chars
        return dataclasses.replace(
            outcome, content=f"{content[: self.max_output_chars]}\n[truncated {dropped} chars]"
        )


def build_toolbox(ctx: ToolContext) -> ToolBox:
    """The agent's tools, built from a context: one `Tool` per capability.

    By name: `search_code`, `read_file`, `grep`, `list_dir`, `edit_file`,
    `create_file`, `run_python`, `run_tests` and `submit`. The rules every one of
    them must hold: every path goes through
    `repolace_shared.paths.resolve_within`; `is_protected_path` and anything
    under `.git` is refused for writes; limits come from `ctx.limits`; and the
    box is built as `ToolBox(tools, max_output_chars=ctx.limits.max_output_chars)`.
    """
    raise NotImplementedError("build_toolbox lands in stream B: toolbox")
