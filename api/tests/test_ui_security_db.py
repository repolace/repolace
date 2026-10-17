"""CSP, same-origin, escaping and the benchmark oracle nulls, checked on real rendered pages."""

import hashlib
import re
from typing import ClassVar

import httpx
import pytest
from api_support import FakeGithub, raw_issue, seed_repo, seed_task
from repolace_api.routes.github import router as github_router
from repolace_api.routes.tasks import BENCHMARK_SCRUBBED_FIELDS
from repolace_api.ui import STATIC_DIR
from repolace_api.ui.security import CSP
from repolace_shared.db.models import Task, TaskOutcome, TaskStatus
from sqlalchemy import func, select
from ui_support import SAME_ORIGIN, at, seed_run, seed_stage, ui_app, ui_client

pytestmark = [pytest.mark.anyio, pytest.mark.db]

XSS = '</pre><script>alert("xss")</script><img src=x onerror=alert(1)>'


async def get(session_factory, path: str, github=None):
    async with ui_client(session_factory, github) as client:
        return await client.get(path)


class TestCsp:
    async def test_every_ui_response_carries_the_policy(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING)
        for path in (
            "/ui/repos",
            f"/ui/repos/{repo.id}/tasks",
            f"/ui/repos/{repo.id}/tasks/new",
            f"/ui/repos/{repo.id}/tasks/new/issues",
            f"/ui/tasks/{task.id}",
            f"/ui/tasks/{task.id}/live",
            f"/ui/tasks/{task.id}/row",
            "/ui/repos/00000000-0000-0000-0000-000000000000/tasks",  # the HTML 404
            "/ui/static/app.css",
        ):
            response = await get(db_session_factory, path, FakeGithub(raw_issue(1)))
            assert response.headers["content-security-policy"] == CSP, path
            assert response.headers["x-content-type-options"] == "nosniff", path

    def test_the_policy_allows_no_inline_script_or_style_and_no_remote_origin(self):
        directives = dict(part.strip().split(" ", 1) for part in CSP.split(";"))
        assert directives["default-src"] == "'self'"
        assert directives["script-src"] == "'self'"
        assert directives["style-src"] == "'self'"
        assert directives["frame-ancestors"] == "'none'"
        assert directives["object-src"] == "'none'"
        assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP
        assert "http" not in CSP and "*" not in CSP

    async def test_the_json_api_is_not_given_the_ui_policy(self, db_session, db_session_factory):
        await seed_repo(db_session)
        response = await get(db_session_factory, "/repos")
        assert "content-security-policy" not in response.headers

    async def test_pages_load_nothing_from_another_origin_and_use_no_inline_script_or_style(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session, repo, status=TaskStatus.PR_OPENED, outcome=TaskOutcome.PASSED, pr_url="https://github.com/a/b/pull/1",
            pr_number=1, patch_diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
        )
        for path in (
            "/ui/repos", f"/ui/repos/{repo.id}/tasks", f"/ui/repos/{repo.id}/tasks/new", f"/ui/tasks/{task.id}",
        ):
            html = (await get(db_session_factory, path, FakeGithub(raw_issue(1)))).text
            assert not re.search(r"<script(?![^>]*\bsrc=\"/ui/static/)", html), path
            assert not re.search(r"<script[^>]*src=\"(?!/ui/static/)", html), path
            assert not re.search(r"<link[^>]*href=\"(?!/ui/static/)", html), path
            assert not re.search(r'\sstyle="', html), path
            assert not re.search(r"\son[a-z]+=", html), path
            for tag in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", html):
                assert 'rel="noopener noreferrer"' in tag

    def test_the_stylesheet_and_font_faces_reference_only_local_files(self):
        css = (STATIC_DIR / "app.css").read_text()
        urls = re.findall(r"url\(([^)]*)\)", css)
        assert urls and all(u.strip("\"'").startswith("/ui/static/fonts/") for u in urls)
        for u in urls:
            assert (STATIC_DIR / u.strip("\"'").removeprefix("/ui/static/")).is_file()


