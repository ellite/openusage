import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")

FLATTEN_MAX_FIELDS = 300
FLATTEN_MAX_STRING_LEN = 200


def _resolve_path(obj, path: str):
    """Resolve a dotted/bracket path like 'data.items[0].balance' against a
    parsed JSON value. Returns None if any segment is missing."""
    if not path:
        return obj
    current = obj
    for m in _PATH_TOKEN_RE.finditer(path):
        key, idx = m.group(1), m.group(2)
        if current is None:
            return None
        if idx is not None:
            try:
                current = current[int(idx)]
            except (IndexError, TypeError, KeyError):
                return None
        elif key:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return None
    return current


def _apply_template(text: str, api_key: str) -> str:
    if not text:
        return text
    return text.replace("{{api_key}}", api_key or "")


def _flatten(obj, prefix: str = "", out: list = None) -> list:
    """Walk a parsed JSON value and list every leaf as {path, value}, so the
    Settings UI can offer them as pickable fields without the user having to
    hand-write a path."""
    if out is None:
        out = []
    if len(out) >= FLATTEN_MAX_FIELDS:
        return out

    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
            if len(out) >= FLATTEN_MAX_FIELDS:
                break
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", out)
            if len(out) >= FLATTEN_MAX_FIELDS:
                break
    else:
        value = obj
        if isinstance(value, str) and len(value) > FLATTEN_MAX_STRING_LEN:
            value = value[:FLATTEN_MAX_STRING_LEN] + "…"
        out.append({"path": prefix, "value": value})
    return out


async def _execute_request(config: dict) -> dict:
    """Runs the configured HTTP request plus success check. Returns
    {"payload": <parsed JSON>} on success or {"error": <message>} on failure."""
    url = (config.get("generic_url") or "").strip()
    if not url:
        return {"error": "Not configured"}

    method = (config.get("generic_method") or "POST").upper()
    api_key = config.get("generic_api_key") or ""
    body_raw = config.get("generic_body") or ""
    headers_raw = config.get("generic_headers") or ""
    success_path = (config.get("generic_success_path") or "").strip()
    success_value = config.get("generic_success_value")

    url = _apply_template(url, api_key)

    headers = {"Content-Type": "application/json"}
    if headers_raw:
        try:
            extra_headers = json.loads(_apply_template(headers_raw, api_key))
        except json.JSONDecodeError as e:
            return {"error": f"Invalid headers JSON: {e}"}
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items()})

    body = None
    if method == "POST" and body_raw:
        try:
            body = json.loads(_apply_template(body_raw, api_key))
        except json.JSONDecodeError as e:
            return {"error": f"Invalid body JSON: {e}"}

    timeout_config = httpx.Timeout(10.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            if method == "GET":
                r = await client.get(url, headers=headers)
            else:
                r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}"}
    except json.JSONDecodeError:
        return {"error": "Response was not valid JSON"}
    except Exception as e:
        logger.warning("Generic fetch exception: %s", e)
        return {"error": str(e)}

    if success_path:
        actual = _resolve_path(payload, success_path)
        if success_value is not None and str(actual) != str(success_value):
            detail = None
            if isinstance(payload, dict):
                detail = payload.get("errorDescription") or payload.get("error") or payload.get("message")
            msg = f"API reported failure ({success_path} = {actual!r}, expected {success_value!r})"
            if detail:
                msg += f": {detail}"
            return {"error": msg}

    return {"payload": payload}


def _extract_fields(payload, fields: list) -> list:
    result = []
    for f in fields:
        label = (f.get("label") or "").strip()
        path = (f.get("path") or "").strip()
        if not label or not path:
            continue
        result.append({
            "label": label,
            "type": f.get("type") or "text",
            "symbol": f.get("symbol") or "",
            "value": _resolve_path(payload, path),
        })
    return result


async def fetch_generic(config: dict = None) -> dict:
    if config is None:
        config = {}
    if not (config.get("generic_url") or "").strip():
        return {"configured": False, "error": "Not configured"}

    result = await _execute_request(config)
    if "error" in result:
        return {"configured": True, "status": "error", "error": result["error"]}

    fields = _extract_fields(result["payload"], config.get("generic_fields") or [])
    return {"configured": True, "status": "ok", "data": {"fields": fields}}


async def test_generic(config: dict) -> dict:
    """Backs the Settings 'Test Connection' button: runs the request and
    returns the flattened raw response (for field-picking) alongside any
    already-configured fields resolved against it."""
    result = await _execute_request(config)
    if "error" in result:
        return {"status": "error", "error": result["error"]}

    payload = result["payload"]
    return {
        "status": "ok",
        "fields": _extract_fields(payload, config.get("generic_fields") or []),
        "response_fields": _flatten(payload),
    }
