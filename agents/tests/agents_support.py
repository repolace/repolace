"""Builders for agent tests.

`<package>_support.py`, globally unique, because pytest's prepend import mode
puts every test directory on sys.path and two files with the same name would
silently resolve to whichever was collected first.
"""

import copy
import itertools
import json
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from repolace_agents.contracts import AgentDeps, AttemptRecord, IssueContext
from repolace_agents.tools.base import ToolBox, ToolOutcome, ToolSpec
from verify.protocol import SuiteResult


@dataclass(frozen=True)
class FakeToolCall:
    """The four attributes `ToolBox.dispatch` reads, and nothing from the gateway.

    The real `repolace_gateway.client.ToolCall` has the same four plus
    `raw_arguments`; building this instead keeps litellm out of the process for
    every test that does not specifically check compatibility.
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    id: str = "call_1"
    parse_error: str | None = None


def object_schema(
    properties: Mapping[str, Mapping[str, Any]] | None = None,
    required: Sequence[str] = (),
    *,
    additional_properties: bool | None = False,
) -> dict[str, Any]:
    """A JSON-schema object. `additional_properties=None` omits the key entirely."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
    }
    if additional_properties is not None:
        schema["additionalProperties"] = additional_properties
    return schema


class FunctionTool:
    """A `Tool` made from a name, a schema and an async function."""

    def __init__(
        self,
        name: str,
        handler: Callable[[Mapping[str, Any]], Awaitable[ToolOutcome]] | None = None,
        *,
        parameters: Mapping[str, Any] | None = None,
        description: str = "a test tool",
    ) -> None:
        self.spec = ToolSpec(name=name, description=description, parameters=parameters or object_schema())
        self._handler = handler or self._echo
        self.calls: list[Mapping[str, Any]] = []

    @staticmethod
    async def _echo(args: Mapping[str, Any]) -> ToolOutcome:
        return ToolOutcome(f"args={dict(args)}")

    async def __call__(self, args: Mapping[str, Any]) -> ToolOutcome:
        self.calls.append(args)
        return await self._handler(args)


def make_deps(**overrides: Any) -> AgentDeps:
    """An `AgentDeps` with every required field filled and nothing real behind it."""

    async def verify_attempt(attempt: int):
        return None

    async def changed_files():
        return []

    fields_ = {
        "llm": None,
        "tools": None,
        "checkout": Path("/tmp/checkout"),
        "issue": IssueContext(7, "title", None, "https://example.test/7", None),
        "retrieved": (),
        "repo_overview": "",
        "baseline": SuiteResult(),
        "baseline_files": (),
        "hidden_paths": frozenset(),
        "verify_attempt": verify_attempt,
        "changed_files": changed_files,
    }
    return AgentDeps(**{**fields_, **overrides})


@contextmanager
def expect_stub(owner: str):
    """Assert the call inside is still a stub, and skip -- never fail -- once it is not.

    A stub test is only useful while the stub exists: it pins that the seam
    *refuses loudly* rather than returning a plausible empty value. But a plain
    `pytest.raises(NotImplementedError)` turns red the moment the stream that owns
    the stub implements it, and then forces that stream to delete a test in a
    file it does not own. So the call is made inside this block:

    * it raises `NotImplementedError` naming `owner` -- the stub, as expected; the
      error is swallowed and the test goes on to its remaining assertions (that
      the stub touched nothing, say);
    * it does anything else -- returns, or raises something else, such as
      `VerifierNotReady` from an implemented `run_subset` called before the
      baseline -- the stub has been replaced, and the test **skips** as
      "implemented".

    This is `try: <call> / except NotImplementedError: <assert> / else:
    pytest.skip("implemented")`, written once instead of at every call site, plus
    the second arm for an implementation that raises a different exception.
    """
    try:
        yield
    except NotImplementedError as exc:
        assert owner in str(exc), f"a stub must name the stream that owns it ({owner!r}), got: {exc}"
    except Exception:
        pytest.skip("implemented")
    else:
        pytest.skip("implemented")


