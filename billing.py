"""BTC Oracle Pro — Stripe Subscriptions webhook (added 2026-11-17).

Cheli picked Stripe Subscriptions as the payment rail. This module is
the ONLY Stripe touchpoint in the service:

- It NEVER takes a card, creates a product or a price, or calls the
  Stripe API. Stripe hosts checkout (a later step, after the price is
  set) and sends subscription events here; the webhook flips the VIP
  flag from them.
- Signature verification is stdlib HMAC only — no Stripe SDK, no new
  dependency. The signing secret comes from the STRIPE_WEBHOOK_SECRET
  Render env var. If it is not set, the endpoint answers 503 and
  accepts nothing.
- VIP stays LOCKED until the 100-window validation passes (see
  auth.validation_unlocked): a subscription that becomes active while
  locked is recorded (subscription_status = 'active') but grants no
  VIP. The unlock endpoint grants those subscribers when the gate opens.

Events handled (everything else -> 200, ignored, logged):
  customer.subscription.created / .updated, status active
      -> subscription_status='active'; VIP only if unlocked.
  customer.subscription.updated, status past_due
      -> subscription_status='past_due'; VIP off.
  customer.subscription.updated / .deleted, status canceled
      -> subscription_status='canceled'; VIP off.
  customer.subscription.updated, any other status (trialing,
  incomplete, unpaid, paused) -> status recorded; VIP off.
  Trialing does NOT grant VIP: a trial is not a paid subscription.

Cancellation policy (Cheli, 2026-11-17): easy self-serve cancellation,
no refunds. A member cancels in the Stripe Customer Portal
(POST /billing/portal below — the portal session URL, opened in a
couple of clicks, no email to us). Cancellation is cancel-at-period-
end: the member keeps VIP until the end of the period they paid for,
and the webhook flips the flag off at period end (a subscription with
cancel_at_period_end set is still status 'active' until then, so the
gate keeps letting it through). There is NO refund flow anywhere — no
refund button, no refund endpoint, no automatic refunds. The no-refund
+ cancel-anytime terms are shown in the app's account panel and must
be shown on the subscribe screen before payment (see the design doc).
The portal's own cancellation mode ("at period end") is a Stripe
dashboard setting — see the design doc's open items.
"""

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, HTTPException, Request

import auth

router = APIRouter()

SIGNATURE_TOLERANCE_SECS = 300  # Stripe's own tolerance is 5 minutes
HANDLED_SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}


