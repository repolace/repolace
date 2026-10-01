"""`LLMClient.complete`: what it sends, what it keeps, and when it refuses.

Driven by a scripted fake provider (`FakeAcompletion`), which fails the test if
called more often than scripted -- so "the call was refused *before* it was made"
is an assertion about `acompletion.calls`, not a hope. Backoff is instant and
exact (`sleeps` records, jitter is pinned), so retry timing is asserted rather
than waited for.

Pricing has its own file; budget arithmetic has its own. This one is about the
client as a whole: ordering (charge, then record, then raise), credentials, retry
and fallback, and the refusals that must happen before any money moves.
"""

import copy
import json
import os
from decimal import Decimal

import pytest
from litellm import exceptions as lx

from repolace_gateway.budget import BudgetExceeded, BudgetLimit, TaskBudget, task_scope
from repolace_gateway.client import TokenUsage
from repolace_gateway.config import parse_config
from repolace_gateway.errors import ConfigError, LLMCallError, MissingProviderKey, NoTaskScope

from gateway_support import (
    ANTHROPIC_KEY,
    MESSAGES,
    OPENAI_KEY,
    TASK_ID,
    TOOLS,
    FakeClock,
    ListRecorder,
    make_config,
    make_harness,
    make_priced_response,
    make_response,
    make_settings,
    raw_config,
    stored_json,
)


def rate_limited():
    return lx.RateLimitError(message="slow down", llm_provider="anthropic", model="test-main")


def overloaded():
    return lx.InternalServerError(message="overloaded", llm_provider="anthropic", model="test-main")


def unauthorised():
    return lx.AuthenticationError(message="bad key", llm_provider="anthropic", model="test-main")


def malformed():
    # `model` before `llm_provider`, unlike its siblings; keywords make that irrelevant.
    return lx.BadRequestError(message="bad request", model="test-main", llm_provider="anthropic")


@pytest.mark.anyio
class TestASuccessfulCall:
    async def test_the_response_carries_content_usage_cost_and_latency(self):
        h = make_harness(
            make_response("hello", prompt_tokens=1000, completion_tokens=200, cache_read=600, cache_write=100),
            clock=FakeClock(step=0.25),
        )
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES, TOOLS, attempt=2)

        assert response.content == "hello"
        assert response.stage == "agent"
        assert response.model == "anthropic/test-main"
        assert response.provider == "anthropic"
        assert response.finish_reason == "stop"
        assert response.usage == TokenUsage(
            input_tokens=1000, cached_input_tokens=600, cache_write_tokens=100, output_tokens=200
        )
        assert response.cost_usd == Decimal("0.004455")
        assert response.latency_ms == 250
        assert (response.retries, response.fallback_used) == (0, False)

    async def test_exactly_one_row_is_recorded_for_the_task_and_attempt(self):
        h = make_harness(make_response(prompt_tokens=1000, completion_tokens=200, cache_read=600))
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES, attempt=2)

        (record,) = h.recorder.records
        assert (record.task_id, record.attempt, record.stage) == (TASK_ID, 2, "agent")
        assert (record.model, record.provider) == ("anthropic/test-main", "anthropic")
        assert (record.input_tokens, record.cached_input_tokens, record.output_tokens) == (1000, 600, 200)
        assert record.cost_usd == response.cost_usd
        assert record.error is None
        assert record.response is not None

    async def test_a_call_outside_any_attempt_is_recorded_with_no_attempt(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("cheap", MESSAGES)
        assert h.recorder.records[0].attempt is None

    async def test_the_call_is_charged_to_the_tasks_budget(self):
        h = make_harness(make_response(prompt_tokens=1000, completion_tokens=200, cache_read=600, cache_write=100))
        with task_scope(TASK_ID, TaskBudget()) as scope:
            await h.client.complete("agent", MESSAGES)
        assert (scope.budget.calls, scope.budget.spent_usd) == (1, Decimal("0.004455"))

    async def test_tool_calls_are_parsed_and_the_assistant_turn_round_trips(self):
        h = make_harness(
            make_response(
                None,
                tool_calls=[("call_1", "read_file", '{"path": "a.py"}'), ("call_2", "submit", "not json")],
            )
        )
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES, TOOLS)

        first, second = response.tool_calls
        assert (first.id, first.name, first.arguments, first.parse_error) == (
            "call_1", "read_file", {"path": "a.py"}, None
        )
        # A malformed argument string is surfaced, not silently collapsed to {}.
        assert second.arguments == {} and second.raw_arguments == "not json" and second.parse_error
        assert response.finish_reason == "tool_calls"
        assert response.content is None
        assert response.message == {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
                {"id": "call_2", "type": "function", "function": {"name": "submit", "arguments": "not json"}},
            ],
        }

    async def test_a_json_array_where_an_object_belongs_is_a_parse_error(self):
        h = make_harness(make_response(None, tool_calls=[("c", "read_file", "[1, 2]")]))
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES, TOOLS)
        assert response.tool_calls[0].arguments == {}
        assert "JSON object" in response.tool_calls[0].parse_error

    async def test_a_plain_text_answer_has_no_tool_calls_and_a_plain_message(self):
        h = make_harness(make_response("just text"))
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("cheap", MESSAGES)
        assert response.tool_calls == ()
        assert response.message == {"role": "assistant", "content": "just text"}

    async def test_the_request_is_stored_with_the_gateways_notes_and_no_key(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES, TOOLS, temperature=0.0)

        request = h.recorder.records[0].request
        assert request["model"] == "anthropic/test-main"
        assert request["messages"] and request["tools"]
        assert request["params"]["temperature"] == 0.0
        assert "api_key" not in request["params"]
        assert request["gateway"] == {
            "stage": "agent",
            "model_key": "main",
            "cache_markers": True,
            "retries": 0,
            "fallback_from": None,
        }

    async def test_the_assistant_message_can_be_appended_straight_to_the_history(self):
        h = make_harness(
            make_response(None, tool_calls=[("call_1", "read_file", "{}")]), make_response("done")
        )
        with task_scope(TASK_ID, TaskBudget()):
            first = await h.client.complete("agent", MESSAGES, TOOLS)
            history = [*MESSAGES, first.message, {"role": "tool", "tool_call_id": "call_1", "content": "x"}]
            second = await h.client.complete("agent", history, TOOLS)
        assert second.content == "done"
        assert h.acompletion.calls[1]["messages"][2]["tool_calls"][0]["id"] == "call_1"


