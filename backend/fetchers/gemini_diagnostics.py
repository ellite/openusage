"""Allowlisted Gemini diagnostics. Never log response bodies or credentials."""

from contextvars import ContextVar
import json
import logging
import time
from urllib.parse import urlsplit

logger = logging.getLogger("fetchers.gemini")
attempt_context = ContextVar("gemini_attempt", default=None)


class _HideGeminiRequestURLs(logging.Filter):
    def filter(self, record):
        # httpx INFO includes the full RPC URL, including Google's f.sid.
        # Our response events below replace these lines for Gemini only.
        message = record.getMessage()
        return not any(url in message for url in (
            "https://gemini.google.com/", "https://accounts.google.com/RotateCookies",
        ))


logging.getLogger("httpx").addFilter(_HideGeminiRequestURLs())


def event(stage, outcome, *, warning=False, **fields):
    context = attempt_context.get()
    base = {}
    if context is not None:
        if stage != "fetch":
            context["stage"] = stage
        base = {
            "attempt": context["attempt"], "service_id": context["service_id"],
            "elapsed_ms": round((time.monotonic() - context["started"]) * 1000),
        }
    logger.log(logging.WARNING if warning else logging.INFO,
               "Gemini refresh %s", json.dumps({**base, "stage": stage, "outcome": outcome, **fields}, sort_keys=True))


def numeric_code(value):
    if type(value) is int:
        return value
    if isinstance(value, list) and value and type(value[0]) is int:
        return value[0]
    return None


def rotation_body_summary(text):
    """Keep only known numeric fields; `di` meanings are undocumented."""
    try:
        body = json.loads(text.removeprefix(")]}'").strip())
    except (ValueError, TypeError):
        return {"body_format": "non_json"}
    result = {"body_format": "json"}
    if isinstance(body, list):
        for row in body:
            if isinstance(row, list) and len(row) >= 2 and type(row[1]) is int:
                if row[0] == "di":
                    result["google_di_raw"] = row[1]
                elif row[0] == "identity.hfcr":
                    result["google_hfcr_raw"] = row[1]
    return result


def response_summary(response):
    mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
    retry = response.headers.get("retry-after", "")
    return {
        "http_status": response.status_code,
        "response_bytes": len(response.content),
        "content_type": mime if mime in ("application/json", "text/html", "text/plain") else "other",
        "retry_after_seconds": int(retry) if retry.isascii() and retry.isdigit() and len(retry) < 10 else None,
        "session_challenge_header_present": "sec-session-challenge" in response.headers,
    }


def redirect_kind(location):
    try:
        host = urlsplit(location).hostname
    except ValueError:
        return "invalid"
    if host == "accounts.google.com":
        return "google_accounts"
    if host == "gemini.google.com":
        return "gemini"
    return "other_or_relative" if location else "missing"
