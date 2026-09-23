import asyncio
import base64
import io
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import pyotp
import qrcode
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Header, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Loaded before the local modules below so their own module-level os.getenv()
# calls (oidc.py, email_notifier.py, fetchers/chatgpt.py, ...) see it too.
# Resolved relative to this file rather than cwd, so it's found the same way
# whether uvicorn is started from the repo root or from backend/.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("openusage")

import db
from db import (
    init_db,
    create_user,
    get_user_by_username,
    get_user_by_id,
    create_session,
    get_user_by_session,
    delete_session,
    delete_all_sessions_for_user,
    get_user_services,
    get_all_user_services,
    get_user_service,
    add_user_service,
    update_user_service,
    delete_user_service,
    save_service_usage,
    get_latest_service_usage,
    verify_password,
    get_categories,
    create_category,
    update_category,
    delete_category,
    move_category,
    get_user_by_email,
    update_user_email,
    update_user_password,
    create_oidc_user,
    create_oidc_state,
    consume_oidc_state,
    create_password_reset_token,
    get_password_reset_token,
    delete_password_reset_token,
    get_service_notify_armed,
    set_service_notify_armed,
    add_push_subscription,
    delete_push_subscription,
)
import email_notifier
import oidc
import two_factor as mfa
import push_notifier
from thresholds import APPLICABLE_THRESHOLDS, extract_usage_percentages
from curl_parse import parse_curl, parse_gemini_curl, parse_openai_session_curl, parse_chatgpt_curl, parse_ollama_curl
from fetchers.claude import fetch_claude
from fetchers.gemini import fetch_gemini
from fetchers.deepseek import fetch_deepseek
from fetchers.copilot import fetch_copilot, start_device_flow, poll_device_flow
from fetchers.openai_api import fetch_openai
from fetchers.chatgpt import fetch_chatgpt
from fetchers.ollama_cloud import fetch_ollama_cloud
from fetchers.generic import fetch_generic, test_generic

ENABLE_ACCOUNT_CREATION = os.getenv("ENABLE_ACCOUNT_CREATION", "true").lower() in ("true", "1", "yes")
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")
PASSWORD_RESET_MAX_AGE = timedelta(hours=1)
DEFAULT_REFRESH_INTERVAL_MINUTES = 5
REFRESH_SCHEDULER_TICK_SECONDS = 60
# Under healthy operation each service refreshes within its configured window,
# so data older than this multiple can only mean live refreshes have been
# failing -- even across a process restart, where the in-memory trackers below
# start out empty.
STALE_DATA_MULTIPLIER = 3


SERVICE_FETCHERS = {
    "claude": fetch_claude,
    "gemini": fetch_gemini,
    "deepseek": fetch_deepseek,
    "copilot": fetch_copilot,
    "openai": fetch_openai,
    "chatgpt": fetch_chatgpt,
    "ollama_cloud": fetch_ollama_cloud,
    "custom": fetch_generic,
}

# Tracks the most recent live-fetch failure per service, so requests that hit
# the fast cached-data path (below) can still surface that the background
# refresh loop is failing, not just requests that trigger a live fetch themselves.
_last_fetch_error: dict[int, str] = {}

# Failed attempts are not written to service_usage_history, but they still need
# to respect the service's configured cadence. Without this timestamp, a failed
# endpoint with stale cached data would be retried every scheduler tick.
_last_fetch_attempt_at: dict[int, datetime] = {}

# Coalesces concurrent live-fetch requests for the same service into one
# in-flight task. Without this, a foreground force-refresh (e.g. the
# dashboard's "Refresh" button, or a poll re-triggered because the service is
# already erroring) can overlap with the background refresh loop's own pass
# for the same service. Each caller fetches its own independent copy of
# `user_service["config"]` from the DB, so two concurrent Gemini fetches each
# rotate the session cookie against their own stale snapshot and then race to
# persist it -- whichever write lands last silently discards the other's
# newer (still-valid) rotated cookie, so the next fetch replays an
# already-invalidated cookie and the session looks "expired" again. Routing
# every caller through the same Task removes the race entirely.
_inflight_fetches: dict[int, asyncio.Task] = {}


