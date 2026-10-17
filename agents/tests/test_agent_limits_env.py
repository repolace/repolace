"""`AgentLimits.from_env`: the two loop bounds are overridable per run, and a bad value is an error."""

import pytest
from repolace_agents.contracts import ENV_MAX_ATTEMPTS, ENV_MAX_STEPS, LIMIT_CEILING, AgentLimits


def test_with_nothing_set_the_defaults_are_unchanged():
    assert AgentLimits.from_env({}) == AgentLimits()
    assert AgentLimits().max_steps_per_attempt == 40 and AgentLimits().max_attempts == 3


def test_both_bounds_can_be_overridden_and_the_rest_keep_their_defaults():
    limits = AgentLimits.from_env({ENV_MAX_STEPS: "80", ENV_MAX_ATTEMPTS: "2"})

    assert (limits.max_steps_per_attempt, limits.max_attempts) == (80, 2)
    assert limits.max_context_snippet_lines == AgentLimits().max_context_snippet_lines


def test_an_empty_value_is_treated_as_unset():
    assert AgentLimits.from_env({ENV_MAX_STEPS: ""}) == AgentLimits()


@pytest.mark.parametrize("raw", ["forty", "4.5", "0", "-3", str(LIMIT_CEILING + 1), "1e3"])
def test_a_value_that_is_not_a_sensible_whole_number_is_an_error_not_a_fallback(raw):
    with pytest.raises(ValueError, match=ENV_MAX_STEPS):
        AgentLimits.from_env({ENV_MAX_STEPS: raw})


def test_the_ceiling_itself_is_allowed():
    assert AgentLimits.from_env({ENV_MAX_STEPS: str(LIMIT_CEILING)}).max_steps_per_attempt == LIMIT_CEILING


def test_it_reads_the_process_environment_when_given_none(monkeypatch):
    monkeypatch.setenv(ENV_MAX_STEPS, "70")
    assert AgentLimits.from_env().max_steps_per_attempt == 70
