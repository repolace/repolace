"""`models.toml`, and the settings that hold the keys.

The config is validated hard at load, and every table rejects unknown keys. That
is the point of most of these tests: a setting that fails *open* -- a misspelt
`supports_prompt_cacheing` silently ignored, a model whose provider key is sent
to the wrong vendor -- is exactly what this repo has been bitten by before, and
the cheapest place to catch one is before the first call.
"""

import copy
from decimal import Decimal

import pytest
from gateway_support import (
    ANTHROPIC_KEY,
    OPENAI_KEY,
    make_config,
    make_settings,
    raw_config,
)
from repolace_gateway.config import (
    DEFAULT_MODELS_PATH,
    KNOWN_PROVIDERS,
    REQUIRED_STAGES,
    GatewaySettings,
    load_config,
    parse_config,
)
from repolace_gateway.errors import ConfigError, MissingProviderKey


class TestTheShippedFile:
    """The real `gateway/models.toml`. If it stops parsing, nothing in the pipeline starts."""

    def test_it_loads_and_defines_both_stages(self):
        config = load_config(DEFAULT_MODELS_PATH)
        assert set(REQUIRED_STAGES) <= set(config.stages)

    def test_the_cheap_stage_is_haiku_4_5_and_the_agent_stage_is_a_sonnet(self):
        config = load_config(DEFAULT_MODELS_PATH)
        assert config.route("cheap").primary.litellm_model == "anthropic/claude-haiku-4-5-20251001"
        assert "sonnet" in config.route("agent").primary.litellm_model

    def test_every_model_routes_to_the_provider_whose_key_it_will_be_sent(self):
        for model in load_config(DEFAULT_MODELS_PATH).models.values():
            assert model.litellm_model.startswith(f"{model.provider}/")
            assert model.provider in KNOWN_PROVIDERS

    @pytest.mark.parametrize(
        ("key", "provider", "litellm_model"),
        [
            ("gemini-3-8-flash", "gemini", "gemini/gemini-3.8-flash"),
            ("minimax-m2-5", "minimax", "minimax/MiniMax-M2.5"),
        ],
    )
    def test_the_comparison_models_are_declared_and_priced_by_litellm(self, key, provider, litellm_model):
        """Selecting one with `GATEWAY_STAGE_MODELS` must not fail at the pre-spend price check."""
        import litellm

        model = load_config(DEFAULT_MODELS_PATH).models[key]
        assert (model.provider, model.litellm_model) == (provider, litellm_model)
        if model.price is None:  # LiteLLM's pinned map must then supply it
            info = litellm.get_model_info(model=model.litellm_model)
            assert info["input_cost_per_token"] and info["output_cost_per_token"]
        else:
            assert model.price.input > 0 and model.price.output > 0

    def test_gemini_is_priced_explicitly_because_the_pinned_map_lacks_it(self):
        """The 2026-10-17 Standard paid rates; they change on 2027-01-01 (see models.toml)."""
        price = load_config(DEFAULT_MODELS_PATH).models["gemini-3-8-flash"].price
        assert price is not None
        assert (price.input, price.output) == (Decimal("0.00000075"), Decimal("0.00000375"))

    def test_the_comparison_models_can_be_routed_to_the_agent_stage(self):
        for key in ("gemini-3-8-flash", "minimax-m2-5"):
            config = load_config(DEFAULT_MODELS_PATH, stage_models={"agent": key})
            assert config.route("agent").primary.key == key

    def test_the_agent_stage_caches_and_the_cheap_stage_does_not(self):
        config = load_config(DEFAULT_MODELS_PATH)
        assert config.route("agent").stage.cache is True
        assert config.route("cheap").stage.cache is False


