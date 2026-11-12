"""Security test suite covering zero-leakage API keys, PBKDF2 ordering, SSRF, traversal, magic bytes, and CSV sanitization."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.local_app.local_db import sanitize_local_slide_key
from backend.local_app.parsers import FileValidationError, SSRFBlockedError
from backend.local_app.parsers.doc_parsers import parse_csv, parse_pdf, parse_xlsx, sanitize_spreadsheet_cell
from backend.local_app.parsers.web_crawler import ainspect_url, validate_public_http_url as crawler_validate_url
from backend.src.gemini_client import (
    parse_safe_text_or_csv,
    sanitize_csv_cell,
    validate_public_http_url as worker_validate_url,
)
from backend.src.main import handle_request
from backend.src.storage import sanitize_r2_key, validate_jpeg_bytes
from tests.conftest import MockWorkerRequest, extract_cookie_token

REPO_ROOT = Path(__file__).resolve().parent.parent

SECRET_GEMINI_KEY = "AIzaSySecretPrefixMidLeak9876543210ZZZ"
SECRET_GROQ_KEY = "gsk_SecretGroqPrefixLeak5544332211QQQ"


def _assert_no_key_leakage(payload_text: str, raw_key: str) -> None:
    """Assert neither full key, 6-char prefix, nor 4-char suffix appears in serialized payloads."""
    prefix = raw_key[:6]
    suffix = raw_key[-4:]
    assert raw_key not in payload_text, f"Plaintext key leaked in response: {payload_text}"
    assert prefix not in payload_text, f"Key prefix '{prefix}' leaked in response: {payload_text}"
    assert suffix not in payload_text, f"Key suffix '{suffix}' leaked in response: {payload_text}"


# ---------------------------------------------------------------------------
# 1. Zero-Leakage API Key Enforcement (Local FastAPI + Cloudflare Worker)
# ---------------------------------------------------------------------------


def test_local_api_zero_key_leakage(local_api_harness: dict[str, Any]) -> None:
    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]
    db = local_api_harness["db"]

    init_resp = client.post(
        "/api/auth/init",
        headers=local_hdrs,
        json={
            "username": "admin",
            "password": "StrongPassword!123",
            "api_key": SECRET_GEMINI_KEY,
            "provider": "gemini",
        },
    )
    assert init_resp.status_code == 201
    _assert_no_key_leakage(init_resp.text, SECRET_GEMINI_KEY)

    token = init_resp.json()["token"]
    auth_hdrs = {**local_hdrs, "Authorization": f"Bearer {token}"}

    add_resp = client.post(
        "/api/pool/add",
        headers=auth_hdrs,
        json={"api_key": SECRET_GROQ_KEY, "provider": "groq"},
    )
    assert add_resp.status_code == 200
    _assert_no_key_leakage(add_resp.text, SECRET_GEMINI_KEY)
    _assert_no_key_leakage(add_resp.text, SECRET_GROQ_KEY)

    login_resp = client.post(
        "/api/auth/login",
        headers=local_hdrs,
        json={"username": "admin", "password": "StrongPassword!123"},
    )
    assert login_resp.status_code == 200
    _assert_no_key_leakage(login_resp.text, SECRET_GEMINI_KEY)
    _assert_no_key_leakage(login_resp.text, SECRET_GROQ_KEY)

    for endpoint in (
        "/api/health",
        "/api/auth/status",
        "/api/pool",
        "/api/courses",
        "/api/courses/course-sample-cs-core",
    ):
        resp = client.get(endpoint, headers=auth_hdrs)
        assert resp.status_code == 200
        _assert_no_key_leakage(resp.text, SECRET_GEMINI_KEY)
        _assert_no_key_leakage(resp.text, SECRET_GROQ_KEY)

    # Snapshot export must exclude admin_users and api_keys completely
    snapshot_text = json.dumps(db.export_workspace_snapshot())
    assert "api_keys" not in snapshot_text
    assert "admin_users" not in snapshot_text
    _assert_no_key_leakage(snapshot_text, SECRET_GEMINI_KEY)
    _assert_no_key_leakage(snapshot_text, SECRET_GROQ_KEY)


async def test_worker_zero_key_leakage(worker_env_web_lite: Any) -> None:
    env = worker_env_web_lite

    init_req = MockWorkerRequest(
        "POST",
        "/api/auth/init",
        json_body={
            "username": "admin",
            "password": "WorkerAdminPassword!9",
            "api_key": SECRET_GEMINI_KEY,
            "provider": "gemini",
        },
    )
    init_resp = await handle_request(init_req, env)
    assert init_resp.status == 201
    _assert_no_key_leakage(init_resp.text(), SECRET_GEMINI_KEY)

    cookie = extract_cookie_token(init_resp.headers.get("Set-Cookie", ""))
    assert cookie
    auth_hdrs = {"Cookie": f"omnilearn_session={cookie}", "Authorization": f"Bearer {cookie}"}

    login_resp = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/auth/login",
            json_body={"password": "WorkerAdminPassword!9"},
        ),
        env,
    )
    assert login_resp.status == 200
    _assert_no_key_leakage(login_resp.text(), SECRET_GEMINI_KEY)

    for path in ("/api/health", "/api/pool", "/api/courses", "/api/courses/course-sample-cs-core"):
        resp = await handle_request(MockWorkerRequest("GET", path, headers=auth_hdrs), env)
        assert resp.status == 200
        _assert_no_key_leakage(resp.text(), SECRET_GEMINI_KEY)


# ---------------------------------------------------------------------------
# 2. PBKDF2 Verified BEFORE Adding Optional Key During Login
# ---------------------------------------------------------------------------


def test_local_login_verifies_pbkdf2_before_adding_key(local_api_harness: dict[str, Any]) -> None:
    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]
    db = local_api_harness["db"]

    init_resp = client.post(
        "/api/auth/init",
        headers=local_hdrs,
        json={"username": "admin", "password": "CorrectAdminPassword!"},
    )
    assert init_resp.status_code == 201
    assert db.get_pool_summary()["total_active_keys"] == 0

    bad_login = client.post(
        "/api/auth/login",
        headers=local_hdrs,
        json={
            "username": "admin",
            "password": "WrongPassword!",
            "api_key": SECRET_GEMINI_KEY,
            "provider": "gemini",
        },
    )
    assert bad_login.status_code == 401
    assert db.get_pool_summary()["total_active_keys"] == 0


async def test_worker_login_verifies_pbkdf2_before_adding_key_and_rate_limits(
    worker_env_web_lite: Any,
) -> None:
    env = worker_env_web_lite
    await handle_request(
        MockWorkerRequest("POST", "/api/auth/init", json_body={"password": "CorrectWorkerPass!"}),
        env,
    )

    for attempt in range(5):
        bad_resp = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/auth/login",
                headers={"CF-Connecting-IP": "198.51.100.42"},
                json_body={
                    "password": f"WrongAttempt{attempt}",
                    "api_key": SECRET_GEMINI_KEY,
                    "provider": "gemini",
                },
            ),
            env,
        )
        assert bad_resp.status == 401

    rows = env.DB.conn.execute("SELECT COUNT(*) AS cnt FROM api_keys").fetchone()
    assert rows["cnt"] == 0

    locked_resp = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/auth/login",
            headers={"CF-Connecting-IP": "198.51.100.42"},
            json_body={"password": "CorrectWorkerPass!"},
        ),
        env,
    )
    assert locked_resp.status == 429


# ---------------------------------------------------------------------------
# 3. SSRF Blocking (Local Crawler + Worker URL Guard + HTTP Endpoints)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ssrf_url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal",
        "file:///etc/passwd",
        "http://[::1]:8080/",
        "http://localhost:8787/api/health",
    ],
)
def test_ssrf_blocked_in_both_validators_and_local_api(
    ssrf_url: str,
    local_api_harness: dict[str, Any],
) -> None:
    with pytest.raises(SSRFBlockedError):
        crawler_validate_url(ssrf_url)

    with pytest.raises(ValueError):
        worker_validate_url(ssrf_url)

    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]
    init_resp = client.post(
        "/api/auth/init",
        headers=local_hdrs,
        json={"password": "AdminPassword!1"},
    )
    auth_hdrs = {**local_hdrs, "Authorization": f"Bearer {init_resp.json()['token']}"}

    inspect_resp = client.post("/api/sources/inspect-url", headers=auth_hdrs, json={"url": ssrf_url})
    assert inspect_resp.status_code == 400
    assert inspect_resp.json()["error"] == "SSRF_BLOCKED"

    ingest_resp = client.post(
        "/api/sources/ingest-url",
        headers=auth_hdrs,
        json={"course_id": "course-sample-cs-core", "url": ssrf_url},
    )
    assert ingest_resp.status_code == 400
    assert ingest_resp.json()["error"] == "SSRF_BLOCKED"


async def test_crawler_blocks_ssrf_redirect_and_worker_blocks_ssrf(
    worker_env_system_app: Any,
) -> None:
    def _redirect_handler(request: httpx.Request) -> httpx.Response:
        if "public.example.org" in str(request.url):
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError("Should never follow redirect into link-local IP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_redirect_handler)) as client:
        with pytest.raises(SSRFBlockedError):
            await ainspect_url("https://public.example.org/notes", client=client)

    env = worker_env_system_app
    init_resp = await handle_request(
        MockWorkerRequest("POST", "/api/auth/init", json_body={"password": "WorkerAdmin!23"}),
        env,
    )
    cookie = extract_cookie_token(init_resp.headers.get("Set-Cookie", ""))
    auth_hdrs = {"Authorization": f"Bearer {cookie}"}

    for ssrf_url in (
        "http://127.0.0.1/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "file:///etc/passwd",
        "http://[::1]/",
    ):
        resp = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/sources/ingest-url",
                headers=auth_hdrs,
                json_body={"course_id": "course-sample-cs-core", "url": ssrf_url},
            ),
            env,
        )
        assert resp.status == 400
        assert resp.json()["error"] == "SSRF_BLOCKED"


# ---------------------------------------------------------------------------
# 4. R2 & Local Slide Path Traversal Rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    [
        "../etc/passwd",
        "courses/../../etc/passwd",
        "courses/c1/%2e%2e/secret.jpg",
        "courses/c1\\..\\secret.jpg",
        "courses/c1/.hidden.jpg",
        "not-courses/c1/slide.jpg",
        "courses/c1/bad\x00.jpg",
    ],
)
def test_slide_path_traversal_rejected_in_sanitizers_and_routes(
    bad_key: str,
    local_api_harness: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        sanitize_r2_key(bad_key)

    with pytest.raises(ValueError):
        sanitize_local_slide_key(bad_key)

    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]
    init_resp = client.post(
        "/api/auth/init",
        headers=local_hdrs,
        json={"password": "AdminPassword!1"},
    )
    auth_hdrs = {**local_hdrs, "Authorization": f"Bearer {init_resp.json()['token']}"}

    resp = client.get(f"/api/slides/{bad_key.replace(chr(0), '%00')}", headers=auth_hdrs)
    assert resp.status_code in (400, 404)


async def test_worker_slide_path_traversal_rejected(worker_env_web_lite: Any) -> None:
    env = worker_env_web_lite
    resp = await handle_request(
        MockWorkerRequest("GET", "/api/slides/courses/demo/%2e%2e/secret.jpg"),
        env,
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# 5. JPEG & Document Magic-Byte Enforcement
# ---------------------------------------------------------------------------


def test_jpeg_and_document_magic_byte_enforcement(local_api_harness: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_jpeg_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")

    validate_jpeg_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIFfakejpeg")

    with pytest.raises(FileValidationError):
        parse_pdf(b"%FAKE-NOT-A-PDF<html>oops</html>", filename="spoofed.pdf")

    with pytest.raises(FileValidationError):
        parse_xlsx(b"NOT_A_ZIP_HEADER", filename="spoofed.xlsx")

    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]
    init_resp = client.post(
        "/api/auth/init",
        headers=local_hdrs,
        json={"password": "AdminPassword!1"},
    )
    auth_hdrs = {**local_hdrs, "Authorization": f"Bearer {init_resp.json()['token']}"}

    bad_slide = client.post(
        "/api/slides/upload",
        headers=auth_hdrs,
        data={"course_id": "course-sample-cs-core", "roi_key": "q1", "timestamp_sec": "1.0"},
        files={"file": ("spoof.jpg", b"\x89PNG\r\n\x1a\nnotjpeg", "image/jpeg")},
    )
    assert bad_slide.status_code == 400
    assert bad_slide.json()["error"] == "INVALID_JPEG"

    good_slide = client.post(
        "/api/slides/upload",
        headers=auth_hdrs,
        data={"course_id": "course-sample-cs-core", "roi_key": "q1", "timestamp_sec": "2.0"},
        files={"file": ("valid.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIFvalid", "image/jpeg")},
    )
    assert good_slide.status_code == 201
    assert good_slide.json()["key"] == "courses/course-sample-cs-core/valid.jpg"

    bad_pdf = client.post(
        "/api/sources/ingest-file",
        headers=auth_hdrs,
        data={"course_id": "course-sample-cs-core"},
        files={"file": ("evil.pdf", b"<html>not a pdf</html>", "application/pdf")},
    )
    assert bad_pdf.status_code == 400
    assert bad_pdf.json()["error"] in ("INVALID_FILE", "PARSE_ERROR")


# ---------------------------------------------------------------------------
# 6. CSV Formula Injection Sanitization (Python Backends + Frontend Export JS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dangerous",
    [
        "=CMD|' /C calc'!A0",
        "+SUM(A1:A2)",
        "-1+1+cmd|' /C calc'!A0",
        "@SUM(1+1)*cmd|' /C calc'!A0",
        "\t=2+2",
        "\r=2+2",
    ],
)
def test_csv_formula_injection_sanitized(dangerous: str) -> None:
    sanitized_doc = sanitize_spreadsheet_cell(dangerous)
    assert sanitized_doc.startswith("'"), f"Expected leading quote in doc sanitizer: {sanitized_doc!r}"

    sanitized_worker = sanitize_csv_cell(dangerous)
    assert sanitized_worker.startswith("'"), f"Expected leading quote in worker sanitizer: {sanitized_worker!r}"

    csv_safe_field = f'"{dangerous}"'
    csv_bytes = f"term,payload\nconcept,{csv_safe_field}\n".encode("utf-8")
    parsed_local = parse_csv(csv_bytes, filename="formulas.csv")
    expected_prefix = f"'{dangerous.lstrip()}".replace("|", "\\|")[:6]
    assert expected_prefix in parsed_local.raw_text_excerpt

    parsed_worker = parse_safe_text_or_csv("formulas.csv", csv_bytes)
    assert f"'{dangerous.lstrip()}" in parsed_worker["raw_text_excerpt"]


def test_frontend_js_export_csv_formula_sanitization() -> None:
    node_script = (
        "import { sanitizeCsvCell } from './frontend/js/export-utils.js';"
        "const inputs = ['=CMD|calc', '+1+1', '-2+3', '@SUM(A1)', '\\t=1', '\\r=2', 'safe'];"
        "console.log(JSON.stringify(inputs.map(sanitizeCsvCell)));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    outputs = json.loads(proc.stdout.strip())
    for val in outputs[:-1]:
        assert val.startswith("'") or val.startswith("\"'"), f"JS sanitizeCsvCell missed dangerous cell: {val!r}"
    assert outputs[-1] == '"safe"'
