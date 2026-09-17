import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://chatgpt.com",
    "Referer": "https://chatgpt.com/",
}

SESSION_URL = "https://chatgpt.com/api/auth/session"
# Undocumented backend-api endpoint the web app itself calls to decide which
# plan features to show — could disappear or change shape without notice.
ACCOUNTS_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
# Reports the Codex 5-hour/weekly usage windows bundled with a ChatGPT
# subscription — same undocumented, could-change-any-time caveat as above.
# Headers/shape reverse-engineered from the Codex CLI's own auth flow.
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

PLAN_LABELS = {
    "chatgptplusplan": "Plus",
    "chatgptproplan": "Pro",
    "chatgptteamplan": "Team",
    "chatgptenterpriseplan": "Enterprise",
    "chatgpteduplan": "Edu",
    "chatgptfreeplan": "Free",
}


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _serialize_cookies(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def _sync_cookies(client: httpx.AsyncClient, cookies: dict, on_config_update) -> None:
    """The auth.js session cookie can rotate on read, same as Google's Gemini
    session cookies — persist any new value so the next fetch doesn't start
    from a stale one."""
    changed = False
    for c in client.cookies.jar:
        if cookies.get(c.name) != c.value:
            cookies[c.name] = c.value
            changed = True
    if changed and on_config_update:
        await on_config_update({"chatgpt_cookies": _serialize_cookies(cookies)})


def _is_cloudflare_challenge(resp: httpx.Response) -> bool:
    """A blocked-by-Cloudflare response looks nothing like an OpenAI auth
    failure (it's an HTML challenge page, not JSON) but still surfaces as a
    403 -- worth telling apart so users aren't told to redo a login step
    that was never the problem. Cloudflare fingerprints the TLS/HTTP2
    handshake itself here, not just cookies, so there's no clearance cookie
    a solver could hand us to get past it from this client."""
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    return (
        resp.status_code == 403
        and "cloudflare" in resp.headers.get("server", "").lower()
        and "html" in resp.headers.get("content-type", "")
    )


def _pick_account(accounts: dict) -> dict | None:
    if not accounts:
        return None
    for acct in accounts.values():
        if (acct.get("account") or {}).get("account_user_role") == "account-owner":
            return acct
    return next(iter(accounts.values()), None)


def _normalize_limit(value: dict | None) -> dict | None:
    """Coerce a wham/usage window (snake_case or camelCase, epoch or
    reset-after-seconds) into the {utilization, resets_at} shape the other
    fetchers already use, so the frontend can render it the same way."""
    if not value or not isinstance(value, dict):
        return None

    used_percent = value.get("used_percent")
    if used_percent is None:
        used_percent = value.get("usedPercent")

    resets_at = value.get("reset_at") or value.get("resetsAt") or value.get("resets_at")
    reset_after_seconds = value.get("reset_after_seconds")

    if used_percent is None and resets_at is None and reset_after_seconds is None:
        return None

    if resets_at is None and reset_after_seconds is not None:
        resets_at = datetime.now(timezone.utc).timestamp() + float(reset_after_seconds)

    resets_iso = None
    if resets_at is not None:
        resets_iso = datetime.fromtimestamp(float(resets_at), tz=timezone.utc).isoformat()

    return {
        "utilization": float(used_percent) if used_percent is not None else None,
        "resets_at": resets_iso,
    }


async def _fetch_codex_usage(client: httpx.AsyncClient, auth_headers: dict, account_id: str | None) -> dict | None:
    headers = {**auth_headers, "OpenAI-Beta": "codex-1", "originator": "Codex Desktop"}
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    try:
        r = await client.get(WHAM_USAGE_URL, headers=headers)
        r.raise_for_status()
        body = r.json()
    except Exception:
        return None

    rate_limit = body.get("rate_limit") or body.get("rateLimits") or {}
    primary = _normalize_limit(rate_limit.get("primary_window") or rate_limit.get("primary"))
    secondary = _normalize_limit(rate_limit.get("secondary_window") or rate_limit.get("secondary"))
    if primary is None and secondary is None:
        return None

    usage = {}
    if primary:
        usage["five_hour"] = primary
    if secondary:
        usage["seven_day"] = secondary
    return usage


async def fetch_chatgpt(config: dict = None, on_config_update=None) -> dict:
    if config is None:
        config = {}
    cookies_raw = config.get("chatgpt_cookies")

    if not cookies_raw:
        return {"configured": False, "error": "Not configured"}

    cookies = parse_cookie_string(cookies_raw)
    timeout_config = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(cookies=cookies, timeout=timeout_config) as client:
        try:
            sess_r = await client.get(SESSION_URL, headers=HEADERS)
            sess_r.raise_for_status()
            session = sess_r.json()
            await _sync_cookies(client, cookies, on_config_update)

            access_token = session.get("accessToken")
            if not access_token:
                logger.warning("ChatGPT session response had no accessToken — session likely expired")
                return {
                    "configured": True,
                    "status": "error",
                    "error": "Session expired or invalid — please re-import cURL in Settings",
                }

            user = session.get("user") or {}
            auth_headers = {**HEADERS, "Authorization": f"Bearer {access_token}"}

            acct_r = await client.get(ACCOUNTS_CHECK_URL, headers=auth_headers)
            acct_r.raise_for_status()
            accounts_body = acct_r.json()
            account = _pick_account(accounts_body.get("accounts") or {})

            data: dict = {"email": user.get("email")}
            account_id = None

            if account:
                account_id = (account.get("account") or {}).get("account_id")
                entitlement = account.get("entitlement") or {}
                last_sub = account.get("last_active_subscription") or {}
                raw_plan = entitlement.get("subscription_plan")
                data["plan"] = PLAN_LABELS.get(raw_plan, raw_plan or "Free")
                data["has_active_subscription"] = entitlement.get("has_active_subscription")
                data["will_renew"] = last_sub.get("will_renew")
                data["expires_at"] = entitlement.get("expires_at")
            else:
                data["plan"] = "Free"

            usage = await _fetch_codex_usage(client, auth_headers, account_id)
            if usage:
                data.update(usage)

            return {"configured": True, "status": "ok", "data": data}
        except httpx.HTTPStatusError as e:
            if _is_cloudflare_challenge(e.response):
                return {
                    "configured": True,
                    "status": "error",
                    "error": "Blocked by Cloudflare's bot check (not an expired login) — "
                             "re-paste a fresh cURL right after loading chatgpt.com; "
                             "the clearance cookie only lasts a few hours",
                }
            if e.response.status_code in (401, 403):
                return {
                    "configured": True,
                    "status": "error",
                    "error": "Session expired or invalid — please re-import cURL in Settings",
                }
            return {"configured": True, "status": "error", "error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            logger.warning("ChatGPT fetch exception: %s", e)
            return {"configured": True, "status": "error", "error": str(e)}
