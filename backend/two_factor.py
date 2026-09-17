"""TOTP secrets are encrypted at rest; recovery and challenge tokens are
stored hashed. Ported from candlr's app/two_factor.py, adapted to this
project's raw-SQL db.py (no ORM session to hold a live row) and to bearer
tokens instead of cookies: begin_login() returns a challenge token in the
response body rather than setting an httpOnly cookie.
"""
import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
from cryptography.fernet import Fernet
from fastapi import HTTPException

import db

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
CHALLENGE_MAX_AGE = timedelta(minutes=5)


def now():
    return datetime.now(timezone.utc)


def cipher():
    key = hashlib.sha256(b"openusage-totp-v1\0" + SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def check_limit(row: dict):
    locked_until = row.get("locked_until")
    if locked_until and datetime.fromisoformat(locked_until) > now():
        raise HTTPException(429, "Too many attempts. Try again in five minutes.")


async def fail(user_id: int, row: dict, detail: str = "Invalid or already used code"):
    failures = row["failures"] + 1
    locked_until = (now() + timedelta(minutes=5)).isoformat() if failures >= 5 else None
    await db.record_2fa_failure(user_id, failures, locked_until)
    raise HTTPException(400, detail)


def totp_step(secret: str, code: str, last_step: int = -1):
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    current = int(now().timestamp()) // 30
    otp = pyotp.TOTP(secret)
    for step in (current, current - 1, current + 1):
        if step > last_step and hmac.compare_digest(otp.at(step * 30), code):
            return step
    return None


def recovery_codes() -> tuple[list[str], list[str]]:
    codes = [secrets.token_hex(8) for _ in range(10)]
    hashes = [digest(code) for code in codes]
    formatted = ["-".join(code[i : i + 4] for i in range(0, 16, 4)) for code in codes]
    return formatted, hashes


async def verify_code(user_id: int, row: dict, code: str):
    """Raises on failure (and records it); on success, persists the new
    last_step / consumed recovery code and returns."""
    check_limit(row)
    code = code.strip().replace(" ", "")
    secret = cipher().decrypt(row["secret"].encode()).decode()
    step = totp_step(secret, code, row["last_step"])
    if step is not None:
        await db.record_2fa_success(user_id, step, row["recovery_hashes"])
        return

    hashed = digest(code.replace("-", "").lower())
    remaining = list(row["recovery_hashes"])
    if hashed not in remaining:
        await fail(user_id, row)
    remaining.remove(hashed)
    await db.record_2fa_success(user_id, row["last_step"], remaining)


async def begin_login(user_id: int) -> str | None:
    """Returns a challenge token if the account has 2FA enabled, None
    otherwise. The caller returns this token to the client instead of a
    session token; it must be presented back to /auth/2fa/verify."""
    row = await db.ensure_2fa_row(user_id)
    if not row["enabled"]:
        return None
    check_limit(row)
    token = secrets.token_urlsafe(32)
    await db.set_2fa_challenge(user_id, digest(token), (now() + CHALLENGE_MAX_AGE).isoformat())
    return token
