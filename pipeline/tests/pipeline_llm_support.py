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
from sqlalchemy import select

from repolace_gateway.client import LLMClient
from repolace_gateway.config import GatewayConfig, GatewaySettings, parse_config
from repolace_gateway.recorder import Recorder
from repolace_gateway.redaction import Redactor
from repolace_shared.db.models import LLMCall
from verify.protocol import SuiteResult

from repolace_agents.contracts import AgentResult
from repolace_pipeline.agent_runner import LLMAgent
from repolace_pipeline.run import RunResult, run_task

from pipeline_support import FakeGithubClient, local_workspace_factory

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

    def results_by_id(self) -> dict[str, str]:
        """Every tool message the model was ever shown, by the id of the call it answers.

        Read across all requests, because a later attempt elides the earlier attempt's tool output;
        the first sighting of each id is the real result.
        """
        seen: dict[str, str] = {}
        for request in self.calls:
            for message in request.get("messages") or []:
                if message.get("role") == "tool":
                    content = message["content"]
                    if isinstance(content, list):  # cache markers turn the content into blocks
                        content = "".join(block.get("text", "") for block in content)
                    seen.setdefault(message["tool_call_id"], content)
        return seen

    def tool_results(self) -> list[str]:
        return list(self.results_by_id().values())

    def result_of(self, tool_call: tuple[str, str, str]) -> str:
        """What the model was told when it made `tool_call` (a tuple from `call()`)."""
        return self.results_by_id()[tool_call[0]]


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


VISIBLE = "tests/test_app.py::test_parse_config_reads_pairs"
OTHER = "tests/test_app.py::test_other"
HIDDEN = "tests/test_hidden.py::test_empty_config"
COLLECTED = ("tests/test_app.py",)
BENCH_COLLECTED = ("tests/test_app.py", "tests/test_hidden.py")

DOCSTRING = '"""Parse the key=value config file at path into a dict."""'
FIXED = '"""Parse the key=value config file at path into a dict. An empty file gives an empty dict."""'
BROKEN = '"""BROKEN"""'

#: A live-issue baseline with one red test, which the attempt turns green: scored PASSED (uncurated).
RED_BASELINE = SuiteResult(passed=(OTHER,), failed=(VISIBLE,), collected_files=COLLECTED)
GREEN_AFTER = SuiteResult(passed=(OTHER, VISIBLE), collected_files=COLLECTED)

#: The benchmark versions: the curated test is red at the base commit because the overlay is on disk.
BENCH_BASELINE = SuiteResult(passed=(VISIBLE,), failed=(HIDDEN,), collected_files=BENCH_COLLECTED)
BENCH_AFTER = SuiteResult(passed=(VISIBLE, HIDDEN), collected_files=BENCH_COLLECTED)


def edit_docstring(old: str, new: str):
    return call("edit_file", path="src/app.py", old_string=old, new_string=new)


async def run_real(
    factory, origin_url, task, provider, backend, *, github=None, config=None, **kwargs
) -> tuple[RunResult, FakeGithubClient]:
    """`run_task` with the real runner and a real client over `provider`: what `--agent llm` builds."""
    github = github if github is not None else FakeGithubClient()
    result = await run_task(
        task.id,
        factory,
        github,
        backend,
        agent=LLMAgent(),
        llm=make_client(factory, provider, config=config),
        workspace_factory=local_workspace_factory(origin_url),
        embedder_warmup=lambda: None,
        **kwargs,
    )
    return result, github


async def llm_rows(factory, task_id) -> list[LLMCall]:
    async with factory() as session:
        rows = await session.execute(select(LLMCall).where(LLMCall.task_id == task_id).order_by(LLMCall.created_at))
        return list(rows.scalars())


def happy_script():
    """search, read, edit, run the tests, submit: the loop the prompt asks for."""
    return [
        reply(call("search_code", query="parse_config empty file")),
        reply(call("read_file", path="src/app.py")),
        reply(edit_docstring(DOCSTRING, FIXED)),
        reply(call("run_tests", targets=["tests/test_app.py"])),
        reply(call("submit", summary="Document that an empty file gives an empty dict.")),
    ]


def assert_record_complete(row) -> None:
    """Every column a benchmark row needs, set: a column nothing writes is the failure that has happened."""
    assert row.patch_diff and row.changed_files, "patch_diff and changed_files"
    assert row.score_reason, "score_reason"
    assert row.agent_stop_reason, "agent_stop_reason"
    assert row.retry_count is not None, "retry_count"
    assert row.cost_usd is not None and row.cost_usd > 0, "cost_usd"
    assert row.patch_sha and row.completed_at is not None


def first_call_to(provider: ScriptedProvider, tool: str) -> tuple[str, str, str]:
    """The `(id, name, args)` tuple of the first call to `tool` the model made, read back from its transcript."""
    for request in provider.calls:
        for message in request.get("messages") or []:
            for raw in message.get("tool_calls") or []:
                if raw["function"]["name"] == tool:
                    return (raw["id"], tool, raw["function"]["arguments"])
    raise AssertionError(f"the model never called {tool}")
