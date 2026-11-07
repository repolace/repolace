"""Signed, expiring cookie values (HMAC-SHA256), with the standard library only.

A value is `<base64url(json)>.<base64url(mac)>`. The payload carries a `k` (kind) so a value minted
for one cookie (the OAuth `state`) cannot be replayed as another (the session), and an `exp`.
Nothing secret goes in a payload: it is signed, not encrypted.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def sign(kind: str, data: dict[str, Any], secret: str, *, max_age: int, now: float | None = None) -> str:
    payload = {**data, "k": kind, "exp": int((time.time() if now is None else now) + max_age)}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_mac(body, secret)}"


def unsign(token: str | None, kind: str, secret: str, *, now: float | None = None) -> dict[str, Any] | None:
    """The payload, or None for anything missing, malformed, forged, of another kind or expired."""
    if not token or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    if not hmac.compare_digest(mac.encode("ascii", "ignore"), _mac(body, secret).encode("ascii")):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("k") != kind:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int | float) or exp < (time.time() if now is None else now):
        return None
    return payload
