"""Single-Admin PBKDF2 Lock, KV brute-force rate limiter, JWT cookies & Web Lite guest sessions."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

try:
    from .api_pool import EditionLimitError, add_personal_api_key, get_pool_summary
    from .cache import kv_delete, kv_get, kv_put
    from .crypto_utils import (
        get_master_secret,
        jwt_sign,
        jwt_verify,
        pbkdf2_hash,
        pbkdf2_verify,
        sha256_hex,
    )
    from .db import d1_first, d1_run, env_get, env_str
    from .edition_guard import get_runtime_mode
except ImportError:
    from api_pool import EditionLimitError, add_personal_api_key, get_pool_summary
    from cache import kv_delete, kv_get, kv_put
    from crypto_utils import (
        get_master_secret,
        jwt_sign,
        jwt_verify,
        pbkdf2_hash,
        pbkdf2_verify,
        sha256_hex,
    )
    from db import d1_first, d1_run, env_get, env_str
    from edition_guard import get_runtime_mode

try:
    import js as _js  # type: ignore
    from pyodide.ffi import to_js as _to_js  # type: ignore

    _HAS_JS_FETCH = hasattr(_js, "fetch")
except ImportError:
    _js = None  # type: ignore
    _to_js = None  # type: ignore
    _HAS_JS_FETCH = False

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_VALID_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

SESSION_COOKIE_NAME = "omnilearn_session"
SESSION_TTL_SECONDS = 30 * 86400    # 30 days persistent session for smooth user experience
GUEST_TTL_SECONDS = 14400           # 4h for Web Lite guest preview
RATE_LIMIT_MAX_FAILS = 5
RATE_LIMIT_WINDOW_SECONDS = 900     # 15 minutes
PBKDF2_ITERATIONS = 100_000


def get_header(request: Any, name: str, default: str = "") -> str:
    """Case-insensitive header lookup for Workers Request, dicts, or mock objects."""
    headers = getattr(request, "headers", None)
    if headers is None and isinstance(request, dict):
        headers = request.get("headers")
    if headers is None:
        return default

    target = name.lower()
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == target and v is not None:
                return str(v)
        return default

    # Cloudflare Workers JS Headers object supports .get(name) case-insensitively
    if hasattr(headers, "get"):
        val = headers.get(name)
        if val is None:
            val = headers.get(target)
        if isinstance(val, str):
            return val
    return default


def get_client_ip(request: Any) -> str:
    """Extract client IP address from Cloudflare / proxy headers."""
    cf_ip = get_header(request, "CF-Connecting-IP").strip()
    if cf_ip:
        return cf_ip
    xff = get_header(request, "X-Forwarded-For").strip()
    if xff:
        return xff.split(",")[0].strip()
    return "127.0.0.1"


def build_session_cookie(token: str, max_age: int = SESSION_TTL_SECONDS) -> str:
    """Build HttpOnly + Secure + SameSite=Lax session cookie string for OAuth compatibility."""
    return (
        f"{SESSION_COOKIE_NAME}={token}; "
        f"HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={int(max_age)}"
    )


def clear_session_cookie() -> str:
    """Build cookie clearing directive."""
    return (
        f"{SESSION_COOKIE_NAME}=; "
        "HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"
    )


def extract_token(request: Any) -> str | None:
    """Extract JWT from `Authorization: Bearer <token>` or `Cookie` header."""
    auth_header = get_header(request, "Authorization").strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    cookie_header = get_header(request, "Cookie").strip()
    if cookie_header:
        for part in cookie_header.split(";"):
            item = part.strip()
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            if k.strip() == SESSION_COOKIE_NAME:
                val = v.strip()
                if val:
                    return val
    return None


async def get_session(request: Any, env: Any) -> dict[str, Any] | None:
    """Verify session JWT from Cookie or Authorization Bearer header."""
    token = extract_token(request)
    if not token:
        return None
    claims = await jwt_verify(token, get_master_secret(env))
    if not claims or not isinstance(claims, dict):
        return None
    role = claims.get("role")
    if role not in ("admin", "user", "guest_lite"):
        return None
    claims["token"] = token
    return claims


async def admin_exists(env: Any) -> bool:
    """Return True if the single-admin user has been initialized in D1."""
    if env_get(env, "DB", None) is None:
        return False
    row = await d1_first(env, "SELECT id FROM admin_users LIMIT 1")
    return row is not None


async def _rate_limit_key(client_ip: str) -> str:
    ip_hash = await sha256_hex((client_ip or "127.0.0.1").strip())
    return f"auth:rl:{ip_hash[:32]}"


async def _get_fail_count(env: Any, rl_key: str) -> int:
    raw = await kv_get(env, rl_key)
    if not raw:
        return 0
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return int(data.get("count", 0))
        return int(data)
    except Exception:
        try:
            return int(raw)
        except Exception:
            return 0


async def _increment_fail_count(env: Any, rl_key: str) -> int:
    count = await _get_fail_count(env, rl_key) + 1
    await kv_put(
        env,
        rl_key,
        json.dumps({"count": count}),
        expiration_ttl=RATE_LIMIT_WINDOW_SECONDS,
    )
    return count


async def register_admin(
    env: Any,
    password: str,
    username: str = "admin",
    api_key: str | None = None,
    provider: str = "gemini",
) -> dict[str, Any]:
    """Initialize the single-admin PBKDF2 account; rejects if admin already exists."""
    if await admin_exists(env):
        return {
            "ok": False,
            "status": 409,
            "error": "ADMIN_ALREADY_EXISTS",
            "message": "Admin account is already initialized. Please sign in.",
        }

    clean_pw = password or ""
    if len(clean_pw) < 4:
        return {
            "ok": False,
            "status": 400,
            "error": "WEAK_PASSWORD",
            "message": "Admin master password must be at least 4 characters.",
        }

    clean_user = (username or "admin").strip() or "admin"
    pwd_hash, salt_hex = await pbkdf2_hash(clean_pw, iterations=PBKDF2_ITERATIONS)

    await d1_run(
        env,
        """
        INSERT INTO admin_users (username, password_hash, salt, last_login)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (clean_user, pwd_hash, salt_hex),
    )

    key_added = False
    key_warning: str | None = None
    if api_key and api_key.strip():
        try:
            await add_personal_api_key(env, api_key, provider=provider, owner_sub=clean_user)
            key_added = True
        except EditionLimitError as exc:
            key_warning = exc.message
        except ValueError as exc:
            key_warning = str(exc)

    runtime_mode = get_runtime_mode(env)
    token = await jwt_sign(
        {"role": "admin", "sub": clean_user, "edition": runtime_mode},
        get_master_secret(env),
        expires_in_seconds=SESSION_TTL_SECONDS,
    )
    pool = await get_pool_summary(env, owner_sub=clean_user)
    return {
        "ok": True,
        "status": 201,
        "authenticated": True,
        "role": "admin",
        "username": clean_user,
        "edition": runtime_mode,
        "token": token,
        "cookie": build_session_cookie(token, max_age=SESSION_TTL_SECONDS),
        "key_added": key_added,
        "key_warning": key_warning,
        "pool": pool,
    }


