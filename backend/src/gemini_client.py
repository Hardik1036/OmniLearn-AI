"""Async Gemini 2.0 Flash + Groq AI client, OpenAlex Lite search, safe TXT/CSV parser & offline fallback."""

from __future__ import annotations

import asyncio
import csv
import html
import io
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote_plus, urlparse

try:
    from .api_pool import get_candidate_keys, mark_key_cooldown, mark_key_used
    from .cache import cache_get, cache_set, make_cache_hash, search_cache_get, search_cache_set
    from .crypto_utils import sha256_hex_sync
    from .edition_guard import (
        MAX_WEB_LITE_EXAM_QUESTIONS,
        MAX_WEB_LITE_FILE_BYTES,
        MAX_WEB_LITE_NOTES_CONCEPTS,
        MAX_WEB_LITE_QUIZ_QUESTIONS,
        MAX_WEB_LITE_SEARCH_HITS,
        MAX_WEB_LITE_SLIDES,
        is_web_lite,
    )
except ImportError:
    from api_pool import get_candidate_keys, mark_key_cooldown, mark_key_used
    from cache import cache_get, cache_set, make_cache_hash, search_cache_get, search_cache_set
    from crypto_utils import sha256_hex_sync
    from edition_guard import (
        MAX_WEB_LITE_EXAM_QUESTIONS,
        MAX_WEB_LITE_FILE_BYTES,
        MAX_WEB_LITE_NOTES_CONCEPTS,
        MAX_WEB_LITE_QUIZ_QUESTIONS,
        MAX_WEB_LITE_SEARCH_HITS,
        MAX_WEB_LITE_SLIDES,
        is_web_lite,
    )


class AIServiceRequiredError(RuntimeError):
    """Raised when online AI synthesis/grading cannot run due to missing key or network outage."""

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

try:
    import js  # type: ignore
    from pyodide.ffi import to_js  # type: ignore

    HAS_JS_FETCH = hasattr(js, "fetch")
except ImportError:
    js = None  # type: ignore
    to_js = None  # type: ignore
    HAS_JS_FETCH = False

GEMINI_MODEL_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.8-flash:generateContent?key={api_key}"
)
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal"})


def sanitize_csv_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection (`=`, `+`, `-`, `@`, tab, CR)."""
    text = str(value or "")
    if text and text[0] in _FORMULA_PREFIXES:
        return "'" + text
    return text


def strip_unsafe_html(text: str) -> str:
    """Remove HTML tags and script/iframe/event-handler markup from untrusted text."""
    cleaned = re.sub(r"(?is)<(script|style|iframe|object|embed)[^>]*>.*?</\1>", " ", text or "")
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _is_disallowed_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def validate_public_http_url(url: str) -> str:
    """Strict SSRF guard allowing only public `http://` and `https://` URLs."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("URL must not be empty")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Blocked URL scheme `{scheme}`; only http/https allowed")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("URL is missing a valid hostname")
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith((".local", ".internal", ".localhost")):
        raise ValueError(f"Blocked private/internal hostname: {hostname}")

    # Direct IP literal check
    try:
        ip_literal = ipaddress.ip_address(hostname.strip("[]"))
        if _is_disallowed_ip(ip_literal):
            raise ValueError(f"Blocked non-public IP address: {hostname}")
        return raw
    except ValueError as exc:
        if "Blocked non-public IP" in str(exc):
            raise

    # DNS resolution check in CPython when socket is available
    if not HAS_JS_FETCH:
        try:
            infos = socket.getaddrinfo(hostname, parsed.port or (443 if scheme == "https" else 80), proto=socket.IPPROTO_TCP)
            for info in infos:
                addr_str = info[4][0].split("%")[0]
                resolved_ip = ipaddress.ip_address(addr_str)
                if _is_disallowed_ip(resolved_ip):
                    raise ValueError(f"Blocked DNS resolution to private IP: {addr_str}")
        except socket.gaierror:
            # Allow unresolved hostnames to fail naturally during HTTP fetch rather than bypassing IP guard
            pass
    return raw


