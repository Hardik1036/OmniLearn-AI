"""Unified source payload models and dispatcher for OmniLearn System App."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SourceParsingError(ValueError):
    """Base error raised when a source cannot be safely parsed or validated."""


class FileValidationError(SourceParsingError):
    """Raised when file size, magic bytes, or container structure fails validation."""


class SSRFBlockedError(SourceParsingError):
    """Raised when a URL fails strict SSRF or public-HTTP scheme validation."""


@dataclass
class NormalizedSourcePayload:
    """Standardized intermediate representation produced by all 9 source parsers."""

    source_type: str
    title: str
    origin_url: str = ""
    file_hash: str = ""
    raw_text_excerpt: str = ""
    text_chunks: list[dict[str, Any]] = field(default_factory=list)
    visual_slides: list[dict[str, Any]] = field(default_factory=list)
    discovered_links: list[dict[str, Any]] = field(default_factory=list)
    source_citations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.file_hash:
            seed = f"{self.source_type}:{self.origin_url}:{self.title}:{self.raw_text_excerpt[:1024]}"
            self.file_hash = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()

    @property
    def structured_chunks(self) -> list[dict[str, Any]]:
        """Alias matching course_sources.structured_chunks_json column."""
        return self.text_chunks

    def sanitized_visual_assets(self) -> list[dict[str, Any]]:
        """Return JSON-serializable slide metadata without raw binary payloads."""
        clean: list[dict[str, Any]] = []
        for slide in self.visual_slides:
            entry = {k: v for k, v in slide.items() if k != "data_bytes"}
            clean.append(entry)
        return clean

    def to_dict(self, include_bytes: bool = False) -> dict[str, Any]:
        """Serialize payload into a JSON-compatible dictionary."""
        slides = self.visual_slides if include_bytes else self.sanitized_visual_assets()
        return {
            "source_type": self.source_type,
            "title": self.title,
            "origin_url": self.origin_url,
            "file_hash": self.file_hash,
            "raw_text_excerpt": self.raw_text_excerpt,
            "text_chunks": self.text_chunks,
            "visual_slides": slides,
            "discovered_links": self.discovered_links,
            "source_citations": self.source_citations,
            "metadata": self.metadata,
            "warnings": self.warnings,
        }


class SourceDispatcher:
    """Routes local files and remote URLs to the appropriate OmniLearn parser."""

    DOC_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".xlsx", ".xls", ".ppt", ".pptx", ".csv"}
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}

    @classmethod
    def dispatch_file(
        cls,
        source: str | Path | bytes,
        filename: str | None = None,
        source_type: str | None = None,
        **kwargs: Any,
    ) -> NormalizedSourcePayload:
        """Parse a local document, spreadsheet, deck, text file, or video file."""
        from backend.local_app.parsers.doc_parsers import parse_document
        from backend.local_app.parsers.video_parser import parse_video

        resolved_name = filename or (Path(source).name if isinstance(source, (str, Path)) else "upload.bin")
        ext = Path(resolved_name).suffix.lower()

        if source_type == "video" or ext in cls.VIDEO_EXTENSIONS:
            if isinstance(source, (bytes, bytearray)):
                raise FileValidationError("Video slicing requires a readable local file path.")
            return parse_video(source, title=kwargs.pop("title", resolved_name), **kwargs)

        if ext in cls.DOC_EXTENSIONS or source_type in {"pdf", "txt", "xlsx", "ppt", "csv"}:
            return parse_document(source, filename=resolved_name, explicit_type=source_type, **kwargs)

        raise FileValidationError(f"Unsupported file type '{ext or source_type}'.")

    @classmethod
    async def adispatch_url(
        cls,
        url: str,
        selected_pdf_urls: list[str] | None = None,
        **kwargs: Any,
    ) -> NormalizedSourcePayload | list[NormalizedSourcePayload]:
        """Inspect or ingest a YouTube URL, direct PDF URL, or webpage (+ selected PDFs)."""
        from backend.local_app.parsers.web_crawler import (
            adownload_selected_pdfs,
            ainspect_url,
            validate_public_http_url,
        )
        from backend.local_app.parsers.youtube_parser import is_youtube_url, parse_youtube_async

        clean_url = validate_public_http_url(url)
        if is_youtube_url(clean_url):
            return await parse_youtube_async(clean_url, **kwargs)

        if selected_pdf_urls:
            return await adownload_selected_pdfs(selected_pdf_urls, **kwargs)

        inspection = await ainspect_url(clean_url, **kwargs)
        return inspection["payload"]


__all__ = [
    "FileValidationError",
    "NormalizedSourcePayload",
    "SSRFBlockedError",
    "SourceDispatcher",
    "SourceParsingError",
]
