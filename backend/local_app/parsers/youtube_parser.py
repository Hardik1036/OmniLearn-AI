"""YouTube metadata, chapters, timed captions/transcript, and thumbnail extractor."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from backend.local_app.parsers import FileValidationError, NormalizedSourcePayload, SourceParsingError
from backend.local_app.parsers.doc_parsers import MAX_SLIDE_JPEG_BYTES, sanitize_plain_text
from backend.local_app.parsers.web_crawler import validate_public_http_url

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_JPEG_MAGIC = b"\xff\xd8\xff"


def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def is_youtube_url(url: str) -> bool:
    """Return True if URL points to a supported YouTube domain."""
    try:
        parsed = urlsplit((url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme.lower() in {"http", "https"} and host in _YOUTUBE_HOSTS


def extract_youtube_video_id(url: str) -> str:
    """Extract the 11-character YouTube video ID from standard, short, shorts, live, or embed URLs."""
    parsed = urlsplit((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise SourceParsingError(f"Not a valid YouTube URL: '{url}'.")

    if host in {"youtu.be", "www.youtu.be"}:
        vid = parsed.path.strip("/").split("/")[0]
    else:
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            vid = qs["v"][0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                vid = parts[1]
            else:
                vid = ""

    if not _VIDEO_ID_RE.fullmatch(vid):
        raise SourceParsingError(f"Could not extract a valid 11-char YouTube video ID from '{url}'.")
    return vid


def _fetch_ytdlp_metadata(canonical_url: str) -> dict[str, Any]:
    """Extract title, channel, duration, chapters, and thumbnail URL via yt-dlp without downloading video."""
    try:
        import yt_dlp
    except ImportError:
        return {}

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical_url, download=False)
            return info if isinstance(info, dict) else {}
    except Exception:
        return {}


def _normalize_transcript_items(raw_items: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in raw_items or []:
        if isinstance(item, dict):
            text = sanitize_plain_text(str(item.get("text", "")))
            start = float(item.get("start", 0.0) or 0.0)
            duration = float(item.get("duration", 0.0) or 0.0)
        else:
            text = sanitize_plain_text(str(getattr(item, "text", "")))
            start = float(getattr(item, "start", 0.0) or 0.0)
            duration = float(getattr(item, "duration", 0.0) or 0.0)
        if text:
            normalized.append({"text": text, "start": start, "duration": duration})
    return normalized


def _fetch_transcript_segments(video_id: str, languages: tuple[str, ...] = ("en", "en-US", "en-GB")) -> list[dict[str, Any]]:
    """Fetch timed captions supporting both youtube-transcript-api 0.6.x and 1.x interfaces."""
    from youtube_transcript_api import YouTubeTranscriptApi

    # 0.6.x classmethod path (also commonly mocked in unit tests)
    get_fn = getattr(YouTubeTranscriptApi, "get_transcript", None)
    if callable(get_fn):
        try:
            raw = get_fn(video_id, languages=list(languages))
            return _normalize_transcript_items(raw)
        except Exception:
            pass

    list_fn = getattr(YouTubeTranscriptApi, "list_transcripts", None)
    if callable(list_fn):
        try:
            transcript_list = list_fn(video_id)
            try:
                transcript = transcript_list.find_transcript(list(languages))
            except Exception:
                transcript = next(iter(transcript_list))
            return _normalize_transcript_items(transcript.fetch())
        except Exception:
            pass

    # 1.x instance API path
    try:
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):
            try:
                fetched = api.fetch(video_id, languages=list(languages))
            except Exception:
                tlist = api.list(video_id)
                first = next(iter(tlist))
                fetched = first.fetch()
            return _normalize_transcript_items(fetched)
    except Exception:
        pass

    return []


async def _fetch_thumbnail_jpeg(thumbnail_url: str) -> bytes | None:
    if not thumbnail_url:
        return None
    try:
        clean_url = validate_public_http_url(thumbnail_url)
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            resp = await client.get(clean_url)
            resp.raise_for_status()
            blob = resp.content
    except Exception:
        return None

    if len(blob) > MAX_SLIDE_JPEG_BYTES:
        return None
    if blob.startswith(_JPEG_MAGIC):
        return blob
    try:
        import fitz

        pix = fitz.Pixmap(blob)
        if pix.alpha or (pix.colorspace and pix.colorspace.n > 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        jpg = pix.tobytes("jpeg")
        if jpg.startswith(_JPEG_MAGIC) and len(jpg) <= MAX_SLIDE_JPEG_BYTES:
            return jpg
    except Exception:
        return None
    return None


def _build_chunks_from_transcript(
    segments: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group transcript captions cleanly by chapters or ~90-second timed windows."""
    if not segments and not chapters:
        return []

    chunks: list[dict[str, Any]] = []
    if chapters:
        for idx, ch in enumerate(chapters):
            ch_start = float(ch.get("start_time", 0.0) or 0.0)
            ch_end = float(ch.get("end_time", 1e12) or 1e12)
            ch_title = sanitize_plain_text(str(ch.get("title", f"Chapter {idx + 1}")))
            lines = [
                f"[{format_timestamp(seg['start'])}] {seg['text']}"
                for seg in segments
                if ch_start <= seg["start"] < ch_end
            ]
            ts_label = format_timestamp(ch_start)
            body = "\n".join(lines) if lines else f"Chapter starts at {ts_label}."
            chunks.append(
                {
                    "index": idx,
                    "label": f"{ch_title} ({ts_label})",
                    "timestamp": ts_label,
                    "start_sec": ch_start,
                    "text": body,
                    "citation": f"[YouTube {ts_label}]",
                }
            )
        return chunks

    # Group into ~90s windows when no chapter markers exist.
    bucket: list[str] = []
    bucket_start = segments[0]["start"]
    idx = 0
    for seg in segments:
        if seg["start"] - bucket_start >= 90.0 and bucket:
            ts_label = format_timestamp(bucket_start)
            chunks.append(
                {
                    "index": idx,
                    "label": f"Segment {ts_label}",
                    "timestamp": ts_label,
                    "start_sec": bucket_start,
                    "text": " ".join(bucket),
                    "citation": f"[YouTube {ts_label}]",
                }
            )
            idx += 1
            bucket = []
            bucket_start = seg["start"]
        bucket.append(seg["text"])

    if bucket:
        ts_label = format_timestamp(bucket_start)
        chunks.append(
            {
                "index": idx,
                "label": f"Segment {ts_label}",
                "timestamp": ts_label,
                "start_sec": bucket_start,
                "text": " ".join(bucket),
                "citation": f"[YouTube {ts_label}]",
            }
        )
    return chunks


