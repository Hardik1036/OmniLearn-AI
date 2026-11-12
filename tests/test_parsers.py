"""Parser suite: PDF, TXT/MD, XLSX, PPTX, CSV, HTML PDF discovery, YouTube, OpenCV video, and FTS5."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
import openpyxl
import pymupdf
import pytest
from pptx import Presentation

from backend.local_app.local_db import LocalDatabase
from backend.local_app.parsers import NormalizedSourcePayload, SourceDispatcher
from backend.local_app.parsers.video_parser import compute_quadrant_rois, parse_video
from backend.local_app.parsers.web_crawler import adownload_selected_pdfs, ainspect_url, extract_pdfs_from_html
from backend.local_app.parsers.youtube_parser import parse_youtube_async


def _build_minimal_pdf_bytes(text: str = "Eigenvalues and eigenvectors diagonalize symmetric matrices.") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 96), text, fontsize=14)
    # Add >= 5 vector drawing primitives so page qualifies as diagram-rich (doc_parsers threshold)
    for idx in range(6):
        y = 130 + idx * 24
        page.draw_rect(pymupdf.Rect(72, y, 320, y + 16), color=(0.1, 0.3, 0.8), fill=(0.9, 0.94, 1.0))
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# 1. PyMuPDF PDF, TXT, XLSX, PPTX, CSV -> NormalizedSourcePayload + FTS5
# ---------------------------------------------------------------------------


def test_pdf_parser_and_fts_indexing(tmp_path: Path, isolated_local_db: LocalDatabase) -> None:
    pdf_path = tmp_path / "linear_algebra.pdf"
    pdf_path.write_bytes(_build_minimal_pdf_bytes("Spectral Theorem and Singular Value Decomposition"))

    payload = SourceDispatcher.dispatch_file(pdf_path)
    assert isinstance(payload, NormalizedSourcePayload)
    assert payload.source_type == "pdf"
    assert "Singular Value Decomposition" in payload.raw_text_excerpt
    assert len(payload.text_chunks) >= 1
    assert len(payload.visual_slides) >= 1
    slide_bytes = payload.visual_slides[0]["data_bytes"]
    assert slide_bytes.startswith(b"\xff\xd8\xff")

    course = isolated_local_db.create_course("Linear Algebra FTS Test")
    saved = isolated_local_db.add_course_source(course["id"], payload)
    assert saved["source_type"] == "pdf"
    assert len(saved["visual_assets"]) >= 1
    asset_url = saved["visual_assets"][0]["url"]
    assert asset_url.startswith(f"/api/slides/courses/{course['id']}/")
    read_back = isolated_local_db.read_slide_bytes(asset_url.removeprefix("/api/slides/"))
    assert read_back is not None
    assert read_back[0].startswith(b"\xff\xd8\xff")

    hits = isolated_local_db.fts_search_sources("Singular Value", course_id=course["id"])
    assert len(hits) == 1
    assert hits[0]["source_id"] == saved["id"]


def test_txt_xlsx_pptx_csv_parsers_and_fts_search(
    tmp_path: Path,
    isolated_local_db: LocalDatabase,
) -> None:
    # 1. TXT
    txt_path = tmp_path / "graphs.txt"
    txt_path.write_text(
        "Bellman-Ford handles negative edge weights cleanly in O(VE) time.\n"
        "Topological sort applies to Directed Acyclic Graphs.",
        encoding="utf-8",
    )
    txt_payload = SourceDispatcher.dispatch_file(txt_path)
    assert txt_payload.source_type == "txt"
    assert "Bellman-Ford" in txt_payload.raw_text_excerpt

    # 2. XLSX (generated cleanly via openpyxl)
    xlsx_path = tmp_path / "complexity.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Asymptotics"
    ws.append(["Algorithm", "Time Complexity", "Injected Note"])
    ws.append(["QuickSort", "O(n log n)", "@HYPERLINK(\"http://evil\")"])
    ws.append(["HeapSort", "O(n log n)", "In-place priority heap"])
    wb.save(xlsx_path)
    xlsx_payload = SourceDispatcher.dispatch_file(xlsx_path)
    assert xlsx_payload.source_type == "xlsx"
    assert "HeapSort" in xlsx_payload.raw_text_excerpt
    assert "'@HYPERLINK" in xlsx_payload.raw_text_excerpt

    # 3. PPTX (generated cleanly via python-pptx with speaker notes)
    pptx_path = tmp_path / "operating_systems.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Virtual Memory & Paging"
    slide.placeholders[1].text = "Translation Lookaside Buffer accelerates address translation."
    slide.notes_slide.notes_text_frame.text = "Speaker note: page fault triggers OS trap handler."
    prs.save(pptx_path)
    pptx_payload = SourceDispatcher.dispatch_file(pptx_path)
    assert pptx_payload.source_type == "ppt"
    assert "Translation Lookaside Buffer" in pptx_payload.raw_text_excerpt
    assert "page fault triggers OS trap" in pptx_payload.raw_text_excerpt

    # 4. CSV
    csv_path = tmp_path / "transactions.csv"
    csv_path.write_text("property,meaning\nAtomicity,All or nothing commit\nFormula,=2+2\n", encoding="utf-8")
    csv_payload = SourceDispatcher.dispatch_file(csv_path)
    assert csv_payload.source_type == "csv"
    assert "Atomicity" in csv_payload.raw_text_excerpt
    assert "'=2+2" in csv_payload.raw_text_excerpt

    # Index all 4 into SQLite FTS5 & verify keyword search hits each one
    course = isolated_local_db.create_course("Multi-Document Parsing Course")
    for payload in (txt_payload, xlsx_payload, pptx_payload, csv_payload):
        isolated_local_db.add_course_source(course["id"], payload)

    assert len(isolated_local_db.fts_search_sources("Bellman", course_id=course["id"])) == 1
    assert len(isolated_local_db.fts_search_sources("HeapSort", course_id=course["id"])) == 1
    assert len(isolated_local_db.fts_search_sources("Lookaside", course_id=course["id"])) == 1
    assert len(isolated_local_db.fts_search_sources("Atomicity", course_id=course["id"])) == 1


# ---------------------------------------------------------------------------
# 2. Website HTML Crawler & Selected PDF Link Downloader (Mocked HTTPX)
# ---------------------------------------------------------------------------


async def test_html_pdf_discovery_and_selected_pdf_ingestion(
    monkeypatch: pytest.MonkeyPatch,
    isolated_local_db: LocalDatabase,
) -> None:
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", int(port or 443)))
        ],
    )
    html = """
    <html>
      <head><title>CS229 Course Handouts</title></head>
      <body>
        <main>
          <h1>Machine Learning Lecture Materials</h1>
          <p>Linear regression, gradient descent, and support vector machines overview.</p>
          <a href="/notes.pdf">Lecture Notes PDF</a>
          <a href="https://cdn.example.org/hw1_solutions.pdf">Homework 1 PDF</a>
        </main>
      </body>
    </html>
    """
    discovered = extract_pdfs_from_html(html, "https://course.example.edu/cs229/index.html")
    urls = [item["url"] for item in discovered]
    assert "https://course.example.edu/notes.pdf" in urls
    assert "https://cdn.example.org/hw1_solutions.pdf" in urls

    pdf_bytes = _build_minimal_pdf_bytes("Support Vector Machines maximize geometric margin.")

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        target = str(request.url)
        if target.endswith("/index.html"):
            return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})
        if target.endswith(".pdf"):
            return httpx.Response(200, content=pdf_bytes, headers={"Content-Type": "application/pdf"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_transport)) as client:
        inspected = await ainspect_url("https://course.example.edu/cs229/index.html", client=client)
        assert inspected["is_direct_pdf"] is False
        assert "Linear regression" in inspected["text"]
        assert any(p["url"] == "https://course.example.edu/notes.pdf" for p in inspected["discovered_pdfs"])

        pdf_payloads = await adownload_selected_pdfs(
            ["https://course.example.edu/notes.pdf"],
            client=client,
        )
        assert len(pdf_payloads) == 1
        assert "geometric margin" in pdf_payloads[0].raw_text_excerpt

    course = isolated_local_db.create_course("Web Crawler Course")
    saved = isolated_local_db.add_course_source(course["id"], pdf_payloads[0])
    fts_hits = isolated_local_db.fts_search_sources("geometric margin", course_id=course["id"])
    assert len(fts_hits) == 1
    assert fts_hits[0]["source_id"] == saved["id"]


# ---------------------------------------------------------------------------
# 3. Mocked YouTube Transcript & Metadata Extraction -> FTS5
# ---------------------------------------------------------------------------


async def test_youtube_parser_mocked_transcript_and_fts(
    monkeypatch: pytest.MonkeyPatch,
    isolated_local_db: LocalDatabase,
) -> None:
    import backend.local_app.parsers.youtube_parser as yt_mod

    monkeypatch.setattr(
        yt_mod,
        "_fetch_ytdlp_metadata",
        lambda canonical_url: {
            "title": "Dynamic Programming & Memoization Deep Dive",
            "uploader": "MIT OpenCourseWare",
            "duration": 360,
            "thumbnail": "https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        },
    )
    monkeypatch.setattr(
        yt_mod,
        "_fetch_transcript_segments",
        lambda video_id, languages=("en", "en-US", "en-GB"): [
            {"start": 0.0, "duration": 15.0, "text": "Welcome to dynamic programming and optimal substructure."},
            {"start": 125.0, "duration": 20.0, "text": "Fibonacci memoization avoids exponential recomputation."},
        ],
    )

    async def _fake_thumb(thumbnail_url: str) -> bytes:
        _ = thumbnail_url
        return b"\xff\xd8\xff\xe0\x00\x10JFIFytthumb"

    monkeypatch.setattr(yt_mod, "_fetch_thumbnail_jpeg", _fake_thumb)

    payload = await parse_youtube_async("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert payload.source_type == "youtube"
    assert payload.title == "Dynamic Programming & Memoization Deep Dive"
    assert any(
        "02:05" in str(c.get("timestamp", "")) and "02:05" in str(c.get("citation", ""))
        for c in payload.text_chunks
    ), f"Unexpected YouTube chunks: {payload.text_chunks!r}"
    assert len(payload.visual_slides) == 1
    assert payload.visual_slides[0]["data_bytes"].startswith(b"\xff\xd8\xff")

    course = isolated_local_db.create_course("YouTube Course")
    saved = isolated_local_db.add_course_source(course["id"], payload)
    assert len(isolated_local_db.fts_search_sources("memoization", course_id=course["id"])) == 1
    assert saved["metadata"]["video_id"] == "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# 4. OpenCV Synthetic 2x2 Quadrant Video Delta Slicer + FTS5
# ---------------------------------------------------------------------------


def test_opencv_synthetic_2x2_video_slicer(tmp_path: Path, isolated_local_db: LocalDatabase) -> None:
    rois = compute_quadrant_rois(320, 240, layout_type="2x2", taskbar_crop_px=20)
    assert [r["roi_key"] for r in rois] == ["top_left", "top_right", "bottom_left", "bottom_right"]

    video_path = tmp_path / "quadrant_lecture.avi"
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    fps = 5.0
    writer = cv2.VideoWriter(str(video_path), fourcc, fps, (320, 240))
    assert writer.isOpened(), "OpenCV MJPG writer failed to initialize"

    # Frame A (0s - 1s): distinct colors in Q1..Q4
    frame_a = np.zeros((240, 320, 3), dtype=np.uint8)
    frame_a[0:110, 0:160] = (240, 240, 240)
    frame_a[0:110, 160:320] = (30, 180, 40)
    frame_a[110:220, 0:160] = (200, 50, 50)
    frame_a[110:220, 160:320] = (50, 160, 220)

    # Frame B (2s - 3s): dramatic content shift in Q1 & Q4 only (Q2 & Q3 stay stable)
    frame_b = frame_a.copy()
    frame_b[10:100, 10:150] = (10, 10, 10)
    frame_b[120:210, 170:310] = (255, 255, 255)

    try:
        # 10 frames of Frame A (2.0s), then 10 frames of Frame B (2.0s)
        for _ in range(10):
            writer.write(frame_a)
        for _ in range(10):
            writer.write(frame_b)
    finally:
        writer.release()

    payload = parse_video(
        video_path,
        layout_type="2x2",
        taskbar_crop_px=20,
        sample_interval_sec=1.0,
        delta_threshold=0.05,
    )
    assert payload.source_type == "video"
    # Initial 4 quadrants captured + Q1 & Q4 change captures => at least 6 slides (< 16 static duplicates)
    assert len(payload.visual_slides) >= 6
    q_counts: dict[str, int] = {}
    for slide in payload.visual_slides:
        assert slide["data_bytes"].startswith(b"\xff\xd8\xff")
        rk = slide["quadrant_key"]
        q_counts[rk] = q_counts.get(rk, 0) + 1
    assert q_counts["top_left"] >= 2
    assert q_counts["bottom_right"] >= 2
    assert q_counts["top_right"] == 1
    assert q_counts["bottom_left"] == 1

    course = isolated_local_db.create_course("Video Slicer Course", layout_type="2x2")
    saved = isolated_local_db.add_course_source(course["id"], payload)
    assert len(saved["visual_assets"]) == len(payload.visual_slides)
    assert len(isolated_local_db.fts_search_sources("quadrant_lecture", course_id=course["id"])) == 1
