import re

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://claude.ai",
    "Referer": "https://claude.ai/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _extract_org_id(org_url: str) -> str | None:
    match = re.search(r"organizations/([^/]+)/", org_url)
    return match.group(1) if match else None


async def _fetch_credits(client: httpx.AsyncClient, org_id: str, cookies: dict) -> dict | None:
    try:
        r = await client.get(
            f"https://claude.ai/api/organizations/{org_id}/prepaid/credits",
            headers=HEADERS,
            cookies=cookies,
            timeout=15,
        )
        r.raise_for_status()
        credits = r.json()
        exponent = 2
        return {
            "balance": round(credits.get("amount", 0) / (10 ** exponent), exponent),
            "currency": credits.get("currency"),
            "next_expires_at": credits.get("next_expires_at"),
        }
    except Exception:
        return None


# Maps the organization's `capabilities` entries to a plan label. Anything not
# listed here (e.g. a bare "chat" capability with none of these) means Free.
PLAN_LABELS = {
    "claude_pro": "Pro",
    "claude_team": "Team",
    "claude_max": "Max",
    "claude_enterprise": "Enterprise",
}


async def _fetch_plan(client: httpx.AsyncClient, org_id: str, cookies: dict) -> str | None:
    try:
        r = await client.get(
            f"https://claude.ai/api/organizations/{org_id}",
            headers=HEADERS,
            cookies=cookies,
            timeout=15,
        )
        r.raise_for_status()
        capabilities = r.json().get("capabilities") or []
    except Exception:
        return None

    for cap in capabilities:
        if cap in PLAN_LABELS:
            return PLAN_LABELS[cap]
    return "Free"


async def fetch_claude(config: dict = None) -> dict:
    if config is None:
        config = {}
    cookies_raw = config.get("claude_cookies")
    org_url = config.get("claude_org_url")

    if not cookies_raw or not org_url:
        return {"configured": False, "error": "Not configured"}

    cookies = parse_cookie_string(cookies_raw)
    org_id = _extract_org_id(org_url)

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                org_url,
                headers=HEADERS,
                cookies=cookies,
                timeout=15,
                follow_redirects=True,
            )
            r.raise_for_status()
            data = r.json()
            if org_id:
                credits = await _fetch_credits(client, org_id, cookies)
                if credits:
                    data["prepaid_credits"] = credits
                plan = await _fetch_plan(client, org_id, cookies)
                if plan:
                    data["plan"] = plan
            return {"configured": True, "status": "ok", "data": data}
        except httpx.HTTPStatusError as e:
            return {"configured": True, "status": "error", "error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            return {"configured": True, "status": "error", "error": str(e)}
