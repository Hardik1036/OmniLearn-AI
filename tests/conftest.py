"""Shared pytest fixtures, Cloudflare D1/KV/R2 mocks, and isolated LocalDatabase helpers."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

os.environ.setdefault("OMNILEARN_IGNORE_DOTENV", "1")

import pytest
from fastapi.testclient import TestClient

from backend.local_app.local_api import create_app
from backend.local_app.local_db import LocalDatabase

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL_PATH = REPO_ROOT / "database" / "schema.sql"
SEED_SQL_PATH = REPO_ROOT / "database" / "seed.sql"


class CaseInsensitiveHeaders(dict):
    """Minimal case-insensitive HTTP headers mapping for Worker mock requests."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        target = key.lower()
        for k, v in self.items():
            if str(k).lower() == target:
                return v
        return default

    def __contains__(self, key: object) -> bool:
        target = str(key).lower()
        return any(str(k).lower() == target for k in self.keys())


class MockD1Statement:
    """SQLite-backed mock of Cloudflare D1 `D1PreparedStatement`."""

    def __init__(self, conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> None:
        self._conn = conn
        self._sql = sql
        self._params = tuple(params)

    def bind(self, *params: Any) -> "MockD1Statement":
        return MockD1Statement(self._conn, self._sql, params)

    async def all(self) -> dict[str, Any]:
        cur = self._conn.execute(self._sql, self._params)
        rows = [dict(r) for r in cur.fetchall()]
        return {"results": rows, "success": True, "meta": {"changes": self._conn.total_changes}}

    async def first(self) -> dict[str, Any] | None:
        cur = self._conn.execute(self._sql, self._params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    async def run(self) -> dict[str, Any]:
        cur = self._conn.execute(self._sql, self._params)
        self._conn.commit()
        return {"success": True, "meta": {"changes": cur.rowcount}}


class MockD1Database:
    """In-memory SQLite mock implementing Cloudflare D1 `.prepare()` and `.batch()`."""

    def __init__(self, *, with_seed: bool = True) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.executescript(SCHEMA_SQL_PATH.read_text(encoding="utf-8"))
        if with_seed and SEED_SQL_PATH.exists():
            self.conn.executescript(SEED_SQL_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def prepare(self, sql: str) -> MockD1Statement:
        return MockD1Statement(self.conn, sql)

    async def batch(self, stmts: Sequence[MockD1Statement]) -> list[dict[str, Any]]:
        results = []
        for stmt in stmts:
            results.append(await stmt.run())
        return results

    def close(self) -> None:
        self.conn.close()


class MockKVNamespace:
    """Dict-backed mock of Cloudflare Workers KV (`APP_CACHE`)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def put(self, key: str, value: str, expirationTtl: int | None = None) -> None:
        _ = expirationTtl
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class MockR2Object:
    """Minimal mock of a Cloudflare R2 object returned by `bucket.get()`."""

    def __init__(self, body: bytes, content_type: str = "image/jpeg") -> None:
        self.body = body
        self.httpMetadata = SimpleNamespace(contentType=content_type)

    async def arrayBuffer(self) -> bytes:
        return self.body


class MockR2Bucket:
    """Dict-backed mock of Cloudflare R2 `SLIDES_BUCKET`."""

    def __init__(self) -> None:
        self.objects: dict[str, MockR2Object] = {}

    async def put(self, key: str, data: bytes, **kwargs: Any) -> None:
        content_type = str(kwargs.get("content_type") or "image/jpeg")
        self.objects[key] = MockR2Object(bytes(data), content_type=content_type)

    async def get(self, key: str) -> MockR2Object | None:
        return self.objects.get(key)

    async def list(self, options: dict[str, Any] | None = None, *, prefix: str = "") -> SimpleNamespace:
        pfx = prefix or (str(options.get("prefix") or "") if isinstance(options, dict) else "")
        matches = [SimpleNamespace(key=k) for k in self.objects.keys() if k.startswith(pfx)]
        return SimpleNamespace(objects=matches)


class MockWorkerRequest:
    """Mock Cloudflare Worker `Request` supporting JSON, raw bytes, headers, and mock `form_data`."""

    def __init__(
        self,
        method: str,
        path_or_url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        raw_body: bytes | str | None = None,
        form_data: dict[str, Any] | None = None,
    ) -> None:
        self.method = method.upper()
        self.url = (
            path_or_url
            if path_or_url.startswith(("http://", "https://"))
            else f"https://omnilearn.example{path_or_url}"
        )
        hdrs = CaseInsensitiveHeaders(headers or {})
        self.headers = hdrs
        self._json_body = json_body
        self._raw_body = raw_body
        self.form_data = form_data

    async def json(self) -> Any:
        if self._json_body is not None:
            return self._json_body
        if self._raw_body is not None:
            txt = self._raw_body.decode("utf-8") if isinstance(self._raw_body, bytes) else self._raw_body
            return json.loads(txt)
        return {}

    async def text(self) -> str:
        if self._raw_body is not None:
            return self._raw_body.decode("utf-8") if isinstance(self._raw_body, bytes) else self._raw_body
        if self._json_body is not None:
            return json.dumps(self._json_body)
        return ""

    async def arrayBuffer(self) -> bytes:
        if isinstance(self._raw_body, bytes):
            return self._raw_body
        if isinstance(self._raw_body, str):
            return self._raw_body.encode("utf-8")
        if self._json_body is not None:
            return json.dumps(self._json_body).encode("utf-8")
        return b""


def build_worker_env(
    *,
    runtime_mode: str = "web_lite",
    with_seed: bool = True,
    master_secret: str = "omnilearn-test-master-secret-32bytes!",
) -> SimpleNamespace:
    """Create an isolated Cloudflare Worker `env` namespace with in-memory D1/KV/R2."""
    return SimpleNamespace(
        DB=MockD1Database(with_seed=with_seed),
        APP_CACHE=MockKVNamespace(),
        SLIDES_BUCKET=MockR2Bucket(),
        RUNTIME_MODE=runtime_mode,
        EDITION_MODE=runtime_mode,
        OMNILEARN_RUNTIME=runtime_mode,
        MASTER_SECRET=master_secret,
    )


def extract_cookie_token(set_cookie_header: str) -> str:
    """Extract the `omnilearn_session` value from a `Set-Cookie` header string."""
    for part in (set_cookie_header or "").split(";"):
        piece = part.strip()
        if piece.startswith("omnilearn_session="):
            return piece.split("=", 1)[1]
    return ""


@pytest.fixture
def worker_env_web_lite() -> Any:
    env = build_worker_env(runtime_mode="web_lite", with_seed=True)
    yield env
    env.DB.close()


@pytest.fixture
def worker_env_system_app() -> Any:
    env = build_worker_env(runtime_mode="system_app", with_seed=True)
    yield env
    env.DB.close()


@pytest.fixture
def isolated_local_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalDatabase:
    monkeypatch.setenv("OMNILEARN_IGNORE_DOTENV", "1")
    monkeypatch.setenv("OMNILEARN_DISABLE_KEYRING", "1")
    monkeypatch.delenv("OMNILEARN_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("OMNILEARN_ADMIN_PASSWORD", raising=False)
    db_path = tmp_path / "omnilearn_test.db"
    assets_dir = tmp_path / "assets"
    return LocalDatabase(db_path=db_path, assets_dir=assets_dir, seed_sample_data=True)


@pytest.fixture
def local_api_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Return an isolated FastAPI `TestClient`, local token, and `LocalDatabase`."""
    monkeypatch.setenv("OMNILEARN_IGNORE_DOTENV", "1")
    monkeypatch.setenv("OMNILEARN_DISABLE_KEYRING", "1")
    monkeypatch.delenv("OMNILEARN_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("OMNILEARN_ADMIN_PASSWORD", raising=False)
    db_path = tmp_path / "local_api_test.db"
    monkeypatch.setenv("OMNILEARN_DB_PATH", str(db_path))
    local_token = "test-local-token-xyz123"
    app = create_app(
        db_path=db_path,
        local_token=local_token,
        require_local_token=True,
    )
    with TestClient(app) as client:
        yield {
            "app": app,
            "client": client,
            "local_token": local_token,
            "headers": {"X-Local-App-Token": local_token},
            "db": app.state.db,
        }
