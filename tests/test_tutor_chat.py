import pytest
from unittest.mock import AsyncMock, patch
from backend.src.gemini_client import build_tutor_chat_prompt


def test_build_tutor_chat_prompt():
    prompt = build_tutor_chat_prompt(
        user_msg="How do I upload a lecture video?",
        course_context="Course Title: Operating Systems\n",
        history=[
            {"role": "user", "content": "Hello tutor!"},
            {"role": "assistant", "content": "Hello! How can I help you today?"},
        ],
    )
    assert "OmniLearn AI Study Tutor & Application Assistant" in prompt
    assert "Operating Systems" in prompt
    assert "Student: Hello tutor!" in prompt
    assert "How do I upload a lecture video?" in prompt


@pytest.mark.asyncio
async def test_tutor_chat_endpoint_mock_worker():
    from backend.src.main import handle_request
    from tests.conftest import MockWorkerRequest, build_worker_env

    env = build_worker_env()
    
    # 1. Unauthenticated request -> 401
    req_unauth = MockWorkerRequest(
        "POST",
        "/api/tutor/chat",
        headers={"Content-Type": "application/json"},
        json_body={"message": "Can you explain paging vs segmentation?"},
    )
    res_unauth = await handle_request(req_unauth, env)
    assert res_unauth.status == 401

    # 2. Authenticated request with empty message -> 400
    admin_hdrs = {"Content-Type": "application/json", "Authorization": "Bearer admin-token"}
    # Mock get_session for admin
    with patch("backend.src.main.get_session", new_callable=AsyncMock) as mock_sess:
        mock_sess.return_value = {"role": "admin", "sub": "admin"}
        
        req_empty = MockWorkerRequest(
            "POST",
            "/api/tutor/chat",
            headers=admin_hdrs,
            json_body={"message": "   "},
        )
        res_empty = await handle_request(req_empty, env)
        assert res_empty.status == 400

        # 3. Authenticated valid request -> 200 with AI reply
        with patch("backend.src.main.call_ai_with_pool", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = {
                "ok": True,
                "cached": False,
                "provider": "gemini",
                "data": {"text": "Paging divides memory into fixed-size frames, while segmentation divides it into logical variable-size segments."},
            }
            req_valid = MockWorkerRequest(
                "POST",
                "/api/tutor/chat",
                headers=admin_hdrs,
                json_body={"message": "Explain paging vs segmentation."},
            )
            res_valid = await handle_request(req_valid, env)
            assert res_valid.status == 200
            import json
            body = json.loads(res_valid.body)
            assert body["ok"] is True
            assert "Paging divides memory" in body["reply"]
            assert body["provider"] == "gemini"


def test_tutor_chat_endpoint_local_api(local_api_harness):
    client = local_api_harness["client"]
    local_hdrs = local_api_harness["headers"]

    # Initialize admin and get auth token
    resp_init = client.post(
        "/api/auth/init",
        headers=local_hdrs,
        json={"username": "admin", "password": "IntegrationPassword!1"},
    )
    assert resp_init.status_code == 201
    token = resp_init.json()["token"]
    auth_hdrs = {**local_hdrs, "Authorization": f"Bearer {token}"}

    # 1. Empty message -> 400
    res_empty = client.post(
        "/api/tutor/chat",
        headers=auth_hdrs,
        json={"message": "   "},
    )
    assert res_empty.status_code == 400

    # 2. Valid request -> 200
    with patch("backend.local_app.local_api.call_ai_pool") as mock_ai:
        mock_ai.return_value = {
            "ok": True,
            "cached": False,
            "provider": "gemini",
            "data": {"text": "Virtual memory provides isolation and abstraction."},
        }
        res_valid = client.post(
            "/api/tutor/chat",
            headers=auth_hdrs,
            json={"message": "What is virtual memory?"},
        )
        assert res_valid.status_code == 200
        body = res_valid.json()
        assert body["ok"] is True
        assert "Virtual memory" in body["reply"]