@pytest.mark.anyio
class TestWhatIsSentToLiteLLM:
    async def test_the_stage_settings_and_our_own_retry_policy_are_applied(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES, TOOLS)
        sent = h.acompletion.calls[0]
        assert sent["model"] == "anthropic/test-main"
        assert sent["max_tokens"] == 4096
        assert sent["timeout"] == 30.0
        # LiteLLM must not retry on its own: ours is the only loop, so every retry is visible and bounded.
        assert sent["num_retries"] == 0

    async def test_a_caller_keyword_overrides_the_stage_default_and_passes_through(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES, max_tokens=100, temperature=0.2, tool_choice="auto")
        sent = h.acompletion.calls[0]
        assert (sent["max_tokens"], sent["temperature"], sent["tool_choice"]) == (100, 0.2, "auto")

    async def test_no_tools_means_no_tools_key(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("cheap", MESSAGES)
        assert "tools" not in h.acompletion.calls[0]

    async def test_the_cheap_stage_uses_its_own_model_and_token_cap(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("cheap", MESSAGES)
        sent = h.acompletion.calls[0]
        assert (sent["model"], sent["max_tokens"]) == ("anthropic/test-small", 256)


@pytest.mark.anyio
class TestCredentials:
    async def test_the_providers_key_is_passed_per_call(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES)
        assert h.acompletion.calls[0]["api_key"] == ANTHROPIC_KEY

    async def test_the_key_never_reaches_the_stored_record(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES, TOOLS)
        assert ANTHROPIC_KEY not in stored_json(h.recorder.records[0])

    async def test_the_key_is_never_exported_into_the_process_environment(self):
        """So a subprocess -- git, docker, the sandbox -- cannot inherit it."""
        before = dict(os.environ)
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES)
        assert dict(os.environ) == before
        assert ANTHROPIC_KEY not in os.environ.values()

    @pytest.mark.parametrize("kwarg", ["api_base", "base_url", "api_key", "custom_llm_provider"])
    async def test_a_caller_cannot_redirect_the_key_to_another_host(self, kwarg):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(TypeError, match="cannot pass"):
                await h.client.complete("agent", MESSAGES, **{kwarg: "https://evil.example"})
        assert h.acompletion.calls == []

    @pytest.mark.parametrize("kwarg", ["num_retries", "fallbacks", "stream"])
    async def test_a_caller_cannot_bypass_the_gateways_own_loop_or_response_shape(self, kwarg):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(TypeError, match="cannot pass"):
                await h.client.complete("agent", MESSAGES, **{kwarg: 1})

    async def test_a_provider_error_that_echoes_the_key_is_scrubbed_everywhere_it_surfaces(self):
        h = make_harness(RuntimeError(f"auth failed for key {ANTHROPIC_KEY}"))
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(LLMCallError) as raised:
                await h.client.complete("agent", MESSAGES)
        assert ANTHROPIC_KEY not in str(raised.value)
        assert ANTHROPIC_KEY not in h.recorder.records[0].error


@pytest.mark.anyio
class TestCaching:
    @staticmethod
    def sent_json(h, index=0) -> str:
        return json.dumps([h.acompletion.calls[index]["messages"], h.acompletion.calls[index].get("tools")])

    async def test_the_agent_stage_marks_tools_system_and_the_latest_turn(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES, TOOLS)
        assert self.sent_json(h).count('"cache_control"') == 3
        assert h.recorder.records[0].request["gateway"]["cache_markers"] is True

    async def test_the_cheap_stage_sends_no_markers(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("cheap", MESSAGES, TOOLS)
        assert "cache_control" not in self.sent_json(h)

    async def test_an_explicit_false_overrides_a_stage_that_caches(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES, TOOLS, cache=False)
        assert "cache_control" not in self.sent_json(h)
        assert h.recorder.records[0].request["gateway"]["cache_markers"] is False

    async def test_an_explicit_true_enables_it_on_a_stage_that_does_not_default_to_it(self):
        # Point the cheap stage (cache = false) at the model that supports caching.
        config = make_config().with_stage_models({"cheap": "main"})
        h = make_harness(make_response(), make_response(), config=config)
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("cheap", MESSAGES, TOOLS)
            await h.client.complete("cheap", MESSAGES, TOOLS, cache=True)
        assert "cache_control" not in self.sent_json(h, 0)
        assert self.sent_json(h, 1).count('"cache_control"') == 3

    async def test_a_model_that_does_not_support_caching_gets_no_markers_even_when_asked(self):
        h = make_harness(make_response(), config=make_config(agent_model="small"))
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES, TOOLS)  # the agent stage defaults to caching
        assert "cache_control" not in self.sent_json(h)
        assert h.recorder.records[0].request["gateway"]["cache_markers"] is False

    async def test_the_callers_history_is_not_marked(self):
        h = make_harness(make_response())
        messages, tools = copy.deepcopy(MESSAGES), copy.deepcopy(TOOLS)
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", messages, tools)
        assert (messages, tools) == (MESSAGES, TOOLS)

    async def test_marking_every_turn_never_exceeds_the_providers_breakpoint_limit(self):
        h = make_harness(make_response(), repeat_last=True)
        history = copy.deepcopy(MESSAGES)
        with task_scope(TASK_ID, TaskBudget(max_calls=100, max_usd=100)):
            for turn in range(8):
                await h.client.complete("agent", history, TOOLS)
                history += [
                    {"role": "assistant", "content": None, "tool_calls": []},
                    {"role": "tool", "tool_call_id": f"c{turn}", "content": f"result {turn}"},
                ]
        for index in range(8):
            assert self.sent_json(h, index).count('"cache_control"') <= 4


@pytest.mark.anyio
class TestRetries:
    async def test_a_transient_failure_is_retried_with_doubling_backoff(self):
        h = make_harness(rate_limited(), overloaded(), make_response("finally"))
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES)

        assert response.content == "finally"
        assert len(h.acompletion.calls) == 3
        assert h.sleeps == [1.0, 2.0]
        assert response.retries == 2
        assert h.recorder.records[0].request["gateway"]["retries"] == 2

    async def test_backoff_is_capped(self):
        raw = raw_config()
        raw["gateway"].update(max_attempts=6)
        h = make_harness(*[rate_limited() for _ in range(5)], make_response(), config=parse_config(raw))
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES)
        assert h.sleeps == [1.0, 2.0, 4.0, 8.0, 10.0]  # max_delay_seconds = 10

    async def test_jitter_shortens_the_delay_but_never_below_half(self):
        h = make_harness(rate_limited(), rate_limited(), make_response(), jitter=lambda: 0.0)
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES)
        assert h.sleeps == [0.5, 1.0]

    @pytest.mark.parametrize(
        "make_error",
        [
            rate_limited,
            overloaded,
            lambda: lx.ServiceUnavailableError(message="down", llm_provider="anthropic", model="m"),
            lambda: lx.BadGatewayError(message="bad gateway", llm_provider="anthropic", model="m"),
            lambda: lx.APIConnectionError(message="reset", llm_provider="anthropic", model="m"),
            lambda: lx.Timeout(message="slow", model="m", llm_provider="anthropic"),
            # A server-side status LiteLLM did not map to a class of its own: Anthropic's 529 "overloaded".
            lambda: lx.APIError(status_code=529, message="overloaded", llm_provider="anthropic", model="m"),
        ],
    )
    async def test_each_transient_failure_is_retried(self, make_error):
        h = make_harness(make_error(), make_response())
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES)
        assert response.retries == 1

    @pytest.mark.parametrize(
        "make_error",
        [
            unauthorised,
            malformed,
            lambda: RuntimeError("something else entirely"),
            lambda: lx.APIError(status_code=400, message="client error", llm_provider="anthropic", model="m"),
        ],
    )
    async def test_a_failure_that_repetition_cannot_fix_is_not_retried(self, make_error):
        h = make_harness(make_error())
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(LLMCallError):
                await h.client.complete("agent", MESSAGES)
        assert len(h.acompletion.calls) == 1
        assert h.sleeps == []

    async def test_exhausted_retries_raise_with_the_cause_and_leave_an_error_row(self):
        final = rate_limited()
        h = make_harness(rate_limited(), rate_limited(), final)
        with task_scope(TASK_ID, TaskBudget()) as scope:
            with pytest.raises(LLMCallError) as raised:
                await h.client.complete("agent", MESSAGES, attempt=1)

        assert len(h.acompletion.calls) == 3 and h.sleeps == [1.0, 2.0]
        error = raised.value
        assert (error.stage, error.model, error.retries) == ("agent", "anthropic/test-main", 2)
        # The cause is the failure that ended the retries, not the first one.
        assert error.__cause__ is final
        (record,) = h.recorder.records
        assert "RateLimitError" in record.error
        # No usage and no cost: "the provider reported nothing", which is not "free".
        assert (record.input_tokens, record.output_tokens, record.cost_usd) == (None, None, None)
        assert record.response is None
        assert record.attempt == 1
        assert (scope.budget.calls, scope.budget.spent_usd) == (1, 0)

    async def test_a_cancelled_call_is_not_swallowed(self):
        import asyncio

        h = make_harness(asyncio.CancelledError())
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(asyncio.CancelledError):
                await h.client.complete("agent", MESSAGES)
        assert h.recorder.records == []


