# Tests for accounts + VIP gate + Stripe webhook + portal + cancellation
# policy. Stdlib only, no network — except the live-shape server test
# at the bottom, which runs uvicorn on localhost and talks to the
# public Kalshi/Coinbase feeds the same way the service does (the poll
# thread needs them, exactly as in production).
# Run: python3 -m unittest discover -s tests

import hashlib
import hmac
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import billing

API_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")


def fresh_db():
    """A temp SQLite DB with the auth tables + the track-record
    summary table the validation lock reads."""
    conn = sqlite3.connect(":memory:")
    auth.init_auth_tables(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS track_record_summary ("
        "series TEXT PRIMARY KEY, settled INTEGER NOT NULL DEFAULT 0, "
        "correct INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    return conn


def make_user(conn, email="user@example.com", password="password123",
              is_owner=False):
    return auth.create_user(
        conn, email, auth.hash_password(password), is_owner=is_owner)


def seed_settled(conn, btc=0, eth=0):
    conn.execute(
        "INSERT OR REPLACE INTO track_record_summary "
        "(series, settled, correct) VALUES ('KXBTC15M', ?, 0)", (btc,))
    conn.execute(
        "INSERT OR REPLACE INTO track_record_summary "
        "(series, settled, correct) VALUES ('KXETH15M', ?, 0)", (eth,))
    conn.commit()


def stripe_event(event_id, event_type, customer, status,
                 cancel_at_period_end=False, current_period_end=None):
    obj = {
        "id": "sub_test",
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
    }
    if current_period_end is not None:
        obj["current_period_end"] = current_period_end
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def sign_payload(payload, secret, ts=None):
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={sig}"


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        h = auth.hash_password("correct horse battery staple")
        self.assertTrue(auth.verify_password("correct horse battery staple", h))
        self.assertFalse(auth.verify_password("wrong password", h))

    def test_tampered_hash_is_no(self):
        h = auth.hash_password("password123")
        parts = h.split("$")
        parts[-1] = "00" * 32
        self.assertFalse(auth.verify_password("password123", "$".join(parts)))
        self.assertFalse(auth.verify_password("password123", "not-a-hash"))
        self.assertFalse(auth.verify_password("password123", ""))

    def test_salts_differ(self):
        self.assertNotEqual(
            auth.hash_password("same-password"), auth.hash_password("same-password"))

    def test_email_normalization(self):
        self.assertEqual(auth.normalize_email("  USER@Example.COM "), "user@example.com")
        self.assertTrue(auth.valid_email("a@b.co"))
        self.assertFalse(auth.valid_email("not-an-email"))
        self.assertFalse(auth.valid_email("a@b"))
        self.assertFalse(auth.valid_email(""))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.user = make_user(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_create_verify_logout(self):
        token = auth.create_session(self.conn, self.user["id"])
        found = auth.verify_session(self.conn, token)
        self.assertIsNotNone(found)
        self.assertEqual(found["email"], "user@example.com")
        self.assertIsNone(auth.verify_session(self.conn, "bogus-token"))
        auth.delete_session(self.conn, token)
        self.assertIsNone(auth.verify_session(self.conn, token))

    def test_expired_session_is_deleted(self):
        token = auth.create_session(self.conn, self.user["id"])
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (time.time() - 10, token_hash))
        self.conn.commit()
        self.assertIsNone(auth.verify_session(self.conn, token))
        row = self.conn.execute(
            "SELECT token_hash FROM sessions WHERE token_hash = ?",
            (token_hash,)).fetchone()
        self.assertIsNone(row)

    def test_sliding_refresh_only_when_close_to_expiry(self):
        token = auth.create_session(self.conn, self.user["id"])
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        # 20 days left: no write, expiry unchanged.
        self.conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (time.time() + 20 * 86400, token_hash))
        self.conn.commit()
        auth.verify_session(self.conn, token)
        row = self.conn.execute(
            "SELECT expires_at FROM sessions WHERE token_hash = ?",
            (token_hash,)).fetchone()
        self.assertAlmostEqual(row[0], time.time() + 20 * 86400, delta=5)
        # 1 day left: expiry extends to ~30 days.
        self.conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (time.time() + 1 * 86400, token_hash))
        self.conn.commit()
        auth.verify_session(self.conn, token)
        row = self.conn.execute(
            "SELECT expires_at FROM sessions WHERE token_hash = ?",
            (token_hash,)).fetchone()
        self.assertAlmostEqual(row[0], time.time() + 30 * 86400, delta=5)


