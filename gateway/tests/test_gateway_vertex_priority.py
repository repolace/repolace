import asyncio

import httpx
import pytest
from repolace_gateway.config import ConfigError, load_config, parse_config
from repolace_gateway.vertex import VertexAPIError, VertexGeminiTransport


def _headers_sent(**extra) -> httpx.Headers:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}})

    transport = VertexGeminiTransport(transport=httpx.MockTransport(handler))
    call = transport.acompletion(
        model="vertex_ai/gemini-3.1-pro-preview",
        messages=[{"role": "user", "content": "hi"}],
        api_key="test-key",
        **extra,
    )
    with pytest.raises(VertexAPIError):
        asyncio.run(call)
    return seen[0]


def test_standard_requests_carry_no_priority_headers():
    headers = _headers_sent()
    assert "x-vertex-ai-llm-shared-request-type" not in headers
    assert "x-vertex-ai-llm-request-type" not in headers


def test_priority_requests_carry_both_priority_headers():
    headers = _headers_sent(vertex_request_type="priority")
    assert headers["x-vertex-ai-llm-shared-request-type"] == "priority"
    assert headers["x-vertex-ai-llm-request-type"] == "shared"


def test_shipped_config_has_a_priced_priority_model_and_standard_is_unchanged():
    config = load_config()
    priority = config.models["vertex-gemini-3-1-pro-preview-priority"]
    standard = config.models["vertex-gemini-3-1-pro-preview"]
    assert priority.request_type == "priority"
    assert standard.request_type is None
    assert priority.litellm_model == standard.litellm_model
    assert priority.price is not None and standard.price is not None
    assert priority.price.input > standard.price.input


def test_request_type_is_refused_for_other_providers_and_values():
    base = {
        "gateway": {"max_attempts": 2, "base_delay_seconds": 1.0, "max_delay_seconds": 2.0, "request_timeout_seconds": 10.0},
        "stages": {"agent": {"model": "m"}, "cheap": {"model": "m"}},
    }
    for provider, litellm_model, value in (
        ("openai", "openai/gpt-x", "priority"),
        ("vertex_ai", "vertex_ai/gemini-3.1-pro-preview", "flex"),
    ):
        table = {
            **base,
            "models": {
                "m": {
                    "provider": provider,
                    "litellm_model": litellm_model,
                    "supports_prompt_caching": False,
                    "request_type": value,
                }
            },
        }
        with pytest.raises(ConfigError, match="request_type"):
            parse_config(table)
