import unittest
from functools import partial
from unittest.mock import patch

import httpx

from curl_parse import parse_opencode_curl
from fetchers import opencode
from fetchers.opencode import (
    fetch_opencode, parse_billing_status, parse_go_status, parse_usage,
)
from thresholds import extract_history_metrics, extract_usage_percentages

DOLLAR = 100_000_000  # micro-cents per dollar

# Shape taken from the console's own /api/go/status schema.
GO_SUBSCRIBER = {
    "product": "go-plus",
    "cancelAtPeriodEnd": False,
    "access": {
        "startsAt": "2026-11-01T00:00:00.000Z",
        "endsAt": "2026-12-01T00:00:00.000Z",
        "cancelAtPeriodEnd": False,
        "meters": {
            "fiveHour": {"startsAt": "2026-11-11T10:00:00.000Z", "resetsAt": "2026-11-11T15:00:00.000Z",
                         "limitMicroCents": str(48 * DOLLAR), "usedMicroCents": str(12 * DOLLAR)},
            "week": {"startsAt": "2026-11-09T00:00:00.000Z", "resetsAt": "2026-11-16T00:00:00.000Z",
                     "limitMicroCents": str(120 * DOLLAR), "usedMicroCents": str(30 * DOLLAR)},
            "month": {"resetsAt": "2026-12-01T00:00:00.000Z",
                      "limitMicroCents": str(240 * DOLLAR), "usedMicroCents": str(60 * DOLLAR)},
        },
    },
}


class OpenCodeParserTests(unittest.TestCase):
    def test_go_subscriber_meters(self):
        out = parse_go_status(GO_SUBSCRIBER)
        self.assertEqual(out["plan"], "Go Plus")
        self.assertEqual(out["five_hour"], {
            "percent_used": 25.0, "used": 12.0, "limit": 48.0, "resets_at": "2026-11-11T15:00:00.000Z"})
        self.assertEqual(out["week"]["percent_used"], 25.0)
        self.assertEqual(out["month"]["limit"], 240.0)
        self.assertIs(out["cancel_at_period_end"], False)

    def test_no_go_subscription(self):
        self.assertEqual(parse_go_status(None), {})

    def test_ended_subscription_has_no_meters(self):
        out = parse_go_status({"product": "go", "cancelAtPeriodEnd": True, "access": None})
        self.assertEqual(out, {"plan": "Go", "cancel_at_period_end": True})

    def test_unstarted_five_hour_window_has_no_reset(self):
        payload = {"product": "go", "access": {"meters": {"fiveHour": {
            "limitMicroCents": str(12 * DOLLAR), "usedMicroCents": "0"}}}}
        self.assertEqual(parse_go_status(payload)["five_hour"]["resets_at"], None)

    def test_zero_limit_gives_no_percent(self):
        payload = {"product": "go", "access": {"meters": {"week": {
            "limitMicroCents": "0", "usedMicroCents": "0", "resetsAt": "x"}}}}
        self.assertIsNone(parse_go_status(payload)["week"]["percent_used"])

    def test_billing_status(self):
        out = parse_billing_status({"mode": "pay-as-you-go", "balanceMicroCents": str(int(12.5 * DOLLAR))})
        self.assertEqual(out, {"balance": 12.5, "billing_mode": "pay-as-you-go"})

    def test_usage_prefers_personal(self):
        payload = {"workspace": {"totalCostMicroCents": str(9 * DOLLAR)},
                   "personal": {"totalCostMicroCents": str(3 * DOLLAR)}}
        self.assertEqual(parse_usage(payload), {"cost_month": 3.0})
        self.assertEqual(parse_usage({"personal": None, "workspace": {"totalCostMicroCents": str(9 * DOLLAR)}}),
                         {"cost_month": 9.0})
        self.assertEqual(parse_usage(None), {})