@pytest.mark.anyio
class TestFallback:
    async def test_a_fallback_serves_the_call_once_the_primarys_retries_are_spent(self):
        h = make_harness(
            rate_limited(), rate_limited(), rate_limited(),
            make_response("from backup", prompt_tokens=1000, completion_tokens=100, model="test-backup"),
            config=make_config(fallback="backup"),
        )
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES)

        assert response.content == "from backup"
        assert response.fallback_used is True
        assert response.model == "openai/test-backup"
        assert response.cost_usd == Decimal("0.0028")  # backup's own price: 1000 x $2/M + 100 x $8/M
        assert [call["model"] for call in h.acompletion.calls] == ["anthropic/test-main"] * 3 + ["openai/test-backup"]

        (record,) = h.recorder.records
        assert (record.model, record.provider) == ("openai/test-backup", "openai")
        assert record.request["gateway"]["fallback_from"] == "anthropic/test-main"
        assert record.request["gateway"]["retries"] == 2

    async def test_each_model_is_sent_its_own_providers_key_and_never_the_others(self):
        """The reason `provider` and the model prefix must agree: a mismatch leaks one vendor's key to another."""
        h = make_harness(
            rate_limited(), rate_limited(), rate_limited(), make_response(model="test-backup"),
            config=make_config(fallback="backup"),
        )
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES)

        keys = [call["api_key"] for call in h.acompletion.calls]
        assert keys == [ANTHROPIC_KEY] * 3 + [OPENAI_KEY]

    async def test_a_non_transient_failure_does_not_fall_back(self):
        """It would fail identically on the next model, and cost a second quota to learn it."""
        h = make_harness(unauthorised(), config=make_config(fallback="backup"))
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(LLMCallError):
                await h.client.complete("agent", MESSAGES)
        assert len(h.acompletion.calls) == 1

    async def test_when_the_fallback_fails_too_the_error_names_the_last_model_tried(self):
        h = make_harness(
            *[rate_limited() for _ in range(6)],
            config=make_config(fallback="backup"),
        )
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(LLMCallError) as raised:
                await h.client.complete("agent", MESSAGES)
        assert raised.value.model == "openai/test-backup"
        assert raised.value.retries == 4  # two backoffs per model
        assert h.recorder.records[0].model == "openai/test-backup"
        assert h.recorder.records[0].request["gateway"]["fallback_from"] == "anthropic/test-main"

    async def test_a_primary_that_succeeds_never_touches_the_fallback(self):
        h = make_harness(make_response(), config=make_config(fallback="backup"))
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES)
        assert response.fallback_used is False
        assert len(h.acompletion.calls) == 1


