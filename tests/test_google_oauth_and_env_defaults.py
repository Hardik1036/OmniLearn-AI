"""Tests for `.env` default API keys, personal-overrides-default routing, and Google OAuth sign-in."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from backend.local_app.gemini_local import synthesize_course_sources
from backend.local_app.google_auth import GoogleAuthError, validate_google_claims
from backend.local_app.local_db import LocalDatabase
from backend.src.api_pool import add_personal_api_key, get_candidate_keys, get_pool_summary, remove_api_key
from backend.src.auth import create_guest_lite_session
from backend.src.main import handle_request
from tests.conftest import MockWorkerRequest, build_worker_env

ENV_DEFAULT_GEMINI = "AIzaSyEnvDefaultSystemKey1122334455"
PERSONAL_USER_GROQ = "gsk_UserPersonalOverrideKey9988776655"
OTHER_USER_GEMINI = "AIzaSyOtherUserPersonalKey7766554433"
TEST_GOOGLE_CLIENT_ID = "1234567890-abcdefg.apps.googleusercontent.com"
TEST_GOOGLE_SECRET = "GOCSPX-TestSecretNeverLeakValue123"


def _assert_no_secrets_leaked(payload: Any, *secrets: str) -> None:
    dumped = json.dumps(payload) if not isinstance(payload, str) else payload
    for secret in secrets:
        assert secret not in dumped
        if len(secret) >= 10:
            assert secret[:6] not in dumped
            assert secret[-4:] not in dumped


def test_env_keys_encrypted_in_db_as_default_and_overridden_by_personal_key(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNILEARN_IGNORE_DOTENV", "1")
    monkeypatch.setenv("OMNILEARN_DISABLE_KEYRING", "1")
    monkeypatch.setenv("GEMINI_API_KEY", ENV_DEFAULT_GEMINI)

    db = LocalDatabase(
        db_path=tmp_path / "env_defaults.db",
        assets_dir=tmp_path / "assets",
        seed_sample_data=False,
    )

    # 1. `.env` default key is encrypted in `api_keys` (is_default=1) and NOT stored plaintext
    with db.connection() as conn:
        row = conn.execute(
            "SELECT owner_sub, is_default, provider, encrypted_key FROM api_keys WHERE is_default = 1"
        ).fetchone()
    assert row is not None
    assert row["owner_sub"] == "system_default"
    assert int(row["is_default"]) == 1
    assert ENV_DEFAULT_GEMINI not in str(row["encrypted_key"])

    # 2. Normal user without personal keys gets `.env` default keys
    alice_candidates = db.get_candidate_keys(owner_sub="alice@gmail.com")
    assert len(alice_candidates) == 1
    assert alice_candidates[0]["api_key"] == ENV_DEFAULT_GEMINI

    summary_default = db.get_pool_summary(owner_sub="alice@gmail.com")
    assert summary_default["using_source"] == "default_env"
    assert summary_default["default_keys_count"] == 1
    assert summary_default["personal_keys_count"] == 0
    _assert_no_secrets_leaked(summary_default, ENV_DEFAULT_GEMINI)

    # 3. Once Alice adds a personal API key, ONLY Alice's personal key is used (`.env` overridden!)
    added = db.add_api_key(PERSONAL_USER_GROQ, provider="groq", owner_sub="alice@gmail.com")
    assert added["ok"] is True
    assert added["is_default"] is False

    alice_override = db.get_candidate_keys(owner_sub="alice@gmail.com")
    assert [c["api_key"] for c in alice_override] == [PERSONAL_USER_GROQ]

    summary_personal = db.get_pool_summary(owner_sub="alice@gmail.com")
    assert summary_personal["using_source"] == "personal"
    assert summary_personal["personal_keys_count"] == 1
    assert summary_personal["default_keys_count"] == 1
    _assert_no_secrets_leaked(summary_personal, ENV_DEFAULT_GEMINI, PERSONAL_USER_GROQ)

    # 4. Bob (different Gmail user without personal key) still uses `.env` default key!
    bob_candidates = db.get_candidate_keys(owner_sub="bob@gmail.com")
    assert [c["api_key"] for c in bob_candidates] == [ENV_DEFAULT_GEMINI]

    # 5. Deleting `.env` default slot is protected; deleting Alice's personal key restores `.env` default
    default_slot = next(s for s in summary_personal["slots"] if s["is_default"])
    with pytest.raises(PermissionError):
        db.delete_api_key(default_slot["id"], owner_sub="alice@gmail.com")

    assert db.delete_api_key(added["slot_id"], owner_sub="alice@gmail.com") is True
    alice_restored = db.get_candidate_keys(owner_sub="alice@gmail.com")
    assert [c["api_key"] for c in alice_restored] == [ENV_DEFAULT_GEMINI]


@pytest.mark.asyncio
async def test_worker_api_pool_default_env_overridden_by_personal_key() -> None:
    env = build_worker_env(runtime_mode="system_app", with_seed=False)
    try:
        env.GEMINI_API_KEY = ENV_DEFAULT_GEMINI

        summary_before = await get_pool_summary(env, owner_sub="student@gmail.com")
        assert summary_before["using_source"] == "default_env"
        assert summary_before["total_active_keys"] == 1
        _assert_no_secrets_leaked(summary_before, ENV_DEFAULT_GEMINI)

        cands_before = await get_candidate_keys(env, owner_sub="student@gmail.com")
        assert [c["api_key"] for c in cands_before] == [ENV_DEFAULT_GEMINI]

        added = await add_personal_api_key(
            env,
            PERSONAL_USER_GROQ,
            provider="groq",
            owner_sub="student@gmail.com",
        )
        cands_after = await get_candidate_keys(env, owner_sub="student@gmail.com")
        assert [c["api_key"] for c in cands_after] == [PERSONAL_USER_GROQ]

        summary_after = await get_pool_summary(env, owner_sub="student@gmail.com")
        assert summary_after["using_source"] == "personal"
        _assert_no_secrets_leaked(summary_after, ENV_DEFAULT_GEMINI, PERSONAL_USER_GROQ)

        # Other users remain on default_env
        other_cands = await get_candidate_keys(env, owner_sub="other@gmail.com")
        assert [c["api_key"] for c in other_cands] == [ENV_DEFAULT_GEMINI]

        assert await remove_api_key(env, added["slot_id"], owner_sub="student@gmail.com") is True
        cands_reverted = await get_candidate_keys(env, owner_sub="student@gmail.com")
        assert [c["api_key"] for c in cands_reverted] == [ENV_DEFAULT_GEMINI]
    finally:
        env.DB.close()


def test_validate_google_claims_security_checks() -> None:
    future_exp = int(time.time()) + 3600
    valid = {
        "aud": TEST_GOOGLE_CLIENT_ID,
        "iss": "https://accounts.google.com",
        "exp": future_exp,
        "email_verified": True,
        "email": "Alice.Student@Gmail.com",
        "sub": "google-sub-12345",
        "name": "Alice Student",
    }
    parsed = validate_google_claims(valid, TEST_GOOGLE_CLIENT_ID)
    assert parsed["email"] == "alice.student@gmail.com"
    assert parsed["google_sub"] == "google-sub-12345"

    # Rejects wrong audience
    with pytest.raises(GoogleAuthError):
        validate_google_claims({**valid, "aud": "wrong-client-id"}, TEST_GOOGLE_CLIENT_ID)

    # Rejects untrusted issuer
    with pytest.raises(GoogleAuthError):
        validate_google_claims({**valid, "iss": "https://evil.example.com"}, TEST_GOOGLE_CLIENT_ID)

    # Rejects expired token
    with pytest.raises(GoogleAuthError):
        validate_google_claims({**valid, "exp": int(time.time()) - 10}, TEST_GOOGLE_CLIENT_ID)

    # Rejects unverified email
    with pytest.raises(GoogleAuthError):
        validate_google_claims({**valid, "email_verified": False}, TEST_GOOGLE_CLIENT_ID)


def test_local_api_google_gmail_signin_and_role_scoping(
    local_api_harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = local_api_harness["client"]
    hdrs = local_api_harness["headers"]
    db: LocalDatabase = local_api_harness["db"]

    # 1. Unconfigured Google OAuth -> disabled & 503
    cfg_off = client.get("/api/auth/google/config", headers=hdrs).json()
    assert cfg_off["enabled"] is False
    unconf = client.post("/api/auth/google", headers=hdrs, json={"id_token": "tok"}).json()
    assert unconf["error"] == "GOOGLE_OAUTH_NOT_CONFIGURED"

    # 2. Configure Google OAuth + default .env API key
    monkeypatch.setenv("GOOGLE_CLIENT_ID", TEST_GOOGLE_CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", TEST_GOOGLE_SECRET)
    monkeypatch.setenv("GEMINI_API_KEY", ENV_DEFAULT_GEMINI)

    cfg_on = client.get("/api/auth/google/config", headers=hdrs)
    assert cfg_on.status_code == 200
    assert cfg_on.json()["enabled"] is True
    assert cfg_on.json()["code_flow_enabled"] is True
    assert TEST_GOOGLE_SECRET not in cfg_on.text

    # 3. Invalid Google token rejected cleanly with 401
    async def fake_verify(id_token: str, expected_cid: str, **_kwargs: Any) -> dict[str, str]:
        if id_token == "valid-alice-google-token":
            return {
                "google_sub": "gsub-alice-001",
                "email": "alice@gmail.com",
                "name": "Alice Gmail",
                "picture": "",
            }
        raise GoogleAuthError("Google rejected the supplied ID token.", status_code=401)

    monkeypatch.setattr("backend.local_app.local_api.verify_google_id_token", fake_verify)

    bad_resp = client.post("/api/auth/google", headers=hdrs, json={"id_token": "forged-token"})
    assert bad_resp.status_code == 401
    assert bad_resp.json()["error"] == "INVALID_GOOGLE_TOKEN"

    # 4. Valid Gmail login creates `users` record, returns `role="user"`, and supports personal key
    good_resp = client.post(
        "/api/auth/google",
        headers=hdrs,
        json={
            "id_token": "valid-alice-google-token",
            "api_key": PERSONAL_USER_GROQ,
            "provider": "groq",
        },
    )
    assert good_resp.status_code == 200
    body = good_resp.json()
    assert body["authenticated"] is True
    assert body["role"] == "user"
    assert body["email"] == "alice@gmail.com"
    assert body["key_added"] is True
    assert body["pool"]["using_source"] == "personal"
    _assert_no_secrets_leaked(body, ENV_DEFAULT_GEMINI, PERSONAL_USER_GROQ, TEST_GOOGLE_SECRET)

    saved_user = db.get_user_by_email("alice@gmail.com")
    assert saved_user is not None
    assert saved_user["google_sub"] == "gsub-alice-001"

    user_hdrs = {**hdrs, "Authorization": f"Bearer {body['token']}"}

    # Gmail user can create a course & access pool, but Admin-only delete course is 403
    created = client.post("/api/courses", headers=user_hdrs, json={"title": "Gmail User Study Course"})
    assert created.status_code == 201
    course_id = created.json()["course"]["id"]

    denied_del = client.delete(f"/api/courses/{course_id}", headers=user_hdrs)
    assert denied_del.status_code == 403
    assert denied_del.json()["error"] == "ADMIN_REQUIRED"

    # Redirect OAuth start builds Google consent URL containing Client ID but never Secret
    start_resp = client.get("/api/auth/google/start?format=json", headers=hdrs)
    assert start_resp.status_code == 200
    auth_url = start_resp.json()["auth_url"]
    assert TEST_GOOGLE_CLIENT_ID in auth_url
    assert TEST_GOOGLE_SECRET not in auth_url


@pytest.mark.asyncio
async def test_worker_google_gmail_signin_and_guest_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    env = build_worker_env(runtime_mode="web_lite", with_seed=True)
    try:
        env.GOOGLE_CLIENT_ID = TEST_GOOGLE_CLIENT_ID
        env.GOOGLE_CLIENT_SECRET = TEST_GOOGLE_SECRET
        env.GEMINI_API_KEY = ENV_DEFAULT_GEMINI

        async def fake_worker_verify(id_token: str, expected_cid: str) -> dict[str, str]:
            assert expected_cid == TEST_GOOGLE_CLIENT_ID
            if id_token == "worker-valid-gmail":
                return {
                    "google_sub": "gsub-worker-99",
                    "email": "student.web@gmail.com",
                    "name": "Student Web",
                    "picture": "",
                }
            raise PermissionError("Google rejected the supplied ID token.")

        monkeypatch.setattr("backend.src.auth.verify_google_id_token", fake_worker_verify)

        # 1. Invalid token -> 401
        bad_req = MockWorkerRequest(
            "POST",
            "/api/auth/google",
            json_body={"id_token": "bad-token"},
        )
        bad_res = await handle_request(bad_req, env)
        assert bad_res.status == 401

        # 2. Valid Google login -> role='user', personal key optional, default overridden
        ok_req = MockWorkerRequest(
            "POST",
            "/api/auth/google",
            json_body={
                "id_token": "worker-valid-gmail",
                "api_key": PERSONAL_USER_GROQ,
                "provider": "groq",
            },
        )
        ok_res = await handle_request(ok_req, env)
        assert ok_res.status == 200
        ok_json = ok_res.json()
        assert ok_json["role"] == "user"
        assert ok_json["email"] == "student.web@gmail.com"
        assert ok_json["pool"]["using_source"] == "personal"
        _assert_no_secrets_leaked(ok_json, ENV_DEFAULT_GEMINI, PERSONAL_USER_GROQ, TEST_GOOGLE_SECRET)

        # 3. Guest-lite remains blocked from adding API keys
        guest = await create_guest_lite_session(env)
        guest_req = MockWorkerRequest(
            "POST",
            "/api/pool/add",
            headers={"Authorization": f"Bearer {guest['token']}"},
            json_body={"api_key": OTHER_USER_GEMINI, "provider": "gemini"},
        )
        guest_res = await handle_request(guest_req, env)
        assert guest_res.status == 403
    finally:
        env.DB.close()


@pytest.mark.asyncio
async def test_cloudflare_worker_ai_relay_security_and_end_to_end_local_fallback(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """System App user without any local API keys seamlessly uses Cloudflare Worker AI relay."""
    worker_env = build_worker_env(runtime_mode="web_lite", with_seed=True)
    worker_env.GEMINI_API_KEY = ENV_DEFAULT_GEMINI
    worker_env.GOOGLE_CLIENT_ID = TEST_GOOGLE_CLIENT_ID

    async def fake_worker_verify(id_token: str, expected_cid: str) -> dict[str, str]:
        if id_token == "valid-desktop-gmail":
            return {
                "google_sub": "gsub-relay-001",
                "email": "desktop.user@gmail.com",
                "name": "Desktop User",
                "picture": "",
            }
        raise PermissionError("Invalid Google token.")

    async def fake_worker_http_request(
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, str]:
        assert ENV_DEFAULT_GEMINI in url
        return (
            200,
            json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {
                                                "title": "Operating Systems",
                                                "topic": "Process Scheduling",
                                                "summary": "Synthesized via Cloudflare AI Relay without exposing secrets.",
                                                "mermaid_diagram": "flowchart TD\nA[Ready] --> B[Running]",
                                                "key_concepts": [
                                                    {
                                                        "title": "Context Switch",
                                                        "explanation": "Kernel saves PCB state cleanly.",
                                                        "citation": "Source 1",
                                                    }
                                                ],
                                                "math_formulas": [],
                                                "quiz_questions": [],
                                                "exam_questions": [],
                                            }
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
        )

    monkeypatch.setattr("backend.src.auth.verify_google_id_token", fake_worker_verify)
    monkeypatch.setattr("backend.src.gemini_client.http_request", fake_worker_http_request)

    try:
        # 1. Guest-lite is blocked from Worker AI relay
        guest = await create_guest_lite_session(worker_env)
        guest_relay = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/ai/relay",
                headers={"Authorization": f"Bearer {guest['token']}"},
                json_body={"prompt": "Hello", "endpoint": "local_synthesize"},
            ),
            worker_env,
        )
        assert guest_relay.status == 403

        # 2. Gmail sign-in on Worker yields cloud JWT for relay
        auth_res = await handle_request(
            MockWorkerRequest(
                "POST",
                "/api/auth/google",
                json_body={"id_token": "valid-desktop-gmail"},
            ),
            worker_env,
        )
        assert auth_res.status == 200
        cloud_token = auth_res.json()["token"]

        # 3. Local DB has ZERO local API keys, only OMNILEARN_CLOUD_URL + user's cloud_token
        monkeypatch.setenv("OMNILEARN_IGNORE_DOTENV", "1")
        monkeypatch.setenv("OMNILEARN_DISABLE_KEYRING", "1")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.setenv("OMNILEARN_CLOUD_URL", "https://omnilearn-test.workers.dev")

        db = LocalDatabase(
            db_path=tmp_path / "relay_local.db",
            assets_dir=tmp_path / "relay_assets",
            seed_sample_data=True,
        )
        db.set_cloud_token("desktop.user@gmail.com", cloud_token)

        pool_relay = db.get_pool_summary(owner_sub="desktop.user@gmail.com")
        assert pool_relay["using_source"] == "cloud_relay"
        assert pool_relay["relay_configured"] is True
        assert pool_relay["total_active_keys"] == 1
        _assert_no_secrets_leaked(pool_relay, ENV_DEFAULT_GEMINI)

        # Bridge httpx requests directly into Worker handle_request
        async def relay_transport_handler(request: httpx.Request) -> httpx.Response:
            req_headers = dict(request.headers.items())
            body_text = request.content.decode("utf-8") if request.content else "{}"
            w_res = await handle_request(
                MockWorkerRequest(
                    request.method,
                    request.url.path,
                    headers=req_headers,
                    json_body=json.loads(body_text),
                ),
                worker_env,
            )
            return httpx.Response(
                status_code=w_res.status,
                headers={"Content-Type": "application/json"},
                content=w_res.text().encode("utf-8"),
            )

        course = db.create_course("OS Relay Test", layout_type="mixed")
        async with httpx.AsyncClient(transport=httpx.MockTransport(relay_transport_handler)) as relay_client:
            synth = await synthesize_course_sources(
                db,
                course["id"],
                target_subject_name="Operating Systems",
                owner_sub="desktop.user@gmail.com",
                client=relay_client,
            )

        assert synth["ok"] is True
        assert synth["subjects"][0]["title"] == "Operating Systems"
        _assert_no_secrets_leaked(synth, ENV_DEFAULT_GEMINI)

        # 4. Adding a personal API key overrides cloud relay automatically
        db.add_api_key(PERSONAL_USER_GROQ, provider="groq", owner_sub="desktop.user@gmail.com")
        pool_override = db.get_pool_summary(owner_sub="desktop.user@gmail.com")
        assert pool_override["using_source"] == "personal"
    finally:
        worker_env.DB.close()


def test_sync_admin_from_env_updates_existing_admin(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNILEARN_IGNORE_DOTENV", "1")
    monkeypatch.setenv("OMNILEARN_DISABLE_KEYRING", "1")
    db = LocalDatabase(
        db_path=tmp_path / "sync_admin.db",
        assets_dir=tmp_path / "assets",
        seed_sample_data=False,
    )
    db.init_admin("initial-pass-123", username="admin")
    assert db.verify_admin("initial-pass-123", username="admin") is True

    monkeypatch.setenv("OMNILEARN_ADMIN_USERNAME", "superadmin")
    monkeypatch.setenv("OMNILEARN_ADMIN_PASSWORD", "updated-pass-456")
    assert db.sync_admin_from_env() is True
    assert db.sync_admin_from_env() is False
    assert db.verify_admin("updated-pass-456", username="superadmin") is True
    assert db.verify_admin("initial-pass-123", username="admin") is False
