import re

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://ollama.com/",
}

# ollama.com moved the usage card (plan, usage credits, used this month) from
# /settings to /dashboard; /settings is now a JS-loaded usage report.
SETTINGS_URL = "https://ollama.com/dashboard"
# That report's figures (e.g. the request count) load asynchronously from this
# fragment endpoint rather than being in the page HTML.
USAGE_REPORT_URL = "https://ollama.com/settings/usage/report?scope=self"

# The dashboard is server-rendered HTML, not a JSON API -- these are
# scraped straight out of the markup and will break if the page changes.
BALANCE_RE = re.compile(r'id="extra-usage-balance"[^>]*>\s*\$?([\d.,]+)')
PLAN_RE = re.compile(
    r'Usage credits\s*<span[^>]*>\s*([^<]+?)\s*</span>', re.IGNORECASE
)
USED_MONTH_RE = re.compile(r'id="dashboard-usage"[^>]*>\s*\$?([\d.,]+)')
PERCENT_RES = (
    # Current markup puts the value in the usage meter's accessible label.
    re.compile(r'aria-label=["\']Free usage:\s*([\d.,]+)%["\']', re.IGNORECASE),
    # Older markup displayed "N% used" in a sibling span.
    re.compile(
        r'Free usage</span>\s*<span[^>]*>\s*([\d.,]+)%\s*(?:used)?\s*</span>',
        re.IGNORECASE,
    ),
)
RESET_RE = re.compile(r'class="[^"]*\blocal-time\b[^"]*"\s+data-time="([^"]+)"')


def _stat(html: str, label: str):
    """Value of a <dt>label</dt><dd>value</dd> pair in the usage report."""
    m = re.search(
        r'<dt[^>]*>\s*' + re.escape(label) + r'\s*</dt>\s*<dd[^>]*>\s*([^<]+?)\s*</dd>',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else None


def _number(text):
    if text is None:
        return None
    try:
        return float(re.sub(r"[^\d.\-]", "", text))
    except ValueError:
        return None


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def parse_settings_html(html: str) -> dict:
    data = {}

    m = BALANCE_RE.search(html)
    if m:
        data["balance"] = float(m.group(1).replace(",", ""))

    m = PLAN_RE.search(html)
    if m:
        data["plan"] = m.group(1).strip().title()

    m = USED_MONTH_RE.search(html)
    if m:
        data["cost_month"] = float(m.group(1).replace(",", ""))

    for pattern in PERCENT_RES:
        m = pattern.search(html)
        if m:
            data["percent_used"] = float(m.group(1).replace(",", ""))
            break

    # `local-time` elements also mark unrelated timestamps (e.g. a recent-requests
    # table), so only treat one as the usage reset when a usage meter was found.
    if "percent_used" in data:
        m = RESET_RE.search(html)
        if m:
            data["resets_at"] = m.group(1)

    return data


def parse_usage_report_html(html: str) -> dict:
    data = {}
    requests = _number(_stat(html, "Requests"))
    if requests is not None:
        data["requests_month"] = int(requests)
    return data


async def fetch_ollama_cloud(config: dict = None) -> dict:
    if config is None:
        config = {}
    cookies_raw = config.get("ollama_cookies")
    if not cookies_raw:
        return {"configured": False, "error": "Not configured"}

    cookies = parse_cookie_string(cookies_raw)

    async with httpx.AsyncClient() as client:
        async def get(url, **extra):
            return await client.get(
                url,
                headers={**HEADERS, **extra},
                cookies=cookies,
                timeout=15,
                follow_redirects=True,
            )

        try:
            r = await get(SETTINGS_URL)
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

        data = parse_settings_html(html)

        # The usage report is best-effort: a failure here (or a markup change)
        # must not turn a valid, signed-in session into an error card.
        try:
            ur = await get(USAGE_REPORT_URL, Referer=SETTINGS_URL)
            ur.raise_for_status()
            data.update(parse_usage_report_html(ur.text))
        except Exception:
            pass

    return {"configured": True, "status": "ok", "data": data}
