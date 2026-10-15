"""The optional `resolve` stage: the merge-conflict resolver's model route.

It is not in `REQUIRED_STAGES`, so a config without it must keep loading, and a missing one
must follow the `agent` stage at `route()` time (so a per-run override of `agent` carries over).
"""

import pytest
from gateway_support import raw_config
from repolace_gateway.config import DEFAULT_MODELS_PATH, REQUIRED_STAGES, load_config, parse_config
from repolace_gateway.errors import ConfigError


class TestResolveStage:
    def test_it_is_optional(self):
        assert "resolve" not in REQUIRED_STAGES

    def test_a_config_without_it_routes_it_like_the_agent_stage(self):
        config = parse_config(raw_config())
        assert "resolve" not in config.stages
        assert config.route("resolve") == config.route("agent")

    def test_the_fallback_follows_a_per_run_override_of_the_agent_stage(self):
        config = parse_config(raw_config(), {"agent": "small"})
        assert config.route("resolve").primary.litellm_model == "anthropic/test-small"

    def test_an_explicit_stage_wins_over_the_fallback(self):
        raw = raw_config()
        raw["stages"]["resolve"] = {"model": "small", "max_tokens": 128}
        config = parse_config(raw)
        assert config.route("resolve").primary.litellm_model == "anthropic/test-small"
        assert config.route("agent").primary.litellm_model == "anthropic/test-main"

    def test_the_shipped_file_has_no_resolve_table_so_an_agent_override_reaches_it(self):
        base = load_config(DEFAULT_MODELS_PATH)
        assert "resolve" not in base.stages
        other = next(key for key in base.models if key != base.route("agent").stage.model)
        config = load_config(DEFAULT_MODELS_PATH, {"agent": other})
        assert config.route("agent").primary.litellm_model != base.route("agent").primary.litellm_model
        assert config.route("resolve") == config.route("agent")

    def test_an_absent_optional_stage_can_be_overridden_by_name(self):
        config = parse_config(raw_config(), {"resolve": "small"})
        assert config.route("resolve").primary.litellm_model == "anthropic/test-small"
        assert config.route("agent").primary.litellm_model == "anthropic/test-main"

    def test_overriding_an_optional_stage_to_an_unknown_model_or_an_unknown_stage_still_raises(self):
        with pytest.raises(ConfigError, match="unknown model"):
            parse_config(raw_config(), {"resolve": "nope"})
        with pytest.raises(ConfigError, match="unknown stage"):
            parse_config(raw_config(), {"bogus": "small"})