class OpenCodeCurlTests(unittest.TestCase):
    def test_org_from_header(self):
        curl = ("curl 'https://opencode.ai/console/api/providers/opencode' "
                "-H 'x-org-id: org_ABC123' -b 'sess=1; other=2'")
        self.assertEqual(parse_opencode_curl(curl), {"cookies": "sess=1; other=2", "org_id": "org_ABC123"})

    def test_org_from_referer(self):
        curl = ("curl 'https://opencode.ai/console/api/x' "
                "-H 'Referer: https://opencode.ai/console/org_XYZ789/go' -H 'cookie: sess=1'")
        self.assertEqual(parse_opencode_curl(curl)["org_id"], "org_XYZ789")

    def test_missing_org_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_opencode_curl("curl 'https://opencode.ai/console' -b 'sess=1'")


class OpenCodeThresholdTests(unittest.TestCase):
    def test_percentages_map_to_buckets(self):
        data = parse_go_status(GO_SUBSCRIBER)
        self.assertEqual(extract_usage_percentages("opencode", data),
                         {"session": 25.0, "weekly": 25.0, "monthly": 25.0})

    def test_no_go_means_no_percentages(self):
        self.assertEqual(extract_usage_percentages("opencode", {"balance": 1.0}), {})

    def test_history_includes_balance_and_spend(self):
        m = extract_history_metrics("opencode", {"balance": 4.5, "cost_month": 1.25})
        self.assertEqual(m["balance"]["value"], 4.5)
        self.assertEqual(m["cost_month"]["value"], 1.25)


class OpenCodeFetchTests(unittest.IsolatedAsyncioTestCase):
    CONFIG = {"opencode_cookies": "sess=1", "opencode_org_id": "org_ABC"}

    def _client(self, handler):
        real = httpx.AsyncClient
        return patch.object(opencode.httpx, "AsyncClient",
                            partial(real, transport=httpx.MockTransport(handler)))

    async def test_not_configured(self):
        self.assertEqual(await fetch_opencode({}), {"configured": False, "error": "Not configured"})

    async def test_missing_org_id(self):
        r = await fetch_opencode({"opencode_cookies": "sess=1"})
        self.assertEqual(r["status"], "error")
        self.assertIn("Workspace id", r["error"])

    async def test_subscriber_end_to_end_sends_org_header(self):
        seen = []

        def handler(request):
            seen.append((request.url.path, request.headers.get("x-org-id")))
            path = request.url.path
            if path.endswith("/billing/status"):
                return httpx.Response(200, json={"mode": "pay-as-you-go", "balanceMicroCents": str(5 * DOLLAR)})
            if path.endswith("/go/status"):
                return httpx.Response(200, json=GO_SUBSCRIBER)
            return httpx.Response(200, json={"personal": {"totalCostMicroCents": str(2 * DOLLAR)}})

        with self._client(handler):
            r = await fetch_opencode(self.CONFIG)
        self.assertEqual(r["status"], "ok")
        d = r["data"]
        self.assertEqual((d["plan"], d["balance"], d["cost_month"]), ("Go Plus", 5.0, 2.0))
        self.assertEqual(d["week"]["percent_used"], 25.0)
        self.assertTrue(all(org == "org_ABC" for _, org in seen))

    async def test_pay_as_you_go_when_go_status_is_null(self):
        def handler(request):
            if request.url.path.endswith("/go/status"):
                return httpx.Response(200, json=None)
            if request.url.path.endswith("/billing/status"):
                return httpx.Response(200, json={"mode": "pay-as-you-go", "balanceMicroCents": "0"})
            return httpx.Response(200, json={"personal": {"totalCostMicroCents": "0"}})

        with self._client(handler):
            r = await fetch_opencode(self.CONFIG)
        self.assertEqual(r["data"]["plan"], "Pay As You Go")
        self.assertNotIn("five_hour", r["data"])

    async def test_expired_session(self):
        with self._client(lambda req: httpx.Response(401, json={"_tag": "Unauthorized"})):
            r = await fetch_opencode(self.CONFIG)
        self.assertEqual(r["status"], "error")
        self.assertIn("session expired", r["error"])

    async def test_optional_endpoint_failure_keeps_card(self):
        def handler(request):
            if request.url.path.endswith("/billing/status"):
                return httpx.Response(200, json={"mode": "pay-as-you-go", "balanceMicroCents": "0"})
            return httpx.Response(500)

        with self._client(handler):
            r = await fetch_opencode(self.CONFIG)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["data"]["balance"], 0.0)


if __name__ == "__main__":
    unittest.main()
