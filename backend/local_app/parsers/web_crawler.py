"""SSRF-guarded web crawler and website PDF link harvester."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from backend.local_app.parsers import FileValidationError, NormalizedSourcePayload, SSRFBlockedError
from backend.local_app.parsers.doc_parsers import (
    MAX_PDF_BYTES,
    parse_pdf,
    safe_filename,
    sanitize_plain_text,
)

MAX_HTML_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_REDIRECTS = 5
DEFAULT_USER_AGENT = "OmniLearn-SystemApp/1.0 (+https://omnilearn.local)"

_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
}


def _assert_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str) -> None:
    """Reject any loopback, private, link-local, CGNAT, multicast, reserved, or unspecified IP."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        _assert_public_ip(ip_obj.ipv4_mapped, host)
        return

    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or (isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NETWORK)
    ):
        raise SSRFBlockedError(
            f"Blocked non-public IP address '{ip_obj}' resolved for host '{host}'."
        )


def validate_public_http_url(url: str) -> str:
    """Validate that a URL is strictly public http(s) and resolves only to public IPs."""
    raw = (url or "").strip()
    if not raw:
        raise SSRFBlockedError("URL cannot be empty.")

    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SSRFBlockedError(f"Blocked unsupported URL scheme '{parsed.scheme or 'none'}'.")

    if parsed.username or parsed.password:
        raise SSRFBlockedError("Blocked URL containing embedded userinfo credentials.")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise SSRFBlockedError("URL is missing a valid hostname.")

    if (
        hostname in _BLOCKED_HOSTNAMES
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
    ):
        raise SSRFBlockedError(f"Blocked internal/localhost hostname '{hostname}'.")

    # Fast check for IP literals before DNS resolution.
    stripped_ip = hostname.strip("[]")
    try:
        ip_literal = ipaddress.ip_address(stripped_ip)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        _assert_public_ip(ip_literal, hostname)
        return raw

    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"Could not resolve hostname '{hostname}': {exc}") from exc

    if not addr_infos:
        raise SSRFBlockedError(f"Hostname '{hostname}' resolved to zero addresses.")

    for _family, _socktype, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0].split("%", 1)[0]
        ip_obj = ipaddress.ip_address(ip_str)
        _assert_public_ip(ip_obj, hostname)

    return raw


def _is_pdf_link(resolved_url: str, mime_hint: str = "") -> bool:
    if "application/pdf" in (mime_hint or "").lower():
        return True
    parsed = urlsplit(resolved_url)
    path_lower = parsed.path.lower()
    return path_lower.endswith(".pdf")


def extract_pdfs_from_html(html: str, base_url: str) -> list[dict[str, str]]:
    """Discover all `.pdf` links across `<a>`, `<iframe>`, `<embed>`, and `<object>` tags."""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    discovered: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    candidates: list[tuple[str, str, str]] = []
    for tag in soup.find_all("a", href=True):
        candidates.append((tag["href"], tag.get_text(" ", strip=True), tag.get("type", "")))
    for tag in soup.find_all("iframe", src=True):
        candidates.append((tag["src"], tag.get("title", ""), ""))
    for tag in soup.find_all("embed", src=True):
        candidates.append((tag["src"], tag.get("title", ""), tag.get("type", "")))
    for tag in soup.find_all("object", data=True):
        candidates.append((tag["data"], tag.get("title", ""), tag.get("type", "")))

    for raw_href, label, mime_hint in candidates:
        href = (raw_href or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        resolved = urljoin(base_url, href)
        parsed = urlsplit(resolved)
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        if not _is_pdf_link(resolved, mime_hint):
            continue
        if resolved in seen_urls:
            continue
        seen_urls.add(resolved)
        filename = safe_filename(Path(parsed.path).name or "document.pdf", default="document.pdf")
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        discovered.append(
            {
                "url": resolved,
                "title": sanitize_plain_text(label) or filename,
                "filename": filename,
            }
        )

    return discovered


def extract_article_text(html: str, url: str = "") -> tuple[str, str]:
    """Extract clean page title and main article text via trafilatura with BeautifulSoup fallback."""
    if not html:
        return "", ""

    soup = BeautifulSoup(html, "html.parser")
    page_title = ""
    if soup.title and soup.title.string:
        page_title = sanitize_plain_text(soup.title.string)

    article_text = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            url=url or None,
            include_comments=False,
            include_tables=True,
        )
        if extracted:
            article_text = sanitize_plain_text(extracted)
    except Exception:
        article_text = ""

    if not article_text:
        for noisy in soup(["script", "style", "noscript", "nav", "footer", "header", "iframe"]):
            noisy.decompose()
        article_text = sanitize_plain_text(soup.get_text(separator="\n"))

    return page_title, article_text


