# Billing setup (Stripe sandbox, local only)

Monthly plans through Stripe Checkout, with GitHub sign-in. **Not live**: this runs on the laptop against a Stripe sandbox and nothing here is public. Never use a live key from a laptop.

The code reads these variables (all optional; unset, `/auth/*` and `/billing/*` answer 503 and the rest of the API behaves as before). They live in `infra/api.env`, which is gitignored; `infra/api.env.example` lists them.

| Variable | Value |
|---|---|
| `STRIPE_SECRET_KEY` | restricted key `rk_test_...` (below) |
| `STRIPE_WEBHOOK_SECRET` | `whsec_...` printed by `stripe listen` |
| `STRIPE_PRICE_ID` | `price_...` of the monthly plan |
| `SESSION_SECRET` | 32 or more random characters, e.g. `openssl rand -base64 48` |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | from the GitHub App |
| `API_BASE_URL` | `http://localhost:8000` |

Never paste a key into chat, a commit or a log.

## 1. Stripe sandbox

1. In the Stripe Dashboard, create a **sandbox** (not the shared test mode).
2. Create a **Product** ("repolace Pro") with one recurring monthly **Price**. Copy the `price_...` id into `STRIPE_PRICE_ID`.
3. Create a **restricted key** (Developers, API keys, Create restricted key) with the minimum permissions:
   - Checkout Sessions: write
   - Customers: write
   - Customer portal (Billing portal sessions): write
   - Subscriptions: read
   - Charges: read (disputes and fraud warnings carry a charge id, resolved to a customer)
   Put it in `STRIPE_SECRET_KEY`.
4. Settings, Billing, Customer portal: activate it, allow cancelling the subscription.

## 2. Webhook (Stripe CLI)

```
stripe listen --forward-to localhost:8000/billing/webhook
```

It prints a signing secret (`whsec_...`): put it in `STRIPE_WEBHOOK_SECRET` and restart the API. The endpoint verifies the signature over the raw body and skips event ids it has seen. Handled events: `checkout.session.completed`, `customer.subscription.created|updated|deleted`, `invoice.paid`, `invoice.payment_failed`, `charge.dispute.created`, `charge.refunded`, `radar.early_fraud_warning.created`. The plan only ever changes from a verified event, never from the success redirect.

## 3. GitHub sign-in

In the GitHub App settings: leave **Request user authorization (OAuth) during installation** unticked (sign-in does not need it, and an install redirect would hit the callback without a state cookie), add the callback URL to `http://localhost:8000/auth/github/callback`, generate a client secret, and put the client id and secret in `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`. Set `SESSION_SECRET`.

## 4. Try it

1. `docker compose` stack up on the rootless daemon, `alembic upgrade head` against the dev database (migration 0019 adds `tasks.user_id`), API running.
2. Open `http://localhost:8000/ui/account`, **Sign in with GitHub**.
3. **Upgrade**, pay with the test card `4242 4242 4242 4242`, any future expiry, any CVC.
4. Back on `/ui/account?checkout=success`: the banner is not proof of payment. The plan flips to `pro` when `stripe listen` delivers the events; reload.
5. **Manage billing** opens the Customer Portal; cancelling there flips the row after the next event.

## Allowance

When sign-in is configured **and** someone is signed in, the enqueue path (JSON `POST /repos/{id}/tasks` and the UI form) records `tasks.user_id` and refuses with `402` once the user has started `2` (free) or `60` (pro) non-benchmark tasks this calendar month (UTC). Failed tasks count. With sign-in unset, or nobody signed in, there is no owner and no limit. The numbers are preview numbers in `api/repolace_api/billing_limits.py`.

## Not live yet

- Stripe calls (`client.v1...`) are **synchronous** and run inside async handlers, so a slow Stripe call blocks the event loop. Fine for one local user, to be moved to a threadpool or the async client before real traffic.
- **Stripe Tax is not enabled** (`automatic_tax` is off); it needs an active tax registration first.
- The **webhook must move to AWS** (a stable public HTTPS endpoint) before public use; `stripe listen` on a laptop is not reliable. Its path is exempt from the Host allowlist (`/billing/webhook`), the rest of the API and UI are not. Never add a public hostname to `UI_ALLOWED_HOSTS`.
- The JSON API and the rest of `/ui` are still unauthenticated; sign-in covers only `/ui/account`, `/billing/*` and the allowance.
- `/pricing` on the public site still says billing is not open; it is untouched.
- The OAuth callback, cookie flags over HTTPS, and the Stripe flow were tested with mocks, not against real Stripe or GitHub (see the status in `CLAUDE.md`).

## Sandbox objects created for development

In the Stripe sandbox "repolace sandbox" (test mode): Product `prod_VaMUVUVW3NZBqr` ("repolace Pro", 60 tasks a month) with Price `price_1UZBm22V3cpV9h6vI4Wpj1Ve` ($99 a month). Put the price id in `STRIPE_PRICE_ID`. These are test-mode objects; live mode needs its own.
