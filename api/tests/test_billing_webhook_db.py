"""`POST /billing/webhook`: signature, replay, and what each handled event does to the row."""

import json
import time
from datetime import UTC, datetime

import pytest
from api_billing_support import (
    FakeStripe,
    build_app,
    client_for,
    event,
    post_event,
    seed_subscription,
    seed_user,
    settings,
    sign_payload,
)
from repolace_shared.db.models import ProcessedStripeEvent, Subscription
from sqlalchemy import func, select

pytestmark = [pytest.mark.anyio, pytest.mark.db]

PERIOD_END = 1_800_000_000
LATER = PERIOD_END + 30 * 86400


def sub_obj(status="active", customer="cus_1", sub_id="sub_1", period_end=PERIOD_END):
    return {"id": sub_id, "customer": customer, "status": status, "current_period_end": period_end}


async def reload(db_session, row) -> Subscription:
    """The one subscription row of the test, re-read (`row` is only there for readability)."""
    db_session.expire_all()
    return (await db_session.execute(select(Subscription))).scalar_one()


async def processed_count(db_session) -> int:
    return await db_session.scalar(select(func.count()).select_from(ProcessedStripeEvent))


@pytest.fixture
async def world(db_session, db_session_factory):
    user = await seed_user(db_session)
    row = await seed_subscription(db_session, user)
    stripe_fake = FakeStripe(charge_customer="cus_1")
    app = build_app(db_session_factory, stripe_client=stripe_fake)
    async with client_for(app) as client:
        yield client, user, row, stripe_fake


class TestAuthentication:
    async def test_a_bad_signature_is_400_and_changes_nothing(self, world, db_session):
        client, _, row, _ = world
        body = json.dumps(event("customer.subscription.updated", sub_obj())).encode()
        response = await client.post(
            "/billing/webhook", content=body, headers={"Stripe-Signature": sign_payload(body, "whsec_other")}
        )
        assert response.status_code == 400
        assert (await reload(db_session, row)).status == "incomplete"
        assert await processed_count(db_session) == 0

    async def test_a_missing_header_is_400(self, world):
        client, *_ = world
        response = await client.post("/billing/webhook", content=json.dumps(event("invoice.paid", {})).encode())
        assert response.status_code == 400

    async def test_a_tampered_body_is_400(self, world):
        client, *_ = world
        body = json.dumps(event("customer.subscription.updated", sub_obj())).encode()
        header = sign_payload(body)
        response = await client.post("/billing/webhook", content=body + b" ", headers={"Stripe-Signature": header})
        assert response.status_code == 400

    async def test_an_old_timestamp_is_400(self, world):
        client, *_ = world
        body = json.dumps(event("customer.subscription.updated", sub_obj())).encode()
        header = sign_payload(body, timestamp=int(time.time()) - 3600)
        assert (await client.post("/billing/webhook", content=body, headers={"Stripe-Signature": header})).status_code == 400

    async def test_unconfigured_is_503(self, db_session_factory):
        app = build_app(db_session_factory, billing=settings(stripe_webhook_secret=None))
        async with client_for(app) as client:
            body = b"{}"
            response = await client.post(
                "/billing/webhook", content=body, headers={"Stripe-Signature": sign_payload(body)}
            )
        assert response.status_code == 503

    async def test_the_host_allowlist_does_not_block_stripe(self, world):
        # post_event sends Host: stripe.example, which is not on the allowlist (["test"]).
        client, *_ = world
        assert (await post_event(client, event("invoice.paid", {"customer": "cus_x"}))).status_code == 200

    async def test_other_paths_still_need_an_allowed_host(self, world):
        client, *_ = world
        assert (await client.get("/auth/github/login", headers={"Host": "stripe.example"})).status_code == 400


