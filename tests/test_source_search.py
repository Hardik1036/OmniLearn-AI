"""Tests for multi-provider internet search aggregator (`local_app/source_search.py`) and cache hits."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from backend.local_app import source_search
from backend.local_app.local_db import LocalDatabase

OPENALEX_SAMPLE = {
    "results": [
        {
            "id": "https://openalex.org/W12345",
            "display_name": "Attention Is All You Need",
            "publication_year": 2017,
            "authorships": [{"author": {"display_name": "Ashish Vaswani"}}],
            "primary_location": {
                "pdf_url": "https://proceedings.neurips.cc/paper/attention.pdf",
                "landing_page_url": "https://proceedings.neurips.cc/paper/attention",
            },
            "open_access": {"oa_url": "https://proceedings.neurips.cc/paper/attention.pdf"},
            "abstract_inverted_index": {
                "Transformers": [0],
                "replace": [1],
                "recurrence": [2],
                "with": [3],
                "self-attention.": [4],
            },
        }
    ]
}

ARXIV_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Transformer Architectures in Practice</title>
    <summary>Scaled dot-product attention stabilizes sequence transduction models.</summary>
    <author><name>A. Vaswani</name></author>
    <link href="https://arxiv.org/abs/1706.03762" rel="alternate" type="text/html"/>
    <link title="pdf" href="https://arxiv.org/pdf/1706.03762.pdf" rel="related" type="application/pdf"/>
  </entry>
</feed>
"""

WIKI_SEARCH_SAMPLE = {
    "query": {
        "search": [
            {"title": "Transformer (deep learning architecture)", "snippet": "Neural sequence model"}
        ]
    }
}

WIKI_SUMMARY_SAMPLE = {
    "title": "Transformer (deep learning architecture)",
    "extract": "A transformer is a deep learning architecture relying on multi-head attention.",
    "content_urls": {
        "desktop": {"page": "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)"}
    },
}

DDG_SAMPLE_HTML = """
<html><body>
  <div class="result">
    <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcs224n.stanford.edu%2F">Stanford CS224N Course Notes</a>
    <a class="result__snippet">Deep Learning for Natural Language Processing syllabus and PDFs.</a>
  </div>
</body></html>
"""


async def test_search_sources_aggregates_all_providers_and_caches_in_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    isolated_local_db: LocalDatabase,
) -> None:
    http_calls: list[str] = []

    def _transport(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        http_calls.append(url)
        if "api.openalex.org" in url:
            return httpx.Response(200, json=OPENALEX_SAMPLE)
        if "export.arxiv.org" in url:
            return httpx.Response(200, text=ARXIV_SAMPLE_XML, headers={"Content-Type": "application/xml"})
        if "en.wikipedia.org/w/api.php" in url:
            return httpx.Response(200, json=WIKI_SEARCH_SAMPLE)
        if "en.wikipedia.org/api/rest_v1/page/summary" in url:
            return httpx.Response(200, json=WIKI_SUMMARY_SAMPLE)
        if "duckduckgo.com" in url:
            return httpx.Response(200, text=DDG_SAMPLE_HTML, headers={"Content-Type": "text/html"})
        return httpx.Response(404)

    yt_calls = 0

    def _mock_youtube(query: str, limit: int = 8) -> list[dict[str, Any]]:
        nonlocal yt_calls
        _ = (query, limit)
        yt_calls += 1
        return [
            {
                "source_type": "youtube",
                "provider": "youtube",
                "title": "Illustrated Transformer Lecture",
                "url": "https://www.youtube.com/watch?v=kCc8FmEb1nY",
                "snippet": "Step-by-step walkthrough of self-attention.",
                "is_direct_pdf": False,
                "can_inspect_pdfs": False,
                "metadata": {"duration": "48:00"},
            }
        ]

    monkeypatch.setattr(source_search, "_search_youtube_sync", _mock_youtube)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_transport)) as client:
        first = await source_search.search_sources(
            "transformer self attention",
            provider_filter="all",
            db=isolated_local_db,
            use_cache=True,
            client=client,
        )

    assert first["ok"] is True
    assert first["cached"] is False
    providers = {item["provider"] for item in first["results"]}
    assert providers == {"openalex", "arxiv", "wikipedia", "duckduckgo", "youtube"}

    by_prov = {item["provider"]: item for item in first["results"]}
    assert by_prov["openalex"]["is_direct_pdf"] is True
    assert "Transformers replace recurrence" in by_prov["openalex"]["snippet"]
    assert by_prov["arxiv"]["is_direct_pdf"] is True
    assert by_prov["arxiv"]["url"].endswith(".pdf")
    assert by_prov["duckduckgo"]["url"] == "https://cs224n.stanford.edu/"
    assert by_prov["duckduckgo"]["can_inspect_pdfs"] is True
    assert by_prov["youtube"]["can_inspect_pdfs"] is False
    assert yt_calls == 1
    assert len(http_calls) >= 5

    # Second identical call must hit SQLite `source_search_history` cache with ZERO network calls
    http_calls.clear()

    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Expected cache hit, unexpected HTTP request to {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_fail_if_called)) as failing_client:
        second = await source_search.search_sources(
            "  Transformer Self Attention  ",
            provider_filter="all",
            db=isolated_local_db,
            use_cache=True,
            client=failing_client,
        )

    assert second["ok"] is True
    assert second["cached"] is True
    assert len(second["results"]) == len(first["results"])
    assert yt_calls == 1


async def test_search_sources_handles_partial_provider_failures_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _flaky_transport(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.openalex.org" in url:
            raise httpx.ReadTimeout("Simulated OpenAlex timeout")
        if "export.arxiv.org" in url:
            return httpx.Response(200, text=ARXIV_SAMPLE_XML)
        return httpx.Response(500)

    monkeypatch.setattr(source_search, "_search_youtube_sync", lambda q, limit=8: [])

    async with httpx.AsyncClient(transport=httpx.MockTransport(_flaky_transport)) as client:
        res = await source_search.search_sources(
            "transformer",
            provider_filter="all",
            db=None,
            use_cache=False,
            client=client,
        )
        assert res["ok"] is True
        assert any(r["provider"] == "arxiv" for r in res["results"])
        assert not any(r["provider"] == "openalex" for r in res["results"])

        # Specific provider alias filter (`web` -> `web`, `academic` -> `academic_pdf`, `pdf` -> `direct_pdf`)
        assert source_search.normalize_provider_filter("academic") == "academic_pdf"
        assert source_search.normalize_provider_filter("pdf") == "direct_pdf"
        assert source_search.normalize_provider_filter("web_docs") == "web"
        filtered = await source_search.search_sources(
            "transformer",
            provider_filter="academic",
            db=None,
            use_cache=False,
            client=client,
        )
        assert [r["provider"] for r in filtered["results"]] == ["arxiv"]
