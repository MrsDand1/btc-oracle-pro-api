"""Reset a BTC Oracle Pro account password (owner tool).

Operator tool — run in the Render shell (or locally with
TURSO_DATABASE_URL / TURSO_AUTH_TOKEN exported). There is no
self-serve password reset in v1 because there is no email service;
the owner resets passwords with this script. It is never imported
by tracker.py.

Usage:
    python reset_password.py user@example.com
Prompts for the new password twice on stdin (never echoed). All of
the user's sessions are deleted, so any other signed-in browser is
signed out at once.

The password is never written to any file.
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
    if len(sys.argv) < 2:
        print("Usage: python reset_password.py user@example.com")
        return 1
    email = auth.normalize_email(sys.argv[1])
    conn = connect()
    try:
        auth.init_auth_tables(conn)
        user = auth.get_user_by_email(conn, email)
        if user is None:
            print(f"No account with email {email}.")
            return 1
        password = getpass.getpass("New password (min 8 characters): ")
        confirm = getpass.getpass("New password again: ")
        if password != confirm:
            print("Passwords do not match.")
            return 1
        if len(password) < auth.MIN_PASSWORD_LEN:
            print(f"Password must be at least {auth.MIN_PASSWORD_LEN} characters.")
            return 1
        auth.set_password(conn, user["id"], auth.hash_password(password))
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ?", (user["id"],)
        )
        conn.commit()
        print(f"Password reset for {email}. All sessions signed out.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