async def parse_youtube_async(url: str, *, fetch_thumbnail: bool = True) -> NormalizedSourcePayload:
    """Extract metadata, chapters, timed transcript, and thumbnail for a YouTube URL."""
    validate_public_http_url(url)
    video_id = extract_youtube_video_id(url)
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    info, segments = await asyncio.gather(
        asyncio.to_thread(_fetch_ytdlp_metadata, canonical_url),
        asyncio.to_thread(_fetch_transcript_segments, video_id),
    )

    title = sanitize_plain_text(str(info.get("title") or f"YouTube Video ({video_id})"))
    channel = sanitize_plain_text(str(info.get("channel") or info.get("uploader") or ""))
    duration_sec = float(info.get("duration") or 0.0)
    raw_chapters = info.get("chapters") or []
    chapters = [
        {
            "title": sanitize_plain_text(str(ch.get("title") or f"Chapter {i + 1}")),
            "start_time": float(ch.get("start_time", 0.0) or 0.0),
            "end_time": float(ch.get("end_time", 0.0) or 0.0),
        }
        for i, ch in enumerate(raw_chapters)
        if isinstance(ch, dict)
    ]

    if not segments and not info:
        raise FileValidationError(
            f"Could not extract captions or metadata for YouTube video '{video_id}'."
        )

    chunks = _build_chunks_from_transcript(segments, chapters)
    warnings: list[str] = []
    if not segments:
        warnings.append("No timed captions available; extracted chapters/metadata only.")

    visual_slides: list[dict[str, Any]] = []
    thumb_url = str(info.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
    if fetch_thumbnail:
        thumb_bytes = await _fetch_thumbnail_jpeg(thumb_url)
        if thumb_bytes:
            visual_slides.append(
                {
                    "filename": f"yt_{video_id}_thumb.jpg",
                    "data_bytes": thumb_bytes,
                    "mime_type": "image/jpeg",
                    "label": f"{title} — Thumbnail",
                    "timestamp": "00:00",
                    "citation": "[YouTube 00:00]",
                }
            )

    excerpt_lines: list[str] = []
    if channel:
        excerpt_lines.append(f"Channel: {channel} | Duration: {format_timestamp(duration_sec)}")
    if chapters:
        excerpt_lines.append(
            "Chapters: "
            + ", ".join(f"[{format_timestamp(c['start_time'])}] {c['title']}" for c in chapters)
        )
    for chunk in chunks:
        excerpt_lines.append(f"[{chunk['label']}]\n{chunk['text']}")

    raw_excerpt = "\n\n".join(excerpt_lines).strip()
    citations = [
        {"source": title, "timestamp": c["timestamp"], "citation": c["citation"]}
        for c in chunks
    ]
    file_hash = hashlib.sha256(f"youtube:{video_id}".encode("utf-8")).hexdigest()

    return NormalizedSourcePayload(
        source_type="youtube",
        title=title,
        origin_url=canonical_url,
        file_hash=file_hash,
        raw_text_excerpt=raw_excerpt,
        text_chunks=chunks,
        visual_slides=visual_slides,
        source_citations=citations,
        metadata={
            "video_id": video_id,
            "channel": channel,
            "yt_duration": duration_sec,
            "duration_formatted": format_timestamp(duration_sec),
            "chapter_count": len(chapters),
            "chapters": chapters,
            "caption_segment_count": len(segments),
            "thumbnail_url": thumb_url,
        },
        warnings=warnings,
    )


def parse_youtube(url: str, *, fetch_thumbnail: bool = True) -> NormalizedSourcePayload:
    """Synchronous wrapper around `parse_youtube_async`."""
    return asyncio.run(parse_youtube_async(url, fetch_thumbnail=fetch_thumbnail))


__all__ = [
    "extract_youtube_video_id",
    "format_timestamp",
    "is_youtube_url",
    "parse_youtube",
    "parse_youtube_async",
]
