"""Parameterized Cloudflare D1 async helpers with Pyodide & CPython mock support."""

from __future__ import annotations

import inspect
from typing import Any, Sequence


def env_get(env: Any, key: str, default: Any = None) -> Any:
    """Safely read a binding or var from a Worker env object or dict mock."""
    if env is None:
        return default
    if isinstance(env, dict):
        val = env.get(key, default)
        return default if val is None else val
    val = getattr(env, key, default)
    return default if val is None else val


def env_str(env: Any, key: str, default: str = "") -> str:
    """Read a string configuration var safely (ignoring unset MagicMock objects)."""
    val = env_get(env, key, None)
    if isinstance(val, str):
        return val
    return default


async def maybe_await(value: Any) -> Any:
    """Await value if it is awaitable, otherwise return it directly."""
    if inspect.isawaitable(value):
        return await value
    return value


def to_py_value(obj: Any) -> Any:
    """Convert Pyodide JsProxy / JsNull objects into plain Python values."""
    if obj is None:
        return None
    type_name = type(obj).__name__
    if type_name in ("JsNull", "JsUndefined"):
        return None
    if hasattr(obj, "to_py"):
        try:
            converted = obj.to_py()
            return to_py_value(converted)
        except Exception:
            pass
    if isinstance(obj, list):
        return [to_py_value(item) for item in obj]
    if isinstance(obj, tuple):
        return [to_py_value(item) for item in obj]
    if isinstance(obj, dict):
        return {k: to_py_value(v) for k, v in obj.items()}
    return obj


def _sanitize_param(param: Any) -> Any:
    """Normalize Python types for Cloudflare D1 `.bind()` compatibility."""
    if isinstance(param, bool):
        return 1 if param else 0
    return param


def _prepare_stmt(db: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    stmt = db.prepare(sql)
    if params:
        clean_params = [_sanitize_param(p) for p in params]
        stmt = stmt.bind(*clean_params)
    return stmt


def get_db(env: Any) -> Any:
    """Return the Cloudflare D1 `DB` binding or raise RuntimeError if missing."""
    db = env_get(env, "DB", None)
    if db is None:
        raise RuntimeError("D1 database binding `DB` is not configured on env")
    return db


async def d1_all(env: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Execute a parameterized SELECT query and return all rows as dicts."""
    db = get_db(env)
    stmt = _prepare_stmt(db, sql, params)
    raw = await maybe_await(stmt.all())
    res = to_py_value(raw)
    if isinstance(res, dict):
        rows = res.get("results") or []
        return [dict(r) if isinstance(r, dict) else r for r in rows]
    if isinstance(res, list):
        return [dict(r) if isinstance(r, dict) else r for r in res]
    return []


async def d1_first(env: Any, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    """Execute a parameterized query and return the first row dict or None."""
    db = get_db(env)
    stmt = _prepare_stmt(db, sql, params)
    if hasattr(stmt, "first"):
        raw = await maybe_await(stmt.first())
        res = to_py_value(raw)
        if res is None:
            return None
        if isinstance(res, dict):
            # Some mocks return D1-style {"results": [...]} even on first()
            if "results" in res and "id" not in res and isinstance(res["results"], list):
                rows = res["results"]
                return dict(rows[0]) if rows else None
            return dict(res)
        return None
    rows = await d1_all(env, sql, params)
    return rows[0] if rows else None


async def d1_run(env: Any, sql: str, params: Sequence[Any] = ()) -> dict[str, Any]:
    """Execute a parameterized INSERT/UPDATE/DELETE statement on D1."""
    db = get_db(env)
    stmt = _prepare_stmt(db, sql, params)
    raw = await maybe_await(stmt.run())
    res = to_py_value(raw)
    if isinstance(res, dict):
        return {
            "success": bool(res.get("success", True)),
            "meta": dict(res.get("meta") or {}),
        }
    return {"success": True, "meta": {}}


async def d1_batch(
    env: Any,
    statements: Sequence[tuple[str, Sequence[Any]]],
) -> list[dict[str, Any]]:
    """Execute multiple parameterized statements as a D1 batch when supported."""
    if not statements:
        return []
    db = get_db(env)
    if hasattr(db, "batch"):
        prepared = [_prepare_stmt(db, sql, params) for sql, params in statements]
        raw = await maybe_await(db.batch(prepared))
        converted = to_py_value(raw)
        if isinstance(converted, list):
            return [dict(item) if isinstance(item, dict) else {"success": True} for item in converted]
    results = []
    for sql, params in statements:
        results.append(await d1_run(env, sql, params))
    return results
