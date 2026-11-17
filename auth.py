"""BTC Oracle Pro — accounts + VIP gate (added 2026-11-17).

Email + password auth (stdlib only: hashlib.scrypt, secrets, hmac).
No magic link (no email-sending service exists), no OAuth (no new
third-party credentials), no new dependencies, no new secrets: sessions
are random tokens stored hashed in the DB, so there is no server-side
signing secret to manage.

VIP model: a per-user `vip` flag plus a `subscription_status` from the
Stripe webhook (see billing.py). The gate never trusts the flag alone
(see effective_vip). VIP is LOCKED until the 100-window validation
passes: the `validation_unlocked` flag in app_config starts at '0',
and no code path can set a user to VIP while it is '0'.

This module must not import tracker at the top level (tracker imports
this module). The DB connection comes from a factory set by tracker
(auth.set_db_factory(db)); tests set their own factory. Rows are read
through _as_dict with explicit columns, so both the tracker's _Row
wrapper (Turso/local) and plain sqlite3 tuples work.
"""

import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

# ------------------------------------------------------------ schema
# One source of truth for the auth tables. tracker._init_db_once runs
# this on both backends (Turso and local SQLite); tests call
# init_auth_tables directly.
AUTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_login_at REAL,
    vip INTEGER NOT NULL DEFAULT 0,
    vip_source TEXT,
    is_owner INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id TEXT UNIQUE,
    subscription_status TEXT NOT NULL DEFAULT 'none',
    subscription_cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    subscription_current_period_end REAL,
    CHECK (vip IN (0, 1)), CHECK (is_owner IN (0, 1))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    received_at REAL NOT NULL,
    handled TEXT NOT NULL
);
"""

USER_COLUMNS = (
    "id", "email", "password_hash", "created_at", "last_login_at",
    "vip", "vip_source", "is_owner", "stripe_customer_id",
    "subscription_status", "subscription_cancel_at_period_end",
    "subscription_current_period_end",
)
SESSION_COLUMNS = ("token_hash", "user_id", "created_at", "expires_at")

SESSION_COOKIE = "btc_oracle_session"
SESSION_DAYS = 30
SESSION_REFRESH_DAYS = 7  # extend the expiry when under 7 days remain
MIN_PASSWORD_LEN = 8
VALIDATION_REQUIRED_SETTLED = 100  # settled windows per series (BTC + ETH)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# scrypt parameters (stdlib hashlib). ~50-100ms per hash on this tier:
# slow enough to hurt guessing, fast enough for a login.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 16384, 8, 1, 32


def init_auth_tables(conn):
    """Create the auth tables on an open connection. Idempotent."""
    conn.executescript(AUTH_SCHEMA_SQL)
    conn.commit()


# ------------------------------------------------------------ db access
_db_factory = None


def set_db_factory(fn):
    """tracker calls this with its db() right after import."""
    global _db_factory
    _db_factory = fn


def get_db():
    """A connection on the configured backend (Turso or local SQLite)."""
    if _db_factory is not None:
        return _db_factory()
    # Fallback only: tracker sets the factory at import, so reaching
    # here means this module was imported standalone.
    from tracker import db as tracker_db

    return tracker_db()


def _as_dict(row, columns):
    """A DB row as a plain dict, on either backend.

    The tracker's _Row answers row["col"]; plain sqlite3 rows are
    tuples. All queries in this module SELECT explicit columns in a
    known order, so both shapes convert the same way.
    """
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {c: row[c] for c in columns}
    return dict(zip(columns, row))


# ------------------------------------------------------------ passwords
def hash_password(password):
    """scrypt hash, encoded as scrypt$n$r$p$salt_hex$hash_hex."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{salt.hex()}${dk.hex()}"
    )


def verify_password(password, stored):
    """Constant-time check. Any malformed stored hash is a plain no."""
    try:
        _, n, r, p, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(dk_hex)),
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def normalize_email(email):
    """Lowercase + strip. Two spellings of one address are one account."""
    return (email or "").strip().lower()