@pytest.mark.anyio
class TestBudgetEnforcement:
    async def test_the_budget_trips_mid_loop_and_the_crossing_call_is_kept(self):
        """$0.60 a call against a $2 cap: three fit, the fourth crosses, the fifth is never made."""
        h = make_harness(make_priced_response("0.60"), repeat_last=True)
        with task_scope(TASK_ID, TaskBudget(max_usd=2.0)) as scope:
            for _ in range(3):
                await h.client.complete("agent", MESSAGES)
            assert scope.budget.spent_usd == Decimal("1.80")

            with pytest.raises(BudgetExceeded) as crossed:
                await h.client.complete("agent", MESSAGES)
            assert crossed.value.limit is BudgetLimit.USD
            assert crossed.value.spent_usd == Decimal("2.40")
            # The call was paid for, so it is on the record and its response is not thrown away.
            assert crossed.value.response is not None
            assert crossed.value.response.cost_usd == Decimal("0.60")
            assert len(h.recorder.records) == 4

            with pytest.raises(BudgetExceeded) as refused:
                await h.client.complete("agent", MESSAGES)
            assert refused.value.response is None
            # Refused before the provider was ever called, and nothing new was recorded.
            assert len(h.acompletion.calls) == 4
            assert len(h.recorder.records) == 4

    async def test_a_call_that_lands_exactly_on_the_cap_returns_normally(self):
        h = make_harness(make_priced_response("1.05"), repeat_last=True)
        with task_scope(TASK_ID, TaskBudget(max_usd="2.10")) as scope:
            await h.client.complete("agent", MESSAGES)
            await h.client.complete("agent", MESSAGES)  # lands on $2.10 exactly: within the cap
            assert scope.budget.spent_usd == Decimal("2.10")
            with pytest.raises(BudgetExceeded):
                await h.client.complete("agent", MESSAGES)
        assert len(h.acompletion.calls) == 2

    async def test_the_call_count_cap_is_enforced(self):
        h = make_harness(make_priced_response("0.45"), repeat_last=True)
        with task_scope(TASK_ID, TaskBudget(max_calls=2)):
            await h.client.complete("agent", MESSAGES)
            await h.client.complete("agent", MESSAGES)
            with pytest.raises(BudgetExceeded) as raised:
                await h.client.complete("agent", MESSAGES)
        assert raised.value.limit is BudgetLimit.CALLS
        assert len(h.acompletion.calls) == 2

    async def test_the_wall_clock_cap_is_enforced(self):
        now = [0.0]
        h = make_harness(make_response(), repeat_last=True)
        with task_scope(TASK_ID, TaskBudget(max_wall_seconds=60, clock=lambda: now[0])):
            await h.client.complete("agent", MESSAGES)
            now[0] = 61.0
            with pytest.raises(BudgetExceeded) as raised:
                await h.client.complete("agent", MESSAGES)
        assert raised.value.limit is BudgetLimit.WALL_TIME
        assert len(h.acompletion.calls) == 1

    async def test_failed_calls_count_so_an_outage_loop_is_bounded(self):
        h = make_harness(unauthorised(), unauthorised())
        with task_scope(TASK_ID, TaskBudget(max_calls=2)):
            for _ in range(2):
                with pytest.raises(LLMCallError):
                    await h.client.complete("agent", MESSAGES)
            with pytest.raises(BudgetExceeded) as raised:
                await h.client.complete("agent", MESSAGES)
        assert raised.value.limit is BudgetLimit.CALLS

    async def test_two_tasks_do_not_share_a_budget(self):
        h = make_harness(make_priced_response("0.60"), repeat_last=True)
        other = TASK_ID.__class__(int=TASK_ID.int + 1)
        with task_scope(TASK_ID, TaskBudget()) as first:
            await h.client.complete("agent", MESSAGES)
        with task_scope(other, TaskBudget()) as second:
            await h.client.complete("agent", MESSAGES)
        assert first.budget.spent_usd == second.budget.spent_usd == Decimal("0.60")
        assert [record.task_id for record in h.recorder.records] == [TASK_ID, other]


