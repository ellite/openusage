import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

import httpx

from .gemini_diagnostics import (
    attempt_context, event, numeric_code, redirect_kind,
    response_summary, rotation_body_summary,
)

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://gemini.google.com",
    "Referer": "https://gemini.google.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

BATCHEXECUTE_URL = "https://gemini.google.com/_/BardChatUi/data/batchexecute"
ROTATE_COOKIES_URL = "https://accounts.google.com/RotateCookies"
DEFAULT_BL = "boq_assistant-bard-web-server_20260730.01_p1"

# Reverse-engineered from the Gemini app's own "Settings -> Usage limits" panel,
# which calls this same RPC with an empty argument list.
USAGE_RPC_ID = "jSf9Qc"


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _parse_batchexecute(text: str):
    """Google's batchexecute format: an anti-hijacking `)]}'` line, then
    repeating (byte-length line, JSON-array chunk) pairs. The declared length
    doesn't line up byte-for-byte with the JSON payload in practice, so rather
    than slicing on it, use it only to skip past the digit line and let the
    JSON decoder itself find where each chunk actually ends."""
    lines = text.split("\n")
    if lines and lines[0].startswith(")]}'"):
        lines = lines[1:]
    body = "\n".join(lines)

    decoder = json.JSONDecoder()
    pos = 0
    n = len(body)
    while pos < n:
        while pos < n and (body[pos].isspace() or body[pos].isdigit()):
            pos += 1
        if pos >= n:
            break
        try:
            parsed, end = decoder.raw_decode(body, pos)
        except json.JSONDecodeError:
            break
        pos = end
        for item in parsed:
            if isinstance(item, list) and len(item) >= 3 and item[0] == "wrb.fr":
                status_code = item[5] if len(item) > 5 else None
                yield item[1], item[2], status_code


def _find_timestamp(node):
    """Recursively find a [seconds, nanos] protobuf Timestamp pair."""
    if isinstance(node, list):
        if (
            len(node) == 2
            and isinstance(node[0], (int, float))
            and isinstance(node[1], (int, float))
            and node[0] > 1_000_000_000
        ):
            return node[0]
        for child in node:
            found = _find_timestamp(child)
            if found:
                return found
    return None


def _serialize_cookies(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _cookie_jar(cookies: dict) -> httpx.Cookies:
    # A dict creates domainless cookies in httpx. Google's .google.com
    # replacements then coexist with those stale originals on the wire.
    jar = httpx.Cookies()
    for name, value in cookies.items():
        jar.set(name, value, domain=".google.com", path="/")
    return jar


async def _sync_and_save_cookies(client: httpx.AsyncClient, current_cookies: dict, on_config_update) -> dict:
    latest = {}
    for c in client.cookies.jar:
        latest[c.name] = c.value
    changed = latest != current_cookies
    changed_names = sorted(k for k, v in latest.items() if current_cookies.get(k) != v)
    removed_names = sorted(current_cookies.keys() - latest.keys())
    current_cookies.clear()
    current_cookies.update(latest)
    if changed and on_config_update:
        await on_config_update({"gemini_cookies": _serialize_cookies(current_cookies)})
    event("cookies", "saved" if changed and on_config_update else "unchanged" if not changed else "not_persisted",
          changed_names=changed_names, removed_names=removed_names)
    return current_cookies


async def _rotate_1psidts(cookies: dict) -> dict | None:
    """Attempt renewal. HTTP 200 may update only auxiliary SIDCC cookies;
    it does not establish that Google renewed __Secure-1PSIDTS.
    Returns issued values, or None when Google rejects the session with 401.
    """
    timeout_config = httpx.Timeout(10.0, connect=5.0)
    event("rotation", "started")
    async with httpx.AsyncClient(cookies=_cookie_jar(cookies), timeout=timeout_config) as client:
        r = await client.post(
            ROTATE_COOKIES_URL,
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Content-Type": "application/json",
                "Origin": "https://accounts.google.com",
            },
            content='[000,"-0000000000000000000"]',
        )
        rotated = {c.name: c.value for c in r.cookies.jar}
        renewed = bool(rotated.get("__Secure-1PSIDTS") and rotated["__Secure-1PSIDTS"] != cookies.get("__Secure-1PSIDTS"))
        event("rotation", "response", warning=r.status_code != 200 or not renewed,
              **response_summary(r), **rotation_body_summary(r.text),
              issued_names=sorted(rotated),
              changed_names=sorted(k for k, v in rotated.items() if cookies.get(k) != v),
              auth_cookie_renewed=renewed,
              reason="auth_cookie_changed" if renewed else "authentication_rejected" if r.status_code == 401
              else "rate_limited" if r.status_code == 429 else "http_error" if r.status_code >= 400
              else "redirect" if 300 <= r.status_code < 400 else "no_auth_cookie_change_reason_unspecified")
        if r.status_code == 401:
            return None
        r.raise_for_status()
        rotated = {c.name: c.value for c in r.cookies.jar}
        if rotated.get("__Secure-1PSIDTS") and rotated["__Secure-1PSIDTS"] != cookies.get("__Secure-1PSIDTS"):
            logger.info("Google renewed Gemini __Secure-1PSIDTS")
        else:
            logger.warning(
                "Gemini RotateCookies HTTP %s did not renew __Secure-1PSIDTS; "
                "returned cookie names: %s. This is not proof of session renewal.",
                r.status_code, ", ".join(sorted(rotated)) or "none",
            )
        return rotated


