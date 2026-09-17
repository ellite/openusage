import re

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://ollama.com/",
}

SETTINGS_URL = "https://ollama.com/settings"

# ollama.com/settings is server-rendered HTML, not a JSON API -- these are
# scraped straight out of the markup and will break if the page changes.
BALANCE_RE = re.compile(r'id="extra-usage-balance"[^>]*>\s*\$?([\d.,]+)')
PERCENT_RE = re.compile(r'Free usage</span>\s*<span class="text-sm"\s*>\s*([\d.]+)%\s*used')
RESET_RE = re.compile(r'class="[^"]*\blocal-time\b[^"]*"\s+data-time="([^"]+)"')


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


async def fetch_ollama_cloud(config: dict = None) -> dict:
    if config is None:
        config = {}
    cookies_raw = config.get("ollama_cookies")
    if not cookies_raw:
        return {"configured": False, "error": "Not configured"}

    cookies = parse_cookie_string(cookies_raw)

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                SETTINGS_URL,
                headers=HEADERS,
                cookies=cookies,
                timeout=15,
                follow_redirects=True,
            )
            r.raise_for_status()
            html = r.text
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (401, 403):
                return {
                    "configured": True,
                    "status": "error",
                    "error": f"HTTP {status} — cookies likely expired (session or Cloudflare clearance), re-paste a fresh cURL",
                }
            return {"configured": True, "status": "error", "error": f"HTTP {status}"}
        except Exception as e:
            return {"configured": True, "status": "error", "error": str(e)}

    if "signin.ollama.com" in str(r.url) or 'id="extra-usage-balance"' not in html:
        return {
            "configured": True,
            "status": "error",
            "error": "Not signed in — cookies expired, re-paste a fresh cURL",
        }

    data = {}

    m = BALANCE_RE.search(html)
    if m:
        data["balance"] = float(m.group(1).replace(",", ""))

    m = PERCENT_RE.search(html)
    if m:
        data["percent_used"] = float(m.group(1))

    m = RESET_RE.search(html)
    if m:
        data["resets_at"] = m.group(1)

    return {"configured": True, "status": "ok", "data": data}
