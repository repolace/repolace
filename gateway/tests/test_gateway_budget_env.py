from decimal import Decimal

import pytest
from repolace_gateway.budget import DEFAULT_MAX_USD, MAX_USD_ENV, default_max_usd


def test_unset_or_empty_gives_the_code_default(monkeypatch):
    monkeypatch.delenv(MAX_USD_ENV, raising=False)
    assert default_max_usd() == DEFAULT_MAX_USD
    monkeypatch.setenv(MAX_USD_ENV, "")
    assert default_max_usd() == DEFAULT_MAX_USD


def test_the_env_var_replaces_the_cap(monkeypatch):
    monkeypatch.setenv(MAX_USD_ENV, "3.60")
    assert default_max_usd() == Decimal("3.60")


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "nan", "inf"])
def test_a_bad_value_raises_rather_than_silently_using_the_default(monkeypatch, bad):
    monkeypatch.setenv(MAX_USD_ENV, bad)
    with pytest.raises(ValueError, match=MAX_USD_ENV):
        default_max_usd()