def _get_or_start_live_fetch(user_service: dict) -> asyncio.Task:
    svc_id = user_service["id"]
    task = _inflight_fetches.get(svc_id)
    if task is None or task.done():
        task = asyncio.create_task(_live_fetch_and_save(user_service))
        _inflight_fetches[svc_id] = task

        def _cleanup(_task: asyncio.Task, svc_id: int = svc_id) -> None:
            if _inflight_fetches.get(svc_id) is _task:
                _inflight_fetches.pop(svc_id, None)

        task.add_done_callback(_cleanup)
    return task


async def _check_thresholds_and_notify(user_service: dict, data: dict):
    """Fires a push notification the moment usage crosses a configured
    threshold, once per crossing -- see service_notify_state's "armed" flag
    in db.py for how the moment usage drops back below it re-arms the check.
    """
    svc_id = user_service["id"]
    svc_type = user_service["service_type"]
    thresholds_cfg = user_service.get("notify_thresholds") or {}
    if not thresholds_cfg:
        return

    percentages = extract_usage_percentages(svc_type, data)
    if not percentages:
        return

    for threshold_type in APPLICABLE_THRESHOLDS.get(svc_type, ()):
        threshold = thresholds_cfg.get(f"{threshold_type}_threshold")
        pct = percentages.get(threshold_type)
        if threshold is None or pct is None:
            continue

        armed = await get_service_notify_armed(svc_id, threshold_type)
        if pct >= threshold:
            if armed:
                try:
                    await push_notifier.send_push(
                        user_service["user_id"],
                        f"{user_service['name']}: {threshold_type} usage at {pct:.0f}%",
                        f"{threshold_type.capitalize()} usage has crossed your {threshold:.0f}% threshold.",
                        url="/",
                    )
                except push_notifier.PushNotifierError as e:
                    logger.info("Skipped threshold push for service id=%s: %s", svc_id, e)
                else:
                    # Only a successful delivery consumes this crossing. If
                    # every device failed (or none are subscribed), leave it
                    # armed so a later refresh can retry.
                    await set_service_notify_armed(svc_id, threshold_type, False)
        elif not armed:
            await set_service_notify_armed(svc_id, threshold_type, True)


async def _live_fetch_and_save(user_service: dict) -> dict:
    svc_id = user_service["id"]
    svc_type = user_service["service_type"]
    svc_name = user_service["name"]
    config = user_service.get("config", {})
    _last_fetch_attempt_at[svc_id] = datetime.now(timezone.utc)

    fetcher = SERVICE_FETCHERS.get(svc_type)
    if not fetcher:
        return {
            "id": svc_id,
            "service_type": svc_type,
            "name": svc_name,
            "result": {"configured": False, "error": f"Unknown service type: {svc_type}"},
        }

    async def _on_config_update(updates: dict):
        config.update(updates)
        await update_user_service(
            svc_id, user_service["user_id"], svc_name, config, user_service.get("category_id")
        )

    try:
        logger.info("Starting live fetch for %s (%s, id=%s)...", svc_name, svc_type, svc_id)
        if svc_type == "gemini":
            result = await fetcher(config, on_config_update=_on_config_update, service_id=svc_id)
        elif svc_type == "chatgpt":
            result = await fetcher(config, on_config_update=_on_config_update)
        else:
            result = await fetcher(config)

        if result.get("status") == "ok":
            data = result.get("data", {})
            await save_service_usage(svc_id, data)
            _last_fetch_error.pop(svc_id, None)
            try:
                await _check_thresholds_and_notify(user_service, data)
            except Exception:
                logger.exception("Threshold notify check failed for %s (id=%s)", svc_name, svc_id)
            logger.info("Live fetch succeeded for %s (id=%s)", svc_name, svc_id)
            return {"id": svc_id, "service_type": svc_type, "name": svc_name, "result": result}

        err_msg = result.get("error") or "Live refresh failed"
        _last_fetch_error[svc_id] = err_msg
        logger.warning("Live fetch failed for %s (id=%s): %s", svc_name, svc_id, err_msg)
        cached_data, fetched_at = await get_latest_service_usage(svc_id)
        if cached_data:
            return {
                "id": svc_id,
                "service_type": svc_type,
                "name": svc_name,
                "result": {
                    "configured": True,
                    "status": "ok",
                    "data": cached_data,
                    "cached": True,
                    "fetched_at": fetched_at,
                    "stale_error": err_msg,
                },
            }
        return {"id": svc_id, "service_type": svc_type, "name": svc_name, "result": result}
    except Exception as e:
        _last_fetch_error[svc_id] = str(e)
        logger.exception("Live fetch exception for %s (id=%s): %s", svc_name, svc_id, e)
        cached_data, fetched_at = await get_latest_service_usage(svc_id)
        if cached_data:
            return {
                "id": svc_id,
                "service_type": svc_type,
                "name": svc_name,
                "result": {
                    "configured": True,
                    "status": "ok",
                    "data": cached_data,
                    "cached": True,
                    "fetched_at": fetched_at,
                    "stale_error": str(e),
                },
            }
        return {
            "id": svc_id,
            "service_type": svc_type,
            "name": svc_name,
            "result": {"configured": True, "status": "error", "error": str(e)},
        }


