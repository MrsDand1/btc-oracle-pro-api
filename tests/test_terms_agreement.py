"""Tests for the BTC Oracle Pro Terms + Refund Policy implementation
(added 2026-11-18, staging deploy-terms).

Covers the signup agreement record (fields, versions, never-overwrite,
additional row on a new version) and the signup endpoint's two-box
gate, calling the endpoint functions directly with a starlette Request
(no HTTP server) — the same pattern as BillingPortalEndpointTests in
test_accounts_vip.py. Live-server checks for /terms, /refund-policy
and GET /auth/agreement live in test_accounts_vip.py (LiveServerTests).

Run: python3 -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
from auth import SignupBody


def fresh_db():
    """A temp SQLite DB with the auth tables (which now include
    terms_agreements) + the track-record summary table."""
    conn = sqlite3.connect(":memory:")
    auth.init_auth_tables(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS track_record_summary ("
        "series TEXT PRIMARY KEY, settled INTEGER NOT NULL DEFAULT 0, "
        "correct INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    return conn


def make_user(conn, email="terms@example.com", password="password123"):
    return auth.create_user(
        conn, email, auth.hash_password(password))


def _request(cookie=None, ip="127.0.0.1", ua="TermsTest/1.0"):
    from starlette.requests import Request

    headers = [(b"user-agent", ua.encode()),
               (b"x-forwarded-for", ip.encode())]
    if cookie:
        headers.append((b"cookie",
                        f"btc_oracle_session={cookie}".encode()))
    scope = {
        "type": "http", "method": "POST", "headers": headers,
        "client": (ip, 1234),
    }
    return Request(scope)


class AgreementRecordTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.user = make_user(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_record_stores_all_fields(self):
        rec = auth.record_terms_agreement(
            self.conn, self.user["id"], self.user["email"],
            age_confirmed=True, terms_agreed=True,
            ip="203.0.113.7", user_agent="TermsTest/1.0")
        self.assertEqual(rec["user_id"], self.user["id"])
        self.assertEqual(rec["email"], "terms@example.com")
        self.assertEqual(rec["terms_version"], "terms-2026-11-18-v1")
        self.assertEqual(rec["terms_effective_date"], "2026-11-18")
        self.assertEqual(rec["policy_version"], "refund-2026-11-18-v1")
        self.assertEqual(rec["policy_effective_date"], "2026-11-18")
        self.assertEqual(rec["age_confirmed"], 1)
        self.assertEqual(rec["terms_agreed"], 1)
        self.assertEqual(rec["method"], "signup checkboxes")
        self.assertEqual(rec["ip"], "203.0.113.7")
        self.assertEqual(rec["user_agent"], "TermsTest/1.0")
        # agreed_at is a UTC ISO timestamp.
        parsed = datetime.fromisoformat(rec["agreed_at"])
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_second_record_same_version_never_overwrites(self):
        first = auth.record_terms_agreement(
            self.conn, self.user["id"], self.user["email"],
            age_confirmed=True, terms_agreed=True,
            agreed_at="2026-11-18T10:00:00+00:00")
        second = auth.record_terms_agreement(
            self.conn, self.user["id"], self.user["email"],
            age_confirmed=True, terms_agreed=True,
            agreed_at="2026-11-19T10:00:00+00:00")
        # The original row (and its original timestamp) survives.
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["agreed_at"], "2026-11-18T10:00:00+00:00")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM terms_agreements WHERE user_id = ?",
            (self.user["id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_new_version_is_an_additional_row(self):
        auth.record_terms_agreement(
            self.conn, self.user["id"], self.user["email"],
            age_confirmed=True, terms_agreed=True,
            agreed_at="2026-11-18T10:00:00+00:00")
        old_terms, old_policy = auth.TERMS_VERSION, auth.POLICY_VERSION
        auth.TERMS_VERSION = "terms-2026-12-01-v2"
        auth.POLICY_VERSION = "refund-2026-12-01-v2"
        try:
            auth.record_terms_agreement(
                self.conn, self.user["id"], self.user["email"],
                age_confirmed=True, terms_agreed=True,
                agreed_at="2026-12-01T10:00:00+00:00")
        finally:
            auth.TERMS_VERSION, auth.POLICY_VERSION = old_terms, old_policy
        rows = self.conn.execute(
            "SELECT terms_version FROM terms_agreements WHERE user_id = ? "
            "ORDER BY id",
            (self.user["id"],)).fetchall()
        self.assertEqual([r[0] for r in rows],
                         ["terms-2026-11-18-v1", "terms-2026-12-01-v2"])
        latest = auth.get_terms_agreement(self.conn, self.user["id"])
        self.assertEqual(latest["terms_version"], "terms-2026-12-01-v2")
        # The original record is untouched.
        original = auth.get_terms_agreement(
            self.conn, self.user["id"], "terms-2026-11-18-v1")
        self.assertEqual(original["agreed_at"], "2026-11-18T10:00:00+00:00")

    def test_no_record_returns_none(self):
        self.assertIsNone(
            auth.get_terms_agreement(self.conn, self.user["id"]))
        self.assertIsNone(auth.public_agreement(None))

    def test_public_agreement_uses_json_booleans(self):
        auth.record_terms_agreement(
            self.conn, self.user["id"], self.user["email"],
            age_confirmed=True, terms_agreed=True)
        pub = auth.public_agreement(
            auth.get_terms_agreement(self.conn, self.user["id"]))
        self.assertIs(pub["age_confirmed"], True)
        self.assertIs(pub["terms_agreed"], True)
        self.assertEqual(pub["method"], "signup checkboxes")


class SignupEndpointTests(unittest.TestCase):
    """POST /auth/signup, called directly (no HTTP server)."""

    def setUp(self):
        # A temp FILE db (not :memory:): the factory hands out a fresh
        # connection per call and require_user closes it.
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

    def tearDown(self):
        auth.set_db_factory(self._old_factory)
        self.conn.close()
        self._tmp.cleanup()

    def _signup(self, email, age_confirm, terms_agree, ip="127.0.0.1"):
        from fastapi import Response

        body = SignupBody(email=email, password="password123",
                          age_confirm=age_confirm, terms_agree=terms_agree)
        return auth.signup(body, _request(ip=ip), Response())

    def test_signup_without_terms_agree_is_400_and_creates_nothing(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self._signup("a@example.com", True, False)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(
            ctx.exception.detail,
            "Please confirm you are 18 or older and agree to the Terms "
            "of Use and Refund and Cancellation Policy before creating "
            "your account.")
        self.assertIsNone(
            auth.get_user_by_email(self.conn, "a@example.com"))

    def test_signup_without_age_confirm_is_400_and_creates_nothing(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self._signup("b@example.com", False, True)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIsNone(
            auth.get_user_by_email(self.conn, "b@example.com"))

    def test_signup_with_both_boxes_creates_user_and_record(self):
        out = self._signup("c@example.com", True, True,
                           ip="203.0.113.9")
        self.assertEqual(out, {"ok": True, "email": "c@example.com",
                               "vip": False})
        user = auth.get_user_by_email(self.conn, "c@example.com")
        self.assertIsNotNone(user)
        rec = auth.get_terms_agreement(self.conn, user["id"])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["terms_version"], "terms-2026-11-18-v1")
        self.assertEqual(rec["policy_version"], "refund-2026-11-18-v1")
        self.assertEqual(rec["method"], "signup checkboxes")
        self.assertEqual(rec["age_confirmed"], 1)
        self.assertEqual(rec["terms_agreed"], 1)
        self.assertEqual(rec["ip"], "203.0.113.9")
        self.assertEqual(rec["user_agent"], "TermsTest/1.0")

    def test_duplicate_signup_is_409_and_keeps_first_record(self):
        from fastapi import HTTPException

        first = self._signup("d@example.com", True, True)
        self.assertTrue(first["ok"])
        user = auth.get_user_by_email(self.conn, "d@example.com")
        first_rec = auth.get_terms_agreement(self.conn, user["id"])
        with self.assertRaises(HTTPException) as ctx:
            self._signup("d@example.com", True, True, ip="203.0.113.10")
        self.assertEqual(ctx.exception.status_code, 409)
        rec = auth.get_terms_agreement(self.conn, user["id"])
        self.assertEqual(rec["id"], first_rec["id"])
        self.assertEqual(rec["agreed_at"], first_rec["agreed_at"])

    def test_get_agreement_endpoint(self):
        from fastapi import HTTPException

        self._signup("e@example.com", True, True)
        user = auth.get_user_by_email(self.conn, "e@example.com")
        token = auth.create_session(self.conn, user["id"])
        out = auth.get_agreement(_request(cookie=token))
        self.assertEqual(out["agreement"]["terms_version"],
                         "terms-2026-11-18-v1")
        self.assertEqual(out["agreement"]["policy_version"],
                         "refund-2026-11-18-v1")
        self.assertTrue(out["agreement"]["age_confirmed"])
        self.assertTrue(out["agreement"]["terms_agreed"])
        # No session -> 401.
        with self.assertRaises(HTTPException) as ctx:
            auth.get_agreement(_request())
        self.assertEqual(ctx.exception.status_code, 401)
        # A user created without the signup form has no record.
        other = make_user(self.conn, "f@example.com")
        token2 = auth.create_session(self.conn, other["id"])
        out2 = auth.get_agreement(_request(cookie=token2))
        self.assertIsNone(out2["agreement"])


if __name__ == "__main__":
    unittest.main()
