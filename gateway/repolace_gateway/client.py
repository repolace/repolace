"""The one door every model call goes through.

`LLMClient.complete` is the only place in the system that talks to a model
provider, which is what makes three guarantees possible rather than merely
intended:

* **Every call is priced and recorded**, from the first benchmark run. An
  unpriced call is a hard error (`UnpricedModelError`), never zero.
* **Every call is attributable** to a task (`task_scope`) and charged to that
  task's budget, which can stop a runaway loop (`BudgetExceeded`).
* **Credentials never leave this module.** The provider key is handed to LiteLLM
  per call, never exported into `os.environ`, and stripped from what is stored.

Order of operations inside `complete`, and why it is this order:

1. Pre-check the budget, the price and the key -- all *before* any spend. A model
   with no price is refused here, not discovered after the money has gone.
2. Call the provider, retrying transient failures with backoff and, if the stage
   has one, falling back to another model once those are exhausted.
3. Price the response, then **charge the budget, then record the row**, and only
   then raise `BudgetExceeded` if the call crossed the cap. Charging precedes
   recording so a failed database write cannot leave the in-memory cap blind to
   money that was spent.
"""

import asyncio
import json
import os
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NoReturn

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Pin LiteLLM to the price map bundled with the installed version, *before* it is
# imported -- it reads this at import time, and by default fetches the current map
# from GitHub instead. A cost that changes because upstream edited a JSON file
# between two benchmark runs is not comparable across the set, which is the whole
# argument for recording cost from the start. `setdefault` leaves an explicit
# choice in the environment alone. Importing `litellm` anywhere before this
# module defeats the pin, so go through the gateway.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402
from litellm import exceptions as litellm_exceptions  # noqa: E402

from repolace_gateway.budget import TaskScope, current_scope  # noqa: E402
from repolace_gateway.config import (  # noqa: E402
    GatewayConfig,
    GatewaySettings,
    ModelConfig,
    Route,
    load_config,
)
from repolace_gateway.errors import LLMCallError, UnpricedModelError  # noqa: E402
from repolace_gateway.recorder import CallRecord, Recorder  # noqa: E402
from repolace_gateway.redaction import Redactor  # noqa: E402

# LiteLLM prints a "Give Feedback / Get Help" banner to stdout on errors, which
# lands in the middle of a stream of JSON log lines.
litellm.suppress_debug_info = True

log = structlog.get_logger()

#: Kwargs a caller may not pass through to LiteLLM. Each one either redirects the
#: provider key to another host (`api_base`, `base_url`, `custom_llm_provider`),
#: bypasses the gateway's own retry and fallback (`num_retries`, `fallbacks`),
#: or changes the response into something this module does not parse (`stream`).
_RESERVED_KWARGS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "api_key",
        "api_base",
        "base_url",
        "custom_llm_provider",
        "num_retries",
        "fallbacks",
        "stream",
    }
)

_EPHEMERAL = {"type": "ephemeral"}

#: Failures worth another try. Auth, bad-request, not-found and context-window
#: errors are deliberately absent: repeating them cannot help and each retry
#: would be a recorded, delayed copy of the same mistake.
_TRANSIENT_TYPES = tuple(
    cls
    for cls in (
        getattr(litellm_exceptions, name, None)
        for name in (
            "RateLimitError",
            "ServiceUnavailableError",
            "BadGatewayError",
            "InternalServerError",
            "APIConnectionError",
            "Timeout",
        )
    )
    if cls is not None
)

_MAX_ERROR_CHARS = 2000


@dataclass(frozen=True)
class TokenUsage:
    #: The total prompt, cached tokens included -- LiteLLM's and OpenAI's convention.
    input_tokens: int
    #: The cache-read part of `input_tokens`, billed at the discounted rate.
    cached_input_tokens: int
    #: Tokens written to the cache, which Anthropic bills at a premium.
    cache_write_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]
    #: What the model actually sent, kept because it is what must be echoed back
    #: in the assistant message, and because `arguments` is empty on a parse error.
    raw_arguments: str
    #: Set when `raw_arguments` was not a JSON object. Surfaced rather than
    #: collapsed to `{}`, so the loop can tell the model what it got wrong
    #: instead of reporting a baffling "missing argument".
    parse_error: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    stage: str
    #: The LiteLLM model id that served the call -- the fallback, if one did.
    model: str
    provider: str
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    usage: TokenUsage
    cost_usd: Decimal
    latency_ms: int
    #: The assistant turn as a plain dict, ready to append to the history. Built
    #: here rather than dumped from LiteLLM's object so the loop never depends on
    #: LiteLLM's message class, and tool calls round-trip exactly as sent.
    message: Mapping[str, Any]
    #: The `llm_calls` row this call was recorded as.
    call_id: uuid.UUID
    retries: int = 0
    fallback_used: bool = False


