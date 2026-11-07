"""Helpers for the billing and sign-in tests. Stripe and GitHub are never called: Stripe through a
recording fake client, GitHub through an `httpx.MockTransport`. Unique basename on purpose (see
`api_support`)."""

import hashlib
import hmac
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from repolace_api.config import BillingSettings, get_billing_settings
from repolace_api.deps import SESSION_COOKIE
from repolace_api.routes import auth as auth_routes
from repolace_api.routes.auth import router as auth_router
from repolace_api.routes.billing import get_stripe_client
from repolace_api.routes.billing import router as billing_router
from repolace_api.sessions import sign
from repolace_api.ui import mount_ui
from repolace_shared.db.models import Subscription, User

SESSION_SECRET = "s" * 40
WEBHOOK_SECRET = "whsec_test_secret_for_tests_only"
SAME_ORIGIN = {"Origin": "http://test"}


def settings(**overrides: Any) -> BillingSettings:
    """Every field explicit, so neither the environment nor a `.env` file leaks into a test."""
    fields: dict[str, Any] = {
        "stripe_secret_key": "sk_test_fake",
        "stripe_webhook_secret": WEBHOOK_SECRET,
        "stripe_price_id": "price_fake",
        "session_secret": SESSION_SECRET,
        "github_oauth_client_id": "client-id",
        "github_oauth_client_secret": "client-secret",
        "api_base_url": "http://test",
    }
    return BillingSettings(_env_file=None, **{**fields, **overrides})


class FakeStripe:
    """The few `StripeClient` calls the routes make, recording what they were given."""

    def __init__(self, charge_customer: str | None = "cus_1"):
        self.customer_calls: list[dict] = []
        self.checkout_calls: list[dict] = []
        self.portal_calls: list[dict] = []
        self.charge_calls: list[str] = []
        outer = self

        class Customers:
            def create(self, params=None, options=None):
                outer.customer_calls.append({"params": params, "options": options})
                return SimpleNamespace(id=f"cus_{len(outer.customer_calls)}")

        class CheckoutSessions:
            def create(self, params=None, options=None):
                outer.checkout_calls.append(params)
                return SimpleNamespace(url="https://checkout.stripe.test/c/pay_1")

        class PortalSessions:
            def create(self, params=None, options=None):
                outer.portal_calls.append(params)
                return SimpleNamespace(url="https://billing.stripe.test/p/session_1")

        class Charges:
            def retrieve(self, charge_id, params=None, options=None):
                outer.charge_calls.append(charge_id)
                return SimpleNamespace(id=charge_id, customer=charge_customer)

        self.v1 = SimpleNamespace(
            customers=Customers(),
            checkout=SimpleNamespace(sessions=CheckoutSessions()),
            billing_portal=SimpleNamespace(sessions=PortalSessions()),
            charges=Charges(),
        )


def build_app(session_factory, *, billing: BillingSettings | None = None, stripe_client: Any = None) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(billing_router)
    mount_ui(app, allowed_hosts=["test"])
    app.state.session_factory = session_factory
    app.dependency_overrides[get_billing_settings] = lambda: billing or settings()
    app.dependency_overrides[get_stripe_client] = lambda: stripe_client or FakeStripe()
    return app


def client_for(app: FastAPI, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", **kwargs)


def override_github(app: FastAPI, handler) -> None:
    """Replace the OAuth HTTP client with one answering from `handler(request) -> httpx.Response`."""

    async def fake():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield client

    app.dependency_overrides[auth_routes.get_oauth_http] = fake


def session_cookie(user_id: uuid.UUID) -> dict[str, str]:
    return {SESSION_COOKIE: sign("session", {"uid": str(user_id)}, SESSION_SECRET, max_age=3600)}


async def seed_user(session, github_user_id: int = 1001, login: str = "octo") -> User:
    user = User(github_user_id=github_user_id, github_login=login)
    session.add(user)
    await session.commit()
    return user


async def seed_subscription(session, user: User, **overrides: Any) -> Subscription:
    fields: dict[str, Any] = {"user_id": user.id, "stripe_customer_id": "cus_1"}
    row = Subscription(**{**fields, **overrides})
    session.add(row)
    await session.commit()
    return row


def event(event_type: str, obj: dict, event_id: str | None = None) -> dict:
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "object": "event",
        "type": event_type,
        "created": int(time.time()),
        "data": {"object": obj},
    }


def sign_payload(body: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """A real `Stripe-Signature` header, so the SDK's own verification is what the tests exercise."""
    t = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


async def post_event(client: httpx.AsyncClient, payload: dict, *, secret: str = WEBHOOK_SECRET) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await client.post(
        "/billing/webhook", content=body, headers={"Stripe-Signature": sign_payload(body, secret), "Host": "stripe.example"}
    )
