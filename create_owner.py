"""Create the first owner account for BTC Oracle Pro.

Operator tool — run ONCE in the Render shell (or locally with
TURSO_DATABASE_URL / TURSO_AUTH_TOKEN exported). It is never imported
by tracker.py and cannot affect the running service.

The owner account is the only account that can use the /admin/*
endpoints (unlock validation, grant/revoke VIP, link a Stripe
customer). The script refuses if an owner already exists — a second
owner is made only by an existing owner editing the row, never here.

Usage (Render shell):
    python create_owner.py
Prompts for email + password on stdin; the password is never echoed
and never written to any file.
"""

import getpass
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth


def connect():
    """Same backend choice as the service: Turso when the env vars
    are set, local oracle.db otherwise."""
    url = os.environ.get("TURSO_DATABASE_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if url:
        if not token:
            raise SystemExit(
                "TURSO_DATABASE_URL is set but TURSO_AUTH_TOKEN is missing"
            )
        import libsql

        return libsql.connect(url, auth_token=token)
    return sqlite3.connect("oracle.db")


def main():
    conn = connect()
    try:
        auth.init_auth_tables(conn)
        row = conn.execute(
            "SELECT email FROM users WHERE is_owner = 1 LIMIT 1"
        ).fetchone()
        if row is not None:
            email = row[0] if not hasattr(row, "keys") else row["email"]
            print(f"An owner account already exists ({email}). Refusing.")
            return 1
        email = input("Owner email: ").strip()
        if not auth.valid_email(auth.normalize_email(email)):
            print("That is not a valid email address.")
            return 1
        password = getpass.getpass("Password (min 8 characters): ")
        confirm = getpass.getpass("Password again: ")
        if password != confirm:
            print("Passwords do not match.")
            return 1
        if len(password) < auth.MIN_PASSWORD_LEN:
            print(f"Password must be at least {auth.MIN_PASSWORD_LEN} characters.")
            return 1
        user = auth.create_user(
            conn, email, auth.hash_password(password), is_owner=True
        )
        print(f"Owner account created: {user['email']}")
        print("Sign in at /app with this email and password.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
