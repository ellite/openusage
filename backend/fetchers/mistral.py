import asyncio

import httpx

SUBSCRIPTION_URL = "https://api.mistral.ai/v1/billing/subscription"
BUDGET_URL = "https://api.mistral.ai/v1/billing/budget"

# Undocumented console surface, reverse-engineered from what the Vibe CLI
# itself reads for its footer usage gauge (vibe/app_server/_billing.py in the
# CLI's wheel): GET /v1/billing/subscription names the plan, and
# /v1/billing/budget carries the monthly Vibe Code and API allowances.
# Both take the regular API key as a Bearer token — no separate admin key,
# unlike the Enterprise-only /v1/admin endpoints.

BAD_KEY_MSG = "API key invalid or revoked — re-paste it from the Mistral console"


def _budget(raw) -> dict | None:
    """A budget block ({vibe,api}_budget): percent of the monthly allowance
    used, the allowance itself, and when it resets. None when Mistral omits
    it (e.g. a workspace member without the usage permission)."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("usage_percentage")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    out = {"percent_used": round(pct, 2)}
    if isinstance(raw.get("initial_budget"), (int, float)):
        out["budget"] = round(raw["initial_budget"], 2)
    if raw.get("currency"):
        out["currency"] = raw["currency"]
    if raw.get("reset_at"):
        out["resets_at"] = raw["reset_at"]
    return out


async def _get_json(client: httpx.AsyncClient, url: str, api_key: str) -> dict:
    r = await client.get(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


async def fetch_mistral(config: dict = None) -> dict:
    if config is None:
        config = {}
    api_key = config.get("mistral_api_key")

    if not api_key:
        return {"configured": False, "error": "Not configured"}

    try:
        async with httpx.AsyncClient() as client:
            sub, budget = await asyncio.gather(
                _get_json(client, SUBSCRIPTION_URL, api_key),
                _get_json(client, BUDGET_URL, api_key),
            )
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status in (401, 403):
            return {"configured": True, "status": "error", "error": BAD_KEY_MSG}
        return {"configured": True, "status": "error", "error": f"HTTP {status}"}
    except Exception as e:
        return {"configured": True, "status": "error", "error": str(e)}

    data = {}
    if sub.get("plan_name"):
        data["plan"] = sub["plan_name"]
    if sub.get("payg_enabled") is not None:
        data["payg_enabled"] = bool(sub["payg_enabled"])

    vibe = _budget(budget.get("vibe_budget"))
    if vibe:
        data["vibe"] = vibe
    api = _budget(budget.get("api_budget"))
    if api:
        data["api"] = api

    if not data:
        return {
            "configured": True,
            "status": "error",
            "error": "Mistral returned no billing data for this key",
        }

    return {"configured": True, "status": "ok", "data": data}
