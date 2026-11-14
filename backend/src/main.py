"""Cloudflare Free-Plan Python Worker Entrypoint & Pure Async Router (`src/main.py`)."""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

try:
    from .api_pool import EditionLimitError, add_personal_api_key, get_candidate_keys, get_pool_summary, remove_api_key
    from .auth import (
        admin_exists,
        build_google_oauth_start_url,
        clear_session_cookie,
        complete_google_oauth_callback,
        create_guest_lite_session,
        get_client_ip,
        get_google_oauth_config,
        get_header,
        get_session,
        login_admin,
        login_with_google,
        register_admin,
    )
    from .crypto_utils import sha256_hex_sync
    from .db import d1_all, d1_first, d1_run, env_get, maybe_await, to_py_value
    from .edition_guard import (
        MAX_WEB_LITE_FILE_BYTES,
        MAX_WEB_LITE_SLIDES,
        detect_client_platform,
        edition_limit_payload,
        enforce_web_lite_limits,
        get_edition_capabilities,
        get_latest_releases,
        get_runtime_mode,
        infer_source_type,
        is_web_lite,
        make_json_response,
        make_response,
    )
    from .gemini_client import (
        AIServiceRequiredError,
        call_ai_with_pool,
        fetch_public_webpage_preview,
        http_request,
        parse_safe_text_or_csv,
        search_openalex_lite,
        strip_unsafe_html,
        synthesize_course,
        validate_public_http_url,
    )
    from .storage import build_slide_key, count_course_slides, fetch_slide, sanitize_r2_key, upload_slide
except ImportError:
    from api_pool import EditionLimitError, add_personal_api_key, get_candidate_keys, get_pool_summary, remove_api_key
    from auth import (
        admin_exists,
        build_google_oauth_start_url,
        clear_session_cookie,
        complete_google_oauth_callback,
        create_guest_lite_session,
        get_client_ip,
        get_google_oauth_config,
        get_header,
        get_session,
        login_admin,
        login_with_google,
        register_admin,
    )
    from crypto_utils import sha256_hex_sync
    from db import d1_all, d1_first, d1_run, env_get, maybe_await, to_py_value
    from edition_guard import (
        MAX_WEB_LITE_FILE_BYTES,
        MAX_WEB_LITE_SLIDES,
        detect_client_platform,
        edition_limit_payload,
        enforce_web_lite_limits,
        get_edition_capabilities,
        get_latest_releases,
        get_runtime_mode,
        infer_source_type,
        is_web_lite,
        make_json_response,
        make_response,
    )
    from gemini_client import (
        AIServiceRequiredError,
        call_ai_with_pool,
        fetch_public_webpage_preview,
        http_request,
        parse_safe_text_or_csv,
        search_openalex_lite,
        strip_unsafe_html,
        synthesize_course,
        validate_public_http_url,
    )
    from storage import build_slide_key, count_course_slides, fetch_slide, sanitize_r2_key, upload_slide

try:
    from workers import WorkerEntrypoint  # type: ignore
except ImportError:
    class WorkerEntrypoint:  # type: ignore
        """CPython fallback base class matching Cloudflare Workers `WorkerEntrypoint`."""

        def __init__(self, env: Any = None, ctx: Any = None) -> None:
            self.env = env
            self.ctx = ctx


def _safe_json_loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return default
    return default


async def _read_raw_body_bytes(request: Any) -> bytes:
    if isinstance(request, dict):
        body = request.get("body")
        if isinstance(body, bytes):
            return body
        if isinstance(body, str):
            return body.encode("utf-8")
        if isinstance(body, dict):
            return json.dumps(body).encode("utf-8")
        return b""

    if hasattr(request, "arrayBuffer"):
        try:
            buf = await maybe_await(request.arrayBuffer())
            if hasattr(buf, "to_py"):
                return bytes(buf.to_py())
            return bytes(buf)
        except Exception:
            pass

    if hasattr(request, "text"):
        try:
            txt = await maybe_await(request.text())
            return str(txt).encode("utf-8")
        except Exception:
            pass

    raw = getattr(request, "body", b"")
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8")
    if isinstance(raw, dict):
        return json.dumps(raw).encode("utf-8")
    return b""


