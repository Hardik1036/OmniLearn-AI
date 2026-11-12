"""Cryptographic utilities using Web Crypto (`js.crypto.subtle`) with CPython fallbacks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

try:
    from .db import env_str
except ImportError:
    from db import env_str

try:
    import js  # type: ignore
    from pyodide.ffi import to_js  # type: ignore

    HAS_WEB_CRYPTO = hasattr(js, "crypto") and hasattr(js.crypto, "subtle")
except ImportError:
    js = None  # type: ignore
    to_js = None  # type: ignore
    HAS_WEB_CRYPTO = False

# Generated once per isolate/process if MASTER_SECRET is omitted in local/unit-test mocks.
_EPHEMERAL_DEV_SECRET = secrets.token_urlsafe(32)


def get_master_secret(env: Any = None) -> str:
    """Resolve the AES-GCM & JWT signing secret from Worker secrets or process env."""
    secret = env_str(env, "MASTER_SECRET") or env_str(env, "JWT_SECRET")
    if secret:
        return secret
    env_secret = os.environ.get("MASTER_SECRET") or os.environ.get("JWT_SECRET")
    if env_secret:
        return env_secret
    return _EPHEMERAL_DEV_SECRET


def b64_encode(data: bytes) -> str:
    """Standard Base64 ASCII encoding."""
    return base64.b64encode(data).decode("ascii")


def b64_decode(text: str) -> bytes:
    """Standard Base64 decoding."""
    return base64.b64decode(text.encode("ascii"))


def b64u_encode(data: bytes) -> str:
    """URL-safe Base64 encoding without padding (JWT standard)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """URL-safe Base64 decoding restoring omitted padding."""
    pad_len = (-len(text)) % 4
    return base64.urlsafe_b64decode((text + ("=" * pad_len)).encode("ascii"))


def random_bytes(nbytes: int = 16) -> bytes:
    """Cryptographically secure random bytes."""
    return secrets.token_bytes(nbytes)


def random_hex(nbytes: int = 16) -> str:
    """Cryptographically secure random lowercase hex string."""
    return secrets.token_hex(nbytes)


def constant_time_compare(a: str | bytes, b: str | bytes) -> bool:
    """Timing-safe string or bytes equality comparison."""
    a_bytes = a.encode("utf-8") if isinstance(a, str) else a
    b_bytes = b.encode("utf-8") if isinstance(b, str) else b
    return hmac.compare_digest(a_bytes, b_bytes)


def _js_obj(mapping: dict[str, Any]) -> Any:
    return to_js(mapping, dict_converter=js.Object.fromEntries)


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


def sha256_hex_sync(data: str | bytes) -> str:
    """Synchronous SHA-256 hex digest using stdlib hashlib."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


async def sha256_bytes(data: str | bytes) -> bytes:
    """Compute raw 32-byte SHA-256 digest via Web Crypto or hashlib."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if HAS_WEB_CRYPTO:
        buf = await js.crypto.subtle.digest("SHA-256", _to_uint8_array(raw))
        return _from_array_buffer(buf)
    return hashlib.sha256(raw).digest()


async def sha256_hex(data: str | bytes) -> str:
    """Compute SHA-256 lowercase hex digest via Web Crypto or hashlib."""
    digest = await sha256_bytes(data)
    return digest.hex()


def _parse_salt_bytes(salt: str) -> bytes:
    if len(salt) % 2 == 0:
        try:
            return bytes.fromhex(salt)
        except ValueError:
            pass
    return salt.encode("utf-8")


async def pbkdf2_hash(
    password: str,
    salt_hex: str | None = None,
    iterations: int = 100_000,
) -> tuple[str, str]:
    """Derive a 256-bit PBKDF2-SHA256 hash and return `(hash_hex, salt_hex)`."""
    if not password:
        raise ValueError("Password must not be empty")
    salt = salt_hex if salt_hex is not None else random_hex(16)
    pwd_bytes = password.encode("utf-8")
    salt_bytes = _parse_salt_bytes(salt)

    if HAS_WEB_CRYPTO:
        base_key = await js.crypto.subtle.importKey(
            "raw",
            _to_uint8_array(pwd_bytes),
            _js_obj({"name": "PBKDF2"}),
            False,
            to_js(["deriveBits"]),
        )
        derived = await js.crypto.subtle.deriveBits(
            _js_obj(
                {
                    "name": "PBKDF2",
                    "salt": _to_uint8_array(salt_bytes),
                    "iterations": int(iterations),
                    "hash": "SHA-256",
                }
            ),
            base_key,
            256,
        )
        return _from_array_buffer(derived).hex(), salt

    digest = hashlib.pbkdf2_hmac("sha256", pwd_bytes, salt_bytes, int(iterations), dklen=32)
    return digest.hex(), salt


async def pbkdf2_verify(
    password: str,
    expected_hash_hex: str,
    salt_hex: str,
    iterations: int = 100_000,
) -> bool:
    """Verify a password against a stored PBKDF2-SHA256 hex digest."""
    if not password or not expected_hash_hex or not salt_hex:
        return False
    computed_hex, _ = await pbkdf2_hash(password, salt_hex=salt_hex, iterations=iterations)
    return constant_time_compare(computed_hex.lower(), expected_hash_hex.lower())


