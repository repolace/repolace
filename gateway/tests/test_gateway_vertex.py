"""Gemini on Vertex AI over REST: request mapping, response mapping, retries and key hygiene.

The HTTP layer is replaced with `httpx.MockTransport`, so nothing here touches the network. What a
mock cannot show -- that Google accepts the packed thought signature, the tool schemas and the
`?key=` form -- is covered only by the opt-in live test in `test_gateway_vertex_live.py`.
"""

import json
from decimal import Decimal

import httpx
import pytest
from gateway_support import (
    MESSAGES,
    TASK_ID,
    TOOLS,
    ListRecorder,
    make_settings,
    raw_config,
    stored_json,
)
from repolace_gateway.budget import TaskBudget, task_scope
from repolace_gateway.client import LLMClient
from repolace_gateway.config import DEFAULT_MODELS_PATH, load_config, parse_config
from repolace_gateway.errors import ConfigError, LLMCallError, MissingProviderKey
from repolace_gateway.redaction import Redactor
from repolace_gateway.vertex import VertexAPIError, VertexGeminiTransport, build_request, parse_response

pytestmark = pytest.mark.anyio

KEY = "AQ.Ab8RN6" + "Kx3Zq9" * 6  # shaped like a key, matches nothing real


def ok_body(parts, *, finish="STOP", usage=None):
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "usageMetadata": usage
        if usage is not None
        else {"promptTokenCount": 1000, "candidatesTokenCount": 40, "thoughtsTokenCount": 60, "cachedContentTokenCount": 400},
        "modelVersion": "gemini-test",
        "responseId": "r1",
    }


def vertex_config():
    raw = raw_config()
    raw["models"]["vertex"] = {
        "provider": "vertex_ai",
        "litellm_model": "vertex_ai/gemini-test",
        "supports_prompt_caching": True,
        "price": {"input": 1.0, "output": 10.0, "cache_read": 0.1, "cache_write": 0.0},
    }
    raw["stages"]["agent"] = {"model": "vertex", "cache": True, "max_tokens": 512}
    return parse_config(raw)


