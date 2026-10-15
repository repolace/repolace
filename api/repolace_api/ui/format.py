"""Small display helpers. All return plain strings; the templates escape them."""

from datetime import UTC, datetime
from decimal import Decimal

from repolace_shared.db.models import AGENT_STOP_REASONS

#: One sentence per `agent_stop_reason`. Keyed on the shared constant by a test,
#: so a ninth reason added to the schema fails there instead of rendering blank.
STOP_REASON_GLOSS: dict[str, str] = {
    "submitted": "The agent declared the fix done.",
    "step_cap": "The agent used all of its steps in one attempt without submitting.",
    "budget_usd": "The per-task cost cap was reached.",
    "budget_calls": "The per-task model-call limit was reached.",
    "budget_wall": "The per-task wall-clock limit was reached.",
    "llm_error": "A model call failed and the run was cut short.",
    "no_change": "The agent finished without changing any file.",
    "max_attempts": "Every attempt was used and the last one still had regressions or collection errors.",
}
assert set(STOP_REASON_GLOSS) == set(AGENT_STOP_REASONS)


def short_id(value: object) -> str:
    """First 8 hex characters of a UUID. A display handle, not an identifier."""
    return str(value).replace("-", "")[:8]


def money(value: Decimal | float | None) -> str:
    if value is None:
        return "\N{EM DASH}"
    return f"${Decimal(str(value)):.2f}"


def money4(value: Decimal | float | None) -> str:
    if value is None:
        return "\N{EM DASH}"
    return f"${Decimal(str(value)):.4f}"


def duration(seconds: float | None) -> str:
    """`4.2 s`, `45 s`, `1 m 48 s`, `1 h 02 m`."""
    if seconds is None or seconds < 0:
        return "\N{EM DASH}"
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{round(seconds)} s"
    whole = round(seconds)
    if whole < 3600:
        return f"{whole // 60} m {whole % 60:02d} s"
    return f"{whole // 3600} h {(whole % 3600) // 60:02d} m"


def clock(seconds: float | None) -> str:
    """`m:ss` for a live elapsed time."""
    if seconds is None or seconds < 0:
        return "0:00"
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def relative(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "not started"
    now = now or datetime.now(UTC)
    delta = (now - when).total_seconds()
    if delta < 0:
        return "in the future"
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{max(1, round(delta / 60))} min ago"
    if delta < 86400:
        return f"{round(delta / 3600)} h ago"
    if delta < 86400 * 30:
        return f"{round(delta / 86400)} d ago"
    return when.astimezone(UTC).strftime("%Y-%m-%d")


def absolute(when: datetime | None) -> str:
    if when is None:
        return "\N{EM DASH}"
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def first_line(text: str | None, limit: int = 140) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"
