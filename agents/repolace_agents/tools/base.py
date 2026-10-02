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

Imports stay cheap on purpose: no `litellm`, no `repolace_gateway.client`
(which pulls `litellm` and FastAPI), no `rag`/`retrieval`, no `torch`. The one
heavier import is `verify.scoring`, for `is_protected_path`, which loads
SQLAlchemy and pgvector through `repolace_shared.db.models` -- not torch.
`ToolCall` is only ever read by attribute, so it is imported for type-checking
alone.
"""

from __future__ import annotations

import copy
import dataclasses
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from verify.protocol import ScriptResult, SuiteResult
from verify.scoring import is_protected_path

if TYPE_CHECKING:
    from repolace_agents.contracts import SearchHit
    from repolace_gateway.client import ToolCall


@dataclass(frozen=True)
class ToolSpec:
    """A tool's name, what it is for, and the JSON schema of its arguments."""

    name: str
    description: str
    #: A JSON-schema object: `{"type": "object", "properties": {...}, "required":
    #: [...], "additionalProperties": False}`. Only the keywords `ToolBox`
    #: enforces may appear in it -- the box refuses any other at construction, so
    #: a schema never advertises a bound that nothing checks. See `ToolBox`.
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

    def __post_init__(self) -> None:
        # The loop stops on `submitted`; an error that also says "submitted" would
        # end the run on a call that, by its own account, did not do what was asked.
        if self.is_error and self.submitted:
            raise ValueError("a ToolOutcome cannot be both an error and a submission")


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
    #: Wall-clock bound, in seconds, on one `run_tests` probe. Scripts had a
    #: timeout policy from the start and the probe did not, so a hung visible
    #: suite would have held the step for as long as the sandbox's own default.
    #: **It must reach the sandbox**: `ToolContext.run_subset` takes the targets
    #: and no timeout, so the pipeline builds that callable as
    #: `Verifier.run_subset(..., timeout_seconds=limits.max_probe_seconds)` and a
    #: tool must call it as given rather than implement a timeout of its own. A
    #: probe that hits the bound comes back as `SuiteResult(error=...)`, which the
    #: tool reports to the model like any other sandbox failure.
    max_probe_seconds: float = 300.0


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
    #: Applies `limits.max_probe_seconds` itself -- see there. Backend failures
    #: come back as `SuiteResult(error=...)`, not raised; `VerifierNotReady` is the
    #: only exception, and the tool maps it to a `ToolError`.
    run_subset: Callable[[Sequence[str]], Awaitable[SuiteResult]] | None
    #: Run a scratch script `(code, timeout_seconds)` (`Verifier.run_script`).
    #: Backend failures come back as `ScriptResult(error=...)`, as above.
    run_script: Callable[[str, float], Awaitable[ScriptResult]] | None
    limits: ToolLimits = ToolLimits()
    #: Is the agent's write tool forbidden from this repo-relative path? **Every
    #: write guard calls this, never `is_protected_path` directly**, so the
    #: pipeline can supply a closure over the baseline's `collected_files` and
    #: `conftests` -- without which a file pytest collects through a custom
    #: `python_files` is editable, and the scorer then discards the whole patch.
    #: The default is the baseline-blind function: correct for the heuristic,
    #: weaker for the collected set. Additive and defaulted, so the shape every
    #: stream already builds against stays valid.
    is_protected: Callable[[str], bool] = is_protected_path


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

#: Keywords that describe a property without constraining it. Allowed anywhere
#: and never enforced, because there is nothing to enforce.
_ANNOTATIONS = frozenset({"description", "title", "default", "examples"})

#: The validation keywords `ToolBox` enforces, per declared type. A keyword that
#: is not here for the property's type is refused at construction -- including a
#: real JSON-schema keyword on the wrong type (`minLength` on an integer), which
#: JSON Schema would silently ignore and which is therefore an advertised bound
#: that nothing checks.
_ENFORCED: dict[str, frozenset[str]] = {
    "string": frozenset({"minLength", "maxLength", "enum"}),
    "integer": frozenset({"minimum", "maximum", "enum"}),
    "number": frozenset({"minimum", "maximum", "enum"}),
    "boolean": frozenset({"enum"}),
    "array": frozenset({"minItems", "maxItems", "items"}),
    "object": frozenset(),
}