class TestRejectingWhatFailsOpen:
    @staticmethod
    def broken(mutate) -> dict:
        raw = raw_config()
        mutate(raw)
        return raw

    def test_a_misspelt_model_key_is_rejected_not_ignored(self):
        raw = self.broken(lambda r: r["models"]["main"].update(supports_prompt_cacheing=True))
        with pytest.raises(ConfigError, match="unknown key.*supports_prompt_cacheing"):
            parse_config(raw)

    @pytest.mark.parametrize(
        ("table", "extra"),
        [("gateway", {"max_attempt": 3}), ("stages", None), ("root", {"modles": {}})],
    )
    def test_unknown_keys_are_rejected_at_every_level(self, table, extra):
        raw = raw_config()
        if table == "gateway":
            raw["gateway"].update(extra)
        elif table == "stages":
            raw["stages"]["agent"]["modle"] = "main"
        else:
            raw.update(extra)
        with pytest.raises(ConfigError, match="unknown key"):
            parse_config(raw)

    def test_a_misspelt_price_key_is_rejected(self):
        raw = self.broken(lambda r: r["models"]["main"]["price"].update(cache_reed=0.3))
        with pytest.raises(ConfigError, match="unknown key.*cache_reed"):
            parse_config(raw)

    def test_a_provider_key_is_never_sent_to_a_different_vendor(self):
        """provider='anthropic' with an openai/ model would carry Anthropic's key to OpenAI."""
        raw = self.broken(lambda r: r["models"]["main"].update(litellm_model="openai/gpt-x"))
        with pytest.raises(ConfigError, match="different vendor"):
            parse_config(raw)

    def test_an_unknown_provider_is_rejected_at_load_not_on_first_call(self):
        raw = self.broken(
            lambda r: r["models"]["main"].update(provider="mistral", litellm_model="mistral/large")
        )
        with pytest.raises(ConfigError, match="unknown provider 'mistral'"):
            parse_config(raw)

    def test_a_bedrock_model_is_accepted_when_its_route_names_bedrock(self):
        raw = self.broken(
            lambda r: r["models"]["main"].update(provider="bedrock", litellm_model="bedrock/global.anthropic.claude-sonnet-5-5")
        )
        assert parse_config(raw).models["main"].provider == "bedrock"

    def test_a_bedrock_model_routed_through_another_vendor_is_rejected(self):
        """Its AWS profile would be sent to someone else's API."""
        raw = self.broken(
            lambda r: r["models"]["main"].update(provider="bedrock", litellm_model="anthropic/claude-sonnet-5-5")
        )
        with pytest.raises(ConfigError, match="different vendor"):
            parse_config(raw)

    def test_a_caching_model_priced_without_cache_rates_is_rejected(self):
        """It would be mispriced on every call that hit the cache -- most of them, in a loop."""
        raw = self.broken(lambda r: r["models"]["main"]["price"].pop("cache_write"))
        with pytest.raises(ConfigError, match="cache_read and cache_write"):
            parse_config(raw)

    def test_a_stage_naming_a_missing_model_is_rejected(self):
        raw = self.broken(lambda r: r["stages"]["agent"].update(model="nope"))
        with pytest.raises(ConfigError, match="unknown model 'nope'"):
            parse_config(raw)

    def test_a_fallback_naming_a_missing_model_is_rejected(self):
        raw = self.broken(lambda r: r["stages"]["agent"].update(fallback="nope"))
        with pytest.raises(ConfigError, match="fallback.*unknown model 'nope'"):
            parse_config(raw)

    @pytest.mark.parametrize("stage", REQUIRED_STAGES)
    def test_a_missing_required_stage_is_rejected(self, stage):
        raw = self.broken(lambda r: r["stages"].pop(stage))
        with pytest.raises(ConfigError, match="missing required stage"):
            parse_config(raw)

    def test_a_negative_price_is_rejected(self):
        raw = self.broken(lambda r: r["models"]["small"]["price"].update(input=-1))
        with pytest.raises(ConfigError, match="cannot be negative"):
            parse_config(raw)

    def test_a_boolean_where_a_number_belongs_is_rejected(self):
        """`bool` is an `int` subclass; `max_tokens = true` is a typo, not 1."""
        raw = self.broken(lambda r: r["stages"]["agent"].update(max_tokens=True))
        with pytest.raises(ConfigError, match="max_tokens"):
            parse_config(raw)

    def test_a_non_positive_max_tokens_is_rejected(self):
        raw = self.broken(lambda r: r["stages"]["agent"].update(max_tokens=0))
        with pytest.raises(ConfigError, match="must be positive"):
            parse_config(raw)

    def test_zero_retry_attempts_is_rejected(self):
        raw = self.broken(lambda r: r["gateway"].update(max_attempts=0))
        with pytest.raises(ConfigError, match="max_attempts"):
            parse_config(raw)

    def test_a_missing_file_and_bad_toml_are_config_errors_not_tracebacks(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "absent.toml")
        bad = tmp_path / "bad.toml"
        bad.write_text("[stages\n")
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_config(bad)


class TestParsing:
    def test_prices_become_exact_decimals_per_token(self):
        price = make_config().models["main"].price
        assert price.input == Decimal("0.000003")
        assert price.output == Decimal("0.000015")
        assert price.cache_read == Decimal("0.0000003")
        assert price.cache_write == Decimal("0.00000375")

    def test_a_price_is_absent_when_litellm_is_meant_to_supply_it(self):
        assert make_config().models["priced_by_litellm"].price is None

    def test_an_empty_string_fallback_means_none(self):
        raw = raw_config()
        raw["stages"]["agent"]["fallback"] = ""
        assert parse_config(raw).route("agent").fallback is None

    def test_routing_resolves_stage_to_primary_and_fallback(self):
        route = make_config(fallback="backup").route("agent")
        assert route.primary.key == "main"
        assert route.fallback is not None and route.fallback.key == "backup"
        assert route.stage.max_tokens == 4096

    def test_an_unknown_stage_names_the_configured_ones(self):
        with pytest.raises(ConfigError, match="unknown stage 'planner'.*agent"):
            make_config().route("planner")

    def test_the_retry_policy_is_read_from_the_gateway_table(self):
        retry = make_config().retry
        assert (retry.max_attempts, retry.base_delay_seconds, retry.max_delay_seconds) == (3, 1.0, 10.0)


