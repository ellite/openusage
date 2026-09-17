import json
import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from fetchers.gemini import fetch_gemini, USAGE_RPC_ID
from fetchers.gemini_diagnostics import rotation_body_summary, attempt_context


class GeminiDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def run_fetch(self, transport, save=None):
        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(transport), **kwargs)
        with patch("fetchers.gemini.httpx.AsyncClient", side_effect=client):
            with self.assertLogs(level=logging.INFO) as logs:
                result = await fetch_gemini({
                    "gemini_cookies": "__Secure-1PSID=secret-cookie; __Secure-1PSIDTS=secret-old",
                    "gemini_at_token": "secret-token", "gemini_sid": "secret-sid",
                }, on_config_update=save, service_id=42)
        output = "\n".join(logs.output)
        for secret in ("secret-cookie", "secret-old", "secret-new", "secret-token", "secret-sid", "secret-body"):
            self.assertNotIn(secret, output)
        events = [json.loads(line.split("Gemini refresh ", 1)[1]) for line in logs.output if "Gemini refresh " in line]
        self.assertEqual(len({e["attempt"] for e in events}), 1)
        self.assertTrue(all(e["service_id"] == 42 for e in events))
        self.assertIsNone(attempt_context.get())
        return result, events

    async def test_real_renewal_and_persistence_are_separate_events(self):
        def transport(request):
            if request.url.host == "accounts.google.com":
                return httpx.Response(200, text=')]}\'\n[["identity.hfcr",600],["di",18],["private","secret-body"]]',
                    headers={"set-cookie": "__Secure-1PSIDTS=secret-new; Domain=.google.com; Path=/; Secure"})
            payload = [None, [["session", 0.25, 1]]]
            return httpx.Response(200, text=json.dumps([["wrb.fr", USAGE_RPC_ID, json.dumps(payload)]]))
        save = AsyncMock()
        result, events = await self.run_fetch(transport, save)
        self.assertEqual(result["status"], "ok")
        rotation = next(e for e in events if e["stage"] == "rotation" and e["outcome"] == "response")
        self.assertTrue(rotation["auth_cookie_renewed"])
        self.assertEqual(rotation["google_di_raw"], 18)
        self.assertEqual(rotation["google_hfcr_raw"], 600)
        self.assertTrue(any(e["stage"] == "persistence" and e["outcome"] == "saved" for e in events))
        self.assertEqual(events[-1]["outcome"], "ok")
        save.assert_awaited()

    async def test_rate_limit_and_redirect_are_safe_and_distinct(self):
        def transport(request):
            if request.url.host == "accounts.google.com":
                return httpx.Response(429, text="secret-body", headers={"retry-after": "120"})
            if request.method == "GET":
                return httpx.Response(302, headers={"location": "https://accounts.google.com/signin?token=secret-token"})
            return httpx.Response(200, text=json.dumps([["wrb.fr", USAGE_RPC_ID, None, None, None, [7]]]))
        result, events = await self.run_fetch(transport)
        self.assertEqual(result["status"], "error")
        self.assertTrue(any(e.get("reason") == "rate_limited" and e["retry_after_seconds"] == 120 for e in events))
        self.assertTrue(any(e.get("rpc_code") == 7 and e.get("rpc_reason") == "permission_denied" for e in events))
        self.assertTrue(any(e.get("destination") == "google_accounts" for e in events))
        self.assertEqual(events[-1]["last_stage"], "page")

    async def test_transport_exception_does_not_leak_request_details(self):
        def transport(request):
            raise httpx.ConnectTimeout("secret-token secret-cookie", request=request)
        result, events = await self.run_fetch(transport)
        self.assertEqual(result["status"], "error")
        self.assertTrue(any(e["stage"] == "rotation" and e.get("error_type") == "ConnectTimeout" for e in events))
        self.assertEqual(events[-1]["last_stage"], "rpc")

    async def test_failed_persistence_is_not_reported_as_saved(self):
        def transport(request):
            return httpx.Response(200, headers={
                "set-cookie": "__Secure-1PSIDTS=secret-new; Domain=.google.com; Path=/; Secure",
            })
        save = AsyncMock(side_effect=RuntimeError("secret-cookie"))
        real_client = httpx.AsyncClient
        with patch("fetchers.gemini.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw)):
            with self.assertLogs("fetchers.gemini", level="INFO") as logs:
                with self.assertRaises(RuntimeError):
                    await fetch_gemini({"gemini_cookies": "__Secure-1PSID=secret-cookie"}, save)
        output = "\n".join(logs.output)
        self.assertNotIn("secret-cookie", output)
        self.assertNotIn("secret-new", output)
        events = [json.loads(line.split("Gemini refresh ", 1)[1]) for line in logs.output if "Gemini refresh " in line]
        self.assertTrue(any(e["stage"] == "persistence" and e["outcome"] == "failed" for e in events))
        self.assertFalse(any(e["outcome"] == "saved" for e in events))
        self.assertEqual(events[-1]["last_stage"], "persistence")
        self.assertIsNone(attempt_context.get())

    def test_rotation_body_allowlist(self):
        self.assertEqual(rotation_body_summary('<html>secret</html>'), {"body_format": "non_json"})
        self.assertEqual(rotation_body_summary('[["di","secret"],["unknown",123]]'), {"body_format": "json"})
