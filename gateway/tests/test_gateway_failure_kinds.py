"""A provider refusing us (a key it rejects, a quota that is spent) is not an agent failure.

`LLMCallError.kind` carries the difference so the agent loop and the pipeline can fail the task as a
harness error instead of scoring it as `llm_error` inside the benchmark's denominator. Also here: a
call that outlives the task's wall clock ends as a budget stop, not as a hung call.
"""

import asyncio

import httpx
import pytest
from gateway_support import MESSAGES, TASK_ID, ListRecorder, make_harness, make_settings
from litellm import exceptions as lx
from repolace_gateway.budget import BudgetExceeded, BudgetLimit, TaskBudget, task_scope
from repolace_gateway.client import LLMClient, failure_kind
from repolace_gateway.errors import (
    AUTH_FAILURE_PREFIX,
    KIND_AUTH,
    KIND_OTHER,
    KIND_QUOTA,
    PROVIDER_FAILURE_MARKERS,
    QUOTA_FAILURE_PREFIX,
    LLMCallError,
)
from repolace_gateway.vertex import VertexAPIError, VertexGeminiTransport
from test_gateway_vertex import KEY, Wire, json_response, make_vertex_client, ok_body

pytestmark = pytest.mark.anyio


async def complete(client):
    with task_scope(TASK_ID, TaskBudget()):
        return await client.complete("agent", MESSAGES)


class TestClassification:
    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (VertexAPIError("x", status_code=401), KIND_AUTH),
            (VertexAPIError("x", status_code=403, error_status="PERMISSION_DENIED"), KIND_AUTH),
            (VertexAPIError("x", status_code=400, error_status="INVALID_ARGUMENT"), KIND_OTHER),
            (VertexAPIError("API key not valid. Please pass a valid API key.", status_code=400), KIND_AUTH),
            (VertexAPIError("x", status_code=429, error_status="RESOURCE_EXHAUSTED", transient=True), KIND_QUOTA),
            (VertexAPIError("x", status_code=503, transient=True), KIND_OTHER),
            (lx.AuthenticationError(message="bad key", llm_provider="anthropic", model="m"), KIND_AUTH),
            (lx.PermissionDeniedError(message="no", llm_provider="anthropic", model="m", response=httpx.Response(403, request=httpx.Request("POST", "https://x"))), KIND_AUTH),
            (lx.RateLimitError(message="slow", llm_provider="anthropic", model="m"), KIND_QUOTA),
            (lx.BadRequestError(message="Your credit balance is too low", llm_provider="anthropic", model="m"), KIND_AUTH),
            (lx.BadRequestError(message="messages must not be empty", llm_provider="anthropic", model="m"), KIND_OTHER),
            (ValueError("anything else"), KIND_OTHER),
        ],
    )
    def test_the_cause_decides_the_kind(self, error, kind):
        assert failure_kind(error) == kind


class TestWhatReachesTheCaller:
    async def test_a_rejected_vertex_key_is_an_auth_failure_with_the_fixed_prefix_and_is_not_retried(self):
        wire = Wire(json_response(403, {"error": {"status": "PERMISSION_DENIED", "message": "key rejected"}}))
        client, recorder, _ = make_vertex_client(wire)

        with pytest.raises(LLMCallError) as raised:
            await complete(client)

        assert raised.value.kind == KIND_AUTH and raised.value.provider_refused
        assert str(raised.value).startswith(AUTH_FAILURE_PREFIX)
        assert len(wire.requests) == 1
        assert recorder.records[0].error  # still recorded, as before

    async def test_spent_vertex_quota_is_a_quota_failure_after_the_bounded_retries(self):
        body = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}
        wire = Wire(json_response(429, body))
        client, _, _ = make_vertex_client(wire)

        with pytest.raises(LLMCallError) as raised:
            await complete(client)

        assert raised.value.kind == KIND_QUOTA and str(raised.value).startswith(QUOTA_FAILURE_PREFIX)
        assert len(wire.requests) == 3  # the test config's max_attempts

    async def test_a_litellm_authentication_error_is_an_auth_failure(self):
        error = lx.AuthenticationError(message="bad key", llm_provider="anthropic", model="test-main")
        harness = make_harness(error)

        with pytest.raises(LLMCallError) as raised:
            await complete(harness.client)

        assert raised.value.kind == KIND_AUTH

    async def test_an_ordinary_failure_keeps_kind_other_and_has_no_prefix(self):
        bad = lx.BadRequestError(message="messages must not be empty", llm_provider="anthropic", model="test-main")
        harness = make_harness(bad)

        with pytest.raises(LLMCallError) as raised:
            await complete(harness.client)

        assert raised.value.kind == KIND_OTHER and not raised.value.provider_refused
        assert not str(raised.value).startswith(tuple(PROVIDER_FAILURE_MARKERS))


class TestTheWallClockInsideACall:
    async def test_a_call_that_hangs_past_the_wall_clock_ends_as_a_budget_stop(self):
        async def hang(**kwargs):
            await asyncio.sleep(30)

        harness = make_harness()
        client = LLMClient(
            harness.client._config, make_settings(), ListRecorder(), acompletion=hang, jitter=lambda: 1.0
        )

        with pytest.raises(BudgetExceeded) as raised, task_scope(TASK_ID, TaskBudget(max_wall_seconds=0.2)):
            await client.complete("agent", MESSAGES)

        assert raised.value.limit is BudgetLimit.WALL_TIME

    async def test_no_retry_starts_after_the_wall_clock_has_run_out(self):
        calls = 0

        async def slow_failure(**kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.25)
            raise lx.InternalServerError(message="overloaded", llm_provider="anthropic", model="m")

        harness = make_harness()

        async def no_sleep(seconds):
            return None

        client = LLMClient(
            harness.client._config, make_settings(), ListRecorder(), acompletion=slow_failure,
            sleep=no_sleep, jitter=lambda: 1.0,
        )

        with pytest.raises(BudgetExceeded), task_scope(TASK_ID, TaskBudget(max_wall_seconds=0.3)):
            await client.complete("agent", MESSAGES)

        assert calls <= 2  # not the 3 the config allows


class TestAnAbandonedCallIsStillRecorded:
    async def test_a_call_cut_off_by_the_wall_clock_is_counted_and_keeps_its_row(self):
        async def hang(**kwargs):
            await asyncio.sleep(30)

        harness = make_harness()
        recorder = ListRecorder()
        client = LLMClient(harness.client._config, make_settings(), recorder, acompletion=hang, jitter=lambda: 1.0)
        budget = TaskBudget(max_wall_seconds=0.2)

        with pytest.raises(BudgetExceeded), task_scope(TASK_ID, budget):
            await client.complete("agent", MESSAGES)

        assert budget.calls == 1  # the call happened, even though nothing came back to price
        assert len(recorder.records) == 1 and recorder.records[0].error.startswith("abandoned")


class TestVertexOddities:
    async def test_the_transport_error_for_a_missing_key_is_not_a_provider_refusal(self):
        transport = VertexGeminiTransport(transport=httpx.MockTransport(Wire(json_response(200, ok_body([])))))
        with pytest.raises(VertexAPIError) as raised:
            await transport.acompletion(model="vertex_ai/gemini-test", messages=MESSAGES)
        assert failure_kind(raised.value) == KIND_OTHER
        assert KEY not in str(raised.value)