# --- scripted model, tools and verifier -------------------------------------
#
# Everything below is for the graph tests. The gateway's real `LLMResponse` and
# `ToolCall` are imported *inside* the builders, not here: importing
# `repolace_gateway.client` pulls LiteLLM, and the many agents tests that only
# need `make_deps` should not pay for it.

_call_ids = itertools.count(1)


def tool_call(name: str, arguments: Mapping[str, Any] | None = None, *, id: str | None = None):
    """A real gateway `ToolCall`, with `raw_arguments` encoded the way a provider sends it."""
    from repolace_gateway.client import ToolCall

    arguments = dict(arguments or {})
    return ToolCall(
        id=id or f"call_{next(_call_ids)}",
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )


def malformed_tool_call(name: str, raw_arguments: str, *, id: str | None = None):
    """A real gateway `ToolCall` whose arguments did not parse, as `LLMClient` builds one.

    `arguments` is empty and `parse_error` says why, while `raw_arguments` keeps what the
    model actually sent -- which is what the assistant message would echo back.
    """
    from repolace_gateway.client import ToolCall

    try:
        decoded = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        error = f"arguments are not valid JSON: {exc}"
    else:
        error = f"arguments must be a JSON object, got {type(decoded).__name__}"
    return ToolCall(id=id or f"call_{next(_call_ids)}", name=name, arguments={}, raw_arguments=raw_arguments, parse_error=error)


def reply(content: str | None = None, *calls, cost: str = "0.01"):
    """A real gateway `LLMResponse`: the assistant turn, in the shape `LLMClient` builds it."""
    from repolace_gateway.client import LLMResponse, TokenUsage

    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.raw_arguments}}
            for c in calls
        ]
    return LLMResponse(
        stage="agent",
        model="scripted/model",
        provider="scripted",
        content=content,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        usage=TokenUsage(input_tokens=100, cached_input_tokens=0, cache_write_tokens=0, output_tokens=10),
        cost_usd=Decimal(cost),
        latency_ms=1,
        message=message,
        call_id=uuid.uuid4(),
    )


def submit_reply(summary: str = "Fixed it.", *, before=()):
    """A reply that calls `submit`, optionally preceded by other calls in the same turn."""
    return reply(None, *before, tool_call("submit", {"summary": summary}))


def check_messages_valid(messages: Sequence[Mapping[str, Any]]) -> None:
    """Assert `messages` is a conversation a provider would accept.

    The property the graph must keep through retries, elision and every kind of
    stop: **every assistant `tool_calls` entry is answered, in order, by a tool
    message with its id, before anything else; and no tool message answers
    nothing.** An unanswered call is rejected by the provider on the next request,
    which would turn a normal stop into a failed task -- and it is easy to break
    precisely by appending a response that could not be dispatched.
    """
    assert messages, "an empty conversation"
    assert messages[0]["role"] == "system", f"first message is {messages[0]['role']}, not system"
    pending: list[str] = []
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "tool":
            assert pending, f"message {index}: a tool result with no call waiting for it"
            expected = pending.pop(0)
            assert message["tool_call_id"] == expected, (
                f"message {index}: answers {message['tool_call_id']!r} but {expected!r} was next"
            )
            continue
        assert not pending, f"message {index} ({role}) arrives while calls {pending} are unanswered"
        if role == "assistant":
            pending = [c["id"] for c in message.get("tool_calls", ())]
    assert not pending, f"the conversation ends with unanswered tool calls {pending}"


@dataclass
class RecordedCall:
    stage: str
    #: A deep copy taken at call time, so a later mutation of the loop's list
    #: cannot change what this call is recorded as having seen.
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    attempt: int | None
    kwargs: dict[str, Any]

    def text(self) -> str:
        """Everything the model was shown in this call, as one string, for sentinel searches."""
        return json.dumps(self.messages, default=str) + json.dumps(self.tools, default=str)


