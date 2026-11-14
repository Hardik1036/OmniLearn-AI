"""Local SQLite 3 (WAL + FTS5) repository with AES-256-GCM OS Keychain key encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.local_app.parsers import NormalizedSourcePayload
from backend.local_app.parsers.doc_parsers import safe_filename

KEYRING_SERVICE = "omnilearn-system-app"
KEYRING_ACCOUNT = "master-wrapping-key"
SUPPORTED_PROVIDERS = frozenset({"gemini", "groq", "openrouter"})
DEFAULT_COOLDOWN_SECONDS = 65
PBKDF2_ITERATIONS = 100_000

_SAFE_SLIDE_KEY_RE = re.compile(r"^courses/([A-Za-z0-9_-]{1,64})/([A-Za-z0-9._-]{1,128})$")


def _resource_base_dir() -> Path:
    """Resolve repository or PyInstaller `_MEIPASS` root directory."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent.parent


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve SQLite DB path from explicit arg, `OMNILEARN_DB_PATH` env, or `~/.omnilearn/`."""
    if db_path is not None:
        return Path(db_path).expanduser().resolve()
    env_path = os.environ.get("OMNILEARN_DB_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    default_dir = Path.home() / ".omnilearn"
    try:
        default_dir.mkdir(parents=True, exist_ok=True)
        test_file = default_dir / ".write_test"
        test_file.touch()
        test_file.unlink(missing_ok=True)
    except (PermissionError, OSError):
        default_dir = Path(__file__).resolve().parent.parent.parent / ".omnilearn"
        default_dir.mkdir(parents=True, exist_ok=True)
    return (default_dir / "omnilearn_local.db").resolve()


def sanitize_local_slide_key(key: str) -> str:
    """Validate slide asset path strictly to `courses/<course_id>/<filename>` without traversal."""
    if not isinstance(key, str):
        raise ValueError("Slide key must be a string")

    raw = key.strip()
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("Invalid characters in slide asset path")

    decoded_once = unquote(raw)
    decoded_twice = unquote(decoded_once)
    for candidate in (raw, decoded_once, decoded_twice):
        if ".." in candidate or "\\" in candidate or "\x00" in candidate:
            raise ValueError("Path traversal (`..`) is prohibited in slide asset paths")
        if candidate.startswith("/"):
            raise ValueError("Leading `/` is prohibited in slide asset paths")

    match = _SAFE_SLIDE_KEY_RE.fullmatch(decoded_once)
    if not match or raw != decoded_once:
        raise ValueError("Slide asset path must match `courses/<course_id>/<filename>`")

    course_id, filename = match.groups()
    if course_id.startswith(".") or filename in (".", "..") or filename.startswith("."):
        raise ValueError("Hidden dot segments are prohibited in slide asset paths")
    return raw


class LocalDatabase:
    """Thread-safe local SQLite WAL + FTS5 repository for OmniLearn System App."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        assets_dir: str | Path | None = None,
        *,
        seed_sample_data: bool = True,
    ) -> None:
        self.db_path = resolve_db_path(db_path)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            self.db_path = (Path(__file__).resolve().parent.parent.parent / ".omnilearn" / "omnilearn_local.db").resolve()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        env_assets = os.environ.get("OMNILEARN_ASSETS_DIR", "").strip()
        if assets_dir is not None:
            self.assets_dir = Path(assets_dir).expanduser().resolve()
        elif env_assets:
            self.assets_dir = Path(env_assets).expanduser().resolve()
        else:
            self.assets_dir = (self.db_path.parent / "assets").resolve()

        try:
            self.assets_dir.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            self.assets_dir = (Path(__file__).resolve().parent.parent.parent / ".omnilearn" / "assets").resolve()
            self.assets_dir.mkdir(parents=True, exist_ok=True)

        self._write_lock = threading.RLock()
        self._wrapping_key: bytes | None = None
        self._cloud_tokens: dict[str, str] = {}
        self._init_database(seed_sample_data=seed_sample_data)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured SQLite WAL connection and commit/rollback cleanly."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA temp_store = MEMORY;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_database(self, *, seed_sample_data: bool = True) -> None:
        db_dir = _resource_base_dir() / "database"
        schema_sql = (db_dir / "schema.sql").read_text(encoding="utf-8")
        local_sql = (db_dir / "local_schema.sql").read_text(encoding="utf-8")
        seed_path = db_dir / "seed.sql"

        with self._write_lock, self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM sqlite_master WHERE type='table' AND name='courses'"
            ).fetchone()
            is_fresh_db = (row["cnt"] if row else 0) == 0

            self._migrate_legacy_api_keys(conn)
            self._migrate_legacy_courses(conn)
            conn.executescript(schema_sql)
            conn.executescript(local_sql)
            if seed_sample_data and is_fresh_db and seed_path.is_file():
                conn.executescript(seed_path.read_text(encoding="utf-8"))
                # Re-run FTS backfill statement idempotently for seeded course_sources.
                conn.execute(
                    """
                    INSERT INTO sources_fts(source_id, course_id, source_type, title, raw_text_excerpt)
                    SELECT cs.id, cs.course_id, cs.source_type, cs.title, cs.raw_text_excerpt
                    FROM course_sources cs
                    WHERE NOT EXISTS (
                        SELECT 1 FROM sources_fts f WHERE f.source_id = cs.id
                    )
                    """
                )
        self.sync_default_keys_from_env()

    @staticmethod
    def _migrate_legacy_api_keys(conn: sqlite3.Connection) -> None:
        """Upgrade legacy `api_keys` table in-place to include `owner_sub` and `is_default`."""
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_keys'"
        ).fetchone()
        if not tbl:
            return
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
        if "owner_sub" in cols and "is_default" in cols:
            return
        conn.execute("DROP INDEX IF EXISTS idx_api_keys_pool")
        conn.execute("DROP INDEX IF EXISTS idx_api_keys_owner_hash")
        conn.execute("ALTER TABLE api_keys RENAME TO _api_keys_legacy")
        conn.execute(
            """
            CREATE TABLE api_keys (
                id TEXT PRIMARY KEY,
                owner_sub TEXT NOT NULL DEFAULT 'admin',
                is_default INTEGER NOT NULL DEFAULT 0,
                provider TEXT NOT NULL DEFAULT 'gemini',
                key_hash TEXT NOT NULL,
                encrypted_key TEXT NOT NULL,
                iv TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                cooldown_until INTEGER DEFAULT 0,
                request_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                last_used_at INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            INSERT INTO api_keys (
                id, owner_sub, is_default, provider, key_hash, encrypted_key, iv,
                status, cooldown_until, request_count, error_count, last_used_at, created_at
            )
            SELECT
                id, 'admin', 0, provider, key_hash, encrypted_key, iv,
                status, cooldown_until, request_count, error_count, last_used_at, created_at
            FROM _api_keys_legacy
            """
        )
        conn.execute("DROP TABLE _api_keys_legacy")

    @staticmethod
    def _migrate_legacy_courses(conn: sqlite3.Connection) -> None:
        """Upgrade legacy `courses` table in-place to include `owner_sub`."""
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='courses'"
        ).fetchone()
        if not tbl:
            return
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(courses)").fetchall()}
        if "owner_sub" not in cols:
            conn.execute("ALTER TABLE courses ADD COLUMN owner_sub TEXT NOT NULL DEFAULT 'admin'")

    # ------------------------------------------------------------------
    # Wrapping Key & AES-256-GCM Encryption (OS Keyring + 0600 fallback)
    # ------------------------------------------------------------------

    def _local_key_file(self) -> Path:
        return self.db_path.parent / ".master.key"

    def _read_local_key_file(self) -> bytes | None:
        key_file = self._local_key_file()
        if not key_file.is_file():
            return None
        try:
            raw = base64.urlsafe_b64decode(key_file.read_text(encoding="ascii").strip().encode("ascii"))
            if len(raw) == 32:
                return raw
        except Exception:
            return None
        return None

    def _write_local_key_file(self, key_bytes: bytes) -> None:
        key_file = self._local_key_file()
        encoded = base64.urlsafe_b64encode(key_bytes).decode("ascii")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(key_file), flags, 0o600)
        try:
            os.write(fd, encoded.encode("ascii"))
        finally:
            os.close(fd)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass

    def _get_or_create_wrapping_key(self) -> bytes:
        if self._wrapping_key is not None:
            return self._wrapping_key

        env_secret = os.environ.get("OMNILEARN_MASTER_KEY", "").strip()
        if env_secret:
            self._wrapping_key = hashlib.sha256(env_secret.encode("utf-8")).digest()
            return self._wrapping_key

        # Check existing local fallback file first to preserve decryption continuity.
        existing_local = self._read_local_key_file()
        if existing_local is not None:
            self._wrapping_key = existing_local
            return self._wrapping_key

        use_keyring = os.environ.get("OMNILEARN_DISABLE_KEYRING", "").strip() != "1"
        if use_keyring:
            try:
                import keyring

                stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
                if stored:
                    decoded = base64.urlsafe_b64decode(stored.strip().encode("ascii"))
                    if len(decoded) == 32:
                        self._wrapping_key = decoded
                        return self._wrapping_key

                new_key = secrets.token_bytes(32)
                encoded = base64.urlsafe_b64encode(new_key).decode("ascii")
                keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, encoded)
                self._wrapping_key = new_key
                return self._wrapping_key
            except Exception:
                pass

        # Headless / CI / no-keyring fallback with 0600 permissions.
        new_key = secrets.token_bytes(32)
        self._write_local_key_file(new_key)
        self._wrapping_key = new_key
        return self._wrapping_key

    def encrypt_secret(self, plaintext: str) -> tuple[str, str]:
        """Encrypt a sensitive string using AES-256-GCM with a random 96-bit IV."""
        key = self._get_or_create_wrapping_key()
        iv = secrets.token_bytes(12)
        ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
        return base64.b64encode(ct).decode("ascii"), base64.b64encode(iv).decode("ascii")

    def decrypt_secret(self, ciphertext_b64: str, iv_b64: str) -> str:
        """Decrypt an AES-256-GCM ciphertext back to plaintext."""
        key = self._get_or_create_wrapping_key()
        ct = base64.b64decode(ciphertext_b64.encode("ascii"))
        iv = base64.b64decode(iv_b64.encode("ascii"))
        return AESGCM(key).decrypt(iv, ct, None).decode("utf-8")

    # ------------------------------------------------------------------
    # Single-Admin PBKDF2 Authentication
    # ------------------------------------------------------------------

    def has_admin(self) -> bool:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM admin_users").fetchone()
            return bool(row and row["cnt"] > 0)

    def init_admin(self, password: str, username: str = "admin") -> dict[str, Any]:
        """Initialize the single admin account using PBKDF2-SHA256 (100k iterations)."""
        if not password or len(password) < 6:
            raise ValueError("Admin password must be at least 6 characters long.")
        with self._write_lock, self.connection() as conn:
            existing = conn.execute("SELECT id FROM admin_users LIMIT 1").fetchone()
            if existing is not None:
                raise ValueError("Admin account is already initialized.")
            salt_hex = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                PBKDF2_ITERATIONS,
                dklen=32,
            ).hex()
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, salt, last_login) VALUES (?, ?, ?, datetime('now'))",
                (username, digest, salt_hex),
            )
            return {"ok": True, "username": username, "initialized": True}

    def verify_admin(self, password: str, username: str = "admin") -> bool:
        """Verify admin username and password via PBKDF2-SHA256 in constant time."""
        clean_user = (username or "").strip()
        if not password or not clean_user:
            return False
        with self._write_lock, self.connection() as conn:
            row = conn.execute(
                "SELECT id, password_hash, salt FROM admin_users WHERE username = ? LIMIT 1",
                (clean_user,),
            ).fetchone()
            if row is None:
                return False

            salt_str = str(row["salt"])
            try:
                salt_bytes = bytes.fromhex(salt_str)
            except ValueError:
                salt_bytes = salt_str.encode("utf-8")

            computed = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt_bytes,
                PBKDF2_ITERATIONS,
                dklen=32,
            ).hex()
            if not hmac.compare_digest(computed.lower(), str(row["password_hash"]).lower()):
                return False

            conn.execute(
                "UPDATE admin_users SET last_login = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            return True

    def login_admin(
        self,
        password: str,
        *,
        api_key: str = "",
        provider: str = "gemini",
        username: str = "admin",
    ) -> dict[str, Any]:
        """Verify PBKDF2 admin user ID & password BEFORE adding any personal API key."""
        clean_user = (username or "").strip()
        if not self.verify_admin(password, username=clean_user):
            raise PermissionError("Invalid admin user ID or password.")
        added_slot = None
        if api_key and api_key.strip():
            added_slot = self.add_api_key(api_key, provider=provider, owner_sub=clean_user)
        return {
            "ok": True,
            "authenticated": True,
            "role": "admin",
            "username": clean_user,
            "key_added": bool(added_slot),
            "pool": self.get_pool_summary(owner_sub=clean_user),
        }

    # ------------------------------------------------------------------
    # Google Gmail Normal Users
    # ------------------------------------------------------------------

    def upsert_google_user(
        self,
        *,
        google_sub: str,
        email: str,
        name: str = "",
        picture: str = "",
        role: str = "user",
    ) -> dict[str, Any]:
        """Create or update a Google-authenticated user record by google_sub/email."""
        clean_sub = (google_sub or "").strip()
        clean_email = (email or "").strip().lower()
        if not clean_sub or not clean_email or "@" not in clean_email:
            raise ValueError("Valid Google sub and verified email are required.")
        clean_role = "user" if role not in ("admin", "user") else role
        clean_name = (name or "").strip()[:120]
        clean_pic = (picture or "").strip()[:512]

        with self._write_lock, self.connection() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM users WHERE google_sub = ? OR email = ? LIMIT 1",
                (clean_sub, clean_email),
            ).fetchone()
            if existing is not None:
                uid = str(existing["id"])
                conn.execute(
                    """
                    UPDATE users
                    SET google_sub = ?, email = ?, name = ?, picture = ?, role = ?, last_login = datetime('now')
                    WHERE id = ?
                    """,
                    (clean_sub, clean_email, clean_name, clean_pic, clean_role, uid),
                )
            else:
                uid = f"usr-{uuid.uuid4().hex[:16]}"
                conn.execute(
                    """
                    INSERT INTO users (id, google_sub, email, name, picture, role, last_login)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (uid, clean_sub, clean_email, clean_name, clean_pic, clean_role),
                )
            row = conn.execute(
                "SELECT id, google_sub, email, name, picture, role, created_at, last_login FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
            return dict(row) if row else {
                "id": uid,
                "google_sub": clean_sub,
                "email": clean_email,
                "name": clean_name,
                "picture": clean_pic,
                "role": clean_role,
            }

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        clean_email = (email or "").strip().lower()
        if not clean_email:
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id, google_sub, email, name, picture, role, created_at, last_login FROM users WHERE email = ? LIMIT 1",
                (clean_email,),
            ).fetchone()
            return dict(row) if row else None

    def set_cloud_token(self, owner_sub: str, token: str) -> None:
        clean_owner = (owner_sub or "").strip()
        clean_token = (token or "").strip()
        if not clean_owner:
            return
        with self._write_lock:
            if clean_token:
                self._cloud_tokens[clean_owner] = clean_token
            else:
                self._cloud_tokens.pop(clean_owner, None)

    def get_cloud_token(self, owner_sub: str | None = None) -> str:
        clean_owner = (owner_sub or "").strip()
        with self._write_lock:
            if clean_owner and clean_owner in self._cloud_tokens:
                return self._cloud_tokens[clean_owner]
        try:
            from .env_config import get_cloud_relay_config
        except ImportError:
            from env_config import get_cloud_relay_config
        return str(get_cloud_relay_config().get("cloud_token") or "")

    def clear_cloud_token(self, owner_sub: str | None = None) -> None:
        clean_owner = (owner_sub or "").strip()
        with self._write_lock:
            if clean_owner:
                self._cloud_tokens.pop(clean_owner, None)
            else:
                self._cloud_tokens.clear()

    def sync_admin_from_env(self) -> bool:
        """Synchronize single-admin credentials from `.env` if configured."""
        try:
            from .env_config import get_env_admin_credentials, load_dotenv_file
        except ImportError:
            from env_config import get_env_admin_credentials, load_dotenv_file

        load_dotenv_file()
        creds = get_env_admin_credentials()
        if creds is None:
            return False
        env_user, env_pass = creds
        with self._write_lock, self.connection() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, salt FROM admin_users ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if row is None:
            self.init_admin(env_pass, username=env_user)
            return True

        existing_user = str(row["username"] or "")
        already_matches = (
            existing_user == env_user
            and self.verify_admin(env_pass, username=env_user)
        )
        if already_matches:
            return False

        salt_hex = secrets.token_hex(16)
        digest_hex = hashlib.pbkdf2_hmac(
            "sha256",
            env_pass.encode("utf-8"),
            bytes.fromhex(salt_hex),
            PBKDF2_ITERATIONS,
            dklen=32,
        ).hex()
        with self._write_lock, self.connection() as conn:
            conn.execute(
                "UPDATE admin_users SET username = ?, password_hash = ?, salt = ? WHERE id = ?",
                (env_user, digest_hex, salt_hex, row["id"]),
            )
        return True

    # ------------------------------------------------------------------
    # Multi-API Key Pool (Zero-Leakage CRUD)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_api_key(raw_key: str, provider: str = "gemini") -> tuple[str, str]:
        clean_key = (raw_key or "").strip()
        if len(clean_key) < 10 or any(ch.isspace() for ch in clean_key):
            raise ValueError("Invalid API key format.")
        clean_provider = (provider or "gemini").strip().lower()
        if clean_provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider '{clean_provider}'.")
        return clean_key, clean_provider

    def sync_default_keys_from_env(self) -> int:
        """Encrypt & synchronize `.env` API keys into `api_keys` (`is_default = 1`)."""
        try:
            from .env_config import get_configured_env_api_keys, load_dotenv_file
        except ImportError:
            from env_config import get_configured_env_api_keys, load_dotenv_file

        load_dotenv_file()
        env_keys = get_configured_env_api_keys()
        desired: dict[str, tuple[str, str]] = {}
        for _slot_label, provider, raw_val in env_keys:
            try:
                clean_key, clean_provider = self._validate_api_key(raw_val, provider)
            except ValueError:
                continue
            kh = hashlib.sha256(clean_key.encode("utf-8")).hexdigest()
            desired.setdefault(kh, (clean_provider, clean_key))

        changed = 0
        with self._write_lock, self.connection() as conn:
            existing_rows = conn.execute(
                "SELECT id, key_hash FROM api_keys WHERE is_default = 1 AND owner_sub = 'system_default'"
            ).fetchall()
            existing_map = {str(r["key_hash"]): str(r["id"]) for r in existing_rows}

            for old_hash, old_id in existing_map.items():
                if old_hash not in desired:
                    conn.execute("DELETE FROM api_keys WHERE id = ?", (old_id,))
                    changed += 1

            for kh, (clean_provider, clean_key) in desired.items():
                if kh in existing_map:
                    continue
                encrypted_key, iv = self.encrypt_secret(clean_key)
                slot_id = f"envdef-{kh[:16]}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO api_keys
                        (id, owner_sub, is_default, provider, key_hash, encrypted_key, iv,
                         status, cooldown_until, request_count, error_count, last_used_at)
                    VALUES (?, 'system_default', 1, ?, ?, ?, ?, 'ACTIVE', 0, 0, 0, 0)
                    """,
                    (slot_id, clean_provider, kh, encrypted_key, iv),
                )
                changed += 1
        return changed

    def add_api_key(
        self,
        raw_key: str,
        provider: str = "gemini",
        *,
        owner_sub: str = "admin",
        is_default: bool = False,
    ) -> dict[str, Any]:
        """Deduplicate via SHA-256, encrypt via AES-256-GCM, and store without returning any key chars."""
        clean_key, clean_provider = self._validate_api_key(raw_key, provider)
        key_hash = hashlib.sha256(clean_key.encode("utf-8")).hexdigest()
        clean_owner = (owner_sub or "admin").strip() or "admin"
        default_flag = 1 if is_default else 0
        if default_flag:
            clean_owner = "system_default"

        with self._write_lock, self.connection() as conn:
            existing = conn.execute(
                "SELECT id, provider, status, is_default FROM api_keys WHERE owner_sub = ? AND key_hash = ?",
                (clean_owner, key_hash),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "INVALID":
                    conn.execute(
                        "UPDATE api_keys SET status = 'ACTIVE', cooldown_until = 0, error_count = 0 WHERE id = ?",
                        (existing["id"],),
                    )
                return {
                    "ok": True,
                    "deduplicated": True,
                    "slot_id": str(existing["id"]),
                    "provider": str(existing["provider"]),
                    "status": "ACTIVE",
                    "is_default": bool(existing["is_default"]),
                }

            encrypted_key, iv = self.encrypt_secret(clean_key)
            slot_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO api_keys
                    (id, owner_sub, is_default, provider, key_hash, encrypted_key, iv,
                     status, cooldown_until, request_count, error_count, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 0, 0, 0, 0)
                """,
                (slot_id, clean_owner, default_flag, clean_provider, key_hash, encrypted_key, iv),
            )
            return {
                "ok": True,
                "deduplicated": False,
                "slot_id": slot_id,
                "provider": clean_provider,
                "status": "ACTIVE",
                "is_default": bool(default_flag),
            }

    def delete_api_key(
        self,
        key_id: str,
        *,
        owner_sub: str | None = None,
        allow_default: bool = False,
    ) -> bool:
        clean_id = (key_id or "").strip()
        if not clean_id:
            return False
        with self._write_lock, self.connection() as conn:
            row = conn.execute(
                "SELECT id, owner_sub, is_default FROM api_keys WHERE id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                return False
            if int(row["is_default"] or 0) == 1 and not allow_default:
                raise PermissionError(
                    "Default `.env` API keys are managed via `.env`. Add a personal API key to override them."
                )
            if owner_sub is not None and int(row["is_default"] or 0) == 0 and str(row["owner_sub"]) != owner_sub:
                return False
            cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (clean_id,))
            return cur.rowcount > 0

    def get_pool_summary(self, owner_sub: str | None = None) -> dict[str, Any]:
        """ZERO-LEAK summary: returns only anonymous slot numbers, source mode, statuses & counts."""
        self.sync_default_keys_from_env()
        now = int(time.time())
        clean_owner = owner_sub.strip() if isinstance(owner_sub, str) and owner_sub.strip() else None

        with self.connection() as conn:
            # Never select encrypted_key, iv, or key_hash.
            if clean_owner is not None:
                rows = conn.execute(
                    """
                    SELECT id, owner_sub, is_default, provider, status, cooldown_until,
                           request_count, error_count, created_at
                    FROM api_keys
                    WHERE (is_default = 0 AND owner_sub = ?) OR is_default = 1
                    ORDER BY is_default ASC, created_at ASC, id ASC
                    """,
                    (clean_owner,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, owner_sub, is_default, provider, status, cooldown_until,
                           request_count, error_count, created_at
                    FROM api_keys
                    ORDER BY is_default ASC, created_at ASC, id ASC
                    """
                ).fetchall()

        personal_total = 0
        personal_active = 0
        default_total = 0
        default_active = 0
        parsed_rows: list[tuple[sqlite3.Row, str, int]] = []

        for row in rows:
            is_def = int(row["is_default"] or 0) == 1
            status = str(row["status"] or "ACTIVE").upper()
            cooldown_until = int(row["cooldown_until"] or 0)
            cooldown_remaining = 0
            if status == "COOLDOWN":
                if cooldown_until <= now:
                    status = "ACTIVE"
                else:
                    cooldown_remaining = cooldown_until - now
            parsed_rows.append((row, status, cooldown_remaining))
            if is_def:
                if status != "INVALID":
                    default_total += 1
                if status == "ACTIVE":
                    default_active += 1
            else:
                if status != "INVALID":
                    personal_total += 1
                if status == "ACTIVE":
                    personal_active += 1

        has_personal_override = personal_total > 0
        try:
            from .env_config import get_cloud_relay_config
        except ImportError:
            from env_config import get_cloud_relay_config
        relay_cfg = get_cloud_relay_config()
        cloud_token = self.get_cloud_token(clean_owner)
        relay_configured = bool(relay_cfg.get("cloud_url") and cloud_token)

        if has_personal_override:
            using_source = "personal"
        elif default_total > 0:
            using_source = "default_env"
        elif relay_configured:
            using_source = "cloud_relay"
        else:
            using_source = "none"
        slots: list[dict[str, Any]] = []

        for idx, (row, status, cooldown_remaining) in enumerate(parsed_rows):
            is_def = int(row["is_default"] or 0) == 1
            req_count = int(row["request_count"] or 0)
            slots.append(
                {
                    "id": str(row["id"]),
                    "slot": idx + 1,
                    "provider": str(row["provider"] or "gemini"),
                    "status": status,
                    "request_count": req_count,
                    "requests_served": req_count,
                    "error_count": int(row["error_count"] or 0),
                    "cooldown_remaining": cooldown_remaining,
                    "is_default": is_def,
                    "source": "env_default" if is_def else "personal",
                    "active_in_routing": (not is_def) if has_personal_override else is_def,
                    "overridden_by_personal": bool(is_def and has_personal_override),
                }
            )

        try:
            from .env_config import get_gemini_format_warnings
        except ImportError:
            from env_config import get_gemini_format_warnings
        invalid_format_warnings = get_gemini_format_warnings()

        return {
            "ok": True,
            "edition": "system_app",
            "max_slots": None,
            "using_source": using_source,
            "relay_configured": relay_configured,
            "personal_keys_count": personal_total,
            "default_keys_count": default_total,
            "total_active_keys": (
                personal_active
                if has_personal_override
                else (default_active if default_total > 0 else (1 if relay_configured else 0))
            ),
            "total_db_keys": personal_total,
            "env_keys_configured": default_total,
            "slots": slots,
            "invalid_format_warnings": invalid_format_warnings,
        }

    def get_candidate_keys(
        self,
        provider: str | None = None,
        owner_sub: str | None = None,
    ) -> list[dict[str, str]]:
        """Return decrypted keys sorted by LRU: user personal keys first, else `.env` defaults."""
        self.sync_default_keys_from_env()
        now = int(time.time())
        clean_owner = owner_sub.strip() if isinstance(owner_sub, str) and owner_sub.strip() else None
        candidates: list[dict[str, str]] = []

        with self._write_lock, self.connection() as conn:
            conn.execute(
                "UPDATE api_keys SET status = 'ACTIVE', cooldown_until = 0 WHERE status = 'COOLDOWN' AND cooldown_until <= ?",
                (now,),
            )

            # Check whether this user (or single-user workspace) has configured personal keys.
            if clean_owner is not None:
                personal_exists = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM api_keys WHERE is_default = 0 AND status != 'INVALID' AND owner_sub = ?",
                    (clean_owner,),
                ).fetchone()
            else:
                personal_exists = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM api_keys WHERE is_default = 0 AND status != 'INVALID'"
                ).fetchone()
            use_personal_only = bool(personal_exists and int(personal_exists["cnt"] or 0) > 0)

            where_clauses = ["status = 'ACTIVE'", "(cooldown_until IS NULL OR cooldown_until <= ?)"]
            params: list[Any] = [now]
            if use_personal_only:
                where_clauses.append("is_default = 0")
                if clean_owner is not None:
                    where_clauses.append("owner_sub = ?")
                    params.append(clean_owner)
            else:
                where_clauses.append("is_default = 1")

            if provider:
                where_clauses.append("provider = ?")
                params.append(provider)

            rows = conn.execute(
                f"""
                SELECT id, provider, encrypted_key, iv
                FROM api_keys
                WHERE {' AND '.join(where_clauses)}
                ORDER BY last_used_at ASC, request_count ASC, id ASC
                """,
                tuple(params),
            ).fetchall()

        for row in rows:
            try:
                plaintext = self.decrypt_secret(str(row["encrypted_key"]), str(row["iv"]))
            except Exception:
                continue
            if plaintext:
                candidates.append(
                    {
                        "id": str(row["id"]),
                        "provider": str(row["provider"]),
                        "api_key": plaintext,
                    }
                )
        candidates.sort(
            key=lambda c: 1 if (c.get("provider") == "gemini" and not str(c.get("api_key") or "").startswith("AIza")) else 0
        )
        return candidates

    def mark_key_used(self, key_id: str) -> None:
        if not key_id or key_id.startswith("env:"):
            return
        now = int(time.time())
        with self._write_lock, self.connection() as conn:
            conn.execute(
                """
                UPDATE api_keys
                SET request_count = request_count + 1,
                    last_used_at = ?,
                    status = 'ACTIVE',
                    cooldown_until = 0
                WHERE id = ?
                """,
                (now, key_id),
            )

    def mark_key_cooldown(
        self,
        key_id: str,
        *,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        is_invalid: bool = False,
    ) -> None:
        if not key_id or key_id.startswith("env:"):
            return
        now = int(time.time())
        with self._write_lock, self.connection() as conn:
            if is_invalid:
                conn.execute(
                    "UPDATE api_keys SET status = 'INVALID', error_count = error_count + 1 WHERE id = ?",
                    (key_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE api_keys
                    SET status = 'COOLDOWN',
                        cooldown_until = ?,
                        error_count = error_count + 1,
                        last_used_at = ?
                    WHERE id = ?
                    """,
                    (now + int(cooldown_seconds), now, key_id),
                )

    # ------------------------------------------------------------------
    # Courses, Sources, Subjects & Progress CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _json_load(raw: Any, fallback: Any) -> Any:
        if raw is None or raw == "":
            return fallback
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return fallback
        return raw

    def list_courses(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM subjects s WHERE s.course_id = c.id) AS subject_count,
                       (SELECT COUNT(*) FROM course_sources cs WHERE cs.course_id = c.id) AS actual_source_count
                FROM courses c
                ORDER BY c.created_at DESC, c.id DESC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["source_count"] = max(int(item.get("source_count") or 0), int(item.pop("actual_source_count", 0) or 0))
            out.append(item)
        return out

    def create_course(
        self,
        title: str,
        *,
        layout_type: str = "mixed",
        video_name: str = "",
        course_id: str | None = None,
        rois: list[dict[str, Any]] | None = None,
        owner_sub: str = "admin",
    ) -> dict[str, Any]:
        cid = course_id or f"course-{uuid.uuid4().hex[:12]}"
        clean_title = (title or "").strip() or "Untitled Multi-Source Course"
        clean_layout = (layout_type or "mixed").strip() or "mixed"
        with self._write_lock, self.connection() as conn:
            conn.execute(
                """
                INSERT INTO courses
                    (id, title, video_name, video_duration, resolution, layout_type, source_count, edition_origin, status, progress, current_stage, owner_sub)
                VALUES (?, ?, ?, 0.0, 'MULTI_SOURCE', ?, 0, 'system_app', 'READY', 100, 'Ready', ?)
                """,
                (cid, clean_title, video_name, clean_layout, owner_sub),
            )
            if rois:
                for roi in rois:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO window_rois
                            (id, course_id, roi_key, name, ymin, ymax, xmin, xmax, color)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            roi.get("id") or f"roi-{uuid.uuid4().hex[:8]}",
                            cid,
                            roi["roi_key"],
                            roi.get("name") or roi["roi_key"],
                            int(roi.get("ymin", 0)),
                            int(roi.get("ymax", 500)),
                            int(roi.get("xmin", 0)),
                            int(roi.get("xmax", 500)),
                            roi.get("color", "#3B82F6"),
                        ),
                    )
            row = conn.execute("SELECT * FROM courses WHERE id = ?", (cid,)).fetchone()
            return dict(row)

    def delete_course(self, course_id: str) -> bool:
        with self._write_lock, self.connection() as conn:
            cur = conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))
            deleted = cur.rowcount > 0
        if deleted:
            course_assets = self.assets_dir / "courses" / safe_filename(course_id, default="invalid")
            if course_assets.is_dir():
                shutil.rmtree(course_assets, ignore_errors=True)
        return deleted

    def get_course(self, course_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            c_row = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
            if c_row is None:
                return None

            rois = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM window_rois WHERE course_id = ? ORDER BY id ASC",
                    (course_id,),
                ).fetchall()
            ]
            sources = [
                self._deserialize_source_row(dict(r))
                for r in conn.execute(
                    "SELECT * FROM course_sources WHERE course_id = ? ORDER BY created_at DESC, id DESC",
                    (course_id,),
                ).fetchall()
            ]
            subj_rows = conn.execute(
                "SELECT * FROM subjects WHERE course_id = ? ORDER BY rowid ASC",
                (course_id,),
            ).fetchall()

            subjects: list[dict[str, Any]] = []
            for s in subj_rows:
                sid = str(s["id"])
                det = conn.execute("SELECT * FROM subject_details WHERE subject_id = ?", (sid,)).fetchone()
                plan = conn.execute("SELECT * FROM study_plans WHERE subject_id = ?", (sid,)).fetchone()
                quiz = conn.execute("SELECT * FROM quizzes WHERE subject_id = ?", (sid,)).fetchone()
                paper = conn.execute("SELECT * FROM model_papers WHERE subject_id = ?", (sid,)).fetchone()
                prog = conn.execute("SELECT * FROM user_progress WHERE subject_id = ?", (sid,)).fetchone()

                subjects.append(
                    {
                        "id": sid,
                        "course_id": str(s["course_id"]),
                        "quadrant_key": str(s["quadrant_key"]),
                        "title": str(s["title"]),
                        "short_title": s["short_title"] or s["title"],
                        "topic": s["topic"] or "",
                        "unit": s["unit"] or "",
                        "instructor": s["instructor"] or "",
                        "badge_color": s["badge_color"] or "#3B82F6",
                        "summary": s["summary"] or "",
                        "subtitles_transcript": self._json_load(s["subtitles_transcript"], []),
                        "slide_crops": self._json_load(s["slide_crops"], []),
                        "source_citations": self._json_load(s["source_citations"], []),
                        "mermaid_diagram": s["mermaid_diagram"] or "",
                        "key_concepts": self._json_load(det["key_concepts"], []) if det else [],
                        "math_formulas": self._json_load(det["math_formulas"], []) if det else [],
                        "study_plan": {
                            "target_days": int(plan["target_days"]) if plan else 14,
                            "daily_hours": float(plan["daily_hours"]) if plan else 3.0,
                            "schedule": self._json_load(plan["schedule_json"], []) if plan else [],
                        },
                        "quizzes": self._json_load(quiz["questions_json"], []) if quiz else [],
                        "model_paper": {
                            "title": paper["title"] if paper else f"{s['title']} Model Exam Paper",
                            "time_allowed": paper["time_allowed"] if paper else "60 Minutes",
                            "max_marks": int(paper["max_marks"]) if paper else 25,
                            "questions": self._json_load(paper["questions_json"], []) if paper else [],
                        },
                        "progress": {
                            "quiz_attempts": int(prog["quiz_attempts"]) if prog else 0,
                            "last_quiz_score": float(prog["last_quiz_score"]) if prog else 0.0,
                            "mastery_score": float(prog["mastery_score"]) if prog else 0.0,
                            "completed_tasks": self._json_load(prog["completed_tasks"], []) if prog else [],
                            "weak_spots": self._json_load(prog["weak_spots"], []) if prog else [],
                        },
                    }
                )

        course_dict = dict(c_row)
        course_dict["source_count"] = len(sources)
        return {
            "ok": True,
            "course": course_dict,
            "rois": rois,
            "window_rois": rois,
            "subjects": subjects,
            "sources": sources,
        }

    def _deserialize_source_row(self, row: dict[str, Any]) -> dict[str, Any]:
        row["structured_chunks"] = self._json_load(row.get("structured_chunks_json"), [])
        row["visual_assets"] = self._json_load(row.get("visual_assets_json"), [])
        row["discovered_links"] = self._json_load(row.get("discovered_links_json"), [])
        row["metadata"] = self._json_load(row.get("metadata_json"), {})
        return row

    def save_slide_bytes(self, course_id: str, filename: str, data: bytes) -> str:
        """Persist validated JPEG slide image into local assets dir and return `/api/slides/...` URL."""
        clean_course = re.sub(r"[^A-Za-z0-9_-]+", "_", course_id).strip("_") or "course"
        clean_file = safe_filename(filename, default="slide.jpg")
        key = sanitize_local_slide_key(f"courses/{clean_course}/{clean_file}")
        target = (self.assets_dir / key).resolve()
        if not str(target).startswith(str(self.assets_dir)):
            raise ValueError("Resolved asset path escaped assets directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"/api/slides/{key}"

    def read_slide_bytes(self, slide_key: str) -> tuple[bytes, str] | None:
        """Read a local slide image securely after path traversal sanitization."""
        clean_key = sanitize_local_slide_key(slide_key)
        target = (self.assets_dir / clean_key).resolve()
        if not str(target).startswith(str(self.assets_dir)) or not target.is_file():
            return None
        return target.read_bytes(), "image/jpeg"

    def add_course_source(
        self,
        course_id: str,
        payload: NormalizedSourcePayload,
        *,
        search_query: str = "",
        save_assets: bool = True,
    ) -> dict[str, Any]:
        """Insert a normalized source into `course_sources` (auto-indexed by FTS5 trigger)."""
        with self._write_lock, self.connection() as conn:
            course = conn.execute("SELECT id FROM courses WHERE id = ?", (course_id,)).fetchone()
            if course is None:
                raise KeyError(f"Course '{course_id}' not found.")

            existing = conn.execute(
                "SELECT * FROM course_sources WHERE course_id = ? AND file_hash = ? LIMIT 1",
                (course_id, payload.file_hash),
            ).fetchone()
            if existing is not None:
                out = self._deserialize_source_row(dict(existing))
                out["deduplicated"] = True
                return out

            source_id = f"src-{uuid.uuid4().hex[:12]}"
            visual_assets: list[dict[str, Any]] = []
            for idx, slide in enumerate(payload.visual_slides):
                slide_meta = {k: v for k, v in slide.items() if k != "data_bytes"}
                data_bytes = slide.get("data_bytes")
                if save_assets and isinstance(data_bytes, (bytes, bytearray)) and data_bytes:
                    fname = slide.get("filename") or f"{source_id}_{idx + 1}.jpg"
                    slide_meta["url"] = self.save_slide_bytes(course_id, fname, bytes(data_bytes))
                visual_assets.append(slide_meta)

            storage_key = f"courses/{course_id}/{source_id}"
            conn.execute(
                """
                INSERT INTO course_sources (
                    id, course_id, source_type, title, origin_url, search_query, file_hash,
                    storage_key, raw_text_excerpt, structured_chunks_json, visual_assets_json,
                    discovered_links_json, metadata_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PARSED')
                """,
                (
                    source_id,
                    course_id,
                    payload.source_type,
                    payload.title,
                    payload.origin_url,
                    search_query,
                    payload.file_hash,
                    storage_key,
                    payload.raw_text_excerpt,
                    json.dumps(payload.text_chunks, ensure_ascii=False),
                    json.dumps(visual_assets, ensure_ascii=False),
                    json.dumps(payload.discovered_links, ensure_ascii=False),
                    json.dumps(payload.metadata, ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE courses SET source_count = (SELECT COUNT(*) FROM course_sources WHERE course_id = ?) WHERE id = ?",
                (course_id, course_id),
            )
            row = conn.execute("SELECT * FROM course_sources WHERE id = ?", (source_id,)).fetchone()
            out = self._deserialize_source_row(dict(row))
            out["deduplicated"] = False
            return out

    def list_course_sources(self, course_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM course_sources WHERE course_id = ? ORDER BY created_at DESC, id DESC",
                (course_id,),
            ).fetchall()
        return [self._deserialize_source_row(dict(r)) for r in rows]

    def delete_course_source(self, source_id: str) -> bool:
        with self._write_lock, self.connection() as conn:
            row = conn.execute(
                "SELECT course_id FROM course_sources WHERE id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                return False
            course_id = str(row["course_id"])
            conn.execute("DELETE FROM course_sources WHERE id = ?", (source_id,))
            conn.execute(
                "UPDATE courses SET source_count = (SELECT COUNT(*) FROM course_sources WHERE course_id = ?) WHERE id = ?",
                (course_id, course_id),
            )
            return True

    def save_synthesized_course(
        self,
        course_id: str,
        subjects_data: list[dict[str, Any]],
        *,
        target_days: int = 14,
        daily_hours: float = 3.0,
        rois: list[dict[str, Any]] | None = None,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        """Save or upsert synthesized subjects, details, plans, quizzes, and model papers."""
        with self._write_lock, self.connection() as conn:
            course = conn.execute("SELECT id FROM courses WHERE id = ?", (course_id,)).fetchone()
            if course is None:
                raise KeyError(f"Course '{course_id}' not found.")

            if replace_existing:
                conn.execute("DELETE FROM subjects WHERE course_id = ?", (course_id,))
            else:
                for subj in subjects_data:
                    target_title = str(subj.get("title") or "").strip().lower()
                    target_sid = str(subj.get("id") or "").strip()
                    if target_sid:
                        conn.execute(
                            "DELETE FROM subjects WHERE course_id = ? AND id = ?",
                            (course_id, target_sid),
                        )
                    elif target_title:
                        conn.execute(
                            "DELETE FROM subjects WHERE course_id = ? AND LOWER(title) = ?",
                            (course_id, target_title),
                        )

            if rois:
                conn.execute("DELETE FROM window_rois WHERE course_id = ?", (course_id,))
                for roi in rois:
                    conn.execute(
                        """
                        INSERT INTO window_rois (id, course_id, roi_key, name, ymin, ymax, xmin, xmax, color)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            roi.get("id") or f"roi-{uuid.uuid4().hex[:8]}",
                            course_id,
                            roi.get("roi_key", "main"),
                            roi.get("name", "Window"),
                            int(roi.get("ymin", 0)),
                            int(roi.get("ymax", 1000)),
                            int(roi.get("xmin", 0)),
                            int(roi.get("xmax", 1000)),
                            roi.get("color", "#3B82F6"),
                        ),
                    )

            for idx, subj in enumerate(subjects_data):
                sid = subj.get("id") or f"subj-{uuid.uuid4().hex[:10]}"
                conn.execute(
                    """
                    INSERT INTO subjects (
                        id, course_id, quadrant_key, title, short_title, topic, unit, instructor,
                        badge_color, summary, subtitles_transcript, slide_crops, source_citations, mermaid_diagram
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        course_id,
                        subj.get("quadrant_key") or f"q{idx + 1}",
                        subj.get("title") or f"Subject {idx + 1}",
                        subj.get("short_title") or subj.get("title") or f"S{idx + 1}",
                        subj.get("topic") or "",
                        subj.get("unit") or "",
                        subj.get("instructor") or "OmniLearn AI",
                        subj.get("badge_color") or "#3B82F6",
                        subj.get("summary") or "",
                        json.dumps(subj.get("subtitles_transcript", []), ensure_ascii=False),
                        json.dumps(subj.get("slide_crops", []), ensure_ascii=False),
                        json.dumps(subj.get("source_citations", []), ensure_ascii=False),
                        subj.get("mermaid_diagram") or "",
                    ),
                )
                conn.execute(
                    "INSERT INTO subject_details (subject_id, key_concepts, math_formulas) VALUES (?, ?, ?)",
                    (
                        sid,
                        json.dumps(subj.get("key_concepts", []), ensure_ascii=False),
                        json.dumps(subj.get("math_formulas", []), ensure_ascii=False),
                    ),
                )
                conn.execute(
                    "INSERT INTO study_plans (subject_id, target_days, daily_hours, schedule_json) VALUES (?, ?, ?, ?)",
                    (
                        sid,
                        int(subj.get("target_days", target_days)),
                        float(subj.get("daily_hours", daily_hours)),
                        json.dumps(subj.get("schedule", []), ensure_ascii=False),
                    ),
                )
                conn.execute(
                    "INSERT INTO quizzes (subject_id, questions_json) VALUES (?, ?)",
                    (sid, json.dumps(subj.get("quizzes", []), ensure_ascii=False)),
                )
                paper = subj.get("model_paper") or {}
                conn.execute(
                    "INSERT INTO model_papers (subject_id, title, time_allowed, max_marks, questions_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        sid,
                        paper.get("title") or f"Model Exam Paper — {subj.get('title', 'Subject')}",
                        paper.get("time_allowed") or "60 Minutes",
                        int(paper.get("max_marks", 25)),
                        json.dumps(paper.get("questions", []), ensure_ascii=False),
                    ),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_progress (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots)
                    VALUES (?, 0, 0.0, 0.0, '[]', '[]')
                    """,
                    (sid,),
                )

            conn.execute(
                "UPDATE courses SET status = 'READY', progress = 100, current_stage = 'Synthesized' WHERE id = ?",
                (course_id,),
            )

        return self.get_course(course_id) or {}

    def create_subject(
        self,
        course_id: str,
        title: str,
        *,
        topic: str = "",
        unit: str = "",
        instructor: str = "OmniLearn AI",
        badge_color: str = "",
        quadrant_key: str = "",
    ) -> dict[str, Any]:
        """Add a new subject to a course without any artificial 4-subject ceiling."""
        palette = (
            "#3B82F6",
            "#10B981",
            "#8B5CF6",
            "#F59E0B",
            "#EC4899",
            "#06B6D4",
            "#F97316",
            "#6366F1",
        )
        clean_title = (title or "").strip() or "New Subject"
        with self._write_lock, self.connection() as conn:
            course = conn.execute("SELECT id FROM courses WHERE id = ?", (course_id,)).fetchone()
            if course is None:
                raise KeyError(f"Course '{course_id}' not found.")
            existing_count = conn.execute(
                "SELECT COUNT(*) FROM subjects WHERE course_id = ?",
                (course_id,),
            ).fetchone()[0]
            sid = f"subj-{uuid.uuid4().hex[:10]}"
            color = (badge_color or "").strip() or palette[existing_count % len(palette)]
            qkey = (quadrant_key or "").strip() or f"subj_{existing_count + 1}"
            short = clean_title[:24]
            conn.execute(
                """
                INSERT INTO subjects (
                    id, course_id, quadrant_key, title, short_title, topic, unit, instructor,
                    badge_color, summary, subtitles_transcript, slide_crops, source_citations, mermaid_diagram
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '[]', '[]', '[]', '')
                """,
                (sid, course_id, qkey, clean_title, short, topic, unit, instructor, color),
            )
            conn.execute(
                "INSERT INTO subject_details (subject_id, key_concepts, math_formulas) VALUES (?, '[]', '[]')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO study_plans (subject_id, target_days, daily_hours, schedule_json) VALUES (?, 14, 3.0, '[]')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO quizzes (subject_id, questions_json) VALUES (?, '[]')",
                (sid,),
            )
            conn.execute(
                "INSERT INTO model_papers (subject_id, title, time_allowed, max_marks, questions_json) VALUES (?, ?, '60 Minutes', 25, '[]')",
                (sid, f"Model Exam Paper — {clean_title}"),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO user_progress (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots)
                VALUES (?, 0, 0.0, 0.0, '[]', '[]')
                """,
                (sid,),
            )
            row = conn.execute("SELECT * FROM subjects WHERE id = ?", (sid,)).fetchone()
            return dict(row)

    def update_task_progress(
        self,
        subject_id: str,
        *,
        task_id: str | None = None,
        completed: bool | None = None,
        completed_tasks: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._write_lock, self.connection() as conn:
            subj = conn.execute("SELECT id FROM subjects WHERE id = ?", (subject_id,)).fetchone()
            if subj is None:
                raise KeyError(f"Subject '{subject_id}' not found.")

            conn.execute(
                """
                INSERT OR IGNORE INTO user_progress
                    (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots)
                VALUES (?, 0, 0.0, 0.0, '[]', '[]')
                """,
                (subject_id,),
            )
            row = conn.execute(
                "SELECT * FROM user_progress WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            current_tasks: list[str] = self._json_load(row["completed_tasks"], [])
            if completed_tasks is not None:
                updated_tasks = list(dict.fromkeys(str(t) for t in completed_tasks))
            elif task_id:
                task_set = list(dict.fromkeys(current_tasks))
                if completed and task_id not in task_set:
                    task_set.append(task_id)
                elif completed is False and task_id in task_set:
                    task_set.remove(task_id)
                updated_tasks = task_set
            else:
                updated_tasks = current_tasks

            plan_row = conn.execute(
                "SELECT schedule_json FROM study_plans WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            schedule = self._json_load(plan_row["schedule_json"], []) if plan_row else []
            # plan-view.js toggles top-level schedule item IDs (e.g., 'ds-day-1'); support both day-level and nested task IDs.
            top_ids = {str(day.get("id")) for day in schedule if isinstance(day, dict) and day.get("id")}
            if any(t in top_ids for t in updated_tasks) or not any(
                isinstance(day, dict) and day.get("tasks") for day in schedule
            ):
                total_tasks = len(schedule)
            else:
                total_tasks = sum(max(1, len(day.get("tasks", []))) for day in schedule if isinstance(day, dict))
            task_ratio = (len(updated_tasks) / total_tasks * 100.0) if total_tasks > 0 else float(row["mastery_score"] or 0.0)
            last_quiz = float(row["last_quiz_score"] or 0.0)
            mastery = round(max(last_quiz, min(100.0, 0.6 * last_quiz + 0.4 * task_ratio)), 2)

            conn.execute(
                """
                UPDATE user_progress
                SET completed_tasks = ?, mastery_score = ?, updated_at = datetime('now')
                WHERE subject_id = ?
                """,
                (json.dumps(updated_tasks), mastery, subject_id),
            )
            updated = conn.execute("SELECT * FROM user_progress WHERE subject_id = ?", (subject_id,)).fetchone()
            res = dict(updated)
            res["completed_tasks"] = updated_tasks
            res["weak_spots"] = self._json_load(res.get("weak_spots"), [])
            return res

    def update_quiz_progress(
        self,
        subject_id: str,
        score: float,
        *,
        weak_spots: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._write_lock, self.connection() as conn:
            subj = conn.execute("SELECT id FROM subjects WHERE id = ?", (subject_id,)).fetchone()
            if subj is None:
                raise KeyError(f"Subject '{subject_id}' not found.")

            conn.execute(
                """
                INSERT OR IGNORE INTO user_progress
                    (subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots)
                VALUES (?, 0, 0.0, 0.0, '[]', '[]')
                """,
                (subject_id,),
            )
            row = conn.execute("SELECT * FROM user_progress WHERE subject_id = ?", (subject_id,)).fetchone()
            attempts = int(row["quiz_attempts"] or 0) + 1
            prev_mastery = float(row["mastery_score"] or 0.0)
            clamped_score = max(0.0, min(100.0, float(score)))
            new_mastery = round(max(clamped_score, (prev_mastery * 0.4 + clamped_score * 0.6) if prev_mastery > 0 else clamped_score), 2)
            spots = weak_spots if weak_spots is not None else self._json_load(row["weak_spots"], [])

            conn.execute(
                """
                UPDATE user_progress
                SET quiz_attempts = ?,
                    last_quiz_score = ?,
                    mastery_score = ?,
                    weak_spots = ?,
                    updated_at = datetime('now')
                WHERE subject_id = ?
                """,
                (attempts, clamped_score, new_mastery, json.dumps(spots, ensure_ascii=False), subject_id),
            )
            updated = conn.execute("SELECT * FROM user_progress WHERE subject_id = ?", (subject_id,)).fetchone()
            res = dict(updated)
            res["completed_tasks"] = self._json_load(res.get("completed_tasks"), [])
            res["weak_spots"] = spots
            return res

    # ------------------------------------------------------------------
    # SQLite FTS5 Search, Search History, API Cache & Releases
    # ------------------------------------------------------------------

    @staticmethod
    def _build_fts_query(query: str) -> str:
        tokens = re.findall(r"\w+", (query or "").strip(), flags=re.UNICODE)
        if not tokens:
            return ""
        return " AND ".join(f'"{tok}"*' for tok in tokens[:10])

    def fts_search_sources(
        self,
        query: str,
        course_id: str | None = None,
        *,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Query local SQLite FTS5 `sources_fts` across all ingested sources."""
        fts_expr = self._build_fts_query(query)
        if not fts_expr:
            return []

        sql = """
            SELECT f.source_id,
                   f.course_id,
                   f.source_type,
                   f.title,
                   snippet(sources_fts, 4, '[', ']', '...', 24) AS snippet,
                   bm25(sources_fts) AS rank,
                   cs.origin_url,
                   cs.created_at
            FROM sources_fts f
            LEFT JOIN course_sources cs ON cs.id = f.source_id
            WHERE sources_fts MATCH ?
        """
        params: list[Any] = [fts_expr]
        if course_id:
            sql += " AND f.course_id = ?"
            params.append(course_id)
        sql += " ORDER BY rank ASC LIMIT ?"
        params.append(int(limit))

        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_cached_search(self, query_hash: str, *, max_age_seconds: int = 21600) -> list[dict[str, Any]] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT results_json,
                       (strftime('%s', 'now') - strftime('%s', created_at)) AS age_sec
                FROM source_search_history
                WHERE query_hash = ?
                LIMIT 1
                """,
                (query_hash,),
            ).fetchone()
            if row is None:
                return None
            age_sec = int(row["age_sec"] or 0)
            if age_sec > max_age_seconds:
                return None
            parsed = self._json_load(row["results_json"], None)
            return parsed if isinstance(parsed, list) else None

    def save_search_history(
        self,
        query: str,
        provider_filter: str,
        query_hash: str,
        results: list[dict[str, Any]],
    ) -> None:
        with self._write_lock, self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO source_search_history
                    (id, query, provider_filter, query_hash, results_json, added_source_count, created_at)
                VALUES (?, ?, ?, ?, ?, 0, datetime('now'))
                """,
                (
                    str(uuid.uuid4()),
                    query.strip(),
                    provider_filter.strip().lower(),
                    query_hash,
                    json.dumps(results, ensure_ascii=False),
                ),
            )

    def get_api_cache(self, prompt_hash: str, endpoint: str | None = None) -> Any | None:
        with self.connection() as conn:
            if endpoint:
                row = conn.execute(
                    "SELECT response_json FROM api_cache WHERE prompt_hash = ? AND endpoint = ?",
                    (prompt_hash, endpoint),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT response_json FROM api_cache WHERE prompt_hash = ?",
                    (prompt_hash,),
                ).fetchone()
            if row is None:
                return None
            return self._json_load(row["response_json"], None)

    def set_api_cache(
        self,
        prompt_hash: str,
        endpoint: str,
        response_data: Any,
        tokens_saved: int = 0,
    ) -> None:
        with self._write_lock, self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO api_cache (prompt_hash, endpoint, response_json, tokens_saved, created_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (prompt_hash, endpoint, json.dumps(response_data, ensure_ascii=False), int(tokens_saved)),
            )

    def get_latest_releases(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, version, platform, arch, filename, download_url, sha256,
                       size_bytes, is_latest, release_notes, created_at
                FROM app_releases
                WHERE is_latest = 1
                ORDER BY CASE platform
                    WHEN 'windows' THEN 1
                    WHEN 'macos' THEN 2
                    WHEN 'linux' THEN 3
                    WHEN 'android' THEN 4
                    ELSE 5
                END, created_at DESC
                """
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["is_latest"] = bool(item.get("is_latest", 1))
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Cloud Sync Snapshot Export / Import (Never includes private API keys)
    # ------------------------------------------------------------------

    @staticmethod
    def _to_json_str(val: Any, fallback: Any) -> str:
        if isinstance(val, str):
            return val
        return json.dumps(val if val is not None else fallback, ensure_ascii=False)

    @classmethod
    def _normalize_import_snapshot(cls, snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """Normalize either Worker D1 flat arrays or legacy nested course bundles into flat tables."""
        raw_courses = snapshot.get("courses") or []
        if raw_courses and isinstance(raw_courses[0], dict) and isinstance(raw_courses[0].get("course"), dict):
            courses: list[dict[str, Any]] = []
            window_rois: list[dict[str, Any]] = []
            course_sources: list[dict[str, Any]] = []
            subjects: list[dict[str, Any]] = []
            subject_details: list[dict[str, Any]] = []
            study_plans: list[dict[str, Any]] = []
            quizzes: list[dict[str, Any]] = []
            model_papers: list[dict[str, Any]] = []
            user_progress: list[dict[str, Any]] = []

            for bundle in raw_courses:
                c = bundle.get("course") or {}
                cid = str(c.get("id") or "").strip()
                if not cid:
                    continue
                courses.append(c)
                for r in bundle.get("rois") or bundle.get("window_rois") or []:
                    row = dict(r)
                    row.setdefault("course_id", cid)
                    window_rois.append(row)
                for src in bundle.get("sources") or []:
                    row = dict(src)
                    row.setdefault("course_id", cid)
                    course_sources.append(row)
                for sub in bundle.get("subjects") or []:
                    sid = str(sub.get("id") or "").strip()
                    if not sid:
                        continue
                    srow = dict(sub)
                    srow.setdefault("course_id", cid)
                    subjects.append(srow)
                    subject_details.append(
                        {
                            "subject_id": sid,
                            "key_concepts": sub.get("key_concepts") or [],
                            "math_formulas": sub.get("math_formulas") or [],
                        }
                    )
                    sp = sub.get("study_plan")
                    if isinstance(sp, dict):
                        study_plans.append(
                            {
                                "subject_id": sid,
                                "target_days": int(sp.get("target_days") or 14),
                                "daily_hours": float(sp.get("daily_hours") or 3.0),
                                "schedule_json": sp.get("schedule") or [],
                            }
                        )
                    elif isinstance(sp, list):
                        study_plans.append({"subject_id": sid, "schedule_json": sp})
                    if sub.get("quiz") is not None:
                        quizzes.append({"subject_id": sid, "questions_json": sub.get("quiz") or []})
                    mp = sub.get("model_paper")
                    if isinstance(mp, dict):
                        model_papers.append(
                            {
                                "subject_id": sid,
                                "title": mp.get("title") or f"{sub.get('title', 'Subject')} Exam",
                                "time_allowed": mp.get("time_allowed") or "60 Minutes",
                                "max_marks": int(mp.get("max_marks") or 25),
                                "questions_json": mp.get("questions") or [],
                            }
                        )
                    if any(
                        k in sub
                        for k in ("quiz_attempts", "last_quiz_score", "mastery_score", "completed_tasks", "weak_spots")
                    ):
                        user_progress.append(
                            {
                                "subject_id": sid,
                                "quiz_attempts": int(sub.get("quiz_attempts") or 0),
                                "last_quiz_score": float(sub.get("last_quiz_score") or 0.0),
                                "mastery_score": float(sub.get("mastery_score") or 0.0),
                                "completed_tasks": sub.get("completed_tasks") or [],
                                "weak_spots": sub.get("weak_spots") or [],
                            }
                        )
            return {
                "courses": courses,
                "window_rois": window_rois,
                "course_sources": course_sources,
                "subjects": subjects,
                "subject_details": subject_details,
                "study_plans": study_plans,
                "quizzes": quizzes,
                "model_papers": model_papers,
                "user_progress": user_progress,
            }

        return {
            "courses": [dict(x) for x in raw_courses if isinstance(x, dict)],
            "window_rois": [dict(x) for x in (snapshot.get("window_rois") or []) if isinstance(x, dict)],
            "course_sources": [dict(x) for x in (snapshot.get("course_sources") or []) if isinstance(x, dict)],
            "subjects": [dict(x) for x in (snapshot.get("subjects") or []) if isinstance(x, dict)],
            "subject_details": [dict(x) for x in (snapshot.get("subject_details") or []) if isinstance(x, dict)],
            "study_plans": [dict(x) for x in (snapshot.get("study_plans") or []) if isinstance(x, dict)],
            "quizzes": [dict(x) for x in (snapshot.get("quizzes") or []) if isinstance(x, dict)],
            "model_papers": [dict(x) for x in (snapshot.get("model_papers") or []) if isinstance(x, dict)],
            "user_progress": [dict(x) for x in (snapshot.get("user_progress") or []) if isinstance(x, dict)],
        }

    def export_workspace_snapshot(self, course_id: str | None = None) -> dict[str, Any]:
        """Export flat D1-compatible workspace tables (strictly excluding admin_users & api_keys)."""
        with self.connection() as conn:
            if course_id:
                courses = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM courses WHERE id = ? ORDER BY created_at DESC",
                        (course_id,),
                    ).fetchall()
                ]
                window_rois = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM window_rois WHERE course_id = ?",
                        (course_id,),
                    ).fetchall()
                ]
                course_sources = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM course_sources WHERE course_id = ? ORDER BY created_at ASC",
                        (course_id,),
                    ).fetchall()
                ]
                subjects = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM subjects WHERE course_id = ?",
                        (course_id,),
                    ).fetchall()
                ]
                sub_filter = "WHERE subject_id IN (SELECT id FROM subjects WHERE course_id = ?)"
                sub_params: tuple[Any, ...] = (course_id,)
            else:
                courses = [dict(r) for r in conn.execute("SELECT * FROM courses ORDER BY created_at DESC").fetchall()]
                window_rois = [dict(r) for r in conn.execute("SELECT * FROM window_rois").fetchall()]
                course_sources = [
                    dict(r) for r in conn.execute("SELECT * FROM course_sources ORDER BY created_at ASC").fetchall()
                ]
                subjects = [dict(r) for r in conn.execute("SELECT * FROM subjects").fetchall()]
                sub_filter = ""
                sub_params = ()

            subject_details = [
                dict(r) for r in conn.execute(f"SELECT * FROM subject_details {sub_filter}", sub_params).fetchall()
            ]
            study_plans = [
                dict(r) for r in conn.execute(f"SELECT * FROM study_plans {sub_filter}", sub_params).fetchall()
            ]
            quizzes = [
                dict(r) for r in conn.execute(f"SELECT * FROM quizzes {sub_filter}", sub_params).fetchall()
            ]
            model_papers = [
                dict(r) for r in conn.execute(f"SELECT * FROM model_papers {sub_filter}", sub_params).fetchall()
            ]
            user_progress = [
                dict(r) for r in conn.execute(f"SELECT * FROM user_progress {sub_filter}", sub_params).fetchall()
            ]

        return {
            "ok": True,
            "version": "1.0",
            "exported_at": int(time.time()),
            "courses": courses,
            "window_rois": window_rois,
            "course_sources": course_sources,
            "subjects": subjects,
            "subject_details": subject_details,
            "study_plans": study_plans,
            "quizzes": quizzes,
            "model_papers": model_papers,
            "user_progress": user_progress,
        }

    def import_workspace_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Idempotently merge a Worker D1 flat (or nested) workspace snapshot into local SQLite."""
        tables = self._normalize_import_snapshot(snapshot or {})
        counts: dict[str, int] = {}

        with self._write_lock, self.connection() as conn:
            for c in tables["courses"]:
                cid = str(c.get("id") or "").strip()
                if not cid:
                    continue
                conn.execute(
                    """
                    INSERT INTO courses (
                        id, title, video_name, video_duration, resolution, layout_type,
                        source_count, edition_origin, status, progress, current_stage, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        video_name = excluded.video_name,
                        video_duration = excluded.video_duration,
                        resolution = excluded.resolution,
                        layout_type = excluded.layout_type,
                        source_count = excluded.source_count,
                        edition_origin = excluded.edition_origin,
                        status = excluded.status,
                        progress = excluded.progress,
                        current_stage = excluded.current_stage
                    """,
                    (
                        cid,
                        str(c.get("title") or "Synced Course"),
                        str(c.get("video_name") or ""),
                        float(c.get("video_duration") or 0.0),
                        str(c.get("resolution") or "MULTI_SOURCE"),
                        str(c.get("layout_type") or "mixed"),
                        int(c.get("source_count") or 0),
                        str(c.get("edition_origin") or "web_lite"),
                        str(c.get("status") or "READY"),
                        int(c.get("progress") or 100),
                        str(c.get("current_stage") or "Ready"),
                        c.get("created_at"),
                    ),
                )
                counts["courses"] = counts.get("courses", 0) + 1

            for r in tables["window_rois"]:
                rid = str(r.get("id") or "").strip()
                cid = str(r.get("course_id") or "").strip()
                if not rid or not cid:
                    continue
                conn.execute(
                    """
                    INSERT INTO window_rois (id, course_id, roi_key, name, ymin, ymax, xmin, xmax, color)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        course_id = excluded.course_id,
                        roi_key = excluded.roi_key,
                        name = excluded.name,
                        ymin = excluded.ymin,
                        ymax = excluded.ymax,
                        xmin = excluded.xmin,
                        xmax = excluded.xmax,
                        color = excluded.color
                    """,
                    (
                        rid,
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

            for src in tables["course_sources"]:
                sid = str(src.get("id") or "").strip()
                cid = str(src.get("course_id") or "").strip()
                if not sid or not cid:
                    continue
                chunks_raw = src.get("structured_chunks_json")
                if chunks_raw is None:
                    chunks_raw = src.get("structured_chunks")
                assets_raw = src.get("visual_assets_json")
                if assets_raw is None:
                    assets_raw = src.get("visual_assets")
                links_raw = src.get("discovered_links_json")
                if links_raw is None:
                    links_raw = src.get("discovered_links")
                meta_raw = src.get("metadata_json")
                if meta_raw is None:
                    meta_raw = src.get("metadata")
                conn.execute(
                    """
                    INSERT INTO course_sources (
                        id, course_id, source_type, title, origin_url, search_query, file_hash,
                        storage_key, raw_text_excerpt, structured_chunks_json, visual_assets_json,
                        discovered_links_json, metadata_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
                    ON CONFLICT(id) DO UPDATE SET
                        course_id = excluded.course_id,
                        source_type = excluded.source_type,
                        title = excluded.title,
                        origin_url = excluded.origin_url,
                        search_query = excluded.search_query,
                        file_hash = excluded.file_hash,
                        storage_key = excluded.storage_key,
                        raw_text_excerpt = excluded.raw_text_excerpt,
                        structured_chunks_json = excluded.structured_chunks_json,
                        visual_assets_json = excluded.visual_assets_json,
                        discovered_links_json = excluded.discovered_links_json,
                        metadata_json = excluded.metadata_json,
                        status = excluded.status
                    """,
                    (
                        sid,
                        cid,
                        str(src.get("source_type") or "txt"),
                        str(src.get("title") or "Synced Source"),
                        str(src.get("origin_url") or ""),
                        str(src.get("search_query") or ""),
                        str(src.get("file_hash") or hashlib.sha256(sid.encode()).hexdigest()),
                        str(src.get("storage_key") or ""),
                        str(src.get("raw_text_excerpt") or ""),
                        self._to_json_str(chunks_raw, []),
                        self._to_json_str(assets_raw, []),
                        self._to_json_str(links_raw, []),
                        self._to_json_str(meta_raw, {}),
                        str(src.get("status") or "PARSED"),
                        src.get("created_at"),
                    ),
                )
                counts["course_sources"] = counts.get("course_sources", 0) + 1

            for sub in tables["subjects"]:
                sid = str(sub.get("id") or "").strip()
                cid = str(sub.get("course_id") or "").strip()
                if not sid or not cid:
                    continue
                conn.execute(
                    """
                    INSERT INTO subjects (
                        id, course_id, quadrant_key, title, short_title, topic, unit, instructor,
                        badge_color, summary, subtitles_transcript, slide_crops, source_citations, mermaid_diagram
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        course_id = excluded.course_id,
                        quadrant_key = excluded.quadrant_key,
                        title = excluded.title,
                        short_title = excluded.short_title,
                        topic = excluded.topic,
                        unit = excluded.unit,
                        instructor = excluded.instructor,
                        badge_color = excluded.badge_color,
                        summary = excluded.summary,
                        subtitles_transcript = excluded.subtitles_transcript,
                        slide_crops = excluded.slide_crops,
                        source_citations = excluded.source_citations,
                        mermaid_diagram = excluded.mermaid_diagram
                    """,
                    (
                        sid,
                        cid,
                        str(sub.get("quadrant_key") or "q1"),
                        str(sub.get("title") or "Subject"),
                        str(sub.get("short_title") or ""),
                        str(sub.get("topic") or ""),
                        str(sub.get("unit") or ""),
                        str(sub.get("instructor") or ""),
                        str(sub.get("badge_color") or "#3B82F6"),
                        str(sub.get("summary") or ""),
                        self._to_json_str(sub.get("subtitles_transcript"), []),
                        self._to_json_str(sub.get("slide_crops"), []),
                        self._to_json_str(sub.get("source_citations"), []),
                        str(sub.get("mermaid_diagram") or ""),
                    ),
                )
                counts["subjects"] = counts.get("subjects", 0) + 1

            for sd in tables["subject_details"]:
                sid = str(sd.get("subject_id") or "").strip()
                if not sid:
                    continue
                conn.execute(
                    """
                    INSERT INTO subject_details (subject_id, key_concepts, math_formulas)
                    VALUES (?, ?, ?)
                    ON CONFLICT(subject_id) DO UPDATE SET
                        key_concepts = excluded.key_concepts,
                        math_formulas = excluded.math_formulas
                    """,
                    (
                        sid,
                        self._to_json_str(sd.get("key_concepts"), []),
                        self._to_json_str(sd.get("math_formulas"), []),
                    ),
                )
                counts["subject_details"] = counts.get("subject_details", 0) + 1

            for sp in tables["study_plans"]:
                sid = str(sp.get("subject_id") or "").strip()
                if not sid:
                    continue
                sched_raw = sp.get("schedule_json")
                if sched_raw is None:
                    sched_raw = sp.get("schedule")
                conn.execute(
                    """
                    INSERT INTO study_plans (subject_id, target_days, daily_hours, schedule_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(subject_id) DO UPDATE SET
                        target_days = excluded.target_days,
                        daily_hours = excluded.daily_hours,
                        schedule_json = excluded.schedule_json
                    """,
                    (
                        sid,
                        int(sp.get("target_days") or 14),
                        float(sp.get("daily_hours") or 3.0),
                        self._to_json_str(sched_raw, []),
                    ),
                )
                counts["study_plans"] = counts.get("study_plans", 0) + 1

            for q in tables["quizzes"]:
                sid = str(q.get("subject_id") or "").strip()
                if not sid:
                    continue
                q_raw = q.get("questions_json")
                if q_raw is None:
                    q_raw = q.get("questions")
                conn.execute(
                    """
                    INSERT INTO quizzes (subject_id, questions_json)
                    VALUES (?, ?)
                    ON CONFLICT(subject_id) DO UPDATE SET
                        questions_json = excluded.questions_json
                    """,
                    (sid, self._to_json_str(q_raw, [])),
                )
                counts["quizzes"] = counts.get("quizzes", 0) + 1

            for mp in tables["model_papers"]:
                sid = str(mp.get("subject_id") or "").strip()
                if not sid:
                    continue
                mp_raw = mp.get("questions_json")
                if mp_raw is None:
                    mp_raw = mp.get("questions")
                conn.execute(
                    """
                    INSERT INTO model_papers (subject_id, title, time_allowed, max_marks, questions_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(subject_id) DO UPDATE SET
                        title = excluded.title,
                        time_allowed = excluded.time_allowed,
                        max_marks = excluded.max_marks,
                        questions_json = excluded.questions_json
                    """,
                    (
                        sid,
                        str(mp.get("title") or "Model Exam Paper"),
                        str(mp.get("time_allowed") or "60 Minutes"),
                        int(mp.get("max_marks") or 25),
                        self._to_json_str(mp_raw, []),
                    ),
                )
                counts["model_papers"] = counts.get("model_papers", 0) + 1

            for up in tables["user_progress"]:
                sid = str(up.get("subject_id") or "").strip()
                if not sid:
                    continue
                conn.execute(
                    """
                    INSERT INTO user_progress (
                        subject_id, quiz_attempts, last_quiz_score, mastery_score, completed_tasks, weak_spots, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
                    ON CONFLICT(subject_id) DO UPDATE SET
                        quiz_attempts = excluded.quiz_attempts,
                        last_quiz_score = excluded.last_quiz_score,
                        mastery_score = excluded.mastery_score,
                        completed_tasks = excluded.completed_tasks,
                        weak_spots = excluded.weak_spots,
                        updated_at = datetime('now')
                    """,
                    (
                        sid,
                        int(up.get("quiz_attempts") or 0),
                        float(up.get("last_quiz_score") or 0.0),
                        float(up.get("mastery_score") or 0.0),
                        self._to_json_str(up.get("completed_tasks"), []),
                        self._to_json_str(up.get("weak_spots"), []),
                        up.get("updated_at"),
                    ),
                )
                counts["user_progress"] = counts.get("user_progress", 0) + 1

        return {"ok": True, "imported_courses": counts.get("courses", 0), "imported": counts}


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "KEYRING_ACCOUNT",
    "KEYRING_SERVICE",
    "LocalDatabase",
    "SUPPORTED_PROVIDERS",
    "resolve_db_path",
    "sanitize_local_slide_key",
]
