"""Async Multi-API Gemini 2.0 Flash + Groq client, cross-source synthesizer, and rubric AI grader."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import quote_plus

import httpx

from backend.local_app.local_db import DEFAULT_COOLDOWN_SECONDS, LocalDatabase
from backend.local_app.parsers.doc_parsers import sanitize_plain_text

GEMINI_MODEL_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-4-hydrogen:generateContent?key={api_key}"
)
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Workload classification:
# gemini-4-hydrogen is the primary flagship model for heavy course synthesis and exam grading
# (capped at 20 requests/day on the free tier).
# gemini-3.8-flash and gemini-3.5-flash-lite act as automatic fallbacks when Hydrogen returns 429 RESOURCE_EXHAUSTED.
HEAVY_AI_TASKS = frozenset({
    "courses_synthesize",
    "course_synthesize",
    "exam_grade",
    "exam_grading",
    "heavy_synthesis",
})


def resolve_gemini_model(model_name: str) -> str:
    """Normalize model names and apply deprecation / automatic routing rules.

    gemini-2.5-flash is deprecated and will automatically route to gemini-3.5-flash-lite
    starting December 1, 2026.
    """
    clean = (model_name or "").strip().lower()
    clean = clean.removeprefix("models/")
    if clean == "gemini-2.5-flash":
        try:
            now = datetime.now(timezone.utc)
            if (now.year, now.month, now.day) >= (2026, 12, 1):
                return "gemini-3.5-flash-lite"
        except Exception:
            return "gemini-3.5-flash-lite"
        return "gemini-2.5-flash"
    return clean


def get_gemini_models_for_task(task: str | None = None) -> tuple[str, ...]:
    """Select Gemini model hierarchy based on workload intensity.

    Heavy tasks (synthesis & exam grading) start with gemini-4-hydrogen, with gemini-3.8-flash
    and gemini-3.5-flash-lite as automatic fallbacks upon 429 RESOURCE_EXHAUSTED.
    """
    clean_task = (task or "").strip().lower()
    if clean_task in HEAVY_AI_TASKS:
        models = (
            "gemini-4-hydrogen",
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
        )
    else:
        # Standard lightweight tasks default to flash models to preserve Hydrogen quota
        models = (
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
        )
    return tuple(resolve_gemini_model(m) for m in models)

_ENV_FALLBACK_SPECS: tuple[tuple[str, str], ...] = (
    ("GEMINI_API_KEY", "gemini"),
    *tuple((f"GEMINI_KEY_{i}", "gemini") for i in range(1, 11)),
    ("GROQ_API_KEY", "groq"),
    *tuple((f"GROQ_KEY_{i}", "groq") for i in range(1, 11)),
    ("OPENROUTER_API_KEY", "openrouter"),
    *tuple((f"OPENROUTER_KEY_{i}", "openrouter") for i in range(1, 11)),
)
_ENV_COOLDOWNS: dict[str, int] = {}

_BADGE_COLORS = (
    "#3B82F6",
    "#10B981",
    "#8B5CF6",
    "#F59E0B",
    "#EC4899",
    "#06B6D4",
    "#F97316",
    "#6366F1",
)


class AIServiceRequiredError(RuntimeError):
    """Raised when online AI synthesis/grading fails due to missing key or internet outage."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        error_code: str = "INTERNET_OR_API_UNAVAILABLE",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```[^\n]*$", "", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
        # Sanitize unescaped LaTeX math backslashes common in academic AI formula responses
        try:
            sanitized = re.sub(r'\\(?![/"\\bfnrtu]|u[0-9a-fA-F]{4})', r'\\\\', candidate)
            parsed = json.loads(sanitized)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _get_all_candidate_keys(
    db: LocalDatabase | None,
    *,
    owner_sub: str | None = None,
) -> list[dict[str, str]]:
    if db is not None:
        return db.get_candidate_keys(owner_sub=owner_sub)

    try:
        from .env_config import get_configured_env_api_keys
    except ImportError:
        from env_config import get_configured_env_api_keys

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    now = int(time.time())
    for sec_id, provider, val in get_configured_env_api_keys():
        if not val or val in seen:
            continue
        if _ENV_COOLDOWNS.get(sec_id, 0) > now:
            continue
        seen.add(val)
        candidates.append({"id": sec_id, "provider": provider, "api_key": val})
    candidates.sort(
        key=lambda c: 1 if (c.get("provider") == "gemini" and not str(c.get("api_key") or "").startswith("AIza")) else 0
    )
    return candidates


async def _invoke_provider(
    client: httpx.AsyncClient,
    provider: str,
    api_key: str,
    prompt: str,
    *,
    expect_json: bool = True,
    multimodal_parts: list[dict[str, Any]] | None = None,
    task: str | None = None,
) -> tuple[int, str]:
    if provider == "gemini":
        gemini_models = get_gemini_models_for_task(task)
        last_status = 503
        last_text = "Gemini service unavailable"
        for raw_model in gemini_models:
            model = resolve_gemini_model(raw_model)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={quote_plus(api_key)}"
            gen_cfg: dict[str, Any] = {"temperature": 0.25}
            if expect_json:
                gen_cfg["responseMimeType"] = "application/json"
            parts: list[dict[str, Any]] = [{"text": prompt}]
            if multimodal_parts:
                parts.extend(multimodal_parts)
            body = {
                "contents": [{"parts": parts}],
                "generationConfig": gen_cfg,
            }
            resp = await client.post(url, json=body)
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                    res_parts = payload["candidates"][0]["content"]["parts"]
                    return 200, str(res_parts[0].get("text", ""))
                except Exception:
                    return 502, resp.text
            last_status, last_text = resp.status_code, resp.text

            # Automatic Fallback Behavior:
            # gemini-4-hydrogen is the primary model for heavy synthesis & grading (capped at 20 req/day).
            # When Hydrogen returns 429 RESOURCE_EXHAUSTED or when an endpoint returns 404/503,
            # automatically fail over to Gemini 3.8 Flash or Gemini 3.5 Flash-Lite.
            is_exhausted = (resp.status_code == 429) or ("RESOURCE_EXHAUSTED" in resp.text) or ("QUOTA_EXCEEDED" in resp.text)
            if resp.status_code in (404, 503) or is_exhausted:
                continue

            if resp.status_code in (401, 403):
                break
        return last_status, last_text

    if provider in {"groq", "openrouter"}:
        url = GROQ_CHAT_URL if provider == "groq" else OPENROUTER_CHAT_URL
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OmniLearn-AI/1.0",
        }
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://omnilearn.ai"
            headers["X-Title"] = "OmniLearn AI"
            models_to_try = ("meta-llama/llama-3.3-70b-instruct", "google/gemini-2.0-flash-001", "openrouter/auto")
        else:
            models_to_try = ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b")

        last_status = 503
        last_text = f"{provider} unavailable"
        for model in models_to_try:
            body: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.25,
            }
            if expect_json and provider == "groq":
                body["response_format"] = {"type": "json_object"}
            resp = await client.post(url, headers=headers, json=body)
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                    content = payload["choices"][0]["message"]["content"]
                    return 200, str(content)
                except Exception:
                    return 502, resp.text
            last_status, last_text = resp.status_code, resp.text
            if resp.status_code not in (404, 503):
                break
        return last_status, last_text

    return 400, f"Unsupported provider: {provider}"