@pytest.mark.anyio
class TestRefusalsBeforeSpend:
    async def test_a_call_outside_a_task_scope_is_refused(self):
        h = make_harness(make_response())
        with pytest.raises(NoTaskScope):
            await h.client.complete("agent", MESSAGES)
        assert h.acompletion.calls == []

    async def test_an_unknown_stage_is_a_config_error_naming_the_real_ones(self):
        h = make_harness(make_response())
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(ConfigError, match="unknown stage 'planner'"):
                await h.client.complete("planner", MESSAGES)
        assert h.acompletion.calls == []

    async def test_a_missing_key_is_refused_before_the_call(self):
        h = make_harness(make_response(), settings=make_settings(anthropic_api_key=None))
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(MissingProviderKey, match="ANTHROPIC_API_KEY"):
                await h.client.complete("agent", MESSAGES)
        assert h.acompletion.calls == []

    async def test_a_fallbacks_missing_key_is_found_up_front_not_mid_outage(self):
        """A fallback that cannot be used is a config bug; discovering it during an outage is the worst time."""
        h = make_harness(
            make_response(),
            config=make_config(fallback="backup"),
            settings=make_settings(openai_api_key=None),
        )
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(MissingProviderKey, match="OPENAI_API_KEY"):
                await h.client.complete("agent", MESSAGES)
        assert h.acompletion.calls == []

    async def test_an_exhausted_budget_is_refused_before_anything_else_is_checked(self):
        h = make_harness()
        with task_scope(TASK_ID, TaskBudget(max_usd=1)) as scope:
            scope.budget.charge(Decimal("1.00"))
            with pytest.raises(BudgetExceeded):
                await h.client.complete("agent", MESSAGES)
        assert h.acompletion.calls == []