class EffectiveVipTests(unittest.TestCase):
    def _user(self, **kw):
        base = {
            "vip": 0, "vip_source": None, "is_owner": 0,
            "subscription_status": "none",
        }
        base.update(kw)
        return base

    def test_truth_table(self):
        self.assertFalse(auth.effective_vip(None))
        self.assertFalse(auth.effective_vip(self._user()))
        # Owner always sees the VIP view.
        self.assertTrue(auth.effective_vip(self._user(is_owner=1)))
        # Admin grant needs no subscription.
        self.assertTrue(auth.effective_vip(
            self._user(vip=1, vip_source="admin")))
        # Subscription grant needs a CURRENTLY ACTIVE subscription.
        self.assertTrue(auth.effective_vip(
            self._user(vip=1, vip_source="subscription",
                       subscription_status="active")))
        self.assertFalse(auth.effective_vip(
            self._user(vip=1, vip_source="subscription",
                       subscription_status="past_due")))
        self.assertFalse(auth.effective_vip(
            self._user(vip=1, vip_source="subscription",
                       subscription_status="canceled")))
        self.assertFalse(auth.effective_vip(
            self._user(vip=1, vip_source="subscription",
                       subscription_status="none")))
        # Cancel-at-period-end is still active: the member keeps VIP
        # until the period ends (status stays 'active').
        self.assertTrue(auth.effective_vip(
            self._user(vip=1, vip_source="subscription",
                       subscription_status="active",
                       subscription_cancel_at_period_end=1)))


class ValidationLockTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_lock_defaults_off(self):
        self.assertFalse(auth.validation_unlocked(self.conn))

    def test_unlock_sweep_grants_active_subscribers_only(self):
        u_active = make_user(self.conn, "active@example.com")
        u_past = make_user(self.conn, "past@example.com")
        u_none = make_user(self.conn, "none@example.com")
        auth.set_subscription_status(self.conn, u_active["id"], "active")
        auth.set_subscription_status(self.conn, u_past["id"], "past_due")
        granted = auth.grant_vip_to_active_subscribers(self.conn)
        self.assertEqual(granted, 1)
        self.assertTrue(auth.get_user_by_id(self.conn, u_active["id"])["vip"])
        self.assertFalse(auth.get_user_by_id(self.conn, u_past["id"])["vip"])
        self.assertFalse(auth.get_user_by_id(self.conn, u_none["id"])["vip"])

    def test_settled_counts(self):
        seed_settled(self.conn, btc=51, eth=50)
        self.assertEqual(auth.settled_counts(self.conn), (51, 50))
        seed_settled(self.conn, btc=100, eth=100)
        self.assertEqual(auth.settled_counts(self.conn), (100, 100))


class StripeSignatureTests(unittest.TestCase):
    SECRET = "whsec_test_secret"

    def test_valid(self):
        payload = b'{"id":"evt_1"}'
        header = sign_payload(payload, self.SECRET)
        self.assertTrue(
            billing.verify_stripe_signature(payload, header, self.SECRET))

    def test_wrong_secret(self):
        payload = b'{"id":"evt_1"}'
        header = sign_payload(payload, self.SECRET)
        self.assertFalse(
            billing.verify_stripe_signature(payload, header, "whsec_other"))

    def test_tampered_body(self):
        payload = b'{"id":"evt_1"}'
        header = sign_payload(payload, self.SECRET)
        self.assertFalse(billing.verify_stripe_signature(
            b'{"id":"evt_2"}', header, self.SECRET))

    def test_stale_timestamp(self):
        payload = b'{"id":"evt_1"}'
        header = sign_payload(payload, self.SECRET,
                              ts=int(time.time()) - 3600)
        self.assertFalse(
            billing.verify_stripe_signature(payload, header, self.SECRET))

    def test_malformed_header(self):
        payload = b'{"id":"evt_1"}'
        for bad in ("", "t=abc,v1=deadbeef", "v1=deadbeef", "t=123"):
            self.assertFalse(billing.verify_stripe_signature(
                payload, bad, self.SECRET))
        self.assertFalse(billing.verify_stripe_signature(payload, None, self.SECRET))
        self.assertFalse(billing.verify_stripe_signature(b"", "t=1,v1=x", self.SECRET))

    def test_multiple_v1_one_valid(self):
        payload = b'{"id":"evt_1"}'
        good = sign_payload(payload, self.SECRET).split("v1=")[1]
        header = f"t={int(time.time())},v1=deadbeef,v1={good}"
        self.assertTrue(
            billing.verify_stripe_signature(payload, header, self.SECRET))


class StripeWebhookTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.user = make_user(self.conn, "sub@example.com")
        auth.set_stripe_customer_id(self.conn, self.user["id"], "cus_test123")

    def tearDown(self):
        self.conn.close()

    def _user(self):
        return auth.get_user_by_id(self.conn, self.user["id"])

    def test_active_grants_when_unlocked(self):
        auth.set_validation_unlocked(self.conn, True)
        out = billing.handle_stripe_event(
            self.conn, stripe_event("evt_1", "customer.subscription.created",
                                    "cus_test123", "active"))
        self.assertEqual(out, "granted")
        self.assertEqual(self._user()["subscription_status"], "active")
        self.assertTrue(self._user()["vip"])
        self.assertEqual(self._user()["vip_source"], "subscription")

    def test_active_locked_records_but_does_not_grant(self):
        out = billing.handle_stripe_event(
            self.conn, stripe_event("evt_1", "customer.subscription.created",
                                    "cus_test123", "active"))
        self.assertEqual(out, "locked")
        self.assertEqual(self._user()["subscription_status"], "active")
        self.assertFalse(self._user()["vip"])

    def test_cancel_at_period_end_keeps_vip_until_period_end(self):
        auth.set_validation_unlocked(self.conn, True)
        period_end = int(time.time()) + 20 * 86400
        out = billing.handle_stripe_event(
            self.conn, stripe_event(
                "evt_1", "customer.subscription.updated", "cus_test123",
                "active", cancel_at_period_end=True,
                current_period_end=period_end))
        self.assertEqual(out, "granted")
        user = self._user()
        self.assertTrue(user["vip"])
        self.assertTrue(auth.effective_vip(user))
        self.assertEqual(user["subscription_cancel_at_period_end"], 1)
        self.assertEqual(user["subscription_current_period_end"], period_end)

    def test_deleted_at_period_end_revokes(self):
        auth.set_validation_unlocked(self.conn, True)
        billing.handle_stripe_event(
            self.conn, stripe_event("evt_1", "customer.subscription.created",
                                    "cus_test123", "active"))
        self.assertTrue(self._user()["vip"])
        out = billing.handle_stripe_event(
            self.conn, stripe_event("evt_2", "customer.subscription.deleted",
                                    "cus_test123", "canceled"))
        self.assertEqual(out, "revoked")
        self.assertFalse(self._user()["vip"])
        self.assertEqual(self._user()["subscription_status"], "canceled")

    def test_past_due_revokes(self):
        auth.set_validation_unlocked(self.conn, True)
        billing.handle_stripe_event(
            self.conn, stripe_event("evt_1", "customer.subscription.created",
                                    "cus_test123", "active"))
        out = billing.handle_stripe_event(
            self.conn, stripe_event("evt_2", "customer.subscription.updated",
                                    "cus_test123", "past_due"))
        self.assertEqual(out, "revoked")
        self.assertFalse(self._user()["vip"])
        self.assertEqual(self._user()["subscription_status"], "past_due")

    def test_trialing_does_not_grant(self):
        auth.set_validation_unlocked(self.conn, True)
        out = billing.handle_stripe_event(
            self.conn, stripe_event("evt_1", "customer.subscription.created",
                                    "cus_test123", "trialing"))
        self.assertEqual(out, "recorded")
        self.assertFalse(self._user()["vip"])
        self.assertEqual(self._user()["subscription_status"], "trialing")

    def test_unknown_customer_is_no_user(self):
        out = billing.handle_stripe_event(
            self.conn, stripe_event("evt_1", "customer.subscription.created",
                                    "cus_nobody", "active"))
        self.assertEqual(out, "no_user")

    def test_unhandled_type_is_ignored(self):
        out = billing.handle_stripe_event(
            self.conn, {"id": "evt_9", "type": "invoice.paid",
                        "data": {"object": {}}})
        self.assertEqual(out, "ignored")

    def test_duplicate_event_applies_once(self):
        auth.set_validation_unlocked(self.conn, True)
        event = stripe_event("evt_1", "customer.subscription.created",
                             "cus_test123", "active")
        self.assertEqual(billing.handle_stripe_event(self.conn, event), "granted")
        self.assertEqual(billing.handle_stripe_event(self.conn, event), "duplicate")
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM stripe_events WHERE event_id = 'evt_1'"
        ).fetchone()
        self.assertEqual(rows[0], 1)

    def test_malformed_event_raises(self):
        with self.assertRaises(ValueError):
            billing.handle_stripe_event(self.conn, {"type": "invoice.paid"})
        with self.assertRaises(ValueError):
            billing.handle_stripe_event(self.conn, {"id": "evt_1"})


