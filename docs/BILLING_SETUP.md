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
stripe listen --forward-to localhost:8000/billing/webhook --events checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.paid,invoice.payment_failed,charge.dispute.created,charge.refunded,radar.early_fraud_warning.created
```

On Stripe CLI 1.55.8 the bare command (no `--events`) exited with "must specify events to forward", so the event list is given here. `stripe listen --print-secret` prints the signing secret without starting the listener. The listener prints a signing secret (`whsec_...`): put it in `STRIPE_WEBHOOK_SECRET` and restart the API. **The listener does not outlive the CLI's login:** on 2026-11-15 it exited overnight with a 401 ("expired OAuth token") and Stripe events stopped reaching the API with nothing else failing. Check it is still running before you rely on a webhook, and after `stripe login` start it again; the signing secret stayed the same. **A bare `stripe listen` (no `--forward-to`) prints `Ready!` and receives events but forwards none**, so the API never sees them; on 2026-11-16 that looked like a working listener. Delivery to the CLI is not guaranteed either: on 2026-11-16 two of four events from one Checkout (`customer.subscription.created`, `checkout.session.completed`) stayed `pending_webhooks: 1` until `stripe events resend <evt_id>` delivered them. `stripe subscriptions cancel <sub_id>` waits at a confirmation prompt; add `--confirm` when scripting it. The Stripe variables belong in `infra/api.env`: the `api` container never reads the repository `.env` (which `infra/.env` links to). The endpoint verifies the signature over the raw body and skips event ids it has seen. Handled events: `checkout.session.completed`, `customer.subscription.created|updated|deleted`, `invoice.paid`, `invoice.payment_failed`, `charge.dispute.created`, `charge.refunded`, `radar.early_fraud_warning.created`. The plan only ever changes from a verified event, never from the success redirect.

## 3. GitHub sign-in

In the GitHub App settings: leave **Request user authorization (OAuth) during installation** unticked (sign-in does not need it, and an install redirect would hit the callback without a state cookie), add the callback URL to `http://localhost:8000/auth/github/callback`, generate a client secret, and put the client id and secret in `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`. Set `SESSION_SECRET`. Set the App's **Setup URL** to `http://localhost:8000/github/callback`, so that after an install the browser goes back through sign-in and the new repositories become visible. Sign-in is not only for billing: every repo and task route requires it (see `.claude/rules/api-and-ui.md`, "Access"), and the API refuses to start without it unless `DEV_NO_AUTH=1` is set on a loopback-only install.

## 4. Try it

1. `docker compose` stack up on the rootless daemon, `alembic upgrade head` against the dev database (migration 0019 adds `tasks.user_id`), API running.
2. Open `http://localhost:8000/ui/account`, **Sign in with GitHub**.
3. **Upgrade**, pay with the test card `4242 4242 4242 4242`, any future expiry, any CVC.
4. Back on `/ui/account?checkout=success`: the banner is not proof of payment. The plan flips to `pro` when `stripe listen` delivers the events; reload.
5. **Manage billing** opens the Customer Portal; cancelling there stores `cancel_at` (the account page shows "Cancels on <date>" and keeps Pro until then) and the plan drops to free when Stripe sends `customer.subscription.deleted` at the period end; resuming in the portal clears it.

## Allowance

When sign-in is configured **and** someone is signed in, the enqueue path (JSON `POST /repos/{id}/tasks` and the UI form) records `tasks.user_id` and refuses with `402` once the user has started `2` (free) or `60` (pro) non-benchmark tasks this calendar month (UTC). Failed tasks count. With sign-in unset, or nobody signed in, there is no owner and no limit. The numbers are preview numbers in `api/repolace_api/billing_limits.py`.

## Not live yet

