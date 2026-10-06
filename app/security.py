"""Authentication, sessions, MFA (TOTP), lockout, role checks and quotas."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass

import pyotp

from . import db
from .config import Settings

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


class AuthError(Exception):
    """Authentication failed (HTTP 401)."""


class PermissionDenied(Exception):
    """Authenticated but not allowed (HTTP 403)."""


class QuotaExceeded(Exception):
    """Quota / rate / concurrency limit hit (HTTP 429)."""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    mfa_enabled: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ---------- password hashing (stdlib scrypt, per-user salt) ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def validate_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters.")


# ---------- users ----------

def create_user(username: str, password: str, role: str = "user") -> int:
    if not USERNAME_RE.match(username):
        raise ValueError("Username must be 3-32 chars: letters, digits, _ . -")
    if role not in ("admin", "user"):
        raise ValueError("Invalid role")
    validate_password(password)
    if db.one("SELECT id FROM users WHERE username = ?", (username,)):
        raise ValueError("Username already exists")
    return db.execute(
        "INSERT INTO users(username, password_hash, role, active, created_at) VALUES (?,?,?,1,?)",
        (username, hash_password(password), role, time.time()),
    )


def ensure_admin(settings: Settings) -> None:
    if db.one("SELECT id FROM users LIMIT 1"):
        return
    if not settings.admin_password:
        raise RuntimeError("No users exist yet: set ADMIN_PASSWORD in .env for the first start.")
    try:
        create_user(settings.admin_username, settings.admin_password, "admin")
    except ValueError as exc:
        raise RuntimeError(f"ADMIN_PASSWORD / ADMIN_USERNAME in .env is not valid: {exc} "
                           "Fix it in .env (or delete .env and run .\\scripts\\setup.ps1 again).") from exc


def set_active(user_id: int, active: bool) -> None:
    db.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
    if not active:  # deactivation kills every live session immediately
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_role(user_id: int, role: str) -> None:
    if role not in ("admin", "user"):
        raise ValueError("Invalid role")
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def reset_password(user_id: int, password: str) -> None:
    validate_password(password)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def list_users() -> list[dict]:
    return db.query(
        "SELECT id, username, role, active, totp_secret IS NOT NULL AS mfa_enabled, created_at FROM users ORDER BY id"
    )


# ---------- MFA ----------

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="DocAssistant POC")


def enable_mfa(user_id: int, secret: str, code: str) -> None:
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        raise AuthError("Invalid MFA code")
    db.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (secret, user_id))


def disable_mfa(user_id: int) -> None:
    db.execute("UPDATE users SET totp_secret = NULL WHERE id = ?", (user_id,))


# ---------- login / sessions ----------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _locked_out(username: str, settings: Settings) -> bool:
    since = time.time() - settings.login_lockout_minutes * 60
    row = db.one("SELECT COUNT(*) AS n FROM login_failures WHERE username = ? AND at > ?", (username.lower(), since))
    return row["n"] >= settings.login_max_failures


def login(username: str, password: str, totp_code: str | None, settings: Settings) -> str:
    """Returns an opaque session token. Same generic error for every failure reason."""
    username = (username or "").strip()
    if _locked_out(username, settings):
        db.log_usage(None, "auth.locked", {"username": username}, ok=False)
        raise QuotaExceeded("Too many failed attempts. Try again later.")
    row = db.one("SELECT * FROM users WHERE username = ?", (username,))
    ok = bool(row) and verify_password(password or "", row["password_hash"]) and row["active"] == 1
    if ok and row["totp_secret"]:
        ok = bool(totp_code) and pyotp.TOTP(row["totp_secret"]).verify(totp_code, valid_window=1)
    if not ok:
        db.execute("INSERT INTO login_failures(username, at) VALUES (?, ?)", (username.lower(), time.time()))
        db.log_usage(row["id"] if row else None, "auth.failed", {"username": username}, ok=False)
        raise AuthError("Invalid username, password or MFA code")
    db.execute("DELETE FROM login_failures WHERE username = ?", (username.lower(),))
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (_token_hash(token), row["id"], now, now + settings.session_hours * 3600),
    )
    db.log_usage(row["id"], "auth.login")
    return token


def logout(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def user_from_token(token: str | None) -> User:
    """Validates on EVERY request: token exists, not expired, user still active."""
    if not token:
        raise AuthError("Not authenticated")
    row = db.one(
        """SELECT u.id, u.username, u.role, u.active, u.totp_secret, s.expires_at
           FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?""",
        (_token_hash(token),),
    )
    if not row or row["expires_at"] < time.time() or row["active"] != 1:
        raise AuthError("Session invalid or expired")
    return User(row["id"], row["username"], row["role"], bool(row["totp_secret"]))


def require_admin(user: User) -> None:
    if not user.is_admin:
        raise PermissionDenied("Admin role required")


# ---------- quotas & concurrency ----------

def check_question_quota(user: User, settings: Settings) -> None:
    since = time.time() - 86400
    row = db.one("SELECT COUNT(*) AS n FROM usage_log WHERE user_id = ? AND kind = 'chat.ask' AND created_at > ?", (user.id, since))
    if row["n"] >= settings.daily_question_quota:
        raise QuotaExceeded("Daily question quota reached")


class ConcurrencyGate:
    """Bounded concurrency for expensive work; rejects quickly instead of queueing forever."""

    def __init__(self, limit: int, wait_seconds: float = 20.0):
        self._sem = threading.BoundedSemaphore(limit)
        self._wait = wait_seconds

    def __enter__(self):
        if not self._sem.acquire(timeout=self._wait):
            raise QuotaExceeded("Server busy, please retry shortly")
        return self

    def __exit__(self, *exc):
        self._sem.release()