BUCKET_TYPE_LABELS = {1: "Session", 2: "Weekly"}


async def fetch_gemini(config: dict = None, on_config_update=None, *, service_id=None) -> dict:
    context = {"attempt": uuid.uuid4().hex[:12], "service_id": service_id,
               "started": time.monotonic(), "stage": "start"}
    token = attempt_context.set(context)
    try:
        event("fetch", "started")

        async def save(updates):
            event("persistence", "started", fields=sorted(updates))
            try:
                await on_config_update(updates)
            except Exception as exc:
                event("persistence", "failed", warning=True, error_type=type(exc).__name__)
                raise
            event("persistence", "saved", fields=sorted(updates))

        result = await _fetch_gemini(config, save if on_config_update else None)
        event("fetch", result.get("status", "not_configured"),
              warning=result.get("status") != "ok", last_stage=context["stage"],
              limit_buckets=len(result.get("data", {}).get("limits", [])))
        return result
    except BaseException as exc:
        event("fetch", "aborted", warning=True, last_stage=context["stage"], error_type=type(exc).__name__)
        raise
    finally:
        attempt_context.reset(token)


async def _fetch_gemini(config: dict = None, on_config_update=None) -> dict:
    if config is None:
        config = {}
    cookies_raw = config.get("gemini_cookies")

    if not cookies_raw:
        logger.warning("Gemini fetch called but no cookies configured")
        return {"configured": False, "error": "Not configured"}

    cookies = parse_cookie_string(cookies_raw)
    logger.info("Starting Gemini fetch with %d cookies", len(cookies))
    event("credentials", "loaded", cookie_count=len(cookies),
          auth_cookie_present={name: bool(cookies.get(name)) for name in (
              "SID", "__Secure-1PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS")},
          cached_page_token=bool(config.get("gemini_at_token")),
          persistence_enabled=on_config_update is not None)
    renewal_unavailable = False

    if "__Secure-1PSID" in cookies:
        try:
            rotated = await _rotate_1psidts(cookies)
        except Exception as e:
            event("rotation", "failed_continuing", warning=True, error_type=type(e).__name__)
            rotated = {}
        if rotated is None:
            logger.warning("Gemini RotateCookies returned 401 — continuing, but session is likely dead")
            rotated = {}
        renewal_unavailable = not (
            rotated.get("__Secure-1PSIDTS")
            and rotated["__Secure-1PSIDTS"] != cookies.get("__Secure-1PSIDTS")
        )
        new_values = {k: v for k, v in rotated.items() if cookies.get(k) != v}
        if new_values:
            cookies.update(new_values)
            logger.info("Updated Gemini cookies: %s", ", ".join(sorted(new_values)))
            if on_config_update:
                await on_config_update({"gemini_cookies": _serialize_cookies(cookies)})
    else:
        event("rotation", "skipped", warning=True, reason="missing___Secure-1PSID")

    at_token = config.get("gemini_at_token")
    bl = config.get("gemini_bl") or DEFAULT_BL
    sid = config.get("gemini_sid") or "0"

    def session_error(message: str) -> str:
        if renewal_unavailable:
            return (
                f"{message}. Google did not renew the authentication cookie. "
                "If imported from Brave, Chrome, or Edge, sign in to Gemini in Firefox "
                "and import a fresh cURL in Settings; device-bound browser sessions "
                "cannot be renewed from copied cookies."
            )
        return f"{message} — please re-import cURL in Settings"

    timeout_config = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(cookies=_cookie_jar(cookies), timeout=timeout_config) as client:
        try:
            async def _refresh_page_tokens():
                """Re-derive at/bl/sid from the app page. Returns an error
                message, or None on success (nonlocals updated)."""
                nonlocal at_token, bl, sid
                event("page", "started")
                logger.info("Fetching https://gemini.google.com/app to extract session tokens...")
                page = await client.get(
                    "https://gemini.google.com/app",
                    headers=HEADERS,
                    follow_redirects=False,
                )
                event("page", "response", **response_summary(page))
                if page.status_code in (301, 302, 303, 307, 308):
                    loc = page.headers.get("location", "")
                    event("page", "redirected", warning=True, destination=redirect_kind(loc))
                    return session_error("Session expired or flagged by Google")
                page.raise_for_status()
                await _sync_and_save_cookies(client, cookies, on_config_update)

                event("page", "extracting_tokens")
                html = page.text
                at_token = None

                at_match = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', html)
                if at_match:
                    at_token = at_match.group(1)
                else:
                    af_match = re.search(r'"(AFWLbD[^\"]+)"', html)
                    if af_match:
                        at_token = af_match.group(1)
                    else:
                        wiz_match = re.search(r'WIZ_global_data\s*=\s*(\{.+?\});', html)
                        if wiz_match:
                            try:
                                wiz_data = json.loads(wiz_match.group(1))
                                for k, v in wiz_data.items():
                                    if isinstance(v, str) and v.startswith("AFWLbD"):
                                        at_token = v
                                        break
                                    if k == "SNlM0e" and v:
                                        at_token = v
                                        break
                            except Exception:
                                pass

                bl_match = re.search(r'"cfb2h"\s*:\s*"([^"]+)"', html)
                sid_match = re.search(r'"FdrFJe"\s*:\s*"(-?\d+)"', html)
                if bl_match:
                    bl = bl_match.group(1)
                if sid_match:
                    sid = sid_match.group(1)

                event("page", "tokens_extracted" if at_token else "token_missing",
                      warning=not bool(at_token), at_found=bool(at_token),
                      bl_found=bool(bl_match), sid_found=bool(sid_match))

                if not at_token:
                    logger.warning("No session token found in page — session may be expired")
                    return session_error("Session token not found")
                return None

            rpc_attempt = 0

            async def _run_usage_rpc():
                nonlocal rpc_attempt
                rpc_attempt += 1
                event("rpc", "started", rpc_attempt=rpc_attempt, rpc_id=USAGE_RPC_ID)
                params = {
                    "rpcids": USAGE_RPC_ID,
                    "source-path": "/app",
                    "bl": bl,
                    "f.sid": sid,
                    "hl": "en",
                    "_reqid": "1000001",
                    "rt": "c",
                }
                body = {
                    "f.req": json.dumps([[[USAGE_RPC_ID, "[]", None, "generic"]]], separators=(",", ":")),
                    "at": at_token,
                }
                r = await client.post(
                    BATCHEXECUTE_URL,
                    params=params,
                    data=body,
                    headers={
                        **HEADERS,
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "X-Same-Domain": "1",
                    },
                )
                event("rpc", "response", rpc_attempt=rpc_attempt, **response_summary(r))
                r.raise_for_status()
                await _sync_and_save_cookies(client, cookies, on_config_update)

                event("rpc", "decoding", rpc_attempt=rpc_attempt)
                payload = None
                rpc_status_code = None
                matched = False
                for item in _parse_batchexecute(r.text):
                    rpcid = item[0]
                    inner = item[1]
                    st_code = item[2] if len(item) > 2 else None
                    if rpcid == USAGE_RPC_ID:
                        matched = True
                        if inner:
                            payload = json.loads(inner)
                        elif st_code:
                            rpc_status_code = st_code
                        break
                code = numeric_code(rpc_status_code)
                event("rpc", "parsed", rpc_attempt=rpc_attempt, matched_rpc=matched,
                      payload_present=payload is not None, rpc_code=code,
                      rpc_reason={7: "permission_denied", 16: "unauthenticated", 8: "resource_exhausted",
                                  14: "unavailable"}.get(code, "unspecified"),
                      payload_items=len(payload) if isinstance(payload, list) else None)
                return payload, rpc_status_code

            used_cached_token = bool(at_token)
            if not at_token:
                err = await _refresh_page_tokens()
                if err:
                    return {"configured": True, "status": "error", "error": err}

            event("rpc", "token_selected", source="cached" if used_cached_token else "page")
            payload, rpc_status_code = await _run_usage_rpc()

            if (payload is None or len(payload) < 2) and used_cached_token:
                # The stored at-token expires independently of the cookies; a
                # fresh one can be minted from the page as long as the cookies
                # themselves are still valid.
                logger.info("Cached Gemini at-token was rejected — refreshing tokens from page and retrying once")
                event("rpc", "retrying", reason="cached_token_empty_or_rejected")
                err = await _refresh_page_tokens()
                if err:
                    return {"configured": True, "status": "error", "error": err}
                payload, rpc_status_code = await _run_usage_rpc()

            if payload is None or len(payload) < 2:
                if rpc_status_code:
                    event("rpc", "failed", warning=True, reason="google_rpc_error",
                          rpc_code=numeric_code(rpc_status_code))
                    error = session_error("Gemini denied access after refreshing session tokens")
                else:
                    logger.warning("Gemini batchexecute RPC returned empty payload")
                    error = session_error("Gemini returned no usage data after refreshing session tokens")
                return {
                    "configured": True,
                    "status": "error",
                    "error": error,
                }

            limits = []
            for bucket in payload[1]:
                bucket_type = bucket[2] if len(bucket) > 2 else None
                label = BUCKET_TYPE_LABELS.get(bucket_type)
                if label is None:
                    continue
                ts = _find_timestamp(bucket)
                resets_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
                limits.append({
                    "id": bucket[0],
                    "percent_used": round((bucket[1] or 0) * 100, 2),
                    "resets_at": resets_at,
                    "label": label,
                })
            limits.sort(key=lambda x: x["resets_at"] or "")

            if on_config_update:
                token_updates = {}
                if at_token != config.get("gemini_at_token"):
                    token_updates["gemini_at_token"] = at_token
                if bl != config.get("gemini_bl"):
                    token_updates["gemini_bl"] = bl
                if sid != config.get("gemini_sid"):
                    token_updates["gemini_sid"] = sid
                if token_updates:
                    await on_config_update(token_updates)

            logger.info("Gemini fetch succeeded: retrieved %d limit buckets", len(limits))
            return {"configured": True, "status": "ok", "data": {"limits": limits}}
        except httpx.HTTPStatusError as e:
            event(attempt_context.get()["stage"] if attempt_context.get() else "http",
                  "http_error", warning=True, http_status=e.response.status_code)
            return {"configured": True, "status": "error", "error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            event(attempt_context.get()["stage"] if attempt_context.get() else "fetch",
                  "exception", warning=True, error_type=type(e).__name__)
            return {"configured": True, "status": "error", "error": f"Gemini refresh failed ({type(e).__name__})"}
