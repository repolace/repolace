"""Review fixes: Host allowlist (DNS rebinding), Pygments budget, request-size caps, headers on 500s, safe links."""

import hashlib
import re
import time

import httpx
import pytest
from api_support import FakeGithub, raw_issue, seed_repo, seed_task
from fastapi import FastAPI, Request
from repolace_api.ui import STATIC_DIR, mount_ui
from repolace_api.ui.diffview import LEXERS, MAX_HIGHLIGHT_LINE, highlight_line, parse_diff
from repolace_api.ui.security import CSP, normalise_host, parse_allowed_hosts, same_origin
from repolace_shared.db.models import TaskStatus
from ui_support import SAME_ORIGIN, seed_stage, ui_client

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def bare_app(hosts) -> FastAPI:
    app = FastAPI()
    mount_ui(app, allowed_hosts=hosts)

    @app.get("/ui/boom")
    def boom():
        raise RuntimeError("secret host path /home/worker/.env")

    @app.get("/plain")
    def plain():
        return {"ok": True}

    return app


def client_of(app: FastAPI, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, **kwargs), base_url="http://localhost")


class TestHostAllowlist:
    async def test_a_foreign_host_is_refused_on_pages_and_on_the_json_api(self):
        async with client_of(bare_app(None)) as client:
            for path in ("/ui/static/app.css", "/plain", "/ui/repos"):
                response = await client.get(path, headers={"Host": "evil.example"})
                assert response.status_code == 400, path
                assert "evil.example" not in response.text

    @pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8765", "[::1]", "[::1]:8000", "LOCALHOST"])
    async def test_the_default_hosts_are_allowed_with_or_without_a_port(self, host):
        async with client_of(bare_app(None)) as client:
            assert (await client.get("/plain", headers={"Host": host})).status_code == 200

    async def test_the_list_is_configurable_by_argument_and_by_environment(self, monkeypatch):
        async with client_of(bare_app("tunnel.example, localhost")) as client:
            assert (await client.get("/plain", headers={"Host": "tunnel.example"})).status_code == 200
            assert (await client.get("/plain", headers={"Host": "other.example"})).status_code == 400
        monkeypatch.setenv("UI_ALLOWED_HOSTS", "from-env.example")
        async with client_of(bare_app(None)) as client:
            assert (await client.get("/plain", headers={"Host": "from-env.example"})).status_code == 200
            assert (await client.get("/plain", headers={"Host": "localhost"})).status_code == 400

    def test_parsing_normalises_ports_brackets_and_falls_back_to_localhost(self):
        assert parse_allowed_hosts("A.example:8000, [::1]:9") == {"a.example", "::1"}
        assert parse_allowed_hosts("") == {"localhost", "127.0.0.1", "::1"}
        assert normalise_host("[::1]:8000") == "::1"
        assert normalise_host("host:80") == "host"

    async def test_a_rebinding_origin_is_not_trusted_just_because_it_matches_the_host(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        # Host passes the middleware (allowlisted), but the Origin is the attacker's: refused.
        async with ui_client(db_session_factory, FakeGithub(raw_issue(7))) as client:
            response = await client.post(
                f"/ui/repos/{repo.id}/tasks",
                data={"issue_number": "7"},
                headers={"Origin": "http://evil.example"},
            )
        assert response.status_code == 403

    def test_same_origin_compares_against_the_allowlist_never_against_host(self):
        app = FastAPI()
        app.state.ui_allowed_hosts = parse_allowed_hosts("localhost")

        def request(host: str, origin: str) -> Request:
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/",
                "app": app,
                "headers": [(b"host", host.encode()), (b"origin", origin.encode())],
            }
            return Request(scope)

        assert same_origin(request("localhost:8000", "http://localhost:8000"))
        assert not same_origin(request("evil.example", "http://evil.example"))  # origin == host, still refused
        assert not same_origin(request("localhost", "http://evil.example"))

    @pytest.mark.parametrize("value", ["http://[", "http://[::1", "http://exa mple:99999999", "//"])
    async def test_a_malformed_origin_or_referer_is_refused_not_a_500(self, db_session, db_session_factory, value):
        repo = await seed_repo(db_session)
        for header in ("Origin", "Referer"):
            async with ui_client(db_session_factory, FakeGithub(raw_issue(7))) as client:
                response = await client.post(
                    f"/ui/repos/{repo.id}/tasks", data={"issue_number": "7"}, headers={header: value}
                )
            assert response.status_code == 403, (header, value)


class TestRequestSizes:
    async def post(self, db_session_factory, **kwargs):
        headers = {**SAME_ORIGIN, **kwargs.pop("headers", {})}
        async with ui_client(db_session_factory, FakeGithub(raw_issue(7))) as client:
            return await client.post("/ui/repos/00000000-0000-0000-0000-000000000000/tasks", headers=headers, **kwargs)

    async def test_a_declared_oversize_body_is_refused_before_it_is_read(self, db_session_factory):
        response = await self.post(
            db_session_factory, content=b"x" * 20000, headers={"content-type": "application/x-www-form-urlencoded"}
        )
        assert response.status_code == 413

    async def test_a_chunked_body_with_no_length_is_capped_while_streaming(self, db_session_factory):
        async def body():
            for _ in range(40):
                yield b"a=" + b"x" * 1000 + b"&"

        response = await self.post(db_session_factory, content=body())
        assert response.status_code == 413


