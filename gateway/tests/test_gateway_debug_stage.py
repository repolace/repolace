"""The `debug` stage: the roles debugger's own stage, so it can run on a different model from the editor."""

from gateway_support import raw_config
from repolace_gateway.config import OPTIONAL_STAGE_FALLBACKS, parse_config


def test_debug_follows_agent_unless_a_run_names_it():
    config = parse_config(raw_config())

    assert OPTIONAL_STAGE_FALLBACKS["debug"] == "agent"
    assert config.route("debug").primary == config.route("agent").primary


def test_a_run_can_put_the_editor_and_the_debugger_on_different_models():
    config = parse_config(raw_config(), {"agent": "small", "debug": "main", "plan": "main", "review": "main"})

    assert config.route("agent").primary.key == "small"
    assert [config.route(stage).primary.key for stage in ("debug", "plan", "review")] == ["main"] * 3
