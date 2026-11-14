"""Edition Guard enforcing Cloudflare Free-Plan Web Lite caps & Release Hub queries."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

try:
    from .db import d1_all, d1_first, env_get, env_str
except ImportError:
    from db import d1_all, d1_first, env_get, env_str

try:
    from workers import Response as _WorkersResponse  # type: ignore

    HAS_WORKERS_RESPONSE = True
except ImportError:
    _WorkersResponse = None  # type: ignore
    HAS_WORKERS_RESPONSE = False

MAX_WEB_LITE_SOURCES = 2
MAX_WEB_LITE_API_KEYS = 1
MAX_WEB_LITE_FILE_BYTES = 500 * 1024  # 500 KB
MAX_WEB_LITE_SLIDES = 8
MAX_WEB_LITE_SEARCH_HITS = 3
MAX_WEB_LITE_QUIZ_QUESTIONS = 3
MAX_WEB_LITE_EXAM_QUESTIONS = 1
MAX_WEB_LITE_NOTES_CONCEPTS = 2

LOCKED_SOURCE_TYPES = frozenset({"pdf", "xlsx", "xls", "ppt", "pptx", "youtube", "web_pdf"})
LOCKED_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".ppt", ".pptx")

API_SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://esm.sh https://cdn.jsdelivr.net https://accounts.google.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://accounts.google.com; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com; "
        "frame-src https://accounts.google.com; "
        "frame-ancestors 'none'"
    ),
}


class WorkerResponse:
    """CPython-compatible HTTP Response mirror matching Cloudflare Workers Response."""

    def __init__(
        self,
        body: str | bytes = "",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.status = int(status)
        self.headers: dict[str, str] = dict(headers or {})

    @property
    def status_code(self) -> int:
        return self.status

    def text(self) -> str:
        if isinstance(self.body, bytes):
            return self.body.decode("utf-8", errors="replace")
        return self.body

    def json(self) -> Any:
        return json.loads(self.text())


def make_response(
    body: str | bytes,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> Any:
    """Create a Cloudflare Workers Response or CPython WorkerResponse."""
    merged_headers = dict(API_SECURITY_HEADERS)
    if headers:
        merged_headers.update(headers)
    if HAS_WORKERS_RESPONSE and _WorkersResponse is not None:
        return _WorkersResponse(body, status=status, headers=merged_headers)
    return WorkerResponse(body=body, status=status, headers=merged_headers)


def make_json_response(
    data: Any,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> Any:
    """Serialize payload as JSON with mandatory /api/* security and no-store headers."""
    merged = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        merged.update(headers)
    return make_response(json.dumps(data, ensure_ascii=False), status=status, headers=merged)


def get_runtime_mode(env: Any = None) -> str:
    """Return active runtime edition: 'web_lite' (default) or 'system_app'."""
    mode = env_str(env, "RUNTIME_MODE") or os.environ.get("RUNTIME_MODE", "")
    if mode.strip().lower() == "system_app":
        return "system_app"
    return "web_lite"


def is_web_lite(env: Any = None) -> bool:
    """Return True when running in Cloudflare Web Lite mode."""
    return get_runtime_mode(env) == "web_lite"


def get_edition_capabilities(env: Any = None) -> dict[str, Any]:
    """Return edition capability metadata consumed by frontend edition-gate.js."""
    if not is_web_lite(env):
        return {
            "edition": "system_app",
            "max_sources_per_course": None,
            "max_api_keys": None,
            "max_file_size_bytes": None,
            "max_video_slides": None,
            "max_search_hits": None,
            "max_quiz_questions": None,
            "max_exam_questions": None,
            "max_notes_concepts": None,
            "allowed_source_types": [
                "video",
                "pdf",
                "txt",
                "xlsx",
                "ppt",
                "csv",
                "web_url",
                "web_pdf",
                "youtube",
            ],
            "locked_features": [],
        }

    return {
        "edition": "web_lite",
        "max_sources_per_course": MAX_WEB_LITE_SOURCES,
        "max_api_keys": MAX_WEB_LITE_API_KEYS,
        "max_file_size_bytes": MAX_WEB_LITE_FILE_BYTES,
        "max_video_slides": MAX_WEB_LITE_SLIDES,
        "max_search_hits": MAX_WEB_LITE_SEARCH_HITS,
        "max_quiz_questions": MAX_WEB_LITE_QUIZ_QUESTIONS,
        "max_exam_questions": MAX_WEB_LITE_EXAM_QUESTIONS,
        "max_notes_concepts": MAX_WEB_LITE_NOTES_CONCEPTS,
        "allowed_source_types": ["txt", "csv", "video", "web_url"],
        "locked_features": [
            "pdf",
            "xlsx",
            "ppt",
            "youtube",
            "web_pdf",
            "inspect_url",
            "fts",
            "ai_grader",
            "full_exam",
            "exports",
            "multi_api_pool",
        ],
    }


DEFAULT_APP_RELEASES: list[dict[str, Any]] = [
    {
        "id": "rel-win-1.0.0",
        "version": "1.0.0",
        "platform": "windows",
        "arch": "x64",
        "filename": "OmniLearn-Setup-1.0.0.exe",
        "download_url": "https://github.com/Hardik1036/OmniLearn-AI/releases/latest",
        "sha256": "verified-on-release-checksum",
        "size_bytes": 50331648,
        "is_latest": True,
        "release_notes": "Full Windows System App (Edge WebView2 + Local FastAPI + SQLite WAL/FTS5 + OpenCV + All 9 Source Parsers)",
        "source_fallback_command": "python -m backend.local_app.app_launcher",
    },
    {
        "id": "rel-mac-1.0.0",
        "version": "1.0.0",
        "platform": "macos",
        "arch": "universal",
        "filename": "OmniLearn-1.0.0-universal.dmg",
        "download_url": "https://github.com/Hardik1036/OmniLearn-AI/releases/latest",
        "sha256": "verified-on-release-checksum",
        "size_bytes": 56623104,
        "is_latest": True,
        "release_notes": "Full macOS Universal System App (WKWebView + Keychain AES-GCM + SQLite WAL/FTS5 + All 9 Source Parsers)",
        "source_fallback_command": "python -m backend.local_app.app_launcher",
    },
    {
        "id": "rel-linux-1.0.0",
        "version": "1.0.0",
        "platform": "linux",
        "arch": "x64",
        "filename": "OmniLearn-1.0.0-x86_64.AppImage",
        "download_url": "https://github.com/Hardik1036/OmniLearn-AI/releases/latest",
        "sha256": "verified-on-release-checksum",
        "size_bytes": 54525952,
        "is_latest": True,
        "release_notes": "Full Linux AppImage (WebKitGTK + SecretService + SQLite WAL/FTS5 + All 9 Source Parsers)",
        "source_fallback_command": "python -m backend.local_app.app_launcher",
    },
    {
        "id": "rel-android-1.0.0",
        "version": "1.0.0",
        "platform": "android",
        "arch": "universal",
        "filename": "OmniLearn-1.0.0.apk",
        "download_url": "https://github.com/Hardik1036/OmniLearn-AI/releases/latest",
        "sha256": "verified-on-release-checksum",
        "size_bytes": 14680064,
        "is_latest": True,
        "release_notes": "Full Android Capacitor APK (Responsive Touch UI + Offline Local SQLite + Cloud Sync)",
        "source_fallback_command": "python -m backend.local_app.app_launcher",
    },
]


def detect_client_platform(user_agent: str = "") -> str:
    """Detect user OS platform from User-Agent for Release Hub highlighting."""
    ua = (user_agent or "").lower()
    if "android" in ua:
        return "android"
    if "mac os x" in ua or "macintosh" in ua or "darwin" in ua:
        return "macos"
    if "linux" in ua or "x11" in ua:
        return "linux"
    return "windows"


async def get_latest_releases(env: Any) -> list[dict[str, Any]]:
    """Fetch latest installer binaries from Cloudflare D1 `app_releases`."""
    if env_get(env, "DB", None) is None:
        return [dict(r) for r in DEFAULT_APP_RELEASES]
    try:
        rows = await d1_all(
            env,
            """
            SELECT id, version, platform, arch, filename, download_url, sha256,
                   size_bytes, is_latest, release_notes, created_at
            FROM app_releases
            WHERE is_latest = 1
            ORDER BY CASE platform
                WHEN 'windows' THEN 1
                WHEN 'macos' THEN 2
                WHEN 'linux' THEN 3
                WHEN 'android' THEN 4
                ELSE 5
            END, created_at DESC
            """,
        )
    except Exception:
        return [dict(r) for r in DEFAULT_APP_RELEASES]

    if not rows:
        return [dict(r) for r in DEFAULT_APP_RELEASES]

    releases: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["is_latest"] = bool(item.get("is_latest", 1))
        if "size_bytes" in item and item["size_bytes"] is not None:
            item["size_bytes"] = int(item["size_bytes"])
        dl_url = str(item.get("download_url") or "")
        if "github.com/omnilearn-ai/omnilearn" in dl_url:
            dl_url = dl_url.replace("github.com/omnilearn-ai/omnilearn", "github.com/Hardik1036/OmniLearn-AI")
        item["download_url"] = dl_url
        item["source_fallback_command"] = "python -m backend.local_app.app_launcher"
        releases.append(item)
    return releases


def infer_source_type(payload: dict[str, Any] | None) -> str:
    """Infer normalized source_type from explicit field, filename, or URL."""
    if not payload:
        return ""
    raw_type = str(payload.get("source_type") or "").strip().lower()
    if raw_type:
        if raw_type == "pptx":
            return "ppt"
        if raw_type == "xls":
            return "xlsx"
        if raw_type == "md":
            return "txt"
        return raw_type

    filename = str(payload.get("filename") or payload.get("title") or "").strip().lower()
    if filename.endswith(".pdf"):
        return "pdf"
    if filename.endswith((".xlsx", ".xls")):
        return "xlsx"
    if filename.endswith((".ppt", ".pptx")):
        return "ppt"
    if filename.endswith(".csv"):
        return "csv"
    if filename.endswith((".txt", ".md")):
        return "txt"
    if filename.endswith((".mp4", ".mkv", ".webm", ".mov")):
        return "video"

    url = str(payload.get("url") or payload.get("origin_url") or "").strip()
    if url:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        if "youtube.com" in host or "youtu.be" in host:
            return "youtube"
        if path.endswith(".pdf"):
            return "web_pdf"
        return "web_url"
    return ""


def edition_limit_payload(feature: str, message: str) -> dict[str, Any]:
    """Build standard HTTP 403 JSON error payload for Web Lite gates."""
    return {
        "ok": False,
        "error": "EDITION_LIMIT_EXCEEDED",
        "feature": feature,
        "message": message,
        "edition": "web_lite",
        "upgrade_required": True,
    }


def _sync_auth_error(session: dict[str, Any] | None) -> tuple[int, dict[str, Any]] | None:
    if not session:
        return 401, {
            "ok": False,
            "error": "AUTH_REQUIRED",
            "message": "Admin sign-in is required to access cloud sync.",
        }
    if session.get("role") != "admin":
        return 403, {
            "ok": False,
            "error": "ADMIN_REQUIRED",
            "message": "Authenticated Admin session is required for cloud sync.",
        }
    return None


async def check_web_lite_limit(
    endpoint: str,
    payload: dict[str, Any] | None = None,
    env: Any = None,
    session: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]] | None:
    """Evaluate Web Lite limits and Admin Sync access; returns `(status, dict)` or None."""
    norm_ep = (endpoint or "").strip().lower()
    data = payload or {}

    # Cloud sync is unlocked ONLY for authenticated Admin sessions (even in web_lite).
    if norm_ep.startswith("/api/sync") or norm_ep in ("sync", "sync_push", "sync_pull"):
        return _sync_auth_error(session)

    if not is_web_lite(env):
        return None

    # 1. Locked heavy endpoints and feature identifiers
    if norm_ep in ("/api/exam/grade", "ai_grader", "exam_grade") or data.get("feature") == "ai_grader":
        return 403, edition_limit_payload(
            "ai_grader",
            "AI Rubric Grader requires the downloaded OmniLearn System App.",
        )

    if norm_ep in ("/api/exam/full", "full_exam") or data.get("feature") == "full_exam" or data.get("full_exam"):
        return 403, edition_limit_payload(
            "full_exam",
            "Full 3-section University Model Papers require the downloaded OmniLearn System App.",
        )

    if norm_ep in ("/api/sources/fts", "fts", "fts_search") or data.get("feature") == "fts":
        return 403, edition_limit_payload(
            "fts_search",
            "SQLite FTS5 full-text search across all materials runs locally in the OmniLearn System App.",
        )

    if norm_ep in ("/api/sources/inspect-url", "inspect_url", "web_pdf_crawler") or data.get("feature") == "inspect_url":
        return 403, edition_limit_payload(
            "inspect_url",
            "Website PDF crawling and multi-link extraction require the downloaded OmniLearn System App.",
        )

    if (
        norm_ep.startswith("/api/export")
        or norm_ep in ("exports", "export", "export_anki", "export_notes", "export_paper")
        or data.get("feature") in ("exports", "export")
    ):
        return 403, edition_limit_payload(
            "exports",
            "Markdown, Anki CSV, and Model Paper exports are unlocked in the downloaded OmniLearn System App.",
        )

    # 2. Search provider filters
    if norm_ep in ("/api/sources/search", "sources_search"):
        search_filter = str(data.get("filter") or data.get("provider_filter") or "all").strip().lower()
        if search_filter not in ("all", "academic"):
            return 403, edition_limit_payload(
                "full_source_search",
                "Web, Direct PDF, and YouTube in-app search require the OmniLearn System App. Web Lite supports OpenAlex abstract previews (up to 3 hits).",
            )

    # 3. Source type gates (pdf, xlsx, ppt, youtube, web_pdf)
    src_type = infer_source_type(data)
    if not src_type and norm_ep in LOCKED_SOURCE_TYPES:
        src_type = norm_ep
    if src_type in LOCKED_SOURCE_TYPES:
        feature_map = {
            "youtube": "youtube_ingestion",
            "web_pdf": "web_pdf_crawler",
        }
        feature = feature_map.get(src_type, "pdf_xlsx_ppt_ingestion")
        return 403, edition_limit_payload(
            feature,
            f"Download OmniLearn System App to unlock {src_type.upper()} ingestion and full multi-source synthesis.",
        )

    # 4. File size cap (<= 500 KB for .txt/.csv in Web Lite)
    size_bytes = data.get("size_bytes")
    if size_bytes is not None:
        try:
            if int(size_bytes) > MAX_WEB_LITE_FILE_BYTES:
                return 403, edition_limit_payload(
                    "file_size_limit",
                    "Web Lite limits .txt/.csv uploads to 500 KB. Download OmniLearn System App for full document parsing.",
                )
        except (TypeError, ValueError):
            pass

    # 5. Sources per course cap (<= 2 sources in Web Lite)
    if norm_ep in ("/api/sources/ingest-file", "/api/sources/ingest-url", "sources_ingest", "add_source") or "source_count" in data or "existing_sources" in data:
        explicit_count = data.get("source_count")
        existing_sources = data.get("existing_sources")
        if explicit_count is not None and int(explicit_count) > MAX_WEB_LITE_SOURCES:
            return 403, edition_limit_payload(
                "multi_source_limit",
                "Web Lite is limited to 2 sources per course. Download OmniLearn System App for unlimited mixed sources.",
            )
        if existing_sources is not None and int(existing_sources) >= MAX_WEB_LITE_SOURCES:
            return 403, edition_limit_payload(
                "multi_source_limit",
                "Web Lite is limited to 2 sources per course. Download OmniLearn System App for unlimited mixed sources.",
            )
        course_id = data.get("course_id")
        if existing_sources is None and explicit_count is None and course_id and env_get(env, "DB", None) is not None:
            row = await d1_first(
                env,
                "SELECT COUNT(*) AS cnt FROM course_sources WHERE course_id = ?",
                (str(course_id),),
            )
            if row and int(row.get("cnt") or 0) >= MAX_WEB_LITE_SOURCES:
                return 403, edition_limit_payload(
                    "multi_source_limit",
                    "Web Lite is limited to 2 sources per course. Download OmniLearn System App for unlimited mixed sources.",
                )

    # 6. Multi-API Key Pool cap (<= 1 key in Web Lite)
    if norm_ep in ("/api/pool/add", "pool_add", "api_pool") or "api_keys" in data or "api_keys_count" in data or "existing_keys" in data:
        for count_key in ("api_keys", "api_keys_count"):
            if count_key in data and data[count_key] is not None and int(data[count_key]) > MAX_WEB_LITE_API_KEYS:
                return 403, edition_limit_payload(
                    "multi_api_pool",
                    "Web Lite caps the API pool at 1 key slot. Download OmniLearn System App for unlimited Multi-API rotation.",
                )
        existing_keys = data.get("existing_keys")
        if existing_keys is not None and int(existing_keys) >= MAX_WEB_LITE_API_KEYS:
            return 403, edition_limit_payload(
                "multi_api_pool",
                "Web Lite caps the API pool at 1 key slot. Download OmniLearn System App for unlimited Multi-API rotation.",
            )
        if existing_keys is None and env_get(env, "DB", None) is not None and norm_ep in ("/api/pool/add", "pool_add", "api_pool"):
            owner_sub = str((session or {}).get("sub") or "").strip()
            if owner_sub:
                row = await d1_first(
                    env,
                    "SELECT COUNT(*) AS cnt FROM api_keys WHERE is_default = 0 AND owner_sub = ? AND status != 'INVALID'",
                    (owner_sub,),
                )
            else:
                row = await d1_first(
                    env,
                    "SELECT COUNT(*) AS cnt FROM api_keys WHERE is_default = 0 AND status != 'INVALID'",
                )
            if row and int(row.get("cnt") or 0) >= MAX_WEB_LITE_API_KEYS:
                return 403, edition_limit_payload(
                    "multi_api_pool",
                    "Web Lite caps the API pool at 1 key slot. Download OmniLearn System App for unlimited Multi-API rotation.",
                )

    # 7. Browser canvas slide cap (<= 8 slides in Web Lite)
    if norm_ep in ("/api/slides/upload", "slides_upload", "/api/courses/synthesize") or "slide_count" in data or "total_slides" in data:
        for k in ("slide_count", "total_slides"):
            if data.get(k) is not None and int(data[k]) > MAX_WEB_LITE_SLIDES:
                return 403, edition_limit_payload(
                    "video_slide_limit",
                    "Web Lite caps browser video slicing at 8 slides. Download OmniLearn System App for unlimited OpenCV processing.",
                )
        slides_list = data.get("slides")
        if isinstance(slides_list, list) and len(slides_list) > MAX_WEB_LITE_SLIDES:
            return 403, edition_limit_payload(
                "video_slide_limit",
                "Web Lite caps browser video slicing at 8 slides. Download OmniLearn System App for unlimited OpenCV processing.",
            )

    return None


async def enforce_web_lite_limits(
    request: Any,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    env: Any = None,
    session: dict[str, Any] | None = None,
) -> Any | None:
    """Return an HTTP 403/401 Response if blocked by Web Lite / Sync gates, else None."""
    blocked = await check_web_lite_limit(endpoint=endpoint, payload=payload, env=env, session=session)
    if blocked is None:
        return None
    status, body = blocked
    return make_json_response(body, status=status)