async def _derive_aes_key_bytes(master_secret: str) -> bytes:
    return await sha256_bytes(master_secret.encode("utf-8"))


async def aes_gcm_encrypt(plaintext: str, master_secret: str) -> tuple[str, str]:
    """Encrypt plaintext using AES-256-GCM with a random 96-bit IV."""
    iv = random_bytes(12)
    pt_bytes = plaintext.encode("utf-8")

    if HAS_WEB_CRYPTO:
        raw_key = await _derive_aes_key_bytes(master_secret)
        crypto_key = await js.crypto.subtle.importKey(
            "raw",
            _to_uint8_array(raw_key),
            _js_obj({"name": "AES-GCM"}),
            False,
            to_js(["encrypt"]),
        )
        ct_buf = await js.crypto.subtle.encrypt(
            _js_obj({"name": "AES-GCM", "iv": _to_uint8_array(iv), "tagLength": 128}),
            crypto_key,
            _to_uint8_array(pt_bytes),
        )
        return b64_encode(_from_array_buffer(ct_buf)), b64_encode(iv)

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise RuntimeError("`cryptography` package is required for CPython AES-GCM") from exc

    key_bytes = await _derive_aes_key_bytes(master_secret)
    ct_bytes = AESGCM(key_bytes).encrypt(iv, pt_bytes, None)
    return b64_encode(ct_bytes), b64_encode(iv)


async def aes_gcm_decrypt(ciphertext_b64: str, iv_b64: str, master_secret: str) -> str:
    """Decrypt Base64 AES-256-GCM ciphertext and 96-bit IV back to plaintext."""
    ct_bytes = b64_decode(ciphertext_b64)
    iv_bytes = b64_decode(iv_b64)

    if HAS_WEB_CRYPTO:
        raw_key = await _derive_aes_key_bytes(master_secret)
        crypto_key = await js.crypto.subtle.importKey(
            "raw",
            _to_uint8_array(raw_key),
            _js_obj({"name": "AES-GCM"}),
            False,
            to_js(["decrypt"]),
        )
        pt_buf = await js.crypto.subtle.decrypt(
            _js_obj({"name": "AES-GCM", "iv": _to_uint8_array(iv_bytes), "tagLength": 128}),
            crypto_key,
            _to_uint8_array(ct_bytes),
        )
        return _from_array_buffer(pt_buf).decode("utf-8")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise RuntimeError("`cryptography` package is required for CPython AES-GCM") from exc

    key_bytes = await _derive_aes_key_bytes(master_secret)
    pt_bytes = AESGCM(key_bytes).decrypt(iv_bytes, ct_bytes, None)
    return pt_bytes.decode("utf-8")


async def _hmac_sha256(secret: str, message: bytes) -> bytes:
    secret_bytes = secret.encode("utf-8")
    if HAS_WEB_CRYPTO:
        key = await js.crypto.subtle.importKey(
            "raw",
            _to_uint8_array(secret_bytes),
            _js_obj({"name": "HMAC", "hash": {"name": "SHA-256"}}),
            False,
            to_js(["sign"]),
        )
        sig_buf = await js.crypto.subtle.sign(
            "HMAC",
            key,
            _to_uint8_array(message),
        )
        return _from_array_buffer(sig_buf)
    return hmac.new(secret_bytes, message, hashlib.sha256).digest()


async def jwt_sign(
    payload: dict[str, Any],
    secret: str,
    expires_in_seconds: int = 86400,
) -> str:
    """Create a compact HS256 JWT token."""
    now = int(time.time())
    claims = dict(payload)
    claims.setdefault("iat", now)
    claims.setdefault("exp", now + int(expires_in_seconds))

    header_b64 = b64u_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    claims_b64 = b64u_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{claims_b64}"
    sig = await _hmac_sha256(secret, signing_input.encode("ascii"))
    return f"{signing_input}.{b64u_encode(sig)}"


async def jwt_verify(token: str, secret: str) -> dict[str, Any] | None:
    """Verify an HS256 JWT signature and expiration, returning claims or None."""
    if not token or not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    header_b64, claims_b64, sig_b64 = parts
    try:
        header = json.loads(b64u_decode(header_b64).decode("utf-8"))
        if not isinstance(header, dict) or header.get("alg") != "HS256":
            return None
        claims = json.loads(b64u_decode(claims_b64).decode("utf-8"))
        if not isinstance(claims, dict):
            return None
        provided_sig = b64u_decode(sig_b64)
    except Exception:
        return None

    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    expected_sig = await _hmac_sha256(secret, signing_input)
    if not hmac.compare_digest(expected_sig, provided_sig):
        return None

    exp = claims.get("exp")
    if exp is not None:
        try:
            if int(time.time()) >= int(exp):
                return None
        except (TypeError, ValueError):
            return None
    return claims
