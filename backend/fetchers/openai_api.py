import asyncio
from datetime import datetime, timezone

import httpx

COSTS_URL = "https://api.openai.com/v1/organization/costs"
# Undocumented legacy endpoint — not part of the official API surface, could
# disappear without notice. Needs a platform.openai.com browser session key
# ("sess-..."), not an API key — regular API keys are explicitly rejected.
CREDIT_GRANTS_URL = "https://api.openai.com/v1/dashboard/billing/credit_grants"
SUBSCRIPTION_URL = "https://api.openai.com/v1/dashboard/billing/subscription"


def _month_start_epoch() -> int:
    now = datetime.now(tz=timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


SESSION_EXPIRED_MSG = "Session key expired or invalid — please re-import cURL in Settings"


async def _fetch_subscription(client: httpx.AsyncClient, session_key: str, org_id: str) -> tuple[dict | None, str | None]:
    if not session_key:
        return None, None
    headers = {"Authorization": f"Bearer {session_key}"}
    if org_id:
        headers["OpenAI-Organization"] = org_id

    try:
        r = await client.get(SUBSCRIPTION_URL, headers=headers, timeout=10)
        r.raise_for_status()
        body = r.json()
        plan_info = body.get("plan", {})
        raw_tier = body.get("trust_tier")
        tier_label = f"Tier {raw_tier.replace('tier', '')}" if raw_tier and str(raw_tier).startswith("tier") else raw_tier

        return {
            "plan_title": plan_info.get("title") or body.get("subscription_title") or "Pay-as-you-go",
            "trust_tier": tier_label,
            "account_name": body.get("account_name"),
            "billing_email": body.get("billing_email"),
            "has_payment_method": body.get("has_payment_method"),
            "hard_limit_usd": body.get("hard_limit_usd") or body.get("system_hard_limit_usd"),
            "auto_recharge_enabled": body.get("auto_recharge_enabled"),
        }, None
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            return None, SESSION_EXPIRED_MSG
        return None, None
    except Exception:
        return None, None


async def _fetch_credit_balance(client: httpx.AsyncClient, session_key: str, org_id: str) -> tuple[dict | None, str | None]:
    if not session_key:
        return None, None
    headers = {"Authorization": f"Bearer {session_key}"}
    if org_id:
        headers["OpenAI-Organization"] = org_id
    try:
        r = await client.get(
            CREDIT_GRANTS_URL,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        return {
            "total_granted": body.get("total_granted"),
            "total_used": body.get("total_used"),
            "total_available": body.get("total_available"),
        }, None
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            return None, SESSION_EXPIRED_MSG
        return None, None
    except Exception:
        return None, None


async def _fetch_costs(client: httpx.AsyncClient, headers: dict, start_time: int) -> dict:
    buckets = []
    page = None
    while True:
        params = {"start_time": start_time, "bucket_width": "1d", "limit": 31}
        if page:
            params["page"] = page
        r = await client.get(COSTS_URL, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        body = r.json()
        buckets.extend(body.get("data", []))
        if not body.get("has_more") or not body.get("next_page"):
            break
        page = body["next_page"]

    total = 0.0
    currency = "usd"
    by_line_item = {}
    for bucket in buckets:
        for item in bucket.get("results", []):
            amount = item.get("amount", {})
            value = float(amount.get("value") or 0)
            currency = amount.get("currency", currency)
            total += value
            label = item.get("line_item") or item.get("project_name") or "other"
            by_line_item[label] = by_line_item.get(label, 0) + value

    return {
        "spend_this_month": round(total, 2),
        "currency": currency,
        "period_start": datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat(),
        "by_line_item": {k: round(v, 2) for k, v in by_line_item.items()},
    }


async def fetch_openai(config: dict = None) -> dict:
    if config is None:
        config = {}
    api_key = config.get("openai_admin_key")

    if not api_key:
        return {"configured": False, "error": "Not configured"}

    start_time = _month_start_epoch()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    session_key = config.get("openai_session_key")
    org_id = config.get("openai_org_id")

    async with httpx.AsyncClient() as client:
        try:
            data, (credit_balance, credit_err), (subscription, sub_err) = await asyncio.gather(
                _fetch_costs(client, headers, start_time),
                _fetch_credit_balance(client, session_key, org_id),
                _fetch_subscription(client, session_key, org_id),
            )

            if credit_balance:
                data["credit_balance"] = credit_balance
            if subscription:
                data["subscription"] = subscription

            session_error = credit_err or sub_err
            if session_error:
                data["session_error"] = session_error

            return {"configured": True, "status": "ok", "data": data}
        except httpx.HTTPStatusError as e:
            return {"configured": True, "status": "error", "error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            return {"configured": True, "status": "error", "error": str(e)}
