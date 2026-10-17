"""The optional `plan` and `review` stages: the Phase 2 planner's and reviewer's model routes.

The same rules as `resolve` (`test_gateway_resolve_stage.py`): not required, so a config without
them keeps loading; a missing one follows the `agent` stage at `route()` time, so a per-run
override of `agent` carries over and a roles sweep never mixes the editor's model with another.
"""

import pytest
from gateway_support import raw_config
from repolace_gateway.config import (
    DEFAULT_MODELS_PATH,
    OPTIONAL_STAGE_FALLBACKS,
    REQUIRED_STAGES,
    load_config,
    parse_config,
)
from repolace_gateway.errors import ConfigError

ROLE_STAGES = ("plan", "review")


@pytest.mark.parametrize("stage", ROLE_STAGES)
class TestRoleStages:
    def test_it_is_optional_and_follows_the_agent_stage(self, stage):
        assert stage not in REQUIRED_STAGES and OPTIONAL_STAGE_FALLBACKS[stage] == "agent"

    def test_a_config_without_it_routes_it_like_the_agent_stage(self, stage):
        config = parse_config(raw_config())
        assert stage not in config.stages
        assert config.route(stage) == config.route("agent")

    def test_the_fallback_follows_a_per_run_override_of_the_agent_stage(self, stage):
        config = parse_config(raw_config(), {"agent": "small"})
        assert config.route(stage).primary.litellm_model == "anthropic/test-small"

    def test_an_explicit_stage_wins_over_the_fallback(self, stage):
        raw = raw_config()
        raw["stages"][stage] = {"model": "small", "max_tokens": 128}
        config = parse_config(raw)
        assert config.route(stage).primary.litellm_model == "anthropic/test-small"
        assert config.route("agent").primary.litellm_model == "anthropic/test-main"

    def test_the_shipped_file_has_no_table_for_it_so_an_agent_override_reaches_it(self, stage):
        base = load_config(DEFAULT_MODELS_PATH)
        assert stage not in base.stages
        other = next(key for key in base.models if key != base.route("agent").stage.model)
        config = load_config(DEFAULT_MODELS_PATH, {"agent": other})
        assert config.route(stage) == config.route("agent")

    def test_an_absent_role_stage_can_be_overridden_by_name_and_only_to_a_known_model(self, stage):
        config = parse_config(raw_config(), {stage: "small"})
        assert config.route(stage).primary.litellm_model == "anthropic/test-small"
        assert config.route("agent").primary.litellm_model == "anthropic/test-main"
        with pytest.raises(ConfigError, match="unknown model"):
            parse_config(raw_config(), {stage: "nope"})


def test_resolve_still_follows_the_agent_stage():
    assert OPTIONAL_STAGE_FALLBACKS["resolve"] == "agent"
