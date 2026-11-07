"""`POST /billing/checkout` and `/billing/portal`: what is sent to Stripe, and who may call."""

import re

import pytest
from api_billing_support import (
    SAME_ORIGIN,
    FakeStripe,
    build_app,
    client_for,
    seed_subscription,
    seed_user,
    session_cookie,
    settings,
)
from repolace_shared.db.models import Subscription
from sqlalchemy import select

pytestmark = [pytest.mark.anyio, pytest.mark.db]


class TestUnconfigured:
    @pytest.mark.parametrize("missing", ["stripe_secret_key", "stripe_webhook_secret", "stripe_price_id"])
    @pytest.mark.parametrize("path", ["/billing/checkout", "/billing/portal"])
    async def test_billing_routes_are_503_before_anything_else(self, db_session_factory, missing, path):
        app = build_app(db_session_factory, billing=settings(**{missing: None}))
        async with client_for(app) as client:
            assert (await client.post(path, headers=SAME_ORIGIN)).status_code == 503


class TestCheckout:
    async def test_it_requires_a_signed_in_user(self, db_session_factory):
        app = build_app(db_session_factory)
        async with client_for(app) as client:
            assert (await client.post("/billing/checkout", headers=SAME_ORIGIN)).status_code == 401

    async def test_a_forged_session_cookie_is_401(self, db_session_factory):
        app = build_app(db_session_factory)
        async with client_for(app, cookies={"repolace_session": "x.y"}) as client:
            assert (await client.post("/billing/checkout", headers=SAME_ORIGIN)).status_code == 401

    async def test_a_cross_site_post_is_refused(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        fake = FakeStripe()
        app = build_app(db_session_factory, stripe_client=fake)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            response = await client.post("/billing/checkout", headers={"Origin": "http://evil.example"})
            no_origin = await client.post("/billing/checkout")
        assert response.status_code == no_origin.status_code == 403
        assert fake.checkout_calls == [] and fake.customer_calls == []

    async def test_the_session_params(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        fake = FakeStripe()
        app = build_app(db_session_factory, stripe_client=fake)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            response = await client.post("/billing/checkout", headers=SAME_ORIGIN)
        assert response.status_code == 200 and response.json() == {"url": "https://checkout.stripe.test/c/pay_1"}

        (params,) = fake.checkout_calls
        assert "payment_method_types" not in params
        assert re.fullmatch(r"repolace-checkout-[a-z]{8}", params["integration_identifier"])
        assert params["mode"] == "subscription"
        assert params["line_items"] == [{"price": "price_fake", "quantity": 1}]
        assert params["client_reference_id"] == str(user.id)
        assert params["success_url"].startswith("http://test/") and params["cancel_url"].startswith("http://test/")
        row = (await db_session.execute(select(Subscription))).scalar_one()
        assert params["customer"] == row.stripe_customer_id == "cus_1"
        assert fake.customer_calls[0]["options"] == {"idempotency_key": f"repolace-customer-{user.id}"}

    async def test_the_integration_identifier_differs_per_call(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        fake = FakeStripe()
        app = build_app(db_session_factory, stripe_client=fake)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            await client.post("/billing/checkout", headers=SAME_ORIGIN)
            await client.post("/billing/checkout", headers=SAME_ORIGIN)
        first, second = fake.checkout_calls
        assert first["integration_identifier"] != second["integration_identifier"]
        assert len(fake.customer_calls) == 1  # the second call reuses the stored customer

    async def test_a_browser_form_post_gets_a_303_to_stripe(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        app = build_app(db_session_factory)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            response = await client.post("/billing/checkout", headers={**SAME_ORIGIN, "Accept": "text/html,*/*"})
        assert response.status_code == 303
        assert response.headers["location"] == "https://checkout.stripe.test/c/pay_1"

    async def test_a_paid_user_is_sent_to_the_portal_instead(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        await seed_subscription(db_session, user, plan="pro", status="active")
        fake = FakeStripe()
        app = build_app(db_session_factory, stripe_client=fake)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            response = await client.post("/billing/checkout", headers=SAME_ORIGIN)
        assert response.status_code == 409 and fake.checkout_calls == []


class TestPortal:
    async def test_without_a_subscription_row_it_is_404(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        app = build_app(db_session_factory)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            assert (await client.post("/billing/portal", headers=SAME_ORIGIN)).status_code == 404

    async def test_it_requires_sign_in(self, db_session_factory):
        app = build_app(db_session_factory)
        async with client_for(app) as client:
            assert (await client.post("/billing/portal", headers=SAME_ORIGIN)).status_code == 401

    async def test_it_opens_the_portal_for_the_stored_customer(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        await seed_subscription(db_session, user, stripe_customer_id="cus_stored")
        fake = FakeStripe()
        app = build_app(db_session_factory, stripe_client=fake)
        async with client_for(app, cookies=session_cookie(user.id)) as client:
            response = await client.post("/billing/portal", headers=SAME_ORIGIN)
        assert response.json() == {"url": "https://billing.stripe.test/p/session_1"}
        assert fake.portal_calls == [{"customer": "cus_stored", "return_url": "http://test/ui/repos"}]