# --- cache markers -----------------------------------------------------------


def _mark(message: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of `message` whose last content block carries a cache breakpoint.

    A copy, never the caller's dict: the agent keeps its message history across
    turns, and a marker written into it would still be there next turn -- joined
    by a new one -- until the request exceeded Anthropic's four-breakpoint limit
    and was rejected.
    """
    out = dict(message)
    content = out.get("content")
    if isinstance(content, str):
        if content:
            out["content"] = [{"type": "text", "text": content, "cache_control": dict(_EPHEMERAL)}]
    elif isinstance(content, list) and content:
        blocks = [dict(block) if isinstance(block, Mapping) else block for block in content]
        if isinstance(blocks[-1], dict):
            blocks[-1]["cache_control"] = dict(_EPHEMERAL)
        out["content"] = blocks
    return out


def with_cache_control(
    messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Add Anthropic cache breakpoints, without touching the inputs.

    Three breakpoints, inside Anthropic's limit of four:

    * the **last tool**, which caches the whole tool block;
    * the **last system message**, which caches tools + system together (the
      cache prefix runs tools, then system, then messages);
    * the **last user or tool message** -- a rolling breakpoint. Without it the
      conversation itself is never cached, and a 40-step loop re-bills its entire
      growing history at the full input rate on every step. With it, each step
      reads the previous steps from the cache and pays the write premium only on
      what is new.

    Built here rather than through LiteLLM's `cache_control_injection_points` so
    it is a pure function with a test, and does not depend on how a particular
    LiteLLM release chooses to inject.
    """
    out = [dict(message) for message in messages]

    for index in range(len(out) - 1, -1, -1):
        if out[index].get("role") == "system":
            out[index] = _mark(out[index])
            break

    for index in range(len(out) - 1, -1, -1):
        role = out[index].get("role")
        if role == "tool":
            # LiteLLM reads a tool result's marker from the message, not from a
            # content block (factory.py: `message.get("cache_control")`).
            if out[index].get("content"):
                out[index]["cache_control"] = dict(_EPHEMERAL)
            break
        if role == "user":
            out[index] = _mark(out[index])
            break

    marked_tools: list[dict[str, Any]] | None = None
    if tools:
        marked_tools = [dict(tool) for tool in tools]
        marked_tools[-1]["cache_control"] = dict(_EPHEMERAL)
    return out, marked_tools


# --- reading a response ------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field off a LiteLLM object or a plain mapping alike."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def extract_usage(response: Any) -> TokenUsage:
    usage = _get(response, "usage")
    if usage is None:
        # Without usage there is nothing to price. Refuse rather than record a
        # free call -- see the module docstring.
        raise UnpricedModelError("the provider's response carried no usage, so the call cannot be priced")
    details = _get(usage, "prompt_tokens_details")
    return TokenUsage(
        input_tokens=_count(_get(usage, "prompt_tokens")),
        cached_input_tokens=_count(_get(details, "cached_tokens"))
        or _count(_get(usage, "cache_read_input_tokens")),
        # LiteLLM spells this three ways depending on the code path that built the
        # Usage; reading all of them is cheaper than being wrong about which.
        cache_write_tokens=_count(_get(usage, "cache_creation_input_tokens"))
        or _count(_get(details, "cache_creation_tokens"))
        or _count(_get(details, "cache_write_tokens")),
        output_tokens=_count(_get(usage, "completion_tokens")),
    )


def _parse_tool_call(raw: Any) -> ToolCall:
    function = _get(raw, "function")
    arguments = _get(function, "arguments")
    if isinstance(arguments, Mapping):
        raw_arguments, parsed, error = json.dumps(arguments), dict(arguments), None
    else:
        raw_arguments = arguments if isinstance(arguments, str) else ""
        parsed, error = {}, None
        if raw_arguments.strip():
            try:
                decoded = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                error = f"arguments are not valid JSON: {exc}"
            else:
                if isinstance(decoded, dict):
                    parsed = decoded
                else:
                    error = f"arguments must be a JSON object, got {type(decoded).__name__}"
    return ToolCall(
        id=str(_get(raw, "id") or ""),
        name=str(_get(function, "name") or ""),
        arguments=parsed,
        raw_arguments=raw_arguments,
        parse_error=error,
    )


def _assistant_message(content: str | None, tool_calls: Sequence[ToolCall]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments},
            }
            for call in tool_calls
        ]
    return message


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, _TRANSIENT_TYPES):
        return True
    # A provider status LiteLLM did not map to a specific class (Anthropic's 529
    # "overloaded" arrives this way) is still a server-side failure.
    status = getattr(exc, "status_code", None)
    return isinstance(exc, litellm_exceptions.APIError) and isinstance(status, int) and status >= 500