def valid_email(email):
    return bool(email) and bool(_EMAIL_RE.match(email))


# ------------------------------------------------------------ sessions
def create_session(conn, user_id):
    """New random token; only its SHA-256 is stored. Returns the token."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (
            hashlib.sha256(token.encode()).hexdigest(),
            user_id, now, now + SESSION_DAYS * 86400,
        ),
    )
    conn.commit()
    return token


def verify_session(conn, token):
    """The user dict for a live session token, or None.

    Extends the expiry (sliding) only when under 7 days remain, so an
    ordinary page load does not cost a DB write per request.
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
        (hashlib.sha256(token.encode()).hexdigest(),),
    ).fetchone()
    session = _as_dict(row, ("user_id", "expires_at"))
    if session is None:
        return None
    now = time.time()
    if session["expires_at"] <= now:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        conn.commit()
        return None
    if session["expires_at"] - now < SESSION_REFRESH_DAYS * 86400:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (
                now + SESSION_DAYS * 86400,
                hashlib.sha256(token.encode()).hexdigest(),
            ),
        )
        conn.commit()
    return get_user_by_id(conn, session["user_id"])


def delete_session(conn, token):
    if not token:
        return
    conn.execute(
        "DELETE FROM sessions WHERE token_hash = ?",
        (hashlib.sha256(token.encode()).hexdigest(),),
    )
    conn.commit()


def set_session_cookie(response, token):
    """HttpOnly, SameSite=Lax, Secure, 30 days. SameSite=Lax is the
    CSRF defense for the state-changing endpoints: a cross-site POST
    does not carry the cookie. (Render serves HTTPS, so Secure is
    always correct in production.)"""
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 86400, httponly=True,
        samesite="lax", secure=True, path="/",
    )


def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE, path="/")


# ------------------------------------------------------------ users
def get_user_by_email(conn, email):
    row = conn.execute(
        "SELECT " + ", ".join(USER_COLUMNS) + " FROM users WHERE email = ?",
        (normalize_email(email),),
    ).fetchone()
    return _as_dict(row, USER_COLUMNS)


def get_user_by_id(conn, user_id):
    row = conn.execute(
        "SELECT " + ", ".join(USER_COLUMNS) + " FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return _as_dict(row, USER_COLUMNS)


def get_user_by_stripe_customer_id(conn, stripe_customer_id):
    if not stripe_customer_id:
        return None
    row = conn.execute(
        "SELECT " + ", ".join(USER_COLUMNS)
        + " FROM users WHERE stripe_customer_id = ?",
        (stripe_customer_id,),
    ).fetchone()
    return _as_dict(row, USER_COLUMNS)


def create_user(conn, email, password_hash, is_owner=False):
    """Insert a free account. Returns the user dict. Raises on a
    duplicate email (the UNIQUE constraint) — the caller maps it to 409."""
    now = time.time()
    conn.execute(
        "INSERT INTO users (email, password_hash, created_at, is_owner) "
        "VALUES (?, ?, ?, ?)",
        (normalize_email(email), password_hash, now, 1 if is_owner else 0),
    )
    conn.commit()
    return get_user_by_email(conn, email)


def set_password(conn, user_id, password_hash):
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    conn.commit()


def set_vip(conn, user_id, vip, source=None):
    """Set the VIP flag. The CALLER enforces the validation lock —
    this function is deliberately dumb so every grant path is visible
    at its call site (admin grant, webhook, unlock sweep)."""
    conn.execute(
        "UPDATE users SET vip = ?, vip_source = ? WHERE id = ?",
        (1 if vip else 0, source, user_id),
    )
    conn.commit()


def set_subscription_status(conn, user_id, status):
    conn.execute(
        "UPDATE users SET subscription_status = ? WHERE id = ?",
        (status, user_id),
    )
    conn.commit()


def set_subscription_details(conn, user_id, status, cancel_at_period_end, current_period_end):
    """Record a subscription event's status AND its period-end state.

    A subscription with cancel_at_period_end set is still 'active'
    until the period ends — the member keeps VIP until then, and the
    flag flips off at period end when Stripe sends the deleted event.
    current_period_end is a unix timestamp (or None when Stripe does
    not send one)."""
    conn.execute(
        "UPDATE users SET subscription_status = ?, "
        "subscription_cancel_at_period_end = ?, "
        "subscription_current_period_end = ? WHERE id = ?",
        (
            status,
            1 if cancel_at_period_end else 0,
            current_period_end,
            user_id,
        ),
    )
    conn.commit()


def set_stripe_customer_id(conn, user_id, stripe_customer_id):
    conn.execute(
        "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
        (stripe_customer_id, user_id),
    )
    conn.commit()


def touch_login(conn, user_id):
    conn.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?",
        (time.time(), user_id),
    )
    conn.commit()