async def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, str]:
    """Perform an async HTTP request using `js.fetch()` in Workers or `httpx` in CPython."""
    req_headers = dict(headers or {})
    body_text: str | None = None
    if json_body is not None:
        body_text = json.dumps(json_body, ensure_ascii=False)
        req_headers.setdefault("Content-Type", "application/json")

    if HAS_JS_FETCH and js is not None and to_js is not None:
        init: dict[str, Any] = {"method": method.upper()}
        if req_headers:
            if hasattr(js, "Headers"):
                h = js.Headers.new()
                for k, v in req_headers.items():
                    h.set(str(k), str(v))
                init["headers"] = h
            else:
                init["headers"] = req_headers
        if body_text is not None:
            init["body"] = body_text
        resp = await js.fetch(url, to_js(init, dict_converter=js.Object.fromEntries))
        status = int(resp.status)
        text = str(await resp.text())
        return status, text

    try:
        import httpx  # type: ignore

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers=req_headers,
                content=body_text.encode("utf-8") if body_text is not None else None,
            )
            return int(resp.status_code), resp.text
    except ImportError:
        def _sync_urllib() -> tuple[int, str]:
            data = body_text.encode("utf-8") if body_text is not None else None
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return int(r.status), r.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as err:
                return int(err.code), err.read().decode("utf-8", errors="replace")

        return await asyncio.to_thread(_sync_urllib)


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


async def _invoke_provider(
    provider: str,
    api_key: str,
    prompt: str,
    expect_json: bool = True,
    multimodal_parts: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    if provider == "gemini":
        gemini_models = ("gemini-3.8-flash", "gemini-2.5-flash", "gemini-1.5-flash")
        last_status = 503
        last_text = "Gemini service unavailable"
        for model in gemini_models:
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
            status, text = await http_request(url, method="POST", json_body=body, timeout=30.0)
            if status == 200:
                try:
                    payload = json.loads(text)
                    res_parts = payload["candidates"][0]["content"]["parts"]
                    return 200, str(res_parts[0].get("text", ""))
                except Exception:
                    return 502, text
            last_status, last_text = status, text
            if status not in (404, 503):
                break
        return last_status, last_text

    if provider in ("groq", "openrouter"):
        url = GROQ_CHAT_URL if provider == "groq" else OPENROUTER_CHAT_URL
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OmniLearn-AI/1.0",
        }
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://omnilearn.ai"
            headers["X-Title"] = "OmniLearn AI"
            models_to_try = ("openrouter/auto", "meta-llama/llama-3.3-70b-instruct")
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
            status, text = await http_request(url, method="POST", headers=headers, json_body=body, timeout=30.0)
            if status == 200:
                try:
                    payload = json.loads(text)
                    content = payload["choices"][0]["message"]["content"]
                    return 200, str(content)
                except Exception:
                    return 502, text
            last_status, last_text = status, text
            if status not in (404, 503):
                break
        return last_status, last_text

    return 400, "Unsupported provider"


