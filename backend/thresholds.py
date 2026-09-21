"""Per-service-type mapping from a fetcher's `data` payload to the
percentage-used figures that push notification thresholds are checked
against. Mirrors the session/weekly/monthly buckets
frontend/src/pages/index.astro's updateCardContent renders on each card --
keep both in sync if a fetcher's data shape changes.
"""

# Which of session/weekly/monthly thresholds make sense to offer for each
# service_type. Settings' modal (frontend/src/pages/settings.astro) has its
# own copy of this mapping to decide which threshold inputs to show.
APPLICABLE_THRESHOLDS = {
    "claude": ("session", "weekly"),
    "gemini": ("session", "weekly"),
    "chatgpt": ("session", "weekly"),
    "copilot": ("monthly",),
    "ollama_cloud": ("session",),
    "deepseek": (),
    "openai": (),
    "custom": (),
}


def _num(value):
    return value if isinstance(value, (int, float)) else None


def extract_usage_percentages(service_type: str, data: dict) -> dict:
    """Returns {threshold_type: percent_used} for whichever of session/weekly/
    monthly this service type and payload actually carry a live figure for."""
    data = data or {}
    out = {}

    if service_type in ("claude", "chatgpt"):
        five_hour = data.get("five_hour") or {}
        seven_day = data.get("seven_day") or {}
        pct = _num(five_hour.get("utilization"))
        if pct is not None:
            out["session"] = pct
        pct = _num(seven_day.get("utilization"))
        if pct is not None:
            out["weekly"] = pct

    elif service_type == "gemini":
        for limit in data.get("limits") or []:
            label = (limit.get("label") or "").strip().lower()
            pct = _num(limit.get("percent_used"))
            if pct is None or label not in ("session", "weekly"):
                continue
            out[label] = pct

    elif service_type == "copilot":
        # Multiple monthly quotas can apply at once (premium requests, chat,
        # completions) -- the worst (highest used %) drives the notification.
        pcts = []
        for key in ("premium_requests", "chat", "completions"):
            q = data.get(key) or {}
            if q.get("unlimited"):
                continue
            pct = _num(q.get("percent_remaining"))
            if pct is not None:
                pcts.append(100 - pct)
        if pcts:
            out["monthly"] = max(pcts)

    elif service_type == "ollama_cloud":
        pct = _num(data.get("percent_used"))
        if pct is not None:
            out["session"] = pct

    return out
