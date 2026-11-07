"""The cookie signer: forged, tampered, expired and wrong-kind values are all just None."""

import pytest
from repolace_api.sessions import sign, unsign

SECRET = "k" * 40


def test_round_trip():
    token = sign("session", {"uid": "abc"}, SECRET, max_age=60)
    assert unsign(token, "session", SECRET)["uid"] == "abc"


@pytest.mark.parametrize("token", [None, "", "nodot", "a.b.c", "!!!.???"])
def test_garbage_is_none(token):
    assert unsign(token, "session", SECRET) is None


def test_wrong_secret_wrong_kind_and_tampering_are_none():
    token = sign("session", {"uid": "abc"}, SECRET, max_age=60)
    assert unsign(token, "session", "z" * 40) is None
    assert unsign(token, "oauth-state", SECRET) is None
    body, mac = token.split(".")
    forged = sign("session", {"uid": "evil"}, "z" * 40, max_age=60).split(".")[0]
    assert unsign(f"{forged}.{mac}", "session", SECRET) is None
    assert unsign(f"{body}x.{mac}", "session", SECRET) is None


def test_expiry():
    token = sign("session", {}, SECRET, max_age=60, now=1000)
    assert unsign(token, "session", SECRET, now=1059) is not None
    assert unsign(token, "session", SECRET, now=1061) is None
