"""Cost arithmetic, and the rule that an unpriced call is an error rather than zero.

Three layers, kept apart so a failure says which one broke:

* `Price.cost` -- our own arithmetic for a model LiteLLM's map does not cover.
* The client's use of it, and of LiteLLM's `completion_cost` for the models it
  does cover, driven by a fake provider and an injected cost function.
* A small `TestAgainstRealLiteLLM` that runs the *real* `completion_cost` and
  `get_model_info` on a real `ModelResponse`. The fakes above prove our logic;
  only these prove our call signature and our reading of LiteLLM's usage are
  right against the installed version. They compare against the price map
  itself rather than hard-coded rates, so a price change upstream does not break
  them -- only a change in how LiteLLM *reports* usage would, which is the thing
  worth finding out.

An unpriced call that counted as free would under-report every task that used
it, and the benchmark's headline includes cost.
"""

from decimal import Decimal

import litellm
import pytest

from repolace_gateway.budget import TaskBudget, task_scope
from repolace_gateway.client import TokenUsage, extract_usage
from repolace_gateway.config import Price, parse_config
from repolace_gateway.errors import UnpricedModelError

from gateway_support import (
    MESSAGES,
    TASK_ID,
    FakeClock,
    make_config,
    make_harness,
    make_response,
    raw_config,
)

PRICE = Price(
    input=Decimal("3") / 1_000_000,
    output=Decimal("15") / 1_000_000,
    cache_read=Decimal("0.3") / 1_000_000,
    cache_write=Decimal("3.75") / 1_000_000,
)


def usage_kwargs(**overrides) -> dict:
    return {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, **overrides}


class TestPriceArithmetic:
    def test_each_kind_of_token_is_priced_at_its_own_rate(self):
        """300 plain + 600 cache-read + 100 cache-write input, 200 output.

        0.0009 + 0.00018 + 0.000375 + 0.003, by hand.
        """
        cost = PRICE.cost(input_tokens=1000, cached_input_tokens=600, cache_write_tokens=100, output_tokens=200)
        assert cost == Decimal("0.004455")

    def test_input_tokens_is_the_total_so_cached_tokens_are_not_billed_twice(self):
        """The trap in LiteLLM's convention: prompt_tokens already includes the cached part."""
        all_cached = PRICE.cost(**usage_kwargs(input_tokens=1000, cached_input_tokens=1000))
        assert all_cached == 1000 * PRICE.cache_read
        assert all_cached < PRICE.cost(**usage_kwargs(input_tokens=1000))

    def test_a_call_with_no_cache_activity_needs_no_cache_rates(self):
        price = Price(input=Decimal("1") / 1_000_000, output=Decimal("5") / 1_000_000)
        assert price.cost(**usage_kwargs(input_tokens=1000, output_tokens=100)) == Decimal("0.0015")

    def test_cache_reads_with_no_rate_are_an_error_not_the_input_rate(self):
        """Falling back to the input rate would overstate a cache read tenfold, plausibly."""
        price = Price(input=Decimal("0.000001"), output=Decimal("0.000005"))
        with pytest.raises(UnpricedModelError, match="cache_read"):
            price.cost(**usage_kwargs(input_tokens=100, cached_input_tokens=50))

    def test_cache_writes_with_no_rate_are_an_error(self):
        price = Price(input=Decimal("0.000001"), output=Decimal("0.000005"))
        with pytest.raises(UnpricedModelError, match="cache_write"):
            price.cost(**usage_kwargs(input_tokens=100, cache_write_tokens=50))

    def test_usage_that_does_not_add_up_is_refused_rather_than_clamped(self):
        with pytest.raises(UnpricedModelError, match="inconsistent usage"):
            PRICE.cost(**usage_kwargs(input_tokens=100, cached_input_tokens=80, cache_write_tokens=40))

    def test_an_explicit_zero_price_is_allowed_and_prices_a_call_at_zero(self):
        """Free is a statement someone made in the config; unknown is the error."""
        free = Price(input=Decimal(0), output=Decimal(0))
        assert free.cost(**usage_kwargs(input_tokens=500, output_tokens=500)) == 0


