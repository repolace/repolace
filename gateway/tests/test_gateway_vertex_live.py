"""Opt-in live checks against Vertex AI. Skipped unless `RUN_LIVE_LLM_TESTS=1`.

These spend a few cents and need `GOOGLE_API_KEY` in `.env` (or the environment). They are the only
tests that can show what a mock cannot: that Google accepts the `?key=` request, the tool schemas and
the thought signature packed into a tool call id, and that `usageMetadata` carries what the cost
arithmetic assumes. Run:

    RUN_LIVE_LLM_TESTS=1 uv run pytest gateway/tests/test_gateway_vertex_live.py -s
"""

import os

import pytest
from gateway_support import TOOLS
from repolace_gateway.config import GatewaySettings
from repolace_gateway.vertex import VertexGeminiTransport

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_LLM_TESTS") != "1", reason="set RUN_LIVE_LLM_TESTS=1 to call Vertex AI"),
]

MODELS = ["vertex_ai/gemini-3.8-flash", "vertex_ai/gemini-3.1-pro-preview"]


def api_key() -> str:
    # The gateway's own settings object: reads `.env` without exporting anything into os.environ.
    return GatewaySettings().api_key_for("vertex_ai")


@pytest.mark.parametrize("model", MODELS)
async def test_the_model_answers_ok_and_reports_usage(model):
    transport = VertexGeminiTransport()

    response = await transport.acompletion(
        model=model,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        api_key=api_key(),
        max_tokens=512,
        timeout=120,
    )

    assert (response["choices"][0]["message"]["content"] or "").strip() == "OK"
    usage = response["usage"]
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0


async def test_a_tool_call_survives_a_second_turn_with_its_thought_signature():
    transport = VertexGeminiTransport()
    key = api_key()
    messages = [{"role": "user", "content": "Use the read_file tool to read README.md, then say DONE."}]

    first = await transport.acompletion(
        model=MODELS[1], messages=messages, tools=TOOLS, api_key=key, max_tokens=1024, timeout=120
    )
    message = first["choices"][0]["message"]
    assert message.get("tool_calls"), "the model did not call the tool"
    call = message["tool_calls"][0]

    messages += [message, {"role": "tool", "tool_call_id": call["id"], "content": "# repolace"}]
    second = await transport.acompletion(
        model=MODELS[1], messages=messages, tools=TOOLS, api_key=key, max_tokens=1024, timeout=120
    )

    # Accepting the packed signature is what is under test: a rejected one is an HTTP 400, which raises
    # above. The model may answer in text or call the `submit` tool (TOOLS has one), so either is a pass.
    answer = second["choices"][0]["message"]
    assert answer.get("content") or answer.get("tool_calls"), "the second turn came back empty"
