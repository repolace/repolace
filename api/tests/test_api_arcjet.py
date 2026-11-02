"""Arcjet protection: policy routing, denial mapping, fail-open, the no-key no-op and the issue scan.

Arcjet itself is never called: a fake client stands in for each policy, so these tests need neither a
key nor the network. What they hold is the wiring (which route asks for which policy, one `protect()`
per request) and the behaviour around the SDK call.
"""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import structlog
import structlog.testing
from fastapi import Depends, FastAPI
from repolace_api import protection
from repolace_api.deps import get_github_client
from repolace_api.protection import (
    ENQUEUE,
    GITHUB_READ,
    SCAN_MAX_CHARS,
    SHIELD,
    WEBHOOK,
    ArcjetSettings,
    Protection,
    get_protection,
    protect,
    scan_issue,
)
from repolace_api.routes.github import router as github_router
from repolace_api.routes.repos import router as repos_router
from repolace_api.routes.tasks import router as tasks_router
from repolace_api.ui import mount_ui
from repolace_api.ui.router import router as ui_router
from repolace_shared.github.schemas import Issue

pytestmark = pytest.mark.anyio


class FakeDecision:
    def __init__(self, *, denied: str | None = None, error: bool = False, flagged: bool = False):
        self.id = "d1"
        self._denied = denied
        self._error = error
        self.reason_v2 = SimpleNamespace(type=denied or "SHIELD")
        self.results = [SimpleNamespace(is_denied=lambda: flagged)]

    def is_denied(self) -> bool:
        return self._denied is not None

    def is_error(self) -> bool:
        return self._error


