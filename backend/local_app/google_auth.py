"""Google OAuth 2.0 & OpenID Connect ID Token verification for OmniLearn System App."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
VALID_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})


class GoogleAuthError(ValueError):
    """Raised when Google OAuth configuration or token verification fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 401,
        error_code: str = "INVALID_GOOGLE_TOKEN",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


def build_google_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Build Google OAuth 2.0 Authorization Code URL with `openid email profile` scope."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def validate_google_claims(claims: dict[str, Any], expected_client_id: str) -> dict[str, str]:
    """Validate `aud`, `iss`, `exp`, and `email_verified` on verified Google token claims."""
    if not expected_client_id:
        raise GoogleAuthError(
            "Google OAuth is not configured (`GOOGLE_CLIENT_ID` is missing).",
            status_code=503,
            error_code="GOOGLE_OAUTH_NOT_CONFIGURED",
        )

    aud = str(claims.get("aud") or "").strip()
    if aud != expected_client_id:
        raise GoogleAuthError("Google token audience (`aud`) does not match configured Client ID.")

    iss = str(claims.get("iss") or "").strip()
    if iss not in VALID_GOOGLE_ISSUERS:
        raise GoogleAuthError("Google token issuer (`iss`) is not trusted.")

    try:
        exp = int(claims.get("exp") or 0)
    except (TypeError, ValueError) as exc:
        raise GoogleAuthError("Google token expiration (`exp`) is malformed.") from exc
    if exp <= int(time.time()):
        raise GoogleAuthError("Google token has expired.")

    verified_raw = claims.get("email_verified")
    email_verified = (
        verified_raw is True
        or str(verified_raw).strip().lower() == "true"
    )
    email = str(claims.get("email") or "").strip().lower()
    sub = str(claims.get("sub") or "").strip()
    if not email_verified or not email or "@" not in email or not sub:
        raise GoogleAuthError("Google account must have a verified email address.")

    return {
        "google_sub": sub,
        "email": email,
        "name": str(claims.get("name") or email.split("@", 1)[0]).strip(),
        "picture": str(claims.get("picture") or "").strip(),
    }


async def verify_google_id_token(
    id_token: str,
    expected_client_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Verify a Google ID token via Google's `tokeninfo` endpoint and validate claims."""
    clean_token = (id_token or "").strip()
    if not clean_token:
        raise GoogleAuthError("Missing Google `id_token`.")
    if not expected_client_id:
        raise GoogleAuthError(
            "Google OAuth is not configured (`GOOGLE_CLIENT_ID` is missing).",
            status_code=503,
            error_code="GOOGLE_OAUTH_NOT_CONFIGURED",
        )

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(GOOGLE_TOKENINFO_URL, params={"id_token": clean_token})
    except Exception as exc:
        raise GoogleAuthError(
            f"Unable to reach Google OAuth verification service (internet required): {exc}",
            status_code=503,
            error_code="GOOGLE_OAUTH_UNREACHABLE",
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        raise GoogleAuthError("Google rejected the supplied ID token.")

    try:
        claims = resp.json()
    except Exception as exc:
        raise GoogleAuthError("Malformed response from Google token verification.") from exc

    if not isinstance(claims, dict):
        raise GoogleAuthError("Malformed Google token claims.")
    return validate_google_claims(claims, expected_client_id)


async def exchange_google_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Exchange an authorization `code` for tokens and verify the embedded `id_token`."""
    if not client_id or not client_secret:
        raise GoogleAuthError(
            "Google OAuth code flow requires both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.",
            status_code=503,
            error_code="GOOGLE_OAUTH_NOT_CONFIGURED",
        )
    if not (code or "").strip():
        raise GoogleAuthError("Missing Google authorization code.", status_code=400)

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code.strip(),
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code != 200:
            raise GoogleAuthError("Google authorization code exchange failed.")
        payload = resp.json()
        id_token = str(payload.get("id_token") or "").strip() if isinstance(payload, dict) else ""
        return await verify_google_id_token(id_token, client_id, client=client)
    except GoogleAuthError:
        raise
    except Exception as exc:
        raise GoogleAuthError(
            f"Unable to exchange Google OAuth code (internet required): {exc}",
            status_code=503,
            error_code="GOOGLE_OAUTH_UNREACHABLE",
        ) from exc
    finally:
        if owns_client:
            await client.aclose()
