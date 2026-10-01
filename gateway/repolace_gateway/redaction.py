"""Scrubbing secrets out of what the recorder stores.

`llm_calls` keeps every request and response verbatim, so it is the one table in
the system where a credential could be written down by accident: through a
traceback echoed in an error, through repo source the agent read that happens to
contain a key, through a provider error that quotes the request. Two defences,
because each misses what the other catches:

* **Exact match** on the keys this process actually holds. Format-independent,
  so it works for a provider whose key shape nobody wrote a pattern for.
* **Patterns** for credential *shapes*: GitHub tokens, JWTs and private keys
  (reusing `git.repo.redact`, so there is one list to keep current) plus the
  providers' own key formats. Catches a key that is not ours -- the one in the
  repo's `.env.example` the agent just read.

Also strips NUL characters, which has nothing to do with secrets and everything
to do with this being the only place that sees arbitrary file bytes on the way
into JSONB: Postgres rejects `\\u0000` in a JSON string, and one binary-ish file
in a tool result would otherwise fail the insert and, with it, the task.
"""

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from repolace_shared.git.repo import redact as redact_github_shapes

REDACTED = "<redacted-secret>"

#: A lookbehind and a digit requirement keep these from eating ordinary code:
#: `disk-usage-monitoring-tool` contains `sk-usage-monitoring-tool`, and most
#: hyphenated identifiers have no digit in them.
_PROVIDER_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
)

#: Same floor as `GatewaySettings.secret_values`; see the note there.
_MIN_SECRET_LENGTH = 8


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        # Longest first, so a key that happens to be a prefix of another is not
        # replaced piecemeal and the tail left behind.
        self._secrets = tuple(
            sorted({s for s in secrets if len(s) >= _MIN_SECRET_LENGTH}, key=len, reverse=True)
        )

    def text(self, value: str) -> str:
        value = value.replace("\x00", "")
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        value = redact_github_shapes(value)
        for pattern in _PROVIDER_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value

    def json(self, value: Any) -> Any:
        """A JSON-safe copy of `value` with every string scrubbed.

        Coerces rather than raises on a type JSON cannot carry, because a
        recorder that throws on an odd value loses the very record it exists
        to keep. Pydantic models (LiteLLM's responses) are dumped; anything else
        unrecognised is stored as its `repr`, which is lossy but never absent.
        """
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            # NaN and Infinity are valid Python and invalid JSON; Postgres refuses them.
            return value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, bytes):
            return self.text(value.decode("utf-8", errors="replace"))
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (dt.datetime, dt.date)):
            return value.isoformat()
        if isinstance(value, Mapping):
            return {self.text(str(key)): self.json(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.json(item) for item in value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return self.json(dump())
        return self.text(repr(value))
