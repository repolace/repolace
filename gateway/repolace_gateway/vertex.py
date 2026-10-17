"""Gemini on Google Vertex AI, called over REST with a Vertex API key.

LiteLLM's `vertex_ai/` route authenticates with Google Cloud OAuth credentials, and its `gemini/`
route is Google AI Studio, which uses a different key and a different billing account. A Vertex API
key (`GOOGLE_API_KEY`) fits neither, so this module is a thin transport that speaks
`generateContent` directly and answers in the shape `LLMClient` already reads from LiteLLM:
`choices[0].message` (with `content` and OpenAI-style `tool_calls`), `finish_reason`, and `usage`.
Nothing downstream of the call (usage extraction, pricing, recording, tool-call parsing) changes.

Decisions that are easy to undo by accident:

* **The endpoint is fixed in code.** A caller cannot pass `api_base` (it is in the gateway's reserved
  kwargs), so the key cannot be redirected to another host.
* **The key travels as a query parameter**, the form the Vertex REST API documents. A URL is the thing
  logs and exception messages love to print, so every httpx failure is re-raised as a `VertexAPIError`
  built from the exception's *type* only (`from None`), never from its text or its request.
* **Thinking tokens are billed as output.** `usageMetadata.thoughtsTokenCount` is added to the
  candidates' tokens; leaving it out would understate the cost of every call by the thinking share.
* **A missing `usageMetadata` yields a response with no `usage`**, which `extract_usage` refuses:
  an unpriced call is never recorded as free.
* **Gemini 3 thought signatures.** Multi-turn function calling needs the `thoughtSignature` that came
  with a `functionCall` sent back with it, or the next request is rejected. The gateway's assistant
  message (`id`, `type`, `function`) has nowhere to carry it, so it is packed into the tool call `id`
  (`call_<n>~<signature>`) and unpacked when the history is rebuilt. Only a live call can confirm the
  API accepts the result; see `gateway/tests/test_gateway_vertex_live.py`.
"""

import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx


class _RedactKeyInLogs(logging.Filter):
    """httpx logs every request at INFO with its full URL, and the URL carries `?key=`."""

    _PATTERN = re.compile(r"([?&]key=)[^&\s\"']+")

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, (str, httpx.URL)):
            return self._PATTERN.sub(r"\1[REDACTED]", str(value))
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._scrub(arg) for arg in record.args)
        return True


# On the loggers themselves, not a handler: whoever configures logging later cannot route around it.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_RedactKeyInLogs())

#: Fixed on purpose: see the module docstring.
VERTEX_BASE_URL = "https://aiplatform.googleapis.com/v1/publishers/google/models"

#: Separates the call number from the thought signature inside a tool call id.
_SIGNATURE_SEPARATOR = "~"

#: A thought signature is opaque. Anything larger than this, or not a string, is not packed into the
#: tool call id (the id is echoed in every later request and stored); a missing signature then makes
#: Gemini 3 reject the next turn, which is why the cap is generous.
MAX_SIGNATURE_CHARS = 32768

#: What a Vertex model id may contain. It becomes a URL path segment, so `/`, `?`, `#` and `:` are out.
_MODEL_ID = re.compile(r"[A-Za-z0-9._-]+")

#: JSON-Schema keywords `functionDeclarations` accepts; anything else (`additionalProperties`,
#: `$schema`, `default`, ...) is dropped, because the API rejects the request rather than ignoring it.
_SCHEMA_KEYS = frozenset(
    {
        "type", "format", "description", "nullable", "enum", "properties", "required", "items",
        "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "pattern", "anyOf", "title",
    }
)

_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
}

_TOOL_CHOICES = {"auto": "AUTO", "required": "ANY", "none": "NONE"}


class VertexAPIError(Exception):
    """A failed Vertex call. `transient` decides whether the gateway retries it."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_status: str | None = None,
        retry_after: float | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_status = error_status
        self.retry_after = retry_after
        self.transient = transient


# --- request ------------------------------------------------------------------


def _clean_schema(schema: Any) -> Any:
    """`schema` reduced to the keywords Gemini accepts. Property *names* are never filtered."""
    if isinstance(schema, list):
        return [_clean_schema(item) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, Mapping):
            out[key] = {name: _clean_schema(sub) for name, sub in value.items()}
        else:
            out[key] = _clean_schema(value)
    return out


def _text_of(content: Any) -> str:
    """Plain text of a message's `content`, which is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return ""