class TestReadingUsage:
    def test_anthropic_shaped_usage_is_split_into_its_parts(self):
        response = make_response(prompt_tokens=1000, completion_tokens=200, cache_read=600, cache_write=100)
        assert extract_usage(response) == TokenUsage(
            input_tokens=1000, cached_input_tokens=600, cache_write_tokens=100, output_tokens=200
        )

    def test_a_response_with_no_cache_activity_reports_zeros(self):
        usage = extract_usage(make_response(prompt_tokens=50, completion_tokens=5))
        assert (usage.cached_input_tokens, usage.cache_write_tokens) == (0, 0)

    def test_a_response_with_no_usage_cannot_be_priced(self):
        class Bare:
            usage = None

        with pytest.raises(UnpricedModelError, match="no usage"):
            extract_usage(Bare())


@pytest.mark.anyio
class TestThroughTheClient:
    async def test_a_model_with_its_own_price_uses_our_arithmetic(self):
        h = make_harness(
            make_response(prompt_tokens=1000, completion_tokens=200, cache_read=600, cache_write=100),
            clock=FakeClock(),
        )
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES)
        assert response.cost_usd == Decimal("0.004455")
        assert h.recorder.records[0].cost_usd == Decimal("0.004455")

    async def test_token_counts_are_recorded_split_not_just_totalled(self):
        h = make_harness(make_response(prompt_tokens=1000, completion_tokens=200, cache_read=600, cache_write=100))
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES)
        record = h.recorder.records[0]
        assert (record.input_tokens, record.cached_input_tokens, record.output_tokens) == (1000, 600, 200)

    async def test_a_model_without_a_price_is_priced_by_litellm(self):
        seen = {}

        def cost_fn(**kwargs):
            seen.update(kwargs)
            return 0.0123

        h = make_harness(
            make_response(),
            config=make_config(agent_model="priced_by_litellm"),
            cost_fn=cost_fn,
            model_info_fn=lambda **_: {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6},
        )
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES)
        assert response.cost_usd == Decimal("0.0123")  # via str(), not the float's binary expansion
        assert seen["model"] == "anthropic/test-priced"
        assert isinstance(seen["completion_response"], litellm.ModelResponse)

    async def test_a_zero_from_litellm_with_real_tokens_is_a_missing_price_not_a_free_model(self):
        h = make_harness(
            make_response(prompt_tokens=100, completion_tokens=20),
            config=make_config(agent_model="priced_by_litellm"),
            cost_fn=lambda **_: 0.0,
            model_info_fn=lambda **_: {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6},
        )
        with task_scope(TASK_ID, TaskBudget()) as scope:
            with pytest.raises(UnpricedModelError, match=r"priced.*\$0"):
                await h.client.complete("agent", MESSAGES)
        assert scope.budget.spent_usd == 0


@pytest.mark.anyio
class TestRefusedBeforeAnyMoneyIsSpent:
    """The cheapest place to find out a model has no price is before the call."""

    async def test_a_model_litellm_does_not_know_is_refused_before_the_call(self):
        def unmapped(**_):
            raise Exception("This model isn't mapped yet")

        h = make_harness(config=make_config(agent_model="priced_by_litellm"), model_info_fn=unmapped)
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(UnpricedModelError, match=r"\[models\.priced_by_litellm\.price\]"):
                await h.client.complete("agent", MESSAGES)
        assert h.acompletion.calls == []
        assert h.recorder.records == []

    async def test_a_map_entry_with_missing_rates_is_refused_before_the_call(self):
        h = make_harness(
            config=make_config(agent_model="priced_by_litellm"),
            model_info_fn=lambda **_: {"input_cost_per_token": None, "output_cost_per_token": None},
        )
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(UnpricedModelError):
                await h.client.complete("agent", MESSAGES)
        assert h.acompletion.calls == []

    async def test_the_check_runs_once_per_model_not_once_per_call(self):
        lookups = []

        def info(**kwargs):
            lookups.append(kwargs)
            return {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6}

        h = make_harness(
            make_response(),
            repeat_last=True,
            config=make_config(agent_model="priced_by_litellm"),
            cost_fn=lambda **_: 0.01,
            model_info_fn=info,
        )
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES)
            await h.client.complete("agent", MESSAGES)
        assert len(lookups) == 1

    async def test_a_model_with_its_own_price_never_consults_litellms_map(self):
        def must_not_be_called(**_):
            raise AssertionError("an overridden price must not depend on LiteLLM's map")

        h = make_harness(make_response(), model_info_fn=must_not_be_called, cost_fn=must_not_be_called)
        with task_scope(TASK_ID, TaskBudget()):
            await h.client.complete("agent", MESSAGES)