class TestStatic:
    async def test_assets_are_served_with_the_right_type_and_cache_policy(self, db_session_factory):
        css = await get(db_session_factory, "/ui/static/app.css")
        js = await get(db_session_factory, "/ui/static/vendor/htmx.min.js")
        theme = await get(db_session_factory, "/ui/static/theme.js")
        font = await get(db_session_factory, "/ui/static/fonts/ibm-plex-mono-latin-500.woff2")
        licence = await get(db_session_factory, "/ui/static/fonts/OFL.txt")

        assert css.status_code == js.status_code == theme.status_code == font.status_code == 200
        assert "text/css" in css.headers["content-type"]
        assert "javascript" in js.headers["content-type"]
        assert css.headers["cache-control"] == "no-cache"
        assert "immutable" in font.headers["cache-control"]
        assert font.content[:4] == b"wOF2"
        assert "SIL Open Font License" in licence.text

    async def test_the_vendored_htmx_matches_the_recorded_hash(self, db_session_factory):
        readme = (STATIC_DIR / "README.md").read_text()
        digest = hashlib.sha256((STATIC_DIR / "vendor" / "htmx.min.js").read_bytes()).hexdigest()
        assert digest in readme
        assert "htmx.org@2.0.4" in readme

    async def test_the_six_font_files_the_stylesheet_needs_exist(self):
        for family in ("sans", "mono"):
            for weight in (400, 500, 600):
                assert (STATIC_DIR / "fonts" / f"ibm-plex-{family}-latin-{weight}.woff2").stat().st_size > 5000

    async def test_a_path_outside_the_static_directory_is_not_served(self, db_session_factory):
        assert (await get(db_session_factory, "/ui/static/../router.py")).status_code == 404
        assert (await get(db_session_factory, "/ui/static/%2e%2e/router.py")).status_code == 404

    async def test_the_base_page_links_the_assets_and_turns_htmx_eval_off(self, db_session_factory):
        html = (await get(db_session_factory, "/ui/repos")).text
        assert 'src="/ui/static/vendor/htmx.min.js"' in html and 'href="/ui/static/app.css"' in html
        assert '"allowEval": false' in html and '"includeIndicatorStyles": false' in html
        assert 'href="#main"' in html  # skip link
        assert '<html lang="en"' in html