def _trigger_background_live_fetch(user_service: dict):
    # Fire-and-forget: joins whatever fetch is already in flight for this
    # service instead of starting a redundant one.
    _get_or_start_live_fetch(user_service)


async def _fetch_and_save_user_service(
    user_service: dict, force_refresh: bool = False, max_age_seconds: int | None = None
) -> dict:
    svc_id = user_service["id"]
    svc_type = user_service["service_type"]
    svc_name = user_service["name"]
    if max_age_seconds is None:
        try:
            interval_minutes = int(
                user_service.get("refresh_interval_minutes", DEFAULT_REFRESH_INTERVAL_MINUTES)
            )
        except (TypeError, ValueError):
            interval_minutes = DEFAULT_REFRESH_INTERVAL_MINUTES
        max_age_seconds = max(1, interval_minutes) * 60

    # 1. If not force_refresh, check if we have any cached data
    if not force_refresh:
        inflight = _inflight_fetches.get(svc_id)
        if inflight is not None and not inflight.done():
            return await inflight

        last_attempt = _last_fetch_attempt_at.get(svc_id)
        attempt_age = (
            (datetime.now(timezone.utc) - last_attempt).total_seconds()
            if last_attempt is not None
            else None
        )
        attempt_is_due = attempt_age is None or attempt_age >= max_age_seconds
        cached_data, fetched_at = await get_latest_service_usage(svc_id)
        if cached_data and fetched_at:
            age = None
            try:
                fetch_time = datetime.fromisoformat(fetched_at)
                age = (datetime.now(timezone.utc) - fetch_time).total_seconds()
            except Exception:
                pass

            stale_error = _last_fetch_error.get(svc_id)
            if not stale_error and age is not None and age >= max_age_seconds * STALE_DATA_MULTIPLIER:
                stale_error = f"No successful refresh since {fetched_at}"

            if age is not None and age >= max_age_seconds and attempt_is_due:
                _trigger_background_live_fetch(user_service)

            return {
                "id": svc_id,
                "service_type": svc_type,
                "name": svc_name,
                "result": {
                    "configured": True,
                    "status": "ok",
                    "data": cached_data,
                    "cached": True,
                    "fetched_at": fetched_at,
                    "stale_error": stale_error,
                },
            }

        if not attempt_is_due and svc_id in _last_fetch_error:
            return {
                "id": svc_id,
                "service_type": svc_type,
                "name": svc_name,
                "result": {
                    "configured": True,
                    "status": "error",
                    "error": _last_fetch_error[svc_id],
                },
            }

    # 2. Force refresh or no cached data yet -> live fetch (joins an
    # in-flight fetch for this service if one is already running)
    return await _get_or_start_live_fetch(user_service)