async def login_admin(
    env: Any,
    password: str,
    client_ip: str = "127.0.0.1",
    api_key: str | None = None,
    provider: str = "gemini",
    username: str = "admin",
) -> dict[str, Any]:
    """Authenticate Admin user ID + PBKDF2 password BEFORE processing optional personal API key."""
    rl_key = await _rate_limit_key(client_ip)
    fail_count = await _get_fail_count(env, rl_key)
    if fail_count >= RATE_LIMIT_MAX_FAILS:
        return {
            "ok": False,
            "status": 429,
            "error": "RATE_LIMITED",
            "message": "Too many failed login attempts. Try again in 15 minutes.",
        }

    clean_user = (username or "").strip()
    if not clean_user:
        new_fails = await _increment_fail_count(env, rl_key)
        remaining = max(0, RATE_LIMIT_MAX_FAILS - new_fails)
        return {
            "ok": False,
            "status": 401,
            "error": "INVALID_CREDENTIALS",
            "message": "Invalid admin user ID or password.",
            "attempts_remaining": remaining,
        }

    any_admin = await d1_first(env, "SELECT id FROM admin_users LIMIT 1")
    if not any_admin:
        return {
            "ok": False,
            "status": 404,
            "error": "ADMIN_NOT_INITIALIZED",
            "message": "Admin account is not initialized yet.",
        }

    row = await d1_first(
        env,
        "SELECT id, username, password_hash, salt FROM admin_users WHERE username = ? LIMIT 1",
        (clean_user,),
    )
    if not row:
        new_fails = await _increment_fail_count(env, rl_key)
        remaining = max(0, RATE_LIMIT_MAX_FAILS - new_fails)
        return {
            "ok": False,
            "status": 401,
            "error": "INVALID_CREDENTIALS",
            "message": "Invalid admin user ID or password.",
            "attempts_remaining": remaining,
        }

    is_valid = await pbkdf2_verify(
        password or "",
        str(row["password_hash"]),
        str(row["salt"]),
        iterations=PBKDF2_ITERATIONS,
    )
    if not is_valid:
        # CRITICAL: Never call add_personal_api_key when password verification fails!
        new_fails = await _increment_fail_count(env, rl_key)
        remaining = max(0, RATE_LIMIT_MAX_FAILS - new_fails)
        return {
            "ok": False,
            "status": 401,
            "error": "INVALID_CREDENTIALS",
            "message": "Invalid admin master password.",
            "attempts_remaining": remaining,
        }

    # Password verified: clear IP brute-force counter & record login timestamp
    await kv_delete(env, rl_key)
    await d1_run(
        env,
        "UPDATE admin_users SET last_login = datetime('now') WHERE id = ?",
        (row["id"],),
    )

    # ONLY AFTER PBKDF2 passes may optional personal API key be encrypted & added
    key_added = False
    key_warning: str | None = None
    username = str(row.get("username") or "admin")
    if api_key and api_key.strip():
        try:
            await add_personal_api_key(env, api_key, provider=provider, owner_sub=username)
            key_added = True
        except EditionLimitError as exc:
            key_warning = exc.message
        except ValueError as exc:
            key_warning = str(exc)

    runtime_mode = get_runtime_mode(env)
    token = await jwt_sign(
        {"role": "admin", "sub": username, "edition": runtime_mode},
        get_master_secret(env),
        expires_in_seconds=SESSION_TTL_SECONDS,
    )
    pool = await get_pool_summary(env, owner_sub=username)
    return {
        "ok": True,
        "status": 200,
        "authenticated": True,
        "role": "admin",
        "username": username,
        "edition": runtime_mode,
        "token": token,
        "cookie": build_session_cookie(token, max_age=SESSION_TTL_SECONDS),
        "key_added": key_added,
        "key_warning": key_warning,
        "pool": pool,
    }