class TestSameOrigin:
    async def post(self, session_factory, repo, headers, **form):
        async with ui_client(session_factory, FakeGithub(raw_issue(7))) as client:
            return await client.post(
                f"/ui/repos/{repo.id}/tasks",
                data={"issue_number": "7", "target_branch": "main", **form},
                headers=headers,
                follow_redirects=False,
            )

    async def count(self, session) -> int:
        return (await session.execute(select(func.count()).select_from(Task))).scalar_one()

    @pytest.mark.parametrize(
        "headers",
        [
            {"Origin": "http://evil.example"},
            {"Origin": "null"},
            {"Origin": "http://test.evil.example"},
            {"Referer": "http://evil.example/page"},
            {},  # neither header: refused, not trusted
            {"Origin": "http://test", "Sec-Fetch-Site": "cross-site"},
            {"Origin": "http://evil.example", "Referer": "http://test/ui/repos"},  # Origin wins over Referer
        ],
        ids=["other-origin", "null-origin", "suffix-trick", "other-referer", "no-headers", "fetch-metadata", "origin-wins"],
    )
    async def test_a_cross_origin_post_is_refused_and_creates_nothing(self, db_session, db_session_factory, headers):
        repo = await seed_repo(db_session)

        response = await self.post(db_session_factory, repo, headers)

        assert response.status_code == 403
        assert "text/html" in response.headers["content-type"]
        assert await self.count(db_session) == 0

    @pytest.mark.parametrize(
        "headers",
        [SAME_ORIGIN, {"Referer": "http://test/ui/repos/x/tasks/new"}, {**SAME_ORIGIN, "Sec-Fetch-Site": "same-origin"}],
    )
    async def test_a_same_origin_post_creates_the_task_and_redirects(self, db_session, db_session_factory, headers):
        repo = await seed_repo(db_session)

        response = await self.post(db_session_factory, repo, headers)

        assert response.status_code == 303
        assert await self.count(db_session) == 1
        task = (await db_session.execute(select(Task))).scalar_one()
        assert response.headers["location"] == f"/ui/tasks/{task.id}"

    async def test_get_requests_are_not_origin_checked(self, db_session_factory):
        assert (await get(db_session_factory, "/ui/repos")).status_code == 200

    async def test_an_oversized_form_is_refused(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        response = await self.post(db_session_factory, repo, SAME_ORIGIN, junk="x" * 20000)
        assert response.status_code == 413


class TestEscaping:
    async def test_stored_text_in_every_field_is_escaped_on_every_page(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session,
            repo,
            issue_title=XSS,
            target_branch=XSS,
            status=TaskStatus.FAILED,
            outcome=TaskOutcome.PASSED_WITH_TEST_EDIT,
            test_edit_justification=XSS,
            test_edit_approved_by=XSS,
            test_edit_approved_at=at(1),
            error_message=XSS,
            score_reason=XSS,
            agent_stop_reason="submitted",
            pr_url="https://github.com/a/b/pull/1",
            pr_number=1,
            patch_diff=f"diff --git a/{XSS} b/{XSS}\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-{XSS}\n+{XSS}\n",
        )
        await seed_stage(db_session, task, "verify_baseline", "failed", 0, 1, error=XSS)
        await seed_run(db_session, task, 0, error=XSS)
        github = FakeGithub(raw_issue(1, title=XSS, labels=[{"name": XSS}], user={"login": XSS}))

        for path in (
            f"/ui/repos/{repo.id}/tasks",
            f"/ui/repos/{repo.id}/tasks/new",
            f"/ui/repos/{repo.id}/tasks/new/issues",
            f"/ui/repos/{repo.id}/tasks/new/confirm?issue_number=1&target_branch={XSS}",
            f"/ui/tasks/{task.id}",
            f"/ui/tasks/{task.id}/live",
            f"/ui/tasks/{task.id}/row",
        ):
            html = (await get(db_session_factory, path, github)).text
            assert "<script>" not in html and "<script " not in html.replace('<script src="/ui/static', ""), path
            assert "</pre><script" not in html and "<img src=x" not in html, path
            assert "<img" not in html, path

        detail = (await get(db_session_factory, f"/ui/tasks/{task.id}")).text
        assert "&lt;script&gt;alert(&#34;xss&#34;)&lt;/script&gt;" in detail  # shown, as text

    async def test_a_hostile_post_value_is_escaped_in_the_error_page(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        async with ui_client(db_session_factory, FakeGithub(raw_issue(7))) as client:
            response = await client.post(
                f"/ui/repos/{repo.id}/tasks",
                data={"issue_number": "7", "target_branch": XSS},
                headers=SAME_ORIGIN,
            )
        assert response.status_code == 422
        assert "<script>" not in response.text and "<img src=x" not in response.text
        assert "&lt;script&gt;" in response.text


class TestBenchmarkOracle:
    SENTINELS: ClassVar[dict] = {
        "score_reason": "SENTINEL-SCORE tests/hidden/test_secret.py::test_answer",
        "patch_diff": "diff --git a/SENTINEL-DIFF.py b/SENTINEL-DIFF.py\n--- a/SENTINEL-DIFF.py\n+++ b/SENTINEL-DIFF.py\n@@ -1 +1 @@\n-a\n+SENTINEL-ADDED\n",
        "changed_files": ["SENTINEL-FILE.py"],
        "error_message": "SENTINEL-ERROR hidden/test_secret.py",
        "test_edit_justification": "SENTINEL-JUSTIFICATION",
    }

    async def seed_benchmark(self, db_session, repo, **overrides):
        fields = {
            **self.SENTINELS,
            "eval_run_id": "run-1",
            "instance_id": "inst-1",
            "run_index": 0,
            "status": TaskStatus.FAILED,
            "outcome": TaskOutcome.PASSED_WITH_TEST_EDIT,
            "agent_stop_reason": "submitted",
        }
        task = await seed_task(db_session, repo, **{**fields, **overrides})
        await seed_stage(db_session, task, "verify_baseline", "failed", 0, 1, error="SENTINEL-STAGE-ERROR")
        await seed_run(db_session, task, 0, passed=2, failed=1)
        return task

    def assert_no_sentinel(self, html: str, where: str):
        assert "SENTINEL" not in html, where
        assert "test_secret" not in html, where

    async def test_the_benchmark_detail_page_shows_the_same_nulls_the_json_route_returns(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        task = await self.seed_benchmark(db_session, repo)

        async with ui_client(db_session_factory) as client:
            api = (await client.get(f"/tasks/{task.id}")).json()
            html = (await client.get(f"/ui/tasks/{task.id}")).text
            live = (await client.get(f"/ui/tasks/{task.id}/live")).text
            row = (await client.get(f"/ui/tasks/{task.id}/row")).text
            listing = (await client.get(f"/ui/repos/{repo.id}/tasks?benchmark=1")).text

        for name in BENCHMARK_SCRUBBED_FIELDS:
            assert api[name] is None, name
        assert all(s["error"] is None for s in api["stages"])
        for where, body in (("detail", html), ("live", live), ("row", row), ("list", listing)):
            self.assert_no_sentinel(body, where)
        # the page says the text is withheld rather than showing an empty block
        assert "withheld" in html
        assert "benchmark" in html
        # what is not oracle material stays: status, outcome, stop reason, counts
        assert "submitted" in html and "failed" in html

    async def test_the_scrub_is_the_json_routes_function_not_a_copy(self):
        import importlib
        import inspect

        source = "".join(
            inspect.getsource(importlib.import_module(f"repolace_api.ui.{name}")) for name in ("router", "views")
        )
        for name in BENCHMARK_SCRUBBED_FIELDS:
            assert f'"{name}"' not in source, f"{name} is scrubbed in the UI layer: it must come from load_task_detail"
        assert "load_task_detail" in source

    async def test_benchmark_rows_are_hidden_from_the_list_until_asked_for(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        await seed_task(db_session, repo, issue_number=1, issue_title="product task")
        await self.seed_benchmark(db_session, repo, issue_number=2, issue_title="bench task")

        default = (await get(db_session_factory, f"/ui/repos/{repo.id}/tasks")).text
        shown = (await get(db_session_factory, f"/ui/repos/{repo.id}/tasks?benchmark=1")).text

        assert "product task" in default and "bench task" not in default
        assert "product task" in shown and "bench task" in shown
        assert '<span class="tag">benchmark</span>' in shown
        assert 'name="benchmark" value="1" checked' in shown
        assert 'value="1" checked' not in default

    async def test_a_benchmark_cost_estimate_never_sees_benchmark_rows(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        for i in range(6):
            await self.seed_benchmark(db_session, repo, issue_number=i + 1, cost_usd=1.5, run_index=i)
        html = (await get(db_session_factory, f"/ui/repos/{repo.id}/tasks/new/confirm")).text
        assert "No estimate yet" in html


class TestHostCheck:
    """The allowlist is for the UI and API; the signed webhook must be reachable on the public name."""

    def client(self, session_factory) -> httpx.AsyncClient:
        app = ui_app(session_factory)
        app.include_router(github_router)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://tunnel.example")

    async def test_the_webhook_is_reached_on_a_host_off_the_allowlist_and_still_needs_its_signature(
        self, db_session_factory
    ):
        async with self.client(db_session_factory) as client:
            response = await client.post("/github/webhook", content=b"{}")
        assert response.status_code == 401  # the handler's missing-signature refusal, not the Host 400

    @pytest.mark.parametrize("path", ["/ui/repos", "/repos", "/github/webhook/extra", "/github"])
    async def test_every_other_path_still_refuses_a_host_off_the_allowlist(self, db_session_factory, path):
        async with self.client(db_session_factory) as client:
            response = await client.get(path)
        assert response.status_code == 400
        assert response.text == "Invalid host header"