class PortalSessionTests(unittest.TestCase):
    SECRET = "sk_test_secret"

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_success_returns_url_and_posts_customer(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            seen["body"] = req.data.decode()
            return self.FakeResponse(
                json.dumps({"url": "https://billing.stripe.com/session/abc"}).encode())

        url = billing.create_portal_session(
            self.SECRET, "cus_test123", "https://example.com/app",
            urlopen=fake_urlopen)
        self.assertEqual(url, "https://billing.stripe.com/session/abc")
        self.assertEqual(seen["url"], "https://api.stripe.com/v1/billing_portal/sessions")
        self.assertTrue(seen["auth"].startswith("Basic "))
        self.assertIn("customer=cus_test123", seen["body"])
        self.assertIn("return_url=", seen["body"])

    def test_stripe_http_error_is_value_error(self):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(
                    json.dumps({"error": {"message": "No such customer"}}).encode()))

        with self.assertRaises(ValueError) as ctx:
            billing.create_portal_session(
                self.SECRET, "cus_nope", "https://example.com/app",
                urlopen=fake_urlopen)
        self.assertIn("No such customer", str(ctx.exception))

    def test_missing_url_is_value_error(self):
        def fake_urlopen(req, timeout=None):
            return self.FakeResponse(b"{}")

        with self.assertRaises(ValueError):
            billing.create_portal_session(
                self.SECRET, "cus_test123", "https://example.com/app",
                urlopen=fake_urlopen)