class Wire:
    """A scripted Vertex endpoint that records every request it receives."""

    def __init__(self, *responses: httpx.Response | Exception):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def json_response(status: int, body: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def make_vertex_client(wire: Wire, *, settings=None, recorder=None):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    settings = settings or make_settings(google_api_key=KEY)
    recorder = recorder or ListRecorder(Redactor(settings.secret_values()))
    client = LLMClient(vertex_config(), settings, recorder, sleep=fake_sleep, jitter=lambda: 1.0)
    client._vertex = VertexGeminiTransport(transport=httpx.MockTransport(wire))
    return client, recorder, sleeps


async def complete(client, tools=None, messages=None, **kwargs):
    with task_scope(TASK_ID, TaskBudget()):
        return await client.complete("agent", messages or MESSAGES, tools, **kwargs)


class TestAChat:
    async def test_a_normal_response_is_read_priced_and_recorded(self):
        wire = Wire(json_response(200, ok_body([{"text": "OK"}])))
        client, recorder, _ = make_vertex_client(wire)

        result = await complete(client)

        assert result.content == "OK" and result.tool_calls == () and result.finish_reason == "stop"
        # 1000 input of which 400 cached; output is the candidates' 40 plus the 60 thinking tokens.
        assert (result.usage.input_tokens, result.usage.cached_input_tokens, result.usage.output_tokens) == (1000, 400, 100)
        expected = (Decimal(600) * 1 + Decimal(400) * Decimal("0.1") + Decimal(100) * 10) / 1_000_000
        assert result.cost_usd == expected
        assert len(recorder.records) == 1 and recorder.records[0].provider == "vertex_ai"

    async def test_the_request_goes_to_the_vertex_endpoint_with_the_key_as_a_query_parameter(self):
        wire = Wire(json_response(200, ok_body([{"text": "OK"}])))
        client, _, _ = make_vertex_client(wire)

        await complete(client)

        request = wire.requests[0]
        assert request.url.host == "aiplatform.googleapis.com"
        assert request.url.path == "/v1/publishers/google/models/gemini-test:generateContent"
        assert request.url.params["key"] == KEY
        body = wire.bodies()[0]
        assert body["systemInstruction"] == {"parts": [{"text": "You fix bugs."}]}
        assert body["contents"][0]["role"] == "user"
        assert body["generationConfig"]["maxOutputTokens"] == 512

    async def test_a_response_with_no_usage_is_refused_not_recorded_as_free(self):
        body = ok_body([{"text": "OK"}])
        del body["usageMetadata"]
        client, recorder, _ = make_vertex_client(Wire(json_response(200, body)))

        from repolace_gateway.errors import UnpricedModelError

        with pytest.raises(UnpricedModelError):
            await complete(client)
        assert recorder.records[0].error and recorder.records[0].cost_usd is None


CALL = {"functionCall": {"name": "read_file", "args": {"path": "a.py"}}, "thoughtSignature": "SIG+/=="}


class TestToolCalls:

    async def test_a_tool_call_comes_back_in_the_gateways_shape(self):
        client, _, _ = make_vertex_client(Wire(json_response(200, ok_body([CALL]))))

        result = await complete(client, TOOLS)

        (call,) = result.tool_calls
        assert (call.name, dict(call.arguments)) == ("read_file", {"path": "a.py"})
        assert result.finish_reason == "tool_calls"
        assert call.id == "call_0~SIG+/=="

    async def test_the_thought_signature_is_sent_back_with_the_call_on_the_next_turn(self):
        wire = Wire(json_response(200, ok_body([CALL])), json_response(200, ok_body([{"text": "done"}])))
        client, _, _ = make_vertex_client(wire)
        first = await complete(client, TOOLS)

        history = [
            *MESSAGES,
            first.message,
            {"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": "print('hi')"},
        ]
        await complete(client, TOOLS, history)

        sent = wire.bodies()[1]["contents"]
        assert sent[1] == {
            "role": "model",
            "parts": [{"functionCall": {"name": "read_file", "args": {"path": "a.py"}}, "thoughtSignature": "SIG+/=="}],
        }
        assert sent[2] == {
            "role": "user",
            "parts": [{"functionResponse": {"name": "read_file", "response": {"content": "print('hi')"}}}],
        }

    def test_results_of_parallel_calls_are_returned_together_in_one_turn(self):
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_0", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                    {"id": "call_1", "type": "function", "function": {"name": "b", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_0", "content": "A"},
            {"role": "tool", "tool_call_id": "call_1", "content": "B"},
        ]

        contents = build_request(messages)["contents"]

        assert len(contents) == 3 and len(contents[2]["parts"]) == 2
        assert [p["functionResponse"]["name"] for p in contents[2]["parts"]] == ["a", "b"]

    def test_tool_schemas_keep_property_names_and_drop_keywords_gemini_rejects(self):
        tool = {
            "type": "function",
            "function": {
                "name": "t",
                "description": "d",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"default": {"type": "string", "default": "x", "maxLength": 5}},
                    "required": ["default"],
                },
            },
        }

        declaration = build_request([{"role": "user", "content": "x"}], [tool])["tools"][0]["functionDeclarations"][0]

        assert declaration["parameters"] == {
            "type": "object",
            "properties": {"default": {"type": "string", "maxLength": 5}},
            "required": ["default"],
        }

    def test_malformed_arguments_are_replayed_as_an_empty_object(self):
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c", "type": "function", "function": {"name": "a", "arguments": "{oops"}}],
            },
        ]
        part = build_request(messages)["contents"][1]["parts"][0]
        assert part["functionCall"]["args"] == {}

    def test_a_thinking_part_is_not_returned_as_the_answer(self):
        parsed = parse_response(ok_body([{"text": "secret reasoning", "thought": True}, {"text": "answer"}]))
        assert parsed["choices"][0]["message"]["content"] == "answer"


