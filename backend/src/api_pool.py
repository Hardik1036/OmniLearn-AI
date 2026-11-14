"""Encrypted Multi-API Key Pool with SHA-256 dedup, Least-Used routing & 65s 429 cooldowns."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

try:
    from .cache import kv_get, kv_put
    from .crypto_utils import aes_gcm_decrypt, aes_gcm_encrypt, get_master_secret, sha256_hex
    from .db import d1_all, d1_first, d1_run, env_get, env_str
    from .edition_guard import MAX_WEB_LITE_API_KEYS, is_web_lite
except ImportError:
    from cache import kv_get, kv_put
    from crypto_utils import aes_gcm_decrypt, aes_gcm_encrypt, get_master_secret, sha256_hex
    from db import d1_all, d1_first, d1_run, env_get, env_str
    from edition_guard import MAX_WEB_LITE_API_KEYS, is_web_lite

SUPPORTED_PROVIDERS = frozenset({"gemini", "groq", "openrouter"})
DEFAULT_COOLDOWN_SECONDS = 65

_FALLBACK_SECRET_SPECS: tuple[tuple[str, str], ...] = (
    ("GEMINI_API_KEY", "gemini"),
    *tuple((f"GEMINI_KEY_{i}", "gemini") for i in range(1, 11)),
    ("GROQ_API_KEY", "groq"),
    *tuple((f"GROQ_KEY_{i}", "groq") for i in range(1, 11)),
    ("OPENROUTER_API_KEY", "openrouter"),
    *tuple((f"OPENROUTER_KEY_{i}", "openrouter") for i in range(1, 11)),
)
_MULTI_FALLBACK_SPECS: tuple[tuple[str, str], ...] = (
    ("GEMINI_API_KEYS", "gemini"),
    ("GROQ_API_KEYS", "groq"),
    ("OPENROUTER_API_KEYS", "openrouter"),
)


class EditionLimitError(Exception):
    """Raised when adding an API key exceeds Web Lite slot quota."""

    def __init__(self, feature: str, message: str) -> None:
        super().__init__(message)
        self.feature = feature
        self.message = message


def validate_gemini_key_format(key: str) -> tuple[bool, str | None]:
    """Check if a Gemini API key matches standard format (`AIza...`).
    Returns `(is_valid, warning_message)`.
    Never returns any plaintext characters of the key itself.
    """
    clean = (key or "").strip()
    if not clean:
        return True, None
    if not clean.startswith("AIza"):
        if clean.startswith("AQ.") or clean.startswith("ya29."):
            return False, "Gemini key appears to be a Google OAuth/access token, not an AI Studio API key starting with 'AIza'."
        return False, "Gemini API key should start with 'AIza' (from Google AI Studio)."
    return True, None


def validate_api_key_input(api_key: str, provider: str = "gemini") -> tuple[str, str]:
    """Validate and normalize API key text and provider name."""
    clean_key = (api_key or "").strip()
    if len(clean_key) < 10 or any(ch.isspace() for ch in clean_key):
        raise ValueError("Invalid API key format")
    clean_provider = (provider or "gemini").strip().lower()
    if clean_provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported API provider: {clean_provider}")
    return clean_key, clean_provider


async def add_personal_api_key(
    env: Any,
    api_key: str,
    provider: str = "gemini",
    *,
    owner_sub: str = "admin",
    is_default: bool = False,
) -> dict[str, Any]:
    """Deduplicate, enforce Web Lite slot cap, AES-256-GCM encrypt, and store key in D1."""
    clean_key, clean_provider = validate_api_key_input(api_key, provider)
    key_hash = await sha256_hex(clean_key)
    clean_owner = (owner_sub or "admin").strip() or "admin"
    default_flag = 1 if is_default else 0
    if default_flag:
        clean_owner = "system_default"

    existing = await d1_first(
        env,
        "SELECT id, provider, status, is_default FROM api_keys WHERE owner_sub = ? AND key_hash = ?",
        (clean_owner, key_hash),
    )
    if existing:
        if existing.get("status") == "INVALID":
            await d1_run(
                env,
                "UPDATE api_keys SET status = 'ACTIVE', cooldown_until = 0, error_count = 0 WHERE id = ?",
                (existing["id"],),
            )
        return {
            "ok": True,
            "deduplicated": True,
            "slot_id": str(existing["id"]),
            "provider": str(existing["provider"]),
            "status": "ACTIVE",
            "is_default": bool(existing.get("is_default") or 0),
        }

    if is_web_lite(env) and not default_flag:
        count_row = await d1_first(
            env,
            "SELECT COUNT(*) AS cnt FROM api_keys WHERE is_default = 0 AND owner_sub = ? AND status != 'INVALID'",
            (clean_owner,),
        )
        current_count = int((count_row or {}).get("cnt") or 0)
        if current_count >= MAX_WEB_LITE_API_KEYS:
            raise EditionLimitError(
                "multi_api_pool",
                "Web Lite caps the API pool at 1 key slot. Download OmniLearn System App for unlimited Multi-API rotation.",
            )

    master_secret = get_master_secret(env)
    encrypted_key, iv = await aes_gcm_encrypt(clean_key, master_secret)
    slot_id = str(uuid.uuid4())

    await d1_run(
        env,
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


async def remove_api_key(
    env: Any,
    slot_id: str,
    *,
    owner_sub: str | None = None,
) -> bool:
    """Remove an encrypted personal API key slot by ID from D1 `api_keys`."""
    clean_id = (slot_id or "").strip()
    if not clean_id:
        return False
    existing = await d1_first(
        env,
        "SELECT id, owner_sub, is_default FROM api_keys WHERE id = ?",
        (clean_id,),
    )
    if not existing:
        return False
    if int(existing.get("is_default") or 0) == 1:
        raise PermissionError("Default system keys are managed via environment secrets and overridden by personal keys.")
    if owner_sub is not None and str(existing.get("owner_sub") or "") != owner_sub:
        return False
    await d1_run(env, "DELETE FROM api_keys WHERE id = ?", (clean_id,))
    return True


def _get_fallback_env_secrets(env: Any) -> list[tuple[str, str, str]]:
    """Return configured Worker secret fallbacks `(secret_id, provider, secret_value)`."""
    found: list[tuple[str, str, str]] = []
    seen_values: set[str] = set()
    for var_name, provider in _FALLBACK_SECRET_SPECS:
        val = env_str(env, var_name).strip()
        if val and val not in seen_values:
            seen_values.add(val)
            found.append((f"env:{var_name}", provider, val))
    for var_name, provider in _MULTI_FALLBACK_SPECS:
        raw_list = env_str(env, var_name).strip()
        if not raw_list:
            continue
        for idx, part in enumerate(re.split(r"[,;\s]+", raw_list), start=1):
            piece = part.strip()
            if piece and piece not in seen_values:
                seen_values.add(piece)
                found.append((f"env:{var_name}_{idx}", provider, piece))
    return found


async def get_pool_summary(env: Any, owner_sub: str | None = None) -> dict[str, Any]:
    """Return strictly anonymous pool slot statuses — ZERO characters of any API key."""
    now = int(time.time())
    clean_owner = owner_sub.strip() if isinstance(owner_sub, str) and owner_sub.strip() else None
    rows: list[dict[str, Any]] = []
    if env_get(env, "DB", None) is not None:
        # Deliberately omit encrypted_key, iv, and key_hash from the SELECT list.
        if clean_owner is not None:
            rows = await d1_all(
                env,
                """
                SELECT id, owner_sub, is_default, provider, status, cooldown_until,
                       request_count, error_count, last_used_at, created_at
                FROM api_keys
                WHERE (is_default = 0 AND owner_sub = ?) OR is_default = 1
                ORDER BY is_default ASC, created_at ASC, id ASC
                """,
                (clean_owner,),
            )
        else:
            rows = await d1_all(
                env,
                """
                SELECT id, owner_sub, is_default, provider, status, cooldown_until,
                       request_count, error_count, last_used_at, created_at
                FROM api_keys
                ORDER BY is_default ASC, created_at ASC, id ASC
                """,
            )

    personal_total = 0
    personal_active = 0
    default_db_total = 0
    default_db_active = 0
    parsed: list[tuple[dict[str, Any], str, int]] = []
    for row in rows:
        is_def = int(row.get("is_default") or 0) == 1
        status = str(row.get("status") or "ACTIVE").upper()
        cooldown_until = int(row.get("cooldown_until") or 0)
        cooldown_remaining = 0
        if status == "COOLDOWN":
            if cooldown_until <= now:
                status = "ACTIVE"
            else:
                cooldown_remaining = cooldown_until - now
        parsed.append((row, status, cooldown_remaining))
        if is_def:
            if status != "INVALID":
                default_db_total += 1
            if status == "ACTIVE":
                default_db_active += 1
        else:
            if status != "INVALID":
                personal_total += 1
            if status == "ACTIVE":
                personal_active += 1

    fallback_secrets = _get_fallback_env_secrets(env)
    default_total = default_db_total + len(fallback_secrets)
    default_active = default_db_active + len(fallback_secrets)
    has_personal_override = personal_total > 0
    using_source = "personal" if has_personal_override else ("default_env" if default_total > 0 else "none")

    slots: list[dict[str, Any]] = []
    for idx, (row, status, cooldown_remaining) in enumerate(parsed):
        is_def = int(row.get("is_default") or 0) == 1
        req_count = int(row.get("request_count") or 0)
        slots.append(
            {
                "id": str(row["id"]),
                "slot": idx + 1,
                "provider": str(row.get("provider") or "gemini"),
                "status": status,
                "request_count": req_count,
                "requests_served": req_count,
                "error_count": int(row.get("error_count") or 0),
                "cooldown_remaining": cooldown_remaining,
                "is_default": is_def,
                "source": "env_default" if is_def else "personal",
                "active_in_routing": (not is_def) if has_personal_override else is_def,
                "overridden_by_personal": bool(is_def and has_personal_override),
            }
        )

    for sec_id, sec_provider, _sec_val in fallback_secrets:
        slots.append(
            {
                "id": sec_id,
                "slot": len(slots) + 1,
                "provider": sec_provider,
                "status": "ACTIVE",
                "request_count": 0,
                "requests_served": 0,
                "error_count": 0,
                "cooldown_remaining": 0,
                "is_default": True,
                "source": "env_default",
                "active_in_routing": not has_personal_override,
                "overridden_by_personal": has_personal_override,
            }
        )

    invalid_format_warnings: list[str] = []
    for sec_id, sec_provider, sec_val in fallback_secrets:
        if sec_provider == "gemini":
            is_valid, warn_msg = validate_gemini_key_format(sec_val)
            if not is_valid and warn_msg:
                invalid_format_warnings.append(f"[{sec_id}] {warn_msg}")

    return {
        "ok": True,
        "edition": "web_lite" if is_web_lite(env) else "system_app",
        "max_slots": MAX_WEB_LITE_API_KEYS if is_web_lite(env) else None,
        "using_source": using_source,
        "personal_keys_count": personal_total,
        "default_keys_count": default_total,
        "total_active_keys": personal_active if has_personal_override else default_active,
        "total_db_keys": personal_total,
        "fallback_secrets_configured": default_total,
        "slots": slots,
        "invalid_format_warnings": invalid_format_warnings,
    }


async def _is_env_secret_in_cooldown(env: Any, secret_id: str, now: int) -> bool:
    raw = await kv_get(env, f"pool:cd:{secret_id}")
    if not raw:
        return False
    try:
        return int(raw) > now
    except ValueError:
        return False


async def get_candidate_keys(
    env: Any,
    provider: str | None = None,
    owner_sub: str | None = None,
) -> list[dict[str, Any]]:
    """Select active keys: user personal keys override `.env` / secret defaults when present."""
    now = int(time.time())
    clean_owner = owner_sub.strip() if isinstance(owner_sub, str) and owner_sub.strip() else None
    candidates: list[dict[str, Any]] = []
    seen_plaintext: set[str] = set()
    master_secret = get_master_secret(env)
    use_personal_only = False

    if env_get(env, "DB", None) is not None:
        await d1_run(
            env,
            "UPDATE api_keys SET status = 'ACTIVE', cooldown_until = 0 WHERE status = 'COOLDOWN' AND cooldown_until <= ?",
            (now,),
        )
        if clean_owner is not None:
            p_row = await d1_first(
                env,
                "SELECT COUNT(*) AS cnt FROM api_keys WHERE is_default = 0 AND status != 'INVALID' AND owner_sub = ?",
                (clean_owner,),
            )
        else:
            p_row = await d1_first(
                env,
                "SELECT COUNT(*) AS cnt FROM api_keys WHERE is_default = 0 AND status != 'INVALID'",
            )
        use_personal_only = bool(p_row and int(p_row.get("cnt") or 0) > 0)

        where = ["status = 'ACTIVE'", "(cooldown_until IS NULL OR cooldown_until <= ?)"]
        params: list[Any] = [now]
        if use_personal_only:
            where.append("is_default = 0")
            if clean_owner is not None:
                where.append("owner_sub = ?")
                params.append(clean_owner)
        else:
            where.append("is_default = 1")
        if provider:
            where.append("provider = ?")
            params.append(provider)

        rows = await d1_all(
            env,
            f"""
            SELECT id, provider, encrypted_key, iv, last_used_at, request_count
            FROM api_keys
            WHERE {' AND '.join(where)}
            ORDER BY last_used_at ASC, request_count ASC, id ASC
            """,
            tuple(params),
        )

        for row in rows:
            try:
                decrypted = await aes_gcm_decrypt(
                    str(row["encrypted_key"]),
                    str(row["iv"]),
                    master_secret,
                )
            except Exception:
                continue
            if decrypted and decrypted not in seen_plaintext:
                seen_plaintext.add(decrypted)
                candidates.append(
                    {
                        "id": str(row["id"]),
                        "provider": str(row.get("provider") or "gemini"),
                        "api_key": decrypted,
                        "source": "db" if use_personal_only else "env_default",
                    }
                )

    if not use_personal_only:
        for secret_id, sec_provider, sec_val in _get_fallback_env_secrets(env):
            if provider and sec_provider != provider:
                continue
            if sec_val in seen_plaintext:
                continue
            if await _is_env_secret_in_cooldown(env, secret_id, now):
                continue
            seen_plaintext.add(sec_val)
            candidates.append(
                {
                    "id": secret_id,
                    "provider": sec_provider,
                    "api_key": sec_val,
                    "source": "env",
                }
            )

    # Prioritize keys with valid format (e.g. real AIza keys, Groq gsk_, OpenRouter sk-or-)
    # over keys that fail format validation (such as AQ. OAuth tokens pasted as Gemini API keys).
    candidates.sort(
        key=lambda c: 1 if (c.get("provider") == "gemini" and not str(c.get("api_key") or "").startswith("AIza")) else 0
    )
    return candidates


async def mark_key_used(env: Any, key_id: str | None) -> None:
    """Record successful API usage and update Round-Robin / Least-Used timestamp."""
    if not key_id or key_id.startswith("env:") or env_get(env, "DB", None) is None:
        return
    now = int(time.time())
    await d1_run(
        env,
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


async def mark_key_cooldown(
    env: Any,
    key_id: str | None,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    is_invalid: bool = False,
) -> None:
    """Put a rate-limited key into 65s COOLDOWN or mark an invalid key as INVALID."""
    if not key_id:
        return
    now = int(time.time())
    if key_id.startswith("env:"):
        ttl = max(60, int(cooldown_seconds))
        await kv_put(env, f"pool:cd:{key_id}", str(now + ttl), expiration_ttl=ttl)
        return

    if env_get(env, "DB", None) is None:
        return

    if is_invalid:
        await d1_run(
            env,
            "UPDATE api_keys SET status = 'INVALID', error_count = error_count + 1 WHERE id = ?",
            (key_id,),
        )
    else:
        until = now + int(cooldown_seconds)
        await d1_run(
            env,
            """
            UPDATE api_keys
            SET status = 'COOLDOWN',
                cooldown_until = ?,
                error_count = error_count + 1,
                last_used_at = ?
            WHERE id = ?
            """,
            (until, now, key_id),
        )
