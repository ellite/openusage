import httpx

BASE = "https://opencode.ai/console"
GO_STATUS_URL = f"{BASE}/api/go/status"
BILLING_STATUS_URL = f"{BASE}/api/billing/status"
USAGE_URL = f"{BASE}/api/billing/drawer/usage"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# The console's JSON API reports money in micro-cents: $1 = 100,000,000.
MICRO_CENTS_PER_DOLLAR = 100_000_000

PLAN_NAMES = {"go": "Go", "go-plus": "Go Plus", "go-teams": "Go Teams"}

# Go's usage meters, as named in the console's /api/go/status response.
GO_METERS = (("fiveHour", "five_hour"), ("week", "week"), ("month", "month"))


class _AuthError(Exception):
    pass


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _dollars(micro_cents):
    """Micro-cents (an int, or the numeric string the API uses for bigints) to
    dollars. None when absent or not a number."""
    if micro_cents is None or isinstance(micro_cents, bool):
        return None
    try:
        return int(micro_cents) / MICRO_CENTS_PER_DOLLAR
    except (TypeError, ValueError):
        return None


def _meter(raw):
    if not isinstance(raw, dict):
        return None
    limit = _dollars(raw.get("limitMicroCents"))
    used = _dollars(raw.get("usedMicroCents"))
    if limit is None or used is None:
        return None
    return {
        "percent_used": round(used / limit * 100, 2) if limit > 0 else None,
        "used": round(used, 2),
        "limit": round(limit, 2),
        "resets_at": raw.get("resetsAt"),
    }


def parse_go_status(payload) -> dict:
    """/api/go/status is null for a workspace with no Go subscription. For a
    subscriber, `access.meters` carries the 5-hour, weekly and monthly meters."""
    if not isinstance(payload, dict):
        return {}
    out = {}
    product = payload.get("product")
    if product:
        out["plan"] = PLAN_NAMES.get(product, str(product).replace("-", " ").title())
    access = payload.get("access")
    meters = access.get("meters") if isinstance(access, dict) else None
    if isinstance(meters, dict):
        for api_key, key in GO_METERS:
            m = _meter(meters.get(api_key))
            if m:
                out[key] = m
    if "cancelAtPeriodEnd" in payload:
        out["cancel_at_period_end"] = bool(payload["cancelAtPeriodEnd"])
    return out


def parse_billing_status(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = {}
    balance = _dollars(payload.get("balanceMicroCents"))
    if balance is not None:
        out["balance"] = round(balance, 2)
    mode = payload.get("mode")
    if mode:
        out["billing_mode"] = str(mode)
    return out


def parse_usage(payload) -> dict:
    """Spend so far this billing month, preferring the user's own figure over
    the whole workspace's."""
    if not isinstance(payload, dict):
        return {}
    for scope in ("personal", "workspace"):
        block = payload.get(scope)
        if isinstance(block, dict):
            cost = _dollars(block.get("totalCostMicroCents"))
            if cost is not None:
                return {"cost_month": round(cost, 2)}
    return {}


async def fetch_opencode(config: dict = None) -> dict:
    if config is None:
        config = {}
    cookies_raw = config.get("opencode_cookies")
    if not cookies_raw:
        return {"configured": False, "error": "Not configured"}
    org_id = (config.get("opencode_org_id") or "").strip()
    if not org_id:
        return {
            "configured": True,
            "status": "error",
            "error": "Workspace id missing — re-paste a cURL from the opencode.ai console",
        }

    cookies = parse_cookie_string(cookies_raw)
    headers = {**HEADERS, "x-org-id": org_id, "Referer": f"{BASE}/{org_id}/go"}

    async with httpx.AsyncClient(
        cookies=cookies, headers=headers, timeout=15, follow_redirects=False
    ) as client:
        async def get_json(url, **params):
            r = await client.get(url, params=params or None)
            if r.status_code in (401, 403):
                raise _AuthError()
            r.raise_for_status()
            return r.json()

        try:
            data = parse_billing_status(await get_json(BILLING_STATUS_URL))
        except _AuthError:
            return {
                "configured": True,
                "status": "error",
                "error": "Not signed in — session expired, re-paste a fresh cURL",
            }
        except httpx.HTTPStatusError as e:
            return {"configured": True, "status": "error", "error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            return {"configured": True, "status": "error", "error": str(e)}

        # Signed in. The rest is best-effort so one changed endpoint doesn't
        # blank the whole card.
        try:
            go = parse_go_status(await get_json(GO_STATUS_URL, fresh="true"))
        except Exception:
            go = {}
        try:
            data.update(parse_usage(await get_json(USAGE_URL)))
        except Exception:
            pass

    data.update(go)
    if "plan" not in data:
        mode = data.get("billing_mode", "")
        data["plan"] = "Pay As You Go" if mode == "pay-as-you-go" else (mode.replace("-", " ").title() or "Zen")
    return {"configured": True, "status": "ok", "data": data}
