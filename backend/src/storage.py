"""Cloudflare R2 `SLIDES_BUCKET` storage helpers with strict key & JPEG validation."""

from __future__ import annotations

import inspect
import re
from typing import Any
from urllib.parse import unquote

try:
    from .db import env_get, maybe_await
except ImportError:
    from db import env_get, maybe_await

try:
    import js  # type: ignore
    from pyodide.ffi import to_js  # type: ignore

    HAS_JS_FFI = True
except ImportError:
    js = None  # type: ignore
    to_js = None  # type: ignore
    HAS_JS_FFI = False

MAX_SLIDE_BYTES = int(2.5 * 1024 * 1024)  # 2.5 MiB (2,621,440 bytes)
JPEG_MAGIC = b"\xff\xd8\xff"

_SAFE_KEY_RE = re.compile(r"^courses/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9._-]{1,128})$")


def sanitize_r2_key(key: str) -> str:
    """Validate and normalize an R2 slide key strictly to `courses/<id>/<file>`."""
    if not isinstance(key, str):
        raise ValueError("R2 object key must be a string")

    raw = key.strip()
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("Invalid characters in R2 object key")

    # Decode percent-encoding once and twice to defeat %2e%2e / double-encoded traversal.
    decoded_once = unquote(raw)
    decoded_twice = unquote(decoded_once)
    for candidate in (raw, decoded_once, decoded_twice):
        if ".." in candidate or "\\" in candidate or "\x00" in candidate:
            raise ValueError("Path traversal (`..`) is prohibited in R2 object keys")
        if candidate.startswith("/"):
            raise ValueError("Leading `/` is prohibited in R2 object keys")

    match = _SAFE_KEY_RE.fullmatch(decoded_once)
    if not match or raw != decoded_once:
        raise ValueError("R2 object key must match `courses/<course_id>/<filename>`")

    _, filename = match.groups()
    if filename in (".", "..") or filename.startswith("."):
        raise ValueError("Invalid slide filename in R2 object key")

    return raw


def build_slide_key(course_id: str, filename: str) -> str:
    """Safely construct and validate `courses/<course_id>/<filename>`."""
    clean_course = (course_id or "").strip()
    clean_file = (filename or "").strip()
    return sanitize_r2_key(f"courses/{clean_course}/{clean_file}")


def validate_jpeg_bytes(data: bytes) -> None:
    """Enforce JPEG magic bytes (`FF D8 FF`) and `<= 2.5 MB` size cap."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("Slide payload must be binary bytes")
    size = len(data)
    if size == 0:
        raise ValueError("Slide image cannot be empty")
    if size > MAX_SLIDE_BYTES:
        raise ValueError(f"Slide image exceeds 2.5 MB limit ({size} bytes)")
    if bytes(data[:3]) != JPEG_MAGIC:
        raise ValueError("Invalid slide image format: missing JPEG magic header (FF D8 FF)")


def _get_slides_bucket(env: Any) -> Any:
    bucket = env_get(env, "SLIDES_BUCKET", None)
    if bucket is None:
        raise RuntimeError("R2 binding `SLIDES_BUCKET` is not configured on env")
    return bucket


def _to_uint8_array(data: bytes) -> Any:
    arr = to_js(data)
    if hasattr(arr, "byteLength") and not hasattr(arr, "BYTES_PER_ELEMENT"):
        return js.Uint8Array.new(arr)
    return arr


def _from_array_buffer(buf: Any) -> bytes:
    u8 = js.Uint8Array.new(buf)
    if hasattr(u8, "to_bytes"):
        return u8.to_bytes()
    return bytes(u8.to_py())


async def upload_slide(
    env: Any,
    key: str,
    data: bytes,
    content_type: str = "image/jpeg",
) -> dict[str, Any]:
    """Validate key + JPEG magic + 2.5 MB cap, then store slide into R2 `SLIDES_BUCKET`."""
    clean_key = sanitize_r2_key(key)
    raw = bytes(data)
    validate_jpeg_bytes(raw)
    bucket = _get_slides_bucket(env)

    if isinstance(bucket, dict):
        bucket[clean_key] = {"body": raw, "content_type": content_type}
    elif HAS_JS_FFI and to_js is not None and js is not None:
        opts = to_js(
            {"httpMetadata": {"contentType": content_type}},
            dict_converter=js.Object.fromEntries,
        )
        await maybe_await(bucket.put(clean_key, _to_uint8_array(raw), opts))
    else:
        try:
            sig = inspect.signature(bucket.put)
            params = list(sig.parameters.values())
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            if accepts_kwargs:
                await maybe_await(bucket.put(clean_key, raw, content_type=content_type))
            else:
                await maybe_await(bucket.put(clean_key, raw))
        except (TypeError, ValueError):
            await maybe_await(bucket.put(clean_key, raw))

    return {
        "ok": True,
        "key": clean_key,
        "size_bytes": len(raw),
        "content_type": content_type,
        "url": f"/api/slides/{clean_key}",
    }


async def fetch_slide(env: Any, key: str) -> tuple[bytes, str] | None:
    """Validate key and retrieve `(bytes, content_type)` from R2 `SLIDES_BUCKET`."""
    clean_key = sanitize_r2_key(key)
    bucket = _get_slides_bucket(env)

    if isinstance(bucket, dict):
        entry = bucket.get(clean_key)
        if entry is None:
            return None
        if isinstance(entry, (bytes, bytearray)):
            return bytes(entry), "image/jpeg"
        if isinstance(entry, dict):
            return bytes(entry.get("body", b"")), str(entry.get("content_type", "image/jpeg"))
        return None

    obj = await maybe_await(bucket.get(clean_key))
    if obj is None:
        return None

    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj), "image/jpeg"
    if isinstance(obj, dict):
        body = obj.get("body") or obj.get("data") or b""
        return bytes(body), str(obj.get("content_type", "image/jpeg"))

    content_type = "image/jpeg"
    http_meta = getattr(obj, "httpMetadata", None)
    if http_meta is not None:
        ct = getattr(http_meta, "contentType", None)
        if isinstance(ct, str) and ct:
            content_type = ct

    if hasattr(obj, "arrayBuffer"):
        buf = await maybe_await(obj.arrayBuffer())
        if HAS_JS_FFI and js is not None:
            return _from_array_buffer(buf), content_type
        return bytes(buf), content_type

    if hasattr(obj, "body"):
        body = getattr(obj, "body")
        if isinstance(body, (bytes, bytearray)):
            return bytes(body), content_type

    return None


async def count_course_slides(env: Any, course_id: str) -> int:
    """Count existing slide objects stored in R2 for `courses/<course_id>/`."""
    clean_id = (course_id or "").strip()
    if not clean_id or env_get(env, "SLIDES_BUCKET", None) is None:
        return 0
    bucket = _get_slides_bucket(env)
    prefix = f"courses/{clean_id}/"
    if isinstance(bucket, dict):
        return sum(1 for k in bucket.keys() if isinstance(k, str) and k.startswith(prefix))
    if hasattr(bucket, "list"):
        try:
            if HAS_JS_FFI and to_js is not None and js is not None:
                opts = to_js({"prefix": prefix}, dict_converter=js.Object.fromEntries)
                listed = await maybe_await(bucket.list(opts))
            else:
                listed = await maybe_await(bucket.list(prefix=prefix))
            objs = getattr(listed, "objects", None)
            if objs is None and isinstance(listed, dict):
                objs = listed.get("objects")
            if objs is not None:
                return len(list(objs))
        except Exception:
            return 0
    return 0

