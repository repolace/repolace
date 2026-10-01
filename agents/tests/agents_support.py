"""Builders for agent tests.

`<package>_support.py`, globally unique, because pytest's prepend import mode
puts every test directory on sys.path and two files with the same name would
silently resolve to whichever was collected first.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from repolace_agents.tools.base import ToolOutcome, ToolSpec


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