#: What the top-level parameters object may carry. `additionalProperties` is
#: enforced only as a boolean; a schema there (or `patternProperties`,
#: `minProperties`, `oneOf` ...) would be advertised and not checked.
_TOP_LEVEL = frozenset({"type", "properties", "required", "additionalProperties"}) | _ANNOTATIONS

#: Cap on elements named in one message, so a 10,000-element array of wrong types
#: costs the model a sentence rather than the whole output budget.
_MAX_ELEMENT_PROBLEMS = 5

#: The truncation trailer has to fit inside the cap with room to spare for at
#: least some content; below this the cap cannot hold any plausible trailer.
_MIN_OUTPUT_CHARS = 64
_TRUNCATION_TRAILER = "\n[truncated {} chars]"


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


def _preview(value: object, limit: int = 60) -> str:
    """A short repr of something the model sent, so an error never echoes a megabyte back."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}... ({len(text)} chars)"


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_bound(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_property(tool: str, where: str, prop: object, *, nested: bool = False) -> None:
    """Refuse a property schema the box could not enforce.

    `nested` is True for the schema under `items`, which may use the same
    keywords as a property except another `items`: an array of arrays is checked
    as far as "each element is an array" and no further, so a deeper schema would
    be advertised and not enforced.
    """
    if not isinstance(prop, Mapping):
        raise ValueError(f"tool {tool!r}: {where} must be a schema object, got {_json_type(prop)}")

    declared = prop.get("type")
    if isinstance(declared, (list, tuple)):
        raise ValueError(
            f"tool {tool!r}: {where} has type {list(declared)!r}; a type must be a single name "
            f"(unions such as ['string', 'null'] are not supported -- make the argument optional "
            f"instead). Supported: {', '.join(sorted(_TYPE_CHECKS))}"
        )
    if declared is None:
        raise ValueError(
            f"tool {tool!r}: {where} declares no 'type', so nothing about it would be checked. "
            f"Supported: {', '.join(sorted(_TYPE_CHECKS))}"
        )
    if not isinstance(declared, str) or declared not in _TYPE_CHECKS:
        raise ValueError(
            f"tool {tool!r}: {where} has unsupported type {declared!r}; "
            f"supported: {', '.join(sorted(_TYPE_CHECKS))}"
        )

    enforced = _ENFORCED[declared] - ({"items"} if nested else frozenset())
    unenforced = sorted(set(prop) - {"type"} - _ANNOTATIONS - enforced)
    if unenforced:
        raise ValueError(
            f"tool {tool!r}: {where} uses {', '.join(map(repr, unenforced))}, which the toolbox does "
            f"not enforce for type {declared!r} (it enforces: {', '.join(sorted(enforced)) or 'nothing'}). "
            f"Check it inside the tool and drop it from the schema, so the schema never advertises a "
            f"bound nothing checks"
        )

    if "enum" in prop:
        options = prop["enum"]
        if not isinstance(options, (list, tuple)) or not options:
            raise ValueError(f"tool {tool!r}: {where} 'enum' must be a non-empty list")
        for option in options:
            if not _TYPE_CHECKS[declared](option):
                raise ValueError(
                    f"tool {tool!r}: {where} 'enum' member {option!r} is not of the declared type {declared}"
                )
    for keyword in ("minimum", "maximum"):
        if keyword in prop and not _is_bound(prop[keyword]):
            raise ValueError(f"tool {tool!r}: {where} {keyword!r} must be a finite number")
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        if keyword in prop and not _is_count(prop[keyword]):
            raise ValueError(f"tool {tool!r}: {where} {keyword!r} must be a non-negative integer")
    for low, high in (("minimum", "maximum"), ("minLength", "maxLength"), ("minItems", "maxItems")):
        if low in prop and high in prop and prop[low] > prop[high]:
            raise ValueError(f"tool {tool!r}: {where} {low!r} ({prop[low]}) exceeds {high!r} ({prop[high]})")

    if "items" in prop:
        _check_property(tool, f"{where} items", prop["items"], nested=True)


def _check_spec(spec: ToolSpec) -> None:
    """Refuse a spec the box could not enforce, at construction rather than per call.

    A schema keyword the box does not check would be sent to the provider as if
    it bound the model -- and the tool author would believe it protected an
    argument it does not. Better a loud `ValueError` the first time the toolbox
    is built, naming the keyword. That covers an unknown `type`, a union type, a
    validation keyword the box lacks (`pattern`, `format`, `oneOf`, `anyOf`,
    `allOf`, `$ref`, `const`, `uniqueItems`, nested `properties`, ...) and a typo
    in one that it has.
    """
    parameters = spec.parameters
    if not isinstance(parameters, Mapping):
        raise ValueError(f"tool {spec.name!r}: parameters must be a schema object")

    unenforced = sorted(set(parameters) - _TOP_LEVEL)
    if unenforced:
        raise ValueError(
            f"tool {spec.name!r}: the parameters object uses {', '.join(map(repr, unenforced))}, which "
            f"the toolbox does not enforce (it accepts: {', '.join(sorted(_TOP_LEVEL))}). Check it "
            f"inside the tool and drop it from the schema"
        )
    if parameters.get("type", "object") != "object":
        raise ValueError(f"tool {spec.name!r}: the parameters object must have type 'object'")
    additional = parameters.get("additionalProperties", True)
    if not isinstance(additional, bool):
        raise ValueError(
            f"tool {spec.name!r}: 'additionalProperties' must be true or false; a schema there "
            f"would not be enforced"
        )

    properties = parameters.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError(f"tool {spec.name!r}: 'properties' must be an object")
    for name, prop in properties.items():
        _check_property(spec.name, f"property {name!r}", prop)

    required = parameters.get("required", ())
    if not isinstance(required, (list, tuple)):
        raise ValueError(f"tool {spec.name!r}: 'required' must be a list of property names")
    for name in required:
        if name not in properties:
            raise ValueError(f"tool {spec.name!r}: required property {name!r} is not declared")


def _value_problems(label: str, value: Any, prop: Mapping[str, Any]) -> list[str]:
    """Everything wrong with one value against its property schema.

    `label` is how the message names it (`argument 'limit'`, or
    `argument 'paths'[1]` for an element), so a bad element is reported *by
    position* rather than as "the array was wrong". Assumes `prop` passed
    `_check_property`, which is why `prop["type"]` is read unguarded.
    """
    declared = prop["type"]
    if not _TYPE_CHECKS[declared](value):
        # Models routinely send null for an optional argument, and "must be
        # integer, got null" does not tell them to leave it out.
        hint = "; omit an optional argument rather than sending null" if value is None else ""
        return [f"{label} must be {declared}, got {_json_type(value)}{hint}"]

    problems: list[str] = []

    if "enum" in prop and value not in prop["enum"]:
        options = ", ".join(_preview(option, 40) for option in prop["enum"])
        problems.append(f"{label} must be one of {options}; got {_preview(value)}")

    if declared == "number" and isinstance(value, float) and not math.isfinite(value):
        # NaN compares false against every bound, so it would sail through
        # `minimum` and `maximum` below.
        problems.append(f"{label} must be a finite number, got {value}")
    elif declared in ("integer", "number"):
        if "minimum" in prop and value < prop["minimum"]:
            problems.append(f"{label} must be at least {prop['minimum']}, got {value}")
        if "maximum" in prop and value > prop["maximum"]:
            problems.append(f"{label} must be at most {prop['maximum']}, got {value}")

    elif declared == "string":
        if "minLength" in prop and len(value) < prop["minLength"]:
            problems.append(f"{label} must be at least {prop['minLength']} characters, got {len(value)}")
        if "maxLength" in prop and len(value) > prop["maxLength"]:
            problems.append(f"{label} must be at most {prop['maxLength']} characters, got {len(value)}")

    elif declared == "array":
        if "minItems" in prop and len(value) < prop["minItems"]:
            problems.append(f"{label} must have at least {prop['minItems']} item(s), got {len(value)}")
        if "maxItems" in prop and len(value) > prop["maxItems"]:
            problems.append(f"{label} must have at most {prop['maxItems']} item(s), got {len(value)}")
        if "items" in prop:
            element_problems: list[str] = []
            for index, element in enumerate(value):
                element_problems.extend(_value_problems(f"{label}[{index}]", element, prop["items"]))
            problems.extend(element_problems[:_MAX_ELEMENT_PROBLEMS])
            if len(element_problems) > _MAX_ELEMENT_PROBLEMS:
                problems.append(
                    f"... and {len(element_problems) - _MAX_ELEMENT_PROBLEMS} more problem(s) in {label}"
                )
    return problems


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
        if isinstance(prop, Mapping):
            problems.extend(_value_problems(f"argument {name!r}", value, prop))
    return problems


def _truncate(content: str, cap: int) -> str:
    """`content` cut so that the result, trailer included, is at most `cap` characters.

    The trailer states how many characters were dropped, and its own length
    depends on how many digits that count has -- so the kept length and the
    count are solved together, by trying each digit count in turn. Cutting the
    body to `cap` and appending the trailer, as this once did, returned up to 22
    characters more than the cap that callers (a prompt budget, a column width)
    were promised.
    """
    fixed = len(_TRUNCATION_TRAILER.format(""))
    for digits in range(1, 25):
        keep = cap - fixed - digits
        dropped = len(content) - keep
        if len(str(dropped)) <= digits:
            return content[:keep] + _TRUNCATION_TRAILER.format(dropped)
    raise AssertionError("unreachable: a count of more than 24 digits")  # pragma: no cover


class ToolBox:
    """The tools of one agent run, and the one place a call is checked and run.

    `dispatch` is what makes the agent's authority a property of code. It checks
    the arguments against the tool's schema, and **what it checks is exactly what
    the schema may say**: the six basic JSON types, `required`,
    `additionalProperties: false`, and per property `enum`, `minimum`/`maximum`,
    `minLength`/`maxLength`, `minItems`/`maxItems` and `items` (each element's
    type and the same per-element keywords, reported by position). `ToolBox(...)`
    refuses at construction any other validation keyword -- `pattern`, `format`, `oneOf`,
    `anyOf`, `allOf`, `$ref`, `const`, `uniqueItems`, nested `properties`, a
    union `type` -- because a schema that advertises a bound nothing enforces is
    sent to the provider as if it bound the model, and the tool author believes
    the argument is protected. A tool that needs one of those checks does it
    itself, and says so by leaving it out of the schema.

    Constraints the box does not know about cannot be hidden from a model by
    omission, so the *authority* checks (paths, protected files, what a command
    may name) still live in the tools; the box is the backstop that makes the
    declared bounds true.
    """

    def __init__(self, tools: Sequence[Tool], *, max_output_chars: int = ToolLimits().max_output_chars) -> None:
        if isinstance(max_output_chars, bool) or not isinstance(max_output_chars, int):
            raise ValueError(f"max_output_chars must be an integer, got {_json_type(max_output_chars)}")
        if max_output_chars < _MIN_OUTPUT_CHARS:
            # Non-positive would slice from the end or keep nothing; a small
            # positive cap cannot hold the truncation trailer, which is counted
            # inside the cap.
            raise ValueError(
                f"max_output_chars must be at least {_MIN_OUTPUT_CHARS} so the truncation trailer "
                f"fits inside it, got {max_output_chars}"
            )
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

        Every outcome's content is at most `max_output_chars` characters
        *including* any `[truncated N chars]` trailer, whoever produced it; the
        trailer says how much was dropped so the model knows the output is
        incomplete rather than short. The cut keeps the head, so a tool whose
        useful part is the tail (a traceback, a test summary) must trim its own
        output before it gets here.
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
        return dataclasses.replace(outcome, content=_truncate(content, self.max_output_chars))


def build_toolbox(ctx: ToolContext) -> ToolBox:
    """The agent's tools, built from a context: one `Tool` per capability.

    By name: `search_code`, `read_file`, `grep`, `list_dir`, `edit_file`,
    `create_file`, `run_python`, `run_tests` and `submit`. The rules every one of
    them must hold: every path goes through
    `repolace_shared.paths.resolve_within`; a write is refused when
    `ctx.is_protected(path)` is true (never `is_protected_path` directly, so the
    pipeline's baseline-aware closure is honoured -- and that already covers
    `.github/`) or the path is anything under `.git`; limits come from
    `ctx.limits`, `ctx.limits.max_probe_seconds` included; and the box is built
    as `ToolBox(tools, max_output_chars=ctx.limits.max_output_chars)`.

    The implementation lives in `repolace_agents.tools.toolbox`. It is imported
    inside the function because `toolbox` imports this module, so a top-level
    import would be a cycle. Both import paths build the same box.
    """
    from repolace_agents.tools.toolbox import build_toolbox as _build_toolbox

    return _build_toolbox(ctx)