async def call_ai_with_pool(
    env: Any,
    prompt: str,
    expect_json: bool = True,
    cache_endpoint: str = "ai_synthesize",
    owner_sub: str | None = None,
    multimodal_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Invoke Gemini 2.0 Flash / Groq with 2-tier caching and 65s 429 cooldown failover."""
    cache_seed = prompt
    if multimodal_parts:
        cache_seed += ":" + str(len(multimodal_parts))
    prompt_hash = await make_cache_hash(cache_endpoint, cache_seed)
    cached = await cache_get(env, prompt_hash, endpoint=cache_endpoint)
    if isinstance(cached, dict):
        return {"ok": True, "cached": True, "data": cached}

    candidates = await get_candidate_keys(env, owner_sub=owner_sub)
    if not candidates:
        return None

    for candidate in candidates:
        key_id = str(candidate["id"])
        provider = str(candidate["provider"])
        api_key = str(candidate["api_key"])
        try:
            status, text = await _invoke_provider(
                provider,
                api_key,
                prompt,
                expect_json=expect_json,
                multimodal_parts=multimodal_parts,
            )
        except Exception:
            await mark_key_cooldown(env, key_id, cooldown_seconds=65)
            continue

        if status == 200:
            await mark_key_used(env, key_id)
            if expect_json:
                parsed = _extract_json_object(text)
                if parsed is None:
                    continue
                await cache_set(env, prompt_hash, cache_endpoint, parsed, tokens_saved=max(100, len(text) // 4))
                return {"ok": True, "cached": False, "provider": provider, "data": parsed}
            result_obj = {"text": text}
            await cache_set(env, prompt_hash, cache_endpoint, result_obj, tokens_saved=max(50, len(text) // 4))
            return {"ok": True, "cached": False, "provider": provider, "data": result_obj}

        if status in (429, 503):
            await mark_key_cooldown(env, key_id, cooldown_seconds=65)
            continue

        if status in (401, 403):
            await mark_key_cooldown(env, key_id, is_invalid=True)
            continue

        await mark_key_cooldown(env, key_id, cooldown_seconds=30)

    return None


def _reconstruct_openalex_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""
    positions: dict[int, str] = {}
    for word, indices in inverted_index.items():
        if not isinstance(indices, list):
            continue
        for pos in indices:
            if isinstance(pos, int) and pos < 120:
                positions[pos] = str(word)
    if not positions:
        return ""
    words = [positions[idx] for idx in sorted(positions.keys())]
    text = " ".join(words)
    return text[:400] + ("..." if len(text) > 400 else "")


async def search_openalex_lite(
    env: Any,
    query: str,
    limit: int = MAX_WEB_LITE_SEARCH_HITS,
) -> dict[str, Any]:
    """Query OpenAlex abstracts (strictly `<= 3` hits in Web Lite) with 2-tier search cache."""
    clean_query = (query or "").strip()
    if not clean_query:
        return {"ok": True, "query": "", "results": [], "edition": "web_lite" if is_web_lite(env) else "system_app"}

    effective_limit = min(max(1, int(limit)), MAX_WEB_LITE_SEARCH_HITS) if is_web_lite(env) else min(max(1, int(limit)), 10)
    cached = await search_cache_get(env, clean_query, "academic")
    if cached is not None:
        return {
            "ok": True,
            "query": clean_query,
            "cached": True,
            "edition": "web_lite" if is_web_lite(env) else "system_app",
            "results": cached[:effective_limit],
        }

    url = (
        f"{OPENALEX_WORKS_URL}?search={quote_plus(clean_query)}"
        f"&per-page={effective_limit}"
        "&select=id,title,publication_year,doi,open_access,abstract_inverted_index,authorships"
    )
    results: list[dict[str, Any]] = []
    offline = False
    try:
        status, text = await http_request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "OmniLearn-Worker/1.0"},
            timeout=10.0,
        )
        if status == 200:
            payload = json.loads(text)
            for work in (payload.get("results") or [])[:effective_limit]:
                if not isinstance(work, dict):
                    continue
                oa = work.get("open_access") or {}
                oa_url = oa.get("oa_url") or ""
                doi = work.get("doi") or ""
                work_id = str(work.get("id") or "")
                authors = []
                for auth in (work.get("authorships") or [])[:3]:
                    name = ((auth or {}).get("author") or {}).get("display_name")
                    if name:
                        authors.append(str(name))
                abstract = strip_unsafe_html(_reconstruct_openalex_abstract(work.get("abstract_inverted_index")))
                is_pdf = bool(oa_url and str(oa_url).lower().endswith(".pdf"))
                results.append(
                    {
                        "id": work_id,
                        "title": strip_unsafe_html(str(work.get("title") or "Untitled Academic Paper")),
                        "url": str(oa_url or doi or work_id),
                        "source_provider": "openalex",
                        "is_direct_pdf": is_pdf,
                        "can_inspect_pdfs": False,
                        "snippet": abstract or "OpenAlex open-access metadata record.",
                        "year": work.get("publication_year"),
                        "authors": authors,
                    }
                )
    except Exception:
        offline = True

    if results:
        await search_cache_set(env, clean_query, "academic", results)

    return {
        "ok": True,
        "query": clean_query,
        "cached": False,
        "offline": offline,
        "edition": "web_lite" if is_web_lite(env) else "system_app",
        "results": results[:effective_limit],
    }


def parse_safe_text_or_csv(
    filename: str,
    content_bytes: bytes,
    max_bytes: int = MAX_WEB_LITE_FILE_BYTES,
) -> dict[str, Any]:
    """Parse small `.txt`/`.md`/`.csv` files safely within Web Lite size & formula rules."""
    if not isinstance(content_bytes, (bytes, bytearray)):
        raise ValueError("File content must be bytes")
    raw = bytes(content_bytes)
    if len(raw) == 0:
        raise ValueError("Uploaded file is empty")
    if len(raw) > max_bytes:
        raise ValueError(f"File exceeds {max_bytes} byte Web Lite limit ({len(raw)} bytes)")

    clean_name = (filename or "notes.txt").strip()
    lower = clean_name.lower()
    decoded = raw.decode("utf-8", errors="replace").replace("\x00", "")
    file_hash = sha256_hex_sync(raw)

    if lower.endswith(".csv"):
        reader = csv.reader(io.StringIO(decoded))
        rows: list[list[str]] = []
        for idx, row in enumerate(reader):
            if idx >= 200:
                break
            rows.append([sanitize_csv_cell(cell.strip()) for cell in row[:30]])
        headers = rows[0] if rows else []
        lines = []
        if headers:
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows[1:51]:
            lines.append("| " + " | ".join(row) + " |")
        excerpt = "\n".join(lines) if lines else decoded[:4000]
        chunks = [{"index": 1, "label": f"CSV Table ({len(rows)} rows)", "text": excerpt[:4000]}]
        return {
            "source_type": "csv",
            "title": clean_name,
            "file_hash": file_hash,
            "raw_text_excerpt": excerpt[:8000],
            "structured_chunks": chunks,
            "metadata": {"row_count": len(rows), "columns": headers, "size_bytes": len(raw)},
        }

    # .txt or .md
    safe_text = re.sub(r"(?is)<(script|iframe)[^>]*>.*?</\1>", "", decoded)
    safe_text = re.sub(r"(?i)\son[a-z]+\s*=", " data-blocked-handler=", safe_text)
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", safe_text) if p.strip()]
    chunks = [
        {"index": i + 1, "label": f"Section {i + 1}", "text": p[:2000]}
        for i, p in enumerate(paragraphs[:12])
    ]
    if not chunks and safe_text.strip():
        chunks = [{"index": 1, "label": "Section 1", "text": safe_text.strip()[:2000]}]
    return {
        "source_type": "txt",
        "title": clean_name,
        "file_hash": file_hash,
        "raw_text_excerpt": safe_text[:8000],
        "structured_chunks": chunks,
        "metadata": {"char_count": len(safe_text), "size_bytes": len(raw)},
    }


async def fetch_public_webpage_preview(url: str) -> dict[str, Any]:
    """Fetch a single public webpage preview safely (SSRF-guarded, stripped of scripts/tags)."""
    safe_url = validate_public_http_url(url)
    status, html_text = await http_request(
        safe_url,
        method="GET",
        headers={"User-Agent": "OmniLearn-WebLite/1.0", "Accept": "text/html,text/plain"},
        timeout=10.0,
    )
    if status >= 400:
        raise ValueError(f"Upstream webpage returned HTTP {status}")

    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text)
    page_title = strip_unsafe_html(title_match.group(1)) if title_match else urlparse(safe_url).netloc
    clean_text = strip_unsafe_html(html_text)[:8000]
    file_hash = sha256_hex_sync(f"{safe_url}:{clean_text[:1000]}".encode("utf-8"))
    return {
        "source_type": "web_url",
        "title": page_title or safe_url,
        "origin_url": safe_url,
        "file_hash": file_hash,
        "raw_text_excerpt": clean_text,
        "structured_chunks": [{"index": 1, "label": page_title or "Web Preview", "text": clean_text[:3000]}],
        "metadata": {"http_status": status, "preview_chars": len(clean_text)},
    }


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
        # Quote node labels with unquoted parentheses, e.g. A[Label (Extra)] -> A["Label (Extra)"]
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


def _offline_fallback_subject(
    course_title: str,
    sources: list[dict[str, Any]],
    slide_urls: list[str] | None = None,
    target_days: int = 14,
    daily_hours: float = 3.0,
    web_lite: bool = True,
) -> dict[str, Any]:
    """Deterministic structured subject synthesis when zero AI keys exist or APIs fail."""
    clean_title = (course_title or "Unified Study Module").strip()
    slides = list(slide_urls or [])
    if web_lite:
        slides = slides[:MAX_WEB_LITE_SLIDES]

    source_titles = [str(s.get("title") or s.get("source_type") or "Source") for s in sources]
    snippets = [str(s.get("raw_text_excerpt") or "").strip() for s in sources if s.get("raw_text_excerpt")]
    combined_preview = " ".join(snippets)[:600]
    summary_text = (
        f"Comprehensive academic synthesis for {clean_title}.\n\n"
        f"1. Foundations & Motivation: Integrates {len(sources)} core source(s) ({', '.join(source_titles) if source_titles else 'lecture materials'}). "
        f"{combined_preview if combined_preview else 'Foundational theoretical concepts, invariant properties, and architectural principles are derived across all staged materials.'}\n\n"
        f"2. Mechanics & Invariant Properties: Systematic cross-referencing between formal definitions, lecture slides, and derivations provides dual-coding reinforcement.\n\n"
        f"3. Practical Applications & Complexity Bounds: Structured checkpoints optimize retention through spaced recall and applied analytical practice."
    ).strip()

    citations = [
        {
            "source_id": str(s.get("id") or f"src-{idx+1}"),
            "title": str(s.get("title") or f"Source {idx+1}"),
            "location": str(s.get("source_type") or "txt").upper(),
        }
        for idx, s in enumerate(sources)
    ]

    def _slide_ref(idx: int) -> tuple[str, str, str]:
        if not slides:
            return "", "", ""
        s = slides[idx % len(slides)]
        if isinstance(s, dict):
            return s.get("url") or "", s.get("caption") or f"Lecture Visual #{idx + 1}", s.get("timestamp") or "00:00"
        return str(s), f"Lecture Visual #{idx + 1}", "00:00"

    s1_url, s1_cap, s1_ts = _slide_ref(0)
    s2_url, s2_cap, s2_ts = _slide_ref(1)
    s3_url, s3_cap, s3_ts = _slide_ref(2)

    concepts = [
        {
            "title": f"Core Architectural Framework of {clean_title}",
            "explanation": combined_preview[:240] or f"Foundational structural mechanisms and operational constraints of {clean_title}.",
            "citation": source_titles[0] if source_titles else "Course Foundations",
            "slide_url": s1_url,
            "slide_caption": s1_cap,
            "slide_timestamp": s1_ts,
        },
        {
            "title": "State Invariants & Algorithmic Pathways",
            "explanation": "Guarantees safe state transitions, exception handling, and edge-case behavior under varying execution parameters.",
            "citation": source_titles[-1] if source_titles else "Lecture Architecture",
            "slide_url": s2_url,
            "slide_caption": s2_cap,
            "slide_timestamp": s2_ts,
        },
        {
            "title": "Tradeoff & Performance Complexity Bounds",
            "explanation": "Rigorous evaluation of time, space, and throughput bounds across candidate strategies and resource constraints.",
            "citation": source_titles[0] if source_titles else "Analytical Review",
            "slide_url": s3_url,
            "slide_caption": s3_cap,
            "slide_timestamp": s3_ts,
        },
    ]

    formulas = [
        {
            "name": "Mastery Progress Ratio",
            "latex": r"M = \frac{\text{Correct Active Recall Attempts}}{\text{Total Verification Checkpoints}} \times 100\%",
            "note": "Quantifies cumulative retrieval fidelity across spaced study iterations",
        },
        {
            "name": "Spaced Retention Schedule",
            "latex": r"I_{n} = I_{n-1} \cdot \mathrm{EF},\quad \text{where}\;\mathrm{EF} \ge 1.3",
            "note": "SM-2 optimal memory consolidation multiplier",
        },
    ]

    mcqs = [
        {
            "id": "q1",
            "question": f"Which practice best reinforces long-term mastery of {clean_title}?",
            "options": [
                "Active recall with spaced repetition across source citations",
                "Passive single-pass reading without self-testing",
                "Ignoring edge cases and worked examples",
                "Memorizing isolated keywords without context",
            ],
            "correct_index": 0,
            "explanation": "Active recall paired with spaced repetition builds durable retrieval paths.",
            "citation": source_titles[0] if source_titles else "Study Guide",
        },
        {
            "id": "q2",
            "question": "Why should heterogeneous sources (slides, notes, tables) be cross-referenced?",
            "options": [
                "Visuals and formal definitions reinforce distinct memory pathways",
                "It eliminates the need to verify assumptions",
                "It removes all formulas from the curriculum",
                "It prevents diagram synthesis",
            ],
            "correct_index": 0,
            "explanation": "Combining visual and textual sources strengthens conceptual transfer.",
            "citation": source_titles[0] if source_titles else "Study Guide",
        },
        {
            "id": "q3",
            "question": "When reviewing a missed quiz item, what is the highest-leverage next step?",
            "options": [
                "Inspect the cited source excerpt and re-derive the solution",
                "Skip directly to unrelated chapters",
                "Hide all explanations",
                "Reset study progress",
            ],
            "correct_index": 0,
            "explanation": "Diagnosing the exact misconception in the source material closes knowledge gaps.",
            "citation": source_titles[-1] if source_titles else "Study Guide",
        },
        {
            "id": "q4",
            "question": "How should exam time be budgeted across multi-section papers?",
            "options": [
                "Proportional to section mark weights with a final review buffer",
                "Spend 90% of time on a single low-mark item",
                "Skip structured steps in derivations",
                "Submit without checking units or edge conditions",
            ],
            "correct_index": 0,
            "explanation": "Allocating minutes per mark prevents unfinished high-value sections.",
            "citation": "Exam Strategy",
        },
    ]

    exam_questions = [
        {
            "id": "ex1",
            "section": "Section A",
            "marks": 5,
            "question": f"Summarize the central problem solved in {clean_title} and identify one key constraint.",
            "model_answer": summary_text,
            "rubric": ["Identifies core objective (2 marks)", "Explains key constraint or tradeoff (3 marks)"],
        },
        {
            "id": "ex2",
            "section": "Section B",
            "marks": 10,
            "question": f"Compare two design approaches covered in {clean_title} with an explicit complexity or accuracy comparison.",
            "model_answer": "Contrast primary assumptions, worst-case bounds, and practical applicability.",
            "rubric": ["Accurate comparison criteria (5 marks)", "Clear worked example (5 marks)"],
        },
        {
            "id": "ex3",
            "section": "Section C",
            "marks": 10,
            "question": f"Design an end-to-end solution applying concepts from {clean_title} to a novel scenario.",
            "model_answer": "Specify inputs, algorithmic pipeline, verification metric, and failure mitigation.",
            "rubric": ["Architecture & steps (6 marks)", "Edge-case mitigation (4 marks)"],
        },
    ]

    days = max(1, min(30, int(target_days or 14)))
    subj_slug = sha256_hex_sync(clean_title)[:6]
    schedule = [
        {
            "day": d,
            "id": f"{subj_slug}-day-{d}",
            "title": f"Day {d}: {'Core Concepts' if d <= days // 2 else 'Practice & Mock Exam'} ({clean_title})",
            "phase": "Foundations" if d <= max(1, days // 3) else ("Practice & Drills" if d <= (2 * days) // 3 else "Timed Mock & Revision"),
            "focus": clean_title,
            "estimated_hours": float(daily_hours or 3.0),
            "tasks": [
                {
                    "id": f"{subj_slug}-d{d}-t1",
                    "text": f"Review visual notes & formulas (Day {d})",
                    "duration_min": 60,
                },
                {
                    "id": f"{subj_slug}-d{d}-t2",
                    "text": f"Complete active recall checkpoint #{d}",
                    "duration_min": 60,
                },
            ],
        }
        for d in range(1, days + 1)
    ]

    if web_lite:
        concepts = concepts[:MAX_WEB_LITE_NOTES_CONCEPTS]
        mcqs = mcqs[:MAX_WEB_LITE_QUIZ_QUESTIONS]
        exam_questions = exam_questions[:MAX_WEB_LITE_EXAM_QUESTIONS]

    safe_node = re.sub(r"[^A-Za-z0-9 ]", "", clean_title)[:32] or "Course"
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

    return {
        "id": f"subj-{sha256_hex_sync(clean_title)[:10]}",
        "quadrant_key": "q1",
        "title": clean_title,
        "short_title": clean_title[:24],
        "topic": clean_title,
        "unit": "Module 1",
        "instructor": "OmniLearn AI",
        "badge_color": "#3B82F6",
        "summary": summary_text,
        "mermaid_diagram": mermaid,
        "slide_crops": slides,
        "source_citations": citations,
        "key_concepts": concepts,
        "math_formulas": formulas,
        "study_plan": schedule,
        "quiz_questions": mcqs,
        "model_paper": {
            "title": f"{clean_title} — Model Paper",
            "time_allowed": "20 Minutes" if web_lite else "60 Minutes",
            "max_marks": 5 if web_lite else 25,
            "questions": exam_questions,
        },
    }


async def synthesize_course(
    env: Any,
    course_title: str,
    sources: list[dict[str, Any]],
    slide_urls: list[str] | None = None,
    transcripts: list[str] | None = None,
    target_days: int = 14,
    daily_hours: float = 3.0,
    target_subject_name: str = "",
    owner_sub: str | None = None,
) -> dict[str, Any]:
    """Synthesize unified study notes, plan, quiz & model paper online via Multi-API pool."""
    keys = await get_candidate_keys(env, owner_sub=owner_sub)
    if not keys:
        raise AIServiceRequiredError(
            "No AI API key configured. Add an API key in Sign-In or Multi-API Pool (internet required).",
            status_code=400,
            error_code="API_KEY_REQUIRED",
        )

    web_lite = is_web_lite(env)
    resolved_title = strip_unsafe_html(target_subject_name).strip() or course_title
    scaffold = _offline_fallback_subject(
        course_title=resolved_title,
        sources=sources,
        slide_urls=slide_urls,
        target_days=target_days,
        daily_hours=daily_hours,
        web_lite=web_lite,
    )

    source_summary = "\n".join(
        f"- [{s.get('source_type', 'txt')}] {s.get('title', 'Untitled')}: {str(s.get('raw_text_excerpt') or '')[:1200]}"
        for s in sources[:8]
    )
    transcript_summary = "\n".join(str(t)[:600] for t in (transcripts or [])[:8])

    slide_lines = []
    normalized_slides: list[dict[str, str]] = []
    for idx, s in enumerate(slide_urls or []):
        if isinstance(s, dict):
            s_url = s.get("url") or s.get("path") or ""
            s_cap = s.get("caption") or s.get("label") or f"Visual Slide #{idx + 1}"
            s_ts = s.get("timestamp") or ""
            normalized_slides.append({"url": s_url, "caption": s_cap, "timestamp": s_ts})
            slide_lines.append(f"- URL: {s_url} | Caption: {s_cap} | Timestamp: {s_ts}")
        elif isinstance(s, str) and s.strip():
            s_url = s.strip()
            s_cap = f"Visual Lecture Frame #{idx + 1}"
            normalized_slides.append({"url": s_url, "caption": s_cap, "timestamp": ""})
            slide_lines.append(f"- URL: {s_url} | Caption: {s_cap}")
    slide_summary = "\nAvailable Visual Video Slides & Keyframes:\n" + "\n".join(slide_lines[:24]) if slide_lines else ""

    prompt = (
        "You are OmniLearn AI, an elite university tutor and exam synthesis engine.\n"
        "Synthesize a deeply technical, academic JSON study pack for this subject with exact keys:\n"
        "`title`: Concise subject title.\n"
        "`topic`: Subject topic.\n"
        "`summary`: Rigorous 3-paragraph executive summary detailing (1) Theoretical motivation & foundational concepts, "
        "(2) Core mechanisms, invariant properties, and state transitions, and (3) Practical tradeoffs, complexity bounds, and real-world implementations.\n"
        "`mermaid_diagram`: A comprehensive, multi-stage architecture flowchart (`flowchart TD`) with 8 to 14 distinct nodes "
        "organized into `subgraph` sections (e.g. Ingestion, Processing, Architecture, Verification). "
        "Every node label containing parentheses or quotes MUST be strictly enclosed in double quotes (e.g. A[\"Label (with info)\"]). "
        "Include condition labels on connector edges (e.g. -->|\"Valid\"| B).\n"
        "`key_concepts`: List of 6 to 12 in-depth concepts ({title, explanation, citation, slide_url, slide_caption, slide_timestamp}). "
        "For each concept, attach the most relevant `slide_url` from the Available Visual Video Slides list below so lecture slides illustrate the notes.\n"
        "`math_formulas`: List of 4 to 8 rigorous mathematical expressions ({name, latex, note}) with complete KaTeX equations.\n"
        "`quiz_questions`: List of 5+ high-yield MCQs ({id, question, options, correct_index, explanation, citation}).\n"
        "`exam_questions`: List of Section A/B/C university examination questions ({id, section, marks, question, model_answer, rubric}).\n\n"
        "Multi-Window Video Rule: treat any 4 quadrants of a split video as 4 distinct lectures of THIS single subject.\n"
        f"Course Title: {resolved_title}\n"
        f"Sources:\n{source_summary}\n"
        + (f"Video Transcripts:\n{transcript_summary}\n" if transcript_summary else "")
        + (f"{slide_summary}\n" if slide_summary else "")
    )

    ai_resp = await call_ai_with_pool(
        env,
        prompt,
        expect_json=True,
        cache_endpoint="courses_synthesize",
        owner_sub=owner_sub,
    )
    if not ai_resp or not isinstance(ai_resp.get("data"), dict):
        raise AIServiceRequiredError(
            "Internet connection and a reachable AI API key are required for synthesis.",
            status_code=503,
            error_code="INTERNET_OR_API_UNAVAILABLE",
        )

    ai_data = ai_resp["data"]
    subject = dict(scaffold)
    if isinstance(ai_data.get("title"), str) and ai_data["title"].strip():
        subject["title"] = strip_unsafe_html(ai_data["title"])[:120]
        subject["short_title"] = subject["title"][:24]
    if isinstance(ai_data.get("topic"), str) and ai_data["topic"].strip():
        subject["topic"] = strip_unsafe_html(ai_data["topic"])[:120]
    if isinstance(ai_data.get("summary"), str) and ai_data["summary"].strip():
        subject["summary"] = strip_unsafe_html(ai_data["summary"])[:4000]
    if isinstance(ai_data.get("mermaid_diagram"), str):
        subject["mermaid_diagram"] = sanitize_mermaid(ai_data["mermaid_diagram"])
    if isinstance(ai_data.get("key_concepts"), list) and ai_data["key_concepts"]:
        raw_concepts = ai_data["key_concepts"]
        cleaned_concepts = []
        for idx, c in enumerate(raw_concepts):
            if not isinstance(c, dict):
                continue
            slide_match = normalized_slides[idx % len(normalized_slides)] if normalized_slides else None
            cleaned_concepts.append(
                {
                    "title": strip_unsafe_html(str(c.get("title") or c.get("name") or "Concept")),
                    "explanation": strip_unsafe_html(str(c.get("explanation") or "")),
                    "citation": strip_unsafe_html(str(c.get("citation") or resolved_title)),
                    "slide_url": str(c.get("slide_url") or (slide_match["url"] if slide_match else "")),
                    "slide_caption": str(c.get("slide_caption") or (slide_match["caption"] if slide_match else "")),
                    "slide_timestamp": str(c.get("slide_timestamp") or (slide_match["timestamp"] if slide_match else "")),
                }
            )
        subject["key_concepts"] = cleaned_concepts[:MAX_WEB_LITE_NOTES_CONCEPTS] if web_lite else cleaned_concepts[:12]
    if isinstance(ai_data.get("math_formulas"), list) and ai_data["math_formulas"]:
        subject["math_formulas"] = ai_data["math_formulas"][:8]
    if isinstance(ai_data.get("quiz_questions"), list) and ai_data["quiz_questions"]:
        qs = ai_data["quiz_questions"]
        subject["quiz_questions"] = qs[:MAX_WEB_LITE_QUIZ_QUESTIONS] if web_lite else qs[:25]
    if isinstance(ai_data.get("exam_questions"), list) and ai_data["exam_questions"]:
        exs = ai_data["exam_questions"]
        subject["model_paper"]["questions"] = exs[:MAX_WEB_LITE_EXAM_QUESTIONS] if web_lite else exs[:10]

    return {
        "subject": subject,
        "used_fallback": False,
        "cached": bool(ai_resp.get("cached", False)),
    }


async def transcribe_media(
    env: Any,
    *,
    course_title: str = "",
    audio_base64: str = "",
    audio_mime: str = "audio/wav",
    keyframes: list[dict[str, Any]] | None = None,
    source_title: str = "",
    owner_sub: str | None = None,
) -> dict[str, Any]:
    """Transcribe lecture speech, audio track, and visual video keyframes using Gemini Flash."""
    clean_title = (source_title or course_title or "Lecture Video").strip()
    keyframe_list = keyframes or []

    # Fast offline fallback transcript if zero AI keys configured
    keys = await get_candidate_keys(env, owner_sub=owner_sub)
    if not keys:
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

    ai_resp = await call_ai_with_pool(
        env,
        prompt,
        expect_json=True,
        cache_endpoint="transcribe_video",
        owner_sub=owner_sub,
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
                "speaker": strip_unsafe_html(str(t.get("speaker") or "Instructor")),
                "text": strip_unsafe_html(str(t.get("text") or "")),
                "key_point": strip_unsafe_html(str(t.get("key_point") or "")),
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