class ScriptedLLM:
    """An `LLMClientLike` that plays back a script and records every request.

    Each script item is an `LLMResponse` (returned) or an exception (raised), in
    order. Every request is checked with `check_messages_valid` on arrival, so
    **every graph test is also a test that the transcript stayed valid**. Running
    out of script is an `AssertionError`, never a canned reply: a loop that makes
    one call more than the test planned is a finding.
    """

    def __init__(self, script: Sequence[Any]) -> None:
        self._script = deque(script)
        self.calls: list[RecordedCall] = []

    async def complete(
        self,
        stage: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        attempt: int | None = None,
        cache: bool | None = None,
        **kw: Any,
    ):
        snapshot = copy.deepcopy([dict(m) for m in messages])
        self.calls.append(RecordedCall(stage, snapshot, copy.deepcopy(list(tools or [])), attempt, kw))
        check_messages_valid(snapshot)
        if not self._script:
            raise AssertionError(f"ScriptedLLM has no response for call {len(self.calls)}")
        item = self._script.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def unused(self) -> int:
        return len(self._script)


def scripted_toolbox(result_for: Callable[[str, Mapping[str, Any]], str] | None = None):
    """A real `ToolBox` over fake tools: `read_file`, `edit_file`, `run_tests`, `submit`.

    `result_for(name, args)` is the content a non-submit tool returns; the default
    echoes the call, which is enough to see in a transcript which outputs were
    kept and which elided. Returns the box and the tools by name (each records
    its calls), so a test can assert what ran -- and, for `submit`, what did not.
    """
    result_for = result_for or (lambda name, args: f"{name} output for {dict(args)}")

    def make(name: str, properties: Mapping[str, Mapping[str, Any]], required: Sequence[str]):
        async def handler(args: Mapping[str, Any]) -> ToolOutcome:
            if name == "submit":
                return ToolOutcome("submitted", submitted=True, summary=args["summary"])
            return ToolOutcome(result_for(name, args))

        return FunctionTool(name, handler, parameters=object_schema(properties, required), description=f"fake {name}")

    path = {"path": {"type": "string"}}
    tools = {
        "read_file": make("read_file", path, ["path"]),
        "edit_file": make("edit_file", {**path, "new": {"type": "string"}}, ["path", "new"]),
        "run_tests": make("run_tests", {"targets": {"type": "array", "items": {"type": "string"}}}, []),
        "submit": make("submit", {"summary": {"type": "string", "maxLength": 1000}}, ["summary"]),
    }
    return ToolBox(list(tools.values())), tools


def suite(
    passed: Sequence[str] = (),
    failed: Sequence[str] = (),
    *,
    collect_failures: Sequence[str] = (),
    collected_files: Sequence[str] = (),
    stdout_tail: str = "",
    error: str | None = None,
    **extra: Any,
) -> SuiteResult:
    """A `SuiteResult` with a stable fingerprint, so two of them never drift by accident."""
    return SuiteResult(
        passed=tuple(passed),
        failed=tuple(failed),
        collect_failures=tuple(collect_failures),
        collected_files=tuple(collected_files),
        fingerprint={"rootdir": "/repo", "ini": {}, "plugins": []},
        stdout_tail=stdout_tail,
        error=error,
        **extra,
    )


def attempt_record(attempt: int, result: SuiteResult, *, infrastructure_error: bool = False) -> AttemptRecord:
    return AttemptRecord(attempt, f"sha{attempt:038d}", result, infrastructure_error)


class ScriptedVerifier:
    """A `verify_attempt` that plays back records (or None, or an exception) and records its calls."""

    def __init__(self, script: Sequence[AttemptRecord | BaseException | None]) -> None:
        self._script = deque(script)
        self.calls: list[int] = []

    async def __call__(self, attempt: int) -> AttemptRecord | None:
        self.calls.append(attempt)
        if not self._script:
            raise AssertionError(f"ScriptedVerifier has no result for verify_attempt({attempt})")
        item = self._script.popleft()
        if isinstance(item, BaseException):
            raise item
        return item
