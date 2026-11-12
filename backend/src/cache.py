"""Two-tier edge cache backed by Cloudflare Workers KV (L1) and D1 (L2)."""

from __future__ import annotations

import inspect
import json
import uuid
from typing import Any

try:
    from .crypto_utils import sha256_hex
    from .db import d1_first, d1_run, env_get, maybe_await, to_py_value
except ImportError:
    from crypto_utils import sha256_hex
    from db import d1_first, d1_run, env_get, maybe_await, to_py_value

try:
    import js  # type: ignore
    from pyodide.ffi import to_js  # type: ignore

    HAS_JS_FFI = True
except ImportError:
    js = None  # type: ignore
    to_js = None  # type: ignore
    HAS_JS_FFI = False

DEFAULT_API_CACHE_TTL = 7 * 24 * 3600  # 7 days
DEFAULT_SEARCH_CACHE_TTL = 6 * 3600    # 6 hours


def _get_kv_binding(env: Any) -> Any:
    kv = env_get(env, "APP_CACHE", None)
    if kv is not None:
        return kv
    # Isolated per-env in-memory fallback when APP_CACHE is absent in local unit tests.
    if isinstance(env, dict):
        return env.setdefault("_mem_kv", {})
    if env is not None:
        mem = getattr(env, "_mem_kv", None)
        if mem is None:
            mem = {}
            try:
                setattr(env, "_mem_kv", mem)
            except Exception:
                return None
        return mem
    return None


async def kv_get(env: Any, key: str) -> str | None:
    """Read a string value from `APP_CACHE` KV (or mock dict)."""
    kv = _get_kv_binding(env)
    if kv is None:
        return None
    if isinstance(kv, dict):
        val = kv.get(key)
        return val if isinstance(val, str) else (None if val is None else str(val))
    raw = await maybe_await(kv.get(key))
    py_val = to_py_value(raw)
    if py_val is None:
        return None
    return py_val if isinstance(py_val, str) else str(py_val)


async def kv_put(env: Any, key: str, value: str, expiration_ttl: int | None = None) -> bool:
    """Write a string value to `APP_CACHE` KV with optional TTL (min 60s on Cloudflare)."""
    kv = _get_kv_binding(env)
    if kv is None:
        return False
    if isinstance(kv, dict):
        kv[key] = value
        return True

    if expiration_ttl is not None:
        ttl = max(60, int(expiration_ttl))
        if HAS_JS_FFI and to_js is not None and js is not None:
            opts = to_js({"expirationTtl": ttl}, dict_converter=js.Object.fromEntries)
            await maybe_await(kv.put(key, value, opts))
            return True
        try:
            sig = inspect.signature(kv.put)
            params = list(sig.parameters.values())
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            if accepts_kwargs or "expirationTtl" in sig.parameters:
                await maybe_await(kv.put(key, value, expirationTtl=ttl))
                return True
            if "expiration_ttl" in sig.parameters:
                await maybe_await(kv.put(key, value, expiration_ttl=ttl))
                return True
            if len(params) >= 3:
                await maybe_await(kv.put(key, value, {"expirationTtl": ttl}))
                return True
        except (TypeError, ValueError):
            pass

    await maybe_await(kv.put(key, value))
    return True


async def kv_delete(env: Any, key: str) -> bool:
    """Delete a key from `APP_CACHE` KV."""
    kv = _get_kv_binding(env)
    if kv is None:
        return False
    if isinstance(kv, dict):
        kv.pop(key, None)
        return True
    if hasattr(kv, "delete"):
        await maybe_await(kv.delete(key))
        return True
    return False


async def make_cache_hash(*parts: Any) -> str:
    """Build deterministic SHA-256 fingerprint across cache key components."""
    serialized = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return await sha256_hex(serialized)