async def _read_json_body(request: Any) -> dict[str, Any]:
    if isinstance(request, dict):
        if isinstance(request.get("json"), dict):
            return dict(request["json"])
        if isinstance(request.get("body"), dict):
            return dict(request["body"])

    if hasattr(request, "json") and callable(getattr(request, "json")):
        try:
            raw = await maybe_await(request.json())
            parsed = to_py_value(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    raw_bytes = await _read_raw_body_bytes(request)
    if not raw_bytes:
        return {}
    try:
        parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def _read_multipart_or_json(request: Any) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Parse multipart/form-data (Workers FormData or raw HTTP bytes) or JSON body."""
    content_type = get_header(request, "Content-Type")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}

    if "multipart/form-data" not in content_type.lower():
        # Also allow CPython test mocks that provide .form_data directly
        mock_form = getattr(request, "form_data", None)
        if isinstance(mock_form, dict):
            for k, v in mock_form.items():
                if isinstance(v, tuple) and len(v) == 2:
                    files[str(k)] = (str(v[0]), bytes(v[1]))
                else:
                    fields[str(k)] = str(v)
            return fields, files

        body = await _read_json_body(request)
        for k, v in body.items():
            if k in ("data_b64", "content_b64") and isinstance(v, str):
                try:
                    raw_bytes = base64.b64decode(v.encode("ascii"))
                    files["file"] = (str(body.get("filename") or "upload.bin"), raw_bytes)
                except Exception:
                    pass
            elif k == "content" and isinstance(v, str) and "file" not in files:
                files["file"] = (str(body.get("filename") or "notes.txt"), v.encode("utf-8"))
            elif v is not None:
                fields[str(k)] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        return fields, files

    # 1. Cloudflare Workers native request.formData()
    if hasattr(request, "formData") and callable(getattr(request, "formData")):
        try:
            form = await maybe_await(request.formData())
            for item in form.entries():
                pair = to_py_value(item)
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                key, val = str(pair[0]), pair[1]
                if hasattr(val, "arrayBuffer"):
                    fname = str(getattr(val, "name", "") or "upload.bin")
                    buf = await maybe_await(val.arrayBuffer())
                    try:
                        import js  # type: ignore

                        u8 = js.Uint8Array.new(buf)
                        raw_bytes = u8.to_bytes() if hasattr(u8, "to_bytes") else bytes(u8.to_py())
                    except ImportError:
                        raw_bytes = bytes(buf)
                    files[key] = (fname, raw_bytes)
                else:
                    fields[key] = str(val)
            return fields, files
        except Exception:
            pass

    # 2. Pure Python stdlib email parser fallback for CPython tests
    raw_bytes = await _read_raw_body_bytes(request)
    if raw_bytes:
        header_block = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
        msg = BytesParser(policy=email_policy).parsebytes(header_block + raw_bytes)
        if msg.is_multipart():
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                filename = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                if filename is not None:
                    files[str(name)] = (str(filename), payload)
                else:
                    fields[str(name)] = payload.decode("utf-8", errors="replace")
    return fields, files


def _unauthorized(msg: str = "Authentication required") -> Any:
    return make_json_response({"ok": False, "error": "UNAUTHORIZED", "message": msg}, status=401)


def _forbidden(msg: str = "Admin access required") -> Any:
    return make_json_response({"ok": False, "error": "FORBIDDEN", "message": msg}, status=403)


def _bad_request(msg: str, error: str = "BAD_REQUEST") -> Any:
    return make_json_response({"ok": False, "error": error, "message": msg}, status=400)


def _not_found(msg: str = "Resource not found") -> Any:
    return make_json_response({"ok": False, "error": "NOT_FOUND", "message": msg}, status=404)


def _format_source_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "course_id": str(row.get("course_id") or ""),
        "source_type": str(row.get("source_type") or "txt"),
        "title": str(row.get("title") or ""),
        "origin_url": str(row.get("origin_url") or ""),
        "search_query": str(row.get("search_query") or ""),
        "file_hash": str(row.get("file_hash") or ""),
        "storage_key": str(row.get("storage_key") or ""),
        "raw_text_excerpt": str(row.get("raw_text_excerpt") or ""),
        "structured_chunks": _safe_json_loads(row.get("structured_chunks_json"), []),
        "visual_assets": _safe_json_loads(row.get("visual_assets_json"), []),
        "discovered_links": _safe_json_loads(row.get("discovered_links_json"), []),
        "metadata": _safe_json_loads(row.get("metadata_json"), {}),
        "status": str(row.get("status") or "PARSED"),
        "created_at": str(row.get("created_at") or ""),
    }


async def _build_course_bundle(env: Any, course_id: str) -> dict[str, Any] | None:
    course = await d1_first(env, "SELECT * FROM courses WHERE id = ?", (course_id,))
    if not course:
        return None

    rois = await d1_all(env, "SELECT * FROM window_rois WHERE course_id = ?", (course_id,))
    src_rows = await d1_all(
        env,
        "SELECT * FROM course_sources WHERE course_id = ? ORDER BY created_at ASC, id ASC",
        (course_id,),
    )
    sources = [_format_source_row(r) for r in src_rows]

    subj_rows = await d1_all(
        env,
        "SELECT * FROM subjects WHERE course_id = ? ORDER BY quadrant_key ASC, id ASC",
        (course_id,),
    )
    subjects: list[dict[str, Any]] = []
    for s in subj_rows:
        sid = str(s["id"])
        details = await d1_first(env, "SELECT * FROM subject_details WHERE subject_id = ?", (sid,)) or {}
        plan = await d1_first(env, "SELECT * FROM study_plans WHERE subject_id = ?", (sid,)) or {}
        quiz = await d1_first(env, "SELECT * FROM quizzes WHERE subject_id = ?", (sid,)) or {}
        paper = await d1_first(env, "SELECT * FROM model_papers WHERE subject_id = ?", (sid,)) or {}
        prog = await d1_first(env, "SELECT * FROM user_progress WHERE subject_id = ?", (sid,)) or {}
        schedule = _safe_json_loads(plan.get("schedule_json"), [])
        target_days = int(plan.get("target_days") or 14)
        daily_hours = float(plan.get("daily_hours") or 3.0)

        quiz_questions = _safe_json_loads(quiz.get("questions_json"), [])
        quiz_attempts = int(prog.get("quiz_attempts") or 0)
        last_quiz_score = float(prog.get("last_quiz_score") or 0.0)
        mastery_score = float(prog.get("mastery_score") or 0.0)
        completed_tasks = _safe_json_loads(prog.get("completed_tasks"), [])
        weak_spots = _safe_json_loads(prog.get("weak_spots"), [])

        subjects.append(
            {
                "id": sid,
                "course_id": str(s.get("course_id") or course_id),
                "quadrant_key": str(s.get("quadrant_key") or "q1"),
                "title": str(s.get("title") or ""),
                "short_title": str(s.get("short_title") or s.get("title") or ""),
                "topic": str(s.get("topic") or ""),
                "unit": str(s.get("unit") or ""),
                "instructor": str(s.get("instructor") or ""),
                "badge_color": str(s.get("badge_color") or "#3B82F6"),
                "summary": str(s.get("summary") or ""),
                "subtitles_transcript": _safe_json_loads(s.get("subtitles_transcript"), []),
                "slide_crops": _safe_json_loads(s.get("slide_crops"), []),
                "source_citations": _safe_json_loads(s.get("source_citations"), []),
                "mermaid_diagram": str(s.get("mermaid_diagram") or ""),
                "key_concepts": _safe_json_loads(details.get("key_concepts"), []),
                "math_formulas": _safe_json_loads(details.get("math_formulas"), []),
                "target_days": target_days,
                "daily_hours": daily_hours,
                "schedule": schedule,
                "study_plan": {
                    "target_days": target_days,
                    "daily_hours": daily_hours,
                    "schedule": schedule,
                },
                "quizzes": quiz_questions,
                "quiz": quiz_questions,
                "model_paper": {
                    "title": str(paper.get("title") or f"{s.get('title', 'Subject')} Model Exam Paper"),
                    "time_allowed": str(paper.get("time_allowed") or "60 Minutes"),
                    "max_marks": int(paper.get("max_marks") or 25),
                    "questions": _safe_json_loads(paper.get("questions_json"), []),
                },
                "quiz_attempts": quiz_attempts,
                "last_quiz_score": last_quiz_score,
                "mastery_score": mastery_score,
                "completed_tasks": completed_tasks,
                "weak_spots": weak_spots,
                "progress": {
                    "quiz_attempts": quiz_attempts,
                    "last_quiz_score": last_quiz_score,
                    "mastery_score": mastery_score,
                    "completed_tasks": completed_tasks,
                    "weak_spots": weak_spots,
                },
            }
        )

    return {
        "ok": True,
        "course": dict(course),
        "rois": [dict(r) for r in rois],
        "window_rois": [dict(r) for r in rois],
        "sources": sources,
        "subjects": subjects,
    }


async def _handle_get_course_detail(env: Any, course_id: str) -> Any:
    bundle = await _build_course_bundle(env, course_id)
    if bundle is None:
        return _not_found("Course not found")
    return make_json_response(bundle)


async def _persist_synthesized_subject(
    env: Any,
    course_id: str,
    subject: dict[str, Any],
    transcripts: list[Any],
    target_days: int,
    daily_hours: float,
) -> dict[str, Any]:
    sid = f"subj-{course_id[:8]}-{sha256_hex_sync(subject.get('title', 'subj'))[:8]}"
    await d1_run(
        env,
        """
        INSERT OR REPLACE INTO subjects
            (id, course_id, quadrant_key, title, short_title, topic, unit, instructor,
             badge_color, summary, subtitles_transcript, slide_crops, source_citations, mermaid_diagram)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            course_id,
            str(subject.get("quadrant_key") or "q1"),
            str(subject.get("title") or "Unified Subject"),
            str(subject.get("short_title") or "Subject"),
            str(subject.get("topic") or ""),
            str(subject.get("unit") or "Module 1"),
            str(subject.get("instructor") or "OmniLearn AI"),
            str(subject.get("badge_color") or "#3B82F6"),
            str(subject.get("summary") or ""),
            json.dumps(transcripts or [], ensure_ascii=False),
            json.dumps(subject.get("slide_crops") or [], ensure_ascii=False),
            json.dumps(subject.get("source_citations") or [], ensure_ascii=False),
            str(subject.get("mermaid_diagram") or ""),
        ),
    )
    await d1_run(
        env,
        "INSERT OR REPLACE INTO subject_details (subject_id, key_concepts, math_formulas) VALUES (?, ?, ?)",
        (
            sid,
            json.dumps(subject.get("key_concepts") or [], ensure_ascii=False),
            json.dumps(subject.get("math_formulas") or [], ensure_ascii=False),
        ),
    )
    await d1_run(
        env,
        "INSERT OR REPLACE INTO study_plans (subject_id, target_days, daily_hours, schedule_json) VALUES (?, ?, ?, ?)",
        (
            sid,
            int(target_days or 14),
            float(daily_hours or 3.0),
            json.dumps(subject.get("study_plan") or [], ensure_ascii=False),
        ),
    )
    await d1_run(
        env,
        "INSERT OR REPLACE INTO quizzes (subject_id, questions_json) VALUES (?, ?)",
        (sid, json.dumps(subject.get("quiz_questions") or [], ensure_ascii=False)),
    )
    mp = subject.get("model_paper") or {}
    await d1_run(
        env,
        "INSERT OR REPLACE INTO model_papers (subject_id, title, time_allowed, max_marks, questions_json) VALUES (?, ?, ?, ?, ?)",
        (
            sid,
            str(mp.get("title") or "Model Exam Paper"),
            str(mp.get("time_allowed") or "60 Minutes"),
            int(mp.get("max_marks") or 25),
            json.dumps(mp.get("questions") or [], ensure_ascii=False),
        ),
    )
    await d1_run(
        env,
        """
        INSERT OR IGNORE INTO user_progress
            (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots)
        VALUES (?, 0, 0.0, 0.0, '[]', '[]')
        """,
        (sid,),
    )
    await d1_run(
        env,
        "UPDATE courses SET status = 'READY', progress = 100, current_stage = 'Ready' WHERE id = ?",
        (course_id,),
    )

    formatted = dict(subject)
    formatted["id"] = sid
    formatted["course_id"] = course_id
    formatted["quizzes"] = subject.get("quiz_questions") or []
    formatted["study_plan"] = {
        "target_days": int(target_days or 14),
        "daily_hours": float(daily_hours or 3.0),
        "schedule": subject.get("study_plan") or [],
    }
    return formatted