def _split_id(call_id: str) -> tuple[str, str | None]:
    base, _, signature = call_id.partition(_SIGNATURE_SEPARATOR)
    return base, signature or None


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def build_request(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    tool_choice: Any = None,
) -> dict[str, Any]:
    """The `generateContent` body for an OpenAI-format conversation."""
    system_parts: list[dict[str, str]] = []
    contents: list[dict[str, Any]] = []
    names: dict[str, str] = {}  # tool call id -> function name, for the tool results that answer them

    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            text = _text_of(message.get("content"))
            if text:
                system_parts.append({"text": text})
        elif role == "user":
            text = _text_of(message.get("content"))
            if text:  # an empty text part is rejected
                contents.append({"role": "user", "parts": [{"text": text}]})
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            text = _text_of(message.get("content"))
            if text:
                parts.append({"text": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                call_id = str(call.get("id") or "")
                name = str(function.get("name") or "")
                names[call_id] = name
                part: dict[str, Any] = {
                    "functionCall": {"name": name, "args": _arguments(function.get("arguments"))}
                }
                signature = _split_id(call_id)[1]
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            part = {
                "functionResponse": {
                    "name": names.get(call_id, str(message.get("name") or "")),
                    "response": {"content": _text_of(message.get("content"))},
                }
            }
            # The answers to one model turn's calls go back together, in one user turn.
            if contents and contents[-1]["role"] == "user" and contents[-1].get("_tool_results"):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part], "_tool_results": True})

    for content in contents:
        content.pop("_tool_results", None)

    body: dict[str, Any] = {"contents": contents}
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    if tools:
        declarations = []
        for tool in tools:
            function = tool.get("function") or {}
            declaration: dict[str, Any] = {"name": function.get("name"), "description": function.get("description", "")}
            if function.get("parameters"):
                declaration["parameters"] = _clean_schema(function["parameters"])
            declarations.append(declaration)
        body["tools"] = [{"functionDeclarations": declarations}]
        config = _tool_config(tool_choice)
        if config:
            body["toolConfig"] = {"functionCallingConfig": config}
    generation: dict[str, Any] = {}
    if max_tokens is not None:
        generation["maxOutputTokens"] = max_tokens
    if temperature is not None:
        generation["temperature"] = temperature
    if generation:
        body["generationConfig"] = generation
    return body


def _tool_config(tool_choice: Any) -> dict[str, Any] | None:
    if isinstance(tool_choice, str) and tool_choice in _TOOL_CHOICES:
        return {"mode": _TOOL_CHOICES[tool_choice]}
    if isinstance(tool_choice, Mapping):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return {"mode": "ANY", "allowedFunctionNames": [name]}
    return None


# --- response -----------------------------------------------------------------


def _usage(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, Mapping) or "promptTokenCount" not in metadata:
        return None

    def count(name: str) -> int:
        value = metadata.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    prompt = count("promptTokenCount")
    output = count("candidatesTokenCount") + count("thoughtsTokenCount")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
        "prompt_tokens_details": {"cached_tokens": count("cachedContentTokenCount")},
    }