class TestHeadersOnErrors:
    async def test_an_unhandled_500_carries_the_csp_and_leaks_nothing(self):
        async with client_of(bare_app(None), raise_app_exceptions=False) as client:
            response = await client.get("/ui/boom")
        assert response.status_code == 500
        assert response.headers["content-security-policy"] == CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "text/html" in response.headers["content-type"]
        assert "secret host path" not in response.text
        assert "RuntimeError" not in response.text

    async def test_htmx_is_locked_down_and_error_and_diff_containers_are_not_processed(
        self, db_session, db_session_factory
    ):
        repo = await seed_repo(db_session)
        task = await seed_task(
            db_session,
            repo,
            status=TaskStatus.FAILED,
            error_message="boom",
            patch_diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
        )
        async with ui_client(db_session_factory) as client:
            page = (await client.get(f"/ui/tasks/{task.id}")).text
        assert '"allowScriptTags": false' in page
        assert '"selfRequestsOnly": true' in page
        assert re.search(r'<section class="err-block"[^>]* hx-disable>', page)
        assert re.search(r'<details class="diff" hx-disable', page)


class TestLinks:
    async def test_only_https_urls_become_links(self, db_session, db_session_factory):
        repo = await seed_repo(db_session)
        bad = await seed_task(
            db_session,
            repo,
            status=TaskStatus.PR_OPENED,
            pr_url="javascript:alert(1)",
            pr_number=1,
            issue_url="data:text/html,x",
            issue_number=1,
        )
        good = await seed_task(
            db_session,
            repo,
            status=TaskStatus.PR_OPENED,
            pr_url="https://github.com/a/b/pull/1",
            pr_number=1,
            issue_number=2,
        )
        async with ui_client(db_session_factory) as client:
            bad_page = (await client.get(f"/ui/tasks/{bad.id}")).text
            bad_list = (await client.get(f"/ui/repos/{repo.id}/tasks")).text
            good_page = (await client.get(f"/ui/tasks/{good.id}")).text
        assert 'href="javascript:' not in bad_page + bad_list
        assert 'href="data:' not in bad_page
        assert 'href="https://github.com/a/b/pull/1"' in good_page


class TestFontHashes:
    def test_every_font_hash_in_the_readme_matches_the_file(self):
        readme = (STATIC_DIR / "README.md").read_text()
        fonts = sorted((STATIC_DIR / "fonts").glob("*.woff2"))
        assert len(fonts) == 6
        for font in fonts:
            assert hashlib.sha256(font.read_bytes()).hexdigest() in readme, font.name


SLOW = {
    "x.ly": "\\relative c' { " + "a" * 800,
    "x.mcschema": '{"a": "' + "\\" * 800,
    "x.service": "[Unit]\n" + "=" * 400 + " " * 400,
}


class TestHighlightBudget:
    @pytest.mark.parametrize("name", list(SLOW))
    def test_a_slow_lexer_extension_is_never_lexed_so_the_diff_renders_fast(self, name):
        line = SLOW[name]
        diff = f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -0,0 +1,50 @@\n" + (f"+{line}\n" * 50)
        started = time.perf_counter()
        parsed = parse_diff(diff)
        assert time.perf_counter() - started < 1.0
        assert all("<span" not in str(ln.html) for ln in parsed.files[0].lines if ln.kind == "add")

    @pytest.mark.parametrize(
        ("name", "code", "token"),
        [
            ("a.py", "def f(): return 1", '<span class="k">def</span>'),
            ("a.js", "const x = 1;", '<span class="k">const</span>'),
            ("a.json", '{"a": 1}', '<span class="nu">1</span>'),
            ("a.sh", "echo hi", '<span class="f">echo</span>'),
            ("a.go", "func main() {}", '<span class="k">func</span>'),
            ("a.rs", "fn main() {}", '<span class="k">fn</span>'),
            ("a.sql", "SELECT 1", '<span class="k">SELECT</span>'),
            ("a.css", "a { color: red; }", "<span"),
            ("a.html", "<div>x</div>", "<span"),
            ("a.toml", "a = 1", "<span"),
            ("a.yaml", "a: 1", "<span"),
        ],
    )
    def test_the_allowlisted_languages_still_highlight(self, name, code, token):
        assert token in str(highlight_line(code, name))

    def test_the_allowlist_is_exactly_the_agreed_extensions(self):
        assert set(LEXERS) == {
            ".py", ".js", ".ts", ".tsx", ".json", ".md", ".toml", ".yml", ".yaml", ".sh", ".css", ".html", ".go",
            ".rs", ".sql", ".txt",
        }  # fmt: skip

    def test_a_long_line_and_an_exhausted_budget_fall_back_to_escaped_text(self):
        long_line = "x = '<b>'" + " " * MAX_HIGHLIGHT_LINE
        rendered = str(highlight_line(long_line, "a.py"))
        assert "<span" not in rendered
        assert "&lt;b&gt;" in rendered
        diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+def f(): pass\n"
        assert "<span" in str(parse_diff(diff).files[0].lines[1].html)
        assert "<span" not in str(parse_diff(diff, budget_seconds=-1).files[0].lines[1].html)

    async def test_the_task_page_renders_the_diff_off_the_event_loop(self, db_session, db_session_factory, monkeypatch):
        import importlib

        router_module = importlib.import_module("repolace_api.ui.router")

        calls = []
        real = router_module.run_in_threadpool

        async def spy(func, *args):
            calls.append(func.__name__)
            return await real(func, *args)

        monkeypatch.setattr(router_module, "run_in_threadpool", spy)
        repo = await seed_repo(db_session)
        task = await seed_task(db_session, repo, status=TaskStatus.RUNNING)
        await seed_stage(db_session, task, "agent", "running", 0, None)
        async with ui_client(db_session_factory) as client:
            await client.get(f"/ui/tasks/{task.id}")
            await client.get(f"/ui/tasks/{task.id}/live")
        assert calls == ["build_detail", "build_detail"]
