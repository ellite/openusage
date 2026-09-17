import unittest
import json
from unittest.mock import AsyncMock, patch

import httpx

from fetchers.gemini import _cookie_jar, _rotate_1psidts, _sync_and_save_cookies
from fetchers.gemini import fetch_gemini, USAGE_RPC_ID


class GeminiCookiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_replacement_is_sent_once_and_persisted(self):
        cookies = {"__Secure-1PSIDTS": "old"}
        saved = AsyncMock()
        async with httpx.AsyncClient(cookies=_cookie_jar(cookies)) as client:
            response = httpx.Response(
                200,
                headers={"set-cookie": "__Secure-1PSIDTS=new; Domain=.google.com; Path=/; Secure"},
                request=httpx.Request("GET", "https://gemini.google.com/app"),
            )
            client.cookies.extract_cookies(response)
            await _sync_and_save_cookies(client, cookies, saved)
            for host in ("gemini.google.com", "accounts.google.com"):
                request = client.build_request("GET", f"https://{host}/")
                self.assertEqual(request.headers["cookie"], "__Secure-1PSIDTS=new")
            saved.assert_awaited_once_with({"gemini_cookies": "__Secure-1PSIDTS=new"})
            self.assertNotIn("cookie", client.build_request("GET", "https://example.com").headers)

    async def test_deleted_cookie_is_not_resurrected_from_config(self):
        cookies = {"__Secure-1PSIDTS": "old", "SID": "keep"}
        saved = AsyncMock()
        async with httpx.AsyncClient(cookies=_cookie_jar(cookies)) as client:
            client.cookies.extract_cookies(httpx.Response(
                200,
                headers={"set-cookie": "__Secure-1PSIDTS=; Max-Age=0; Domain=.google.com; Path=/"},
                request=httpx.Request("GET", "https://gemini.google.com/app"),
            ))
            await _sync_and_save_cookies(client, cookies, saved)
        self.assertEqual(cookies, {"SID": "keep"})
        saved.assert_awaited_once_with({"gemini_cookies": "SID=keep"})

    async def test_auxiliary_rotation_does_not_claim_auth_renewal(self):
        response = httpx.Response(
            200,
            headers={"set-cookie": "SIDCC=auxiliary; Domain=.google.com; Path=/"},
            request=httpx.Request("POST", "https://accounts.google.com/RotateCookies"),
        )
        with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=response)):
            with self.assertLogs("fetchers.gemini", level="WARNING") as logs:
                rotated = await _rotate_1psidts({"__Secure-1PSIDTS": "old"})
        self.assertEqual(rotated, {"SIDCC": "auxiliary"})
        self.assertIn("did not renew __Secure-1PSIDTS", "\n".join(logs.output))

    async def test_denied_usage_after_token_refresh_explains_unavailable_renewal(self):
        denied = httpx.Response(
            200,
            text=json.dumps([["wrb.fr", USAGE_RPC_ID, None, None, None, [7]]]),
            request=httpx.Request("POST", "https://gemini.google.com/_/BardChatUi/data/batchexecute"),
        )
        page = httpx.Response(
            200,
            text='{"SNlM0e":"fresh-token","cfb2h":"build","FdrFJe":"123"}',
            request=httpx.Request("GET", "https://gemini.google.com/app"),
        )
        with (
            patch("fetchers.gemini._rotate_1psidts", AsyncMock(return_value={"SIDCC": "auxiliary"})),
            patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=denied)) as post,
            patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=page)) as get,
        ):
            result = await fetch_gemini({
                "gemini_cookies": "__Secure-1PSID=session; __Secure-1PSIDTS=stale",
                "gemini_at_token": "cached-token",
            })
        self.assertEqual(result["status"], "error")
        self.assertEqual(post.await_count, 2)
        get.assert_awaited_once()
        self.assertIn("Google did not renew", result["error"])
        self.assertIn("Brave", result["error"])
        self.assertIn("Firefox", result["error"])


if __name__ == "__main__":
    unittest.main()