def parse_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A `generateContent` response as a LiteLLM-shaped mapping."""
    candidates = payload.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for part in (candidate.get("content") or {}).get("parts") or []:
        if "functionCall" in part:
            call = part["functionCall"]
            call_id = f"call_{len(tool_calls)}"
            signature = part.get("thoughtSignature")
            if isinstance(signature, str) and 0 < len(signature) <= MAX_SIGNATURE_CHARS:
                call_id += _SIGNATURE_SEPARATOR + signature
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call.get("name", ""), "arguments": json.dumps(call.get("args") or {})},
                }
            )
        elif part.get("text") and not part.get("thought"):
            texts.append(part["text"])
    reason = _FINISH_REASONS.get(str(candidate.get("finishReason") or ""))
    if tool_calls:
        reason = "tool_calls"
    elif reason is None and candidate.get("finishReason"):
        reason = str(candidate["finishReason"]).lower()
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    response: dict[str, Any] = {
        "id": payload.get("responseId"),
        "model": payload.get("modelVersion"),
        "choices": [{"index": 0, "message": message, "finish_reason": reason}],
    }
    usage = _usage(payload.get("usageMetadata"))
    if usage is not None:
        response["usage"] = usage
    return response


# --- transport ----------------------------------------------------------------


def is_valid_model_id(name: str) -> bool:
    return _MODEL_ID.fullmatch(name) is not None


def _model_name(model: str) -> str:
    """`vertex_ai/gemini-3.8-flash` -> `gemini-3.8-flash`, refusing anything that is not a plain id."""
    name = model.split("/", 1)[1] if "/" in model else model
    if not is_valid_model_id(name):
        raise VertexAPIError("the Vertex model id contains characters that are not allowed in a URL path")
    return name


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


def _error_from(response: httpx.Response, api_key: str = "") -> VertexAPIError:
    status = response.status_code
    error_status: str | None = None
    detail = ""
    try:
        error = response.json().get("error") or {}
        error_status = error.get("status")
        detail = str(error.get("message") or "")
    except (ValueError, AttributeError):
        pass
    if api_key:
        # Google does not echo a key in its error text as far as is known; if it ever did, the
        # exception must not be the place it escapes from.
        detail = detail.replace(api_key, "[REDACTED]")
    transient = status == 429 or status >= 500
    if error_status == "RESOURCE_EXHAUSTED" or status == 429:
        message = f"Vertex quota or rate limit exhausted (RESOURCE_EXHAUSTED, HTTP {status}): {detail[:300]}"
    else:
        message = f"Vertex returned HTTP {status}{f' {error_status}' if error_status else ''}: {detail[:300]}"
    return VertexAPIError(
        message, status_code=status, error_status=error_status, retry_after=_retry_after(response), transient=transient
    )


def _parsed(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise VertexAPIError("Vertex returned a body that is not JSON", status_code=200) from None
    if not isinstance(payload, Mapping):
        raise VertexAPIError("Vertex returned a body that is not a JSON object", status_code=200)
    try:
        parsed = parse_response(payload)
    except (AttributeError, TypeError, KeyError, IndexError):
        raise VertexAPIError("Vertex returned a response in an unexpected shape", status_code=200) from None
    choice = parsed["choices"][0]
    message = choice["message"]
    if not message["content"] and not message.get("tool_calls"):
        feedback = payload.get("promptFeedback")
        block = feedback.get("blockReason") if isinstance(feedback, Mapping) else None
        if block and not payload.get("candidates"):
            raise VertexAPIError(f"Vertex blocked the prompt ({block})", status_code=200)
        # A MALFORMED_FUNCTION_CALL (Gemini emitted a tool call it cannot itself parse) is returned as
        # an empty turn, not retried here: Vertex has billed its tokens, and a response thrown away
        # inside the transport would be spend that no `llm_calls` row records. The agent loop sees a
        # reply with no tool call, nudges, and asks again, with the call priced and counted.
    return parsed


class VertexGeminiTransport:
    """`await transport.acompletion(model=..., messages=..., api_key=..., ...)`, like LiteLLM's."""

    def __init__(
        self,
        *,
        base_url: str = VERTEX_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._transport = transport

    def _prepare(self, kwargs: Mapping[str, Any], endpoint: str) -> tuple[str, dict[str, Any], dict[str, str], float]:
        api_key = kwargs.get("api_key")
        if not api_key:
            raise VertexAPIError("no Vertex API key was passed to the transport")
        body = build_request(
            kwargs["messages"],
            kwargs.get("tools"),
            max_tokens=kwargs.get("max_tokens"),
            temperature=kwargs.get("temperature"),
            tool_choice=kwargs.get("tool_choice"),
        )
        url = f"{self._base_url}/{_model_name(str(kwargs['model']))}:{endpoint}"
        params = {"key": str(api_key)}
        return url, body, params, float(kwargs.get("timeout") or 180.0)

    async def acompletion(self, **kwargs: Any) -> dict[str, Any]:
        url, body, params, timeout = self._prepare(kwargs, "generateContent")
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                response = await client.post(url, params=params, json=body)
        except httpx.TimeoutException:
            raise VertexAPIError("Vertex request timed out", transient=True) from None
        except httpx.HTTPError as exc:
            # The type only: an httpx error's text can carry the request URL, and the URL carries the key.
            raise VertexAPIError(f"Vertex request failed: {type(exc).__name__}", transient=True) from None
        if response.status_code != 200:
            raise _error_from(response, params["key"])
        return _parsed(response)

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Server-sent chunks as `{"text", "tool_calls", "finish_reason", "usage"}` dicts.

        Not used by `LLMClient.complete`, which is non-streaming by design; here for callers that
        want incremental text.
        """
        url, body, params, timeout = self._prepare(kwargs, "streamGenerateContent")
        params["alt"] = "sse"
        try:
            async with (
                httpx.AsyncClient(timeout=timeout, transport=self._transport) as client,
                client.stream("POST", url, params=params, json=body) as response,
            ):
                if response.status_code != 200:
                    await response.aread()
                    raise _error_from(response, params["key"])
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data:
                        continue
                    try:
                        parsed = parse_response(json.loads(data))
                    except ValueError:
                        continue
                    choice = parsed["choices"][0]
                    yield {
                        "text": choice["message"]["content"] or "",
                        "tool_calls": choice["message"].get("tool_calls") or [],
                        "finish_reason": choice["finish_reason"],
                        "usage": parsed.get("usage"),
                    }
        except httpx.TimeoutException:
            raise VertexAPIError("Vertex request timed out", transient=True) from None
        except httpx.HTTPError as exc:
            raise VertexAPIError(f"Vertex request failed: {type(exc).__name__}", transient=True) from None