# ------------------------------------------------------------ the gate
def effective_vip(user):
    """What the gate actually enforces. Never trust the vip flag alone.

    - The owner always sees the VIP view (they must, to test it).
    - An admin grant (comped account) needs no subscription.
    - A subscription grant needs a CURRENTLY ACTIVE subscription:
      if the webhook missed a cancellation, a past_due/canceled status
      still blocks access at request time. Either layer can revoke.
    """
    if user is None:
        return False
    if user.get("is_owner"):
        return True
    if not user.get("vip"):
        return False
    if user.get("vip_source") in ("admin", "owner"):
        return True
    return user.get("subscription_status") == "active"


def validation_unlocked(conn):
    row = conn.execute(
        "SELECT value FROM app_config WHERE key = 'validation_unlocked'"
    ).fetchone()
    if row is None:
        return False
    value = row[0] if not hasattr(row, "keys") else row["value"]
    return value == "1"


def set_validation_unlocked(conn, unlocked):
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES ('validation_unlocked', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("1" if unlocked else "0",),
    )
    conn.commit()


def settled_counts(conn):
    """(btc_settled, eth_settled) from the track-record summary table."""
    counts = {}
    for series in ("KXBTC15M", "KXETH15M"):
        row = conn.execute(
            "SELECT settled FROM track_record_summary WHERE series = ?",
            (series,),
        ).fetchone()
        if row is None:
            counts[series] = 0
        elif hasattr(row, "keys"):
            counts[series] = row["settled"] or 0
        else:
            counts[series] = row[0] or 0
    return counts["KXBTC15M"], counts["KXETH15M"]


def grant_vip_to_active_subscribers(conn):
    """Unlock sweep: every user whose subscription is active gets VIP.
    Returns how many were granted. Used by the unlock endpoint so
    nobody who paid while the gate was locked is left waiting.

    The count is read before the UPDATE (the UPDATE's own WHERE clause
    is the source of truth for who gets VIP): the tracker's DB cursor
    wrapper does not expose rowcount, and a pre-count works identically
    on SQLite and Turso."""
    row = conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE subscription_status = 'active' AND vip = 0"
    ).fetchone()
    count = row[0] if not hasattr(row, "keys") else row["COUNT(*)"]
    if count is None:
        count = 0
    conn.execute(
        "UPDATE users SET vip = 1, vip_source = 'subscription' "
        "WHERE subscription_status = 'active' AND vip = 0"
    )
    conn.commit()
    return count


# ------------------------------------------------------------ rate limits
# In-memory, per process. Resets on restart — acceptable on the free
# tier: the scrypt hash is the real defense and accounts hold no money.
_RATE_LIMITS = {
    "login_email": {}, "login_ip": {}, "signup_ip": {}, "portal_user": {},
}
_RATE_RULES = {
    "login_email": (5, 15 * 60),   # 5 failed logins per email per 15 min
    "login_ip": (20, 15 * 60),     # 20 failed logins per IP per 15 min
    "signup_ip": (10, 60 * 60),    # 10 signups per IP per hour
    "portal_user": (10, 60 * 60),  # 10 portal sessions per user per hour
}


