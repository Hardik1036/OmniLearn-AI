"""Standard-library .env configuration loader for OmniLearn AI System App.

Allows administrators to configure their Admin User ID, Admin Master Password,
and Multi-API provider keys directly in `.env` at the project root without
ever exposing key secrets through HTTP endpoints or frontend bundles.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ENV_ADMIN_USER_KEY = "OMNILEARN_ADMIN_USERNAME"
ENV_ADMIN_PASS_KEY = "OMNILEARN_ADMIN_PASSWORD"
ENV_REQUIRE_INTERNET_KEY = "OMNILEARN_REQUIRE_INTERNET"
ENV_GOOGLE_CLIENT_ID_KEY = "GOOGLE_CLIENT_ID"
ENV_GOOGLE_CLIENT_SECRET_KEY = "GOOGLE_CLIENT_SECRET"
ENV_GOOGLE_REDIRECT_URI_KEY = "GOOGLE_REDIRECT_URI"
ENV_CLOUD_URL_KEY = "OMNILEARN_CLOUD_URL"
ENV_CLOUD_TOKEN_KEY = "OMNILEARN_CLOUD_TOKEN"
ENV_SYNC_TOKEN_KEY = "OMNILEARN_SYNC_TOKEN"

_SINGLE_KEY_SPECS: tuple[tuple[str, str], ...] = (
    ("GEMINI_API_KEY", "gemini"),
    *tuple((f"GEMINI_KEY_{idx}", "gemini") for idx in range(1, 11)),
    ("GROQ_API_KEY", "groq"),
    *tuple((f"GROQ_KEY_{idx}", "groq") for idx in range(1, 11)),
    ("OPENROUTER_API_KEY", "openrouter"),
    *tuple((f"OPENROUTER_KEY_{idx}", "openrouter") for idx in range(1, 11)),
)

_MULTI_KEY_SPECS: tuple[tuple[str, str], ...] = (
    ("GEMINI_API_KEYS", "gemini"),
    ("GROQ_API_KEYS", "groq"),
    ("OPENROUTER_API_KEYS", "openrouter"),
)


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def find_env_file() -> Path | None:
    """Locate `.env` next to executable (frozen), project root, or CWD."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")
    project_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(project_root / ".env")
    candidates.append(Path.cwd() / ".env")
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from a `.env` file using pure stdlib rules."""
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        clean_key = key.strip()
        if not clean_key:
            continue
        clean_val = val.strip()
        if len(clean_val) >= 2 and (
            (clean_val.startswith('"') and clean_val.endswith('"'))
            or (clean_val.startswith("'") and clean_val.endswith("'"))
        ):
            clean_val = clean_val[1:-1]
        elif "#" in clean_val:
            clean_val = clean_val.split("#", 1)[0].strip()
        parsed[clean_key] = clean_val
    return parsed


def load_dotenv_file(env_path: Path | None = None, *, override: bool = False) -> Path | None:
    """Load `.env` variables into `os.environ` unless disabled via `OMNILEARN_IGNORE_DOTENV`."""
    if env_path is None and _truthy(os.environ.get("OMNILEARN_IGNORE_DOTENV")):
        return None
    target = env_path or find_env_file()
    if target is None or not target.is_file():
        return None
    values = parse_env_file(target)
    for key, val in values.items():
        if override or not os.environ.get(key):
            os.environ[key] = val
    return target


def is_internet_required() -> bool:
    """Return True when active internet connectivity & AI API access are enforced."""
    raw = os.environ.get(ENV_REQUIRE_INTERNET_KEY)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def get_env_admin_credentials() -> tuple[str, str] | None:
    """Return `(username, password)` configured in `.env` / environment if non-empty."""
    username = os.environ.get(ENV_ADMIN_USER_KEY, "").strip()
    password = os.environ.get(ENV_ADMIN_PASS_KEY, "")
    if not username or not password or len(password) < 4:
        return None
    return username, password


def get_configured_env_api_keys() -> list[tuple[str, str, str]]:
    """Collect deduplicated `(slot_label, provider, raw_key)` tuples from env/.env."""
    found: list[tuple[str, str, str]] = []
    seen_values: set[str] = set()

    for var_name, provider in _SINGLE_KEY_SPECS:
        raw_val = os.environ.get(var_name, "").strip()
        if raw_val and raw_val not in seen_values:
            seen_values.add(raw_val)
            found.append((f"env:{var_name}", provider, raw_val))

    for var_name, provider in _MULTI_KEY_SPECS:
        raw_list = os.environ.get(var_name, "").strip()
        if not raw_list:
            continue
        parts = [p.strip() for p in re.split(r"[,;\s]+", raw_list) if p.strip()]
        for idx, piece in enumerate(parts, start=1):
            if piece not in seen_values:
                seen_values.add(piece)
                found.append((f"env:{var_name}_{idx}", provider, piece))

    return found


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


def get_gemini_format_warnings() -> list[str]:
    """Inspect all configured Gemini env keys and return non-leaking warning messages."""
    warnings: list[str] = []
    keys = get_configured_env_api_keys()
    for slot_label, provider, raw_key in keys:
        if provider == "gemini":
            valid, msg = validate_gemini_key_format(raw_key)
            if not valid and msg:
                warnings.append(f"[{slot_label}] {msg}")
    return warnings


def get_google_oauth_config() -> dict[str, object]:
    """Return Google OAuth settings loaded from `.env` / environment."""
    client_id = os.environ.get(ENV_GOOGLE_CLIENT_ID_KEY, "").strip()
    client_secret = os.environ.get(ENV_GOOGLE_CLIENT_SECRET_KEY, "").strip()
    redirect_uri = os.environ.get(ENV_GOOGLE_REDIRECT_URI_KEY, "").strip()
    return {
        "enabled": bool(client_id),
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "oauth_code_flow_enabled": bool(client_id and client_secret),
    }


def get_cloud_relay_config() -> dict[str, str]:
    """Return optional Cloudflare Worker URL & relay/sync token from environment."""
    cloud_url = os.environ.get(ENV_CLOUD_URL_KEY, "").strip().rstrip("/")
    token = (
        os.environ.get(ENV_CLOUD_TOKEN_KEY, "").strip()
        or os.environ.get(ENV_SYNC_TOKEN_KEY, "").strip()
    )
    return {
        "cloud_url": cloud_url,
        "cloud_token": token,
    }
