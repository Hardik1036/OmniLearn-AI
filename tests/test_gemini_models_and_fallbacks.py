import json
from datetime import datetime, timezone
import pytest
from unittest.mock import patch

from backend.src.gemini_client import (
    resolve_gemini_model,
    get_gemini_models_for_task,
    _invoke_provider as worker_invoke_provider,
    HEAVY_AI_TASKS,
)
from backend.local_app.gemini_local import (
    resolve_gemini_model as local_resolve_gemini_model,
    get_gemini_models_for_task as local_get_gemini_models_for_task,
    _invoke_provider as local_invoke_provider,
)


def test_gemini_model_task_hierarchy():
    # Heavy tasks (synthesis & grading) must prioritize gemini-4-hydrogen first
    # with automatic fallbacks gemini-3.8-flash and gemini-3.5-flash-lite
    for task in ["courses_synthesize", "exam_grade", "heavy_synthesis"]:
        models = get_gemini_models_for_task(task)
        assert models[0] == "gemini-4-hydrogen"
        assert "gemini-3.8-flash" in models
        assert "gemini-3.5-flash-lite" in models
        # Also verify local desktop app matches
        local_models = local_get_gemini_models_for_task(task)
        assert local_models[0] == "gemini-4-hydrogen"
        assert "gemini-3.8-flash" in local_models
        assert "gemini-3.5-flash-lite" in local_models

    # Standard lightweight tasks (video transcription, search summary) must avoid gemini-4-hydrogen
    # to preserve the 20 requests/day cap
    for task in ["transcribe_video", "search_preview", "general"]:
        models = get_gemini_models_for_task(task)
        assert "gemini-4-hydrogen" not in models
        assert models[0] == "gemini-3.8-flash"
        assert models[1] == "gemini-3.5-flash-lite"


def test_gemini_2_5_flash_deprecation_routing():
    # Before Dec 1, 2026: routes to gemini-2.5-flash
    before_date = datetime(2026, 11, 15, tzinfo=timezone.utc)
    with patch("backend.src.gemini_client.datetime") as mock_dt:
        mock_dt.now.return_value = before_date
        assert resolve_gemini_model("gemini-2.5-flash") == "gemini-2.5-flash"

    # On / After Dec 1, 2026: automatically routes to gemini-3.5-flash-lite
    after_date = datetime(2026, 12, 1, 0, 0, 1, tzinfo=timezone.utc)
    with patch("backend.src.gemini_client.datetime") as mock_dt:
        mock_dt.now.return_value = after_date
        assert resolve_gemini_model("gemini-2.5-flash") == "gemini-3.5-flash-lite"

    # Also verify local app
    with patch("backend.local_app.gemini_local.datetime") as mock_dt:
        mock_dt.now.return_value = after_date
        assert local_resolve_gemini_model("gemini-2.5-flash") == "gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_worker_gemini_hydrogen_429_automatic_fallback(monkeypatch):
    """When gemini-4-hydrogen hits 429 RESOURCE_EXHAUSTED, it must fall back to gemini-3.8-flash."""
    call_log = []

    async def fake_http_request(url, method="POST", headers=None, json_body=None, timeout=30.0):
        call_log.append(url)
        if "gemini-4-hydrogen" in url:
            # Hydrogen returned 429 RESOURCE_EXHAUSTED (exceeded 20 req/day cap)
            return (
                429,
                json.dumps({
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "Quota exceeded for quota metric 'Generate Content API requests' and limit 'Generate Content API requests per day' of service 'generativelanguage.googleapis.com' for consumer 'project_number:12345'."
                    }
                })
            )
        if "gemini-3.8-flash" in url:
            # Fallback model succeeds
            return (
                200,
                json.dumps({
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": '{"title": "Operating Systems", "summary": "Success from fallback"}'}]
                            }
                        }
                    ]
                })
            )
        return 500, "Unexpected model called"

    monkeypatch.setattr("backend.src.gemini_client.http_request", fake_http_request)

    status, text = await worker_invoke_provider(
        provider="gemini",
        api_key="AIzaSyTest_Key_12345",
        prompt="Synthesize course",
        expect_json=True,
        task="courses_synthesize",
    )

    assert status == 200
    assert "Success from fallback" in text
    # Verify gemini-4-hydrogen was tried first, followed by gemini-3.8-flash
    assert any("gemini-4-hydrogen" in u for u in call_log)
    assert any("gemini-3.8-flash" in u for u in call_log)