async def _refresh_loop():
    await asyncio.sleep(5)
    while True:
        try:
            all_services = await get_all_user_services()
            if all_services:
                await asyncio.gather(
                    *(_fetch_and_save_user_service(svc) for svc in all_services)
                )
        except Exception:
            pass
        await asyncio.sleep(REFRESH_SCHEDULER_TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(_refresh_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Authentication dependency
async def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header"
        )
    token = authorization.replace("Bearer ", "").strip()
    user = await get_user_by_session(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session token"
        )
    return user


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/version")
async def version():
    return {"version": os.getenv("APP_VERSION", "dev")}


@app.get("/api/auth/config")
async def auth_config():
    return {"enable_account_creation": ENABLE_ACCOUNT_CREATION}


def _public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user.get("email"),
        "is_admin": bool(user.get("is_admin")),
    }


def _normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    email = email.strip().lower()
    if "@" not in email or " " in email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter a valid email address")
    return email


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = None


class AuthRequest(BaseModel):
    username: str
    password: str


class TwoFactorVerifyRequest(BaseModel):
    challenge_token: str
    code: str


@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    if not ENABLE_ACCOUNT_CREATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account creation is disabled"
        )
    username = req.username.strip().lower()
    if len(username) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Username must be at least 3 characters"
        )
    if len(req.password) < 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 4 characters"
        )
    email = _normalize_email(req.email)

    existing = await get_user_by_username(username)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Username is already taken"
        )
    if email and await get_user_by_email(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already registered"
        )

    user_id = await create_user(username, req.password, email)
    token = await create_session(user_id)
    user = await get_user_by_username(username)
    return {"token": token, "user": _public_user(user)}


@app.post("/api/auth/login")
async def login(req: AuthRequest):
    username = req.username.strip().lower()
    user = await get_user_by_username(username)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        )

    challenge_token = await mfa.begin_login(user["id"])
    if challenge_token:
        return {"requires_2fa": True, "challenge_token": challenge_token}

    token = await create_session(user["id"])
    return {"token": token, "user": _public_user(user)}


@app.post("/api/auth/2fa/verify")
async def two_factor_verify(req: TwoFactorVerifyRequest):
    row = await db.get_2fa_by_challenge_hash(mfa.digest(req.challenge_token))
    if not row or not row["enabled"]:
        raise HTTPException(status_code=401, detail="Sign in again to verify your code")
    if not row["challenge_expires_at"] or datetime.fromisoformat(row["challenge_expires_at"]) <= mfa.now():
        raise HTTPException(status_code=401, detail="Your sign-in attempt expired. Sign in again.")

    await mfa.verify_code(row["user_id"], row, req.code)
    await db.clear_2fa_challenge(row["user_id"])
    user = await get_user_by_id(row["user_id"])
    token = await create_session(user["id"])
    return {"token": token, "user": _public_user(user)}


