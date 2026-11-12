"""End-to-end API integration, Worker router, JS syntax, SW bypass, route parity, and headless smoke tests."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.local_app.app_launcher import build_arg_parser, find_free_loopback_port
from backend.local_app.local_db import LocalDatabase
from backend.local_app.sync_client import CloudSyncClient
from backend.src.main import handle_request
from tests.conftest import CaseInsensitiveHeaders, MockWorkerRequest, build_worker_env, extract_cookie_token

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = REPO_ROOT / "frontend"


def _auth_headers_from_init(local_api_harness: dict[str, Any]) -> dict[str, str]:
    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]
    resp = client.post(
        "/api/auth/init",
        headers=local_hdrs,
        json={"username": "admin", "password": "IntegrationPassword!1"},
    )
    assert resp.status_code == 201
    token = resp.json()["token"]
    return {**local_hdrs, "Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Local FastAPI Engine Full Integration (`backend/local_app/local_api.py`)
# ---------------------------------------------------------------------------


def test_local_api_token_guard_and_full_lifecycle(
    local_api_harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]
    db: LocalDatabase = local_api_harness["db"]

    # Health is accessible without token; protected routes require X-Local-App-Token
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["edition"] == "system_app"
    assert health.headers["cache-control"].startswith("no-store")
    assert client.get("/api/courses").status_code == 401

    # Static shell served at /
    index_resp = client.get("/")
    assert index_resp.status_code == 200
    assert "omnilearn-app" in index_resp.text.lower()

    # Auth lifecycle: status -> init -> logout -> login
    st_before = client.get("/api/auth/status", headers=local_hdrs).json()
    assert st_before["initialized"] is False
    assert st_before["authenticated"] is False

    auth_hdrs = _auth_headers_from_init(local_api_harness)
    assert client.get("/api/auth/status", headers=auth_hdrs).json()["authenticated"] is True

    # Seeded CS Core course present with 4 subjects
    courses = client.get("/api/courses", headers=auth_hdrs).json()["courses"]
    assert any(c["id"] == "course-sample-cs-core" for c in courses)
    sample = client.get("/api/courses/course-sample-cs-core", headers=auth_hdrs).json()
    assert len(sample["subjects"]) == 4

    # Create new course, ingest source, synthesize (offline fallback), update task & quiz progress
    c_resp = client.post(
        "/api/courses",
        headers=auth_hdrs,
        json={"title": "Algorithms Capstone", "layout_type": "mixed"},
    )
    assert c_resp.status_code == 201
    cid = c_resp.json()["course"]["id"]

    up_resp = client.post(
        "/api/sources/ingest-file",
        headers=auth_hdrs,
        data={"course_id": cid},
        files={
            "file": (
                "dp_notes.txt",
                b"Dynamic Programming breaks problems into overlapping subproblems with memoization.",
                "text/plain",
            )
        },
    )
    assert up_resp.status_code == 201

    # Without an API key or internet, synthesis must fail explicitly (no offline fallback)
    missing_key_resp = client.post(
        "/api/courses/synthesize",
        headers=auth_hdrs,
        json={"course_id": cid, "target_days": 7, "daily_hours": 2.0},
    )
    assert missing_key_resp.status_code == 400
    assert missing_key_resp.json()["error"] == "API_KEY_REQUIRED"

    # Add an API key & mock online AI provider response
    assert (
        client.post(
            "/api/pool/add",
            headers=auth_hdrs,
            json={"api_key": "AIzaSyTestLocalKey_1234567890", "provider": "gemini"},
        ).status_code
        == 200
    )

    async def fake_ai_pool(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "provider": "gemini",
            "cached": False,
            "data": {
                "title": "Dynamic Programming",
                "topic": "Memoization & Optimal Substructure",
                "summary": "Online AI synthesis of Dynamic Programming notes.",
                "key_concepts": [{"title": "Memoization", "explanation": "Cache subproblems.", "citation": "dp_notes.txt"}],
            },
        }

    monkeypatch.setattr("backend.local_app.gemini_local.call_ai_pool", fake_ai_pool)

    synth_resp = client.post(
        "/api/courses/synthesize",
        headers=auth_hdrs,
        json={"course_id": cid, "target_days": 7, "daily_hours": 2.0},
    )
    assert synth_resp.status_code == 200
    synth_data = synth_resp.json()
    assert synth_data["ok"] is True
    assert synth_data["used_fallback"] is False
    assert len(synth_data["subjects"]) >= 1
    synth_sid = synth_data["subjects"][0]["id"]

    # Day-level toggle on seeded ds-day-1..ds-day-14 must reach 100% completion cleanly
    completed_days = [f"ds-day-{i}" for i in range(1, 15)]
    p_task = client.post(
        "/api/progress/task",
        headers=auth_hdrs,
        json={"subject_id": "subj-ds", "completed_tasks": completed_days},
    )
    assert p_task.status_code == 200

    p_quiz = client.post(
        "/api/progress/quiz",
        headers=auth_hdrs,
        json={"subject_id": synth_sid, "score": 85.0, "weak_spots": ["State transitions"]},
    )
    assert p_quiz.status_code == 200
    assert p_quiz.json()["progress"]["mastery_score"] >= 50.0

    # Logout & re-login roundtrip
    assert client.post("/api/auth/logout", headers=auth_hdrs).status_code == 200
    login_resp = client.post(
        "/api/auth/login",
        headers=local_hdrs,
        json={"username": "admin", "password": "IntegrationPassword!1"},
    )
    assert login_resp.status_code == 200
    assert login_resp.json()["authenticated"] is True
    _ = db


def test_local_api_slide_fetch_with_local_token_query_param(local_api_harness: dict[str, Any]) -> None:
    client = local_api_harness["client"]
    local_token = local_api_harness["local_token"]
    auth_hdrs = _auth_headers_from_init(local_api_harness)

    jpg_data = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x11" * 40
    up = client.post(
        "/api/slides/upload",
        headers=auth_hdrs,
        data={"course_id": "course-sample-cs-core", "roi_key": "q1", "timestamp_sec": "3.0"},
        files={"file": ("frame_1.jpg", jpg_data, "image/jpeg")},
    )
    assert up.status_code == 201
    url_path = up.json()["url"]

    # <img> tags pass ?local_token=... without Bearer header
    fetched = client.get(f"{url_path}?local_token={local_token}")
    assert fetched.status_code == 200
    assert fetched.content == jpg_data
    assert fetched.headers["content-type"].startswith("image/jpeg")


# ---------------------------------------------------------------------------
# 2. Cloudflare Worker Router Integration (`backend/src/main.py:handle_request`)
# ---------------------------------------------------------------------------


async def test_worker_router_full_flow_and_guest_isolation(
    worker_env_web_lite: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = worker_env_web_lite

    # Health + Releases UA detection
    h = await handle_request(MockWorkerRequest("GET", "/api/health"), env)
    assert h.status == 200
    assert h.json()["edition"] == "web_lite"
    assert "no-store" in h.headers["Cache-Control"]

    rel = await handle_request(
        MockWorkerRequest(
            "GET",
            "/api/releases/latest",
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8)"},
        ),
        env,
    )
    assert rel.status == 200
    assert rel.json()["recommended_platform"] == "android"

    # Guest session: can read seeded CS Core course, cannot delete sample course
    guest = await handle_request(MockWorkerRequest("POST", "/api/auth/guest-lite"), env)
    assert guest.status == 200
    guest_cookie = extract_cookie_token(guest.headers.get("Set-Cookie", ""))
    guest_hdrs = {"Authorization": f"Bearer {guest_cookie}"}

    sample = await handle_request(
        MockWorkerRequest("GET", "/api/courses/course-sample-cs-core", headers=guest_hdrs),
        env,
    )
    assert sample.status == 200
    assert len(sample.json()["subjects"]) == 4

    del_denied = await handle_request(
        MockWorkerRequest("DELETE", "/api/courses/course-sample-cs-core", headers=guest_hdrs),
        env,
    )
    assert del_denied.status == 403

    # Admin flow: init -> create course -> ingest txt -> synthesize -> progress -> R2 slide -> sync
    init_resp = await handle_request(
        MockWorkerRequest("POST", "/api/auth/init", json_body={"password": "WorkerRouterPass!1"}),
        env,
    )
    assert init_resp.status == 201
    admin_cookie = extract_cookie_token(init_resp.headers.get("Set-Cookie", ""))
    admin_hdrs = {"Authorization": f"Bearer {admin_cookie}"}

    c_resp = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/courses",
            headers=admin_hdrs,
            json_body={"title": "Worker Calculus", "layout_type": "mixed"},
        ),
        env,
    )
    assert c_resp.status == 201
    cid = c_resp.json()["course"]["id"]

    ing = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/sources/ingest-file",
            headers=admin_hdrs,
            form_data={"course_id": cid, "file": ("limits.txt", b"Epsilon-delta limit proofs and continuity.")},
        ),
        env,
    )
    assert ing.status == 201

    # Without any API key configured, Worker synthesis rejects offline fallback
    synth_no_key = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/courses/synthesize",
            headers=admin_hdrs,
            json_body={"course_id": cid, "target_days": 7, "daily_hours": 2.0},
        ),
        env,
    )
    assert synth_no_key.status == 400
    assert synth_no_key.json()["error"] == "API_KEY_REQUIRED"

    assert (
        await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/pool/add",
                headers=admin_hdrs,
                json_body={"provider": "gemini", "api_key": "AIzaSyWorkerTestKey_12345678"},
            ),
            env,
        )
    ).status in (200, 201)

    async def fake_worker_ai(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "provider": "gemini",
            "cached": False,
            "data": {
                "title": "Limits & Continuity",
                "topic": "Epsilon-Delta Proofs",
                "summary": "Online synthesized calculus limit proofs.",
                "key_concepts": [{"title": "Epsilon-Delta", "explanation": "|x-a|<delta => |f(x)-L|<eps", "citation": "limits.txt"}],
            },
        }

    monkeypatch.setattr("backend.src.gemini_client.call_ai_with_pool", fake_worker_ai)

    synth = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/courses/synthesize",
            headers=admin_hdrs,
            json_body={"course_id": cid, "target_days": 7, "daily_hours": 2.0},
        ),
        env,
    )
    assert synth.status == 200
    assert synth.json()["used_fallback"] is False
    sid = synth.json()["subjects"][0]["id"]

    ptask = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/progress/task",
            headers=admin_hdrs,
            json_body={"subject_id": sid, "task_id": "d1", "completed": True},
        ),
        env,
    )
    assert ptask.status == 200
    assert "d1" in ptask.json()["completed_tasks"]

    pquiz = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/progress/quiz",
            headers=admin_hdrs,
            json_body={"subject_id": sid, "score": 3, "total": 3, "weak_spots": []},
        ),
        env,
    )
    assert pquiz.status == 200
    assert pquiz.json()["last_quiz_score"] == 100.0

    # R2 slide upload + GET fetch roundtrip
    jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x44" * 20
    up_slide = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/slides/upload",
            headers=admin_hdrs,
            form_data={"course_id": cid, "file": ("q1_0.jpg", jpg)},
        ),
        env,
    )
    assert up_slide.status == 201
    slide_url = up_slide.json()["url"]
    got_slide = await handle_request(MockWorkerRequest("GET", slide_url, headers=admin_hdrs), env)
    assert got_slide.status == 200
    assert got_slide.body == jpg


async def test_cloud_sync_roundtrip_between_local_app_and_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end CloudSyncClient <-> Worker `/api/sync/push` & `/api/sync/pull` roundtrip."""
    monkeypatch.setenv("OMNILEARN_DISABLE_KEYRING", "1")
    src_db = LocalDatabase(db_path=tmp_path / "src.db", assets_dir=tmp_path / "src_assets", seed_sample_data=True)
    dst_db = LocalDatabase(db_path=tmp_path / "dst.db", assets_dir=tmp_path / "dst_assets", seed_sample_data=False)

    created = src_db.create_course("Synced Quantum Computing")
    src_db.save_synthesized_course(
        created["id"],
        [
            {
                "id": "subj-qc-1",
                "quadrant_key": "q1",
                "title": "Qubits & Gates",
                "short_title": "Qubits",
                "summary": "Superposition and Hadamard gates.",
                "key_concepts": ["Superposition", "Entanglement"],
                "math_formulas": ["H|0\\rangle = \\frac{|0\\rangle + |1\\rangle}{\\sqrt{2}}"],
                "study_plan": [{"id": "qc-day-1", "day": 1, "title": "Day 1", "tasks": []}],
                "quizzes": [{"id": "q1", "question": "Hadamard matrix?", "options": ["H", "X"], "answer_index": 0}],
                "model_paper": {
                    "title": "QC Midterm",
                    "questions": [{"id": "e1", "question": "Define Bell state.", "max_marks": 5}],
                },
            }
        ],
    )

    worker_env = build_worker_env(runtime_mode="web_lite", with_seed=False)
    try:
        init_resp = await handle_request(
            MockWorkerRequest("POST", "/api/auth/init", json_body={"password": "SyncWorkerPass!1"}),
            worker_env,
        )
        assert init_resp.status == 201
        admin_token = extract_cookie_token(init_resp.headers.get("Set-Cookie", ""))

        async def _worker_asgi_transport(request: httpx.Request) -> httpx.Response:
            raw_body = request.content
            headers = CaseInsensitiveHeaders(dict(request.headers.items()))
            form_data = None
            content_type = headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type.lower():
                json_body = None
            else:
                json_body = None
            wreq = MockWorkerRequest(
                request.method,
                str(request.url),
                headers=headers,
                json_body=json_body,
                raw_body=raw_body,
                form_data=form_data,
            )
            wresp = await handle_request(wreq, worker_env)
            body = wresp.body if isinstance(wresp.body, (bytes, bytearray)) else str(wresp.body).encode("utf-8")
            return httpx.Response(wresp.status, content=body, headers=dict(wresp.headers))

        sync_client = CloudSyncClient("https://omnilearn.example", admin_token=admin_token)
        async with httpx.AsyncClient(transport=httpx.MockTransport(_worker_asgi_transport)) as http_client:
            push_res = await sync_client.push_workspace(src_db, course_id=created["id"], client=http_client)
            assert push_res["ok"] is True

            pull_res = await sync_client.pull_workspace(dst_db, client=http_client)
            assert pull_res["ok"] is True
            assert pull_res["imported_courses"] >= 1

        pulled_bundle = dst_db.get_course(created["id"])
        assert pulled_bundle is not None
        assert pulled_bundle["course"]["title"] == "Synced Quantum Computing"
        assert len(pulled_bundle["subjects"]) == 1
        assert pulled_bundle["subjects"][0]["title"] == "Qubits & Gates"
    finally:
        worker_env.DB.close()


