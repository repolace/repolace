"""Stripe: Checkout, the Customer Portal and the webhook that keeps `subscriptions` current.

Stripe is the source of truth and `subscriptions` is a cache of it: only a verified webhook changes
a plan. Nothing is trusted from the success/cancel redirect. The client is always an explicit
`stripe.StripeClient`; the global `stripe.api_key` is never set.
"""

import json
import secrets
import string
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import stripe
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from repolace_shared.db.models import ProcessedStripeEvent, Subscription, User
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from repolace_api.config import BillingSettings, get_billing_settings
from repolace_api.deps import current_user, get_db, require_same_origin, wants_html

router = APIRouter(prefix="/billing", tags=["billing"])
log = structlog.get_logger()

#: Stripe statuses that keep the paid plan. `past_due` stays paid while Stripe retries the card.
PRO_STATUSES = frozenset({"active", "trialing", "past_due"})
#: Stronger flags are never overwritten by weaker ones.
RISK_RANK = {"refunded": 1, "fraud_warning": 2, "dispute": 3}
MAX_WEBHOOK_BYTES = 1_000_000


@lru_cache(maxsize=4)
def _client_for(secret_key: str) -> stripe.StripeClient:
    return stripe.StripeClient(secret_key)


def get_stripe_client(settings: BillingSettings = Depends(get_billing_settings)) -> stripe.StripeClient:
    return _client_for(settings.stripe_secret_key or "")


def require_stripe_ready(settings: BillingSettings = Depends(get_billing_settings)) -> BillingSettings:
    if not settings.stripe_ready:
        raise HTTPException(status_code=503, detail="billing is not configured")
    return settings


def _integration_identifier() -> str:
    letters = "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
    return "repolace-checkout-" + letters


def _respond(request: Request, url: str) -> Response:
    if wants_html(request):
        return RedirectResponse(url, status_code=303)
    return Response(content=json.dumps({"url": url}), media_type="application/json")