def verify_stripe_signature(payload, sig_header, secret, now=None):
    """Stripe's webhook signature check, stdlib only.

    Header: t=<unix timestamp>,v1=<hex hmac>[,v1=<hex hmac>...].
    Signed payload: "<t>.<raw body>". Accepts if ANY v1 value matches
    and the timestamp is within tolerance. Returns False on any
    malformed input — a bad header is never an exception.
    """
    if not payload or not sig_header or not secret:
        return False
    try:
        parts = {}
        for item in sig_header.split(","):
            key, _, value = item.strip().partition("=")
            if key and value:
                parts.setdefault(key, []).append(value)
        timestamp = int(parts["t"][0])
        signatures = parts.get("v1", [])
        if not signatures:
            return False
        check_time = time.time() if now is None else now
        if abs(check_time - timestamp) > SIGNATURE_TOLERANCE_SECS:
            return False
        signed = f"{timestamp}.".encode("utf-8") + payload
        expected = hmac.new(
            secret.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()
        return any(
            hmac.compare_digest(expected, sig) for sig in signatures
        )
    except (ValueError, IndexError, KeyError):
        return False


def _record_event(conn, event_id, event_type, handled):
    conn.execute(
        "INSERT OR IGNORE INTO stripe_events "
        "(event_id, type, received_at, handled) VALUES (?, ?, ?, ?)",
        (event_id, event_type, time.time(), handled),
    )
    conn.commit()


def _event_seen(conn, event_id):
    row = conn.execute(
        "SELECT event_id FROM stripe_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return row is not None


def handle_stripe_event(conn, event):
    """Apply one Stripe event. Returns the handled outcome:
    'granted' | 'revoked' | 'locked' | 'recorded' | 'ignored' |
    'no_user' | 'duplicate'. Pure DB logic — no HTTP, no network —
    so the unit tests drive it directly. Raises ValueError on a
    malformed event (missing id/type)."""
    event_id = event.get("id")
    event_type = event.get("type")
    if not event_id or not event_type:
        raise ValueError("Stripe event is missing id or type")
    if _event_seen(conn, event_id):
        return "duplicate"
    if event_type not in HANDLED_SUBSCRIPTION_EVENTS:
        _record_event(conn, event_id, event_type, "ignored")
        return "ignored"

    obj = (event.get("data") or {}).get("object") or {}
    customer_id = obj.get("customer")
    status = obj.get("status")
    user = auth.get_user_by_stripe_customer_id(conn, customer_id)
    if user is None:
        _record_event(conn, event_id, event_type, "no_user")
        return "no_user"

    # The status AND the period-end state are always recorded, even
    # when VIP does not change: the status is the second layer of the
    # gate (see auth.effective_vip), and the period-end fields are what
    # the frontend shows ("VIP until <date> — cancels then"). A
    # subscription with cancel_at_period_end set is still 'active'
    # until the period ends — the member keeps VIP until then, and the
    # flag flips off at period end when Stripe sends the deleted event.
    cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    current_period_end = obj.get("current_period_end")
    try:
        current_period_end = (
            float(current_period_end)
            if current_period_end is not None else None
        )
    except (TypeError, ValueError):
        current_period_end = None
    if status:
        auth.set_subscription_details(
            conn, user["id"], status, cancel_at_period_end, current_period_end
        )

    if status == "active":
        if auth.validation_unlocked(conn):
            auth.set_vip(conn, user["id"], True, "subscription")
            _record_event(conn, event_id, event_type, "granted")
            return "granted"
        _record_event(conn, event_id, event_type, "locked")
        return "locked"
    if status in ("past_due", "canceled"):
        auth.set_vip(conn, user["id"], False, None)
        _record_event(conn, event_id, event_type, "revoked")
        return "revoked"
    # trialing / incomplete / unpaid / paused / deleted-without-status:
    # recorded, VIP off (a trial is not a paid subscription).
    auth.set_vip(conn, user["id"], False, None)
    _record_event(conn, event_id, event_type, "recorded")
    return "recorded"


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe posts subscription events here. Raw body + signature
    header only — the JSON is parsed AFTER verification, so a forged
    body can never reach the handler."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook is not configured on this service",
        )
    raw = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    if not verify_stripe_signature(raw, sig_header, secret):
        raise HTTPException(
            status_code=400, detail="Invalid Stripe signature"
        )
    try:
        event = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    conn = auth.get_db()
    try:
        handled = handle_stripe_event(conn, event)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()
    return {"received": True, "handled": handled}


# ------------------------------------------------------------ customer portal
# Self-serve cancellation (Cheli's policy, 2026-11-17). The portal is
# Stripe-hosted: this endpoint creates a portal session for the
# signed-in member's Stripe customer and returns its URL. The member
# cancels there in a couple of clicks — no email to us. Cancellation
# is at period end (a dashboard setting in Stripe, and the webhook
# behavior above); there is no refund flow in the portal or anywhere
# in this service.
STRIPE_API_BASE = "https://api.stripe.com/v1"


def create_portal_session(secret_key, customer_id, return_url, urlopen=None):
    """Create a Stripe Customer Portal session. Returns its URL.

    POSTs to the Stripe API with the secret key as Basic-auth username.
    `urlopen` is injectable so tests can stub the network. Raises
    ValueError when Stripe refuses (bad key, unknown customer, portal
    not enabled) and RuntimeError on a transport failure."""
    import urllib.error
    import urllib.parse
    import urllib.request

    opener = urlopen or urllib.request.urlopen
    body = urllib.parse.urlencode(
        {"customer": customer_id, "return_url": return_url}
    ).encode("utf-8")
    req = urllib.request.Request(
        STRIPE_API_BASE + "/billing_portal/sessions",
        data=body,
        method="POST",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{secret_key}:".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with opener(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = (
                json.loads(exc.read().decode("utf-8"))
                .get("error", {})
                .get("message", "")
            )
        except Exception:
            detail = ""
        raise ValueError(
            f"Stripe refused the portal session: {detail or exc}"
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a 502
        raise RuntimeError(f"Could not reach Stripe: {exc}")
    url = data.get("url")
    if not url:
        raise ValueError("Stripe returned no portal URL")
    return url


@router.post("/billing/portal")
def billing_portal(request: Request):
    """Open the Stripe Customer Portal for the signed-in member."""
    user = auth.require_user(request)
    auth.check_rate_limit("portal_user", str(user["id"]))
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret_key:
        raise HTTPException(
            status_code=503,
            detail="Billing portal is not configured on this service yet",
        )
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(
            status_code=409,
            detail="No subscription is linked to this account yet",
        )
    return_url = str(request.base_url).rstrip("/") + "/app"
    try:
        url = create_portal_session(secret_key, customer_id, return_url)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"url": url}