async def _upsert_sync_course_row(env: Any, c: dict[str, Any]) -> bool:
    cid = str(c.get("id") or "").strip()
    if not cid:
        return False
    await d1_run(
        env,
        """
        INSERT OR REPLACE INTO courses
            (id, title, video_name, video_duration, resolution, layout_type, source_count,
             edition_origin, status, progress, current_stage)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid,
            str(c.get("title") or "Synced Course"),
            str(c.get("video_name") or ""),
            float(c.get("video_duration") or 0.0),
            str(c.get("resolution") or "MULTI_SOURCE"),
            str(c.get("layout_type") or "mixed"),
            int(c.get("source_count") or 0),
            str(c.get("edition_origin") or "system_app"),
            str(c.get("status") or "READY"),
            int(c.get("progress") or 100),
            str(c.get("current_stage") or "Ready"),
        ),
    )
    return True


async def _upsert_sync_subject_bundle(env: Any, course_id: str, s: dict[str, Any], counts: dict[str, int]) -> None:
    sid = str(s.get("id") or "").strip()
    cid = str(s.get("course_id") or course_id).strip()
    if not sid or not cid:
        return
    await d1_run(
        env,
        """
        INSERT OR REPLACE INTO subjects
            (id, course_id, quadrant_key, title, short_title, topic, unit, instructor,
             badge_color, summary, subtitles_transcript, slide_crops, source_citations, mermaid_diagram)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            cid,
            str(s.get("quadrant_key") or "q1"),
            str(s.get("title") or "Subject"),
            str(s.get("short_title") or ""),
            str(s.get("topic") or ""),
            str(s.get("unit") or ""),
            str(s.get("instructor") or ""),
            str(s.get("badge_color") or "#3B82F6"),
            str(s.get("summary") or ""),
            s.get("subtitles_transcript")
            if isinstance(s.get("subtitles_transcript"), str)
            else json.dumps(s.get("subtitles_transcript") or [], ensure_ascii=False),
            s.get("slide_crops")
            if isinstance(s.get("slide_crops"), str)
            else json.dumps(s.get("slide_crops") or [], ensure_ascii=False),
            s.get("source_citations")
            if isinstance(s.get("source_citations"), str)
            else json.dumps(s.get("source_citations") or [], ensure_ascii=False),
            str(s.get("mermaid_diagram") or ""),
        ),
    )
    counts["subjects"] = counts.get("subjects", 0) + 1

    if "key_concepts" in s or "math_formulas" in s:
        await d1_run(
            env,
            "INSERT OR REPLACE INTO subject_details (subject_id, key_concepts, math_formulas) VALUES (?, ?, ?)",
            (
                sid,
                json.dumps(s.get("key_concepts") or [], ensure_ascii=False),
                json.dumps(s.get("math_formulas") or [], ensure_ascii=False),
            ),
        )
        counts["subject_details"] = counts.get("subject_details", 0) + 1

    plan = s.get("study_plan") if isinstance(s.get("study_plan"), dict) else {}
    schedule = plan.get("schedule") if "schedule" in plan else s.get("schedule")
    if schedule is not None or plan:
        await d1_run(
            env,
            "INSERT OR REPLACE INTO study_plans (subject_id, target_days, daily_hours, schedule_json) VALUES (?, ?, ?, ?)",
            (
                sid,
                int(plan.get("target_days") or s.get("target_days") or 14),
                float(plan.get("daily_hours") or s.get("daily_hours") or 3.0),
                json.dumps(schedule or [], ensure_ascii=False),
            ),
        )
        counts["study_plans"] = counts.get("study_plans", 0) + 1

    if "quizzes" in s or "quiz" in s:
        q_payload = s.get("quizzes") if "quizzes" in s else s.get("quiz")
        await d1_run(
            env,
            "INSERT OR REPLACE INTO quizzes (subject_id, questions_json) VALUES (?, ?)",
            (sid, json.dumps(q_payload or [], ensure_ascii=False)),
        )
        counts["quizzes"] = counts.get("quizzes", 0) + 1

    if isinstance(s.get("model_paper"), dict):
        mp = s["model_paper"]
        await d1_run(
            env,
            "INSERT OR REPLACE INTO model_papers (subject_id, title, time_allowed, max_marks, questions_json) VALUES (?, ?, ?, ?, ?)",
            (
                sid,
                str(mp.get("title") or "Model Exam Paper"),
                str(mp.get("time_allowed") or "60 Minutes"),
                int(mp.get("max_marks") or 25),
                json.dumps(mp.get("questions") or [], ensure_ascii=False),
            ),
        )
        counts["model_papers"] = counts.get("model_papers", 0) + 1

    up = s.get("progress") if isinstance(s.get("progress"), dict) else None
    if up is None and any(
        k in s for k in ("quiz_attempts", "last_quiz_score", "mastery_score", "completed_tasks", "weak_spots")
    ):
        up = s
    if up is not None:
        completed = up.get("completed_tasks")
        weak = up.get("weak_spots")
        await d1_run(
            env,
            """
            INSERT OR REPLACE INTO user_progress
                (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                sid,
                int(up.get("quiz_attempts") or 0),
                float(up.get("last_quiz_score") or 0.0),
                float(up.get("mastery_score") or 0.0),
                completed if isinstance(completed, str) else json.dumps(completed or [], ensure_ascii=False),
                weak if isinstance(weak, str) else json.dumps(weak or [], ensure_ascii=False),
            ),
        )
        counts["user_progress"] = counts.get("user_progress", 0) + 1


async def _handle_sync_push(env: Any, body: dict[str, Any]) -> Any:
    """Upsert Admin workspace snapshot into D1 (accepts both LocalDB bundles & flat table arrays)."""
    counts: dict[str, int] = {}

    for item in body.get("courses") or []:
        if not isinstance(item, dict):
            continue
        # Support LocalDB export_workspace_snapshot bundle format: {"course": {...}, "rois": [...], "sources": [...], "subjects": [...]}
        if isinstance(item.get("course"), dict):
            c_obj = item["course"]
            cid = str(c_obj.get("id") or "").strip()
            if not cid:
                continue
            if await _upsert_sync_course_row(env, c_obj):
                counts["courses"] = counts.get("courses", 0) + 1
            for r in (item.get("rois") or item.get("window_rois") or []):
                if isinstance(r, dict) and r.get("id"):
                    await d1_run(
                        env,
                        """
                        INSERT OR REPLACE INTO window_rois
                            (id, course_id, roi_key, name, ymin, ymax, xmin, xmax, color)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(r["id"]),
                            cid,
                            str(r.get("roi_key") or "q1"),
                            str(r.get("name") or "Window"),
                            int(r.get("ymin") or 0),
                            int(r.get("ymax") or 1000),
                            int(r.get("xmin") or 0),
                            int(r.get("xmax") or 1000),
                            str(r.get("color") or "#3B82F6"),
                        ),
                    )
                    counts["window_rois"] = counts.get("window_rois", 0) + 1
            for src in (item.get("sources") or item.get("course_sources") or []):
                if isinstance(src, dict) and src.get("id"):
                    await d1_run(
                        env,
                        """
                        INSERT OR REPLACE INTO course_sources
                            (id, course_id, source_type, title, origin_url, search_query, file_hash, storage_key,
                             raw_text_excerpt, structured_chunks_json, visual_assets_json, discovered_links_json, metadata_json, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(src["id"]),
                            cid,
                            str(src.get("source_type") or "txt"),
                            str(src.get("title") or "Source"),
                            str(src.get("origin_url") or ""),
                            str(src.get("search_query") or ""),
                            str(src.get("file_hash") or sha256_hex_sync(str(src["id"]))),
                            str(src.get("storage_key") or ""),
                            str(src.get("raw_text_excerpt") or ""),
                            src.get("structured_chunks_json")
                            if isinstance(src.get("structured_chunks_json"), str)
                            else json.dumps(src.get("structured_chunks") or [], ensure_ascii=False),
                            src.get("visual_assets_json")
                            if isinstance(src.get("visual_assets_json"), str)
                            else json.dumps(src.get("visual_assets") or [], ensure_ascii=False),
                            src.get("discovered_links_json")
                            if isinstance(src.get("discovered_links_json"), str)
                            else json.dumps(src.get("discovered_links") or [], ensure_ascii=False),
                            src.get("metadata_json")
                            if isinstance(src.get("metadata_json"), str)
                            else json.dumps(src.get("metadata") or {}, ensure_ascii=False),
                            str(src.get("status") or "PARSED"),
                        ),
                    )
                    counts["course_sources"] = counts.get("course_sources", 0) + 1
            for s in item.get("subjects") or []:
                if isinstance(s, dict):
                    await _upsert_sync_subject_bundle(env, cid, s, counts)
            continue

        # Flat course row format
        if await _upsert_sync_course_row(env, item):
            counts["courses"] = counts.get("courses", 0) + 1

    for r in body.get("window_rois") or []:
        if not isinstance(r, dict) or not r.get("id") or not r.get("course_id"):
            continue
        await d1_run(
            env,
            """
            INSERT OR REPLACE INTO window_rois
                (id, course_id, roi_key, name, ymin, ymax, xmin, xmax, color)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(r["id"]),
                str(r["course_id"]),
                str(r.get("roi_key") or "q1"),
                str(r.get("name") or "Window"),
                int(r.get("ymin") or 0),
                int(r.get("ymax") or 1000),
                int(r.get("xmin") or 0),
                int(r.get("xmax") or 1000),
                str(r.get("color") or "#3B82F6"),
            ),
        )
        counts["window_rois"] = counts.get("window_rois", 0) + 1

    for src in body.get("course_sources") or []:
        if not isinstance(src, dict) or not src.get("id") or not src.get("course_id"):
            continue
        await d1_run(
            env,
            """
            INSERT OR REPLACE INTO course_sources
                (id, course_id, source_type, title, origin_url, search_query, file_hash, storage_key,
                 raw_text_excerpt, structured_chunks_json, visual_assets_json, discovered_links_json, metadata_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(src["id"]),
                str(src["course_id"]),
                str(src.get("source_type") or "txt"),
                str(src.get("title") or "Source"),
                str(src.get("origin_url") or ""),
                str(src.get("search_query") or ""),
                str(src.get("file_hash") or sha256_hex_sync(str(src["id"]))),
                str(src.get("storage_key") or ""),
                str(src.get("raw_text_excerpt") or ""),
                src.get("structured_chunks_json")
                if isinstance(src.get("structured_chunks_json"), str)
                else json.dumps(src.get("structured_chunks") or []),
                src.get("visual_assets_json")
                if isinstance(src.get("visual_assets_json"), str)
                else json.dumps(src.get("visual_assets") or []),
                src.get("discovered_links_json")
                if isinstance(src.get("discovered_links_json"), str)
                else json.dumps(src.get("discovered_links") or []),
                src.get("metadata_json")
                if isinstance(src.get("metadata_json"), str)
                else json.dumps(src.get("metadata") or {}),
                str(src.get("status") or "PARSED"),
            ),
        )
        counts["course_sources"] = counts.get("course_sources", 0) + 1

    for s in body.get("subjects") or []:
        if not isinstance(s, dict) or not s.get("id") or not s.get("course_id"):
            continue
        await d1_run(
            env,
            """
            INSERT OR REPLACE INTO subjects
                (id, course_id, quadrant_key, title, short_title, topic, unit, instructor,
                 badge_color, summary, subtitles_transcript, slide_crops, source_citations, mermaid_diagram)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(s["id"]),
                str(s["course_id"]),
                str(s.get("quadrant_key") or "q1"),
                str(s.get("title") or "Subject"),
                str(s.get("short_title") or ""),
                str(s.get("topic") or ""),
                str(s.get("unit") or ""),
                str(s.get("instructor") or ""),
                str(s.get("badge_color") or "#3B82F6"),
                str(s.get("summary") or ""),
                s.get("subtitles_transcript")
                if isinstance(s.get("subtitles_transcript"), str)
                else json.dumps(s.get("subtitles_transcript") or []),
                s.get("slide_crops")
                if isinstance(s.get("slide_crops"), str)
                else json.dumps(s.get("slide_crops") or []),
                s.get("source_citations")
                if isinstance(s.get("source_citations"), str)
                else json.dumps(s.get("source_citations") or []),
                str(s.get("mermaid_diagram") or ""),
            ),
        )
        counts["subjects"] = counts.get("subjects", 0) + 1

    for sd in body.get("subject_details") or []:
        if not isinstance(sd, dict) or not sd.get("subject_id"):
            continue
        await d1_run(
            env,
            "INSERT OR REPLACE INTO subject_details (subject_id, key_concepts, math_formulas) VALUES (?, ?, ?)",
            (
                str(sd["subject_id"]),
                sd.get("key_concepts") if isinstance(sd.get("key_concepts"), str) else json.dumps(sd.get("key_concepts") or []),
                sd.get("math_formulas") if isinstance(sd.get("math_formulas"), str) else json.dumps(sd.get("math_formulas") or []),
            ),
        )
        counts["subject_details"] = counts.get("subject_details", 0) + 1

    for sp in body.get("study_plans") or []:
        if not isinstance(sp, dict) or not sp.get("subject_id"):
            continue
        await d1_run(
            env,
            "INSERT OR REPLACE INTO study_plans (subject_id, target_days, daily_hours, schedule_json) VALUES (?, ?, ?, ?)",
            (
                str(sp["subject_id"]),
                int(sp.get("target_days") or 14),
                float(sp.get("daily_hours") or 3.0),
                sp.get("schedule_json") if isinstance(sp.get("schedule_json"), str) else json.dumps(sp.get("schedule") or []),
            ),
        )
        counts["study_plans"] = counts.get("study_plans", 0) + 1

    for q in body.get("quizzes") or []:
        if not isinstance(q, dict) or not q.get("subject_id"):
            continue
        await d1_run(
            env,
            "INSERT OR REPLACE INTO quizzes (subject_id, questions_json) VALUES (?, ?)",
            (
                str(q["subject_id"]),
                q.get("questions_json") if isinstance(q.get("questions_json"), str) else json.dumps(q.get("questions") or []),
            ),
        )
        counts["quizzes"] = counts.get("quizzes", 0) + 1

    for mp in body.get("model_papers") or []:
        if not isinstance(mp, dict) or not mp.get("subject_id"):
            continue
        await d1_run(
            env,
            "INSERT OR REPLACE INTO model_papers (subject_id, title, time_allowed, max_marks, questions_json) VALUES (?, ?, ?, ?, ?)",
            (
                str(mp["subject_id"]),
                str(mp.get("title") or "Model Exam Paper"),
                str(mp.get("time_allowed") or "60 Minutes"),
                int(mp.get("max_marks") or 25),
                mp.get("questions_json") if isinstance(mp.get("questions_json"), str) else json.dumps(mp.get("questions") or []),
            ),
        )
        counts["model_papers"] = counts.get("model_papers", 0) + 1

    for up in body.get("user_progress") or []:
        if not isinstance(up, dict) or not up.get("subject_id"):
            continue
        await d1_run(
            env,
            """
            INSERT OR REPLACE INTO user_progress
                (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                str(up["subject_id"]),
                int(up.get("quiz_attempts") or 0),
                float(up.get("last_quiz_score") or 0.0),
                float(up.get("mastery_score") or 0.0),
                up.get("completed_tasks") if isinstance(up.get("completed_tasks"), str) else json.dumps(up.get("completed_tasks") or []),
                up.get("weak_spots") if isinstance(up.get("weak_spots"), str) else json.dumps(up.get("weak_spots") or []),
            ),
        )
        counts["user_progress"] = counts.get("user_progress", 0) + 1

    return make_json_response({"ok": True, "synced": counts})


async def _handle_sync_pull(env: Any) -> Any:
    """Export workspace snapshot from D1 matching LocalDB bundle schema (excluding admin_users & api_keys)."""
    course_rows = await d1_all(env, "SELECT id FROM courses ORDER BY created_at DESC, id DESC")
    bundles: list[dict[str, Any]] = []
    for c_row in course_rows:
        cid = str(c_row.get("id") or "")
        if cid:
            bundle = await _build_course_bundle(env, cid)
            if bundle is not None:
                bundles.append(bundle)

    snapshot = {
        "ok": True,
        "version": "1.0",
        "exported_at": int(time.time()),
        "courses": bundles,
        "window_rois": await d1_all(env, "SELECT * FROM window_rois"),
        "course_sources": await d1_all(env, "SELECT * FROM course_sources ORDER BY created_at ASC"),
        "subjects": await d1_all(env, "SELECT * FROM subjects"),
        "subject_details": await d1_all(env, "SELECT * FROM subject_details"),
        "study_plans": await d1_all(env, "SELECT * FROM study_plans"),
        "quizzes": await d1_all(env, "SELECT * FROM quizzes"),
        "model_papers": await d1_all(env, "SELECT * FROM model_papers"),
        "user_progress": await d1_all(env, "SELECT * FROM user_progress"),
    }
    return make_json_response(snapshot)


async def handle_request(request: Any, env: Any) -> Any:
    """Pure async Worker router callable both from Cloudflare `Default.fetch` and pytest."""
    raw_url = getattr(request, "url", None) or (request.get("url") if isinstance(request, dict) else "/")
    parsed = urlparse(str(raw_url))
    path = parsed.path or "/"
    query = parse_qs(parsed.query or "")
    method = str(
        getattr(request, "method", None)
        or (request.get("method") if isinstance(request, dict) else "GET")
        or "GET"
    ).upper()

    # Non-API routes delegate straight to Cloudflare Workers Static Assets (`./public`)
    if not path.startswith("/api/") and path != "/api":
        assets = env_get(env, "ASSETS", None)
        if assets is not None and hasattr(assets, "fetch"):
            return await maybe_await(assets.fetch(request))
        return make_json_response({"ok": False, "error": "ASSETS_UNAVAILABLE"}, status=404)

    if method == "OPTIONS":
        return make_response(
            "",
            status=204,
            headers={"Allow": "GET, POST, DELETE, OPTIONS"},
        )

    edition = get_runtime_mode(env)

    try:
        # ------------------------------------------------------------------
        # Public / Release / Auth Routes
        # ------------------------------------------------------------------
        if method == "GET" and path == "/api/health":
            return make_json_response(
                {
                    "ok": True,
                    "service": "omnilearn-worker",
                    "edition": edition,
                    "runtime_mode": edition,
                    "capabilities": get_edition_capabilities(env),
                    "timestamp": int(time.time()),
                }
            )

        if method == "GET" and path == "/api/releases/latest":
            releases = await get_latest_releases(env)
            ua = get_header(request, "User-Agent")
            return make_json_response(
                {
                    "ok": True,
                    "edition": edition,
                    "recommended_platform": detect_client_platform(ua),
                    "releases": releases,
                }
            )

        if method == "GET" and path == "/api/auth/status":
            initialized = await admin_exists(env)
            session = await get_session(request, env)
            oauth_cfg = get_google_oauth_config(env)
            if session:
                pool_summary = await get_pool_summary(env, owner_sub=str(session.get("sub") or ""))
            else:
                pool_summary = await get_pool_summary(env, owner_sub=None)
            return make_json_response(
                {
                    "ok": True,
                    "initialized": initialized,
                    "authenticated": session is not None,
                    "role": session.get("role") if session else None,
                    "username": session.get("sub") if session else None,
                    "email": session.get("email") if session else None,
                    "name": session.get("name") if session else None,
                    "token": session.get("token") if session else None,
                    "google_oauth_enabled": bool(oauth_cfg["enabled"]),
                    "google_client_id": str(oauth_cfg["client_id"] or ""),
                    "google_code_flow_enabled": bool(oauth_cfg["code_flow_enabled"]),
                    "edition": edition,
                    "capabilities": get_edition_capabilities(env),
                    "pool": pool_summary,
                    "pool_summary": pool_summary,
                }
            )

        if method == "GET" and path == "/api/auth/google/config":
            oauth_cfg = get_google_oauth_config(env)
            return make_json_response(
                {
                    "ok": True,
                    "enabled": bool(oauth_cfg["enabled"]),
                    "client_id": str(oauth_cfg["client_id"] or ""),
                    "code_flow_enabled": bool(oauth_cfg["code_flow_enabled"]),
                }
            )

        if method == "POST" and path == "/api/auth/init":
            body = await _read_json_body(request)
            password = str(body.get("password") or "")
            confirm = body.get("confirm_password")
            if confirm is not None and str(confirm) != password:
                return _bad_request("Passwords do not match.", error="PASSWORD_MISMATCH")

            res = await register_admin(
                env,
                password=password,
                username=str(body.get("username") or "admin"),
                api_key=body.get("api_key"),
                provider=str(body.get("provider") or "gemini"),
            )
            status = int(res.pop("status", 200))
            cookie = res.pop("cookie", None)
            headers = {"Set-Cookie": cookie} if cookie else None
            return make_json_response(res, status=status, headers=headers)

        if method == "POST" and path == "/api/auth/login":
            body = await _read_json_body(request)
            res = await login_admin(
                env,
                password=str(body.get("password") or ""),
                client_ip=get_client_ip(request),
                api_key=body.get("api_key"),
                provider=str(body.get("provider") or "gemini"),
                username=str(body.get("username") or "admin"),
            )
            status = int(res.pop("status", 200))
            cookie = res.pop("cookie", None)
            headers = {"Set-Cookie": cookie} if cookie else None
            return make_json_response(res, status=status, headers=headers)

        if method == "POST" and path == "/api/auth/logout":
            return make_json_response(
                {"ok": True, "authenticated": False},
                headers={"Set-Cookie": clear_session_cookie()},
            )

        if method == "POST" and path == "/api/auth/guest-lite":
            res = await create_guest_lite_session(env)
            status = int(res.pop("status", 200))
            cookie = res.pop("cookie", None)
            headers = {"Set-Cookie": cookie} if cookie else None
            return make_json_response(res, status=status, headers=headers)

        if method == "POST" and path == "/api/auth/google":
            body = await _read_json_body(request)
            res = await login_with_google(
                env,
                id_token=str(body.get("id_token") or body.get("credential") or ""),
                api_key=body.get("api_key"),
                provider=str(body.get("provider") or "gemini"),
            )
            status = int(res.pop("status", 200))
            cookie = res.pop("cookie", None)
            headers = {"Set-Cookie": cookie} if cookie else None
            return make_json_response(res, status=status, headers=headers)

        if method == "GET" and path == "/api/auth/google/start":
            origin = (
                get_header(request, "Origin").strip()
                or (
                    f"{get_header(request, 'X-Forwarded-Proto', 'https')}://{get_header(request, 'Host')}".strip()
                    if get_header(request, "Host")
                    else ""
                )
            )
            if not origin:
                parsed_req = urlparse(str(getattr(request, "url", "") or "/"))
                origin = f"{parsed_req.scheme}://{parsed_req.netloc}" if parsed_req.scheme and parsed_req.netloc else ""
            fallback_cb = f"{origin.rstrip('/')}/api/auth/google/callback" if origin else "/api/auth/google/callback"
            ok, payload_val, err_code = await build_google_oauth_start_url(env, fallback_cb)
            if not ok:
                return make_json_response({"ok": False, "error": err_code, "message": payload_val}, status=503)
            parsed_req = urlparse(str(getattr(request, "url", "") or "/"))
            q = parse_qs(parsed_req.query or "")
            if q.get("format", [""])[0] == "json":
                return make_json_response({"ok": True, "auth_url": payload_val})
            return make_response("", status=302, headers={"Location": payload_val})

        if method == "GET" and path == "/api/auth/google/callback":
            parsed_req = urlparse(str(getattr(request, "url", "") or "/"))
            q = parse_qs(parsed_req.query or "")
            if q.get("error"):
                return make_response("", status=302, headers={"Location": "/?auth_error=google_cancelled"})
            origin = (
                get_header(request, "Origin").strip()
                or (
                    f"{get_header(request, 'X-Forwarded-Proto', 'https')}://{get_header(request, 'Host')}".strip()
                    if get_header(request, "Host")
                    else ""
                )
            )
            if not origin:
                origin = f"{parsed_req.scheme}://{parsed_req.netloc}" if parsed_req.scheme and parsed_req.netloc else ""
            fallback_cb = f"{origin.rstrip('/')}/api/auth/google/callback" if origin else "/api/auth/google/callback"
            res = await complete_google_oauth_callback(
                env,
                code=q.get("code", [""])[0],
                state=q.get("state", [""])[0],
                fallback_callback_url=fallback_cb,
            )
            if not res.get("ok"):
                status = int(res.pop("status", 400))
                accept_hdr = get_header(request, "Accept")
                if "application/json" in accept_hdr and "text/html" not in accept_hdr:
                    return make_json_response(res, status=status)
                err_msg = str(res.get("message") or "Google Sign-In failed.")
                return make_response(
                    "",
                    status=302,
                    headers={"Location": f"/?auth_error=google_failed&auth_error_msg={quote_plus(err_msg)}"},
                )
            cookie = res.pop("cookie", None)
            token = str(res.get("token") or "")
            location = f"/?auth_token={token}" if token else "/"
            headers = {"Location": location}
            if cookie:
                headers["Set-Cookie"] = cookie
            return make_response("", status=302, headers=headers)

        # ------------------------------------------------------------------
        # Slide R2 GET: enforce path traversal check before returning asset
        # ------------------------------------------------------------------
        if method == "GET" and path.startswith("/api/slides/"):
            raw_key = unquote(path[len("/api/slides/") :])
            try:
                clean_key = sanitize_r2_key(raw_key)
            except ValueError as exc:
                return _bad_request(str(exc), error="INVALID_SLIDE_KEY")

            session = await get_session(request, env)
            if not session:
                return _unauthorized()

            fetched = await fetch_slide(env, clean_key)
            if fetched is None:
                return _not_found("Slide asset not found")
            slide_bytes, content_type = fetched
            return make_response(
                slide_bytes,
                status=200,
                headers={"Content-Type": content_type},
            )

        # ------------------------------------------------------------------
        # Authenticated Routes (`admin` or `guest_lite`)
        # ------------------------------------------------------------------
        session = await get_session(request, env)
        if not session:
            return _unauthorized()
        is_admin = session.get("role") == "admin"
        can_edit = session.get("role") in ("admin", "user")
        owner_sub = str(session.get("sub") or "admin")

        # Cloud Sync (Admin ONLY; allowed even in web_lite)
        if method == "POST" and path == "/api/sync/push":
            blocked = await enforce_web_lite_limits(request, "/api/sync/push", None, env, session=session)
            if blocked is not None:
                return blocked
            body = await _read_json_body(request)
            return await _handle_sync_push(env, body)

        if method in ("GET", "POST") and path == "/api/sync/pull":
            blocked = await enforce_web_lite_limits(request, "/api/sync/pull", None, env, session=session)
            if blocked is not None:
                return blocked
            return await _handle_sync_pull(env)

        # Multi-API Key Pool
        if method == "GET" and path == "/api/pool":
            return make_json_response(await get_pool_summary(env, owner_sub=owner_sub))

        if method == "POST" and path == "/api/pool/add":
            if not can_edit:
                return _forbidden("Sign in with Admin or Google to manage API pool keys.")
            body = await _read_json_body(request)
            blocked = await enforce_web_lite_limits(request, "/api/pool/add", body, env, session=session)
            if blocked is not None:
                return blocked
            try:
                added = await add_personal_api_key(
                    env,
                    api_key=str(body.get("api_key") or ""),
                    provider=str(body.get("provider") or "gemini"),
                    owner_sub=owner_sub,
                )
            except EditionLimitError as exc:
                return make_json_response(edition_limit_payload(exc.feature, exc.message), status=403)
            except ValueError as exc:
                return _bad_request(str(exc), error="INVALID_API_KEY")

            added["pool"] = await get_pool_summary(env, owner_sub=owner_sub)
            return make_json_response(added, status=201 if not added.get("deduplicated") else 200)

        if method == "DELETE" and path.startswith("/api/pool/"):
            if not can_edit:
                return _forbidden("Sign in with Admin or Google to remove API pool keys.")
            slot_id = unquote(path[len("/api/pool/") :]).strip()
            try:
                removed = await remove_api_key(env, slot_id, owner_sub=owner_sub)
            except PermissionError as exc:
                return _forbidden(str(exc))
            if not removed:
                return _not_found("API key slot not found")
            return make_json_response({"ok": True, "deleted": True, "pool": await get_pool_summary(env, owner_sub=owner_sub)})

        # Courses CRUD
        if method == "GET" and path == "/api/courses":
            rows = await d1_all(env, "SELECT * FROM courses ORDER BY created_at DESC, id DESC")
            return make_json_response({"ok": True, "courses": [dict(r) for r in rows]})

        if method == "POST" and path == "/api/courses":
            body = await _read_json_body(request)
            title = str(body.get("title") or "").strip()
            if not title:
                return _bad_request("Course title is required")
            course_id = str(body.get("id") or f"course-{uuid.uuid4().hex[:12]}")
            layout = str(body.get("layout_type") or "mixed").strip() or "mixed"
            await d1_run(
                env,
                """
                INSERT INTO courses
                    (id, title, video_name, video_duration, resolution, layout_type, source_count,
                     edition_origin, status, progress, current_stage)
                VALUES (?, ?, '', 0.0, 'MULTI_SOURCE', ?, 0, ?, 'DRAFT', 0, 'Sources')
                """,
                (course_id, title[:180], layout, edition),
            )
            created = await d1_first(env, "SELECT * FROM courses WHERE id = ?", (course_id,))
            return make_json_response({"ok": True, "course": dict(created or {"id": course_id, "title": title})}, status=201)

        if method == "GET" and path.startswith("/api/courses/"):
            course_id = unquote(path[len("/api/courses/") :]).strip()
            if not course_id or "/" in course_id:
                return _not_found("Course not found")
            return await _handle_get_course_detail(env, course_id)

        if method == "DELETE" and path.startswith("/api/courses/"):
            if not is_admin:
                return _forbidden("Only Admin sessions can delete courses.")
            course_id = unquote(path[len("/api/courses/") :]).strip()
            existing = await d1_first(env, "SELECT id FROM courses WHERE id = ?", (course_id,))
            if not existing:
                return _not_found("Course not found")
            await d1_run(env, "DELETE FROM courses WHERE id = ?", (course_id,))
            return make_json_response({"ok": True, "deleted": True, "id": course_id})

        if method == "POST" and path.startswith("/api/courses/") and path.endswith("/subjects"):
            if not can_edit:
                return _forbidden("Sign in with Admin or Google to add subjects.")
            course_id = unquote(path[len("/api/courses/") : -len("/subjects")]).strip()
            existing = await d1_first(env, "SELECT id FROM courses WHERE id = ?", (course_id,))
            if not existing:
                return _not_found("Course not found")
            body = await _read_json_body(request)
            title = strip_unsafe_html(str(body.get("title") or "")).strip()
            if not title:
                return _bad_request("Subject title is required.", error="INVALID_SUBJECT")
            cnt_row = await d1_first(env, "SELECT COUNT(*) AS cnt FROM subjects WHERE course_id = ?", (course_id,))
            idx = int((cnt_row or {}).get("cnt") or 0)
            palette = ("#3B82F6", "#10B981", "#8B5CF6", "#F59E0B", "#EC4899", "#06B6D4", "#F97316", "#6366F1")
            color = str(body.get("badge_color") or "").strip() or palette[idx % len(palette)]
            sid = f"subj-{course_id[:8]}-{sha256_hex_sync(f'{title}-{idx}')[:8]}"
            await d1_run(
                env,
                """
                INSERT INTO subjects (
                    id, course_id, quadrant_key, title, short_title, topic, unit, instructor,
                    badge_color, summary, subtitles_transcript, slide_crops, source_citations, mermaid_diagram
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '[]', '[]', '[]', '')
                """,
                (
                    sid,
                    course_id,
                    f"subj_{idx + 1}",
                    title,
                    title[:24],
                    strip_unsafe_html(str(body.get("topic") or "")),
                    strip_unsafe_html(str(body.get("unit") or f"Subject {idx + 1}")),
                    strip_unsafe_html(str(body.get("instructor") or "OmniLearn AI")),
                    color,
                ),
            )
            created_subj = await d1_first(env, "SELECT * FROM subjects WHERE id = ?", (sid,))
            return make_json_response({"ok": True, "subject": dict(created_subj or {"id": sid, "title": title})}, status=201)

        # Synthesize Course
        if method == "POST" and path == "/api/courses/synthesize":
            body = await _read_json_body(request)
            course_id = str(body.get("course_id") or "").strip()
            if not course_id:
                return _bad_request("course_id is required")
            course = await d1_first(env, "SELECT * FROM courses WHERE id = ?", (course_id,))
            if not course:
                return _not_found("Course not found")

            blocked = await enforce_web_lite_limits(request, "/api/courses/synthesize", body, env, session=session)
            if blocked is not None:
                return blocked

            sources = await d1_all(
                env,
                "SELECT * FROM course_sources WHERE course_id = ? ORDER BY created_at ASC",
                (course_id,),
            )
            if is_web_lite(env) and len(sources) > 2:
                return make_json_response(
                    edition_limit_payload(
                        "multi_source_limit",
                        "Web Lite caps courses at 2 sources. Download OmniLearn System App for unlimited sources.",
                    ),
                    status=403,
                )

            slide_urls = body.get("slides") if isinstance(body.get("slides"), list) else []
            if not slide_urls:
                for src in sources:
                    for asset in _safe_json_loads(src.get("visual_assets_json"), []):
                        slide_urls.append(asset)
                    excerpt = str(src.get("raw_text_excerpt") or "")
                    for found_slide in re.findall(r"(/api/slides/courses/[A-Za-z0-9_./-]+)", excerpt):
                        if found_slide not in slide_urls:
                            slide_urls.append(found_slide)
            if is_web_lite(env):
                slide_urls = slide_urls[:MAX_WEB_LITE_SLIDES]

            transcripts = body.get("transcripts") if isinstance(body.get("transcripts"), list) else []
            target_days = int(body.get("target_days") or 14)
            daily_hours = float(body.get("daily_hours") or 3.0)
            target_subject_name = str(body.get("target_subject_name") or body.get("subject_name") or "").strip()

            try:
                synth = await synthesize_course(
                    env,
                    course_title=str(course.get("title") or "Course"),
                    sources=sources,
                    slide_urls=slide_urls,
                    transcripts=transcripts,
                    target_days=target_days,
                    daily_hours=daily_hours,
                    target_subject_name=target_subject_name,
                    owner_sub=owner_sub,
                )
            except AIServiceRequiredError as exc:
                return make_json_response(
                    {"ok": False, "error": exc.error_code, "message": exc.message},
                    status=exc.status_code,
                )
            persisted = await _persist_synthesized_subject(
                env,
                course_id=course_id,
                subject=synth["subject"],
                transcripts=transcripts,
                target_days=target_days,
                daily_hours=daily_hours,
            )
            return make_json_response(
                {
                    "ok": True,
                    "course_id": course_id,
                    "subjects": [persisted],
                    "used_fallback": synth["used_fallback"],
                    "cached": synth["cached"],
                }
            )

        # In-App Source Search
        if method == "POST" and path == "/api/sources/search":
            body = await _read_json_body(request)
            blocked = await enforce_web_lite_limits(request, "/api/sources/search", body, env, session=session)
            if blocked is not None:
                return blocked
            q = str(body.get("query") or "").strip()
            limit = int(body.get("limit") or 3)
            return make_json_response(await search_openalex_lite(env, q, limit=limit))

        # Inspect URL (Locked in Web Lite)
        if method == "POST" and path == "/api/sources/inspect-url":
            body = await _read_json_body(request)
            blocked = await enforce_web_lite_limits(request, "/api/sources/inspect-url", body, env, session=session)
            if blocked is not None:
                return blocked
            url = str(body.get("url") or "").strip()
            try:
                safe_url = validate_public_http_url(url)
            except ValueError as exc:
                return _bad_request(str(exc), error="SSRF_BLOCKED")
            status, html_text = await http_request(safe_url, method="GET", timeout=10.0)
            if status >= 400:
                return _bad_request(f"Upstream returned HTTP {status}")
            hrefs = re.findall(r'(?i)(?:href|src)=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html_text)
            pdfs = list(dict.fromkeys(urljoin(safe_url, h) for h in hrefs))
            excerpt_text = strip_unsafe_html(html_text)[:4000]
            return make_json_response(
                {
                    "ok": True,
                    "url": safe_url,
                    "text_excerpt": excerpt_text,
                    "excerpt": excerpt_text,
                    "text": excerpt_text,
                    "pdf_links": pdfs,
                    "discovered_pdfs": pdfs,
                }
            )

        # Ingest File
        if method == "POST" and path == "/api/sources/ingest-file":
            fields, files = await _read_multipart_or_json(request)
            course_id = (fields.get("course_id") or "").strip()
            if not course_id:
                return _bad_request("course_id is required")
            course = await d1_first(env, "SELECT id FROM courses WHERE id = ?", (course_id,))
            if not course:
                return _not_found("Course not found")

            file_tuple = files.get("file")
            filename = (file_tuple[0] if file_tuple else fields.get("filename")) or "notes.txt"
            file_bytes = file_tuple[1] if file_tuple else (str(fields.get("raw_text") or "").encode("utf-8") if fields.get("raw_text") else b"")
            guard_payload = {
                "course_id": course_id,
                "filename": filename,
                "source_type": fields.get("source_type") or infer_source_type({"filename": filename}),
                "size_bytes": len(file_bytes) if file_bytes else int(fields.get("size_bytes") or 0),
            }
            blocked = await enforce_web_lite_limits(
                request,
                "/api/sources/ingest-file",
                guard_payload,
                env,
                session=session,
            )
            if blocked is not None:
                return blocked

            src_type_inferred = str(guard_payload["source_type"] or "txt").lower()
            if src_type_inferred in ("txt", "csv"):
                try:
                    max_allowed = MAX_WEB_LITE_FILE_BYTES if is_web_lite(env) else 25 * 1024 * 1024
                    parsed_src = parse_safe_text_or_csv(filename, file_bytes, max_bytes=max_allowed)
                except ValueError as exc:
                    return _bad_request(str(exc))
            else:
                # system_app mode non-text metadata record
                raw_bytes = bytes(file_bytes or b"")
                raw_excerpt = str(fields.get("raw_text") or f"[{src_type_inferred.upper()} source: {filename}]")[:8000]
                parsed_src = {
                    "source_type": src_type_inferred,
                    "title": str(fields.get("title") or filename or "document"),
                    "file_hash": sha256_hex_sync(raw_bytes or filename.encode("utf-8")),
                    "raw_text_excerpt": raw_excerpt,
                    "structured_chunks": [{"index": 1, "label": filename, "text": f"{src_type_inferred.upper()} uploaded ({len(raw_bytes)} bytes)"}],
                    "metadata": {"size_bytes": len(raw_bytes)},
                }

            if fields.get("title"):
                parsed_src["title"] = str(fields["title"]).strip()
            if fields.get("source_type"):
                parsed_src["source_type"] = str(fields["source_type"]).strip()

            existing = await d1_first(
                env,
                "SELECT * FROM course_sources WHERE course_id = ? AND file_hash = ?",
                (course_id, parsed_src["file_hash"]),
            )
            if existing:
                return make_json_response(
                    {"ok": True, "deduplicated": True, "source": _format_source_row(existing)}
                )

            src_id = f"src-{uuid.uuid4().hex[:12]}"
            await d1_run(
                env,
                """
                INSERT INTO course_sources
                    (id, course_id, source_type, title, origin_url, search_query, file_hash, storage_key,
                     raw_text_excerpt, structured_chunks_json, visual_assets_json, discovered_links_json, metadata_json, status)
                VALUES (?, ?, ?, ?, '', '', ?, '', ?, ?, '[]', '[]', ?, 'PARSED')
                """,
                (
                    src_id,
                    course_id,
                    parsed_src["source_type"],
                    parsed_src["title"],
                    parsed_src["file_hash"],
                    parsed_src["raw_text_excerpt"],
                    json.dumps(parsed_src["structured_chunks"], ensure_ascii=False),
                    json.dumps(parsed_src["metadata"], ensure_ascii=False),
                ),
            )
            await d1_run(
                env,
                "UPDATE courses SET source_count = (SELECT COUNT(*) FROM course_sources WHERE course_id = ?) WHERE id = ?",
                (course_id, course_id),
            )
            saved = await d1_first(env, "SELECT * FROM course_sources WHERE id = ?", (src_id,))
            return make_json_response(
                {"ok": True, "deduplicated": False, "source": _format_source_row(saved or {"id": src_id, "course_id": course_id, **parsed_src})},
                status=201,
            )

        # Ingest URL
        if method == "POST" and path == "/api/sources/ingest-url":
            body = await _read_json_body(request)
            course_id = str(body.get("course_id") or "").strip()
            urls_list = body.get("urls") if isinstance(body.get("urls"), list) else []
            url = str(body.get("url") or (urls_list[0] if urls_list else "") or "").strip()
            if not course_id:
                return _bad_request("course_id is required")
            course = await d1_first(env, "SELECT id FROM courses WHERE id = ?", (course_id,))
            if not course:
                return _not_found("Course not found")

            guard_body = dict(body)
            if url and not guard_body.get("url"):
                guard_body["url"] = url
            blocked = await enforce_web_lite_limits(request, "/api/sources/ingest-url", guard_body, env, session=session)
            if blocked is not None:
                return blocked

            try:
                validate_public_http_url(url)
            except ValueError as exc:
                return _bad_request(str(exc), error="SSRF_BLOCKED")

            inferred_url_type = infer_source_type(guard_body) or "web_url"
            # Allow inline snippet/title when supplied (e.g., adding OpenAlex search hit) or fetch live preview
            if body.get("snippet") or body.get("raw_text_excerpt"):
                title = strip_unsafe_html(str(body.get("title") or url))[:200]
                excerpt = strip_unsafe_html(str(body.get("raw_text_excerpt") or body.get("snippet") or ""))[:8000]
                file_hash = sha256_hex_sync(f"{url}:{excerpt[:500]}")
                preview = {
                    "source_type": inferred_url_type,
                    "title": title or url,
                    "origin_url": url,
                    "file_hash": file_hash,
                    "raw_text_excerpt": excerpt,
                    "structured_chunks": [{"index": 1, "label": title or "Preview", "text": excerpt[:3000]}],
                    "metadata": {"from_search": True},
                }
            elif inferred_url_type != "web_url":
                preview = {
                    "source_type": inferred_url_type,
                    "title": strip_unsafe_html(str(body.get("title") or url))[:200] or url,
                    "origin_url": url,
                    "file_hash": sha256_hex_sync(url),
                    "raw_text_excerpt": f"[{inferred_url_type.upper()} source: {url}]",
                    "structured_chunks": [{"index": 1, "label": inferred_url_type.upper(), "text": url}],
                    "metadata": {"external_url": url},
                }
            else:
                try:
                    preview = await fetch_public_webpage_preview(url)
                except ValueError as exc:
                    return _bad_request(str(exc), error="FETCH_FAILED")

            existing = await d1_first(
                env,
                "SELECT * FROM course_sources WHERE course_id = ? AND file_hash = ?",
                (course_id, preview["file_hash"]),
            )
            if existing:
                return make_json_response(
                    {"ok": True, "deduplicated": True, "source": _format_source_row(existing)}
                )

            src_id = f"src-{uuid.uuid4().hex[:12]}"
            await d1_run(
                env,
                """
                INSERT INTO course_sources
                    (id, course_id, source_type, title, origin_url, search_query, file_hash, storage_key,
                     raw_text_excerpt, structured_chunks_json, visual_assets_json, discovered_links_json, metadata_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, '[]', '[]', ?, 'PARSED')
                """,
                (
                    src_id,
                    course_id,
                    preview["source_type"],
                    preview["title"],
                    preview["origin_url"],
                    str(body.get("search_query") or ""),
                    preview["file_hash"],
                    preview["raw_text_excerpt"],
                    json.dumps(preview["structured_chunks"], ensure_ascii=False),
                    json.dumps(preview["metadata"], ensure_ascii=False),
                ),
            )
            await d1_run(
                env,
                "UPDATE courses SET source_count = (SELECT COUNT(*) FROM course_sources WHERE course_id = ?) WHERE id = ?",
                (course_id, course_id),
            )
            saved = await d1_first(env, "SELECT * FROM course_sources WHERE id = ?", (src_id,))
            return make_json_response(
                {"ok": True, "deduplicated": False, "source": _format_source_row(saved or {"id": src_id, "course_id": course_id, **preview})},
                status=201,
            )

        # FTS Search (Locked in Web Lite)
        if method == "GET" and path == "/api/sources/fts":
            q = (query.get("q") or [""])[0].strip()
            course_id = (query.get("course_id") or [""])[0].strip()
            blocked = await enforce_web_lite_limits(
                request,
                "/api/sources/fts",
                {"q": q, "course_id": course_id},
                env,
                session=session,
            )
            if blocked is not None:
                return blocked
            like = f"%{q}%"
            if course_id:
                rows = await d1_all(
                    env,
                    "SELECT id, course_id, source_type, title, raw_text_excerpt FROM course_sources WHERE course_id = ? AND (title LIKE ? OR raw_text_excerpt LIKE ?) LIMIT 30",
                    (course_id, like, like),
                )
            else:
                rows = await d1_all(
                    env,
                    "SELECT id, course_id, source_type, title, raw_text_excerpt FROM course_sources WHERE title LIKE ? OR raw_text_excerpt LIKE ? LIMIT 30",
                    (like, like),
                )
            return make_json_response({"ok": True, "query": q, "results": [dict(r) for r in rows]})

        # Delete Source
        if method == "DELETE" and path.startswith("/api/sources/"):
            src_id = unquote(path[len("/api/sources/") :]).strip()
            existing = await d1_first(env, "SELECT id, course_id FROM course_sources WHERE id = ?", (src_id,))
            if not existing:
                return _not_found("Source not found")
            if not is_admin and str(existing.get("course_id") or "") == "course-sample-cs-core":
                return _forbidden("Sample course sources cannot be modified in guest preview.")
            cid = str(existing["course_id"])
            await d1_run(env, "DELETE FROM course_sources WHERE id = ?", (src_id,))
            await d1_run(
                env,
                "UPDATE courses SET source_count = (SELECT COUNT(*) FROM course_sources WHERE course_id = ?) WHERE id = ?",
                (cid, cid),
            )
            return make_json_response({"ok": True, "deleted": True, "id": src_id})

        # Slide Upload to R2
        if method == "POST" and path == "/api/slides/upload":
            fields, files = await _read_multipart_or_json(request)
            course_id = (fields.get("course_id") or "").strip()
            file_tuple = files.get("file")
            filename = (file_tuple[0] if file_tuple else fields.get("filename")) or "slide_1.jpg"
            slide_bytes = file_tuple[1] if file_tuple else b""

            if not course_id:
                return _bad_request("course_id is required")

            guard_payload: dict[str, Any] = {"course_id": course_id}
            if fields.get("total_slides"):
                try:
                    guard_payload["total_slides"] = int(fields["total_slides"])
                except ValueError:
                    pass
            if fields.get("slide_index"):
                try:
                    guard_payload["slide_count"] = int(fields["slide_index"])
                except ValueError:
                    pass
            if is_web_lite(env):
                existing_slide_count = await count_course_slides(env, course_id)
                if existing_slide_count >= MAX_WEB_LITE_SLIDES:
                    guard_payload["slide_count"] = existing_slide_count + 1

            blocked = await enforce_web_lite_limits(
                request,
                "/api/slides/upload",
                guard_payload,
                env,
                session=session,
            )
            if blocked is not None:
                return blocked

            try:
                key = build_slide_key(course_id, filename)
                uploaded = await upload_slide(env, key, slide_bytes)
            except ValueError as exc:
                return _bad_request(str(exc), error="INVALID_SLIDE")

            if not uploaded.get("ok"):
                return make_json_response(uploaded, status=503)

            return make_json_response(uploaded, status=201)

        # Progress Updates
        if method == "POST" and path == "/api/progress/task":
            body = await _read_json_body(request)
            subject_id = str(body.get("subject_id") or "").strip()
            if not subject_id:
                return _bad_request("subject_id is required")
            existing = await d1_first(env, "SELECT * FROM user_progress WHERE subject_id = ?", (subject_id,))
            completed_tasks = _safe_json_loads((existing or {}).get("completed_tasks"), [])
            if isinstance(body.get("completed_tasks"), list):
                completed_tasks = [str(x) for x in body["completed_tasks"]]
            elif body.get("task_id"):
                tid = str(body["task_id"])
                done = bool(body.get("completed", True))
                if done and tid not in completed_tasks:
                    completed_tasks.append(tid)
                elif not done and tid in completed_tasks:
                    completed_tasks.remove(tid)

            await d1_run(
                env,
                """
                INSERT INTO user_progress (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(subject_id) DO UPDATE SET
                    completed_tasks = excluded.completed_tasks,
                    updated_at = datetime('now')
                """,
                (
                    subject_id,
                    int((existing or {}).get("quiz_attempts") or 0),
                    float((existing or {}).get("last_quiz_score") or 0.0),
                    float((existing or {}).get("mastery_score") or 0.0),
                    json.dumps(completed_tasks),
                    str((existing or {}).get("weak_spots") or "[]"),
                ),
            )
            return make_json_response({"ok": True, "subject_id": subject_id, "completed_tasks": completed_tasks})

        if method == "POST" and path == "/api/progress/quiz":
            body = await _read_json_body(request)
            subject_id = str(body.get("subject_id") or "").strip()
            if not subject_id:
                return _bad_request("subject_id is required")
            score = float(body.get("score") or 0.0)
            total = max(1.0, float(body.get("total") or 1.0))
            pct = round((score / total) * 100.0 if score <= total else score, 2)
            weak_spots = body.get("weak_spots") if isinstance(body.get("weak_spots"), list) else []

            existing = await d1_first(env, "SELECT * FROM user_progress WHERE subject_id = ?", (subject_id,))
            attempts = int((existing or {}).get("quiz_attempts") or 0) + 1
            prev_mastery = float((existing or {}).get("mastery_score") or 0.0)
            mastery = round(pct if prev_mastery == 0 else (0.4 * prev_mastery + 0.6 * pct), 2)

            await d1_run(
                env,
                """
                INSERT INTO user_progress (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(subject_id) DO UPDATE SET
                    quiz_attempts = excluded.quiz_attempts,
                    last_quiz_score = excluded.last_quiz_score,
                    mastery_score = excluded.mastery_score,
                    weak_spots = excluded.weak_spots,
                    updated_at = datetime('now')
                """,
                (
                    subject_id,
                    attempts,
                    pct,
                    mastery,
                    str((existing or {}).get("completed_tasks") or "[]"),
                    json.dumps(weak_spots),
                ),
            )
            return make_json_response(
                {
                    "ok": True,
                    "subject_id": subject_id,
                    "quiz_attempts": attempts,
                    "last_quiz_score": pct,
                    "mastery_score": mastery,
                    "weak_spots": weak_spots,
                }
            )

        # AI Grader (Locked in Web Lite)
        if method == "POST" and path == "/api/exam/grade":
            body = await _read_json_body(request)
            blocked = await enforce_web_lite_limits(request, "/api/exam/grade", body, env, session=session)
            if blocked is not None:
                return blocked
            answer = str(body.get("answer") or body.get("student_answer") or "").strip()
            question = str(body.get("question") or "").strip()
            rubric = body.get("rubric") or []
            max_marks = int(body.get("max_marks") or 5)
            prompt = (
                f"Grade the student answer out of {max_marks} marks using JSON keys "
                "`score` (number), `max_marks` (number), `feedback` (string), `missing_points` (list).\n"
                f"Question: {question}\nRubric: {json.dumps(rubric)}\nStudent Answer: {answer}"
            )
            ai_res = await call_ai_with_pool(
                env,
                prompt,
                expect_json=True,
                cache_endpoint="exam_grade",
                owner_sub=owner_sub,
            )
            if ai_res and isinstance(ai_res.get("data"), dict):
                return make_json_response({"ok": True, **ai_res["data"]})
            return make_json_response(
                {
                    "ok": False,
                    "error": "INTERNET_OR_API_UNAVAILABLE",
                    "message": "Internet connection and a valid online AI API key are required for AI grading.",
                },
                status=503,
            )

        # Authenticated AI Relay for Downloaded System App (Keeps Default Keys Secret on Cloudflare)
        if method == "POST" and path == "/api/ai/relay":
            if not can_edit:
                return _forbidden("Web Lite guest preview cannot access System App AI relay. Sign in with Google or Admin.")
            body = await _read_json_body(request)
            prompt = str(body.get("prompt") or "")
            if not prompt.strip():
                return _bad_request("Missing prompt.", error="INVALID_PROMPT")
            if len(prompt) > 60_000:
                return _bad_request("Prompt exceeds maximum relay size.", error="PROMPT_TOO_LARGE")
            endpoint_name = str(body.get("endpoint") or "system_relay").strip()[:64] or "system_relay"
            expect_json = bool(body.get("expect_json", True))

            keys = await get_candidate_keys(env, owner_sub=owner_sub)
            if not keys:
                return make_json_response(
                    {
                        "ok": False,
                        "error": "API_KEY_REQUIRED",
                        "message": "No default API key configured on Cloudflare Worker.",
                    },
                    status=400,
                )

            ai_res = await call_ai_with_pool(
                env,
                prompt,
                expect_json=expect_json,
                cache_endpoint=endpoint_name,
                owner_sub=owner_sub,
            )
            if ai_res and ai_res.get("ok"):
                return make_json_response(
                    {
                        "ok": True,
                        "relayed": True,
                        "cached": bool(ai_res.get("cached", False)),
                        "provider": str(ai_res.get("provider") or "cloud_relay"),
                        "data": ai_res.get("data"),
                    }
                )
            return make_json_response(
                {
                    "ok": False,
                    "error": "INTERNET_OR_API_UNAVAILABLE",
                    "message": "Cloudflare Worker AI relay could not reach upstream AI providers.",
                },
                status=503,
            )

        # Export Endpoints (Locked in Web Lite)
        if path.startswith("/api/export"):
            blocked = await enforce_web_lite_limits(request, path, None, env, session=session)
            if blocked is not None:
                return blocked
            return make_json_response({"ok": True, "edition": edition})

        return _not_found(f"Unknown API route: {method} {path}")

    except Exception as exc:
        # Never echo internal stack traces or secrets in HTTP error responses.
        return make_json_response(
            {
                "ok": False,
                "error": "INTERNAL_ERROR",
                "message": f"Worker request error: {type(exc).__name__}",
            },
            status=500,
        )


class Default(WorkerEntrypoint):
    """Cloudflare Python Worker entrypoint (`wrangler.toml: main = 'src/main.py'`)."""

    async def on_fetch(self, request: Any) -> Any:
        return await handle_request(request, self.env)

    async def fetch(self, request: Any) -> Any:
        return await handle_request(request, self.env)


async def on_fetch(request: Any, env: Any = None) -> Any:
    """Top-level handler for Cloudflare Python Worker runtime."""
    return await handle_request(request, env)
