"""The test-database suffix cannot name the development database.

`db_session` TRUNCATEs every table before every test, so a test database that is
secretly the development one is data loss with a green build. The suffix comes
from an environment variable, which can be set-but-empty (`export X=`, CI's
`${{ vars.UNSET }}`), and `os.environ.get(name, default)` returns "" for that.

No database is needed: these test the pure functions in the root `conftest.py`,
loaded by path because a conftest is not importable by name.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT_CONFTEST = Path(__file__).resolve().parents[2] / "conftest.py"


@pytest.fixture(scope="module")
def conftest_module():
    spec = importlib.util.spec_from_file_location("repolace_root_conftest_under_test", ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALID = ["_test", "_test_w0b", "_a", "_" + "a" * 32, "_0", "_a_b_c", "__"]

INVALID = {
    "empty": "",
    "no leading underscore": "test",
    "no leading underscore, digits": "1_test",
    "bare underscore": "_",
    "uppercase": "_Test",
    "all uppercase": "_TEST",
    "double quote": '_t"x',
    "semicolon": "_t;drop",
    "question mark": "_t?a",
    "hash": "_t#a",
    "slash": "_t/a",
    "backslash": "_t\\a",
    "space": "_t a",
    "hyphen": "_t-a",
    "dot": "_t.a",
    "percent": "_t%41",
    "non-ascii": "_tést",
    "33 characters after the underscore": "_" + "a" * 33,
    "very long": "_" + "a" * 200,
    "trailing newline": "_test\n",
    "leading newline": "\n_test",
    "whitespace only": "   ",
}


class TestSuffixValidator:
    @pytest.mark.parametrize("raw", VALID)
    def test_it_accepts_a_valid_suffix_unchanged(self, conftest_module, raw):
        assert conftest_module._validated_suffix(raw) == raw

    @pytest.mark.parametrize("raw", INVALID.values(), ids=INVALID.keys())
    def test_it_refuses_everything_else(self, conftest_module, raw):
        with pytest.raises(pytest.UsageError):
            conftest_module._validated_suffix(raw)

    def test_the_error_names_the_variable_and_the_rule(self, conftest_module):
        with pytest.raises(pytest.UsageError) as caught:
            conftest_module._validated_suffix("")
        message = str(caught.value)
        assert "REPOLACE_TEST_DB_SUFFIX" in message
        assert "[a-z0-9_]" in message
        # The empty case is the dangerous one and the message has to say why.
        assert "development database" in message

    def test_an_empty_value_is_refused_not_defaulted(self, conftest_module):
        """The regression: `environ.get(name, '_test')` returns '' for a set-but-empty variable."""
        with pytest.raises(pytest.UsageError):
            conftest_module._validated_suffix("")


class TestAnEmptyVariableIsRefusedAtImport:
    """The guard has to be *applied*, not merely defined."""

    def _load(self, monkeypatch, value: str | None):
        if value is None:
            monkeypatch.delenv("REPOLACE_TEST_DB_SUFFIX", raising=False)
        else:
            monkeypatch.setenv("REPOLACE_TEST_DB_SUFFIX", value)
        spec = importlib.util.spec_from_file_location("repolace_root_conftest_import_check", ROOT_CONFTEST)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_set_but_empty_variable_fails_the_import(self, monkeypatch):
        with pytest.raises(pytest.UsageError, match="REPOLACE_TEST_DB_SUFFIX"):
            self._load(monkeypatch, "")

    def test_a_hostile_variable_fails_the_import(self, monkeypatch):
        with pytest.raises(pytest.UsageError):
            self._load(monkeypatch, '_t"; DROP DATABASE repolace; --')

    def test_an_unset_variable_still_defaults_to_the_old_constant(self, monkeypatch):
        assert self._load(monkeypatch, None).TEST_DB_SUFFIX == "_test"

    def test_a_valid_variable_is_used(self, monkeypatch):
        assert self._load(monkeypatch, "_test_stream_c").TEST_DB_SUFFIX == "_test_stream_c"


class TestTestDatabaseName:
    def test_it_appends_the_suffix(self, conftest_module):
        assert conftest_module._test_database_name("repolace", "_test_w0b") == "repolace_test_w0b"

    def test_it_refuses_a_name_equal_to_the_development_database(self, conftest_module):
        """Unreachable through the validator, which is why it is tested directly: it is the backstop."""
        with pytest.raises(pytest.UsageError, match="development database"):
            conftest_module._test_database_name("repolace", "")

    def test_it_refuses_a_url_with_no_database(self, conftest_module):
        for missing in (None, ""):
            with pytest.raises(pytest.UsageError):
                conftest_module._test_database_name(missing, "_test")

    def test_it_leaves_room_for_the_migrations_scratch_database(self, conftest_module):
        """`test_migrations.py` appends '_migrations' (11 bytes); Postgres truncates at 63."""
        dev = "repolace"
        fits = "_" + "a" * (63 - len(dev) - len("_migrations") - 1)
        assert len(f"{dev}{fits}_migrations".encode()) == 63
        assert conftest_module._test_database_name(dev, fits) == f"{dev}{fits}"
        with pytest.raises(pytest.UsageError, match="63"):
            conftest_module._test_database_name(dev, fits + "a")

    def test_the_budget_is_in_bytes_not_characters(self, conftest_module):
        """A multi-byte development database name eats the budget faster than len() suggests."""
        dev = "é" * 25  # 25 characters, 50 bytes
        with pytest.raises(pytest.UsageError):
            conftest_module._test_database_name(dev, "_test")

    def test_two_suffixes_that_would_truncate_together_are_both_refused(self, conftest_module):
        """The silent-sharing case the byte limit exists to prevent."""
        dev = "repolace"
        base = "_" + "a" * 32
        for tail in ("x", "y"):
            long_dev = dev + "_" * 20 + tail
            with pytest.raises(pytest.UsageError):
                conftest_module._test_database_name(long_dev, base)
