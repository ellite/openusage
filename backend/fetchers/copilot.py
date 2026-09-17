import httpx

# Device flow client id used by VS Code's Copilot Chat extension. GitHub does not
# document a public API for personal Copilot subscription status, so this replicates
# the same OAuth device authorization flow official editor extensions use to sign in.
CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
USAGE_URL = "https://api.github.com/copilot_internal/user"

TOKEN_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "GitHubCopilotChat/0.26.7",
    "Editor-Version": "vscode/1.104.1",
}

# Single-user local app: one device flow can be in progress at a time.
_pending_flow: dict = {}


async def start_device_flow() -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            DEVICE_CODE_URL,
            data={"client_id": CLIENT_ID, "scope": "read:user"},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

    _pending_flow["device_code"] = data["device_code"]
    _pending_flow["interval"] = data.get("interval", 5)

    return {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data["verification_uri"],
        "expires_in": data.get("expires_in", 900),
        "interval": _pending_flow["interval"],
    }


async def poll_device_flow(device_code: str = None) -> dict:
    code = device_code or _pending_flow.get("device_code")
    if not code:
        return {"status": "error", "error": "No sign-in in progress"}

    async with httpx.AsyncClient() as client:
        r = await client.post(
            ACCESS_TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "device_code": code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

    error = data.get("error")
    if error == "authorization_pending":
        return {"status": "pending"}
    if error == "slow_down":
        _pending_flow["interval"] = data.get("interval", _pending_flow.get("interval", 5) + 5)
        return {"status": "pending", "interval": _pending_flow["interval"]}
    if error == "expired_token":
        _pending_flow.clear()
        return {"status": "expired"}
    if error == "access_denied":
        _pending_flow.clear()
        return {"status": "denied"}
    if error:
        _pending_flow.clear()
        return {"status": "error", "error": error}

    _pending_flow.clear()
    token = data.get("access_token")
    return {"status": "ok", "token": token}


def _quota(q: dict) -> dict:
    return {
        "unlimited": q.get("unlimited", False),
        "remaining": q.get("remaining"),
        "entitlement": q.get("entitlement"),
        "percent_remaining": q.get("percent_remaining"),
    }


async def fetch_copilot(config: dict = None) -> dict:
    if config is None:
        config = {}
    token = config.get("copilot_token")

    if not token:
        return {"configured": False, "error": "Not configured"}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                USAGE_URL,
                headers={**TOKEN_HEADERS, "Authorization": f"token {token}"},
                timeout=10,
            )
            r.raise_for_status()
            info = r.json()
            quotas = info.get("quota_snapshots", {})
            return {
                "configured": True,
                "status": "ok",
                "data": {
                    "plan": info.get("copilot_plan"),
                    "quota_reset_date": info.get("quota_reset_date"),
                    "premium_requests": _quota(quotas.get("premium_interactions", {})),
                    "chat": _quota(quotas.get("chat", {})),
                    "completions": _quota(quotas.get("completions", {})),
                },
            }
        except httpx.HTTPStatusError as e:
            return {
                "configured": True,
                "status": "error",
                "error": f"HTTP {e.response.status_code} — sign-in may have expired, reconnect in Settings",
            }
        except Exception as e:
            return {"configured": True, "status": "error", "error": str(e)}
