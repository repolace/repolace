"""What the recorder is allowed to write down.

`llm_calls` is the one table where a credential could be stored by accident, so
each test here is a route a secret could take in: the key this process holds, a
key it does not (the one in a repo's `.env.example`), a GitHub token in a tool
result, an error message that quotes the request. And the false-positive side,
which matters as much: redaction that eats ordinary code corrupts the very
record it exists to protect.
"""

import datetime as dt
import json
from decimal import Decimal

import pytest

from repolace_gateway.redaction import REDACTED, Redactor

from gateway_support import ANTHROPIC_KEY, FOREIGN_OPENAI_KEY, OPENAI_KEY


@pytest.fixture
def redactor() -> Redactor:
    return Redactor([ANTHROPIC_KEY, OPENAI_KEY])


class TestExactMatch:
    def test_a_key_this_process_holds_is_replaced_wherever_it_appears(self, redactor):
        text = f"curl -H 'x-api-key: {ANTHROPIC_KEY}' && echo {OPENAI_KEY}"
        out = redactor.text(text)
        assert ANTHROPIC_KEY not in out and OPENAI_KEY not in out
        assert out.count(REDACTED) == 2

    def test_a_key_in_no_known_format_is_still_caught_by_exact_match(self):
        odd = "totally-unconventional-credential-9f8e7d"
        assert odd not in Redactor([odd]).text(f"token={odd}")

    def test_a_secret_too_short_to_be_one_is_ignored_rather_than_replaced_everywhere(self):
        """Replacing 'abc' throughout a stored prompt would corrupt it, not protect it."""
        assert Redactor(["abc"]).text("abc abc abc") == "abc abc abc"

    def test_the_longer_of_two_overlapping_secrets_is_replaced_whole(self):
        short, long = "sk-shortprefix1", "sk-shortprefix1-and-the-tail"
        out = Redactor([short, long]).text(f"key={long}")
        assert "and-the-tail" not in out


class TestShapes:
    def test_a_foreign_provider_key_is_caught_by_pattern(self, redactor):
        """The key the gateway does not hold: sitting in a file the agent just read."""
        assert FOREIGN_OPENAI_KEY not in redactor.text(f"OPENAI_API_KEY={FOREIGN_OPENAI_KEY}")

    def test_an_anthropic_key_is_caught_by_pattern(self):
        key = "sk-ant-api03-" + "Ab1Cd2" * 6
        assert key not in Redactor().text(f"x={key}")

    def test_a_google_key_is_caught_by_pattern(self):
        key = "AIza" + "Sy1" * 11 + "xy"  # AIza + 35 chars
        assert len(key) == 39
        assert key not in Redactor().text(f"key={key}")

    def test_a_github_token_is_caught_through_the_shared_redactor(self):
        token = "ghs_" + "a1B2c3D4" * 4
        assert token not in Redactor().text(f"https://x-access-token:{token}@github.com/a/b")

    def test_a_private_key_block_is_caught(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQ\nabc\n-----END RSA PRIVATE KEY-----"
        out = Redactor().text(f"key:\n{pem}\nend")
        assert "MIIEvQ" not in out


class TestOrdinaryCodeSurvives:
    @pytest.mark.parametrize(
        "code",
        [
            "disk-usage-monitoring-tool is installed",  # contains 'sk-usage-monitoring-tool'
            "task-queue-worker-concurrency",
            "sk-learn",
            "risk-adjusted-return-calculation-2024",
            "def parse_config(path): return json.load(open(path))",
            "https://example.com/a/b?c=d",
        ],
    )
    def test_it_is_left_alone(self, code):
        assert Redactor().text(code) == code


class TestJsonSafety:
    def test_nul_characters_are_stripped(self):
        """Postgres JSONB rejects \\u0000; one binary-looking file in a tool result would fail the insert."""
        assert Redactor().text("a\x00b") == "ab"
        assert Redactor().json({"k\x00": ["v\x00"]}) == {"k": ["v"]}

    def test_nested_structures_are_scrubbed_at_every_level(self, redactor):
        payload = {"messages": [{"role": "tool", "content": [{"text": f"key {ANTHROPIC_KEY}"}]}]}
        assert ANTHROPIC_KEY not in json.dumps(redactor.json(payload))

    def test_dict_keys_are_scrubbed_too(self, redactor):
        assert ANTHROPIC_KEY not in json.dumps(redactor.json({ANTHROPIC_KEY: 1}))

    def test_the_scrubbed_copy_is_new_and_the_original_is_untouched(self, redactor):
        original = {"content": f"key {ANTHROPIC_KEY}"}
        redactor.json(original)
        assert ANTHROPIC_KEY in original["content"]

    def test_values_json_cannot_carry_are_coerced_not_raised(self, redactor):
        out = redactor.json(
            {
                "decimal": Decimal("0.0042"),
                "when": dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC),
                "day": dt.date(2026, 10, 1),
                "raw": b"bytes\xff",
                "tuple": (1, 2),
                "set": {3},
                "odd": object(),
            }
        )
        json.dumps(out)  # must be serialisable
        assert out["decimal"] == "0.0042"
        assert out["when"].startswith("2026-10-01T12:00:00")
        assert out["tuple"] == [1, 2] and out["set"] == [3]

    def test_nan_and_infinity_are_made_serialisable(self):
        """Valid Python, invalid JSON; Postgres refuses both."""
        out = Redactor().json([float("nan"), float("inf"), 1.5])
        assert out == ["nan", "inf", 1.5]

    def test_a_pydantic_model_is_dumped(self, redactor):
        from pydantic import BaseModel

        class Message(BaseModel):
            content: str

        assert redactor.json(Message(content=f"k {ANTHROPIC_KEY}")) == {"content": f"k {REDACTED}"}

    def test_scalars_pass_through_unchanged(self):
        assert Redactor().json([None, True, 3, 2.5, "x"]) == [None, True, 3, 2.5, "x"]
