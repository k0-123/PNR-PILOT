"""Accounts, passwords and single-device sessions.

No public sign-up: the admin creates every account and hands the person a generated password.
Passwords are stored only as a salted PBKDF2 hash. Each user row keeps ONE active session id, so
a fresh sign-in (on any device) overwrites it and the previous device is signed out on its next
action. See app/ui.py (sign_in_gate) for how the dashboard enforces it.

The FLT admin (settings.flt_admin_email) is bootstrapped as an admin account the first time the
app starts, using APP_PASSWORD from .env (or a random password written to the log if unset).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass

from app.core.config import Settings
from app.core.db import Database, now

log = logging.getLogger(__name__)

_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"
# A generated password people can read out: no look-alike characters (0/O, 1/l/I).
_PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
_PW_LENGTH = 12


class UserError(Exception):
    """A bad request from the admin UI (duplicate email, unknown user, …)."""


@dataclass
class User:
    id: int
    email: str
    is_admin: bool
    created_at: str
    disabled_at: str | None
    last_seen_at: str | None

    @property
    def active(self) -> bool:
        return self.disabled_at is None


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    if not password:
        raise UserError("password must not be empty")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def generate_password() -> str:
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(_PW_LENGTH))


def _norm(email: str) -> str:
    return (email or "").strip().lower()


# ---------------------------------------------------------------- user CRUD
def _row_to_user(row) -> User:
    return User(id=row["id"], email=row["email"], is_admin=bool(row["is_admin"]),
                created_at=row["created_at"], disabled_at=row["disabled_at"],
                last_seen_at=row["last_seen_at"])


def get_user(db: Database, email: str) -> User | None:
    row = db.conn.execute("SELECT * FROM users WHERE email=?", (_norm(email),)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_id(db: Database, user_id: int) -> User | None:
    row = db.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def list_users(db: Database) -> list[User]:
    rows = db.conn.execute("SELECT * FROM users ORDER BY is_admin DESC, email").fetchall()
    return [_row_to_user(r) for r in rows]


def create_user(db: Database, email: str, *, is_admin: bool = False,
                password: str | None = None) -> tuple[User, str]:
    """Create an account. Returns (user, plaintext_password) — show the password once."""
    email = _norm(email)
    if "@" not in email or " " in email:
        raise UserError("enter a valid email address")
    if get_user(db, email):
        raise UserError(f"an account for {email} already exists")
    plaintext = password or generate_password()
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO users(email, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
            (email, hash_password(plaintext), 1 if is_admin else 0, now()))
    log.info("User created", extra={"email": email, "is_admin": is_admin})
    user = get_user_by_id(db, cur.lastrowid)
    assert user is not None
    return user, plaintext


def reset_password(db: Database, user_id: int, password: str | None = None) -> str:
    """Set a new password (generated if not given) and sign the user out. Returns the plaintext."""
    if get_user_by_id(db, user_id) is None:
        raise UserError("account not found")
    plaintext = password or generate_password()
    with db.tx() as c:
        c.execute("UPDATE users SET password_hash=?, session_id=NULL WHERE id=?",
                  (hash_password(plaintext), user_id))
    log.info("Password reset", extra={"user_id": user_id})
    return plaintext


def set_disabled(db: Database, user_id: int, disabled: bool) -> None:
    user = get_user_by_id(db, user_id)
    if user is None:
        raise UserError("account not found")
    if user.is_admin and disabled:
        raise UserError("the admin account cannot be disabled")
    with db.tx() as c:
        c.execute("UPDATE users SET disabled_at=?, session_id=CASE WHEN ? THEN NULL ELSE session_id END "
                  "WHERE id=?", (now() if disabled else None, 1 if disabled else 0, user_id))
    log.info("User %s", "disabled" if disabled else "enabled", extra={"user_id": user_id})


# --------------------------------------------------------- login & sessions
def authenticate_login(db: Database, email: str, password: str) -> User | None:
    """Check email + password. None if unknown, wrong or disabled (in constant time)."""
    row = db.conn.execute("SELECT * FROM users WHERE email=?", (_norm(email),)).fetchone()
    stored = row["password_hash"] if row else hash_password("_dummy_no_such_user_")
    ok = verify_password(password, stored)
    if not row or not ok or row["disabled_at"]:
        return None
    return _row_to_user(row)


def start_session(db: Database, user_id: int) -> str:
    """Begin a session, replacing any other device's. Returns the session id to store client-side."""
    session_id = secrets.token_urlsafe(24)
    with db.tx() as c:
        c.execute("UPDATE users SET session_id=?, session_at=?, last_seen_at=? WHERE id=?",
                  (session_id, now(), now(), user_id))
    return session_id


def session_valid(db: Database, user_id: int, session_id: str | None) -> bool:
    """True only while this exact session is still the user's active one (and not disabled)."""
    if not session_id:
        return False
    row = db.conn.execute("SELECT session_id, disabled_at FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or row["disabled_at"] or not row["session_id"]:
        return False
    if not hmac.compare_digest(row["session_id"], session_id):
        return False
    db.conn.execute("UPDATE users SET last_seen_at=? WHERE id=?", (now(), user_id))
    db.conn.commit()
    return True


def end_session(db: Database, user_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE users SET session_id=NULL WHERE id=?", (user_id,))


# ------------------------------------------------------------- bootstrap
def ensure_bootstrap_admin(db: Database, settings: Settings) -> None:
    """Create the first admin account (no admin yet), from APP_EMAIL or, failing that, the FLT
    admin email. Uses APP_PASSWORD if set, otherwise a random password written to the log."""
    if db.conn.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone():
        return
    email = _norm(settings.app_email) or _norm(settings.flt_admin_email)
    if not email:
        return
    pw = settings.app_password.get_secret_value() or None
    _, plaintext = create_user(db, email, is_admin=True, password=pw)
    if pw is None:
        log.warning("Bootstrap admin created for %s with generated password: %s "
                    "(set it from Manage users, then remove this from the log)", email, plaintext)
