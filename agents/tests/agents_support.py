"""Builders for agent tests.

`<package>_support.py`, globally unique, because pytest's prepend import mode
puts every test directory on sys.path and two files with the same name would
silently resolve to whichever was collected first.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from repolace_agents.contracts import AgentDeps, IssueContext
from repolace_agents.tools.base import ToolOutcome, ToolSpec
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