@app.post("/api/auth/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if authorization:
        token = authorization.replace("Bearer ", "").strip()
        await delete_session(token)
    return {"status": "ok"}


@app.get("/api/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {"user": _public_user(user)}


class UpdateEmailRequest(BaseModel):
    email: str


@app.put("/api/auth/email")
async def update_email(req: UpdateEmailRequest, user: dict = Depends(get_current_user)):
    email = _normalize_email(req.email)
    existing = await get_user_by_email(email)
    if existing and existing["id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already registered")
    await update_user_email(user["id"], email)
    return {"ok": True}


class UpdatePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.put("/api/auth/password")
async def update_password(req: UpdatePasswordRequest, user: dict = Depends(get_current_user)):
    row = await db.ensure_2fa_row(user["id"])
    await _reauthenticate(user, row, req.current_password)
    if len(req.new_password) < 4:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must be at least 4 characters")
    await update_user_password(user["id"], req.new_password)
    token = await _rotate_session(user)
    return {"token": token, "user": _public_user(user)}


# ── OIDC / SSO ──

@app.get("/api/oidc/config")
async def oidc_config():
    return {
        "enabled": oidc.OIDC_ENABLED,
        "provider_name": oidc.OIDC_PROVIDER_NAME,
        "disable_password_login": oidc.OIDC_DISABLE_PASSWORD_LOGIN,
    }


@app.get("/api/oidc/authorize")
async def oidc_authorize():
    if not oidc.OIDC_ENABLED:
        raise HTTPException(status_code=400, detail="OIDC not enabled")
    state = await create_oidc_state()
    return RedirectResponse(oidc.build_auth_url(state))


@app.get("/api/oidc/callback")
async def oidc_callback(code: Optional[str] = None, state: Optional[str] = None):
    if not oidc.OIDC_ENABLED:
        raise HTTPException(status_code=400, detail="OIDC not enabled")
    try:
        if not code or not state or not await consume_oidc_state(state):
            raise HTTPException(status_code=400, detail="Invalid or expired sign-in attempt")

        userinfo = oidc.fetch_userinfo(code)
        identifier = userinfo.get(oidc.OIDC_IDENTIFIER_FIELD)
        if not identifier:
            raise HTTPException(
                status_code=400, detail=f"Field '{oidc.OIDC_IDENTIFIER_FIELD}' not found in user info"
            )

        user = await get_user_by_email(str(identifier))
        if not user:
            if not oidc.OIDC_AUTO_CREATE_USERS:
                raise HTTPException(status_code=403, detail="No account found for this identity")

            raw_email = userinfo.get("email", str(identifier))
            raw_username = (
                userinfo.get("preferred_username")
                or userinfo.get("name")
                or (raw_email.split("@")[0] if "@" in raw_email else raw_email)
            )
            username = str(raw_username).strip().lower()[:64] or "user"

            base, counter = username, 1
            while await get_user_by_username(username):
                username = f"{base}{counter}"
                counter += 1

            user_id = await create_oidc_user(username, str(identifier))
            user = await get_user_by_id(user_id)

        challenge_token = await mfa.begin_login(user["id"])
        if challenge_token:
            return RedirectResponse(f"/oidc-callback?challenge_token={challenge_token}")

        token = await create_session(user["id"])
        return RedirectResponse(f"/oidc-callback?token={token}")
    except HTTPException as e:
        return RedirectResponse(f"/login?oidc_error={quote(e.detail)}")


# ── Password reset ──

class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@app.get("/api/auth/password-reset-status")
async def password_reset_status():
    return {"enabled": bool(email_notifier.SMTP_ADDRESS)}


@app.post("/api/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest):
    """Always returns 200, regardless of whether the email is registered, so
    this endpoint can't be used to find out who has an account."""
    if not email_notifier.SMTP_ADDRESS:
        raise HTTPException(status_code=503, detail="Password reset is not configured")

    email = _normalize_email(req.email)
    user = await get_user_by_email(email)
    if user:
        token = await create_password_reset_token(user["id"])
        link = f"{SERVER_URL}/reset-password?token={token}"
        body = (
            "We received a request to reset your OpenUsage password.\n\n"
            f"Reset it here: {link}\n\n"
            "This link expires in 1 hour. If you didn't request this, you can ignore this email."
        )
        try:
            email_notifier.send(user["email"], "Reset your OpenUsage password", body)
        except email_notifier.EmailError as e:
            logger.error(f"Failed to send password reset email to {user['email']}: {e}")

    return {"message": "If that email is registered, a reset link has been sent."}


@app.post("/api/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    record = await get_password_reset_token(req.token)
    if not record:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has already been used")

    created_at = datetime.fromisoformat(record["created_at"])
    if datetime.now(timezone.utc) - created_at > PASSWORD_RESET_MAX_AGE:
        await delete_password_reset_token(req.token)
        raise HTTPException(status_code=400, detail="This reset link has expired")

    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    await update_user_password(record["user_id"], req.new_password)
    await delete_all_sessions_for_user(record["user_id"])
    await delete_password_reset_token(req.token)
    return {"ok": True}


# ── Two-factor authentication ──

class TwoFactorPasswordRequest(BaseModel):
    password: str


class TwoFactorCodeRequest(BaseModel):
    code: str


class TwoFactorManageRequest(BaseModel):
    password: str
    code: str


async def _reauthenticate(user: dict, row: dict, password: str):
    mfa.check_limit(row)
    if not verify_password(password, user["password_hash"]):
        await mfa.fail(user["id"], row, "Incorrect password")


async def _rotate_session(user: dict) -> str:
    await delete_all_sessions_for_user(user["id"])
    return await create_session(user["id"])


@app.get("/api/auth/2fa")
async def two_factor_status(user: dict = Depends(get_current_user)):
    row = await db.get_2fa(user["id"])
    return {
        "enabled": bool(row and row["enabled"]),
        "recovery_codes_remaining": len(row["recovery_hashes"]) if row else 0,
    }


@app.post("/api/auth/2fa/setup")
async def two_factor_setup(req: TwoFactorPasswordRequest, user: dict = Depends(get_current_user)):
    row = await db.ensure_2fa_row(user["id"])
    await _reauthenticate(user, row, req.password)
    if row["enabled"]:
        raise HTTPException(status_code=400, detail="Two-factor authentication is already enabled")

    secret = pyotp.random_base32()
    pending_secret = mfa.cipher().encrypt(secret.encode()).decode()
    await db.set_2fa_pending(user["id"], pending_secret, (mfa.now() + timedelta(minutes=10)).isoformat())

    uri = pyotp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name="OpenUsage")
    image = io.BytesIO()
    qrcode.make(uri).save(image, format="PNG")
    return {"secret": secret, "qr_code": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()}


@app.post("/api/auth/2fa/enable")
async def two_factor_enable(req: TwoFactorCodeRequest, user: dict = Depends(get_current_user)):
    row = await db.ensure_2fa_row(user["id"])
    mfa.check_limit(row)
    if row["enabled"] or not row["pending_secret"] or not row["pending_expires_at"] or \
            datetime.fromisoformat(row["pending_expires_at"]) <= mfa.now():
        raise HTTPException(status_code=400, detail="Start two-factor setup again")

    pending_secret = mfa.cipher().decrypt(row["pending_secret"].encode()).decode()
    step = mfa.totp_step(pending_secret, req.code.strip())
    if step is None:
        await mfa.fail(user["id"], row)

    codes, hashes = mfa.recovery_codes()
    await db.enable_2fa(user["id"], row["pending_secret"], hashes, step)
    token = await _rotate_session(user)
    return {"recovery_codes": codes, "token": token, "user": _public_user(user)}


@app.post("/api/auth/2fa/disable")
async def two_factor_disable(req: TwoFactorManageRequest, user: dict = Depends(get_current_user)):
    row = await db.ensure_2fa_row(user["id"])
    await _reauthenticate(user, row, req.password)
    if not row["enabled"]:
        raise HTTPException(status_code=400, detail="Two-factor authentication is not enabled")
    await mfa.verify_code(user["id"], row, req.code)
    await db.disable_2fa(user["id"])
    token = await _rotate_session(user)
    return {"token": token, "user": _public_user(user)}


@app.post("/api/auth/2fa/recovery-codes")
async def two_factor_recovery_codes(req: TwoFactorManageRequest, user: dict = Depends(get_current_user)):
    row = await db.ensure_2fa_row(user["id"])
    await _reauthenticate(user, row, req.password)
    if not row["enabled"]:
        raise HTTPException(status_code=400, detail="Two-factor authentication is not enabled")
    await mfa.verify_code(user["id"], row, req.code)
    codes, hashes = mfa.recovery_codes()
    await db.update_2fa_recovery_hashes(user["id"], hashes)
    return {"recovery_codes": codes}


# User Services CRUD Endpoints
@app.get("/api/user-services")
async def list_user_services(user: dict = Depends(get_current_user)):
    services = await get_user_services(user["id"])
    return services


class UserServiceCreate(BaseModel):
    service_type: str
    name: str
    config: dict
    category_id: Optional[int] = None
    notify_thresholds: Optional[dict] = None
    refresh_interval_minutes: int = Field(
        default=DEFAULT_REFRESH_INTERVAL_MINUTES, ge=1, le=10080
    )


@app.post("/api/user-services")
async def create_user_service(req: UserServiceCreate, user: dict = Depends(get_current_user)):
    if req.service_type not in SERVICE_FETCHERS:
        raise HTTPException(status_code=400, detail=f"Invalid service_type: {req.service_type}")
    svc_name = req.name.strip() or req.service_type.capitalize()
    svc_id = await add_user_service(
        user["id"],
        req.service_type,
        svc_name,
        req.config,
        req.category_id,
        req.notify_thresholds,
        req.refresh_interval_minutes,
    )
    return {"status": "ok", "id": svc_id}


class UserServiceUpdate(BaseModel):
    name: str
    config: dict
    category_id: Optional[int] = None
    # None means "not part of this update" so category moves, cURL refreshes,
    # and provider credential rotation preserve the existing thresholds.
    notify_thresholds: Optional[dict] = None
    # None likewise preserves the cadence for older clients and internal
    # config/category-only updates.
    refresh_interval_minutes: Optional[int] = Field(default=None, ge=1, le=10080)


@app.put("/api/user-services/{service_id}")
async def update_service(
    service_id: int, req: UserServiceUpdate, user: dict = Depends(get_current_user)
):
    existing = await get_user_service(service_id, user["id"])
    if not existing:
        raise HTTPException(status_code=404, detail="Service not found")
    svc_name = req.name.strip() or existing["service_type"].capitalize()
    effective_thresholds = (
        existing.get("notify_thresholds", {})
        if req.notify_thresholds is None
        else req.notify_thresholds
    )
    effective_refresh_interval = (
        existing.get("refresh_interval_minutes", DEFAULT_REFRESH_INTERVAL_MINUTES)
        if req.refresh_interval_minutes is None
        else req.refresh_interval_minutes
    )
    await update_user_service(
        service_id,
        user["id"],
        svc_name,
        req.config,
        req.category_id,
        req.notify_thresholds,
        req.refresh_interval_minutes,
    )
    _last_fetch_error.pop(service_id, None)
    updated_svc = {
        **existing,
        "name": svc_name,
        "config": req.config,
        "category_id": req.category_id,
        "notify_thresholds": effective_thresholds,
        "refresh_interval_minutes": effective_refresh_interval,
    }
    _trigger_background_live_fetch(updated_svc)
    return {"status": "ok"}


@app.delete("/api/user-services/{service_id}")
async def delete_service(service_id: int, user: dict = Depends(get_current_user)):
    existing = await get_user_service(service_id, user["id"])
    if not existing:
        raise HTTPException(status_code=404, detail="Service not found")
    await delete_user_service(service_id, user["id"])
    _last_fetch_error.pop(service_id, None)
    _last_fetch_attempt_at.pop(service_id, None)
    return {"status": "ok"}


# Category CRUD Endpoints (home-page sections that services can be assigned to)
@app.get("/api/categories")
async def list_categories(user: dict = Depends(get_current_user)):
    return await get_categories(user["id"])


class CategoryCreate(BaseModel):
    name: str


@app.post("/api/categories")
async def create_category_endpoint(req: CategoryCreate, user: dict = Depends(get_current_user)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    cat_id = await create_category(user["id"], name)
    return {"status": "ok", "id": cat_id}


class CategoryUpdate(BaseModel):
    name: str


@app.put("/api/categories/{category_id}")
async def update_category_endpoint(
    category_id: int, req: CategoryUpdate, user: dict = Depends(get_current_user)
):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    updated = await update_category(category_id, user["id"], name)
    if not updated:
        raise HTTPException(status_code=404, detail="Category not found")
    return {"status": "ok"}


@app.delete("/api/categories/{category_id}")
async def delete_category_endpoint(category_id: int, user: dict = Depends(get_current_user)):
    await delete_category(category_id, user["id"])
    return {"status": "ok"}


class CategoryMove(BaseModel):
    direction: str  # "up" or "down"


@app.post("/api/categories/{category_id}/move")
async def move_category_endpoint(
    category_id: int, req: CategoryMove, user: dict = Depends(get_current_user)
):
    if req.direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction must be 'up' or 'down'")
    moved = await move_category(category_id, user["id"], req.direction)
    if not moved:
        raise HTTPException(status_code=400, detail="Cannot move category further in that direction")
    return {"status": "ok"}


# Usage Endpoints
@app.get("/api/usage")
async def get_all_usage(force: bool = False, user: dict = Depends(get_current_user)):
    user_services = await get_user_services(user["id"])
    if not user_services:
        return []
    results = await asyncio.gather(
        *(_fetch_and_save_user_service(s, force_refresh=force) for s in user_services)
    )
    return list(results)


@app.get("/api/usage/{service_id}")
async def get_one_usage(
    service_id: int, force: bool = False, user: dict = Depends(get_current_user)
):
    svc = await get_user_service(service_id, user["id"])
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")
    return await _fetch_and_save_user_service(svc, force_refresh=force)


# Import cURL Helper Endpoints
class CurlItem(BaseModel):
    curl: str


@app.post("/api/import-curl/claude")
async def claude_import_curl(item: CurlItem):
    try:
        parsed = parse_curl(item.curl)
        return {"status": "ok", "org_url": parsed["url"], "cookies": parsed["cookies"]}
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/import-curl/gemini")
async def gemini_import_curl(item: CurlItem):
    try:
        parsed = parse_gemini_curl(item.curl)
        return {
            "status": "ok",
            "cookies": parsed["cookies"],
            "at_token": parsed["at_token"],
            "bl": parsed["bl"],
            "sid": parsed["sid"],
        }
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/import-curl/openai")
async def openai_import_curl(item: CurlItem):
    try:
        parsed = parse_openai_session_curl(item.curl)
        return {"status": "ok", "session_key": parsed["session_key"], "org_id": parsed["org_id"]}
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/import-curl/chatgpt")
async def chatgpt_import_curl(item: CurlItem):
    try:
        parsed = parse_chatgpt_curl(item.curl)
        return {"status": "ok", "cookies": parsed["cookies"]}
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/import-curl/ollama_cloud")
async def ollama_cloud_import_curl(item: CurlItem):
    try:
        parsed = parse_ollama_curl(item.curl)
        return {"status": "ok", "cookies": parsed["cookies"]}
    except ValueError as e:
        return {"status": "error", "error": str(e)}


class GenericTestRequest(BaseModel):
    config: dict


@app.post("/api/test-custom")
async def test_custom_endpoint(req: GenericTestRequest, user: dict = Depends(get_current_user)):
    return await test_generic(req.config)


# Copilot Device Flow Endpoints
@app.post("/api/copilot/device/start")
async def copilot_device_start():
    return await start_device_flow()


class CopilotPollItem(BaseModel):
    device_code: Optional[str] = None


@app.post("/api/copilot/device/poll")
async def copilot_device_poll(item: Optional[CopilotPollItem] = None):
    dev_code = item.device_code if item else None
    return await poll_device_flow(dev_code)


# Push notification endpoints. There's no separate "enabled" flag on the
# user -- subscribing a device on the Settings page is what turns push
# notifications on for that user; unsubscribing every device turns it off.
@app.get("/api/push/public-key")
async def push_public_key():
    return {"public_key": push_notifier.VAPID_PUBLIC_KEY if push_notifier.is_configured() else None}


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionCreate(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys


class PushSubscriptionDelete(BaseModel):
    endpoint: str


@app.post("/api/push/subscribe")
async def push_subscribe(req: PushSubscriptionCreate, user: dict = Depends(get_current_user)):
    if not push_notifier.is_configured():
        raise HTTPException(status_code=400, detail="Push notifications are not configured on this instance")
    await add_push_subscription(user["id"], req.endpoint, req.keys.p256dh, req.keys.auth)
    return {"status": "ok"}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(req: PushSubscriptionDelete, user: dict = Depends(get_current_user)):
    await delete_push_subscription(user["id"], req.endpoint)
    return {"status": "ok"}


@app.post("/api/push/test")
async def push_test(user: dict = Depends(get_current_user)):
    try:
        await push_notifier.send_push(
            user["id"], "OpenUsage test notification", "If you can see this, push notifications are working."
        )
    except push_notifier.PushNotifierError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok"}


# Serve the built frontend. Mounted last so it never shadows the /api/ routes
# above; html=True gives directory index.html lookup and a 404.html fallback,
# matching the previous nginx try_files/error_page behavior.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