def _get_cloud_relay_credentials(
    db: LocalDatabase | None,
    *,
    owner_sub: str | None = None,
) -> tuple[str, str] | None:
    """Return `(cloud_url, bearer_token)` if Cloudflare Worker AI Relay is configured."""
    try:
        from .env_config import get_cloud_relay_config
    except ImportError:
        from env_config import get_cloud_relay_config

    cfg = get_cloud_relay_config()
    cloud_url = str(cfg.get("cloud_url") or "").strip().rstrip("/")
    if not cloud_url.startswith(("http://", "https://")):
        return None
    token = db.get_cloud_token(owner_sub) if db is not None else str(cfg.get("cloud_token") or "").strip()
    if not token:
        token = str(cfg.get("cloud_token") or "").strip()
    if not token:
        return None
    return cloud_url, token


def _has_ai_access(
    db: LocalDatabase | None,
    *,
    owner_sub: str | None = None,
) -> bool:
    """Return True if local API keys OR authenticated Cloudflare AI relay exist."""
    return bool(
        _get_all_candidate_keys(db, owner_sub=owner_sub)
        or _get_cloud_relay_credentials(db, owner_sub=owner_sub)
    )


async def _relay_via_cloud_worker(
    cloud_url: str,
    bearer_token: str,
    prompt: str,
    *,
    endpoint: str,
    expect_json: bool,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Relay AI prompt securely to Cloudflare Worker `/api/ai/relay` using hidden Worker secrets."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(
            f"{cloud_url}/api/ai/relay",
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
            },
            json={
                "prompt": prompt,
                "endpoint": endpoint,
                "expect_json": expect_json,
            },
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("ok") and isinstance(payload.get("data"), dict):
            return {
                "ok": True,
                "cached": bool(payload.get("cached", False)),
                "relayed": True,
                "provider": str(payload.get("provider") or "cloud_relay"),
                "data": payload["data"],
            }
        return None
    except Exception:
        return None
    finally:
        if owns_client:
            await client.aclose()


