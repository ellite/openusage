import re
import urllib.parse


def _delimited_all(prefix: str, text: str):
    """Yield values matched as `prefix'...'` or `prefix"..."`, stopping only at
    the matching closing quote — not at any quote character embedded inside
    the value (e.g. a JSON-valued cookie like g_state={"i_p":123})."""
    for q_prefix, quote in (("'", "'"), ('"', '"'), ("$'", "'")):
        pattern = prefix + re.escape(q_prefix) + f"([^{quote}]+)" + quote
        for m in re.finditer(pattern, text):
            yield m.group(1)


def _first_delimited(prefix: str, text: str):
    for value in _delimited_all(prefix, text):
        return value
    return None


def find_header(curl_text: str, header_name: str) -> str | None:
    prefix = header_name.lower() + ":"
    for header in _delimited_all(r"(?:-H|--header)\s*", curl_text):
        if header.lower().startswith(prefix):
            return header.split(":", 1)[1].strip()
    return None


def parse_openai_session_curl(curl_text: str) -> dict:
    auth = find_header(curl_text, "authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise ValueError("Could not find an Authorization header in the pasted command.")
    session_key = auth.split(" ", 1)[1].strip()

    org_id = find_header(curl_text, "openai-organization")
    if not org_id:
        org_id = find_header(curl_text, "chatgpt-account-id")
    if not org_id:
        m = re.search(r"account_id=([^&'\"]+)", curl_text)
        if m:
            org_id = m.group(1)

    return {"session_key": session_key, "org_id": org_id or ""}


def find_url(curl_text: str) -> str | None:
    # 1. Match explicit --url flag
    m = re.search(r"--url\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))", curl_text)
    if m:
        return m.group(1) or m.group(2) or m.group(3)

    # 2. Match any quoted http(s) URL
    m = re.search(r"['\"](https?://[^'\"]+)['\"]", curl_text)
    if m:
        return m.group(1)

    # 3. Match any unquoted http(s) URL
    m = re.search(r"(https?://\S+)", curl_text)
    if m:
        return m.group(1)

    # 4. Fallback to original _first_delimited after curl
    return _first_delimited(r"curl(?:\.exe)?\s+", curl_text)


def parse_curl(curl_text: str) -> dict:
    url = find_url(curl_text)
    if not url:
        raise ValueError("Could not find a URL — make sure you pasted the full 'Copy as cURL' output.")

    cookies = None
    for header in _delimited_all(r"(?:-H|--header)\s*", curl_text):
        if header.lower().startswith("cookie:"):
            cookies = header.split(":", 1)[1].strip()
            break

    if not cookies:
        cookies = _first_delimited(r"(?:-b|--cookie)\s*", curl_text)

    if not cookies:
        raise ValueError("Could not find a cookie header in the pasted command.")

    return {"url": url, "cookies": cookies}


def parse_chatgpt_curl(curl_text: str) -> dict:
    parsed = parse_curl(curl_text)
    return {"cookies": parsed["cookies"]}


def parse_ollama_curl(curl_text: str) -> dict:
    parsed = parse_curl(curl_text)
    return {"cookies": parsed["cookies"]}


def parse_gemini_curl(curl_text: str) -> dict:
    parsed = parse_curl(curl_text)

    bl_match = re.search(r"bl=([^&'\"]+)", curl_text)
    bl = urllib.parse.unquote(bl_match.group(1)) if bl_match else None

    sid_match = re.search(r"f\.sid=([^&'\"]+)", curl_text)
    sid = urllib.parse.unquote(sid_match.group(1)) if sid_match else None

    at_match = re.search(r"[?&]at=([^&'\"]+)", curl_text) or re.search(r"at=([^&'\"]+)", curl_text)
    at_token = urllib.parse.unquote(at_match.group(1)) if at_match else None

    return {
        "cookies": parsed["cookies"],
        "at_token": at_token,
        "bl": bl,
        "sid": sid,
    }

