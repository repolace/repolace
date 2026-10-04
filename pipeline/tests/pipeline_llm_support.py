"""A real gateway client over a scripted provider, for driving the real graph through `run_task`.

The agents package's own tests script `LLMClientLike` directly, which skips the gateway: nothing is
priced, budgeted or recorded. The integration tests need the opposite -- the whole stack from the
graph down to the `llm_calls` table -- so the thing scripted here is `litellm.acompletion`, one layer
below `LLMClient`. What runs for real: the client's request building, pricing, budget charge, recorder
(into the test database) and response parsing.

`gateway/tests/gateway_support.py` has the same fakes, but pytest's prepend import mode makes a module
from another package's test directory reachable only by accident of `sys.path`, so these are written
out here, trimmed to what this suite needs.
"""

import copy
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import litellm

from repolace_gateway.client import LLMClient
from repolace_gateway.config import GatewayConfig, GatewaySettings, parse_config
from repolace_gateway.recorder import Recorder
from repolace_gateway.redaction import Redactor

#: Provider-shaped fake credential; the gateway redacts exact matches of it from what it stores.
FAKE_ANTHROPIC_KEY = "sk-ant-api03-" + "Zq9Xk2" * 6

MODEL_NAME = "anthropic/test-main"
#: What one `reply()` costs on the priced model below: 100 prompt tokens at $3/M plus 20 output tokens
#: at $15/M. Round enough that a test can state its budget arithmetic in dollars.
REPLY_COST = "0.0006"


def gateway_config() -> GatewayConfig:
    """One priced model for every stage, so no test reaches LiteLLM's price map."""
    return parse_config(
        {
            "gateway": {
                "max_attempts": 2,
                "base_delay_seconds": 0.01,
                "max_delay_seconds": 0.02,
                "request_timeout_seconds": 30.0,
            },
            "stages": {
                "agent": {"model": "main", "cache": True, "max_tokens": 4096},
                "cheap": {"model": "main", "max_tokens": 256},
            },
            "models": {
                "main": {
                    "provider": "anthropic",
                    "litellm_model": MODEL_NAME,
                    "supports_prompt_caching": True,
                    "price": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
                },
            },
        }
    )


def gateway_settings() -> GatewaySettings:
    """Settings that read neither `.env` nor the developer's real keys."""
    return GatewaySettings(
        _env_file=None, anthropic_api_key=FAKE_ANTHROPIC_KEY, openai_api_key=None, gemini_api_key=None
    )


def call(name: str, **arguments: Any) -> tuple[str, str, str]:
    """A tool call as `(id, name, raw_json)`, with an id that is unique across a run."""
    call.counter += 1  # type: ignore[attr-defined]
    return (f"call_{call.counter}", name, json.dumps(arguments))


call.counter = 0  # type: ignore[attr-defined]


def reply(
    *calls: tuple[str, str, str],
    content: str | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
) -> litellm.ModelResponse:
    """A real `ModelResponse`: one assistant turn carrying `calls`."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
            for call_id, name, arguments in calls
        ]
    usage = litellm.Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return litellm.ModelResponse(
        model="test-main",
        choices=[{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}],
        usage=usage,
    )


@dataclass
class ScriptedProvider:
    """A stand-in for `litellm.acompletion` that plays back a script and records every request.

    Each item is a response (returned), an exception instance (raised) or a callable taking the
    request kwargs and returning either -- the last is how a reply can depend on what the model was
    just shown. Called more often than scripted, it fails the test: a loop that makes one call more
    than the test planned is a finding, not something to answer with a canned reply.
    """

    script: Sequence[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queue = list(self.script)

    async def __call__(self, **kwargs: Any) -> Any:
        # A deep copy: the client builds fresh dicts per call, but a test reading `calls` later should
        # see what was sent, not what the loop has since done to its own history.
        self.calls.append(copy.deepcopy(kwargs))
        if not self._queue:
            raise AssertionError(f"the provider was called {len(self.calls)} time(s), more than scripted")
        item = self._queue.pop(0)
        if callable(item) and not isinstance(item, (BaseException, litellm.ModelResponse)):
            item = item(kwargs)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def unused(self) -> int:
        return len(self._queue)

    def request_text(self, index: int) -> str:
        """Everything the model was sent in request `index` (messages and tool schemas), as one string."""
        request = self.calls[index]
        return json.dumps({"messages": request.get("messages"), "tools": request.get("tools")}, default=str)

    def everything_sent(self) -> str:
        return "\n".join(self.request_text(i) for i in range(len(self.calls)))

    def tool_results(self) -> list[str]:
        """Every tool message the model was ever shown, from the longest transcript (earlier ones are elided)."""
        seen: dict[str, str] = {}
        for request in self.calls:
            for message in request.get("messages") or []:
                if message.get("role") == "tool":
                    content = message["content"]
                    if isinstance(content, list):  # cache markers turn the content into blocks
                        content = "".join(block.get("text", "") for block in content)
                    seen.setdefault(message["tool_call_id"], content)
        return list(seen.values())


def make_client(
    session_factory: Any, provider: ScriptedProvider, *, config: GatewayConfig | None = None
) -> LLMClient:
    """The real `LLMClient`, recording into the test database, calling `provider` instead of the network."""
    settings = gateway_settings()

    async def no_sleep(seconds: float) -> None:
        return None

    return LLMClient(
        config or gateway_config(),
        settings,
        Recorder(session_factory, Redactor(settings.secret_values())),
        acompletion=provider,
        sleep=no_sleep,
        jitter=lambda: 1.0,
    )


Responder = Callable[[dict[str, Any]], Any]