class TestRetries:
    def rate_limit(self, **headers):
        return json_response(
            429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}, headers or None
        )

    async def test_a_429_is_retried_with_backoff_and_then_succeeds(self):
        wire = Wire(self.rate_limit(), self.rate_limit(), json_response(200, ok_body([{"text": "OK"}])))
        client, recorder, sleeps = make_vertex_client(wire)

        result = await complete(client)

        assert result.content == "OK" and result.retries == 2
        assert sleeps == [1.0, 2.0]  # base 1 s doubling, jitter pinned
        assert len(wire.requests) == 3 and len(recorder.records) == 1

    async def test_a_server_error_is_retried(self):
        wire = Wire(json_response(503, {"error": {"message": "unavailable"}}), json_response(200, ok_body([{"text": "OK"}])))
        client, _, _ = make_vertex_client(wire)

        assert (await complete(client)).retries == 1

    async def test_retry_after_longer_than_the_backoff_is_honoured_up_to_the_ceiling(self):
        wire = Wire(self.rate_limit(**{"retry-after": "7"}), json_response(200, ok_body([{"text": "OK"}])))
        client, _, sleeps = make_vertex_client(wire)

        await complete(client)

        assert sleeps == [7.0]

    async def test_a_network_timeout_is_retried(self):
        wire = Wire(httpx.ReadTimeout("slow"), json_response(200, ok_body([{"text": "OK"}])))
        client, _, _ = make_vertex_client(wire)

        assert (await complete(client)).retries == 1

    async def test_exhausted_quota_is_reported_plainly_and_not_retried_forever(self):
        wire = Wire(self.rate_limit())
        client, recorder, _ = make_vertex_client(wire)

        with pytest.raises(LLMCallError) as raised:
            await complete(client)

        assert len(wire.requests) == 3  # the config's max_attempts, not unbounded
        assert "RESOURCE_EXHAUSTED" in str(raised.value) or "quota" in str(raised.value).lower()
        assert "RESOURCE_EXHAUSTED" in (recorder.records[0].error or "")

    async def test_a_bad_request_is_not_retried(self):
        wire = Wire(json_response(400, {"error": {"status": "INVALID_ARGUMENT", "message": "bad schema"}}))
        client, _, sleeps = make_vertex_client(wire)

        with pytest.raises(LLMCallError):
            await complete(client)

        assert len(wire.requests) == 1 and sleeps == []

    async def test_an_invalid_key_is_not_retried(self):
        wire = Wire(json_response(403, {"error": {"status": "PERMISSION_DENIED", "message": "API key not valid"}}))
        client, _, _ = make_vertex_client(wire)

        with pytest.raises(LLMCallError):
            await complete(client)

        assert len(wire.requests) == 1


class TestTheKey:
    async def test_a_missing_key_is_refused_before_any_request_and_names_the_variable(self):
        wire = Wire(json_response(200, ok_body([{"text": "OK"}])))
        client, _, _ = make_vertex_client(wire, settings=make_settings(google_api_key=None))

        with pytest.raises(MissingProviderKey, match="GOOGLE_API_KEY"):
            await complete(client)

        assert wire.requests == []

    async def test_the_key_is_in_no_stored_request_response_or_error(self):
        wire = Wire(json_response(200, ok_body([{"text": "OK"}])))
        client, recorder, _ = make_vertex_client(wire)

        await complete(client)

        assert KEY not in stored_json(recorder.records[0])

    async def test_a_connection_failure_does_not_put_the_url_or_key_in_the_error(self):
        wire = Wire(httpx.ConnectError(f"cannot reach https://x/?key={KEY}"))
        client, recorder, _ = make_vertex_client(wire)

        with pytest.raises(LLMCallError) as raised:
            await complete(client)

        assert KEY not in str(raised.value) and KEY not in stored_json(recorder.records[0])
        assert "ConnectError" in (recorder.records[0].error or "")

    async def test_a_caller_cannot_redirect_the_key_to_another_host(self):
        client, _, _ = make_vertex_client(Wire(json_response(200, ok_body([{"text": "OK"}]))))

        with pytest.raises(TypeError), task_scope(TASK_ID, TaskBudget()):
            await client.complete("agent", MESSAGES, api_base="https://evil.example")

    async def test_the_transport_itself_refuses_a_call_without_a_key(self):
        transport = VertexGeminiTransport(transport=httpx.MockTransport(Wire(json_response(200, {}))))
        with pytest.raises(VertexAPIError):
            await transport.acompletion(model="vertex_ai/x", messages=MESSAGES)

    def test_the_google_key_is_redacted_from_stored_text(self):
        settings = make_settings(google_api_key=KEY)
        assert KEY in settings.secret_values()
        assert KEY not in Redactor(settings.secret_values()).text(f"url ?key={KEY}")