async def _subscription_of(db: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    return await db.scalar(select(Subscription).where(Subscription.user_id == user_id))


@router.post("/checkout")
async def checkout(
    request: Request,
    settings: BillingSettings = Depends(require_stripe_ready),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    client: stripe.StripeClient = Depends(get_stripe_client),
) -> Response:
    require_same_origin(request)
    row = await _subscription_of(db, user.id)
    if row is not None and row.plan == "pro":
        raise HTTPException(status_code=409, detail="already subscribed; manage the plan in the billing portal")

    # The customer id is stored before any event can mention it, so every later event resolves to
    # this user through `stripe_customer_id` and never through event metadata.
    if row is None:
        # The idempotency key makes two concurrent first checkouts share one Stripe customer.
        params: dict[str, Any] = {"metadata": {"user_id": str(user.id)}}
        if user.email:
            params["email"] = user.email
        customer = client.v1.customers.create(params=params, options={"idempotency_key": f"repolace-customer-{user.id}"})
        await db.execute(
            insert(Subscription).values(user_id=user.id, stripe_customer_id=customer.id).on_conflict_do_nothing()
        )
        await db.commit()
        row = await _subscription_of(db, user.id)
        if row is None:  # pragma: no cover - the insert above or a concurrent one made it
            raise HTTPException(status_code=500, detail="could not record the billing customer")

    base = settings.api_base_url.rstrip("/")
    session = client.v1.checkout.sessions.create(
        params={
            "mode": "subscription",
            "line_items": [{"price": settings.stripe_price_id, "quantity": 1}],
            "client_reference_id": str(user.id),
            "customer": row.stripe_customer_id,
            "success_url": f"{base}/ui/repos?checkout=success",
            "cancel_url": f"{base}/ui/repos?checkout=cancelled",
            "integration_identifier": _integration_identifier(),
            # No payment_method_types: the methods come from the Dashboard.
        }
    )
    return _respond(request, session.url)


@router.post("/portal")
async def portal(
    request: Request,
    settings: BillingSettings = Depends(require_stripe_ready),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    client: stripe.StripeClient = Depends(get_stripe_client),
) -> Response:
    require_same_origin(request)
    row = await _subscription_of(db, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="no subscription to manage")
    session = client.v1.billing_portal.sessions.create(
        params={"customer": row.stripe_customer_id, "return_url": f"{settings.api_base_url.rstrip('/')}/ui/repos"}
    )
    return _respond(request, session.url)


# --- webhook ---------------------------------------------------------------------------------


@router.post("/webhook")
async def webhook(
    request: Request,
    settings: BillingSettings = Depends(require_stripe_ready),
    db: AsyncSession = Depends(get_db),
    client: stripe.StripeClient = Depends(get_stripe_client),
    stripe_signature: str | None = Header(default=None),
) -> dict[str, str]:
    """No session and no same-origin check: Stripe is the caller, and the signature over the raw body
    is the authentication. 2xx is returned only after the commit."""
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        stripe.Webhook.construct_event(body, stripe_signature, settings.stripe_webhook_secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="invalid signature") from None
    # The signature covers exactly these bytes, so parsing them is as trustworthy as the SDK's object.
    event = json.loads(body)
    event_type = event.get("type")
    handler = HANDLERS.get(event_type)
    if handler is None:
        return {"status": "ignored"}

    # The marker and the state change share one transaction: if the handler fails, the marker rolls
    # back with it and Stripe's retry runs the handler again. A concurrent duplicate blocks on the
    # primary key until this commits, then finds the row and does nothing.
    inserted = await db.execute(
        insert(ProcessedStripeEvent)
        .values(event_id=event["id"], event_type=event_type)
        .on_conflict_do_nothing()
        .returning(ProcessedStripeEvent.event_id)
    )
    if inserted.scalar_one_or_none() is None:
        await db.rollback()
        log.info("stripe.webhook.duplicate", event_type=event_type)
        return {"status": "duplicate"}

    await handler(db, event, client)
    await db.commit()
    log.info("stripe.webhook.handled", event_type=event_type)
    return {"status": "ok"}


async def _by_customer(db: AsyncSession, customer_id: Any) -> Subscription | None:
    if not isinstance(customer_id, str):
        return None
    # Row lock: two events for one customer apply one after the other.
    return await db.scalar(select(Subscription).where(Subscription.stripe_customer_id == customer_id).with_for_update())


def _period_end(subscription: dict) -> datetime | None:
    """Current API versions put the period on the subscription items, older ones on the root."""
    ts = subscription.get("current_period_end")
    if ts is None:
        items = (subscription.get("items") or {}).get("data") or []
        ts = items[0].get("current_period_end") if items else None
    return datetime.fromtimestamp(ts, UTC) if isinstance(ts, int | float) else None


async def _on_checkout_completed(db: AsyncSession, event: dict, client: stripe.StripeClient) -> None:
    obj = event["data"]["object"]
    if obj.get("mode") != "subscription":
        return
    try:
        user_id = uuid.UUID(str(obj.get("client_reference_id")))
    except ValueError:
        log.warning("stripe.checkout.no_user")
        return
    customer, subscription = obj.get("customer"), obj.get("subscription")
    if not isinstance(customer, str) or await db.get(User, user_id) is None:
        return
    row = await _subscription_of(db, user_id)
    if row is None:
        db.add(Subscription(user_id=user_id, stripe_customer_id=customer, stripe_subscription_id=subscription))
        return
    if row.stripe_customer_id != customer:
        # Never re-link a user to a different customer on the strength of one event.
        log.warning("stripe.checkout.customer_mismatch", user_id=str(user_id))
        return
    if isinstance(subscription, str) and (row.stripe_subscription_id is None or row.status == "canceled"):
        row.stripe_subscription_id = subscription


async def _on_subscription(db: AsyncSession, event: dict, client: stripe.StripeClient) -> None:
    """created / updated / deleted.

    Stripe does not guarantee delivery order, so a stale event must not overwrite newer state.
    Without storing the event time, the choice made here is state-based:
      * `canceled` is terminal for a subscription id, so any later non-delete event for it is ignored;
      * an event whose period end is older than the stored one (same subscription) is stale, because
        a renewal only moves the period forward;
      * an event for a different subscription id is ignored while the stored one is live, and accepted
        once the stored one is canceled or absent (a re-subscription). Checkout refuses a second
        subscription while one is live, so this only matters for out-of-order delivery.
    Equal period ends cannot be ordered (active then past_due within one period); Stripe's retry
    and the next event converge those.
    """
    sub = event["data"]["object"]
    row = await _by_customer(db, sub.get("customer"))
    if row is None:
        log.info("stripe.subscription.unknown_customer", event_type=event["type"])
        return
    sub_id = sub.get("id")
    is_delete = event["type"] == "customer.subscription.deleted"
    same = row.stripe_subscription_id in (None, sub_id)
    if not same and row.status not in ("canceled", "incomplete"):
        return
    if same and row.status == "canceled" and not is_delete:
        return
    period_end = _period_end(sub)
    if same and not is_delete and period_end and row.current_period_end and period_end < row.current_period_end:
        return

    status = "canceled" if is_delete else str(sub.get("status", "incomplete"))
    row.stripe_subscription_id = sub_id
    row.status = status
    row.plan = "pro" if status in PRO_STATUSES else "free"
    if period_end is not None:
        row.current_period_end = period_end


async def _on_invoice_paid(db: AsyncSession, event: dict, client: stripe.StripeClient) -> None:
    row = await _by_customer(db, event["data"]["object"].get("customer"))
    # A paid invoice ends a past_due spell. Anything else about the subscription arrives as its own
    # subscription event, so this does not guess a plan from an invoice.
    if row is not None and row.status == "past_due":
        row.status = "active"


async def _on_invoice_payment_failed(db: AsyncSession, event: dict, client: stripe.StripeClient) -> None:
    row = await _by_customer(db, event["data"]["object"].get("customer"))
    if row is not None and row.status in ("active", "trialing", "incomplete"):
        # The plan is left alone: a failed renewal keeps `pro` while Stripe retries (the
        # subscription event decides when it ends), a failed first payment was never `pro`.
        row.status = "past_due"


def _flag(row: Subscription, flag: str) -> None:
    if RISK_RANK[flag] > RISK_RANK.get(row.risk_flag or "", 0):
        row.risk_flag = flag


async def _flag_customer(db: AsyncSession, customer_id: Any, flag: str) -> None:
    row = await _by_customer(db, customer_id)
    if row is None:
        log.info("stripe.risk.unknown_customer", flag=flag)
        return
    _flag(row, flag)
    log.warning("stripe.risk.flagged", flag=flag, user_id=str(row.user_id))


async def _customer_of_charge(client: stripe.StripeClient, charge_id: Any) -> str | None:
    """Disputes and fraud warnings carry a charge id, not a customer: ask Stripe (needs the key's
    read permission on Charges). A Stripe error propagates, so the event is retried."""
    if not isinstance(charge_id, str):
        return None
    customer = getattr(client.v1.charges.retrieve(charge_id), "customer", None)
    return customer if isinstance(customer, str) else None


async def _on_dispute(db: AsyncSession, event: dict, client: stripe.StripeClient) -> None:
    await _flag_customer(db, await _customer_of_charge(client, event["data"]["object"].get("charge")), "dispute")


async def _on_refund(db: AsyncSession, event: dict, client: stripe.StripeClient) -> None:
    await _flag_customer(db, event["data"]["object"].get("customer"), "refunded")


async def _on_fraud_warning(db: AsyncSession, event: dict, client: stripe.StripeClient) -> None:
    await _flag_customer(db, await _customer_of_charge(client, event["data"]["object"].get("charge")), "fraud_warning")


HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "customer.subscription.created": _on_subscription,
    "customer.subscription.updated": _on_subscription,
    "customer.subscription.deleted": _on_subscription,
    "invoice.paid": _on_invoice_paid,
    "invoice.payment_failed": _on_invoice_payment_failed,
    "charge.dispute.created": _on_dispute,
    "charge.refunded": _on_refund,
    "radar.early_fraud_warning.created": _on_fraud_warning,
}