class TestStageOverrides:
    def test_a_stage_can_be_pointed_at_another_model(self):
        config = make_config().with_stage_models({"agent": "backup"})
        assert config.route("agent").primary.key == "backup"

    def test_the_original_config_is_unchanged(self):
        original = make_config()
        original.with_stage_models({"agent": "backup"})
        assert original.route("agent").primary.key == "main"

    def test_overrides_are_applied_when_loading(self):
        assert parse_config(raw_config(), {"cheap": "backup"}).route("cheap").primary.key == "backup"

    def test_an_override_naming_an_unknown_model_is_rejected(self):
        with pytest.raises(ConfigError, match="unknown model 'nope'"):
            make_config().with_stage_models({"agent": "nope"})

    def test_an_override_naming_an_unknown_stage_is_rejected(self):
        with pytest.raises(ConfigError, match="unknown stage 'planner'"):
            make_config().with_stage_models({"planner": "main"})

    def test_parsing_does_not_mutate_its_input(self):
        raw = raw_config()
        before = copy.deepcopy(raw)
        parse_config(raw, {"agent": "backup"})
        assert raw == before


class TestSettings:
    def test_a_configured_key_is_returned(self):
        settings = make_settings()
        assert settings.api_key_for("anthropic") == ANTHROPIC_KEY
        assert settings.api_key_for("openai") == OPENAI_KEY

    def test_a_missing_key_names_the_environment_variable_to_set(self):
        settings = make_settings(gemini_api_key=None)
        with pytest.raises(MissingProviderKey, match="GEMINI_API_KEY"):
            settings.api_key_for("gemini")

    def test_minimax_has_its_own_key_and_the_error_names_it(self):
        assert make_settings(minimax_api_key="mm-key").api_key_for("minimax") == "mm-key"
        with pytest.raises(MissingProviderKey, match="MINIMAX_API_KEY"):
            make_settings(minimax_api_key=None).api_key_for("minimax")

    def test_an_empty_key_counts_as_missing(self):
        with pytest.raises(MissingProviderKey):
            make_settings(openai_api_key="").api_key_for("openai")

    def test_a_keyed_provider_is_called_with_its_key_and_nothing_else(self):
        assert make_settings().call_kwargs("anthropic") == {"api_key": ANTHROPIC_KEY}

    def test_bedrock_is_called_with_the_profile_and_region_and_no_key(self):
        settings = make_settings(aws_profile_name="repolace-bedrock", aws_region_name="ap-south-1")

        assert settings.call_kwargs("bedrock") == {
            "aws_profile_name": "repolace-bedrock",
            "aws_region_name": "ap-south-1",
        }

    @pytest.mark.parametrize(
        ("overrides", "named"),
        [
            ({"aws_region_name": "ap-south-1"}, "GATEWAY_AWS_PROFILE"),
            ({"aws_profile_name": "p"}, "GATEWAY_AWS_REGION"),
            ({}, "GATEWAY_AWS_PROFILE and GATEWAY_AWS_REGION"),
            ({"aws_profile_name": "", "aws_region_name": "ap-south-1"}, "GATEWAY_AWS_PROFILE"),
        ],
    )
    def test_missing_aws_settings_name_the_variables_to_set(self, overrides, named):
        with pytest.raises(MissingProviderKey, match=named):
            make_settings(**overrides).call_kwargs("bedrock")

    def test_the_aws_settings_come_from_the_gateway_prefixed_environment(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_AWS_PROFILE", "from-env")
        monkeypatch.setenv("GATEWAY_AWS_REGION", "ap-south-1")

        settings = GatewaySettings(_env_file=None)

        assert (settings.aws_profile_name, settings.aws_region_name) == ("from-env", "ap-south-1")

    def test_an_unknown_provider_is_a_missing_key_not_an_attribute_error(self):
        with pytest.raises(MissingProviderKey):
            make_settings().api_key_for("nonesuch")

    def test_secret_values_lists_every_configured_key_and_skips_unset_ones(self):
        assert set(make_settings().secret_values()) == {ANTHROPIC_KEY, OPENAI_KEY}

    def test_a_key_too_short_to_redact_safely_is_not_listed(self):
        assert make_settings(anthropic_api_key="short", openai_api_key=None).secret_values() == ()

    def test_keys_are_not_shown_when_the_settings_are_printed(self):
        """SecretStr: a stray `log.info(settings=settings)` must not write the key down."""
        assert ANTHROPIC_KEY not in repr(make_settings())

    def test_the_stage_override_comes_from_the_gateway_prefixed_environment_variable(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_STAGE_MODELS", '{"agent": "backup"}')
        assert GatewaySettings(_env_file=None).stage_models == {"agent": "backup"}

    def test_the_models_path_comes_from_the_gateway_prefixed_environment_variable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GATEWAY_MODELS_PATH", str(tmp_path / "custom.toml"))
        assert GatewaySettings(_env_file=None).models_path == tmp_path / "custom.toml"

    def test_keys_are_read_under_their_standard_names(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
        assert GatewaySettings(_env_file=None).api_key_for("anthropic") == ANTHROPIC_KEY