# ---------------------------------------------------------------------------
# 3. Frontend JS Syntax, SW `/api/*` Bypass & Contract Parity
# ---------------------------------------------------------------------------


def test_all_frontend_js_files_pass_node_syntax_check() -> None:
    js_files = sorted(PUBLIC_DIR.rglob("*.js"))
    assert len(js_files) >= 15
    for path in js_files:
        proc = subprocess.run(
            ["node", "--check", str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"node --check failed on {path}: {proc.stderr}"


def test_service_worker_explicitly_bypasses_api_routes_at_runtime() -> None:
    node_test = """
    import fs from 'node:fs';
    const swCode = fs.readFileSync('./frontend/sw.js', 'utf8');
    const listeners = {};
    const cacheCalls = [];
    const fetchCalls = [];
    const sandbox = {
      self: {
        location: { origin: 'https://omnilearn.example' },
        addEventListener: (ev, fn) => { listeners[ev] = fn; },
        skipWaiting: async () => {},
        clients: { claim: async () => {} },
      },
      caches: {
        open: async () => ({ addAll: async () => {}, put: async (req) => cacheCalls.push(req.url || req) }),
        keys: async () => [],
        delete: async () => true,
        match: async () => { throw new Error('caches.match must not be called for /api/*'); },
      },
      fetch: async (req) => {
        fetchCalls.push(req.url || req);
        return { ok: true, clone: () => ({ ok: true }) };
      },
      URL,
    };
    const fn = new Function('self', 'caches', 'fetch', 'URL', swCode);
    fn(sandbox.self, sandbox.caches, sandbox.fetch, sandbox.URL);

    let apiResponded = false;
    listeners.fetch({
      request: { method: 'GET', url: 'https://omnilearn.example/api/courses' },
      respondWith: () => { apiResponded = true; },
    });

    let shellResponded = false;
    listeners.fetch({
      request: { method: 'GET', url: 'https://omnilearn.example/index.html', mode: 'navigate' },
      respondWith: (p) => { shellResponded = Boolean(p); },
    });
    console.log(JSON.stringify({ apiResponded, shellResponded }));
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", node_test],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["apiResponded"] is False, "Service worker MUST NOT intercept /api/* requests"
    assert out["shellResponded"] is True


def test_route_parity_between_frontend_worker_and_local_api() -> None:
    api_client_js = (PUBLIC_DIR / "js" / "api-client.js").read_text(encoding="utf-8")
    main_py = (REPO_ROOT / "backend" / "src" / "main.py").read_text(encoding="utf-8")
    local_api_py = (REPO_ROOT / "backend" / "local_app" / "local_api.py").read_text(encoding="utf-8")

    # Collect static `/api/...` endpoints invoked by `api-client.js`
    raw_routes = set(re.findall(r"['\"`](/api/[a-zA-Z0-9_\-/{}$]+)", api_client_js))
    normalized_routes = set()
    for r in raw_routes:
        clean = r.split("?")[0]
        if "${" in clean or clean == "/api/slides/":
            continue
        normalized_routes.add(clean)

    expected_routes = {
        "/api/health",
        "/api/releases/latest",
        "/api/auth/status",
        "/api/auth/init",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/guest-lite",
        "/api/pool",
        "/api/pool/add",
        "/api/courses",
        "/api/courses/synthesize",
        "/api/sources/search",
        "/api/sources/inspect-url",
        "/api/sources/ingest-file",
        "/api/sources/ingest-url",
        "/api/sources/fts",
        "/api/slides/upload",
        "/api/progress/task",
        "/api/progress/quiz",
        "/api/exam/grade",
    }
    assert expected_routes.issubset(normalized_routes)

    for route in normalized_routes:
        assert f'"{route}"' in main_py or f"'{route}'" in main_py, f"Route {route} missing in backend/src/main.py"
        assert f'"{route}"' in local_api_py or f"'{route}'" in local_api_py, (
            f"Route {route} missing in backend/local_app/local_api.py"
        )

    # Verify parameterised DELETE/GET routes also exist in both backends
    for pat in ("/api/pool/", "/api/courses/", "/api/sources/", "/api/slides/"):
        assert pat in main_py
        assert pat in local_api_py

    # Verify frontend getSlideUrl preserves data:image/svg+xml;base64 and prefixes relative paths cleanly
    node_slide_check = """
    import { apiClient } from './frontend/js/api-client.js';
    console.log(JSON.stringify({
      dataUri: apiClient.getSlideUrl('data:image/svg+xml;base64,PHN2Zz48L3N2Zz4='),
      httpsUri: apiClient.getSlideUrl('https://cdn.example.org/img.jpg'),
      relKey: apiClient.getSlideUrl('courses/c1/q1.jpg'),
    }));
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", node_slide_check],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    slide_res = json.loads(proc.stdout.strip())
    assert slide_res["dataUri"] == "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="
    assert slide_res["httpsUri"] == "https://cdn.example.org/img.jpg"
    assert slide_res["relKey"] == "/api/slides/courses/c1/q1.jpg"


def test_mobile_responsive_css_and_manifest_contract() -> None:
    css_combined = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((PUBLIC_DIR / "css").glob("*.css"))
    )
    assert "@media (max-width: 899px)" in css_combined
    assert "48px" in css_combined
    assert "safe-area-inset-bottom" in css_combined

    manifest = json.loads((PUBLIC_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("display") == "standalone"
    assert len(manifest.get("icons") or []) >= 1


# ---------------------------------------------------------------------------
# 4. Headless `app_launcher.py` Subprocess Smoke Check
# ---------------------------------------------------------------------------


def test_app_launcher_cli_parser_and_headless_startup(tmp_path: Path) -> None:
    parsed = build_arg_parser().parse_args(
        ["--headless", "--port", "0", "--db-path", str(tmp_path / "cli.db"), "--no-token"]
    )
    assert parsed.headless is True
    assert parsed.no_token is True

    port = find_free_loopback_port(0)
    db_file = tmp_path / "headless_smoke.db"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backend.local_app.app_launcher",
            "--headless",
            "--port",
            str(port),
            "--db-path",
            str(db_file),
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 12.0
        health_data: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            assert proc.poll() is None, f"Launcher exited early: {proc.stderr.read() if proc.stderr else ''}"
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
                if r.status_code == 200:
                    health_data = r.json()
                    break
            except Exception:
                time.sleep(0.15)
        assert health_data is not None, "Headless app launcher did not become healthy within 12s"
        assert health_data["edition"] == "system_app"

        shell = httpx.get(f"http://127.0.0.1:{port}/", timeout=2.0)
        assert shell.status_code == 200
        assert "omnilearn-app" in shell.text.lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


# ---------------------------------------------------------------------------
# 6. .env Admin + Multi-API Keys, Unlimited Subjects, & 4-Quadrant Lecture Mode
# ---------------------------------------------------------------------------


def test_dotenv_admin_credentials_and_zero_leak_multi_api_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from backend.local_app.env_config import load_dotenv_file, parse_env_file
    from backend.local_app.local_api import create_app

    monkeypatch.setenv("OMNILEARN_DISABLE_KEYRING", "1")
    monkeypatch.delenv("GEMINI_KEY_1", raising=False)
    monkeypatch.delenv("GEMINI_KEY_2", raising=False)
    monkeypatch.delenv("GEMINI_KEY_3", raising=False)
    monkeypatch.delenv("GEMINI_KEY_4", raising=False)

    env_file = tmp_path / ".env"
    secret_single = "AIzaSyEnvSingleKey_11111111"
    secret_list_a = "AIzaSyEnvListA_22222222"
    secret_list_b = "AIzaSyEnvListB_33333333"
    secret_or = "sk-or-test-key-444444444"
    env_file.write_text(
        "\n".join(
            [
                "OMNILEARN_ADMIN_USERNAME=prof_hardik",
                "OMNILEARN_ADMIN_PASSWORD=HardikSuperSecret!99",
                f"GEMINI_API_KEY={secret_single}",
                f"GEMINI_API_KEYS={secret_list_a}, {secret_list_b}",
                f"OPENROUTER_API_KEYS={secret_or}",
                "OMNILEARN_REQUIRE_INTERNET=1",
            ]
        ),
        encoding="utf-8",
    )

    parsed = parse_env_file(env_file)
    for k in parsed:
        # Register pre-test env state with monkeypatch BEFORE load_dotenv_file mutates os.environ.
        monkeypatch.setenv(k, "")
    loaded = load_dotenv_file(env_file, override=True)
    assert loaded == env_file

    app = create_app(
        db_path=tmp_path / "env_test.db",
        local_token="env-token-abc",
        require_local_token=True,
    )
    with TestClient(app) as client:
        hdrs = {"X-Local-App-Token": "env-token-abc"}
        assert client.get("/api/health").json()["internet_required"] is True

        # Wrong username rejected even with right password
        bad_user = client.post(
            "/api/auth/login",
            headers=hdrs,
            json={"username": "admin", "password": "HardikSuperSecret!99"},
        )
        assert bad_user.status_code == 401

        # Exact .env username + password succeeds
        good_login = client.post(
            "/api/auth/login",
            headers=hdrs,
            json={"username": "prof_hardik", "password": "HardikSuperSecret!99"},
        )
        assert good_login.status_code == 200
        auth_hdrs = {**hdrs, "Authorization": f"Bearer {good_login.json()['token']}"}

        pool = client.get("/api/pool", headers=auth_hdrs).json()
        assert pool["env_keys_configured"] == 4
        assert pool["total_active_keys"] == 4
        raw_pool = json.dumps(pool)
        for secret in (secret_single, secret_list_a, secret_list_b, secret_or):
            assert secret not in raw_pool
            assert secret[-6:] not in raw_pool


def test_multi_window_video_quadrants_merge_into_one_subject_with_four_lectures(
    local_api_harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = local_api_harness["client"]
    auth_hdrs = _auth_headers_from_init(local_api_harness)
    db: LocalDatabase = local_api_harness["db"]

    assert (
        client.post(
            "/api/pool/add",
            headers=auth_hdrs,
            json={"api_key": "AIzaSyQuadrantTest_1234567890", "provider": "gemini"},
        ).status_code
        == 200
    )

    course = db.create_course(
        "OS Multi-Lecture Course",
        layout_type="2x2",
        rois=[
            {"roi_key": "top_left", "name": "Top-Left", "ymin": 0, "ymax": 500, "xmin": 0, "xmax": 500},
            {"roi_key": "top_right", "name": "Top-Right", "ymin": 0, "ymax": 500, "xmin": 500, "xmax": 1000},
            {"roi_key": "bottom_left", "name": "Bottom-Left", "ymin": 500, "ymax": 1000, "xmin": 0, "xmax": 500},
            {"roi_key": "bottom_right", "name": "Bottom-Right", "ymin": 500, "ymax": 1000, "xmin": 500, "xmax": 1000},
        ],
    )
    cid = course["id"]

    async def fake_quadrant_ai(db_arg: Any, prompt: str, **kwargs: Any) -> dict[str, Any]:
        assert "ALL 4 quadrant windows" in prompt
        assert "ONE single subject" in prompt
        return {
            "provider": "gemini",
            "cached": False,
            "data": {
                "title": "Operating Systems",
                "topic": "4 Unified Quadrant Lectures",
                "summary": "Combines Lecture 1 (Processes), Lecture 2 (Threads), Lecture 3 (Scheduling), and Lecture 4 (Virtual Memory).",
                "key_concepts": [
                    {"title": "Lecture 1: Processes", "explanation": "PCB & context switch.", "citation": "top_left"},
                    {"title": "Lecture 4: Paging", "explanation": "TLB & page tables.", "citation": "bottom_right"},
                ],
            },
        }

    monkeypatch.setattr("backend.local_app.gemini_local.call_ai_pool", fake_quadrant_ai)

    resp = client.post(
        "/api/courses/synthesize",
        headers=auth_hdrs,
        json={"course_id": cid, "target_subject_name": "Operating Systems"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["subjects"]) == 1
    subj = body["subjects"][0]
    assert subj["title"] == "Operating Systems"
    transcript_blob = json.dumps(subj["subtitles_transcript"])
    for qkey in ("top_left", "top_right", "bottom_left", "bottom_right"):
        assert qkey in transcript_blob


def test_unlimited_multiple_subjects_beyond_four(
    local_api_harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = local_api_harness["client"]
    auth_hdrs = _auth_headers_from_init(local_api_harness)

    assert (
        client.post(
            "/api/pool/add",
            headers=auth_hdrs,
            json={"api_key": "AIzaSyMultiSubjTest_123456789", "provider": "gemini"},
        ).status_code
        == 200
    )

    c_resp = client.post(
        "/api/courses",
        headers=auth_hdrs,
        json={"title": "Full B.Tech CS Track", "layout_type": "mixed"},
    )
    assert c_resp.status_code == 201
    cid = c_resp.json()["course"]["id"]

    # Create 5 manual subjects directly (>4 subjects allowed)
    for idx in range(1, 6):
        s_resp = client.post(
            f"/api/courses/{cid}/subjects",
            headers=auth_hdrs,
            json={"title": f"Subject {idx} Track"},
        )
        assert s_resp.status_code == 201

    # Also synthesize & append a 6th subject without wiping the first 5
    async def fake_append_ai(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "provider": "gemini",
            "cached": False,
            "data": {
                "title": "Compiler Design",
                "topic": "Lexing, Parsing & IR Optimization",
                "summary": "Synthesized Compiler Design curriculum appended cleanly.",
            },
        }

    monkeypatch.setattr("backend.local_app.gemini_local.call_ai_pool", fake_append_ai)

    synth_resp = client.post(
        "/api/courses/synthesize",
        headers=auth_hdrs,
        json={
            "course_id": cid,
            "target_subject_name": "Compiler Design",
            "append_subject": True,
        },
    )
    assert synth_resp.status_code == 200
    detail = client.get(f"/api/courses/{cid}", headers=auth_hdrs).json()
    assert len(detail["subjects"]) == 6


def test_service_worker_online_only_policy() -> None:
    sw_text = (PUBLIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert "caches.match(" not in sw_text
    assert "Internet connection required" in sw_text