def reset_rate_limits():
    """Tests only."""
    for bucket in _RATE_LIMITS.values():
        bucket.clear()


def check_rate_limit(bucket, key):
    """Public wrapper for the in-memory rate limiter (used by the
    billing portal endpoint too). Raises 429 when the bucket is full."""
    _rate_check(bucket, key)


def _rate_check(bucket, key):
    limit, window_secs = _RATE_RULES[bucket]
    now = time.time()
    hits = _RATE_LIMITS[bucket].setdefault(key, [])
    hits[:] = [t for t in hits if now - t < window_secs]
    if len(hits) >= limit:
        raise HTTPException(
            status_code=429, detail="Too many attempts — try again later"
        )
    hits.append(now)


def _client_ip(request):
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------ HTTP layer
router = APIRouter()


class SignupBody(BaseModel):
    email: str
    password: str
    age_confirm: bool = False


class LoginBody(BaseModel):
    email: str
    password: str


class EmailBody(BaseModel):
    email: str


class LinkStripeBody(BaseModel):
    email: str
    stripe_customer_id: str


class UnlockBody(BaseModel):
    unlock: bool = False


def require_user(request: Request):
    """The logged-in user dict, or 401. Used as a dependency."""
    conn = get_db()
    try:
        user = verify_session(conn, request.cookies.get(SESSION_COOKIE))
    finally:
        conn.close()
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    return user


def require_owner(user: dict):
    if not user.get("is_owner"):
        raise HTTPException(
            status_code=403, detail="Owner account required"
        )
    return user


def _public_user(user):
    """What /me returns: no hash, no ids beyond the email. vip_effective
    is what the gate enforces; vip is the raw flag."""
    conn = get_db()
    try:
        unlocked = validation_unlocked(conn)
    finally:
        conn.close()
    return {
        "email": user["email"],
        "vip": bool(user["vip"]),
        "vip_effective": effective_vip(user),
        "vip_source": user["vip_source"],
        "subscription_status": user["subscription_status"],
        "subscription_cancel_at_period_end": bool(
            user.get("subscription_cancel_at_period_end")
        ),
        "subscription_current_period_end": (
            datetime.fromtimestamp(
                user["subscription_current_period_end"], tz=timezone.utc
            ).isoformat()
            if user.get("subscription_current_period_end")
            else None
        ),
        "is_owner": bool(user["is_owner"]),
        "validation_unlocked": unlocked,
    }


@router.post("/auth/signup")
def signup(body: SignupBody, request: Request, response: Response):
    _rate_check("signup_ip", _client_ip(request))
    email = normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if len(body.password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LEN} characters",
        )
    if not body.age_confirm:
        raise HTTPException(
            status_code=400, detail="You must confirm you are 18 or older"
        )
    conn = get_db()
    try:
        if get_user_by_email(conn, email) is not None:
            raise HTTPException(
                status_code=409, detail="An account with that email already exists"
            )
        user = create_user(conn, email, hash_password(body.password))
        token = create_session(conn, user["id"])
    finally:
        conn.close()
    set_session_cookie(response, token)
    return {"ok": True, "email": user["email"], "vip": False}


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    email = normalize_email(body.email)
    conn = get_db()
    try:
        user = get_user_by_email(conn, email)
        # Same 401 either way: no user enumeration.
        if user is None or not verify_password(body.password, user["password_hash"]):
            _rate_check("login_email", email)
            _rate_check("login_ip", _client_ip(request))
            raise HTTPException(
                status_code=401, detail="Email or password is incorrect"
            )
        touch_login(conn, user["id"])
        token = create_session(conn, user["id"])
    finally:
        conn.close()
    set_session_cookie(response, token)
    return {"ok": True, "email": user["email"], "vip_effective": effective_vip(user)}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    conn = get_db()
    try:
        delete_session(conn, request.cookies.get(SESSION_COOKIE))
    finally:
        conn.close()
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    return _public_user(require_user(request))