@pytest.mark.anyio
class TestPricedTooLate:
    """The second line of defence: the preflight passed but the call still cannot be priced."""

    async def test_the_spend_is_kept_in_the_record_with_the_reason_and_the_task_stops(self):
        def cannot_price(**_):
            raise RuntimeError("model not in cost map")

        h = make_harness(
            make_response(prompt_tokens=100, completion_tokens=20),
            config=make_config(agent_model="priced_by_litellm"),
            cost_fn=cannot_price,
            model_info_fn=lambda **_: {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6},
        )
        with task_scope(TASK_ID, TaskBudget()) as scope:
            with pytest.raises(UnpricedModelError, match="could not price"):
                await h.client.complete("agent", MESSAGES)

        (record,) = h.recorder.records  # the call happened, so it is on the record
        assert record.error.startswith("unpriced:")
        assert record.cost_usd is None  # unknown, not zero
        assert (record.input_tokens, record.output_tokens) == (100, 20)  # usage was readable, so kept
        assert (scope.budget.calls, scope.budget.spent_usd) == (1, 0)

    async def test_a_response_with_no_usage_is_recorded_and_refused(self):
        class Bare:
            usage = None
            choices = []

        h = make_harness(Bare())
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(UnpricedModelError, match="no usage"):
                await h.client.complete("agent", MESSAGES)
        (record,) = h.recorder.records
        assert record.input_tokens is None and record.cost_usd is None


@pytest.mark.anyio
class TestAgainstRealLiteLLM:
    """Real `completion_cost` and `get_model_info`, on the installed LiteLLM and its pinned map."""

    HAIKU = "claude-haiku-4-5-20251001"

    @staticmethod
    def haiku_config():
        raw = raw_config()
        raw["models"]["main"].pop("price")  # no override: LiteLLM must price it
        raw["models"]["main"].update(litellm_model="anthropic/claude-haiku-4-5-20251001")
        return parse_config(raw)

    async def test_the_real_cost_matches_the_price_map_for_a_plain_call(self):
        info = litellm.model_cost[self.HAIKU]
        h = make_harness(
            make_response(model=self.HAIKU, prompt_tokens=1000, completion_tokens=500),
            config=self.haiku_config(),
        )
        with task_scope(TASK_ID, TaskBudget()):
            response = await h.client.complete("agent", MESSAGES)

        expected = Decimal(str(info["input_cost_per_token"])) * 1000 + Decimal(
            str(info["output_cost_per_token"])
        ) * 500
        assert abs(response.cost_usd - expected) < Decimal("1e-9")

    async def test_cache_reads_are_cheaper_than_the_same_tokens_uncached(self):
        """Pins that cached tokens reach LiteLLM's calculator in the shape it prices them from."""
        costs = {}
        for label, cache_read in (("uncached", 0), ("cached", 800)):
            h = make_harness(
                make_response(model=self.HAIKU, prompt_tokens=1000, completion_tokens=100, cache_read=cache_read),
                config=self.haiku_config(),
            )
            with task_scope(TASK_ID, TaskBudget()):
                costs[label] = (await h.client.complete("agent", MESSAGES)).cost_usd
        assert costs["cached"] < costs["uncached"]

    async def test_a_model_litellm_has_never_heard_of_is_refused_before_the_call(self):
        raw = raw_config()
        raw["models"]["main"].pop("price")
        raw["models"]["main"].update(litellm_model="anthropic/claude-no-such-model-xyz")
        h = make_harness(config=parse_config(raw))
        with task_scope(TASK_ID, TaskBudget()):
            with pytest.raises(UnpricedModelError):
                await h.client.complete("agent", MESSAGES)
        assert h.acompletion.calls == []