@pytest.mark.anyio
class TestOrderOfOperations:
    async def test_a_failed_database_write_still_leaves_the_budget_charged(self):
        """Charge, then record. If the insert fails, the in-memory cap must still know the money is gone."""
        h = make_harness(
            make_priced_response("0.60"), recorder=ListRecorder(fail=RuntimeError("db down"))
        )
        with task_scope(TASK_ID, TaskBudget()) as scope:
            with pytest.raises(RuntimeError, match="db down"):
                await h.client.complete("agent", MESSAGES)
        assert (scope.budget.calls, scope.budget.spent_usd) == (1, Decimal("0.60"))

    async def test_a_recording_failure_is_not_swallowed(self):
        """A call that happened but was not recorded is spend `tasks.cost_usd` can never include."""
        h = make_harness(make_response(), recorder=ListRecorder(fail=RuntimeError("db down")))
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(RuntimeError, match="db down"):
                await h.client.complete("agent", MESSAGES)

    async def test_the_response_is_recorded_before_budget_exceeded_is_raised(self):
        h = make_harness(make_priced_response("0.60"))
        with task_scope(TASK_ID, TaskBudget(max_usd="0.50")):
            with pytest.raises(BudgetExceeded):
                await h.client.complete("agent", MESSAGES)
        assert len(h.recorder.records) == 1
        assert h.recorder.records[0].cost_usd == Decimal("0.60")
