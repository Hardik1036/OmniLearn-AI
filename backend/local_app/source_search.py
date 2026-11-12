"""Free In-App Research Aggregator (OpenAlex, arXiv, Wikipedia, DuckDuckGo, YouTube)."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

import httpx
from bs4 import BeautifulSoup

from backend.local_app.local_db import LocalDatabase
from backend.local_app.parsers.doc_parsers import sanitize_plain_text

OPENALEX_URL = "https://api.openalex.org/works"
ARXIV_URL = "https://export.arxiv.org/api/query"
WIKI_OPENSEARCH_URL = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"

DEFAULT_HEADERS = {
    "User-Agent": "OmniLearn-SystemApp/1.0 (https://omnilearn.local; educational-research)",
    "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
}


def normalize_provider_filter(provider_filter: str | None) -> str:
    """Normalize UI filter chip labels into canonical search filter values."""
    raw = (provider_filter or "all").strip().lower()
    mapping = {
        "all": "all",
        "web": "web",
        "web_docs": "web",
        "web_articles": "web",
        "direct_pdf": "direct_pdf",
        "direct_pdfs": "direct_pdf",
        "pdfs": "direct_pdf",
        "pdf": "direct_pdf",
        "academic": "academic_pdf",
        "academic_pdf": "academic_pdf",
        "academic_papers": "academic_pdf",
        "youtube": "youtube",
    }
    return mapping.get(raw, "all")


def _reconstruct_openalex_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""
    positions: dict[int, str] = {}
    for word, indices in inverted_index.items():
        if not isinstance(indices, list):
            continue
        for pos in indices:
            if isinstance(pos, int) and 0 <= pos < 160:
                positions[pos] = str(word)
    if not positions:
        return ""
    text = " ".join(positions[idx] for idx in sorted(positions.keys()))
    text = sanitize_plain_text(text)
    return text[:420] + ("..." if len(text) > 420 else "")


def _is_pdf_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlsplit(url.strip())
    return parsed.path.lower().endswith(".pdf")


def _normalize_url_key(url: str) -> str:
    parsed = urlsplit((url or "").strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def _make_result(
    *,
    title: str,
    url: str,
    snippet: str,
    provider: str,
    is_direct_pdf: bool | None = None,
    can_inspect_pdfs: bool | None = None,
    source_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_url = (url or "").strip()
    is_yt = provider == "youtube" or "youtube.com" in clean_url.lower() or "youtu.be" in clean_url.lower()
    direct_pdf = _is_pdf_url(clean_url) if is_direct_pdf is None else bool(is_direct_pdf)
    inspectable = (not direct_pdf and not is_yt) if can_inspect_pdfs is None else bool(can_inspect_pdfs)
    resolved_type = source_type or ("youtube" if is_yt else ("web_pdf" if direct_pdf else "web_url"))
    return {
        "id": f"sr-{uuid.uuid5(uuid.NAMESPACE_URL, clean_url or title).hex[:12]}",
        "title": sanitize_plain_text(title) or clean_url,
        "url": clean_url,
        "snippet": sanitize_plain_text(snippet),
        "provider": provider,
        "source_provider": provider,
        "source_type": resolved_type,
        "is_direct_pdf": direct_pdf,
        "can_inspect_pdfs": inspectable,
        "metadata": metadata or {},
    }


async def search_openalex(
    query: str,
    *,
    limit: int = 8,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Query OpenAlex works API for free open-access papers and PDF links."""
    params = {
        "search": query,
        "per-page": str(limit),
        "select": "id,title,publication_year,doi,primary_location,open_access,abstract_inverted_index,authorships",
    }
    resp = await client.get(OPENALEX_URL, params=params, headers=DEFAULT_HEADERS)
    resp.raise_for_status()
    data = resp.json()

    out: list[dict[str, Any]] = []
    for work in (data.get("results") or [])[:limit]:
        if not isinstance(work, dict):
            continue
        oa = work.get("open_access") or {}
        primary = work.get("primary_location") or {}
        pdf_url = primary.get("pdf_url") or ""
        oa_url = oa.get("oa_url") or ""
        landing = primary.get("landing_page_url") or work.get("doi") or work.get("id") or ""
        chosen_url = str(pdf_url or oa_url or landing).strip()
        if not chosen_url.startswith(("http://", "https://")):
            continue

        authors = [
            str(((a or {}).get("author") or {}).get("display_name"))
            for a in (work.get("authorships") or [])[:3]
            if ((a or {}).get("author") or {}).get("display_name")
        ]
        abstract = _reconstruct_openalex_abstract(work.get("abstract_inverted_index"))
        direct_pdf = bool(pdf_url) or _is_pdf_url(chosen_url)
        out.append(
            _make_result(
                title=str(work.get("title") or "Untitled OpenAlex Work"),
                url=chosen_url,
                snippet=abstract or f"OpenAlex academic paper ({work.get('publication_year') or 'n.d.'}).",
                provider="openalex",
                is_direct_pdf=direct_pdf,
                can_inspect_pdfs=not direct_pdf,
                metadata={
                    "year": work.get("publication_year"),
                    "authors": authors,
                    "doi": work.get("doi") or "",
                },
            )
        )
    return out