def get_google_oauth_config(env: Any) -> dict[str, Any]:
    """Return Worker Google OAuth settings without exposing Client Secret."""
    client_id = env_str(env, "GOOGLE_CLIENT_ID").strip()
    client_secret = env_str(env, "GOOGLE_CLIENT_SECRET").strip()
    redirect_uri = env_str(env, "GOOGLE_REDIRECT_URI").strip()
    return {
        "enabled": bool(client_id),
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code_flow_enabled": bool(client_id and client_secret),
    }


async def _worker_http_json(
    url: str,
    *,
    method: str = "GET",
    form_data: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {}
    body: str | None = None
    if form_data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(form_data)

    if _HAS_JS_FETCH and _js is not None and _to_js is not None:
        opts: dict[str, Any] = {"method": method.upper()}
        if headers:
            if hasattr(_js, "Headers"):
                h = _js.Headers.new()
                for k, v in headers.items():
                    h.set(str(k), str(v))
                opts["headers"] = h
            else:
                opts["headers"] = headers
        if body is not None:
            opts["body"] = body
        dict_converter = getattr(_js.Object, "fromEntries", None)
        converted_opts = _to_js(opts, dict_converter=dict_converter) if dict_converter else _to_js(opts)
        resp = await _js.fetch(url, converted_opts)
        status = int(resp.status)
        text = str(await resp.text())
        try:
            return status, json.loads(text) if text else {}
        except Exception:
            return status, {"raw": text}

    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        if method == "POST":
            resp = await client.post(url, data=form_data or {})
        else:
            resp = await client.get(url)
        try:
            return resp.status_code, resp.json() if resp.text else {}
        except Exception:
            return resp.status_code, {}


def _validate_google_claims(claims: dict[str, Any], expected_client_id: str) -> dict[str, str]:
    if not expected_client_id:
        raise ValueError("Google OAuth is not configured (`GOOGLE_CLIENT_ID` is missing).")
    if str(claims.get("aud") or "").strip() != expected_client_id:
        raise PermissionError("Google token audience does not match configured Client ID.")
    if str(claims.get("iss") or "").strip() not in _VALID_GOOGLE_ISSUERS:
        raise PermissionError("Google token issuer is not trusted.")
    try:
        exp = int(claims.get("exp") or 0)
    except (TypeError, ValueError) as exc:
        raise PermissionError("Google token expiration is invalid.") from exc
    if exp <= int(time.time()):
        raise PermissionError("Google token has expired.")
    verified_raw = claims.get("email_verified")
    email_verified = verified_raw is True or str(verified_raw).strip().lower() == "true"
    email = str(claims.get("email") or "").strip().lower()
    sub = str(claims.get("sub") or "").strip()
    if not email_verified or not email or "@" not in email or not sub:
        raise PermissionError("Google account must have a verified email address.")
    return {
        "google_sub": sub,
        "email": email,
        "name": str(claims.get("name") or email.split("@", 1)[0]).strip(),
        "picture": str(claims.get("picture") or "").strip(),
    }


async def verify_google_id_token(id_token: str, expected_client_id: str) -> dict[str, str]:
    """Verify a Google ID token via Google's `tokeninfo` endpoint."""
    clean_token = (id_token or "").strip()
    if not clean_token:
        raise PermissionError("Missing Google `id_token`.")
    status, payload = await _worker_http_json(
        f"{GOOGLE_TOKENINFO_URL}?{urlencode({'id_token': clean_token})}"
    )
    if status != 200 or not isinstance(payload, dict):
        raise PermissionError("Google rejected the supplied ID token.")
    return _validate_google_claims(payload, expected_client_id)


async def _upsert_google_user_d1(env: Any, claims: dict[str, str]) -> dict[str, Any]:
    email = claims["email"]
    sub = claims["google_sub"]
    name = claims["name"]
    pic = claims["picture"]
    if env_get(env, "DB", None) is not None:
        existing = await d1_first(
            env,
            "SELECT id FROM users WHERE google_sub = ? OR email = ? LIMIT 1",
            (sub, email),
        )
        if existing:
            uid = str(existing["id"])
            await d1_run(
                env,
                """
                UPDATE users
                SET google_sub = ?, email = ?, name = ?, picture = ?, role = 'user', last_login = datetime('now')
                WHERE id = ?
                """,
                (sub, email, name, pic, uid),
            )
        else:
            uid = f"usr-{uuid.uuid4().hex[:16]}"
            await d1_run(
                env,
                """
                INSERT INTO users (id, google_sub, email, name, picture, role, last_login)
                VALUES (?, ?, ?, ?, ?, 'user', datetime('now'))
                """,
                (uid, sub, email, name, pic),
            )
    return claims


async def login_with_google(
    env: Any,
    id_token: str,
    *,
    api_key: str | None = None,
    provider: str = "gemini",
) -> dict[str, Any]:
    """Verify Google ID token, upsert user in D1 `users`, and issue session JWT."""
    cfg = get_google_oauth_config(env)
    client_id = str(cfg["client_id"] or "")
    if not client_id:
        return {
            "ok": False,
            "status": 503,
            "error": "GOOGLE_OAUTH_NOT_CONFIGURED",
            "message": "Google Sign-In requires GOOGLE_CLIENT_ID to be configured.",
        }
    try:
        claims = await verify_google_id_token(id_token, client_id)
    except PermissionError as exc:
        return {
            "ok": False,
            "status": 401,
            "error": "INVALID_GOOGLE_TOKEN",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 503,
            "error": "GOOGLE_OAUTH_UNREACHABLE",
            "message": str(exc),
        }

    await _upsert_google_user_d1(env, claims)
    email = claims["email"]
    key_added = False
    key_warning: str | None = None
    if api_key and api_key.strip():
        try:
            await add_personal_api_key(env, api_key, provider=provider, owner_sub=email)
            key_added = True
        except EditionLimitError as exc:
            key_warning = exc.message
        except ValueError as exc:
            key_warning = str(exc)

    runtime_mode = get_runtime_mode(env)
    token = await jwt_sign(
        {
            "role": "user",
            "sub": email,
            "email": email,
            "name": claims["name"],
            "google_sub": claims["google_sub"],
            "edition": runtime_mode,
        },
        get_master_secret(env),
        expires_in_seconds=SESSION_TTL_SECONDS,
    )
    pool = await get_pool_summary(env, owner_sub=email)
    return {
        "ok": True,
        "status": 200,
        "authenticated": True,
        "role": "user",
        "username": email,
        "email": email,
        "name": claims["name"],
        "google_sub": claims["google_sub"],
        "edition": runtime_mode,
        "token": token,
        "cookie": build_session_cookie(token, max_age=SESSION_TTL_SECONDS),
        "key_added": key_added,
        "key_warning": key_warning,
        "pool": pool,
    }


def resolve_effective_redirect_uri(cfg_uri: str, fallback_callback_url: str) -> str:
    clean_cfg = (cfg_uri or "").strip()
    clean_fallback = (fallback_callback_url or "").strip()
    if clean_cfg and clean_cfg.startswith(("http://", "https://")):
        if clean_fallback and clean_fallback.startswith(("http://", "https://")):
            try:
                from urllib.parse import urlparse
                cfg_netloc = urlparse(clean_cfg).netloc
                fb_netloc = urlparse(clean_fallback).netloc
                if cfg_netloc != fb_netloc and ("localhost" in cfg_netloc or "127.0.0.1" in cfg_netloc):
                    return clean_fallback
            except Exception:
                pass
        return clean_cfg
    return clean_fallback


async def build_google_oauth_start_url(env: Any, fallback_callback_url: str) -> tuple[bool, str, str]:
    """Return `(ok, auth_url_or_error, error_code)` for Authorization Code redirect."""
    cfg = get_google_oauth_config(env)
    client_id = str(cfg["client_id"] or "")
    client_secret = str(cfg["client_secret"] or "")
    if not client_id or not client_secret:
        return (
            False,
            "Google browser redirect login requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
            "GOOGLE_OAUTH_NOT_CONFIGURED",
        )
    redirect_uri = resolve_effective_redirect_uri(str(cfg.get("redirect_uri") or ""), fallback_callback_url)
    state = await jwt_sign(
        {"purpose": "google_oauth_state", "redirect_uri": redirect_uri, "nonce": uuid.uuid4().hex[:16]},
        get_master_secret(env),
        expires_in_seconds=600,
    )
    url = f"{GOOGLE_AUTH_URL}?{urlencode({'client_id': client_id, 'redirect_uri': redirect_uri, 'response_type': 'code', 'scope': 'openid email profile', 'state': state, 'prompt': 'select_account', 'access_type': 'online'})}"
    return True, url, ""


async def complete_google_oauth_callback(
    env: Any,
    code: str,
    state: str,
    fallback_callback_url: str,
) -> dict[str, Any]:
    """Exchange Google authorization `code` and sign the user in."""
    state_claims = await jwt_verify(state, get_master_secret(env))
    if not state_claims or state_claims.get("purpose") != "google_oauth_state":
        return {"ok": False, "status": 400, "error": "INVALID_OAUTH_STATE", "message": "Google OAuth state is invalid or expired."}

    cfg = get_google_oauth_config(env)
    client_id = str(cfg["client_id"] or "")
    client_secret = str(cfg["client_secret"] or "")
    redirect_uri = str(state_claims.get("redirect_uri") or resolve_effective_redirect_uri(str(cfg.get("redirect_uri") or ""), fallback_callback_url)).strip()
    if not client_id or not client_secret:
        return {"ok": False, "status": 503, "error": "GOOGLE_OAUTH_NOT_CONFIGURED", "message": "Google OAuth secrets not configured."}

    status, token_data = await _worker_http_json(
        GOOGLE_TOKEN_URL,
        method="POST",
        form_data={
            "code": code.strip(),
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if status != 200 or not isinstance(token_data, dict):
        err_code = str(token_data.get("error") or "") if isinstance(token_data, dict) else ""
        err_desc = str(token_data.get("error_description") or "") if isinstance(token_data, dict) else ""
        err_msg = f"{err_code}: {err_desc}" if (err_code and err_desc) else (err_desc or err_code or "Unknown upstream error")
        msg = f"Google authorization code exchange failed (HTTP {status}): {err_msg}"
        return {
            "ok": False,
            "status": 401,
            "error": "INVALID_GOOGLE_TOKEN",
            "message": msg,
            "details": {"status": status, "error": err_code, "error_description": err_desc, "redirect_uri": redirect_uri},
        }
    return await login_with_google(env, str(token_data.get("id_token") or ""))


async def create_guest_lite_session(env: Any) -> dict[str, Any]:
    """Issue a limited read-only/sandbox `guest_lite` session for Web Lite Preview."""
    runtime_mode = get_runtime_mode(env)
    token = await jwt_sign(
        {"role": "guest_lite", "sub": "guest_lite", "edition": runtime_mode},
        get_master_secret(env),
        expires_in_seconds=GUEST_TTL_SECONDS,
    )
    return {
        "ok": True,
        "status": 200,
        "authenticated": True,
        "role": "guest_lite",
        "username": "guest_lite",
        "edition": runtime_mode,
        "token": token,
        "cookie": build_session_cookie(token, max_age=GUEST_TTL_SECONDS),
    }