class BillingPortalEndpointTests(unittest.TestCase):
    """The POST /billing/portal endpoint, called directly with a
    starlette Request (no HTTP server): 503 without the secret key,
    409 with no linked subscription, 200 with a stubbed Stripe call."""

    def setUp(self):
        # A temp FILE db (not :memory:): the factory hands out a fresh
        # connection per call and require_user closes it, so a shared
        # :memory: connection would die after the first request.
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmp.name, "test.db")
        first = sqlite3.connect(self._db_path)
        auth.init_auth_tables(first)
        first.execute(
            "CREATE TABLE IF NOT EXISTS track_record_summary ("
            "series TEXT PRIMARY KEY, settled INTEGER NOT NULL DEFAULT 0, "
            "correct INTEGER NOT NULL DEFAULT 0)"
        )
        first.commit()
        first.close()
        self._old_factory = auth._db_factory
        auth.set_db_factory(lambda: sqlite3.connect(self._db_path))
        auth.reset_rate_limits()
        self.conn = sqlite3.connect(self._db_path)
        self.user = make_user(self.conn, "portal@example.com")
        self.token = auth.create_session(self.conn, self.user["id"])
        self._old_secret = os.environ.pop("STRIPE_SECRET_KEY", None)

    def tearDown(self):
        auth.set_db_factory(self._old_factory)
        if self._old_secret is None:
            os.environ.pop("STRIPE_SECRET_KEY", None)
        else:
            os.environ["STRIPE_SECRET_KEY"] = self._old_secret
        self.conn.close()
        self._tmp.cleanup()

    def _request(self):
        from starlette.requests import Request

        scope = {
            "type": "http", "method": "POST",
            "headers": [(b"cookie",
                         f"btc_oracle_session={self.token}".encode())],
            "client": ("127.0.0.1", 1234),
        }
        return Request(scope)

    def test_503_without_secret_key(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            billing.billing_portal(self._request())
        self.assertEqual(ctx.exception.status_code, 503)

    def test_409_without_linked_subscription(self):
        from fastapi import HTTPException

        os.environ["STRIPE_SECRET_KEY"] = "sk_test_secret"
        with self.assertRaises(HTTPException) as ctx:
            billing.billing_portal(self._request())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_200_with_stubbed_stripe(self):
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_secret"
        auth.set_stripe_customer_id(self.conn, self.user["id"], "cus_test123")
        calls = {}

        def fake_create(secret, customer, return_url, urlopen=None):
            calls["secret"] = secret
            calls["customer"] = customer
            return "https://billing.stripe.com/session/abc"

        old = billing.create_portal_session
        billing.create_portal_session = fake_create
        try:
            out = billing.billing_portal(self._request())
        finally:
            billing.create_portal_session = old
        self.assertEqual(out, {"url": "https://billing.stripe.com/session/abc"})
        self.assertEqual(calls["secret"], "sk_test_secret")
        self.assertEqual(calls["customer"], "cus_test123")

    def test_401_without_session(self):
        from fastapi import HTTPException
        from starlette.requests import Request

        scope = {"type": "http", "method": "POST", "headers": [],
                 "client": ("127.0.0.1", 1234)}
        with self.assertRaises(HTTPException) as ctx:
            billing.billing_portal(Request(scope))
        self.assertEqual(ctx.exception.status_code, 401)


# ------------------------------------------------------------ live server
# Spawns uvicorn tracker:app on localhost against a temp local DB and
# drives the real HTTP flow. The poll thread talks to the public
# Kalshi/Coinbase feeds exactly as in production.

def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class LiveServerTests(unittest.TestCase):
    WEBHOOK_SECRET = "whsec_test_secret"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = cls.tmp.name
        for name in ("tracker.py", "auth.py", "billing.py"):
            shutil.copy(os.path.join(API_DIR, name), os.path.join(cls.root, name))
        shutil.copytree(os.path.join(API_DIR, "static"),
                        os.path.join(cls.root, "static"))
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = None
        cls.log_path = os.path.join(cls.root, "server.log")
        cls._log = open(cls.log_path, "ab")
        cls.start_server({})

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()
        cls._log.close()
        cls.tmp.cleanup()

    @classmethod
    def start_server(cls, env_extra):
        env = dict(os.environ)
        for key in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN",
                    "STRIPE_WEBHOOK_SECRET", "STRIPE_SECRET_KEY"):
            env.pop(key, None)
        env.update(env_extra)
        env["PYTHONUNBUFFERED"] = "1"
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tracker:app",
             "--host", "127.0.0.1", "--port", str(cls.port),
             "--log-level", "warning"],
            cwd=cls.root, env=env, stdout=cls._log, stderr=cls._log)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                status, _, _ = cls.http("GET", "/health")
                if status == 200:
                    return
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("server did not come up; see " + cls.log_path)

    @classmethod
    def stop_server(cls):
        if cls.proc is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
            cls.proc = None

    @classmethod
    def http(cls, method, path, body=None, headers=None, cookie=None,
             raw_body=None, timeout=30):
        data = raw_body
        hdrs = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(cls.base + path, data=data,
                                     method=method, headers=hdrs)
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (resp.status, resp.read().decode(),
                        resp.headers.get("Set-Cookie"))
        except urllib.error.HTTPError as exc:
            return (exc.code, exc.read().decode(),
                    exc.headers.get("Set-Cookie"))

    @staticmethod
    def _cookie(set_cookie):
        if not set_cookie:
            return None
        return set_cookie.split(";")[0].strip()

    @classmethod
    def _db(cls):
        return sqlite3.connect(os.path.join(cls.root, "oracle.db"),
                               timeout=30)

    @classmethod
    def _wait_for_table(cls, table, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                conn = cls._db()
                try:
                    conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    return
                finally:
                    conn.close()
            except sqlite3.Error:
                time.sleep(0.5)
        raise RuntimeError(f"table {table} never appeared")

    def test_01_full_flow(self):
        # --- public endpoints answer before any login
        status, body, _ = self.http("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        status, body, _ = self.http("GET", "/me")
        self.assertEqual(status, 401)

        # --- signup validation
        status, body, _ = self.http("POST", "/auth/signup", {
            "email": "not-an-email", "password": "password123",
            "age_confirm": True, "terms_agree": True})
        self.assertEqual(status, 400)
        status, body, _ = self.http("POST", "/auth/signup", {
            "email": "user@example.com", "password": "short",
            "age_confirm": True, "terms_agree": True})
        self.assertEqual(status, 400)
        status, body, _ = self.http("POST", "/auth/signup", {
            "email": "user@example.com", "password": "password123",
            "age_confirm": False, "terms_agree": True})
        self.assertEqual(status, 400)
        # Terms box unticked -> 400 with the docx's error text.
        status, body, _ = self.http("POST", "/auth/signup", {
            "email": "user@example.com", "password": "password123",
            "age_confirm": True, "terms_agree": False})
        self.assertEqual(status, 400)
        self.assertIn("Terms of Use and Refund and Cancellation Policy",
                      body)

        # --- signup + login
        status, body, set_cookie = self.http("POST", "/auth/signup", {
            "email": "user@example.com", "password": "password123",
            "age_confirm": True, "terms_agree": True})
        self.assertEqual(status, 200)
        cookie = self._cookie(set_cookie)
        self.assertIsNotNone(cookie)
        status, body, _ = self.http("POST", "/auth/signup", {
            "email": "user@example.com", "password": "password123",
            "age_confirm": True, "terms_agree": True})
        self.assertEqual(status, 409)
        status, body, _ = self.http("POST", "/auth/login", {
            "email": "user@example.com", "password": "wrongpass"})
        self.assertEqual(status, 401)
        status, body, set_cookie = self.http("POST", "/auth/login", {
            "email": "user@example.com", "password": "password123"})
        self.assertEqual(status, 200)
        cookie = self._cookie(set_cookie)

        # --- /me: free, locked
        status, body, _ = self.http("GET", "/me", cookie=cookie)
        self.assertEqual(status, 200)
        me = json.loads(body)
        self.assertEqual(me["email"], "user@example.com")
        self.assertFalse(me["vip"])
        self.assertFalse(me["vip_effective"])
        self.assertFalse(me["validation_unlocked"])

        # --- /app/live: free keys only (top-markets fetch is slow once)
        status, body, _ = self.http("GET", "/app/live", cookie=cookie,
                                    timeout=120)
        self.assertEqual(status, 200)
        live = json.loads(body)
        self.assertEqual(live["tier"], "free")
        self.assertEqual(set(live["markets"].keys()),
                         {"btc_15m", "eth_15m"})
        self.assertIn("top_markets", live)

        # --- /app/callouts: 403 for free
        status, body, _ = self.http("GET", "/app/callouts", cookie=cookie)
        self.assertEqual(status, 403)
        self.assertIn("locked", body)

        # --- the static frontend serves
        status, body, _ = self.http("GET", "/app")
        self.assertEqual(status, 200)
        self.assertIn("BTC Oracle Pro", body)

        # --- webhook + portal are unconfigured (no Stripe env vars)
        status, body, _ = self.http(
            "POST", "/stripe/webhook", raw_body=b'{"id":"evt_1"}',
            headers={"Stripe-Signature": "t=1,v1=x"})
        self.assertEqual(status, 503)
        status, body, _ = self.http("POST", "/billing/portal", cookie=cookie)
        self.assertEqual(status, 503)

        # --- owner account (direct DB insert; create_owner.py is tested
        # separately below) and the admin lock
        self._wait_for_table("users")
        conn = self._db()
        try:
            conn.execute(
                "INSERT INTO users (email, password_hash, created_at, is_owner) "
                "VALUES (?, ?, ?, 1)",
                ("owner@example.com",
                 auth.hash_password("ownerpass123"), time.time()))
            conn.commit()
        finally:
            conn.close()
        status, body, set_cookie = self.http("POST", "/auth/login", {
            "email": "owner@example.com", "password": "ownerpass123"})
        self.assertEqual(status, 200)
        owner_cookie = self._cookie(set_cookie)
        status, body, _ = self.http("POST", "/admin/grant-vip",
                                    {"email": "user@example.com"},
                                    cookie=cookie)
        self.assertEqual(status, 403)
        status, body, _ = self.http("POST", "/admin/grant-vip",
                                    {"email": "user@example.com"},
                                    cookie=owner_cookie)
        self.assertEqual(status, 409)
        self.assertIn("locked", body)

        # --- seed the validation bar + callout fixtures, then unlock
        self._wait_for_table("track_record_summary")
        conn = self._db()
        try:
            conn.execute(
                "UPDATE track_record_summary SET settled = 100, correct = 60 "
                "WHERE series = 'KXBTC15M'")
            conn.execute(
                "UPDATE track_record_summary SET settled = 100, correct = 55 "
                "WHERE series = 'KXETH15M'")
            now = time.time()
            conn.execute(
                "INSERT OR IGNORE INTO windows "
                "(ticker, open_ts, close_ts, target, target_provisional) "
                "VALUES (?, ?, ?, ?, 0)",
                ("KXBTC15M-26NOV171600-00", now - 600, now + 300, 100000.0))
            conn.execute(
                "INSERT INTO signals (window, ts, minute, direction, "
                "confidence, action, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("KXBTC15M-26NOV171600-00", now - 400, 3.0, "UP", 70.0,
                 "ENTER", "test callout early"))
            conn.execute(
                "INSERT INTO signals (window, ts, minute, direction, "
                "confidence, action, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("KXBTC15M-26NOV171600-00", now - 300, 5.0, "UP", 75.0,
                 "ENTER", "test callout repeat"))
            conn.execute(
                "INSERT INTO signals (window, ts, minute, direction, "
                "confidence, action, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("KXBTC15M-26NOV171600-00", now - 60, 11.0, "DOWN", 65.0,
                 "ENTER", "test callout too late"))
            conn.commit()
        finally:
            conn.close()
        status, body, _ = self.http("GET", "/admin/validation-status",
                                    cookie=owner_cookie)
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertGreaterEqual(state["btc_settled"], 100)
        self.assertGreaterEqual(state["eth_settled"], 100)
        status, body, _ = self.http("POST", "/admin/unlock-validation",
                                    {"unlock": True}, cookie=owner_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["validation_unlocked"])
        status, body, _ = self.http("POST", "/admin/grant-vip",
                                    {"email": "user@example.com"},
                                    cookie=owner_cookie)
        self.assertEqual(status, 200)

        # --- VIP now: full keys + the minute-3 callout only
        status, body, _ = self.http("GET", "/app/live", cookie=cookie)
        self.assertEqual(status, 200)
        live = json.loads(body)
        self.assertEqual(live["tier"], "vip")
        self.assertEqual(set(live["markets"].keys()), {
            "btc_15m", "eth_15m", "gold", "gold_15m", "sol_15m",
            "xrp_15m", "doge_15m", "bnb_15m", "silver_15m"})
        status, body, _ = self.http("GET", "/app/callouts", cookie=cookie)
        self.assertEqual(status, 200)
        callouts = json.loads(body)["callouts"]
        mine = [c for c in callouts
                if c["window"] == "KXBTC15M-26NOV171600-00"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["minute"], 3.0)
        self.assertEqual(mine[0]["outcome"], "pending")

        # --- revoke returns the user to free
        status, body, _ = self.http("POST", "/admin/revoke-vip",
                                    {"email": "user@example.com"},
                                    cookie=owner_cookie)
        self.assertEqual(status, 200)
        status, body, _ = self.http("GET", "/app/callouts", cookie=cookie)
        self.assertEqual(status, 403)

        # --- public endpoints still answer, unchanged
        status, body, _ = self.http("GET", "/live/all", timeout=120)
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body).keys()), {
            "btc_15m", "eth_15m", "gold", "gold_15m", "sol_15m",
            "xrp_15m", "doge_15m", "bnb_15m", "silver_15m"})
        status, body, _ = self.http("GET", "/track-record")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(json.loads(body)["windows_settled"], 100)
        status, body, _ = self.http("GET", "/entries")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)
        status, body, _ = self.http("GET", "/signals")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)
        status, body, _ = self.http("GET", "/windows")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)
        status, body, _ = self.http("GET", "/candles?series=KXSOL15M",
                                    timeout=60)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn(payload["status"], ("ok", "unavailable"))
        self.assertIn("candles", payload)
        status, body, _ = self.http("GET", "/top-markets", timeout=120)
        self.assertEqual(status, 200)
        self.assertIn("by_volume", json.loads(body))

    def test_02_webhook_flow(self):
        # Restart with the webhook secret set; state persists in the DB.
        self.stop_server()
        self.start_server(
            {"STRIPE_WEBHOOK_SECRET": self.WEBHOOK_SECRET})

        def webhook(event):
            raw = json.dumps(event).encode()
            header = sign_payload(raw, self.WEBHOOK_SECRET)
            return self.http("POST", "/stripe/webhook", raw_body=raw,
                             headers={"Stripe-Signature": header})

        # Log the user back in (sessions persist across the restart).
        status, body, set_cookie = self.http("POST", "/auth/login", {
            "email": "user@example.com", "password": "password123"})
        self.assertEqual(status, 200)
        cookie = self._cookie(set_cookie)
        status, body, set_cookie = self.http("POST", "/auth/login", {
            "email": "owner@example.com", "password": "ownerpass123"})
        owner_cookie = self._cookie(set_cookie)

        # Link a Stripe customer, then an active subscription grants VIP.
        status, body, _ = self.http(
            "POST", "/admin/link-stripe",
            {"email": "user@example.com", "stripe_customer_id": "cus_test123"},
            cookie=owner_cookie)
        self.assertEqual(status, 200)
        period_end = int(time.time()) + 20 * 86400
        status, body, _ = webhook(stripe_event(
            "evt_live_1", "customer.subscription.created", "cus_test123",
            "active", cancel_at_period_end=True,
            current_period_end=period_end))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["handled"], "granted")
        status, body, _ = self.http("GET", "/me", cookie=cookie)
        me = json.loads(body)
        self.assertTrue(me["vip_effective"])
        self.assertEqual(me["subscription_status"], "active")
        self.assertTrue(me["subscription_cancel_at_period_end"])
        self.assertIsNotNone(me["subscription_current_period_end"])

        # Same event twice: the second is a duplicate, not a re-apply.
        status, body, _ = webhook(stripe_event(
            "evt_live_1", "customer.subscription.created", "cus_test123",
            "active", cancel_at_period_end=True,
            current_period_end=period_end))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["handled"], "duplicate")

        # Bad signature: 400.
        raw = json.dumps(stripe_event(
            "evt_live_2", "customer.subscription.updated", "cus_test123",
            "active")).encode()
        status, body, _ = self.http(
            "POST", "/stripe/webhook", raw_body=raw,
            headers={"Stripe-Signature": "t=1,v1=deadbeef"})
        self.assertEqual(status, 400)

        # Period end: the deleted event flips VIP off.
        status, body, _ = webhook(stripe_event(
            "evt_live_3", "customer.subscription.deleted", "cus_test123",
            "canceled"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["handled"], "revoked")
        status, body, _ = self.http("GET", "/me", cookie=cookie)
        me = json.loads(body)
        self.assertFalse(me["vip_effective"])
        self.assertEqual(me["subscription_status"], "canceled")
        status, body, _ = self.http("GET", "/app/callouts", cookie=cookie)
        self.assertEqual(status, 403)

        # Portal still 503: no STRIPE_SECRET_KEY on this server.
        status, body, _ = self.http("POST", "/billing/portal", cookie=cookie)
        self.assertEqual(status, 503)

    def test_03_terms_pages_and_agreement(self):
        # --- /terms: the docx text with Cheli's three decisions filled
        # in, and no bracketed placeholders or draft-internal notes.
        status, body, _ = self.http("GET", "/terms")
        self.assertEqual(status, 200)
        self.assertIn("Cheli S Williams", body)
        self.assertIn("Virginia", body)
        self.assertIn("btcoraclepro@proton.me", body)
        self.assertIn("terms-2026-11-18-v1", body)
        self.assertIn("non-refundable", body)
        self.assertIn("18 or older", body)
        for placeholder in ("[OPERATOR", "[STATE", "[SUPPORT",
                            "Lawyer to confirm"):
            self.assertNotIn(placeholder, body)

        # --- /refund-policy: the no-refunds rule + the two exceptions.
        status, body, _ = self.http("GET", "/refund-policy")
        self.assertEqual(status, 200)
        self.assertIn("refund-2026-11-18-v1", body)
        self.assertIn("non-refundable", body)
        self.assertIn("within 30 days", body)
        self.assertIn("Stripe customer portal", body)
        self.assertNotIn("[SUPPORT", body)
        self.assertNotIn("Lawyer to confirm", body)

        # --- the root endpoint list names the new routes.
        status, body, _ = self.http("GET", "/")
        self.assertEqual(status, 200)
        endpoints = json.loads(body)["endpoints"]
        for route in ("/terms", "/refund-policy", "/auth/agreement"):
            self.assertIn(route, endpoints)

        # --- signup with both boxes records the agreement; the
        # endpoint returns it, and 401 without a session.
        status, body, _ = self.http("GET", "/auth/agreement")
        self.assertEqual(status, 401)
        status, body, set_cookie = self.http("POST", "/auth/signup", {
            "email": "terms@example.com", "password": "password123",
            "age_confirm": True, "terms_agree": True})
        self.assertEqual(status, 200)
        cookie = self._cookie(set_cookie)
        status, body, _ = self.http("GET", "/auth/agreement", cookie=cookie)
        self.assertEqual(status, 200)
        agreement = json.loads(body)["agreement"]
        self.assertEqual(agreement["terms_version"], "terms-2026-11-18-v1")
        self.assertEqual(agreement["policy_version"],
                         "refund-2026-11-18-v1")
        self.assertEqual(agreement["method"], "signup checkboxes")
        self.assertTrue(agreement["age_confirmed"])
        self.assertTrue(agreement["terms_agreed"])
        self.assertEqual(agreement["terms_effective_date"], "2026-11-18")

        # --- the viewer carries the new signup + subscribe UI.
        status, body, _ = self.http("GET", "/app")
        self.assertEqual(status, 200)
        self.assertIn('id="terms-agree"', body)
        self.assertIn("I have read and agree to the", body)
        self.assertIn('id="agreement-line"', body)
        self.assertIn("Subscribe to VIP", body)
        self.assertIn("VIP is $25/month, billed through Stripe", body)
        self.assertIn("non-refundable", body)


class OperatorScriptTests(unittest.TestCase):
    def _copy_api(self, tmp):
        for name in ("auth.py", "create_owner.py", "reset_password.py"):
            shutil.copy(os.path.join(API_DIR, name), os.path.join(tmp, name))

    def test_create_owner_then_refuses_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._copy_api(tmp)
            proc = subprocess.run(
                [sys.executable, "create_owner.py"], cwd=tmp,
                input="owner@example.com\nownerpass123\nownerpass123\n",
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("Owner account created", proc.stdout)
            conn = sqlite3.connect(os.path.join(tmp, "oracle.db"))
            try:
                row = conn.execute(
                    "SELECT email, password_hash, is_owner FROM users "
                    "WHERE email = 'owner@example.com'").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[2], 1)
                self.assertTrue(auth.verify_password("ownerpass123", row[1]))
            finally:
                conn.close()
            proc = subprocess.run(
                [sys.executable, "create_owner.py"], cwd=tmp,
                input="other@example.com\notherpass123\notherpass123\n",
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("already exists", proc.stdout)

    def test_reset_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._copy_api(tmp)
            conn = sqlite3.connect(os.path.join(tmp, "oracle.db"))
            try:
                auth.init_auth_tables(conn)
                user = make_user(conn, "user@example.com", "oldpass123")
                token = auth.create_session(conn, user["id"])
            finally:
                conn.close()
            proc = subprocess.run(
                [sys.executable, "reset_password.py", "user@example.com"],
                cwd=tmp, input="newpass123\nnewpass123\n",
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("Password reset", proc.stdout)
            conn = sqlite3.connect(os.path.join(tmp, "oracle.db"))
            try:
                user = auth.get_user_by_email(conn, "user@example.com")
                self.assertTrue(auth.verify_password("newpass123", user["password_hash"]))
                self.assertFalse(auth.verify_password("oldpass123", user["password_hash"]))
                self.assertIsNone(auth.verify_session(conn, token))
            finally:
                conn.close()
