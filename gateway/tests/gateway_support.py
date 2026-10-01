"""Builders and doubles for gateway tests.

Imported by name rather than injected as fixtures, matching the existing
`<package>_support.py` convention. The name is globally unique because pytest's
prepend import mode puts every test directory on sys.path, so a second
`support.py` would silently resolve to whichever was collected first -- the same
shadowing that once broke four `app/` packages.

Self-contained on purpose: it does not import `shared/tests/db_support.py`,
because that only resolves when `shared/tests` happens to have been collected
first, and `pytest gateway/tests` alone must work.

What the fakes stand in for, and what they deliberately do not:

* `FakeAcompletion` replaces the network. It is *scripted*, and it fails the
  test if it is called more times than scripted -- which is how a test proves a
  call was refused *before* it was made.
* `make_response` builds a real `litellm.ModelResponse`, not a stand-in, so the
  parsing under test is run against the shape LiteLLM actually returns.
* `ListRecorder` replaces the database for tests that are about the client;
  the tests that are about the table use `-m db` and the real `Recorder`.
"""

import copy
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import litellm

from repolace_gateway.budget import TaskBudget
from repolace_gateway.client import LLMClient
from repolace_gateway.config import GatewayConfig, GatewaySettings, parse_config
from repolace_gateway.recorder import CallRecord, Recorder
from repolace_gateway.redaction import Redactor
from repolace_shared.db.models import GithubInstallation, RegisteredRepo, Task, TaskStatus

TASK_ID = uuid.UUID("5c1d0e7a-0000-4000-8000-000000000012")

#: Realistic shapes and lengths, because redaction is pattern-based: a fake key
#: too short to match the pattern would test nothing.
ANTHROPIC_KEY = "sk-ant-api03-" + "Zq9Xk2" * 6
OPENAI_KEY = "sk-proj-" + "Lm4Rt8" * 6
#: A key the gateway does NOT hold -- the one sitting in a repo's .env.example.
FOREIGN_OPENAI_KEY = "sk-proj-" + "Qw7Er5" * 6

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit",
            "description": "Finish.",
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}},
        },
    },
]

MESSAGES = [
    {"role": "system", "content": "You fix bugs."},
    {"role": "user", "content": "Issue: parse_config crashes on an empty file."},
]


def raw_config() -> dict[str, Any]:
    """A fresh decoded `models.toml`; tests mutate it, so never share one.

    `main` and `small` carry their own prices, so most client tests never touch
    LiteLLM's price map. `priced_by_litellm` has none, for the tests that do.
    Rates are round numbers so expected costs can be checked by hand.
    """
    return {
        "gateway": {
            "max_attempts": 3,
            "base_delay_seconds": 1.0,
            "max_delay_seconds": 10.0,
            "request_timeout_seconds": 30.0,
        },
        "stages": {
            "agent": {"model": "main", "cache": True, "max_tokens": 4096},
            "cheap": {"model": "small", "max_tokens": 256},
        },
        "models": {
            "main": {
                "provider": "anthropic",
                "litellm_model": "anthropic/test-main",
                "supports_prompt_caching": True,
                "price": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
            },
            "small": {
                "provider": "anthropic",
                "litellm_model": "anthropic/test-small",
                "supports_prompt_caching": False,
                "price": {"input": 1.0, "output": 5.0},
            },
            "backup": {
                "provider": "openai",
                "litellm_model": "openai/test-backup",
                "supports_prompt_caching": False,
                "price": {"input": 2.0, "output": 8.0},
            },
            "priced_by_litellm": {
                "provider": "anthropic",
                "litellm_model": "anthropic/test-priced",
                "supports_prompt_caching": False,
            },
        },
    }


def make_config(*, fallback: str | None = None, agent_model: str | None = None) -> GatewayConfig:
    raw = raw_config()
    if fallback:
        raw["stages"]["agent"]["fallback"] = fallback
    if agent_model:
        raw["stages"]["agent"]["model"] = agent_model
    return parse_config(raw)


def make_settings(**overrides: Any) -> GatewaySettings:
    """Settings that read neither `.env` nor the developer's real keys.

    Every provider is pinned, the unused ones to None: an unset field would fall
    through to the environment, and a developer with GEMINI_API_KEY exported would
    get a different result from CI.
    """
    values: dict[str, Any] = {
        "anthropic_api_key": ANTHROPIC_KEY,
        "openai_api_key": OPENAI_KEY,
        "gemini_api_key": None,
    }
    values.update(overrides)
    return GatewaySettings(_env_file=None, **values)


def make_response(
    content: str | None = "done",
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    cache_read: int = 0,
    cache_write: int = 0,
    tool_calls: list[tuple[str, str, str]] | None = None,
    model: str = "test-main",
    finish_reason: str | None = None,
) -> litellm.ModelResponse:
    """A real `ModelResponse`.

    `prompt_tokens` is the *total* prompt, cache reads and writes included -- the
    convention LiteLLM's Anthropic transformation produces, which is the thing
    `extract_usage` has to read correctly. `tool_calls` is `(id, name, raw_json)`.
    """
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
            for call_id, name, arguments in tool_calls
        ]
    usage = litellm.Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    return litellm.ModelResponse(
        model=model,
        choices=[
            {
                "index": 0,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
                "message": message,
            }
        ],
        usage=usage,
    )