async def _fetch_public_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, dict[str, str], bytes]:
    """Stream-fetch a validated public URL while enforcing SSRF checks on every redirect."""
    current_url = validate_public_http_url(url)
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=False, timeout=timeout)

    try:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_http_url(current_url)
            async with client.stream("GET", current_url, headers=headers) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location", "")
                    if not location:
                        raise SSRFBlockedError("Redirect response missing Location header.")
                    next_url = urljoin(current_url, location)
                    current_url = validate_public_http_url(next_url)
                    continue

                resp.raise_for_status()
                content_length = resp.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise FileValidationError(
                        f"Remote resource '{current_url}' exceeds {max_bytes // (1024 * 1024)} MB cap."
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise FileValidationError(
                            f"Remote resource '{current_url}' exceeded {max_bytes // (1024 * 1024)} MB cap while streaming."
                        )
                    chunks.append(chunk)

                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return str(resp.url or current_url), resp_headers, b"".join(chunks)

        raise SSRFBlockedError(f"Exceeded maximum redirects ({MAX_REDIRECTS}) for '{url}'.")
    finally:
        if owns_client:
            await client.aclose()


async def ainspect_url(
    url: str,
    *,
    timeout: float = 15.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Inspect a public URL: extract clean text and discover all linked PDFs (or parse direct PDF)."""
    clean_url = validate_public_http_url(url)
    final_url, headers, body = await _fetch_public_bytes(
        clean_url,
        max_bytes=MAX_PDF_BYTES if _is_pdf_link(clean_url) else MAX_HTML_BYTES,
        timeout=timeout,
        client=client,
    )

    content_type = headers.get("content-type", "").lower()
    if body.startswith(b"%PDF-") or ("application/pdf" in content_type):
        filename = safe_filename(Path(urlsplit(final_url).path).name or "document.pdf", default="document.pdf")
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        payload = parse_pdf(body, filename=filename)
        payload.source_type = "web_pdf"
        payload.origin_url = final_url
        return {
            "url": final_url,
            "title": payload.title,
            "is_direct_pdf": True,
            "text": payload.raw_text_excerpt,
            "discovered_pdfs": [{"url": final_url, "title": payload.title, "filename": filename}],
            "payload": payload,
        }

    html = body.decode("utf-8", errors="replace")
    title, article_text = extract_article_text(html, url=final_url)
    discovered_pdfs = extract_pdfs_from_html(html, final_url)
    display_title = title or urlsplit(final_url).netloc or final_url
    file_hash = hashlib.sha256(body).hexdigest()

    chunks: list[dict[str, Any]] = []
    if article_text:
        for idx in range(0, len(article_text), 2200):
            part = article_text[idx : idx + 2200].strip()
            if part:
                chunk_no = len(chunks) + 1
                chunks.append(
                    {
                        "index": len(chunks),
                        "label": f"Section {chunk_no}",
                        "text": part,
                        "citation": f"[From: {final_url} §{chunk_no}]",
                    }
                )

    payload = NormalizedSourcePayload(
        source_type="web_url",
        title=display_title,
        origin_url=final_url,
        file_hash=file_hash,
        raw_text_excerpt=article_text,
        text_chunks=chunks,
        discovered_links=discovered_pdfs,
        source_citations=[{"source": display_title, "url": final_url, "citation": f"[From: {final_url}]"}],
        metadata={
            "url": final_url,
            "title": display_title,
            "discovered_pdf_count": len(discovered_pdfs),
            "char_count": len(article_text),
        },
    )
    return {
        "url": final_url,
        "title": display_title,
        "is_direct_pdf": False,
        "text": article_text,
        "discovered_pdfs": discovered_pdfs,
        "payload": payload,
    }


def inspect_url(url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Synchronous wrapper around `ainspect_url`."""
    return asyncio.run(ainspect_url(url, timeout=timeout))


async def adownload_selected_pdfs(
    pdf_urls: list[str],
    *,
    timeout: float = 30.0,
    client: httpx.AsyncClient | None = None,
) -> list[NormalizedSourcePayload]:
    """Download and parse user-selected PDF URLs, verifying SSRF safety and `%PDF-` magic header."""
    if not pdf_urls:
        return []

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=False, timeout=timeout)

    results: list[NormalizedSourcePayload] = []
    try:
        for raw_url in pdf_urls:
            clean_url = validate_public_http_url(raw_url)
            final_url, _headers, body = await _fetch_public_bytes(
                clean_url,
                max_bytes=MAX_PDF_BYTES,
                timeout=timeout,
                client=client,
            )
            if not body.startswith(b"%PDF-"):
                raise FileValidationError(
                    f"Downloaded file from '{final_url}' is missing the %PDF- magic header."
                )
            filename = safe_filename(
                Path(urlsplit(final_url).path).name or "downloaded.pdf",
                default="downloaded.pdf",
            )
            if not filename.lower().endswith(".pdf"):
                filename = f"{filename}.pdf"

            payload = parse_pdf(body, filename=filename)
            payload.source_type = "web_pdf"
            payload.origin_url = final_url
            payload.source_citations = [
                {
                    "source": filename,
                    "url": final_url,
                    "page": c.get("page"),
                    "citation": f"[From: {filename} p.{c.get('page', 1)}]",
                }
                for c in payload.text_chunks
            ]
            payload.metadata["origin_url"] = final_url
            results.append(payload)
        return results
    finally:
        if owns_client:
            await client.aclose()


def download_selected_pdfs(pdf_urls: list[str], *, timeout: float = 30.0) -> list[NormalizedSourcePayload]:
    """Synchronous wrapper around `adownload_selected_pdfs`."""
    return asyncio.run(adownload_selected_pdfs(pdf_urls, timeout=timeout))


__all__ = [
    "MAX_HTML_BYTES",
    "MAX_REDIRECTS",
    "adownload_selected_pdfs",
    "ainspect_url",
    "download_selected_pdfs",
    "extract_article_text",
    "extract_pdfs_from_html",
    "inspect_url",
    "validate_public_http_url",
]
