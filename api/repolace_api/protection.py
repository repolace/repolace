"""Arcjet request protection for the API, as per-route dependencies.

Off unless `ARCJET_KEY` is set, so local runs and the tests (which build a bare `FastAPI()`) behave
as before. A dependency, not middleware: Arcjet's rules want one `protect()` per request, made in
the route that knows which policy it needs. Named `protection`, not `arcjet`, so nothing in this
package can shadow the SDK.

The API has no identity until Phase 4, so every limit is keyed by client IP. Behind a tunnel or
proxy that is the proxy's address unless `ARCJET_TRUSTED_PROXIES` lists it (and uvicorn is told to
trust the same hops); never copy a forwarding header into the client IP by hand.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import structlog
from arcjet import Arcjet, Mode, arcjet, detect_prompt_injection, set_rate_limit_headers, shield, sliding_window
from fastapi import Depends, HTTPException, Request, Response
from pydantic_settings import BaseSettings, SettingsConfigDict
from repolace_shared.github.schemas import Issue

log = structlog.get_logger()

_REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

#: Policy names a route asks for. Each is the base `shield` plus the rule it adds, so one request
#: is one `protect()` call and spends one budget.
SHIELD = "shield"  # pages and cheap reads
GITHUB_READ = "github_read"  # fans out to the GitHub API
ENQUEUE = "enqueue"  # queues a task, which spends LLM money
WEBHOOK = "webhook"  # GitHub's deliveries; authenticated by HMAC, so no bot rule

#: Longest issue text sent for the optional scan, in characters.
SCAN_MAX_CHARS = 20_000


class ArcjetSettings(BaseSettings):
    """Only what the protection needs, so building the app never requires the database or GitHub."""

    model_config = SettingsConfigDict(env_file=_REPO_ROOT_ENV_FILE, extra="ignore")

    #: The site key from the Arcjet console. Unset means no protection at all.
    arcjet_key: str | None = None
    #: `DRY_RUN` records decisions and blocks nothing; start there and read the console.
    arcjet_mode: Literal["LIVE", "DRY_RUN"] = "LIVE"
    #: Comma-separated proxy IPs/CIDRs whose forwarding headers may be believed.
    arcjet_trusted_proxies: str = ""
    #: Send issue text to Arcjet's prompt-injection detector (observe only). Off by default: the
    #: detector is remote, so this sends the issue, possibly from a private repository, to a third party.
    arcjet_scan_issues: bool = False


@lru_cache
def get_arcjet_settings() -> ArcjetSettings:
    return ArcjetSettings()


@dataclass(frozen=True)
class Protection:
    """One client per policy. The clones share the base client's decision cache."""

    policies: dict[str, Arcjet]
    #: Observe-only prompt-injection client; None unless `ARCJET_SCAN_ISSUES` is set.
    scan: Arcjet | None


@lru_cache
def get_protection() -> Protection | None:
    settings = get_arcjet_settings()
    if not settings.arcjet_key:
        return None
    mode = Mode(settings.arcjet_mode)
    proxies = [p.strip() for p in settings.arcjet_trusted_proxies.split(",") if p.strip()]
    base = arcjet(key=settings.arcjet_key, rules=[shield(mode=mode)], proxies=proxies)
    policies = {
        SHIELD: base,
        GITHUB_READ: base.with_rule(sliding_window(mode=mode, interval=60, max=60)),
        ENQUEUE: base.with_rule(sliding_window(mode=mode, interval=600, max=5)),
        WEBHOOK: base.with_rule(sliding_window(mode=mode, interval=60, max=600)),
    }
    scan = None
    if settings.arcjet_scan_issues:
        # Always DRY_RUN: a real issue about prompt injection must not be refused by this layer.
        scan = arcjet(key=settings.arcjet_key, rules=[detect_prompt_injection(mode=Mode.DRY_RUN)], proxies=proxies)
    return Protection(policies=policies, scan=scan)


def protect(policy: str, *, safe_methods_only: bool = False) -> Callable[..., Awaitable[None]]:
    """A dependency that runs `protect()` for `policy`, once per request.

    `safe_methods_only` is for a router-level dependency: a state-changing route in the same router
    carries its own, stricter policy, and must not be counted twice.

    Denied: 429 (with the IETF rate-limit headers) for a rate limit, 403 otherwise. An Arcjet error
    or outage is logged and the request goes through: a protection layer must not take the API down.
    """

    async def dependency(
        request: Request,
        response: Response,
        protection: Protection | None = Depends(get_protection),
    ) -> None:
        if protection is None or (safe_methods_only and request.method not in ("GET", "HEAD")):
            return
        try:
            decision = await protection.policies[policy].protect(request)
        except Exception:
            log.warning("arcjet.protect_failed", policy=policy, exc_info=True)
            return
        if decision.is_error():
            log.warning("arcjet.decision_error", policy=policy, decision_id=decision.id)
            return
        _rate_limit_headers(response, decision)
        if decision.is_denied():
            rate_limited = decision.reason_v2.type == "RATE_LIMIT"
            log.info("arcjet.denied", policy=policy, reason=decision.reason_v2.type, decision_id=decision.id)
            headers: dict[str, str] = {}
            _rate_limit_headers(headers, decision)
            raise HTTPException(
                status_code=429 if rate_limited else 403,
                detail="Too many requests" if rate_limited else "Forbidden",
                headers=headers,
            )

    dependency.policy = policy  # type: ignore[attr-defined] -- read by the route-table test
    return dependency


def _rate_limit_headers(target: object, decision: object) -> None:
    try:
        set_rate_limit_headers(target, decision)
    except Exception:  # cosmetic; never worth failing a request over
        log.debug("arcjet.rate_limit_headers_failed", exc_info=True)


async def scan_issue(request: Request, protection: Protection | None, issue: Issue) -> None:
    """Observe-only prompt-injection scan of an issue. Never raises, never blocks."""
    if protection is None or protection.scan is None:
        return
    text = f"{issue.title}\n\n{issue.body or ''}"[:SCAN_MAX_CHARS]
    try:
        decision = await protection.scan.protect(request, detect_prompt_injection_message=text)
    except Exception:
        log.warning("arcjet.scan_failed", issue_number=issue.number, exc_info=True)
        return
    log.info(
        "arcjet.issue_scan",
        issue_number=issue.number,
        # A DRY_RUN rule never makes the decision a denial, so read the per-rule results.
        flagged=any(r.is_denied() for r in decision.results),
        errored=bool(decision.is_error()),
        decision_id=decision.id,
    )
