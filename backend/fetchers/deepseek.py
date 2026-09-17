import httpx


async def fetch_deepseek(config: dict = None) -> dict:
    if config is None:
        config = {}
    api_key = config.get("deepseek_api_key")

    if not api_key:
        return {"configured": False, "error": "Not configured"}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=10,
            )
            r.raise_for_status()
            return {"configured": True, "status": "ok", "data": r.json()}
        except httpx.HTTPStatusError as e:
            return {"configured": True, "status": "error", "error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            return {"configured": True, "status": "error", "error": str(e)}