def _usd(value: float) -> Decimal:
    """A float dollar amount as a Decimal. `str` first: Decimal(0.1) is the binary expansion."""
    return Decimal(str(value))


class _ProviderFailure(Exception):
    """Internal: one model's retries are over. Carries what `complete` needs to decide next.

    A plain exception rather than a dataclass: a dataclass that defines `__eq__`
    without `__hash__` is unhashable, and exceptions do get hashed (tracebacks,
    exception groups, anything that keeps them in a set).
    """

    def __init__(self, cause: Exception, retries: int, transient: bool) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.retries = retries
        self.transient = transient


class LLMClient:
    def __init__(
        self,
        config: GatewayConfig,
        settings: GatewaySettings,
        recorder: Recorder,
        *,
        acompletion: Callable[..., Awaitable[Any]] | None = None,
        cost_fn: Callable[..., float] | None = None,
        model_info_fn: Callable[..., Mapping[str, Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        # The provider, cost and price lookups are injectable so a test can drive
        # every branch -- retry, fallback, an unpriced model -- without a network
        # and without waiting out a real backoff.
        self._config = config
        self._settings = settings
        self._recorder = recorder
        self._redactor = recorder.redactor
        self._acompletion = acompletion or litellm.acompletion
        self._cost_fn = cost_fn or litellm.completion_cost
        self._model_info_fn = model_info_fn or litellm.get_model_info
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._verified: set[str] = set()

    @classmethod
    def from_settings(
        cls,
        settings: GatewaySettings,
        session_factory: async_sessionmaker[AsyncSession],
        **kwargs: Any,
    ) -> "LLMClient":
        config = load_config(settings.models_path, settings.stage_models)
        recorder = Recorder(session_factory, Redactor(settings.secret_values()))
        return cls(config, settings, recorder, **kwargs)

    # -- pre-spend checks -----------------------------------------------------

    def _verify_usable(self, model: ModelConfig) -> None:
        """Refuse a model that cannot be priced or keyed, before any money is spent."""
        if model.key in self._verified:
            return
        self._settings.api_key_for(model.provider)
        if model.price is None:
            try:
                info = self._model_info_fn(model=model.litellm_model)
            except Exception as exc:
                raise self._unpriced(model) from exc
            if not (info.get("input_cost_per_token") and info.get("output_cost_per_token")):
                raise self._unpriced(model)
        self._verified.add(model.key)

    @staticmethod
    def _unpriced(model: ModelConfig) -> UnpricedModelError:
        return UnpricedModelError(
            f"model {model.key!r} ({model.litellm_model}) has no price in LiteLLM's price map and no "
            f"[price] table in models.toml. Add one under [models.{model.key}.price] (USD per million "
            "tokens), or use a model LiteLLM knows. Refused before the call: an unpriced call would "
            "be recorded as free."
        )

    def _price(self, model: ModelConfig, response: Any, usage: TokenUsage) -> Decimal:
        if model.price is not None:
            return model.price.cost(
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
            )
        try:
            cost = _usd(self._cost_fn(completion_response=response, model=model.litellm_model))
        except Exception as exc:
            raise UnpricedModelError(
                f"LiteLLM could not price a call to {model.litellm_model}: {type(exc).__name__}: {exc}"
            ) from exc
        # A zero from the price map is how an entry with missing rates reports
        # itself. Real tokens at zero cost is a missing price, not a free model.
        if cost <= 0 and (usage.input_tokens or usage.output_tokens):
            raise UnpricedModelError(
                f"LiteLLM priced a call to {model.litellm_model} at ${cost} for "
                f"{usage.input_tokens} input / {usage.output_tokens} output tokens"
            )
        return cost

    # -- the call -------------------------------------------------------------

    def _backoff(self, tries_so_far: int) -> float:
        policy = self._config.retry
        delay = min(policy.max_delay_seconds, policy.base_delay_seconds * 2 ** (tries_so_far - 1))
        # Half to full of the nominal delay, so a fleet of tasks that were all
        # rate-limited together does not come back together.
        return delay * (0.5 + self._jitter() / 2)

    async def _call_with_retries(self, model: ModelConfig, kwargs: dict[str, Any]) -> tuple[Any, int]:
        policy = self._config.retry
        retries = 0
        for tries in range(1, policy.max_attempts + 1):
            try:
                return await self._acompletion(**kwargs), retries
            except Exception as exc:  # CancelledError is a BaseException and passes through
                transient = _is_transient(exc)
                if not transient or tries >= policy.max_attempts:
                    raise _ProviderFailure(cause=exc, retries=retries, transient=transient) from exc
                delay = self._backoff(tries)
                log.warning(
                    "gateway.retry",
                    model=model.litellm_model,
                    attempt=tries,
                    delay_seconds=round(delay, 2),
                    error=self._redactor.text(f"{type(exc).__name__}: {exc}")[:_MAX_ERROR_CHARS],
                )
                retries += 1
                await self._sleep(delay)
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    def _build_request(
        self,
        route: Route,
        model: ModelConfig,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        use_cache: bool,
        extra: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        cached = use_cache and model.supports_prompt_caching
        if cached:
            sent_messages, sent_tools = with_cache_control(messages, tools)
        else:
            sent_messages = [dict(message) for message in messages]
            sent_tools = [dict(tool) for tool in tools] if tools else None
        kwargs: dict[str, Any] = {
            "model": model.litellm_model,
            "messages": sent_messages,
            # Per call, never exported: see the module docstring.
            "api_key": self._settings.api_key_for(model.provider),
            "timeout": self._config.retry.request_timeout_seconds,
            # Our retry loop is the only one, so that every retry is visible and bounded.
            "num_retries": 0,
        }
        if sent_tools:
            kwargs["tools"] = sent_tools
        if route.stage.max_tokens is not None:
            kwargs["max_tokens"] = route.stage.max_tokens
        kwargs.update(extra)
        return kwargs, cached

    @staticmethod
    def _request_payload(
        kwargs: Mapping[str, Any], *, stage: str, model: ModelConfig, cached: bool, retries: int,
        fallback_from: str | None,
    ) -> dict[str, Any]:
        """What is stored as `llm_calls.request`: what was sent, minus the key."""
        return {
            "model": kwargs["model"],
            "messages": kwargs["messages"],
            "tools": kwargs.get("tools"),
            "params": {
                key: value
                for key, value in kwargs.items()
                if key not in {"model", "messages", "tools", "api_key"}
            },
            "gateway": {
                "stage": stage,
                "model_key": model.key,
                "cache_markers": cached,
                "retries": retries,
                "fallback_from": fallback_from,
            },
        }

    async def complete(
        self,
        stage: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        attempt: int | None = None,
        cache: bool | None = None,
        **litellm_kwargs: Any,
    ) -> LLMResponse:
        """Make one model call for the task bound by `task_scope`.

        `attempt` is the edit attempt the call belongs to, or None outside one.
        `cache` overrides the stage's default for Anthropic cache markers. Any
        other keyword goes to LiteLLM (`temperature`, `tool_choice`,
        `max_tokens`, ...), except the few that would defeat the gateway
        (`_RESERVED_KWARGS`).

        Raises `BudgetExceeded` before a call the budget has nothing left for,
        and after a call that crossed the cap (carrying that call's response);
        `UnpricedModelError`, `MissingProviderKey` and `NoTaskScope` before any
        spend; `LLMCallError` when the provider fails for good.
        """
        reserved = _RESERVED_KWARGS & litellm_kwargs.keys()
        if reserved:
            raise TypeError(f"complete() cannot pass {sorted(reserved)} through to LiteLLM")

        scope = current_scope()
        scope.budget.raise_if_reached()

        route = self._config.route(stage)
        candidates = [route.primary] + ([route.fallback] if route.fallback else [])
        for candidate in candidates:
            self._verify_usable(candidate)
        use_cache = route.stage.cache if cache is None else cache

        started = self._clock()
        total_retries = 0
        failure: _ProviderFailure | None = None
        last: tuple[ModelConfig, dict[str, Any], bool] | None = None

        for position, model in enumerate(candidates):
            kwargs, cached = self._build_request(route, model, messages, tools, use_cache, litellm_kwargs)
            last = (model, kwargs, cached)
            try:
                response, retries = await self._call_with_retries(model, kwargs)
            except _ProviderFailure as failed:
                total_retries += failed.retries
                failure = failed
                # Only a transient failure earns the fallback. A bad request or an
                # auth error would fail identically on the next model, and would
                # spend a second model's quota to learn it.
                if failed.transient and position + 1 < len(candidates):
                    log.warning(
                        "gateway.fallback",
                        stage=stage,
                        failed_model=model.litellm_model,
                        fallback_model=candidates[position + 1].litellm_model,
                    )
                    continue
                break
            total_retries += retries
            return await self._finish(
                scope=scope,
                stage=stage,
                model=model,
                kwargs=kwargs,
                cached=cached,
                response=response,
                retries=total_retries,
                started=started,
                attempt=attempt,
                fallback_from=route.primary.litellm_model if position > 0 else None,
            )

        assert failure is not None and last is not None
        return await self._fail(
            scope=scope,
            stage=stage,
            model=last[0],
            kwargs=last[1],
            cached=last[2],
            failure=failure,
            retries=total_retries,
            started=started,
            attempt=attempt,
            fallback_from=route.primary.litellm_model if last[0] is not route.primary else None,
        )

    async def _finish(
        self,
        *,
        scope: TaskScope,
        stage: str,
        model: ModelConfig,
        kwargs: dict[str, Any],
        cached: bool,
        response: Any,
        retries: int,
        started: float,
        attempt: int | None,
        fallback_from: str | None,
    ) -> LLMResponse:
        latency_ms = int((self._clock() - started) * 1000)
        request = self._request_payload(
            kwargs, stage=stage, model=model, cached=cached, retries=retries, fallback_from=fallback_from
        )

        usage: TokenUsage | None = None
        try:
            usage = extract_usage(response)
            cost = self._price(model, response, usage)
        except UnpricedModelError as exc:
            # The call happened and cost something unknowable: count it, keep the
            # row (with whatever usage was readable) so the spend is not lost from
            # the record, and stop -- the measurement is broken.
            scope.budget.charge(None)
            await self._recorder.record(
                CallRecord(
                    task_id=scope.task_id,
                    attempt=attempt,
                    stage=stage,
                    model=model.litellm_model,
                    provider=model.provider,
                    input_tokens=usage.input_tokens if usage else None,
                    cached_input_tokens=usage.cached_input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    latency_ms=latency_ms,
                    request=request,
                    response=response,
                    error=f"unpriced: {exc}",
                )
            )
            raise

        # Charge before recording: if the insert fails, the in-memory cap must
        # still know the money is gone.
        scope.budget.charge(cost)

        choices = _get(response, "choices") or []
        choice = choices[0] if choices else None
        message = _get(choice, "message")
        content = _get(message, "content")
        content = content if isinstance(content, str) else None
        tool_calls = tuple(_parse_tool_call(raw) for raw in (_get(message, "tool_calls") or []))
        finish_reason = _get(choice, "finish_reason")

        call_id = await self._recorder.record(
            CallRecord(
                task_id=scope.task_id,
                attempt=attempt,
                stage=stage,
                model=model.litellm_model,
                provider=model.provider,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                request=request,
                response=response,
            )
        )
        result = LLMResponse(
            stage=stage,
            model=model.litellm_model,
            provider=model.provider,
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            usage=usage,
            cost_usd=cost,
            latency_ms=latency_ms,
            message=_assistant_message(content, tool_calls),
            call_id=call_id,
            retries=retries,
            fallback_used=fallback_from is not None,
        )
        log.info(
            "gateway.call",
            stage=stage,
            model=model.litellm_model,
            attempt=attempt,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=str(cost),
            latency_ms=latency_ms,
            retries=retries,
            spent_usd=str(scope.budget.spent_usd),
            tool_calls=len(tool_calls),
        )
        scope.budget.raise_if_exceeded(result)
        return result

    async def _fail(
        self,
        *,
        scope: TaskScope,
        stage: str,
        model: ModelConfig,
        kwargs: dict[str, Any],
        cached: bool,
        failure: _ProviderFailure,
        retries: int,
        started: float,
        attempt: int | None,
        fallback_from: str | None,
    ) -> NoReturn:
        message = self._redactor.text(f"{type(failure.cause).__name__}: {failure.cause}")[:_MAX_ERROR_CHARS]
        scope.budget.charge(None)
        await self._recorder.record(
            CallRecord(
                task_id=scope.task_id,
                attempt=attempt,
                stage=stage,
                model=model.litellm_model,
                provider=model.provider,
                latency_ms=int((self._clock() - started) * 1000),
                request=self._request_payload(
                    kwargs, stage=stage, model=model, cached=cached, retries=retries,
                    fallback_from=fallback_from,
                ),
                error=message,
            )
        )
        log.error("gateway.call.failed", stage=stage, model=model.litellm_model, retries=retries, error=message)
        raise LLMCallError(
            f"{stage} call to {model.litellm_model} failed after {retries} retries: {message}",
            stage=stage,
            model=model.litellm_model,
            retries=retries,
        ) from failure.cause