class TestStreaming:
    async def test_server_sent_chunks_are_yielded_with_text_and_final_usage(self):
        chunks = [
            ok_body([{"text": "Hel"}], finish="", usage={"promptTokenCount": 5}),
            ok_body([{"text": "lo"}], usage={"promptTokenCount": 5, "candidatesTokenCount": 2}),
        ]
        body = "".join(f"data: {json.dumps(chunk)}\r\n\r\n" for chunk in chunks)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        transport = VertexGeminiTransport(transport=httpx.MockTransport(handler))

        out = [c async for c in transport.stream(model="vertex_ai/gemini-test", messages=MESSAGES, api_key=KEY)]

        assert "".join(c["text"] for c in out) == "Hello"
        assert out[-1]["usage"]["completion_tokens"] == 2
        assert seen[0].url.path.endswith(":streamGenerateContent") and seen[0].url.params["alt"] == "sse"

    async def test_a_failed_stream_raises_the_same_error_type(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "q"}})

        transport = VertexGeminiTransport(transport=httpx.MockTransport(handler))

        with pytest.raises(VertexAPIError) as raised:
            async for _ in transport.stream(model="vertex_ai/gemini-test", messages=MESSAGES, api_key=KEY):
                pass
        assert raised.value.transient and "RESOURCE_EXHAUSTED" in str(raised.value)


class TestShippedModels:
    @pytest.mark.parametrize("key", ["vertex-gemini-3-8-flash", "vertex-gemini-3-1-pro-preview"])
    def test_each_vertex_model_is_declared_priced_and_routable(self, key):
        config = load_config(DEFAULT_MODELS_PATH, stage_models={"agent": key})
        model = config.route("agent").primary
        assert model.provider == "vertex_ai" and model.litellm_model.startswith("vertex_ai/")
        assert model.price is not None  # prices live in models.toml, never in code

    @pytest.mark.parametrize("key", ["vertex-gemini-3-8-flash", "vertex-gemini-3-1-pro-preview"])
    def test_preflight_passes_with_a_key_and_names_the_variable_without_one(self, key):
        config = load_config(DEFAULT_MODELS_PATH, stage_models={"agent": key})
        recorder = ListRecorder()
        LLMClient(config, make_settings(google_api_key=KEY), recorder).preflight("agent")
        with pytest.raises(MissingProviderKey, match="GOOGLE_API_KEY"):
            LLMClient(config, make_settings(google_api_key=None), recorder).preflight("agent")


