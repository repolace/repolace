"""Access rules that need no database: every route is protected, and the API will not start open."""

import pytest
from api_billing_support import settings
from fastapi.routing import APIRoute
from repolace_api.config import access_startup_error
from repolace_api.deps import require_viewer
from repolace_api.main import app

#: Routes that do not need a signed-in viewer, each with its reason. Anything not listed must depend
#: on `require_viewer`, so a new route that forgets the check fails here rather than shipping open
#: (the other tests replace `require_viewer` with all-access and would not notice).
EXEMPT = {
    "/healthz": "liveness probe",
    "/": "redirects to /ui/repos, which is protected",
    "/github/install": "redirect to GitHub's install page",
    "/github/callback": "post-install redirect into sign-in; reads and writes nothing",
    "/github/webhook": "authenticated by its HMAC signature",
    "/billing/webhook": "authenticated by Stripe's signature",
    "/billing/checkout": "requires a signed-in user (`current_user`)",
    "/billing/portal": "requires a signed-in user (`current_user`)",
    "/auth/github/login": "is sign-in",
    "/auth/github/callback": "is sign-in",
    "/auth/logout": "is sign-out",
    "/ui/account": "shows the sign-in link when signed out",
}


def _dependency_calls(dependant) -> set:
    calls = set()
    for sub in dependant.dependencies:
        calls.add(sub.call)
        calls |= _dependency_calls(sub)
    return calls


def api_routes(routes, prefix: str = "", inherited: frozenset = frozenset()):
    """Every `APIRoute` the app serves, as (full path, route, include-level dependency calls).

    FastAPI 0.14x keeps an included router as one `_IncludedRouter` entry in `app.routes`, so a flat
    walk would see none of the repo, task or UI routes; this descends into `original_router`.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route, inherited
        elif hasattr(route, "original_router") and hasattr(route, "include_context"):
            ctx = route.include_context
            extra = frozenset(d.dependency for d in (ctx.dependencies or []))
            yield from api_routes(route.original_router.routes, prefix + (ctx.prefix or ""), inherited | extra)


ROUTES = list(api_routes(app.routes))


def test_the_walk_finds_the_repo_task_and_ui_routes():
    paths = {path for path, _, _ in ROUTES}
    assert {"/repos", "/repos/{repo_id}/tasks", "/tasks/{task_id}", "/ui/repos", "/ui/tasks/{task_id}/live"} <= paths


def test_every_route_requires_a_viewer_unless_exempt():
    unprotected = [
        f"{sorted(route.methods)} {path}"
        for path, route, inherited in ROUTES
        if path not in EXEMPT and require_viewer not in (_dependency_calls(route.dependant) | inherited)
    ]
    assert unprotected == []


def test_the_exempt_list_names_only_routes_that_exist():
    assert set(EXEMPT) - {path for path, _, _ in ROUTES} == set()


def test_the_openapi_docs_are_not_api_routes():
    """`/docs` and `/openapi.json` are FastAPI's own routes, not `APIRoute`s: they list paths, never data."""
    assert not {"/docs", "/openapi.json", "/redoc"} & {path for path, _, _ in ROUTES}


class TestStartup:
    def test_sign_in_configured_starts(self):
        assert access_startup_error(settings(), ["localhost"]) is None

    def test_no_sign_in_and_no_dev_flag_refuses(self):
        problem = access_startup_error(settings(github_oauth_client_id=None), ["localhost"])
        assert problem is not None and "DEV_NO_AUTH" in problem

    def test_dev_no_auth_on_loopback_starts(self):
        local = settings(github_oauth_client_id=None, dev_no_auth=True)
        assert access_startup_error(local, ["localhost", "127.0.0.1", "::1"]) is None

    @pytest.mark.parametrize("hosts", [["localhost", "repolace.example.com"], ["0.0.0.0"]])
    def test_dev_no_auth_with_a_public_host_refuses(self, hosts):
        problem = access_startup_error(settings(dev_no_auth=True), hosts)
        assert problem is not None and "non-loopback" in problem

    def test_dev_no_auth_with_an_https_base_url_refuses(self):
        problem = access_startup_error(settings(dev_no_auth=True, api_base_url="https://repolace.example.com"), ["localhost"])
        assert problem is not None and "https" in problem
