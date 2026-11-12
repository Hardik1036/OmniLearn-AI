"""Tests for Web Lite Edition Guard (`403 EDITION_LIMIT_EXCEEDED`) vs System App full unlock."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from backend.src.edition_guard import (
    MAX_WEB_LITE_API_KEYS,
    MAX_WEB_LITE_FILE_BYTES,
    MAX_WEB_LITE_SLIDES,
    MAX_WEB_LITE_SOURCES,
    check_web_lite_limit,
    get_edition_capabilities,
)
from backend.src.main import handle_request
from tests.conftest import MockWorkerRequest, extract_cookie_token


async def _init_and_admin_headers(env: Any, api_key: str | None = None) -> dict[str, str]:
    body: dict[str, Any] = {"username": "admin", "password": "EditionAdminPassword!1"}
    if api_key:
        body["api_key"] = api_key
        body["provider"] = "gemini"
    resp = await handle_request(MockWorkerRequest("POST", "/api/auth/init", json_body=body), env)
    assert resp.status == 201
    cookie = extract_cookie_token(resp.headers.get("Set-Cookie", ""))
    return {"Authorization": f"Bearer {cookie}"}


def _assert_edition_blocked(resp: Any, expected_feature: str | None = None) -> None:
    assert resp.status == 403, f"Expected 403 EDITION_LIMIT_EXCEEDED, got {resp.status}: {resp.text()}"
    data = resp.json()
    assert data["error"] == "EDITION_LIMIT_EXCEEDED"
    assert data["edition"] == "web_lite"
    assert data["upgrade_required"] is True
    if expected_feature:
        assert expected_feature in str(data.get("feature", "")).lower() or data.get("feature")


# ---------------------------------------------------------------------------
# Direct `check_web_lite_limit` & Worker HTTP route matrix in Web Lite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("/api/sources/ingest-file", {"filename": "slides.pdf", "source_type": "pdf", "size_bytes": 1024}),
        ("/api/sources/ingest-file", {"filename": "grades.xlsx", "source_type": "xlsx", "size_bytes": 1024}),
        ("/api/sources/ingest-file", {"filename": "lecture.ppt", "source_type": "ppt", "size_bytes": 1024}),
        ("/api/sources/ingest-file", {"filename": "deck.pptx", "source_type": "pptx", "size_bytes": 1024}),
        ("/api/sources/ingest-file", {"filename": "big.txt", "source_type": "txt", "size_bytes": MAX_WEB_LITE_FILE_BYTES + 1}),
        ("/api/sources/ingest-url", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "source_type": "youtube"}),
        ("/api/sources/ingest-url", {"url": "https://arxiv.org/pdf/2301.00001.pdf", "source_type": "web_pdf"}),
        ("/api/sources/inspect-url", {"url": "https://cs.stanford.edu/class/"}),
        ("/api/sources/fts", {"q": "trees"}),
        ("/api/exam/grade", {"subject_id": "subj-ds", "student_answer": "Stack uses LIFO order."}),
        ("/api/export", {"format": "csv"}),
        ("/api/slides/upload", {"slide_count": MAX_WEB_LITE_SLIDES + 1}),
    ],
)
async def test_web_lite_direct_guard_blocks_pro_features(
    endpoint: str,
    payload: dict[str, Any],
    worker_env_web_lite: Any,
    worker_env_system_app: Any,
) -> None:
    blocked = await check_web_lite_limit(
        endpoint,
        payload,
        env=worker_env_web_lite,
        session={"role": "admin", "sub": "admin"},
    )
    assert blocked is not None
    status, body = blocked
    assert status == 403
    assert body["error"] == "EDITION_LIMIT_EXCEEDED"
    assert body["upgrade_required"] is True

    allowed = await check_web_lite_limit(
        endpoint,
        payload,
        env=worker_env_system_app,
        session={"role": "admin", "sub": "admin"},
    )
    assert allowed is None


async def test_worker_web_lite_enforces_all_limits_and_allows_admin_sync(
    worker_env_web_lite: Any,
) -> None:
    env = worker_env_web_lite
    admin_hdrs = await _init_and_admin_headers(env)

    # Create fresh empty course
    c_resp = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/courses",
            headers=admin_hdrs,
            json_body={"title": "Web Lite Capped Course", "layout_type": "mixed"},
        ),
        env,
    )
    assert c_resp.status == 201
    cid = c_resp.json()["course"]["id"]

    # 1. File types blocked in web_lite (PDF, XLSX, PPT, PPTX) + >500 KB text
    for fname, content in (
        ("notes.pdf", b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"),
        ("budget.xlsx", b"PK\x03\x04fake"),
        ("slides.ppt", b"\xd0\xcf\x11\xe0fake"),
        ("deck.pptx", b"PK\x03\x04fake"),
        ("huge.txt", b"A" * (MAX_WEB_LITE_FILE_BYTES + 1)),
    ):
        resp = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/sources/ingest-file",
                headers=admin_hdrs,
                form_data={"course_id": cid, "file": (fname, content)},
            ),
            env,
        )
        _assert_edition_blocked(resp)

    # 2. YouTube, Web-PDF URLs, Inspect-URL, FTS, Grader, Export blocked
    for path, body in (
        ("/api/sources/ingest-url", {"course_id": cid, "url": "https://youtu.be/dQw4w9WgXcQ"}),
        ("/api/sources/ingest-url", {"course_id": cid, "url": "https://example.org/syllabus.pdf"}),
        ("/api/sources/inspect-url", {"course_id": cid, "url": "https://example.org/course"}),
        ("/api/exam/grade", {"subject_id": "subj-ds", "student_answer": "LIFO buffer"}),
        ("/api/export", {}),
    ):
        resp = await handle_request(
            MockWorkerRequest("POST", path, headers=admin_hdrs, json_body=body),
            env,
        )
        _assert_edition_blocked(resp)

    fts_resp = await handle_request(
        MockWorkerRequest("GET", "/api/sources/fts?q=binary", headers=admin_hdrs),
        env,
    )
    _assert_edition_blocked(fts_resp)

    # 3. >2 sources limit per course
    assert MAX_WEB_LITE_SOURCES == 2
    for idx in range(MAX_WEB_LITE_SOURCES):
        ok_txt = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/sources/ingest-file",
                headers=admin_hdrs,
                form_data={"course_id": cid, "file": (f"note_{idx}.txt", f"Valid chunk {idx}".encode())},
            ),
            env,
        )
        assert ok_txt.status == 201

    third_txt = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/sources/ingest-file",
            headers=admin_hdrs,
            form_data={"course_id": cid, "file": ("note_3.txt", b"Third note blocked")},
        ),
        env,
    )
    _assert_edition_blocked(third_txt)

    # 4. >1 API key limit in Web Lite
    assert MAX_WEB_LITE_API_KEYS == 1
    first_key = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/pool/add",
            headers=admin_hdrs,
            json_body={"api_key": "AIzaSyFirstWebLiteKey1234567890", "provider": "gemini"},
        ),
        env,
    )
    assert first_key.status == 201

    second_key = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/pool/add",
            headers=admin_hdrs,
            json_body={"api_key": "AIzaSySecondWebLiteKey1234567890", "provider": "gemini"},
        ),
        env,
    )
    _assert_edition_blocked(second_key)

    # 5. >8 video slides cap in Web Lite
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64
    for s_idx in range(1, MAX_WEB_LITE_SLIDES + 1):
        ok_slide = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/slides/upload",
                headers=admin_hdrs,
                form_data={"course_id": cid, "file": (f"slide_{s_idx}.jpg", jpeg_bytes)},
            ),
            env,
        )
        assert ok_slide.status == 201

    ninth_slide = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/slides/upload",
            headers=admin_hdrs,
            form_data={"course_id": cid, "file": ("slide_9.jpg", jpeg_bytes)},
        ),
        env,
    )
    _assert_edition_blocked(ninth_slide)

    # 6. Admin can access /api/sync/pull in web_lite; guest cannot
    sync_admin = await handle_request(
        MockWorkerRequest("POST", "/api/sync/pull", headers=admin_hdrs),
        env,
    )
    assert sync_admin.status == 200

    guest_resp = await handle_request(
        MockWorkerRequest("POST", "/api/auth/guest-lite"),
        env,
    )
    assert guest_resp.status == 200
    guest_cookie = extract_cookie_token(guest_resp.headers.get("Set-Cookie", ""))
    sync_guest = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/sync/pull",
            headers={"Authorization": f"Bearer {guest_cookie}"},
        ),
        env,
    )
    assert sync_guest.status == 403


# ---------------------------------------------------------------------------
# System App Mode Unlocks ALL Limits Cleanly (Worker system_app + Local FastAPI)
# ---------------------------------------------------------------------------


async def test_worker_system_app_unlocks_all_endpoints(
    worker_env_system_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = worker_env_system_app
    caps = get_edition_capabilities(env)
    assert caps["edition"] == "system_app"
    assert caps["max_sources_per_course"] is None

    admin_hdrs = await _init_and_admin_headers(env)
    c_resp = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/courses",
            headers=admin_hdrs,
            json_body={"title": "System App Unlocked Course", "layout_type": "mixed"},
        ),
        env,
    )
    assert c_resp.status == 201
    cid = c_resp.json()["course"]["id"]

    # Multiple API keys allowed
    for idx in range(3):
        k_resp = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/pool/add",
                headers=admin_hdrs,
                json_body={"api_key": f"AIzaSyUnlockedKey{idx}_1234567890123", "provider": "gemini"},
            ),
            env,
        )
        assert k_resp.status == 201

    # >2 sources & PDF/XLSX/PPTX/YouTube/Web-PDF allowed
    for fname in ("a.pdf", "b.xlsx", "c.pptx", "d.txt"):
        r = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/sources/ingest-file",
                headers=admin_hdrs,
                json_body={
                    "course_id": cid,
                    "filename": fname,
                    "data_b64": base64.b64encode(f"unique content for {fname}".encode("utf-8")).decode("ascii"),
                },
            ),
            env,
        )
        assert r.status == 201

    for url in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://example.org/lecture_notes.pdf",
    ):
        u_resp = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/sources/ingest-url",
                headers=admin_hdrs,
                json_body={"course_id": cid, "url": url},
            ),
            env,
        )
        assert u_resp.status == 201

    # AI grader requires live internet/provider (returns 503 when unreachable, 200 when online AI succeeds)
    async def fail_ai_pool(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("backend.src.main.call_ai_with_pool", fail_ai_pool)
    grade_offline = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/exam/grade",
            headers=admin_hdrs,
            json_body={"subject_id": "subj-ds", "student_answer": "Arrays offer O(1) indexed access."},
        ),
        env,
    )
    assert grade_offline.status == 503

    async def fake_grade_ai(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "provider": "gemini",
            "cached": False,
            "data": {"score": 4.5, "max_marks": 5, "feedback": "Accurate complexity explanation.", "missing_points": []},
        }

    monkeypatch.setattr("backend.src.main.call_ai_with_pool", fake_grade_ai)

    grade_resp = await handle_request(
        MockWorkerRequest(
            "POST",
            "/api/exam/grade",
            headers=admin_hdrs,
            json_body={"subject_id": "subj-ds", "student_answer": "Arrays offer O(1) indexed access."},
        ),
        env,
    )
    assert grade_resp.status == 200

    fts_resp = await handle_request(
        MockWorkerRequest("GET", "/api/sources/fts?q=a", headers=admin_hdrs),
        env,
    )
    assert fts_resp.status == 200

    export_resp = await handle_request(
        MockWorkerRequest("POST", "/api/export/markdown", headers=admin_hdrs),
        env,
    )
    assert export_resp.status == 200

    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
    for idx in range(1, 11):
        s_resp = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/slides/upload",
                headers=admin_hdrs,
                form_data={"course_id": cid, "file": (f"slide_{idx}.jpg", jpeg_bytes)},
            ),
            env,
        )
        assert s_resp.status == 201


def test_local_system_app_fastapi_unlocks_multi_keys_sources_fts_and_grader(
    local_api_harness: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]

    health = client.get("/api/health").json()
    assert health["edition"] == "system_app"
    assert health["fts_enabled"] is True

    init_resp = client.post(
        "/api/auth/init",
        headers=local_hdrs,
        json={"password": "SystemAppPassword!1"},
    )
    assert init_resp.status_code == 201
    auth_hdrs = {**local_hdrs, "Authorization": f"Bearer {init_resp.json()['token']}"}

    # Add 2 API keys (exceeding Web Lite cap of 1)
    for idx, prov in enumerate(("gemini", "groq")):
        r = client.post(
            "/api/pool/add",
            headers=auth_hdrs,
            json={"api_key": f"key_unlock_{idx}_abcdefghijklmno", "provider": prov},
        )
        assert r.status_code == 200

    # Create course & upload 3 text sources (exceeding Web Lite cap of 2)
    c_resp = client.post(
        "/api/courses",
        headers=auth_hdrs,
        json={"title": "Local System Course", "layout_type": "2x2"},
    )
    assert c_resp.status_code == 201
    cid = c_resp.json()["course"]["id"]

    for i in range(3):
        up = client.post(
            "/api/sources/ingest-file",
            headers=auth_hdrs,
            data={"course_id": cid},
            files={"file": (f"doc_{i}.txt", f"Dijkstra shortest path lecture note {i}".encode(), "text/plain")},
        )
        assert up.status_code == 201

    fts = client.get("/api/sources/fts", headers=auth_hdrs, params={"q": "Dijkstra", "course_id": cid})
    assert fts.status_code == 200
    assert len(fts.json()["results"]) >= 1

    # Without live network, AI grader returns explicit 503 online-required error
    async def fail_local_grade(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("backend.local_app.gemini_local.call_ai_pool", fail_local_grade)
    grade_down = client.post(
        "/api/exam/grade",
        headers=auth_hdrs,
        json={
            "subject_id": "subj-ds",
            "question_id": "q1",
            "question_text": "Compare BFS and DFS.",
            "student_answer": "BFS uses a queue for level-order shortest path; DFS uses recursion or a stack.",
            "model_answer": "BFS uses a queue; DFS uses a stack.",
            "max_marks": 5,
        },
    )
    assert grade_down.status_code == 503

    async def fake_local_grade(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "provider": "gemini",
            "cached": False,
            "data": {
                "score": 5.0,
                "max_marks": 5,
                "feedback": "Exact BFS/DFS comparison.",
                "strengths": ["Clear data structure choice"],
                "improvements": [],
            },
        }

    monkeypatch.setattr("backend.local_app.gemini_local.call_ai_pool", fake_local_grade)

    grade = client.post(
        "/api/exam/grade",
        headers=auth_hdrs,
        json={
            "subject_id": "subj-ds",
            "question_id": "q1",
            "question_text": "Compare BFS and DFS.",
            "student_answer": "BFS uses a queue for level-order shortest path; DFS uses recursion or a stack.",
            "model_answer": "BFS uses a queue; DFS uses a stack.",
            "max_marks": 5,
        },
    )
    assert grade.status_code == 200
    assert grade.json()["ok"] is True
    assert grade.json()["used_fallback"] is False