class FakeClient:
    """Records every `protect()` call and answers with a scripted decision (or raises)."""

    def __init__(self, decision: FakeDecision | None = None, raises: Exception | None = None):
        self.decision = decision or FakeDecision()
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def protect(self, request, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.decision


def fake_protection(scan: FakeClient | None = None, **clients: FakeClient) -> Protection:
    policies = {name: FakeClient() for name in (SHIELD, GITHUB_READ, ENQUEUE, WEBHOOK)}
    policies.update(clients)
    return Protection(policies=policies, scan=scan)


@pytest.fixture(autouse=True)
def stamp_headers(monkeypatch):
    """The real writer needs real rule results; a marker is enough to see that it ran."""

    def fake(target, decision):
        headers = target.headers if hasattr(target, "headers") else target
        headers["RateLimit"] = "stamped"

    monkeypatch.setattr(protection, "set_rate_limit_headers", fake)


def app_with(protection_: Protection | None, policy: str = SHIELD, **kwargs: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/probe", dependencies=[Depends(protect(policy, **kwargs))])
    async def probe_get() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/probe", dependencies=[Depends(protect(policy, **kwargs))])
    async def probe_post() -> dict[str, str]:
        return {"ok": "yes"}

    app.dependency_overrides[get_protection] = lambda: protection_
    return app


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def get(app: FastAPI, method: str = "GET") -> httpx.Response:
    async with client_for(app) as client:
        return await client.request(method, "/probe")


class TestDecisions:
    async def test_no_key_means_no_protection_and_no_calls(self):
        response = await get(app_with(None))

        assert response.status_code == 200
        assert "ratelimit" not in response.headers

    async def test_an_allowed_request_goes_through_with_the_rate_limit_headers(self):
        shield = FakeClient()

        response = await get(app_with(fake_protection(**{SHIELD: shield})))

        assert response.status_code == 200
        assert response.headers["ratelimit"] == "stamped"
        assert len(shield.calls) == 1

    async def test_a_rate_limit_denial_is_a_429_with_the_headers(self):
        client = FakeClient(FakeDecision(denied="RATE_LIMIT"))

        response = await get(app_with(fake_protection(**{SHIELD: client})))

        assert response.status_code == 429
        assert response.json() == {"detail": "Too many requests"}
        assert response.headers["ratelimit"] == "stamped"

    @pytest.mark.parametrize("reason", ["SHIELD", "BOT", "FILTER"])
    async def test_any_other_denial_is_a_403(self, reason):
        client = FakeClient(FakeDecision(denied=reason))

        response = await get(app_with(fake_protection(**{SHIELD: client})))

        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}

    async def test_an_arcjet_error_decision_lets_the_request_through(self):
        client = FakeClient(FakeDecision(error=True))

        response = await get(app_with(fake_protection(**{SHIELD: client})))

        assert response.status_code == 200

    async def test_an_exception_from_the_sdk_lets_the_request_through(self):
        client = FakeClient(raises=RuntimeError("decide.arcjet.com unreachable"))

        with structlog.testing.capture_logs() as logs:
            response = await get(app_with(fake_protection(**{SHIELD: client})))

        assert response.status_code == 200
        assert any(entry["event"] == "arcjet.protect_failed" for entry in logs)


class TestPolicies:
    async def test_a_route_spends_only_its_own_policy_once(self):
        clients = {name: FakeClient() for name in (SHIELD, GITHUB_READ, ENQUEUE, WEBHOOK)}

        await get(app_with(fake_protection(**clients), ENQUEUE))

        assert {name: len(c.calls) for name, c in clients.items()} == {
            SHIELD: 0,
            GITHUB_READ: 0,
            ENQUEUE: 1,
            WEBHOOK: 0,
        }

    async def test_a_safe_methods_only_dependency_skips_a_post(self):
        shield = FakeClient()
        app = app_with(fake_protection(**{SHIELD: shield}), safe_methods_only=True)

        await get(app, "POST")
        assert shield.calls == []
        await get(app, "GET")
        assert len(shield.calls) == 1


def policies_of(route) -> list[str]:
    """Policy names on a route. FastAPI has already folded the router-level dependencies into it."""
    return [d.dependency.policy for d in route.dependencies if hasattr(d.dependency, "policy")]


class TestRouteTable:
    """Every route the API serves names a policy, so a new route cannot ship unprotected by accident."""

    def routes(self):
        for router in (github_router, repos_router, tasks_router, ui_router):
            for route in router.routes:
                for method in sorted(route.methods - {"HEAD"}):
                    yield method, route.path, policies_of(route)

    def test_every_route_asks_for_a_policy(self):
        # The UI router's own dependency skips non-GET, so its POST must carry one of its own.
        missing = [
            (method, path)
            for method, path, names in self.routes()
            if not names or (path.startswith("/ui") and method != "GET" and names == [SHIELD])
        ]

        assert missing == []

    def test_no_route_asks_for_two_effective_policies(self):
        for method, path, names in self.routes():
            effective = [n for n in names if not (n == SHIELD and method != "GET" and path.startswith("/ui"))]
            assert len(effective) == 1, (method, path, names)

    def test_the_expensive_routes_use_the_strict_policies(self):
        by_route = {(method, path): names for method, path, names in self.routes()}

        assert ENQUEUE in by_route[("POST", "/repos/{repo_id}/tasks")]
        assert ENQUEUE in by_route[("POST", "/ui/repos/{repo_id}/tasks")]
        assert by_route[("GET", "/repos/{repo_id}/issues")] == [GITHUB_READ]
        assert by_route[("GET", "/repos/{repo_id}/branches")] == [GITHUB_READ]
        assert by_route[("POST", "/github/webhook")] == [WEBHOOK]


class TestUiEnqueueDenial:
    async def test_a_denied_form_post_is_an_html_429_before_any_database_work(self):
        app = FastAPI()
        app.include_router(repos_router)
        mount_ui(app, allowed_hosts=["test"])

        def no_database():
            raise AssertionError("the database was reached after a denial")

        app.state.session_factory = no_database
        app.dependency_overrides[get_github_client] = lambda: None
        app.dependency_overrides[get_protection] = lambda: fake_protection(
            **{ENQUEUE: FakeClient(FakeDecision(denied="RATE_LIMIT"))}
        )

        async with client_for(app) as client:
            response = await client.post(
                "/ui/repos/00000000-0000-0000-0000-000000000001/tasks",
                data={"issue_number": "7"},
                headers={"Origin": "http://test"},
            )

        assert response.status_code == 429
        assert "text/html" in response.headers["content-type"]
        assert "Too many requests" in response.text


ISSUE = Issue(number=7, title="Crash on empty input", html_url="https://x/7", state="open", body="traceback ...")


class TestIssueScan:
    async def test_nothing_is_sent_unless_the_scan_client_exists(self):
        await scan_issue(SimpleNamespace(), fake_protection(scan=None), ISSUE)
        await scan_issue(SimpleNamespace(), None, ISSUE)

    async def test_the_issue_text_is_sent_and_the_verdict_only_logged(self):
        scan = FakeClient(FakeDecision(flagged=True))

        with structlog.testing.capture_logs() as logs:
            await scan_issue(SimpleNamespace(), fake_protection(scan=scan), ISSUE)

        sent = scan.calls[0]["detect_prompt_injection_message"]
        assert "Crash on empty input" in sent
        assert "traceback ..." in sent
        (entry,) = [e for e in logs if e["event"] == "arcjet.issue_scan"]
        assert entry["flagged"] is True

    async def test_a_huge_body_is_cut_and_a_missing_one_is_fine(self):
        scan = FakeClient()
        big = ISSUE.model_copy(update={"body": "x" * (SCAN_MAX_CHARS * 2)})
        empty = ISSUE.model_copy(update={"body": None})

        await scan_issue(SimpleNamespace(), fake_protection(scan=scan), big)
        await scan_issue(SimpleNamespace(), fake_protection(scan=scan), empty)

        assert len(scan.calls[0]["detect_prompt_injection_message"]) == SCAN_MAX_CHARS
        assert scan.calls[1]["detect_prompt_injection_message"].startswith("Crash on empty input")

    async def test_a_failing_scan_never_raises(self):
        scan = FakeClient(raises=RuntimeError("down"))

        await scan_issue(SimpleNamespace(), fake_protection(scan=scan), ISSUE)


class TestFactory:
    @pytest.fixture(autouse=True)
    def fresh_cache(self):
        get_protection.cache_clear()
        yield
        get_protection.cache_clear()

    def settings(self, monkeypatch, **fields: Any):
        monkeypatch.setattr(protection, "get_arcjet_settings", lambda: ArcjetSettings(_env_file=None, **fields))

    def test_no_key_builds_nothing(self, monkeypatch):
        self.settings(monkeypatch)

        assert get_protection() is None

    def test_a_key_builds_every_policy_and_no_scan_by_default(self, monkeypatch):
        self.settings(monkeypatch, arcjet_key="ajkey_test", arcjet_mode="DRY_RUN")

        built = get_protection()

        assert set(built.policies) == {SHIELD, GITHUB_READ, ENQUEUE, WEBHOOK}
        assert built.scan is None

    def test_the_scan_client_exists_only_when_asked_for(self, monkeypatch):
        self.settings(monkeypatch, arcjet_key="ajkey_test", arcjet_scan_issues=True)

        assert get_protection().scan is not None

    def test_the_example_defaults_are_safe(self):
        defaults = ArcjetSettings(_env_file=None)

        assert defaults.arcjet_key is None
        assert defaults.arcjet_scan_issues is False
