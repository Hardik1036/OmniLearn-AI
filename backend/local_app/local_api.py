"""Local FastAPI Server (`edition: "system_app"`) serving `public/` and all `/api/*` endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx

from backend.local_app import __version__
from backend.local_app.env_config import (
    get_cloud_relay_config,
    get_google_oauth_config,
    is_internet_required,
    load_dotenv_file,
)
from backend.local_app.gemini_local import (
    AIServiceRequiredError,
    grade_exam_answer,
    synthesize_course_sources,
)
from backend.local_app.google_auth import (
    GoogleAuthError,
    build_google_authorize_url,
    exchange_google_code,
    verify_google_id_token,
)
from backend.local_app.local_db import (
    SUPPORTED_PROVIDERS,
    LocalDatabase,
    resolve_db_path,
    sanitize_local_slide_key,
)
from backend.local_app.parsers import (
    FileValidationError,
    SSRFBlockedError,
    SourceDispatcher,
    SourceParsingError,
)
from backend.local_app.parsers.doc_parsers import MAX_PDF_BYTES, MAX_SLIDE_JPEG_BYTES
from backend.local_app.parsers.web_crawler import ainspect_url
from backend.local_app.source_search import search_sources
from backend.local_app.sync_client import CloudSyncClient

SESSION_COOKIE_NAME = "omnilearn_session"
SESSION_TTL_SECONDS = 86400

_JPEG_MAGIC = b"\xff\xd8\xff"
_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}
_PUBLIC_API_ROUTES = {"/api/health", "/api/releases/latest", "/api/auth/status"}


def _default_public_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "frontend"
        return candidate if candidate.exists() else Path(meipass) / "public"
    return Path(__file__).resolve().parent.parent.parent / "frontend"


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = (-len(text)) % 4
    return base64.urlsafe_b64decode((text + ("=" * pad)).encode("ascii"))


def _sign_jwt(payload: dict[str, Any], secret: bytes, expires_in: int = SESSION_TTL_SECONDS) -> str:
    now = int(time.time())
    claims = dict(payload)
    claims.setdefault("iat", now)
    claims.setdefault("exp", now + expires_in)
    header_b64 = _b64u_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    claims_b64 = _b64u_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{claims_b64}"
    sig = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64u_encode(sig)}"


def _verify_jwt(token: str, secret: bytes) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    header_b64, claims_b64, sig_b64 = parts
    try:
        header = json.loads(_b64u_decode(header_b64).decode("utf-8"))
        if not isinstance(header, dict) or header.get("alg") != "HS256":
            return None
        claims = json.loads(_b64u_decode(claims_b64).decode("utf-8"))
        if not isinstance(claims, dict):
            return None
        provided_sig = _b64u_decode(sig_b64)
    except Exception:
        return None

    expected_sig = hmac.new(secret, f"{header_b64}.{claims_b64}".encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, provided_sig):
        return None
    exp = claims.get("exp")
    if exp is not None and int(time.time()) >= int(exp):
        return None
    return claims


def _detect_platform(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "android" in ua:
        return "android"
    if "mac" in ua or "darwin" in ua:
        return "macos"
    if "linux" in ua or "x11" in ua:
        return "linux"
    return "windows"


def _err(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"ok": False, "error": error, "message": message},
    )


def create_app(
    *,
    db_path: str | Path | None = None,
    local_token: str | None = None,
    require_local_token: bool | None = None,
    public_dir: str | Path | None = None,
) -> FastAPI:
    """Create the OmniLearn Local System Application FastAPI instance."""
    load_dotenv_file()
    app = FastAPI(title="OmniLearn System App API", version=__version__)

    resolved_pub = Path(public_dir).resolve() if public_dir else _default_public_dir().resolve()
    env_token = os.environ.get("OMNILEARN_LOCAL_APP_TOKEN", "").strip()
    active_local_token = local_token if local_token is not None else env_token
    enforce_local_token = (
        bool(require_local_token)
        if require_local_token is not None
        else bool(active_local_token)
    )
    if enforce_local_token and not active_local_token:
        active_local_token = secrets.token_urlsafe(32)

    app.state.pinned_db_path = Path(db_path).resolve() if db_path is not None else None
    app.state.last_env_db_path = os.environ.get("OMNILEARN_DB_PATH", "")
    app.state.db = LocalDatabase(db_path=app.state.pinned_db_path)
    app.state.db.sync_admin_from_env()
    app.state.local_token = active_local_token
    app.state.require_local_token = enforce_local_token
    app.state.public_dir = resolved_pub

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1",
            "http://localhost",
            "https://localhost",
            "capacitor://localhost",
        ],
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    def get_db() -> LocalDatabase:
        if app.state.pinned_db_path is None:
            curr_env = os.environ.get("OMNILEARN_DB_PATH", "")
            if curr_env != app.state.last_env_db_path or resolve_db_path() != app.state.db.db_path:
                app.state.last_env_db_path = curr_env
                app.state.db = LocalDatabase()
        return app.state.db

    def get_jwt_secret(db: LocalDatabase) -> bytes:
        return db._get_or_create_wrapping_key()

    def extract_session(request: Request, db: LocalDatabase) -> dict[str, Any] | None:
        token = ""
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
        if not token:
            return None
        claims = _verify_jwt(token, get_jwt_secret(db))
        if not claims or claims.get("role") not in ("admin", "user"):
            return None
        return claims

    def check_local_token(request: Request) -> bool:
        if not app.state.require_local_token:
            return True
        provided = (
            request.headers.get("x-local-app-token", "").strip()
            or request.query_params.get("local_token", "").strip()
        )
        return bool(provided) and hmac.compare_digest(provided, app.state.local_token)

    def require_admin(
        request: Request,
        db: LocalDatabase,
        *,
        admin_only: bool = False,
    ) -> dict[str, Any] | JSONResponse:
        if not check_local_token(request):
            return _err(401, "LOCAL_TOKEN_REQUIRED", "Missing or invalid X-Local-App-Token.")
        session = extract_session(request, db)
        if session is None:
            return _err(401, "UNAUTHORIZED", "Sign-in is required.")
        if admin_only and session.get("role") != "admin":
            return _err(403, "ADMIN_REQUIRED", "Admin privileges are required for this action.")
        return session

    @app.middleware("http")
    async def security_and_token_middleware(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if (
            path.startswith("/api/")
            and path not in ("/api/health", "/api/auth/google/callback")
            and not check_local_token(request)
        ):
            return _err(401, "LOCAL_TOKEN_REQUIRED", "Missing or invalid X-Local-App-Token.")

        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    # ------------------------------------------------------------------
    # Health, Releases & Single-Admin Auth
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        return {
            "ok": True,
            "edition": "system_app",
            "status": "healthy",
            "version": __version__,
            "fts_enabled": True,
            "internet_required": is_internet_required(),
        }

    @app.get("/api/releases/latest")
    async def api_releases_latest(request: Request) -> dict[str, Any]:
        db = get_db()
        return {
            "ok": True,
            "edition": "system_app",
            "detected_platform": _detect_platform(request.headers.get("user-agent", "")),
            "releases": db.get_latest_releases(),
        }

    async def _resolve_effective_google_config() -> dict[str, Any]:
        oauth_cfg = dict(get_google_oauth_config())
        if oauth_cfg.get("enabled"):
            return oauth_cfg
        cloud_url = str(get_cloud_relay_config().get("cloud_url") or "").strip().rstrip("/")
        if cloud_url.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    resp = await client.get(f"{cloud_url}/api/auth/google/config")
                if resp.status_code == 200:
                    remote = resp.json()
                    if isinstance(remote, dict) and remote.get("enabled") and remote.get("client_id"):
                        return {
                            "enabled": True,
                            "client_id": str(remote["client_id"]),
                            "client_secret": "",
                            "redirect_uri": "",
                            "oauth_code_flow_enabled": False,
                            "via_cloud": True,
                        }
            except Exception:
                pass
        return oauth_cfg

    @app.get("/api/auth/status")
    async def api_auth_status(request: Request) -> dict[str, Any]:
        db = get_db()
        initialized = db.has_admin()
        session = extract_session(request, db)
        oauth_cfg = await _resolve_effective_google_config()
        owner_sub = str(session.get("sub") or "") if session else None
        return {
            "ok": True,
            "edition": "system_app",
            "initialized": initialized,
            "admin_initialized": initialized,
            "authenticated": session is not None,
            "role": session["role"] if session else None,
            "username": session.get("sub") if session else None,
            "email": session.get("email") if session else None,
            "name": session.get("name") if session else None,
            "google_oauth_enabled": bool(oauth_cfg["enabled"]),
            "google_client_id": str(oauth_cfg["client_id"] or ""),
            "google_code_flow_enabled": bool(oauth_cfg["oauth_code_flow_enabled"]),
            "cloud_relay_url_configured": bool(get_cloud_relay_config().get("cloud_url")),
            "pool": db.get_pool_summary(owner_sub=owner_sub) if session else None,
        }

    @app.get("/api/auth/google/config")
    async def api_auth_google_config() -> dict[str, Any]:
        oauth_cfg = await _resolve_effective_google_config()
        return {
            "ok": True,
            "enabled": bool(oauth_cfg["enabled"]),
            "client_id": str(oauth_cfg["client_id"] or ""),
            "code_flow_enabled": bool(oauth_cfg["oauth_code_flow_enabled"]),
        }

    @app.post("/api/auth/init")
    async def api_auth_init(request: Request) -> Response:
        db = get_db()
        if db.has_admin():
            return _err(409, "ADMIN_ALREADY_EXISTS", "Admin account is already initialized.")

        body = await request.json()
        password = str(body.get("password") or "")
        confirm = body.get("confirm_password")
        if confirm is not None and str(confirm) != password:
            return _err(400, "PASSWORD_MISMATCH", "Passwords do not match.")

        username = str(body.get("username") or "admin").strip() or "admin"
        try:
            db.init_admin(password, username=username)
        except ValueError as exc:
            return _err(400, "WEAK_PASSWORD", str(exc))

        key_added = False
        key_warning = None
        raw_key = str(body.get("api_key") or "").strip()
        provider = str(body.get("provider") or "gemini").strip().lower()
        if raw_key:
            try:
                db.add_api_key(raw_key, provider=provider, owner_sub=username)
                key_added = True
            except ValueError as exc:
                key_warning = str(exc)

        token = _sign_jwt(
            {"role": "admin", "sub": username, "edition": "system_app"},
            get_jwt_secret(db),
        )
        resp = JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "authenticated": True,
                "role": "admin",
                "username": username,
                "edition": "system_app",
                "token": token,
                "key_added": key_added,
                "key_warning": key_warning,
                "pool": db.get_pool_summary(owner_sub=username),
            },
        )
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp

    @app.post("/api/auth/login")
    async def api_auth_login(request: Request) -> Response:
        db = get_db()
        if not db.has_admin():
            return _err(404, "ADMIN_NOT_INITIALIZED", "Admin account is not initialized yet.")

        body = await request.json()
        password = str(body.get("password") or "")
        username = str(body.get("username") or "admin").strip() or "admin"
        raw_key = str(body.get("api_key") or "").strip()
        provider = str(body.get("provider") or "gemini").strip().lower()

        try:
            # Enforces PBKDF2 verification BEFORE touching api_keys!
            login_res = db.login_admin(
                password,
                api_key=raw_key,
                provider=provider,
                username=username,
            )
            key_warning = None
        except PermissionError as exc:
            return _err(401, "INVALID_CREDENTIALS", str(exc))
        except ValueError as exc:
            # Password succeeded, optional key was malformed
            login_res = db.login_admin(password, api_key="", username=username)
            key_warning = str(exc)

        cloud_cfg = get_cloud_relay_config()
        cloud_url = str(cloud_cfg.get("cloud_url") or "").strip().rstrip("/")
        if cloud_url.startswith(("http://", "https://")) and not cloud_cfg.get("cloud_token"):
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    cloud_resp = await client.post(
                        f"{cloud_url}/api/auth/login",
                        json={"username": username, "password": password},
                    )
                if cloud_resp.status_code == 200:
                    cloud_json = cloud_resp.json()
                    if isinstance(cloud_json, dict) and cloud_json.get("token"):
                        db.set_cloud_token(username, str(cloud_json["token"]))
                        login_res["pool"] = db.get_pool_summary(owner_sub=username)
            except Exception:
                pass

        token = _sign_jwt(
            {"role": "admin", "sub": username, "edition": "system_app"},
            get_jwt_secret(db),
        )
        resp = JSONResponse(
            status_code=200,
            content={
                **login_res,
                "edition": "system_app",
                "token": token,
                "key_warning": key_warning,
            },
        )
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp

    @app.post("/api/auth/logout")
    async def api_auth_logout(request: Request) -> Response:
        db = get_db()
        session = extract_session(request, db)
        if session and session.get("sub"):
            db.clear_cloud_token(str(session["sub"]))
        resp = JSONResponse(status_code=200, content={"ok": True})
        resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return resp

    @app.post("/api/auth/guest-lite")
    async def api_auth_guest_lite() -> Response:
        return _err(
            403,
            "SYSTEM_APP_ADMIN_REQUIRED",
            "System App requires Admin or Google Sign-In rather than Web Lite guest preview.",
        )

    @app.post("/api/auth/google")
    async def api_auth_google_login(request: Request) -> Response:
        db = get_db()
        oauth_cfg = get_google_oauth_config()
        client_id = str(oauth_cfg["client_id"] or "")
        cloud_url = str(get_cloud_relay_config().get("cloud_url") or "").strip().rstrip("/")

        body = await request.json()
        id_token = str(body.get("id_token") or body.get("credential") or "").strip()
        if not id_token:
            return _err(401, "INVALID_GOOGLE_TOKEN", "Missing Google `id_token`.")

        claims: dict[str, str] | None = None
        cloud_session_token = ""

        if client_id:
            try:
                claims = await verify_google_id_token(id_token, client_id)
            except GoogleAuthError as exc:
                return _err(exc.status_code, exc.error_code, exc.message)

        if cloud_url.startswith(("http://", "https://")):
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    cloud_resp = await client.post(
                        f"{cloud_url}/api/auth/google",
                        json={"id_token": id_token},
                    )
                if cloud_resp.status_code == 200:
                    cloud_data = cloud_resp.json()
                    if isinstance(cloud_data, dict):
                        cloud_session_token = str(cloud_data.get("token") or "")
                        if claims is None and cloud_data.get("email"):
                            claims = {
                                "google_sub": str(cloud_data.get("google_sub") or cloud_data["email"]),
                                "email": str(cloud_data["email"]).strip().lower(),
                                "name": str(cloud_data.get("name") or cloud_data["email"]),
                                "picture": str(cloud_data.get("picture") or ""),
                            }
                elif claims is None:
                    err_data = cloud_resp.json() if cloud_resp.headers.get("content-type", "").startswith("application/json") else {}
                    return _err(
                        cloud_resp.status_code if cloud_resp.status_code >= 400 else 401,
                        str(err_data.get("error") or "INVALID_GOOGLE_TOKEN"),
                        str(err_data.get("message") or "Google Sign-In failed on Cloudflare Worker."),
                    )
            except Exception as exc:
                if claims is None:
                    return _err(503, "GOOGLE_OAUTH_UNREACHABLE", f"Unable to verify Google token via Cloudflare Worker: {exc}")

        if claims is None:
            return _err(
                503,
                "GOOGLE_OAUTH_NOT_CONFIGURED",
                "Google Sign-In requires GOOGLE_CLIENT_ID or OMNILEARN_CLOUD_URL in `.env`.",
            )

        user = db.upsert_google_user(
            google_sub=claims["google_sub"],
            email=claims["email"],
            name=claims["name"],
            picture=claims["picture"],
            role="user",
        )
        email = str(user["email"])
        if cloud_session_token:
            db.set_cloud_token(email, cloud_session_token)
        key_added = False
        key_warning: str | None = None
        raw_key = str(body.get("api_key") or "").strip()
        provider = str(body.get("provider") or "gemini").strip().lower()
        if raw_key:
            try:
                db.add_api_key(raw_key, provider=provider, owner_sub=email)
                key_added = True
            except ValueError as exc:
                key_warning = str(exc)

        token = _sign_jwt(
            {
                "role": "user",
                "sub": email,
                "email": email,
                "name": str(user.get("name") or email),
                "google_sub": str(user["google_sub"]),
                "edition": "system_app",
            },
            get_jwt_secret(db),
        )
        resp = JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "authenticated": True,
                "role": "user",
                "username": email,
                "email": email,
                "name": str(user.get("name") or email),
                "edition": "system_app",
                "token": token,
                "key_added": key_added,
                "key_warning": key_warning,
                "pool": db.get_pool_summary(owner_sub=email),
            },
        )
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp

    @app.get("/api/auth/google/start")
    async def api_auth_google_start(request: Request) -> Response:
        db = get_db()
        oauth_cfg = get_google_oauth_config()
        client_id = str(oauth_cfg["client_id"] or "")
        client_secret = str(oauth_cfg["client_secret"] or "")
        if not client_id or not client_secret:
            return _err(
                503,
                "GOOGLE_OAUTH_NOT_CONFIGURED",
                "Google browser redirect login requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in `.env`.",
            )
        redirect_uri = str(oauth_cfg["redirect_uri"] or "").strip()
        if not redirect_uri:
            redirect_uri = f"{request.url.scheme}://{request.url.netloc}/api/auth/google/callback"
        state_jwt = _sign_jwt(
            {
                "purpose": "google_oauth_state",
                "redirect_uri": redirect_uri,
                "nonce": secrets.token_hex(12),
            },
            get_jwt_secret(db),
            expires_in=600,
        )
        auth_url = build_google_authorize_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state_jwt,
        )
        if request.query_params.get("format") == "json":
            return JSONResponse(content={"ok": True, "auth_url": auth_url})
        return RedirectResponse(url=auth_url, status_code=302)

    @app.get("/api/auth/google/callback")
    async def api_auth_google_callback(request: Request) -> Response:
        db = get_db()
        oauth_error = str(request.query_params.get("error") or "").strip()
        if oauth_error:
            return RedirectResponse(url="/?auth_error=google_cancelled", status_code=302)

        state_token = str(request.query_params.get("state") or "").strip()
        code = str(request.query_params.get("code") or "").strip()
        state_claims = _verify_jwt(state_token, get_jwt_secret(db))
        if not state_claims or state_claims.get("purpose") != "google_oauth_state":
            return _err(400, "INVALID_OAUTH_STATE", "Google OAuth state is invalid or expired.")

        oauth_cfg = get_google_oauth_config()
        redirect_uri = str(state_claims.get("redirect_uri") or oauth_cfg["redirect_uri"] or "").strip()
        if not redirect_uri:
            redirect_uri = f"{request.url.scheme}://{request.url.netloc}/api/auth/google/callback"
        try:
            claims = await exchange_google_code(
                code=code,
                client_id=str(oauth_cfg["client_id"] or ""),
                client_secret=str(oauth_cfg["client_secret"] or ""),
                redirect_uri=redirect_uri,
            )
        except GoogleAuthError as exc:
            return _err(exc.status_code, exc.error_code, exc.message)

        user = db.upsert_google_user(
            google_sub=claims["google_sub"],
            email=claims["email"],
            name=claims["name"],
            picture=claims["picture"],
            role="user",
        )
        email = str(user["email"])
        token = _sign_jwt(
            {
                "role": "user",
                "sub": email,
                "email": email,
                "name": str(user.get("name") or email),
                "google_sub": str(user["google_sub"]),
                "edition": "system_app",
            },
            get_jwt_secret(db),
        )
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp

    # ------------------------------------------------------------------
    # Multi-API Key Pool (Zero-Leakage)
    # ------------------------------------------------------------------

    @app.get("/api/pool")
    async def api_get_pool(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        return JSONResponse(content=db.get_pool_summary(owner_sub=str(auth.get("sub") or "")))

    @app.post("/api/pool/add")
    async def api_pool_add(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        raw_key = str(body.get("api_key") or "")
        provider = str(body.get("provider") or "gemini").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            return _err(400, "UNSUPPORTED_PROVIDER", f"Unsupported provider '{provider}'.")
        owner_sub = str(auth.get("sub") or "admin")
        try:
            res = db.add_api_key(raw_key, provider=provider, owner_sub=owner_sub)
        except ValueError as exc:
            return _err(400, "INVALID_API_KEY", str(exc))
        return JSONResponse(content={**res, "pool": db.get_pool_summary(owner_sub=owner_sub)})

    @app.delete("/api/pool/{slot_id}")
    async def api_pool_delete(slot_id: str, request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        owner_sub = str(auth.get("sub") or "admin")
        try:
            deleted = db.delete_api_key(slot_id, owner_sub=owner_sub)
        except PermissionError as exc:
            return _err(403, "DEFAULT_KEY_PROTECTED", str(exc))
        if not deleted:
            return _err(404, "NOT_FOUND", "API key slot not found.")
        return JSONResponse(content={"ok": True, "deleted": True, "pool": db.get_pool_summary(owner_sub=owner_sub)})

    # ------------------------------------------------------------------
    # Courses & Cross-Source Synthesis
    # ------------------------------------------------------------------

    @app.get("/api/courses")
    async def api_list_courses(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        return JSONResponse(content={"ok": True, "courses": db.list_courses()})

    @app.post("/api/courses")
    async def api_create_course(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        title = str(body.get("title") or "").strip()
        layout_type = str(body.get("layout_type") or "mixed").strip()
        course = db.create_course(title, layout_type=layout_type)
        return JSONResponse(status_code=201, content={"ok": True, "course": course})

    @app.get("/api/courses/{course_id}")
    async def api_get_course(course_id: str, request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        bundle = db.get_course(course_id)
        if bundle is None:
            return _err(404, "NOT_FOUND", "Course not found.")
        return JSONResponse(content=bundle)

    @app.delete("/api/courses/{course_id}")
    async def api_delete_course(course_id: str, request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db, admin_only=True)
        if isinstance(auth, JSONResponse):
            return auth
        deleted = db.delete_course(course_id)
        if not deleted:
            return _err(404, "NOT_FOUND", "Course not found.")
        return JSONResponse(content={"ok": True, "deleted": True})

    @app.post("/api/courses/{course_id}/subjects")
    async def api_create_subject(course_id: str, request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        title = str(body.get("title") or "").strip()
        if not title:
            return _err(400, "INVALID_SUBJECT", "Subject title is required.")
        try:
            subj = db.create_subject(
                course_id,
                title,
                topic=str(body.get("topic") or "").strip(),
                unit=str(body.get("unit") or "").strip(),
                instructor=str(body.get("instructor") or "OmniLearn AI").strip() or "OmniLearn AI",
                badge_color=str(body.get("badge_color") or "").strip(),
            )
            return JSONResponse(status_code=201, content={"ok": True, "subject": subj})
        except KeyError as exc:
            return _err(404, "NOT_FOUND", str(exc))

    @app.post("/api/courses/synthesize")
    async def api_synthesize_course(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        course_id = str(body.get("course_id") or "").strip()
        if not course_id:
            return _err(400, "MISSING_COURSE_ID", "course_id is required.")
        source_ids = body.get("source_ids")
        target_days = int(body.get("target_days") or 14)
        daily_hours = float(body.get("daily_hours") or 3.0)
        target_subject_name = str(body.get("target_subject_name") or body.get("subject_name") or "").strip()
        raw_subject_names = body.get("subject_names")
        subject_names = (
            [str(x).strip() for x in raw_subject_names if str(x).strip()]
            if isinstance(raw_subject_names, list)
            else None
        )
        append_subject = bool(body.get("append_subject", False))
        try:
            result = await synthesize_course_sources(
                db,
                course_id,
                source_ids=source_ids if isinstance(source_ids, list) else None,
                target_days=target_days,
                daily_hours=daily_hours,
                target_subject_name=target_subject_name,
                subject_names=subject_names,
                append_subject=append_subject,
                owner_sub=str(auth.get("sub") or ""),
            )
            return JSONResponse(content=result)
        except AIServiceRequiredError as exc:
            return _err(exc.status_code, exc.error_code, exc.message)
        except KeyError as exc:
            return _err(404, "NOT_FOUND", str(exc))

    # ------------------------------------------------------------------
    # Sources: Search, Inspect-URL (PDF Harvester), Ingest, FTS5 Search
    # ------------------------------------------------------------------

    @app.post("/api/sources/search")
    async def api_sources_search(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        query = str(body.get("query") or "").strip()
        filt = str(body.get("filter") or body.get("provider_filter") or "all")
        if not query:
            return _err(400, "MISSING_QUERY", "Search query is required.")
        res = await search_sources(query, provider_filter=filt, db=db)
        return JSONResponse(content=res)

    @app.post("/api/sources/inspect-url")
    async def api_sources_inspect_url(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        url = str(body.get("url") or "").strip()
        try:
            inspected = await ainspect_url(url)
        except SSRFBlockedError as exc:
            return _err(400, "SSRF_BLOCKED", str(exc))
        except SourceParsingError as exc:
            return _err(400, "PARSE_ERROR", str(exc))
        except Exception as exc:
            return _err(502, "UPSTREAM_FETCH_ERROR", f"Failed to inspect URL: {exc}")

        excerpt = inspected["text"]
        return JSONResponse(
            content={
                "ok": True,
                "url": inspected["url"],
                "title": inspected["title"],
                "is_direct_pdf": inspected["is_direct_pdf"],
                "text": excerpt,
                "text_excerpt": excerpt,
                "excerpt": excerpt,
                "pdf_links": inspected["discovered_pdfs"],
                "discovered_pdfs": inspected["discovered_pdfs"],
            }
        )

    @app.post("/api/sources/ingest-file")
    async def api_sources_ingest_file(
        request: Request,
        course_id: str = Form(...),
        file: UploadFile = File(...),
        layout_type: str = Form("2x2"),
        taskbar_crop: int = Form(48),
        subject_name: str = Form(""),
    ) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth

        if db.get_course(course_id) is None:
            return _err(404, "NOT_FOUND", f"Course '{course_id}' not found.")

        clean_subj = subject_name.strip()
        filename = file.filename or "upload.bin"
        ext = Path(filename).suffix.lower()
        temp_path: Path | None = None
        try:
            if ext in _VIDEO_EXTS:
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    temp_path = Path(tmp.name)
                    while True:
                        chunk = await file.read(1024 * 1024)
                        if not chunk:
                            break
                        tmp.write(chunk)
                payload = SourceDispatcher.dispatch_file(
                    temp_path,
                    filename=filename,
                    layout_type=layout_type,
                    taskbar_crop_px=int(taskbar_crop),
                    subject_name=clean_subj,
                )
            else:
                chunks: list[bytes] = []
                total = 0
                cap = MAX_PDF_BYTES + 1024
                while True:
                    chunk = await file.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap:
                        return _err(400, "FILE_TOO_LARGE", "Uploaded file exceeds 50 MB limit.")
                    chunks.append(chunk)
                payload = SourceDispatcher.dispatch_file(b"".join(chunks), filename=filename)
                if clean_subj:
                    payload.metadata["subject_name"] = clean_subj

            saved = db.add_course_source(course_id, payload)
            return JSONResponse(status_code=201, content={"ok": True, "source": saved})
        except FileValidationError as exc:
            return _err(400, "INVALID_FILE", str(exc))
        except SourceParsingError as exc:
            return _err(400, "PARSE_ERROR", str(exc))
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    @app.post("/api/sources/ingest-url")
    async def api_sources_ingest_url(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        course_id = str(body.get("course_id") or "").strip()
        if not course_id or db.get_course(course_id) is None:
            return _err(404, "NOT_FOUND", "Target course not found.")

        url = str(body.get("url") or "").strip()
        urls = body.get("urls") if isinstance(body.get("urls"), list) else None
        search_query = str(body.get("search_query") or "").strip()

        if not url and not urls:
            return _err(400, "MISSING_URL", "Either 'url' or 'urls' is required.")

        try:
            result = await SourceDispatcher.adispatch_url(
                url or str(urls[0]),
                selected_pdf_urls=[str(u) for u in urls] if urls else None,
            )
            payloads = result if isinstance(result, list) else [result]
            added = [
                db.add_course_source(course_id, p, search_query=search_query)
                for p in payloads
            ]
            return JSONResponse(
                status_code=201,
                content={
                    "ok": True,
                    "source": added[0] if added else None,
                    "sources": added,
                },
            )
        except SSRFBlockedError as exc:
            return _err(400, "SSRF_BLOCKED", str(exc))
        except SourceParsingError as exc:
            return _err(400, "PARSE_ERROR", str(exc))
        except Exception as exc:
            return _err(502, "UPSTREAM_FETCH_ERROR", f"Failed to ingest URL: {exc}")

    @app.delete("/api/sources/{source_id}")
    async def api_delete_source(source_id: str, request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        deleted = db.delete_course_source(source_id)
        if not deleted:
            return _err(404, "NOT_FOUND", "Source not found.")
        return JSONResponse(content={"ok": True, "deleted": True})

    @app.get("/api/sources/fts")
    async def api_sources_fts(request: Request, q: str = "", course_id: str = "") -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        results = db.fts_search_sources(q, course_id=course_id or None)
        return JSONResponse(content={"ok": True, "query": q, "results": results})

    # ------------------------------------------------------------------
    # Slide Images (JPEG Magic + Size + Path Traversal Guards)
    # ------------------------------------------------------------------

    @app.post("/api/slides/upload")
    async def api_slides_upload(
        request: Request,
        course_id: str = Form(...),
        file: UploadFile = File(...),
        roi_key: str = Form("q1"),
        timestamp_sec: float = Form(0.0),
        key: str = Form(""),
    ) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth

        raw = await file.read(MAX_SLIDE_JPEG_BYTES + 1)
        if len(raw) == 0:
            return _err(400, "EMPTY_SLIDE", "Slide image is empty.")
        if len(raw) > MAX_SLIDE_JPEG_BYTES:
            return _err(400, "SLIDE_TOO_LARGE", "Slide image exceeds 2.5 MB limit.")
        if not raw.startswith(_JPEG_MAGIC):
            return _err(400, "INVALID_JPEG", "Slide image must start with JPEG magic bytes (FF D8 FF).")

        try:
            if key:
                clean_explicit = sanitize_local_slide_key(key)
                fname = clean_explicit.rsplit("/", 1)[-1]
            else:
                raw_fname = (file.filename or f"{roi_key}_{int(timestamp_sec)}.jpg").strip()
                if not raw_fname or ".." in raw_fname or "/" in raw_fname or "\\" in raw_fname or raw_fname.startswith("."):
                    raise ValueError("Invalid slide filename.")
                fname = raw_fname
            url = db.save_slide_bytes(course_id, fname, raw)
            stored_key = url[len("/api/slides/") :]
            return JSONResponse(
                status_code=201,
                content={"ok": True, "key": stored_key, "url": url, "size_bytes": len(raw)},
            )
        except ValueError as exc:
            return _err(400, "INVALID_SLIDE_KEY", str(exc))

    @app.get("/api/slides/{slide_path:path}")
    async def api_slides_get(slide_path: str, request: Request) -> Response:
        db = get_db()
        raw_path = request.scope.get("raw_path", b"").decode("latin-1", errors="ignore")
        raw_decoded_once = unquote(raw_path)
        raw_decoded_twice = unquote(raw_decoded_once)
        for cand in (raw_path, raw_decoded_once, raw_decoded_twice, slide_path, unquote(slide_path)):
            if ".." in cand or "\\" in cand or "\x00" in cand:
                return _err(400, "INVALID_SLIDE_PATH", "Path traversal is prohibited.")

        # Allow Admin session OR valid per-launch local_token (for <img> tags).
        has_token = app.state.require_local_token and check_local_token(request)
        if not has_token and extract_session(request, db) is None:
            return _err(401, "UNAUTHORIZED", "Authentication required to access slide assets.")

        try:
            clean_key = sanitize_local_slide_key(slide_path)
        except ValueError as exc:
            return _err(400, "INVALID_SLIDE_PATH", str(exc))

        res = db.read_slide_bytes(clean_key)
        if res is None:
            return _err(404, "NOT_FOUND", "Slide image not found.")
        img_bytes, mime = res
        return Response(content=img_bytes, media_type=mime)

    # ------------------------------------------------------------------
    # Progress, AI Rubric Grader & Cloud Sync
    # ------------------------------------------------------------------

    @app.post("/api/progress/task")
    async def api_progress_task(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        subject_id = str(body.get("subject_id") or "").strip()
        if not subject_id:
            return _err(400, "MISSING_SUBJECT_ID", "subject_id is required.")
        try:
            prog = db.update_task_progress(
                subject_id,
                task_id=body.get("task_id"),
                completed=body.get("completed"),
                completed_tasks=body.get("completed_tasks"),
            )
            return JSONResponse(content={"ok": True, "progress": prog})
        except KeyError as exc:
            return _err(404, "NOT_FOUND", str(exc))

    @app.post("/api/progress/quiz")
    async def api_progress_quiz(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        subject_id = str(body.get("subject_id") or "").strip()
        if not subject_id:
            return _err(400, "MISSING_SUBJECT_ID", "subject_id is required.")
        raw_score = float(body.get("score") or 0.0)
        weak_spots = body.get("weak_spots") if isinstance(body.get("weak_spots"), list) else None
        try:
            prog = db.update_quiz_progress(subject_id, raw_score, weak_spots=weak_spots)
            return JSONResponse(content={"ok": True, "progress": prog})
        except KeyError as exc:
            return _err(404, "NOT_FOUND", str(exc))

    @app.post("/api/exam/grade")
    async def api_exam_grade(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        try:
            res = await grade_exam_answer(
                db,
                subject_id=str(body.get("subject_id") or ""),
                question_id=str(body.get("question_id") or ""),
                student_answer=str(body.get("student_answer") or ""),
                question_prompt=str(body.get("question_text") or body.get("question_prompt") or ""),
                model_answer=str(body.get("model_answer") or ""),
                rubric=body.get("rubric"),
                max_marks=int(body.get("max_marks") or 5),
                owner_sub=str(auth.get("sub") or ""),
            )
            return JSONResponse(content=res)
        except AIServiceRequiredError as exc:
            return _err(exc.status_code, exc.error_code, exc.message)
        except ValueError as exc:
            return _err(400, "INVALID_ANSWER", str(exc))

    @app.post("/api/sync/push")
    async def api_sync_push(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db, admin_only=True)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        cloud_url = str(body.get("cloud_url") or os.environ.get("OMNILEARN_CLOUD_URL", "")).strip()
        admin_token = str(body.get("admin_token") or os.environ.get("OMNILEARN_SYNC_TOKEN", "")).strip()
        course_id = str(body.get("course_id") or "").strip() or None
        try:
            client = CloudSyncClient(cloud_url, admin_token=admin_token)
            res = await client.push_workspace(db, course_id=course_id)
            return JSONResponse(content=res)
        except Exception as exc:
            return _err(400, "SYNC_PUSH_FAILED", str(exc))

    @app.post("/api/sync/pull")
    async def api_sync_pull(request: Request) -> Response:
        db = get_db()
        auth = require_admin(request, db, admin_only=True)
        if isinstance(auth, JSONResponse):
            return auth
        body = await request.json()
        cloud_url = str(body.get("cloud_url") or os.environ.get("OMNILEARN_CLOUD_URL", "")).strip()
        admin_token = str(body.get("admin_token") or os.environ.get("OMNILEARN_SYNC_TOKEN", "")).strip()
        try:
            client = CloudSyncClient(cloud_url, admin_token=admin_token)
            res = await client.pull_workspace(db)
            return JSONResponse(content=res)
        except Exception as exc:
            return _err(400, "SYNC_PULL_FAILED", str(exc))

    # Unknown /api/* routes return JSON 404 rather than SPA index.html
    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def api_not_found(rest: str) -> JSONResponse:
        return _err(404, "NOT_FOUND", f"API endpoint '/api/{rest}' not found.")

    # Serve shared Google Lit + Pure CSS3 frontend from public/
    if resolved_pub.is_dir():
        app.mount("/", StaticFiles(directory=str(resolved_pub), html=True), name="public_static")

    return app


app = create_app()

__all__ = ["app", "create_app"]
