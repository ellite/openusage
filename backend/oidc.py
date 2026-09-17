"""OIDC / SSO login. Ported from candlr's app/routers/oidc.py, adapted for
a fully static frontend: there's no SSR layer to hold the CSRF state in an
httpOnly cookie or to do the code exchange itself, so /api/oidc/authorize
and /api/oidc/callback (in main.py) handle the whole redirect round trip
server-side, and the callback hands the browser a session token via a
query param on a static page instead of a JSON response."""
import os
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

OIDC_ENABLED = os.getenv("OIDC_ENABLED", "false").lower() in ("true", "1", "yes")
OIDC_PROVIDER_NAME = os.getenv("OIDC_PROVIDER_NAME", "SSO")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET")
OIDC_AUTH_URL = os.getenv("OIDC_AUTH_URL")
OIDC_TOKEN_URL = os.getenv("OIDC_TOKEN_URL")
OIDC_USERINFO_URL = os.getenv("OIDC_USERINFO_URL")
# Must point at this app's /api/oidc/callback.
OIDC_REDIRECT_URL = os.getenv("OIDC_REDIRECT_URL", "http://localhost:8000/api/oidc/callback")
OIDC_IDENTIFIER_FIELD = os.getenv("OIDC_IDENTIFIER_FIELD", "email")
OIDC_SCOPES = os.getenv("OIDC_SCOPES", "openid email profile")
OIDC_AUTO_CREATE_USERS = os.getenv("OIDC_AUTO_CREATE_USERS", "true").lower() in ("true", "1", "yes")
OIDC_DISABLE_PASSWORD_LOGIN = os.getenv("OIDC_DISABLE_PASSWORD_LOGIN", "false").lower() in ("true", "1", "yes")


def build_auth_url(state: str) -> str:
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": OIDC_REDIRECT_URL,
        "response_type": "code",
        "scope": OIDC_SCOPES,
        "state": state,
    }
    return f"{OIDC_AUTH_URL}?{urlencode(params)}"


def fetch_userinfo(code: str) -> dict:
    """Exchanges an authorization code for an access token, then fetches
    the provider's userinfo endpoint. Raises HTTPException on any failure."""
    try:
        with httpx.Client(timeout=10.0) as client:
            token_resp = client.post(
                OIDC_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": OIDC_REDIRECT_URL,
                    "client_id": OIDC_CLIENT_ID,
                    "client_secret": OIDC_CLIENT_SECRET,
                },
                headers={"Accept": "application/json"},
            )
            if not token_resp.is_success:
                raise HTTPException(status_code=400, detail="Token exchange failed")

            access_token = token_resp.json().get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="No access token in response")

            userinfo_resp = client.get(
                OIDC_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if not userinfo_resp.is_success:
                raise HTTPException(status_code=400, detail="Failed to fetch user info")

            return userinfo_resp.json()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Provider connection failed")