- Stripe calls use the SDK's `*_async` methods, which go through httpx: the `api` package depends on `stripe[async]`, so the requirement is stated where it is used rather than resting on `repolace-shared` also depending on httpx. Before 2026-11-17 they were blocking calls inside async handlers. The test fake has only the async methods, so a blocking call fails the tests.
- **Stripe Tax is not enabled** (`automatic_tax` is off); it needs an active tax registration first.
- The **webhook must move to AWS** (a stable public HTTPS endpoint) before public use; `stripe listen` on a laptop is not reliable. Its path is exempt from the Host allowlist (`/billing/webhook`), the rest of the API and UI are not. Never add a public hostname to `UI_ALLOWED_HOSTS`.
- Sign-in now covers every repo and task route (since 2026-11-17), with access per repository from GitHub's permissions; the API is still not reviewed for public exposure.
- `/pricing` on the public site still says billing is not open; it is untouched.
- **Run against the real sandbox and GitHub on 2026-11-14, in part.** On the compose stack: GitHub sign-in (the code exchange and the session cookie), a Customer Portal session opening in Chrome, and one signed webhook (`stripe trigger charge.refunded`, answered 200 and logged `stripe.webhook.handled`). The `subscriptions` row from an earlier sandbox Checkout and portal cancellation (last updated 2026-11-07) rendered correctly on `/ui/account`; that earlier run is known only from the stored row. **A subscription change by webhook, 2026-11-15:** the pending cancellation was cleared through the Stripe API (`cancel_at` emptied) and then restored to the same timestamp with `proration_behavior=none`. Each change arrived as `customer.subscription.updated`, was answered 200 and logged `stripe.webhook.handled`, moved `subscriptions.cancel_at` and `updated_at`, and showed on `/ui/account` (the "Cancels on" notice went and came back). **A new Checkout and a cancellation, 2026-11-16 (UTC):** the earlier subscription's `customer.subscription.deleted` was handled at 11:37 (row to `free`/`canceled`). At 21:08 Upgrade on `/ui/account` and the 4242 test card produced `customer.subscription.created`, `invoice.paid` and `checkout.session.completed`, each answered 200 and logged `stripe.webhook.handled`; the row went to `pro`/`active` and the page showed 1 of 60. Cancelling it with `stripe subscriptions cancel` sent `customer.subscription.deleted` (200) and the row went back to `free`/`canceled`. That run used the full `sk_test_` key. **Repeated with the restricted key at 21:16:** the Checkout session, the portal session and every webhook worked, and the second cancellation reached the row. **It also found a defect:** this time `checkout.session.completed` was handled before `customer.subscription.created` (after a resend, see section 2), and the row stayed `free`/`canceled` while Stripe said `active`. After a cancellation, the Checkout handler links the new subscription id but keeps the status `canceled` (`routes/billing.py`, `_on_checkout_completed`), and `_on_subscription` then drops every non-delete event for that id as stale. In the first run the two events came in the other order. **Fixed in code since:** on a re-subscription (the row is `canceled`) the Checkout handler reads the new subscription from Stripe (Subscriptions: read) and stores its own status, period and `cancel_at` (under the row lock, and only if the subscription belongs to the session's customer), so either order ends `pro`/`active` when `created` carries a paid status, as it did with the 4242 card. Tests cover both orders, a late event for the old subscription, and a Stripe error, which leaves the event to be retried. **The fix was run against the sandbox at 23:01 UTC the same day, forcing the failing order.** The listener left out `customer.subscription.created`, so `checkout.session.completed` arrived alone. The handler fetched the new subscription (a 200 with the restricted key) and the row went to `pro`/`active`. A listener with the full list then delivered `created`, which left it unchanged, and a cancellation took it back to `free`/`canceled`. **Two further points from the review of that fix, both fixed since (2026-11-17, tests only, not run on the sandbox):**
  - A `created` carrying `incomplete` (a 3DS card) that is handled after the stored `active` has the same period end. It used to overwrite the row with `incomplete`. Stripe never moves a subscription back to `incomplete`, so such an event for the stored id is now ignored once the row holds any other status. A 3DS card is still untested on the sandbox.
  - A row at `incomplete_expired` or `unpaid` could start a new Checkout whose events were then dropped in either order. Now `incomplete_expired` counts as ended, like `canceled`: it can take a new subscription id, and the Checkout handler fetches the new subscription. Checkout refuses `unpaid` and `paused` with 409, because that subscription still exists and a second Checkout would make a second one. The account page shows such a row a notice and Manage billing instead of Upgrade.
- **Known gaps in the webhook's ordering, from the review of the 2026-11-17 fixes; none is seen in a run, all are recorded here instead of fixed:**
  - **An immediate pause can be dropped.** Pausing sets the period end to the time of the pause, so `updated(paused)` looks older than the stored period and the period guard ignores it; the row keeps `pro`. A pause scheduled in the portal (at the period end) and a resume are accepted. Exempting `paused` from the guard would let a stale pause override a later resume, so it was not done.
  - **A second Checkout while a first subscription is still `incomplete`** (allowed, because `incomplete` is also the new row's default) makes a second subscription. If the first is then paid too, its events are dropped and the customer is billed twice.
  - **After a subscription ends, a late event for an older one is accepted** and can set a paid status that nothing corrects. Checking such an event with Stripe, as the Checkout handler does, would close it.
  - **The Checkout handler's Stripe lookup holds the row lock** for as long as the call takes (the SDK's default timeout is 80 s per attempt). Cookie flags over HTTPS are still tested only with mocks.
- **The buttons did nothing in Chrome until 2026-11-14.** The UI's `form-action 'self'` blocked the 303 from `/billing/checkout` and `/billing/portal` to Stripe (Chrome applies `form-action` to a form's redirect; the mocked tests never follow it). The policy now names Stripe's two hosted pages; see `.claude/rules/api-and-ui.md`.
- The key used locally until 2026-11-16 was a full test secret key (`sk_test_`). Since then `infra/api.env` holds the restricted key from step 1, and the 21:16 run above (Checkout session, portal session, webhooks) passed with it. Not exercised with it yet: the charge lookup for disputes, refunds and fraud warnings (Charges: read).

## Sandbox objects created for development

In the Stripe sandbox "repolace sandbox" (test mode): Product `prod_VaMUVUVW3NZBqr` ("repolace Pro", 60 tasks a month) with Price `price_1UZBm22V3cpV9h6vI4Wpj1Ve` ($99 a month). Put the price id in `STRIPE_PRICE_ID`. These are test-mode objects; live mode needs its own.