async def cache_get(env: Any, cache_key: str, endpoint: str | None = None) -> Any | None:
    """Lookup cached response in L1 KV (`APP_CACHE`), warming from L2 D1 (`api_cache`)."""
    kv_key = f"api:{cache_key}"
    raw_kv = await kv_get(env, kv_key)
    if raw_kv:
        try:
            return json.loads(raw_kv)
        except Exception:
            pass

    if env_get(env, "DB", None) is None:
        return None

    if endpoint:
        row = await d1_first(
            env,
            "SELECT response_json FROM api_cache WHERE prompt_hash = ? AND endpoint = ?",
            (cache_key, endpoint),
        )
    else:
        row = await d1_first(
            env,
            "SELECT response_json FROM api_cache WHERE prompt_hash = ?",
            (cache_key,),
        )
    if not row or not row.get("response_json"):
        return None

    raw_d1 = str(row["response_json"])
    try:
        parsed = json.loads(raw_d1)
    except Exception:
        return None

    # Warm L1 KV on L2 hit
    await kv_put(env, kv_key, raw_d1, expiration_ttl=DEFAULT_API_CACHE_TTL)
    return parsed


async def cache_set(
    env: Any,
    cache_key: str,
    endpoint: str,
    value: Any,
    ttl_seconds: int = DEFAULT_API_CACHE_TTL,
    tokens_saved: int = 0,
) -> None:
    """Persist JSON response into both L1 KV (`APP_CACHE`) and L2 D1 (`api_cache`)."""
    serialized = json.dumps(value, ensure_ascii=False)
    await kv_put(env, f"api:{cache_key}", serialized, expiration_ttl=ttl_seconds)

    if env_get(env, "DB", None) is not None:
        await d1_run(
            env,
            """
            INSERT OR REPLACE INTO api_cache (prompt_hash, endpoint, response_json, tokens_saved)
            VALUES (?, ?, ?, ?)
            """,
            (cache_key, endpoint, serialized, int(tokens_saved)),
        )


async def search_cache_get(
    env: Any,
    query: str,
    provider_filter: str = "all",
) -> list[dict[str, Any]] | None:
    """Lookup search query results in L1 KV (`srch:<hash>`) and L2 D1 (`source_search_history`)."""
    norm_q = (query or "").strip().lower()
    norm_filter = (provider_filter or "all").strip().lower()
    if not norm_q:
        return None

    q_hash = await make_cache_hash("search", norm_q, norm_filter)
    raw_kv = await kv_get(env, f"srch:{q_hash}")
    if raw_kv:
        try:
            parsed = json.loads(raw_kv)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

    if env_get(env, "DB", None) is None:
        return None

    row = await d1_first(
        env,
        "SELECT results_json FROM source_search_history WHERE query_hash = ?",
        (q_hash,),
    )
    if not row or not row.get("results_json"):
        return None

    raw_d1 = str(row["results_json"])
    try:
        parsed = json.loads(raw_d1)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None

    await kv_put(env, f"srch:{q_hash}", raw_d1, expiration_ttl=DEFAULT_SEARCH_CACHE_TTL)
    return parsed


async def search_cache_set(
    env: Any,
    query: str,
    provider_filter: str,
    results: list[dict[str, Any]],
) -> None:
    """Store search query results in L1 KV (`srch:<hash>`) and L2 D1 (`source_search_history`)."""
    norm_q = (query or "").strip().lower()
    norm_filter = (provider_filter or "all").strip().lower()
    if not norm_q:
        return

    q_hash = await make_cache_hash("search", norm_q, norm_filter)
    serialized = json.dumps(results, ensure_ascii=False)
    await kv_put(env, f"srch:{q_hash}", serialized, expiration_ttl=DEFAULT_SEARCH_CACHE_TTL)

    if env_get(env, "DB", None) is not None:
        await d1_run(
            env,
            """
            INSERT OR REPLACE INTO source_search_history
                (id, query, provider_filter, query_hash, results_json, added_source_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), query.strip(), norm_filter, q_hash, serialized, 0),
        )
