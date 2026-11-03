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


_PERCENT_LABELS = {"session": "Session", "weekly": "Weekly", "monthly": "Monthly"}


def extract_history_metrics(service_type: str, data: dict) -> dict:
    """Returns {metric_key: {"label", "unit", "value"}} for every figure worth
    charting from one stored fetch: the percent-used buckets plus the
    balance/spend figures of the API-key providers."""
    data = data or {}
    out = {}
    for key, pct in extract_usage_percentages(service_type, data).items():
        out[key] = {"label": _PERCENT_LABELS.get(key, key), "unit": "%", "value": pct}

    if service_type == "deepseek":
        infos = data.get("balance_infos") or []
        info = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else None)
        try:
            if info is not None:
                out["balance"] = {
                    "label": "Balance",
                    "unit": info.get("currency") or "USD",
                    "value": float(info.get("total_balance")),
                }
        except (TypeError, ValueError):
            pass

    elif service_type == "custom":
        out.update(_custom_history_metrics(data))

    elif service_type == "openai":
        currency = (data.get("currency") or "usd").upper()
        spend = _num(data.get("spend_this_month"))
        if spend is not None:
            out["spend"] = {"label": "Spend this month", "unit": currency, "value": spend}
        balance = _num((data.get("credit_balance") or {}).get("total_available"))
        if balance is not None:
            out["balance"] = {"label": "Credit balance", "unit": currency, "value": balance}

    return out


_PROGRESS_TYPES = {"progress", "progress_remaining", "progress_bar", "progress_bar_remaining"}
_REMAINING_TYPES = {"progress_remaining", "progress_bar_remaining"}


def _to_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _custom_history_metrics(data: dict) -> dict:
    """Custom API fields the user ticked "track history" on. The field's
    configured type decides how the number is read and charted."""
    out = {}
    for f in data.get("fields") or []:
        if not f.get("history"):
            continue
        label = (f.get("label") or "").strip()
        ftype = f.get("type") or "text"
        value = _to_float(f.get("value"))
        if not label or value is None:
            continue
        unit = ""
        if ftype in _PROGRESS_TYPES:
            total = _to_float(f.get("total"))
            if not total or total <= 0:
                continue
            used = total - value if ftype in _REMAINING_TYPES else value
            value, unit = used / total * 100, "%"
        elif ftype == "percentage":
            unit = "%"
        elif ftype in ("percentage_fraction", "percentage_fraction_ring"):
            value, unit = value * 100, "%"
        elif ftype in ("currency", "balance"):
            unit = (f.get("symbol") or "").strip()
        elif ftype != "number":
            continue
        out[f"field:{label}"] = {"label": label, "unit": unit, "value": value}
    return out
