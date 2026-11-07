"""`/ui/account`: what each visitor sees, the forms it posts, and that it stays inside the UI's CSP rules."""

import re

import httpx
import pytest
from api_billing_support import FakeStripe, build_app, seed_subscription, seed_user, session_cookie, settings
from api_support import seed_repo, seed_task
from repolace_api.billing_limits import FREE_TASKS_PER_MONTH

pytestmark = [pytest.mark.anyio, pytest.mark.db]


def get(session_factory, *, billing=None, cookies=None, stripe=None) -> httpx.AsyncClient:
    app = build_app(session_factory, billing=billing, stripe_client=stripe)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies)


class TestVisitors:
    async def test_signed_out_gets_a_sign_in_link(self, db_session_factory):
        async with get(db_session_factory) as c:
            response = await c.get("/ui/account")
        assert response.status_code == 200
        assert 'href="/auth/github/login"' in response.text and "Sign in with GitHub" in response.text
        assert "/billing/checkout" not in response.text

    async def test_unconfigured_says_so_and_offers_no_sign_in(self, db_session_factory):
        async with get(db_session_factory, billing=settings(session_secret=None)) as c:
            response = await c.get("/ui/account")
        assert response.status_code == 200
        assert "not configured" in response.text and "/auth/github/login" not in response.text

    async def test_a_free_user_sees_usage_and_an_upgrade_button(self, db_session, db_session_factory):
        user, repo = await seed_user(db_session), await seed_repo(db_session)
        for i in range(2):
            await seed_task(db_session, repo, issue_number=i + 1, user_id=user.id)
        async with get(db_session_factory, cookies=session_cookie(user.id)) as c:
            text = (await c.get("/ui/account")).text
        assert f"2 of {FREE_TASKS_PER_MONTH}" in text and ">free<" in text
        assert re.search(r'<form method="post" action="/billing/checkout">', text)
        assert "/billing/portal" not in text
        assert 'action="/auth/logout"' in text and "octo" in text

    async def test_a_pro_user_sees_the_portal_not_the_upgrade(self, db_session, db_session_factory):
        from datetime import UTC, datetime

        user = await seed_user(db_session)
        await seed_subscription(
            db_session, user, plan="pro", status="active", current_period_end=datetime(2027, 1, 2, 3, 4, tzinfo=UTC)
        )
        async with get(db_session_factory, cookies=session_cookie(user.id)) as c:
            text = (await c.get("/ui/account")).text
        assert ">pro<" in text and "2027-01-02 03:04:00 UTC" in text
        assert "/billing/portal" in text and "/billing/checkout" not in text

    async def test_a_pending_cancellation_is_shown_and_pro_is_kept(self, db_session, db_session_factory):
        from datetime import UTC, datetime

        user = await seed_user(db_session)
        await seed_subscription(
            db_session, user, plan="pro", status="active", cancel_at=datetime(2027, 2, 3, 4, 5, tzinfo=UTC)
        )
        async with get(db_session_factory, cookies=session_cookie(user.id)) as c:
            text = (await c.get("/ui/account")).text
        assert "Cancels on 2027-02-03 04:05:00 UTC. You keep Pro until then." in text
        assert ">pro<" in text and "/billing/portal" in text

    async def test_no_cancel_notice_for_an_active_subscription(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        await seed_subscription(db_session, user, plan="pro", status="active")
        async with get(db_session_factory, cookies=session_cookie(user.id)) as c:
            text = (await c.get("/ui/account")).text
        assert "Cancels on" not in text and "payment failed" not in text

    async def test_past_due_shows_a_payment_notice(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        await seed_subscription(db_session, user, plan="pro", status="past_due")
        async with get(db_session_factory, cookies=session_cookie(user.id)) as c:
            text = (await c.get("/ui/account")).text
        assert "payment failed" in text and "Manage billing" in text

    async def test_stripe_not_configured_hides_the_buttons(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        async with get(db_session_factory, billing=settings(stripe_secret_key=None), cookies=session_cookie(user.id)) as c:
            text = (await c.get("/ui/account")).text
        assert "/billing/checkout" not in text and "unavailable" in text


class TestNotices:
    async def test_success_is_not_treated_as_proof_of_payment(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        async with get(db_session_factory, cookies=session_cookie(user.id)) as c:
            text = (await c.get("/ui/account?checkout=success")).text
        assert "not confirmation of payment" in text and ">free<" in text

    async def test_cancelled_and_unknown(self, db_session_factory):
        async with get(db_session_factory) as c:
            assert "cancelled" in (await c.get("/ui/account?checkout=cancelled")).text
            assert 'class="panel notice"' not in (await c.get("/ui/account?checkout=<script>")).text


class TestHardening:
    async def test_csp_and_no_inline_script_or_style(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        async with get(db_session_factory, cookies=session_cookie(user.id)) as c:
            response = await c.get("/ui/account")
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert " style=" not in response.text and "<style" not in response.text
        assert not re.search(r"<script(?![^>]*\bsrc=)", response.text) and not re.search(r"\son\w+=", response.text)

    async def test_the_page_forms_work_with_the_browser_origin(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        fake = FakeStripe()
        async with get(db_session_factory, cookies=session_cookie(user.id), stripe=fake) as c:
            response = await c.post(
                "/billing/checkout", headers={"Origin": "http://test", "Accept": "text/html,application/xhtml+xml"}
            )
        assert response.status_code == 303 and response.headers["location"].startswith("https://checkout.stripe.test")
        assert fake.checkout_calls[0]["success_url"] == "http://test/ui/account?checkout=success"
        assert fake.checkout_calls[0]["cancel_url"] == "http://test/ui/account?checkout=cancelled"

    async def test_nav_link_on_every_page(self, db_session_factory):
        async with get(db_session_factory) as c:
            assert 'href="/ui/account"' in (await c.get("/ui/account")).text