async def search_arxiv(
    query: str,
    *,
    limit: int = 6,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Query arXiv Atom API and emit direct PDF links."""
    params = {
        "search_query": f"all:{query}",
        "start": "0",
        "max_results": str(limit),
    }
    resp = await client.get(ARXIV_URL, params=params, headers=DEFAULT_HEADERS)
    resp.raise_for_status()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(resp.text)
    out: list[dict[str, Any]] = []

    for entry in root.findall("atom:entry", ns)[:limit]:
        title_el = entry.find("atom:title", ns)
        summary_el = entry.find("atom:summary", ns)
        id_el = entry.find("atom:id", ns)
        published_el = entry.find("atom:published", ns)

        raw_id = (id_el.text or "").strip() if id_el is not None else ""
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "").strip()
                break
        if not pdf_url and "/abs/" in raw_id:
            pdf_url = raw_id.replace("/abs/", "/pdf/") + ".pdf"
        if not pdf_url.startswith(("http://", "https://")):
            continue

        authors = [
            (a.findtext("atom:name", default="", namespaces=ns) or "").strip()
            for a in entry.findall("atom:author", ns)[:3]
        ]
        title = re.sub(r"\s+", " ", (title_el.text or "arXiv Preprint") if title_el is not None else "arXiv Preprint")
        summary = re.sub(r"\s+", " ", (summary_el.text or "") if summary_el is not None else "")[:400]
        published = (published_el.text or "")[:10] if published_el is not None else ""

        out.append(
            _make_result(
                title=title,
                url=pdf_url,
                snippet=summary,
                provider="arxiv",
                is_direct_pdf=True,
                can_inspect_pdfs=False,
                metadata={"authors": [a for a in authors if a], "published": published, "abs_url": raw_id},
            )
        )
    return out


async def search_wikipedia(
    query: str,
    *,
    limit: int = 3,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Query Wikipedia REST summary / opensearch for conceptual references."""
    out: list[dict[str, Any]] = []

    # First try OpenSearch to resolve exact encyclopedic article titles.
    try:
        resp = await client.get(
            WIKI_OPENSEARCH_URL,
            params={
                "action": "opensearch",
                "search": query,
                "limit": str(limit),
                "namespace": "0",
                "format": "json",
            },
            headers=DEFAULT_HEADERS,
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, list) and len(payload) >= 4:
            titles = payload[1]
            descs = payload[2]
            urls = payload[3]
            for idx, title in enumerate(titles[:limit]):
                page_url = urls[idx] if idx < len(urls) else ""
                snippet = descs[idx] if idx < len(descs) else ""
                if not snippet:
                    # Fetch REST summary for top hit if opensearch description is blank.
                    try:
                        sum_resp = await client.get(
                            WIKI_SUMMARY_URL.format(title=quote_plus(str(title).replace(" ", "_"))),
                            headers=DEFAULT_HEADERS,
                        )
                        if sum_resp.status_code == 200:
                            sdata = sum_resp.json()
                            snippet = sdata.get("extract", "")
                            page_url = ((sdata.get("content_urls") or {}).get("desktop") or {}).get("page") or page_url
                    except Exception:
                        pass
                if page_url and str(page_url).startswith(("http://", "https://")):
                    out.append(
                        _make_result(
                            title=str(title),
                            url=str(page_url),
                            snippet=str(snippet or f"Wikipedia article on {title}.")[:400],
                            provider="wikipedia",
                            is_direct_pdf=False,
                            can_inspect_pdfs=True,
                        )
                    )
            if out:
                return out
    except Exception:
        pass

    # Fallback direct REST summary call
    try:
        sum_resp = await client.get(
            WIKI_SUMMARY_URL.format(title=quote_plus(query.strip().replace(" ", "_"))),
            headers=DEFAULT_HEADERS,
        )
        if sum_resp.status_code == 200:
            sdata = sum_resp.json()
            page_url = ((sdata.get("content_urls") or {}).get("desktop") or {}).get("page") or ""
            if page_url:
                out.append(
                    _make_result(
                        title=str(sdata.get("title") or query),
                        url=str(page_url),
                        snippet=str(sdata.get("extract") or "")[:400],
                        provider="wikipedia",
                        is_direct_pdf=False,
                        can_inspect_pdfs=True,
                    )
                )
    except Exception:
        pass
    return out


def _unwrap_ddg_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlsplit(href)
    if "duckduckgo.com" in (parsed.hostname or "").lower() and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg and uddg[0]:
            return unquote(uddg[0])
    return href


async def search_duckduckgo(
    query: str,
    *,
    limit: int = 8,
    pdf_only: bool = False,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Query DuckDuckGo HTML endpoint for tutorials and `filetype:pdf` lecture notes."""
    effective_query = query.strip()
    if pdf_only and "filetype:pdf" not in effective_query.lower():
        effective_query = f"{effective_query} filetype:pdf"

    resp = await client.post(
        DDG_HTML_URL,
        data={"q": effective_query},
        headers={**DEFAULT_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[dict[str, Any]] = []
    anchors = soup.select("a.result__a") or soup.select("a.result-link")
    snippets = soup.select(".result__snippet") or soup.select(".result-snippet")

    for idx, anchor in enumerate(anchors[:limit]):
        raw_href = anchor.get("href", "")
        target_url = _unwrap_ddg_url(raw_href)
        if not target_url.startswith(("http://", "https://")):
            continue
        title = anchor.get_text(" ", strip=True)
        snippet = snippets[idx].get_text(" ", strip=True) if idx < len(snippets) else ""
        is_pdf = _is_pdf_url(target_url)
        out.append(
            _make_result(
                title=title or target_url,
                url=target_url,
                snippet=snippet,
                provider="duckduckgo",
                is_direct_pdf=is_pdf,
                can_inspect_pdfs=not is_pdf,
            )
        )
    return out


def _search_youtube_sync(query: str, limit: int = 8) -> list[dict[str, Any]]:
    try:
        import yt_dlp
    except ImportError:
        return []

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    out: list[dict[str, Any]] = []
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("id") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not url.startswith("http") and vid:
            url = f"https://www.youtube.com/watch?v={vid}"
        if not url.startswith(("http://", "https://")):
            continue
        duration = entry.get("duration")
        channel = str(entry.get("channel") or entry.get("uploader") or "YouTube")
        dur_str = f"{int(duration // 60)}m {int(duration % 60)}s" if isinstance(duration, (int, float)) and duration > 0 else "Video"
        out.append(
            _make_result(
                title=str(entry.get("title") or "YouTube Video"),
                url=url,
                snippet=f"{channel} • {dur_str}",
                provider="youtube",
                is_direct_pdf=False,
                can_inspect_pdfs=False,
                source_type="youtube",
                metadata={
                    "video_id": vid,
                    "channel": channel,
                    "duration": duration,
                    "thumbnail": entry.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""),
                },
            )
        )
    return out


async def search_youtube(query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Query YouTube via `yt-dlp ytsearch8` without requiring YouTube API quotas."""
    return await asyncio.to_thread(_search_youtube_sync, query, limit)


async def search_sources(
    query: str,
    *,
    provider_filter: str = "all",
    db: LocalDatabase | None = None,
    use_cache: bool = True,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Aggregate, normalize, deduplicate, and cache multi-provider source search results."""
    clean_query = (query or "").strip()
    norm_filter = normalize_provider_filter(provider_filter)
    if not clean_query:
        return {
            "ok": True,
            "query": "",
            "filter": norm_filter,
            "cached": False,
            "results": [],
        }

    query_hash = hashlib.sha256(f"{norm_filter}:{clean_query.lower()}".encode("utf-8")).hexdigest()
    if use_cache and db is not None:
        cached = db.get_cached_search(query_hash)
        if cached is not None:
            return {
                "ok": True,
                "query": clean_query,
                "filter": norm_filter,
                "cached": True,
                "results": cached,
            }

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=12.0)

    try:
        tasks: list[Any] = []
        if norm_filter == "all":
            tasks = [
                search_openalex(clean_query, limit=6, client=client),
                search_arxiv(clean_query, limit=5, client=client),
                search_wikipedia(clean_query, limit=2, client=client),
                search_duckduckgo(clean_query, limit=6, pdf_only=False, client=client),
                search_youtube(clean_query, limit=6),
            ]
        elif norm_filter == "web":
            tasks = [
                search_duckduckgo(clean_query, limit=8, pdf_only=False, client=client),
                search_wikipedia(clean_query, limit=3, client=client),
            ]
        elif norm_filter == "direct_pdf":
            tasks = [
                search_duckduckgo(clean_query, limit=8, pdf_only=True, client=client),
                search_arxiv(clean_query, limit=6, client=client),
                search_openalex(clean_query, limit=6, client=client),
            ]
        elif norm_filter == "academic_pdf":
            tasks = [
                search_openalex(clean_query, limit=8, client=client),
                search_arxiv(clean_query, limit=8, client=client),
            ]
        elif norm_filter == "youtube":
            tasks = [search_youtube(clean_query, limit=8)]

        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        deduped: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for batch in gathered:
            if isinstance(batch, BaseException) or not isinstance(batch, list):
                continue
            for item in batch:
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                if norm_filter == "direct_pdf" and not item.get("is_direct_pdf"):
                    continue
                norm_url = _normalize_url_key(url)
                if norm_url in seen_urls:
                    continue
                seen_urls.add(norm_url)
                deduped.append(item)

        if use_cache and db is not None and deduped:
            db.save_search_history(clean_query, norm_filter, query_hash, deduped)

        return {
            "ok": True,
            "query": clean_query,
            "filter": norm_filter,
            "cached": False,
            "results": deduped,
        }
    finally:
        if owns_client:
            await client.aclose()


__all__ = [
    "normalize_provider_filter",
    "search_arxiv",
    "search_duckduckgo",
    "search_openalex",
    "search_sources",
    "search_wikipedia",
    "search_youtube",
]