class TestIdempotency:
    async def test_a_replayed_event_id_changes_nothing_the_second_time(self, world, db_session):
        client, _, row, _ = world
        first = event("customer.subscription.updated", sub_obj("active"), event_id="evt_same")
        assert (await post_event(client, first)).json() == {"status": "ok"}
        assert (await reload(db_session, row)).plan == "pro"

        # Same id, different content: a replay must not be applied, whatever it says.
        replay = event("customer.subscription.updated", sub_obj("canceled", period_end=LATER), event_id="evt_same")
        response = await post_event(client, replay)
        assert response.status_code == 200 and response.json() == {"status": "duplicate"}
        after = await reload(db_session, row)
        assert (after.status, after.plan) == ("active", "pro")
        assert await processed_count(db_session) == 1

    async def test_a_failing_handler_leaves_no_marker_so_the_retry_runs(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        await seed_subscription(db_session, user)

        class Boom(FakeStripe):
            def __init__(self):
                super().__init__()
                self.v1.charges.retrieve = self._boom

            def _boom(self, charge_id, **_):
                raise RuntimeError("stripe is down")

        app = build_app(db_session_factory, stripe_client=Boom())
        payload = event("charge.dispute.created", {"charge": "ch_1"}, event_id="evt_retry")
        async with client_for(app, ) as client:
            with pytest.raises(RuntimeError):
                await post_event(client, payload)
        assert await processed_count(db_session) == 0


class TestHandledEvents:
    async def test_checkout_completed_links_the_user_by_client_reference_id(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        app = build_app(db_session_factory)
        obj = {"mode": "subscription", "client_reference_id": str(user.id), "customer": "cus_new", "subscription": "sub_9"}
        async with client_for(app) as client:
            assert (await post_event(client, event("checkout.session.completed", obj))).status_code == 200
        row = (await db_session.execute(select(Subscription))).scalar_one()
        assert (row.user_id, row.stripe_customer_id, row.stripe_subscription_id) == (user.id, "cus_new", "sub_9")
        assert row.plan == "free"  # the plan comes from the subscription event, not from checkout

    async def test_checkout_completed_never_relinks_a_different_customer(self, world, db_session):
        client, user, row, _ = world
        obj = {"mode": "subscription", "client_reference_id": str(user.id), "customer": "cus_attacker", "subscription": "sub_x"}
        await post_event(client, event("checkout.session.completed", obj))
        after = await reload(db_session, row)
        assert (after.stripe_customer_id, after.stripe_subscription_id) == ("cus_1", None)

    async def test_checkout_completed_with_a_bogus_reference_is_a_noop(self, world, db_session):
        client, *_ = world
        obj = {"mode": "subscription", "client_reference_id": "not-a-uuid", "customer": "cus_z", "subscription": "sub_z"}
        assert (await post_event(client, event("checkout.session.completed", obj))).status_code == 200
        assert (await db_session.scalar(select(func.count()).select_from(Subscription))) == 1

    @pytest.mark.parametrize(
        "status,plan",
        [("active", "pro"), ("trialing", "pro"), ("past_due", "pro"), ("unpaid", "free"), ("canceled", "free"), ("incomplete", "free")],
    )
    @pytest.mark.parametrize("event_type", ["customer.subscription.created", "customer.subscription.updated"])
    async def test_subscription_status_decides_the_plan(self, world, db_session, event_type, status, plan):
        client, _, row, _ = world
        await post_event(client, event(event_type, sub_obj(status)))
        after = await reload(db_session, row)
        assert (after.status, after.plan, after.stripe_subscription_id) == (status, plan, "sub_1")
        assert after.current_period_end == datetime.fromtimestamp(PERIOD_END, UTC)

    async def test_the_period_end_is_read_from_the_items_in_newer_api_versions(self, world, db_session):
        client, _, row, _ = world
        obj = {"id": "sub_1", "customer": "cus_1", "status": "active", "items": {"data": [{"current_period_end": PERIOD_END}]}}
        await post_event(client, event("customer.subscription.updated", obj))
        assert (await reload(db_session, row)).current_period_end == datetime.fromtimestamp(PERIOD_END, UTC)

    async def test_subscription_deleted_cancels_and_drops_to_free(self, world, db_session):
        client, _, row, _ = world
        await post_event(client, event("customer.subscription.updated", sub_obj("active")))
        await post_event(client, event("customer.subscription.deleted", sub_obj("canceled")))
        after = await reload(db_session, row)
        assert (after.status, after.plan) == ("canceled", "free")

    async def test_an_event_for_an_unknown_customer_is_a_200_noop(self, world, db_session):
        client, _, row, _ = world
        response = await post_event(client, event("customer.subscription.updated", sub_obj(customer="cus_nobody")))
        assert response.status_code == 200
        assert (await reload(db_session, row)).status == "incomplete"

    async def test_invoice_paid_recovers_a_past_due_subscription(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        row = await seed_subscription(db_session, user, status="past_due", plan="pro", stripe_subscription_id="sub_1")
        app = build_app(db_session_factory)
        async with client_for(app) as client:
            await post_event(client, event("invoice.paid", {"customer": "cus_1"}))
        assert (await reload(db_session, row)).status == "active"

    async def test_invoice_payment_failed_marks_past_due_and_keeps_the_paid_plan(self, db_session, db_session_factory):
        user = await seed_user(db_session)
        row = await seed_subscription(db_session, user, status="active", plan="pro", stripe_subscription_id="sub_1")
        app = build_app(db_session_factory)
        async with client_for(app) as client:
            await post_event(client, event("invoice.payment_failed", {"customer": "cus_1"}))
        after = await reload(db_session, row)
        assert (after.status, after.plan) == ("past_due", "pro")

    async def test_a_dispute_flags_through_the_stored_customer_not_metadata(self, world, db_session):
        client, _, row, stripe_fake = world
        obj = {"charge": "ch_1", "metadata": {"customer": "cus_attacker", "user_id": "whatever"}}
        await post_event(client, event("charge.dispute.created", obj))
        assert stripe_fake.charge_calls == ["ch_1"]
        assert (await reload(db_session, row)).risk_flag == "dispute"

    async def test_a_refund_flags_refunded(self, world, db_session):
        client, _, row, _ = world
        await post_event(client, event("charge.refunded", {"customer": "cus_1"}))
        assert (await reload(db_session, row)).risk_flag == "refunded"

    async def test_an_early_fraud_warning_flags_and_a_weaker_flag_never_overwrites_it(self, world, db_session):
        client, _, row, _ = world
        await post_event(client, event("radar.early_fraud_warning.created", {"charge": "ch_2"}))
        assert (await reload(db_session, row)).risk_flag == "fraud_warning"
        await post_event(client, event("charge.dispute.created", {"charge": "ch_3"}))
        assert (await reload(db_session, row)).risk_flag == "dispute"
        await post_event(client, event("charge.refunded", {"customer": "cus_1"}))
        assert (await reload(db_session, row)).risk_flag == "dispute"

    async def test_an_unknown_event_type_is_200_and_not_recorded(self, world, db_session):
        client, *_ = world
        response = await post_event(client, event("customer.created", {"id": "cus_1"}))
        assert response.status_code == 200 and response.json() == {"status": "ignored"}
        assert await processed_count(db_session) == 0


class TestOrdering:
    async def test_an_older_period_end_does_not_overwrite_a_newer_one(self, world, db_session):
        client, _, row, _ = world
        await post_event(client, event("customer.subscription.updated", sub_obj("active", period_end=LATER)))
        await post_event(client, event("customer.subscription.updated", sub_obj("past_due", period_end=PERIOD_END)))
        after = await reload(db_session, row)
        assert (after.status, after.current_period_end) == ("active", datetime.fromtimestamp(LATER, UTC))

    async def test_a_late_update_cannot_resurrect_a_canceled_subscription(self, world, db_session):
        client, _, row, _ = world
        await post_event(client, event("customer.subscription.deleted", sub_obj("canceled")))
        await post_event(client, event("customer.subscription.updated", sub_obj("active", period_end=LATER)))
        after = await reload(db_session, row)
        assert (after.status, after.plan) == ("canceled", "free")

    async def test_an_event_for_another_subscription_is_ignored_while_one_is_live(self, world, db_session):
        client, _, row, _ = world
        await post_event(client, event("customer.subscription.updated", sub_obj("active")))
        await post_event(client, event("customer.subscription.deleted", sub_obj("canceled", sub_id="sub_old")))
        after = await reload(db_session, row)
        assert (after.stripe_subscription_id, after.status) == ("sub_1", "active")

    async def test_a_resubscription_after_cancellation_is_accepted(self, world, db_session):
        client, _, row, _ = world
        await post_event(client, event("customer.subscription.deleted", sub_obj("canceled")))
        await post_event(client, event("customer.subscription.created", sub_obj("active", sub_id="sub_2", period_end=LATER)))
        after = await reload(db_session, row)
        assert (after.stripe_subscription_id, after.status, after.plan) == ("sub_2", "active", "pro")