class TestHardening:
    @pytest.mark.parametrize("name", ["gemini?alt=sse", "gemini/other", "g#x", "a:b", "", "x y"])
    async def test_a_model_id_that_is_not_a_plain_id_never_reaches_a_url(self, name):
        wire = Wire(json_response(200, ok_body([{"text": "OK"}])))
        transport = VertexGeminiTransport(transport=httpx.MockTransport(wire))

        with pytest.raises(VertexAPIError):
            await transport.acompletion(model=f"vertex_ai/{name}", messages=MESSAGES, api_key=KEY)

        assert wire.requests == []

    def test_such_an_id_is_refused_when_the_config_loads(self):
        raw = raw_config()
        raw["models"]["bad"] = {
            "provider": "vertex_ai",
            "litellm_model": "vertex_ai/gemini?x=1",
            "supports_prompt_caching": False,
            "price": {"input": 1.0, "output": 2.0},
        }
        with pytest.raises(ConfigError, match="plain Vertex model id"):
            parse_config(raw)

    async def test_a_key_echoed_in_googles_error_text_is_scrubbed_from_the_exception(self):
        body = {"error": {"status": "INVALID_ARGUMENT", "message": f"key {KEY} is not valid"}}
        transport = VertexGeminiTransport(transport=httpx.MockTransport(Wire(json_response(400, body))))

        with pytest.raises(VertexAPIError) as raised:
            await transport.acompletion(model="vertex_ai/gemini-test", messages=MESSAGES, api_key=KEY)

        assert KEY not in str(raised.value) and "[REDACTED]" in str(raised.value)

    @pytest.mark.parametrize("payload", [[], "text", {"candidates": [5]}, {"candidates": [{"content": {"parts": [7]}}]}])
    async def test_an_odd_response_shape_is_a_clean_non_transient_error(self, payload):
        wire = Wire(json_response(200, payload))
        client, _, _ = make_vertex_client(wire)

        with pytest.raises(LLMCallError) as raised:
            await complete(client)

        assert len(wire.requests) == 1  # not retried: the same body would come back
        assert "AttributeError" not in str(raised.value) and "TypeError" not in str(raised.value)

    @pytest.mark.parametrize("signature", ["x" * 40000, 12345, ["a"], ""])
    def test_a_signature_that_is_oversized_or_not_a_string_is_not_packed_into_the_id(self, signature):
        parsed = parse_response(
            ok_body([{"functionCall": {"name": "read_file", "args": {}}, "thoughtSignature": signature}])
        )
        assert parsed["choices"][0]["message"]["tool_calls"][0]["id"] == "call_0"

    def test_a_signature_within_the_cap_is_packed(self):
        parsed = parse_response(
            ok_body([{"functionCall": {"name": "read_file", "args": {}}, "thoughtSignature": "s" * 10000}])
        )
        assert parsed["choices"][0]["message"]["tool_calls"][0]["id"] == "call_0~" + "s" * 10000


class TestReviewFindings:
    """Found by an independent review of the first version of this module."""

    async def test_the_request_log_httpx_writes_at_info_does_not_carry_the_key(self, caplog, monkeypatch):
        import logging

        from repolace_gateway import vertex

        # LiteLLM adds a redaction filter of its own to this logger and sets it to WARNING when it is
        # imported. Neither is something to rely on for a secret, so the test drops LiteLLM's filter
        # and puts the level back to INFO: only this module's filter is then between the key and the log.
        httpx_logger = logging.getLogger("httpx")
        monkeypatch.setattr(httpx_logger, "filters", [f for f in httpx_logger.filters if isinstance(f, vertex._RedactKeyInLogs)])
        wire = Wire(json_response(200, ok_body([{"text": "OK"}])))
        client, _, _ = make_vertex_client(wire)

        with caplog.at_level(logging.INFO, logger="httpx"):
            await complete(client)

        assert "HTTP Request" in caplog.text  # the log line exists, so the test can see what it checks
        assert KEY not in caplog.text and "key=[REDACTED]" in caplog.text

    async def test_a_malformed_function_call_reply_is_retried(self):
        bad = {"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL"}], "usageMetadata": {"promptTokenCount": 10}}
        wire = Wire(json_response(200, bad), json_response(200, ok_body([{"text": "OK"}])))
        client, _, _ = make_vertex_client(wire)

        result = await complete(client)

        assert result.content == "OK" and result.retries == 1

    async def test_a_blocked_prompt_is_a_clean_error_not_an_empty_reply(self):
        blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 10}}
        wire = Wire(json_response(200, blocked))
        client, _, _ = make_vertex_client(wire)

        with pytest.raises(LLMCallError, match="blocked the prompt"):
            await complete(client)

        assert len(wire.requests) == 1

    def test_an_empty_user_message_sends_no_empty_text_part(self):
        contents = build_request([{"role": "user", "content": ""}, {"role": "user", "content": "go"}])["contents"]
        assert contents == [{"role": "user", "parts": [{"text": "go"}]}]

    def test_an_expired_key_is_classified_as_an_auth_failure(self):
        from repolace_gateway.client import failure_kind

        error = VertexAPIError("API key expired. Please renew the API key.", status_code=400)
        assert failure_kind(error) == "auth"