def make_priced_response(cost: str) -> litellm.ModelResponse:
    """A response that costs exactly `cost` dollars on the `main` model.

    Built from 100k prompt tokens ($0.30 at $3/M) and enough output ($15/M) to
    make up the rest, so a budget test can state its arithmetic in dollars.

    Refuses a cost that is not reachable in whole tokens: at these rates $1.00 is
    not (3p + 15n = 1,000,000 has no integer solution), and an "exactly on the
    cap" test that is off by a fraction of a cent proves nothing. $0.45, $0.60,
    $0.75 and $1.05 are all exact.
    """
    remaining = Decimal(cost) - Decimal("0.30")
    tokens = remaining / (Decimal(15) / Decimal(1_000_000))
    assert remaining > 0 and tokens == tokens.to_integral_value(), (
        f"${cost} is not reachable in whole tokens at the test rates"
    )
    return make_response(prompt_tokens=100_000, completion_tokens=int(tokens))


class FakeAcompletion:
    """A scripted stand-in for `litellm.acompletion`.

    Each call consumes the next outcome: an exception instance is raised, anything
    else is returned. Called more often than scripted, it fails the test -- which
    is how a test asserts that a call was refused before it was ever made.
    """

    def __init__(self, *outcomes: Any, repeat_last: bool = False) -> None:
        self._outcomes = list(outcomes)
        self._repeat_last = repeat_last
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        # Deep-copied: the client builds fresh dicts per call, but a test that
        # inspects `calls` later should see what was sent, not what the loop
        # has since done to its own history.
        self.calls.append(copy.deepcopy(kwargs))
        if not self._outcomes:
            raise AssertionError(f"acompletion called {len(self.calls)} time(s), more than scripted")
        outcome = self._outcomes[0] if (self._repeat_last and len(self._outcomes) == 1) else self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ListRecorder(Recorder):
    """A `Recorder` that keeps records in a list instead of a database.

    Subclasses the real one so the client sees the same type, `redactor` included.
    """

    def __init__(self, redactor: Redactor | None = None, fail: Exception | None = None) -> None:
        super().__init__(session_factory=None, redactor=redactor or Redactor())  # type: ignore[arg-type]
        self.records: list[CallRecord] = []
        self._fail = fail

    async def record(self, record: CallRecord) -> uuid.UUID:
        if self._fail is not None:
            raise self._fail
        self.records.append(record)
        return uuid.uuid4()


class FakeClock:
    """Advances a fixed step per read, so latency is deterministic."""

    def __init__(self, step: float = 0.25) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


@dataclass
class Harness:
    client: LLMClient
    recorder: ListRecorder
    acompletion: FakeAcompletion
    sleeps: list[float] = field(default_factory=list)


def make_harness(
    *outcomes: Any,
    config: GatewayConfig | None = None,
    settings: GatewaySettings | None = None,
    recorder: ListRecorder | None = None,
    repeat_last: bool = False,
    **client_kwargs: Any,
) -> Harness:
    """A client wired to a scripted provider, with backoff made instant and exact.

    `jitter` is pinned to 1.0 so the delay is the nominal one (jitter scales it
    between half and full), and `sleep` records instead of waiting.
    """
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    acompletion = FakeAcompletion(*outcomes, repeat_last=repeat_last)
    recorder = recorder or ListRecorder(Redactor(make_settings().secret_values()))
    client_kwargs.setdefault("jitter", lambda: 1.0)
    client = LLMClient(
        config or make_config(),
        settings or make_settings(),
        recorder,
        acompletion=acompletion,
        sleep=fake_sleep,
        **client_kwargs,
    )
    return Harness(client=client, recorder=recorder, acompletion=acompletion, sleeps=sleeps)


def make_budget(**overrides: Any) -> TaskBudget:
    return TaskBudget(**overrides)


def stored_json(record: CallRecord) -> str:
    """Everything a record would store, flattened, for 'is this string anywhere in it'."""
    return json.dumps(
        {"request": record.request, "response": record.response, "error": record.error},
        default=repr,
    )


async def seed_task(session, **task_overrides: Any) -> Task:
    """Installation -> repo -> task, committed. Returns the task."""
    session.add(
        GithubInstallation(id=4242, account_login="acme", account_id=1, account_type="Organization")
    )
    repo = RegisteredRepo(
        id=uuid.uuid4(),
        installation_id=4242,
        github_repo_id=99,
        owner="acme",
        name="sample",
        full_name="acme/sample",
        default_branch="main",
        private=False,
    )
    session.add(repo)
    await session.flush()
    fields: dict[str, Any] = {
        "id": TASK_ID,
        "repo_id": repo.id,
        "issue_number": 1,
        "issue_title": "parse_config crashes on an empty config file",
        "issue_url": "https://github.com/acme/sample/issues/1",
        "target_branch": "main",
        "status": TaskStatus.QUEUED,
    }
    fields.update(task_overrides)
    task = Task(**fields)
    session.add(task)
    await session.commit()
    return task