async def call_ai_pool(
    db: LocalDatabase | None,
    prompt: str,
    *,
    endpoint: str = "ai_synthesize",
    expect_json: bool = True,
    owner_sub: str | None = None,
    client: httpx.AsyncClient | None = None,
    multimodal_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Call local personal/env keys or relay through Cloudflare Worker when user has no local key."""
    cache_seed = prompt + (f":{len(multimodal_parts)}" if multimodal_parts else "")
    prompt_hash = hashlib.sha256(f"{endpoint}:{cache_seed}".encode("utf-8")).hexdigest()
    if db is not None:
        cached = db.get_api_cache(prompt_hash, endpoint=endpoint)
        if isinstance(cached, dict):
            return {"ok": True, "cached": True, "data": cached}

    candidates = _get_all_candidate_keys(db, owner_sub=owner_sub)
    if not candidates:
        relay = _get_cloud_relay_credentials(db, owner_sub=owner_sub)
        if relay is None:
            return None
        cloud_url, bearer_token = relay
        relayed = await _relay_via_cloud_worker(
            cloud_url,
            bearer_token,
            prompt,
            endpoint=endpoint,
            expect_json=expect_json,
            client=client,
        )
        if relayed and db is not None and isinstance(relayed.get("data"), dict):
            db.set_api_cache(prompt_hash, endpoint, relayed["data"], tokens_saved=80)
        return relayed

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=25.0)

    try:
        for cand in candidates:
            key_id = cand["id"]
            provider = cand["provider"]
            api_key = cand["api_key"]
            try:
                status, raw_text = await _invoke_provider(
                    client,
                    provider,
                    api_key,
                    prompt,
                    expect_json=expect_json,
                    multimodal_parts=multimodal_parts,
                    task=endpoint,
                )
            except Exception:
                if key_id.startswith("env:"):
                    _ENV_COOLDOWNS[key_id] = int(time.time()) + DEFAULT_COOLDOWN_SECONDS
                elif db is not None:
                    db.mark_key_cooldown(key_id, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS)
                continue

            if status == 200:
                if db is not None:
                    db.mark_key_used(key_id)
                data_obj: dict[str, Any] | None
                if expect_json:
                    data_obj = _extract_json_object(raw_text)
                    if data_obj is None:
                        continue
                else:
                    data_obj = {"text": raw_text}
                if db is not None:
                    db.set_api_cache(prompt_hash, endpoint, data_obj, tokens_saved=max(80, len(raw_text) // 4))
                return {"ok": True, "cached": False, "provider": provider, "data": data_obj}

            if status in {429, 503}:
                if key_id.startswith("env:"):
                    _ENV_COOLDOWNS[key_id] = int(time.time()) + DEFAULT_COOLDOWN_SECONDS
                elif db is not None:
                    db.mark_key_cooldown(key_id, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS)
                continue

            if status in {401, 403}:
                if key_id.startswith("env:"):
                    _ENV_COOLDOWNS[key_id] = int(time.time()) + 3600
                elif db is not None:
                    db.mark_key_cooldown(key_id, is_invalid=True)
                continue

            if db is not None:
                db.mark_key_cooldown(key_id, cooldown_seconds=30)

        return None
    finally:
        if owns_client:
            await client.aclose()


def sanitize_mermaid(diagram_src: str) -> str:
    """Sanitize Mermaid code so it parses reliably in strict mode without syntax errors."""
    if not isinstance(diagram_src, str) or not diagram_src.strip():
        return "flowchart TD\n    A[\"Core Principles\"] --> B[\"Exam Mastery\"]"
    lines = diagram_src.strip().splitlines()
    header_found = False
    cleaned_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        if any(stripped.startswith(prefix) for prefix in ("flowchart", "graph", "stateDiagram", "classDiagram", "erDiagram")):
            header_found = True
            cleaned_lines.append(stripped)
            continue
        line_clean = re.sub(
            r'([A-Za-z0-9_-]+)\[([^"\]\n]*\([^"\]\n]*\)[^"\]\n]*)\]',
            r'\1["\2"]',
            stripped,
        )
        line_clean = re.sub(r'(?<=\w)\s*&\s*(?=\w)', ' and ', line_clean)
        cleaned_lines.append("    " + line_clean if not line_clean.startswith("subgraph") and not line_clean.startswith("end") else line_clean)
    if not header_found:
        cleaned_lines.insert(0, "flowchart TD")
    return "\n".join(cleaned_lines)


def _collect_slide_crops(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    crops: list[dict[str, Any]] = []
    for src in sources:
        assets = src.get("visual_assets") or []
        if isinstance(assets, str):
            try:
                assets = json.loads(assets)
            except Exception:
                assets = []
        for item in assets:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            if url:
                crops.append(
                    {
                        "url": url,
                        "caption": item.get("label") or src.get("title") or "Visual Slide",
                        "citation": item.get("citation") or f"From: {src.get('title', 'Source')}",
                        "source_id": src.get("id", ""),
                        "timestamp": item.get("timestamp", ""),
                    }
                )
    return crops


def _build_schedule(title: str, target_days: int, daily_hours: float, sid_prefix: str) -> list[dict[str, Any]]:
    days = max(1, min(30, int(target_days or 14)))
    hours = float(daily_hours or 3.0)
    schedule: list[dict[str, Any]] = []
    for day in range(1, days + 1):
        phase = (
            "Foundations"
            if day <= max(1, days // 3)
            else ("Practice & Drills" if day <= max(2, (2 * days) // 3) else "Timed Mock & Revision")
        )
        schedule.append(
            {
                "day": day,
                "id": f"{sid_prefix}-day-{day}",
                "title": f"Day {day}: {phase} — {title[:40]}",
                "phase": phase,
                "focus": f"{title} ({phase})",
                "estimated_hours": hours,
                "tasks": [
                    {
                        "id": f"{sid_prefix}-d{day}-t1",
                        "text": f"Review visual notes & citations for {title} (Day {day})",
                        "duration_min": int(hours * 20),
                    },
                    {
                        "id": f"{sid_prefix}-d{day}-t2",
                        "text": f"Complete active-recall questions & formula drills ({phase})",
                        "duration_min": int(hours * 20),
                    },
                    {
                        "id": f"{sid_prefix}-d{day}-t3",
                        "text": "Practice 1 timed exam rubric answer",
                        "duration_min": int(hours * 20),
                    },
                ],
            }
        )
    return schedule


def build_subject_scaffold(
    course_title: str,
    sources: list[dict[str, Any]],
    *,
    rois: list[dict[str, Any]] | None = None,
    target_days: int = 14,
    daily_hours: float = 3.0,
    target_subject_name: str = "",
    subject_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build baseline subject structures merging all 4 video quadrants as lectures of 1 subject."""
    clean_target = sanitize_plain_text(target_subject_name).strip()
    if not clean_target:
        for src in sources:
            meta = src.get("metadata") or {}
            if isinstance(meta, dict) and str(meta.get("subject_name") or "").strip():
                clean_target = sanitize_plain_text(str(meta["subject_name"])).strip()
                break
    clean_title = clean_target or sanitize_plain_text(course_title) or "Unified Course"
    all_slides = _collect_slide_crops(sources)

    concepts: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    transcript_entries: list[dict[str, Any]] = []
    for idx, src in enumerate(sources):
        src_title = str(src.get("title") or f"Source {idx + 1}")
        src_type = str(src.get("source_type") or "txt")
        excerpt = sanitize_plain_text(str(src.get("raw_text_excerpt") or ""))
        chunks = src.get("structured_chunks") or []
        citations.append(
            {
                "source_id": str(src.get("id") or f"src-{idx + 1}"),
                "title": src_title,
                "location": src.get("origin_url") or src_type.upper(),
            }
        )
        if chunks and isinstance(chunks, list):
            for ch in chunks[:2]:
                if not isinstance(ch, dict):
                    continue
                ch_text = sanitize_plain_text(str(ch.get("text") or ""))
                if ch_text:
                    concepts.append(
                        {
                            "title": str(ch.get("label") or src_title)[:80],
                            "name": str(ch.get("label") or src_title)[:80],
                            "explanation": ch_text[:320],
                            "citation": str(ch.get("citation") or src_title),
                        }
                    )
                if ch.get("timestamp"):
                    transcript_entries.append(
                        {"timestamp": str(ch["timestamp"]), "text": ch_text[:200]}
                    )
        elif excerpt:
            concepts.append(
                {
                    "title": f"{src_title} Highlights",
                    "name": f"{src_title} Highlights",
                    "explanation": excerpt[:320],
                    "citation": src_title,
                }
            )

    if not concepts:
        concepts = [
            {
                "title": f"Core Concepts of {clean_title}",
                "name": f"Core Concepts of {clean_title}",
                "explanation": f"Synthesized study foundation across {len(sources)} source(s).",
                "citation": clean_title,
            }
        ]

    # Bind slide crops to concepts if slides exist
    for idx, c in enumerate(concepts):
        if all_slides:
            s_match = all_slides[idx % len(all_slides)]
            c["slide_url"] = s_match.get("url") or ""
            c["slide_caption"] = s_match.get("caption") or f"Visual Slide #{idx + 1}"
            c["slide_timestamp"] = s_match.get("timestamp") or "00:00"

    combined_excerpt = " ".join(
        sanitize_plain_text(str(s.get("raw_text_excerpt") or ""))[:400] for s in sources[:5]
    ).strip()
    summary = (
        f"Comprehensive academic synthesis for {clean_title}.\n\n"
        f"1. Foundations & Scope: Synthesizes {len(sources)} ingested materials. "
        f"{combined_excerpt[:500] if combined_excerpt else 'Establishes foundational theoretical principles, formal models, and structural constraints.'}\n\n"
        f"2. Mechanics & State Transitions: Cross-references lecture slides and diagrams to reinforce invariant properties through visual dual-coding.\n\n"
        f"3. Practical Applications: Connects theoretical bounds with applied problem-solving and university-level examination rubrics."
    )

    sid = f"subj-{uuid.uuid4().hex[:8]}"
    safe_node = re.sub(r"[^A-Za-z0-9 ]", "", clean_title)[:28] or "Module"
    mermaid = (
        "flowchart TD\n"
        "    subgraph Ingestion[\"📚 Ingested Materials & Video\"]\n"
        f"        S1[\"Lecture Video & Transcripts ({len(sources)} sources)\"]\n"
        "        S2[\"Visual Keyframes & Slide Crops\"]\n"
        "    end\n"
        f"    subgraph Architecture[\"🧠 {safe_node} Foundations\"]\n"
        "        C1[\"Core Principles & Invariants\"]\n"
        "        C2[\"State Transitions & Mechanisms\"]\n"
        "        C3[\"Mathematical Bounds & Proofs\"]\n"
        "    end\n"
        "    subgraph Application[\"⚡ Synthesis & Verification\"]\n"
        "        A1[\"Active Recall Diagnostics\"]\n"
        "        A2[\"Model University Exam Solutions\"]\n"
        "    end\n"
        "    S1 --> C1\n"
        "    S2 --> C2\n"
        "    C1 --> C2\n"
        "    C2 --> C3\n"
        "    C2 -->|\"Applied Drills\"| A1\n"
        "    C3 -->|\"Analytical Solutions\"| A2"
    )

    formulas = [
        {
            "name": "Active Recall Retention Curve",
            "latex": r"R(t) = e^{-t / S}",
            "note": "Memory stability S increases with each spaced recall session",
        },
        {
            "name": "Weighted Exam Mastery Score",
            "latex": r"M = 0.6 \cdot \text{Quiz\%} + 0.4 \cdot \text{Plan\%}",
            "note": "Combines quiz accuracy and study plan completion",
        },
    ]

    mcqs = [
        {
            "id": f"{sid}-q1",
            "question": f"Which source-backed principle is central to {concepts[0]['title']}?",
            "options": [
                concepts[0]["explanation"][:110] or f"Core principles of {clean_title}",
                "Unverified heuristic bypassing constraints",
                "Ignoring empirical complexity and edge cases",
                "Single-pass memorization without testing",
            ],
            "correct_index": 0,
            "answer_index": 0,
            "explanation": f"Grounded directly in {concepts[0]['citation']}.",
            "citation": concepts[0]["citation"],
        },
        {
            "id": f"{sid}-q2",
            "question": f"How many heterogeneous sources contributed to {clean_title}?",
            "options": [
                f"{len(sources)} cross-referenced source(s)",
                "Zero local sources",
                "Only unparsed binary data",
                "External unverified stubs",
            ],
            "correct_index": 0,
            "answer_index": 0,
            "explanation": "OmniLearn synthesizes notes, slides, and citations across all staged sources.",
            "citation": clean_title,
        },
        {
            "id": f"{sid}-q3",
            "question": "Why should visual diagrams and textual notes be reviewed together?",
            "options": [
                "Dual-coding links structural diagrams with formal definitions",
                "It eliminates formulas from exam papers",
                "It disables citation tracking",
                "It skips spaced repetition",
            ],
            "correct_index": 0,
            "answer_index": 0,
            "explanation": "Pairing rendered slides/diagrams with derivations strengthens exam recall.",
            "citation": "Visual Synthesis",
        },
    ]

    exam_questions = [
        {
            "id": f"{sid}-mp1",
            "section": "Section A",
            "marks": 5,
            "question": f"Summarize the primary concepts in {clean_title} with reference to at least one ingested source.",
            "prompt": f"Summarize the primary concepts in {clean_title} with reference to at least one ingested source.",
            "rubric": ["States core definitions accurately (3m)", "Cites concrete mechanism/example (2m)"],
            "model_answer": summary,
        },
        {
            "id": f"{sid}-mp2",
            "section": "Section B",
            "marks": 10,
            "question": f"Analyze key tradeoffs, assumptions, and edge cases found in {concepts[0]['title']}.",
            "prompt": f"Analyze key tradeoffs, assumptions, and edge cases found in {concepts[0]['title']}.",
            "rubric": ["Explains assumptions & constraints (5m)", "Provides worked analysis/edge cases (5m)"],
            "model_answer": concepts[0]["explanation"],
        },
        {
            "id": f"{sid}-mp3",
            "section": "Section C",
            "marks": 10,
            "question": f"Synthesize an end-to-end applied solution combining insights from {clean_title}.",
            "prompt": f"Synthesize an end-to-end applied solution combining insights from {clean_title}.",
            "rubric": ["Coherent multi-step architecture (6m)", "Verification & complexity discussion (4m)"],
            "model_answer": summary,
        },
    ]

    cleaned_names = [
        sanitize_plain_text(n).strip()
        for n in (subject_names or [])
        if sanitize_plain_text(n).strip()
    ]
    if cleaned_names:
        multi_subjects: list[dict[str, Any]] = []
        for idx, sname in enumerate(cleaned_names):
            s_sid = f"subj-{uuid.uuid4().hex[:8]}"
            multi_subjects.append(
                {
                    "id": s_sid,
                    "quadrant_key": f"subj_{idx + 1}",
                    "title": sname,
                    "short_title": sname[:24],
                    "topic": sname,
                    "unit": f"Subject {idx + 1}",
                    "instructor": "OmniLearn AI",
                    "badge_color": _BADGE_COLORS[idx % len(_BADGE_COLORS)],
                    "summary": summary,
                    "subtitles_transcript": transcript_entries,
                    "slide_crops": all_slides[:32],
                    "source_citations": citations,
                    "mermaid_diagram": mermaid,
                    "key_concepts": concepts[:12],
                    "math_formulas": formulas,
                    "target_days": target_days,
                    "daily_hours": daily_hours,
                    "schedule": _build_schedule(sname, target_days, daily_hours, s_sid),
                    "quizzes": mcqs,
                    "model_paper": {
                        "title": f"University Model Exam Paper — {sname}",
                        "time_allowed": "60 Minutes",
                        "max_marks": 25,
                        "questions": exam_questions,
                    },
                }
            )
        return multi_subjects

    # All 4 quadrants of a multi-window video represent 4 distinct lectures of THIS 1 subject.
    lecture_transcripts: list[dict[str, Any]] = list(transcript_entries)
    lecture_concepts: list[dict[str, Any]] = list(concepts)
    if rois and len(rois) > 1:
        for idx, roi in enumerate(rois):
            rkey = str(roi.get("roi_key") or f"q{idx + 1}")
            rname = str(roi.get("name") or f"Window {idx + 1}")
            lecture_label = f"Lecture {idx + 1} ({rname})"
            lecture_slides_count = sum(
                1
                for s in all_slides
                if rkey in str(s.get("caption", "")).lower() or rkey in str(s.get("url", "")).lower()
            )
            lecture_transcripts.append(
                {
                    "timestamp": f"Quadrant {idx + 1}",
                    "lecture": lecture_label,
                    "quadrant_key": rkey,
                    "text": f"{lecture_label} for {clean_title} ({lecture_slides_count} visual slide(s) extracted).",
                }
            )
            if idx < 4 and not any(lecture_label in str(c.get("title", "")) for c in lecture_concepts):
                lecture_concepts.append(
                    {
                        "title": f"{lecture_label} Highlights",
                        "name": f"{lecture_label} Highlights",
                        "explanation": f"Visual lecture stream from {rname} quadrant merged into {clean_title}.",
                        "citation": f"{lecture_label} • {rkey}",
                    }
                )

    return [
        {
            "id": sid,
            "quadrant_key": "q1",
            "title": clean_title,
            "short_title": clean_title[:24],
            "topic": clean_title,
            "unit": f"{len(rois)} Quadrant Lectures Unified" if rois and len(rois) > 1 else "Unified Module",
            "instructor": "OmniLearn AI",
            "badge_color": _BADGE_COLORS[0],
            "summary": summary,
            "subtitles_transcript": lecture_transcripts,
            "slide_crops": all_slides[:48],
            "source_citations": citations,
            "mermaid_diagram": mermaid,
            "key_concepts": lecture_concepts[:16],
            "math_formulas": formulas,
            "target_days": target_days,
            "daily_hours": daily_hours,
            "schedule": _build_schedule(clean_title, target_days, daily_hours, sid),
            "quizzes": mcqs,
            "model_paper": {
                "title": f"University Model Exam Paper — {clean_title}",
                "time_allowed": "60 Minutes",
                "max_marks": 25,
                "questions": exam_questions,
            },
        }
    ]


offline_synthesize_subjects = build_subject_scaffold


def _apply_ai_payload_to_subject(
    target: dict[str, Any],
    data: dict[str, Any],
    course_title: str,
) -> dict[str, Any]:
    """Merge validated online AI response fields into a subject scaffold."""
    if isinstance(data.get("title"), str) and data["title"].strip():
        target["title"] = sanitize_plain_text(data["title"])[:120]
        target["short_title"] = target["title"][:24]
    if isinstance(data.get("topic"), str) and data["topic"].strip():
        target["topic"] = sanitize_plain_text(data["topic"])[:160]
    if isinstance(data.get("summary"), str) and data["summary"].strip():
        target["summary"] = sanitize_plain_text(data["summary"])[:4000]
    if isinstance(data.get("mermaid_diagram"), str):
        target["mermaid_diagram"] = sanitize_mermaid(data["mermaid_diagram"])
    if isinstance(data.get("key_concepts"), list) and data["key_concepts"]:
        available_slides = target.get("slide_crops") or []
        cleaned_concepts = []
        for idx, c in enumerate(data["key_concepts"][:16]):
            if not isinstance(c, dict):
                continue
            slide_match = available_slides[idx % len(available_slides)] if available_slides else None
            s_url = str(c.get("slide_url") or (slide_match.get("url") if isinstance(slide_match, dict) else slide_match) or "")
            s_cap = str(c.get("slide_caption") or (slide_match.get("caption") if isinstance(slide_match, dict) else f"Visual Frame #{idx + 1}") or "")
            s_ts = str(c.get("slide_timestamp") or (slide_match.get("timestamp") if isinstance(slide_match, dict) else "") or "")
            cleaned_concepts.append(
                {
                    "title": sanitize_plain_text(str(c.get("title") or c.get("name") or "Concept")),
                    "name": sanitize_plain_text(str(c.get("name") or c.get("title") or "Concept")),
                    "explanation": sanitize_plain_text(str(c.get("explanation") or "")),
                    "exam_tip": sanitize_plain_text(str(c.get("exam_tip") or "")),
                    "citation": sanitize_plain_text(str(c.get("citation") or course_title)),
                    "slide_url": s_url,
                    "slide_caption": s_cap,
                    "slide_timestamp": s_ts,
                }
            )
        target["key_concepts"] = cleaned_concepts
    if isinstance(data.get("math_formulas"), list) and data["math_formulas"]:
        target["math_formulas"] = [f for f in data["math_formulas"][:10] if isinstance(f, dict)]
    if isinstance(data.get("quizzes"), list) and data["quizzes"]:
        cleaned_qs = []
        for idx, q in enumerate(data["quizzes"][:25]):
            if not isinstance(q, dict):
                continue
            cidx = int(q.get("correct_index", q.get("answer_index", 0)) or 0)
            cleaned_qs.append(
                {
                    "id": str(q.get("id") or f"q-{idx + 1}"),
                    "question": sanitize_plain_text(str(q.get("question") or "")),
                    "options": [sanitize_plain_text(str(o)) for o in (q.get("options") or [])],
                    "correct_index": cidx,
                    "answer_index": cidx,
                    "explanation": sanitize_plain_text(str(q.get("explanation") or "")),
                    "citation": sanitize_plain_text(str(q.get("citation") or "")),
                }
            )
        if cleaned_qs:
            target["quizzes"] = cleaned_qs
    if isinstance(data.get("exam_questions"), list) and data["exam_questions"]:
        cleaned_ex = [q for q in data["exam_questions"][:12] if isinstance(q, dict)]
        if cleaned_ex:
            target["model_paper"]["questions"] = cleaned_ex
    return target


def build_tutor_chat_prompt(user_msg: str, course_context: str = "", history: list[dict[str, Any]] | None = None) -> str:
    """Build a pedagogy-aligned system prompt for the OmniLearn AI Study Tutor & Application Assistant."""
    history_lines: list[str] = []
    if history:
        for h in history[-8:]:
            role = "Student" if str(h.get("role", "")).lower() in ("user", "student") else "Tutor"
            content = str(h.get("content") or "").strip()
            if content:
                history_lines.append(f"{role}: {content}")
    history_str = "\n".join(history_lines) if history_lines else "None"

    return f"""You are the OmniLearn AI Study Tutor & Application Assistant.
Your mission is twofold:
1. ACADEMIC TUTOR: Explain concepts clearly, clarify student doubts, break down formulas and math step-by-step, provide intuitive real-world examples, and generate practice questions grounded in the student's study materials.
2. APPLICATION GUIDE: Help the student navigate OmniLearn AI features:
   - "Sources" tab: Upload PDFs, PPTXs, CSVs, TXT/MD, or multi-window video lectures (2x2 grid, 1x2 split screen). Paste web URLs or search OpenAlex papers & YouTube.
   - "Synthesize": Click "Synthesize Subject Notes & Exam Pack" to automatically generate unified notes, KaTeX formulas, Mermaid diagrams, adaptive study plans, and quizzes.
   - "Notes" tab: Read concepts, view slide visuals, and download high-quality notes as PDF or Markdown.
   - "Study Plan" tab: Track adaptive milestones, customizable study days, and hours/day.
   - "Quiz" tab: Practice interactive multiple-choice quizzes with instant grading and explanations.
   - "Exam" tab: Simulate timed mock exam papers with rubric-based AI grading.
   - "Progress" tab: Track mastery scores and analytics across subjects.
   - "Settings": Add personal Gemini, Groq, or OpenRouter API keys for custom quota.

PEDAGOGICAL & FORMATTING GUIDELINES:
- Be encouraging, approachable, and pedagogically clear.
- Use GitHub-flavored Markdown: bold key terms, use bullet points, numbered steps, and code blocks where helpful.
- For mathematical expressions, use standard KaTeX LaTeX: inline `$formula$` or block `$$formula$$`.
- When explaining course topics, prioritize the course context provided below.
- Keep answers structured, insightful, and concise.

COURSE CONTEXT:
{course_context if course_context else "No active course selected."}

CONVERSATION HISTORY:
{history_str}

STUDENT QUESTION:
{user_msg}

TUTOR RESPONSE:"""


async def synthesize_course_sources(
    db: LocalDatabase,
    course_id: str,
    *,
    source_ids: list[str] | None = None,
    target_days: int = 14,
    daily_hours: float = 3.0,
    target_subject_name: str = "",
    subject_names: list[str] | None = None,
    append_subject: bool = False,
    owner_sub: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Synthesize unified or multi-subject study materials online via the Multi-API Pool."""
    bundle = db.get_course(course_id)
    if bundle is None:
        raise KeyError(f"Course '{course_id}' not found.")

    if not _has_ai_access(db, owner_sub=owner_sub):
        raise AIServiceRequiredError(
            "No AI API key configured. Add a personal API key, set `.env`, or sign in with Cloudflare AI Relay configured.",
            status_code=400,
            error_code="API_KEY_REQUIRED",
        )

    course = bundle["course"]
    all_sources = bundle.get("sources") or []
    if source_ids:
        selected_set = set(source_ids)
        sources = [s for s in all_sources if s["id"] in selected_set]
    else:
        sources = all_sources

    rois = bundle.get("rois") or []
    if not rois:
        for src in sources:
            meta = src.get("metadata") or {}
            if isinstance(meta, dict) and isinstance(meta.get("rois"), list) and meta["rois"]:
                rois = meta["rois"]
                break
    scaffolds = build_subject_scaffold(
        course["title"],
        sources,
        rois=rois,
        target_days=target_days,
        daily_hours=daily_hours,
        target_subject_name=target_subject_name,
        subject_names=subject_names,
    )

    source_digest = "\n".join(
        f"- [{s.get('source_type')}] {s.get('title')}: {str(s.get('raw_text_excerpt') or '')[:1800]}"
        for s in sources[:16]
    ) or f"- [course] {course['title']}: No additional document excerpts supplied yet."
    quadrant_note = ""
    if rois and len(rois) > 1 and not subject_names:
        q_labels = ", ".join(
            f"Lecture {idx + 1} ({r.get('name') or r.get('roi_key')})"
            for idx, r in enumerate(rois)
        )
        quadrant_note = (
            f"Multi-Window Video Mode: ALL {len(rois)} quadrant windows ({q_labels}) contain different "
            f"lectures of ONE single subject ('{scaffolds[0]['title']}'). Merge all quadrants into unified notes, "
            "concepts, formulas, quiz questions, and exam questions for that single subject.\n"
        )
    multi_subj_note = ""
    if subject_names:
        multi_subj_note = (
            f"Multiple Subjects Requested (unlimited count={len(subject_names)}): {', '.join(subject_names)}. "
            "Return JSON with top-level key `subjects` containing one object per subject.\n"
        )

    all_slides = _collect_slide_crops(sources)
    slide_lines = []
    for idx, s in enumerate(all_slides[:24]):
        s_url = s.get("url") or ""
        s_cap = s.get("caption") or f"Visual Frame #{idx + 1}"
        s_ts = s.get("timestamp") or ""
        slide_lines.append(f"- URL: {s_url} | Caption: {s_cap} | Timestamp: {s_ts}")
    slide_summary = "\nAvailable Visual Video Slides & Keyframes:\n" + "\n".join(slide_lines) if slide_lines else ""

    prompt = (
        "You are OmniLearn AI, an elite university tutor and exam synthesis engine.\n"
        "Synthesize a deeply technical, pedagogical, academic JSON study pack for this subject with exact keys:\n"
        "`title`: Concise subject title.\n"
        "`topic`: Subject topic.\n"
        "`summary`: Rigorous, clear 3-paragraph executive summary detailing (1) Intuitive theoretical motivation, mental models & fundamental principles, "
        "(2) Core mechanisms, architectural state transitions, and invariant properties, and (3) Practical tradeoffs, asymptotic complexity, and real-world failure modes.\n"
        "`mermaid_diagram`: A publication-quality, multi-stage architecture flowchart (`flowchart TD`) with 8 to 14 distinct nodes "
        "organized into `subgraph` sections (e.g. Ingestion, Processing, Architecture, Verification). "
        "Every node label containing parentheses, brackets, or quotes MUST be strictly enclosed in double quotes (e.g. A[\"Label (with info)\"]). "
        "Include condition labels on connector edges (e.g. -->|\"Valid\"| B).\n"
        "`key_concepts`: List of 6 to 12 in-depth concepts ({title, explanation, exam_tip, citation, slide_url, slide_caption, slide_timestamp}). "
        "For `explanation`, provide 3-5 comprehensive sentences explaining the underlying mechanism and mental model. "
        "For `exam_tip`, provide actionable exam advice or common trap to avoid. "
        "Attach the most relevant `slide_url` from the Available Visual Video Slides list below so lecture slides illustrate the notes.\n"
        "`math_formulas`: List of 4 to 8 rigorous mathematical expressions ({name, latex, note}) with complete KaTeX equations. "
        "In `note`, explain what each variable means and how to apply the formula in problem-solving.\n"
        "`quizzes`: List of 5+ high-yield MCQs ({id, question, options, correct_index, explanation, citation}).\n"
        "`exam_questions`: List of Section A/B/C university examination questions ({id, section, marks, question, rubric, model_answer}).\n\n"
        f"Course: {course['title']}\n"
        f"{quadrant_note}{multi_subj_note}"
        f"Target Days: {target_days}, Daily Hours: {daily_hours}\n"
        f"Sources:\n{source_digest}\n"
        + (f"{slide_summary}\n" if slide_summary else "")
    )

    ai_resp = await call_ai_pool(
        db,
        prompt,
        endpoint="local_synthesize",
        expect_json=True,
        owner_sub=owner_sub,
        client=client,
    )
    if not ai_resp or not isinstance(ai_resp.get("data"), dict):
        raise AIServiceRequiredError(
            "Internet connection and a valid online AI API key are required to synthesize notes. Check network connectivity and `.env` API keys.",
            status_code=503,
            error_code="INTERNET_OR_API_UNAVAILABLE",
        )

    data = ai_resp["data"]
    cached = bool(ai_resp.get("cached", False))

    if isinstance(data.get("subjects"), list) and data["subjects"]:
        merged_subjects: list[dict[str, Any]] = []
        raw_subs = [s for s in data["subjects"] if isinstance(s, dict)]
        for idx, ai_subj in enumerate(raw_subs):
            base = (
                dict(scaffolds[idx])
                if idx < len(scaffolds)
                else dict(
                    build_subject_scaffold(
                        str(ai_subj.get("title") or f"Subject {idx + 1}"),
                        sources,
                        rois=[],
                        target_days=target_days,
                        daily_hours=daily_hours,
                    )[0]
                )
            )
            base["id"] = f"subj-{uuid.uuid4().hex[:8]}"
            base["quadrant_key"] = f"subj_{idx + 1}"
            base["badge_color"] = _BADGE_COLORS[idx % len(_BADGE_COLORS)]
            merged_subjects.append(_apply_ai_payload_to_subject(base, ai_subj, course["title"]))
        scaffolds = merged_subjects
    else:
        scaffolds[0] = _apply_ai_payload_to_subject(scaffolds[0], data, course["title"])

    updated_bundle = db.save_synthesized_course(
        course_id,
        scaffolds,
        target_days=target_days,
        daily_hours=daily_hours,
        replace_existing=not append_subject,
    )
    updated_bundle["used_fallback"] = False
    updated_bundle["cached"] = cached
    return updated_bundle


def _offline_rubric_grade(
    student_answer: str,
    *,
    max_marks: int,
    model_answer: str,
    rubric: list[str],
) -> dict[str, Any]:
    ans_tokens = set(re.findall(r"[a-z0-9]{3,}", student_answer.lower()))
    ref_tokens = set(re.findall(r"[a-z0-9]{3,}", (model_answer + " " + " ".join(rubric)).lower()))
    if not ans_tokens:
        ratio = 0.0
    elif not ref_tokens:
        ratio = min(1.0, len(ans_tokens) / 25.0)
    else:
        overlap = len(ans_tokens & ref_tokens)
        coverage = overlap / max(1, min(len(ref_tokens), 18))
        length_bonus = min(0.25, len(ans_tokens) / 80.0)
        ratio = min(1.0, coverage * 0.85 + length_bonus)

    score = round(max_marks * ratio, 1)
    pct = round((score / max(1, max_marks)) * 100.0, 1)
    return {
        "ok": True,
        "score": score,
        "max_marks": max_marks,
        "percentage": pct,
        "feedback": (
            f"Evaluated offline against {len(rubric) or 1} rubric criteria ({pct}% match). "
            + ("Strong coverage of key concepts." if pct >= 70 else "Include more specific technical terms and citations from the model answer.")
        ),
        "strengths": ["Clear attempt addressing core prompt"] if len(ans_tokens) >= 6 else [],
        "improvements": rubric[:2] if pct < 85 else [],
        "used_fallback": True,
    }


async def grade_exam_answer(
    db: LocalDatabase,
    *,
    subject_id: str = "",
    question_id: str = "",
    student_answer: str,
    question_prompt: str = "",
    model_answer: str = "",
    rubric: list[str] | None = None,
    max_marks: int = 5,
    owner_sub: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Grade a student's written exam answer against the rubric via AI pool or deterministic scorer."""
    clean_ans = sanitize_plain_text(student_answer)
    if not clean_ans:
        raise ValueError("Student answer cannot be empty.")

    resolved_prompt = question_prompt
    resolved_model = model_answer
    if isinstance(rubric, str):
        resolved_rubric = [r.strip() for r in re.split(r"[;\n]+", rubric) if r.strip()]
    else:
        resolved_rubric = [str(r).strip() for r in (rubric or []) if str(r).strip()]
    resolved_marks = int(max_marks or 5)

    if subject_id:
        with db.connection() as conn:
            row = conn.execute("SELECT questions_json FROM model_papers WHERE subject_id = ?", (subject_id,)).fetchone()
            if row:
                questions = json.loads(row["questions_json"] or "[]")
                for q in questions:
                    if str(q.get("id")) == str(question_id):
                        resolved_prompt = resolved_prompt or str(q.get("question") or q.get("prompt") or "")
                        resolved_model = resolved_model or str(q.get("model_answer") or "")
                        resolved_rubric = resolved_rubric or [str(r) for r in (q.get("rubric") or [])]
                        resolved_marks = int(q.get("marks") or resolved_marks)
                        break

    prompt = (
        "You are a strict university exam grader. Return JSON with keys: "
        "`score` (number <= max_marks), `max_marks` (number), `feedback` (string), "
        "`strengths` (list of strings), `improvements` (list of strings).\n"
        f"Max Marks: {resolved_marks}\n"
        f"Question: {resolved_prompt}\n"
        f"Rubric: {json.dumps(resolved_rubric)}\n"
        f"Reference Model Answer: {resolved_model}\n"
        f"Student Answer: {clean_ans}"
    )

    if not _has_ai_access(db, owner_sub=owner_sub):
        raise AIServiceRequiredError(
            "No AI API key configured. Add a personal API key, set `.env`, or sign in with Cloudflare AI Relay configured.",
            status_code=400,
            error_code="API_KEY_REQUIRED",
        )

    ai_resp = await call_ai_pool(
        db,
        prompt,
        endpoint="exam_grade",
        expect_json=True,
        owner_sub=owner_sub,
        client=client,
    )
    if ai_resp and isinstance(ai_resp.get("data"), dict):
        data = ai_resp["data"]
        try:
            score = max(0.0, min(float(resolved_marks), float(data.get("score", 0.0))))
        except (TypeError, ValueError):
            score = 0.0
        pct = round((score / max(1, resolved_marks)) * 100.0, 1)
        return {
            "ok": True,
            "score": round(score, 1),
            "max_marks": resolved_marks,
            "percentage": pct,
            "feedback": sanitize_plain_text(str(data.get("feedback") or "Graded.")),
            "strengths": [sanitize_plain_text(str(s)) for s in (data.get("strengths") or [])],
            "improvements": [sanitize_plain_text(str(s)) for s in (data.get("improvements") or [])],
            "used_fallback": False,
            "provider": ai_resp.get("provider", "gemini"),
        }

    raise AIServiceRequiredError(
        "Internet connection and a valid online AI API key are required for AI grading.",
        status_code=503,
        error_code="INTERNET_OR_API_UNAVAILABLE",
    )


async def transcribe_media(
    db: LocalDatabase | None,
    *,
    course_title: str = "",
    audio_base64: str = "",
    audio_mime: str = "audio/wav",
    keyframes: list[dict[str, Any]] | None = None,
    source_title: str = "",
    owner_sub: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Transcribe lecture speech, audio track, and visual video keyframes using Gemini Flash in local app."""
    clean_title = (source_title or course_title or "Lecture Video").strip()
    keyframe_list = keyframes or []

    if not _has_ai_access(db, owner_sub=owner_sub):
        fallback_transcripts = []
        for idx, kf in enumerate(keyframe_list[:16]):
            ts = str(kf.get("timestamp") or kf.get("timestamp_sec") or f"{idx * 30}s")
            qkey = str(kf.get("roi_key") or f"q{idx + 1}")
            label = str(kf.get("label") or kf.get("caption") or f"Slide {idx + 1}")
            fallback_transcripts.append(
                {
                    "timestamp": ts,
                    "timestamp_sec": idx * 30,
                    "speaker": f"Lecture Section ({qkey})",
                    "text": f"Visual Slide #{idx + 1}: {label}. Explains core architectural concepts and diagrams for {clean_title}.",
                    "key_point": label,
                }
            )
        if not fallback_transcripts:
            fallback_transcripts = [
                {
                    "timestamp": "00:00",
                    "timestamp_sec": 0,
                    "speaker": "Instructor",
                    "text": f"Introduction and foundational overview of {clean_title}.",
                    "key_point": "Course Foundations",
                }
            ]
        return {
            "ok": True,
            "title": clean_title,
            "used_ai": False,
            "transcripts": fallback_transcripts,
            "full_transcript": "\n".join(f"[{t['timestamp']}] {t['speaker']}: {t['text']}" for t in fallback_transcripts),
        }

    keyframe_digest = "\n".join(
        f"- [Frame {i+1} @ {k.get('timestamp', k.get('timestamp_sec', '0s'))}] ROI: {k.get('roi_key', 'q1')} | Label: {k.get('label', k.get('caption', 'Slide'))}"
        for i, k in enumerate(keyframe_list[:20])
    )

    multimodal_parts: list[dict[str, Any]] = []
    has_audio = bool(audio_base64 and len(audio_base64) > 100)
    if has_audio:
        multimodal_parts.append(
            {
                "inlineData": {
                    "mimeType": audio_mime or "audio/wav",
                    "data": audio_base64,
                }
            }
        )

    prompt = (
        "You are OmniLearn AI's Lecture Transcriber.\n"
        f"Transcribe the lecture audio and visual slides for: '{clean_title}'.\n"
        + ("An audio recording of the lecture is attached. Transcribe all spoken words, explanations, and student/professor interactions.\n" if has_audio else "")
        + (f"Extracted Video Keyframe Slides:\n{keyframe_digest}\n" if keyframe_digest else "")
        + "Return a JSON object with keys:\n"
        "`title`: Title of the lecture.\n"
        "`duration_sec`: Approximate duration in seconds.\n"
        "`transcripts`: Array of objects, each with:\n"
        "  - `timestamp`: (string MM:SS, e.g. '01:24')\n"
        "  - `timestamp_sec`: (integer seconds)\n"
        "  - `speaker`: ('Instructor', 'Slide Presentation', etc.)\n"
        "  - `text`: (clean, verbatim transcript of lecture speech or slide contents)\n"
        "  - `key_point`: (brief 3-6 word concept summary)\n"
        "`full_transcript`: Complete text of the lecture.\n"
    )

    ai_resp = await call_ai_pool(
        db,
        prompt,
        endpoint="transcribe_video",
        expect_json=True,
        owner_sub=owner_sub,
        client=client,
        multimodal_parts=multimodal_parts if has_audio else None,
    )
    if not ai_resp or not isinstance(ai_resp.get("data"), dict):
        raise AIServiceRequiredError(
            "AI transcription service is temporarily unreachable.",
            status_code=503,
            error_code="TRANSCRIPTION_FAILED",
        )

    data = ai_resp["data"]
    raw_transcripts = data.get("transcripts") if isinstance(data.get("transcripts"), list) else []
    cleaned_transcripts = []
    for idx, t in enumerate(raw_transcripts):
        if not isinstance(t, dict):
            continue
        cleaned_transcripts.append(
            {
                "timestamp": str(t.get("timestamp") or f"{idx * 30:02d}:00"),
                "timestamp_sec": int(t.get("timestamp_sec") or idx * 30),
                "speaker": sanitize_plain_text(str(t.get("speaker") or "Instructor")),
                "text": sanitize_plain_text(str(t.get("text") or "")),
                "key_point": sanitize_plain_text(str(t.get("key_point") or "")),
            }
        )

    full_text = str(data.get("full_transcript") or "\n".join(f"[{t['timestamp']}] {t['speaker']}: {t['text']}" for t in cleaned_transcripts))
    return {
        "ok": True,
        "title": clean_title,
        "used_ai": True,
        "transcripts": cleaned_transcripts,
        "full_transcript": full_text,
    }


__all__ = [
    "AIServiceRequiredError",
    "build_subject_scaffold",
    "call_ai_pool",
    "grade_exam_answer",
    "offline_synthesize_subjects",
    "synthesize_course_sources",
    "transcribe_media",
]