@router.get("/admin/validation-status")
def admin_validation_status(request: Request):
    require_owner(require_user(request))
    conn = get_db()
    try:
        btc, eth = settled_counts(conn)
        unlocked = validation_unlocked(conn)
    finally:
        conn.close()
    return {
        "validation_unlocked": unlocked,
        "btc_settled": btc,
        "eth_settled": eth,
        "required_settled_per_series": VALIDATION_REQUIRED_SETTLED,
    }


@router.post("/admin/unlock-validation")
def admin_unlock_validation(body: UnlockBody, request: Request):
    require_owner(require_user(request))
    if not body.unlock:
        raise HTTPException(
            status_code=400, detail="Pass {\"unlock\": true} to open the VIP gate"
        )
    conn = get_db()
    try:
        btc, eth = settled_counts(conn)
        if btc < VALIDATION_REQUIRED_SETTLED or eth < VALIDATION_REQUIRED_SETTLED:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Validation bar not met yet: "
                    f"BTC {btc}/{VALIDATION_REQUIRED_SETTLED} settled, "
                    f"ETH {eth}/{VALIDATION_REQUIRED_SETTLED} settled. "
                    "The gate opens after 100 settled windows per series "
                    "plus the out-of-sample review."
                ),
            )
        set_validation_unlocked(conn, True)
        granted = grant_vip_to_active_subscribers(conn)
    finally:
        conn.close()
    return {
        "ok": True,
        "validation_unlocked": True,
        "btc_settled": btc,
        "eth_settled": eth,
        "active_subscribers_granted_vip": granted,
    }


@router.post("/admin/grant-vip")
def admin_grant_vip(body: EmailBody, request: Request):
    require_owner(require_user(request))
    conn = get_db()
    try:
        # The VIP lock: no grant by any path while validation is locked.
        if not validation_unlocked(conn):
            raise HTTPException(
                status_code=409,
                detail=(
                    "VIP is locked until the 100-window validation passes. "
                    "No account can hold VIP status before then."
                ),
            )
        user = get_user_by_email(conn, body.email)
        if user is None:
            raise HTTPException(status_code=404, detail="No account with that email")
        set_vip(conn, user["id"], True, "admin")
    finally:
        conn.close()
    return {"ok": True, "email": normalize_email(body.email), "vip": True}


@router.post("/admin/revoke-vip")
def admin_revoke_vip(body: EmailBody, request: Request):
    require_owner(require_user(request))
    conn = get_db()
    try:
        user = get_user_by_email(conn, body.email)
        if user is None:
            raise HTTPException(status_code=404, detail="No account with that email")
        set_vip(conn, user["id"], False, None)
    finally:
        conn.close()
    return {"ok": True, "email": normalize_email(body.email), "vip": False}


@router.post("/admin/link-stripe")
def admin_link_stripe(body: LinkStripeBody, request: Request):
    """Store a Stripe customer id on a user so the webhook can find
    them. This is how a subscriber is linked before the checkout flow
    exists (checkout will set it automatically when it is built)."""
    require_owner(require_user(request))
    customer_id = (body.stripe_customer_id or "").strip()
    if not customer_id.startswith("cus_"):
        raise HTTPException(
            status_code=400, detail="Stripe customer ids start with cus_"
        )
    conn = get_db()
    try:
        user = get_user_by_email(conn, body.email)
        if user is None:
            raise HTTPException(status_code=404, detail="No account with that email")
        other = get_user_by_stripe_customer_id(conn, customer_id)
        if other is not None and other["id"] != user["id"]:
            raise HTTPException(
                status_code=409,
                detail="That Stripe customer is already linked to another account",
            )
        set_stripe_customer_id(conn, user["id"], customer_id)
    finally:
        conn.close()
    return {
        "ok": True,
        "email": normalize_email(body.email),
        "stripe_customer_id": customer_id,
    }
