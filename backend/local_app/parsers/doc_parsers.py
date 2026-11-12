"""Universal document parsers for PDF, TXT/MD, XLSX, CSV, and PPT/PPTX."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes

from backend.local_app.parsers import FileValidationError, NormalizedSourcePayload

MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_PDF_PAGES = 250
MAX_XLSX_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_PPTX_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_TEXT_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_SLIDE_JPEG_BYTES = 2_500_000  # 2.5 MB
MAX_ZIP_DECOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB ZIP bomb cap

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_JPEG_MAGIC = b"\xff\xd8\xff"

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_PURE_NEGATIVE_NUMBER_RE = re.compile(r"^-\d+(?:\.\d+)?$")
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_IFRAME_TAG_RE = re.compile(r"<iframe\b[^>]*>.*?</iframe>", re.IGNORECASE | re.DOTALL)


def safe_filename(name: str | Path, default: str = "document") -> str:
    """Strip directory traversal components and control characters from a filename."""
    base = Path(str(name)).name.replace("\x00", "").strip()
    base = re.sub(r"[^A-Za-z0-9._\- ]+", "_", base).strip(" .")
    return base or default


def sanitize_plain_text(text: str) -> str:
    """Clean extracted text by removing null bytes and dangerous active HTML tags."""
    if not text:
        return ""
    cleaned = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _SCRIPT_TAG_RE.sub("", cleaned)
    cleaned = _IFRAME_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def sanitize_spreadsheet_cell(value: Any, *, for_markdown: bool = False) -> str:
    """Neutralize spreadsheet formula injection (`=`, `+`, `-`, `@`, `\\t`, `\\r`)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)

    raw = str(value).replace("\x00", "")
    # Strip leading tabs/carriage-returns so obfuscated formulas cannot hide behind control chars.
    normalized = raw.replace("\r", " ").replace("\n", " ").strip()
    if not normalized:
        return ""

    raw_stripped = raw.lstrip(" ")
    if raw_stripped and raw_stripped[0] in _FORMULA_PREFIXES:
        candidate = raw_stripped.lstrip("\t\r ")
        if candidate and not _PURE_NEGATIVE_NUMBER_RE.fullmatch(candidate):
            normalized = f"'{candidate}"
        elif not candidate:
            normalized = f"'{normalized}"
    elif normalized[0] in _FORMULA_PREFIXES and not _PURE_NEGATIVE_NUMBER_RE.fullmatch(normalized):
        normalized = f"'{normalized}"

    if for_markdown:
        normalized = normalized.replace("|", "\\|")
    return normalized


sanitize_formula_cell = sanitize_spreadsheet_cell
neutralize_formula_injection = sanitize_spreadsheet_cell


def _read_source_bytes(
    source: str | Path | bytes | bytearray,
    filename: str | None,
    max_bytes: int,
) -> tuple[bytes, str]:
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        name = safe_filename(filename or "upload.bin")
    else:
        path = Path(source)
        if not path.is_file():
            raise FileValidationError(f"File not found: {path}")
        size = path.stat().st_size
        if size > max_bytes:
            raise FileValidationError(
                f"File '{path.name}' exceeds maximum size limit ({max_bytes // (1024 * 1024)} MB)."
            )
        data = path.read_bytes()
        name = safe_filename(filename or path.name)

    if not data:
        raise FileValidationError(f"File '{name}' is empty.")
    if len(data) > max_bytes:
        raise FileValidationError(
            f"File '{name}' exceeds maximum size limit ({max_bytes // (1024 * 1024)} MB)."
        )
    return data, name


def _validate_ooxml_zip(data: bytes, name: str) -> None:
    """Verify OOXML ZIP magic bytes and guard against ZIP decompression bombs."""
    if not data.startswith(_ZIP_MAGIC):
        raise FileValidationError(f"File '{name}' has invalid OOXML/ZIP magic header.")
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            total_uncompressed = 0
            for info in zf.infolist():
                if info.file_size < 0:
                    raise FileValidationError(f"Invalid archive entry size in '{name}'.")
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_ZIP_DECOMPRESSED_BYTES:
                    raise FileValidationError(
                        f"Archive '{name}' exceeds decompressed safety limit ({MAX_ZIP_DECOMPRESSED_BYTES // (1024 * 1024)} MB)."
                    )
                if info.file_size > 1_000_000 and info.compress_size > 0:
                    if (info.file_size / info.compress_size) > 120:
                        raise FileValidationError(
                            f"Archive '{name}' triggered ZIP decompression ratio protection."
                        )
    except zipfile.BadZipFile as exc:
        raise FileValidationError(f"Corrupt OOXML archive '{name}': {exc}") from exc


def _decode_text_bytes(data: bytes, name: str) -> str:
    """Decode bytes cleanly using UTF-8-SIG or charset-normalizer, rejecting binary blobs."""
    if b"\x00" in data[:8192]:
        raise FileValidationError(f"Binary null bytes detected; '{name}' is not valid plain text.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        best = from_bytes(data).best()
        if best is None:
            raise FileValidationError(f"Could not detect text encoding for '{name}'.")
        return str(best)


def _format_markdown_table(rows: list[list[str]], max_cols: int = 40) -> str:
    """Render sanitized 2D rows into a clean Markdown table."""
    if not rows:
        return ""
    col_count = min(max(len(r) for r in rows), max_cols)
    if col_count == 0:
        return ""

    padded = [r[:col_count] + [""] * max(0, col_count - len(r)) for r in rows]
    header = padded[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * col_count) + " |",
    ]
    for row in padded[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _render_pixmap_jpeg(page: Any, initial_dpi: int = 150) -> bytes | None:
    """Render a PyMuPDF page to JPEG <= 2.5 MB with clean DPI fallback."""
    for dpi in (initial_dpi, 110, 80):
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        try:
            img_bytes = pix.tobytes("jpeg", jpg_quality=85)
        except TypeError:
            img_bytes = pix.tobytes("jpeg")
        if img_bytes.startswith(_JPEG_MAGIC) and len(img_bytes) <= MAX_SLIDE_JPEG_BYTES:
            return img_bytes
    return None


def parse_pdf(
    source: str | Path | bytes | bytearray,
    filename: str | None = None,
    *,
    max_pages: int = MAX_PDF_PAGES,
    max_bytes: int = MAX_PDF_BYTES,
    render_diagrams: bool = True,
) -> NormalizedSourcePayload:
    """Parse a PDF document into page chunks and 150 DPI diagram JPEG slides."""
    import fitz  # PyMuPDF

    data, name = _read_source_bytes(source, filename or "document.pdf", max_bytes)
    if not data.startswith(_PDF_MAGIC):
        raise FileValidationError(f"File '{name}' does not have a valid %PDF- magic header.")

    file_hash = hashlib.sha256(data).hexdigest()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise FileValidationError(f"Failed to open PDF '{name}': {exc}") from exc

    try:
        total_pages = doc.page_count
        if total_pages <= 0:
            raise FileValidationError(f"PDF '{name}' contains zero pages.")

        processed_pages = min(total_pages, max_pages)
        warnings: list[str] = []
        if total_pages > max_pages:
            warnings.append(
                f"PDF truncated at {max_pages} of {total_pages} pages to stay within safety cap."
            )

        text_chunks: list[dict[str, Any]] = []
        visual_slides: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        excerpt_parts: list[str] = []
        stem = Path(name).stem

        for idx in range(processed_pages):
            page = doc.load_page(idx)
            page_num = idx + 1
            page_text = sanitize_plain_text(page.get_text("text"))
            citation = f"[From: {name} p.{page_num}]"

            has_images = len(page.get_images(full=True)) > 0
            has_drawings = len(page.get_drawings()) >= 5
            is_diagram_rich = has_images or has_drawings

            if page_text:
                text_chunks.append(
                    {
                        "index": idx,
                        "page": page_num,
                        "label": f"Page {page_num}",
                        "text": page_text,
                        "has_diagrams": is_diagram_rich,
                        "citation": citation,
                    }
                )
                excerpt_parts.append(f"[Page {page_num}]\n{page_text}")
                citations.append({"source": name, "page": page_num, "citation": citation})

            if render_diagrams and is_diagram_rich and len(visual_slides) < 60:
                img_bytes = _render_pixmap_jpeg(page, initial_dpi=150)
                if img_bytes:
                    visual_slides.append(
                        {
                            "filename": f"{stem}_p{page_num}.jpg",
                            "data_bytes": img_bytes,
                            "mime_type": "image/jpeg",
                            "label": f"{name} — Page {page_num} Diagram",
                            "page": page_num,
                            "citation": citation,
                        }
                    )

        raw_excerpt = "\n\n".join(excerpt_parts)
        if not raw_excerpt and not visual_slides:
            warnings.append("No extractable text or diagrams found in PDF.")

        return NormalizedSourcePayload(
            source_type="pdf",
            title=name,
            file_hash=file_hash,
            raw_text_excerpt=raw_excerpt,
            text_chunks=text_chunks,
            visual_slides=visual_slides,
            source_citations=citations,
            metadata={
                "filename": name,
                "page_count": total_pages,
                "processed_pages": processed_pages,
                "truncated_pages": total_pages > max_pages,
                "diagram_count": len(visual_slides),
                "size_bytes": len(data),
            },
            warnings=warnings,
        )
    finally:
        doc.close()


def parse_text(
    source: str | Path | bytes | bytearray,
    filename: str | None = None,
    *,
    max_bytes: int = MAX_TEXT_BYTES,
) -> NormalizedSourcePayload:
    """Parse `.txt` or `.md` plain-text / Markdown documents into structured chunks."""
    data, name = _read_source_bytes(source, filename or "notes.txt", max_bytes)
    file_hash = hashlib.sha256(data).hexdigest()
    decoded = sanitize_plain_text(_decode_text_bytes(data, name))

    if not decoded:
        raise FileValidationError(f"Text file '{name}' contains no readable text.")

    chunks: list[dict[str, Any]] = []
    sections = re.split(r"(?m)(?=^#{1,3}\s+.+$)", decoded)
    section_idx = 0
    for block in sections:
        block = block.strip()
        if not block:
            continue
        # Sub-split very long sections cleanly near paragraph boundaries.
        while len(block) > 2500:
            split_at = block.rfind("\n\n", 800, 2500)
            if split_at == -1:
                split_at = 2500
            part = block[:split_at].strip()
            block = block[split_at:].strip()
            if part:
                first_line = part.splitlines()[0].lstrip("# ").strip()[:80]
                chunks.append(
                    {
                        "index": section_idx,
                        "label": first_line or f"Section {section_idx + 1}",
                        "text": part,
                        "citation": f"[From: {name} §{section_idx + 1}]",
                    }
                )
                section_idx += 1
        if block:
            first_line = block.splitlines()[0].lstrip("# ").strip()[:80]
            chunks.append(
                {
                    "index": section_idx,
                    "label": first_line or f"Section {section_idx + 1}",
                    "text": block,
                    "citation": f"[From: {name} §{section_idx + 1}]",
                }
            )
            section_idx += 1

    citations = [
        {"source": name, "section": c["label"], "citation": c["citation"]}
        for c in chunks
    ]
    return NormalizedSourcePayload(
        source_type="txt",
        title=name,
        file_hash=file_hash,
        raw_text_excerpt=decoded,
        text_chunks=chunks,
        source_citations=citations,
        metadata={
            "filename": name,
            "char_count": len(decoded),
            "line_count": len(decoded.splitlines()),
            "chunk_count": len(chunks),
            "size_bytes": len(data),
        },
    )


def parse_xlsx(
    source: str | Path | bytes | bytearray,
    filename: str | None = None,
    *,
    max_bytes: int = MAX_XLSX_BYTES,
    max_rows_per_sheet: int = 500,
) -> NormalizedSourcePayload:
    """Parse `.xlsx` workbooks in read-only mode into formula-safe Markdown tables."""
    import openpyxl

    data, name = _read_source_bytes(source, filename or "workbook.xlsx", max_bytes)
    _validate_ooxml_zip(data, name)
    file_hash = hashlib.sha256(data).hexdigest()

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise FileValidationError(f"Failed to open XLSX workbook '{name}': {exc}") from exc

    try:
        chunks: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        warnings: list[str] = []
        excerpt_parts: list[str] = []
        sheet_names = list(wb.sheetnames)

        for idx, sheet_name in enumerate(sheet_names):
            ws = wb[sheet_name]
            sanitized_rows: list[list[str]] = []
            truncated = False
            raw_row_count = 0

            for row in ws.iter_rows(values_only=True):
                if raw_row_count >= max_rows_per_sheet:
                    truncated = True
                    break
                cells = [sanitize_spreadsheet_cell(cell, for_markdown=True) for cell in row]
                while cells and cells[-1] == "":
                    cells.pop()
                if any(cells):
                    sanitized_rows.append(cells)
                    raw_row_count += 1

            if truncated:
                warnings.append(
                    f"Sheet '{sheet_name}' truncated at {max_rows_per_sheet} rows."
                )
            if not sanitized_rows:
                continue

            md_table = _format_markdown_table(sanitized_rows)
            citation = f"[From: {name} • {sheet_name}]"
            chunks.append(
                {
                    "index": idx,
                    "sheet": sheet_name,
                    "label": f"Sheet: {sheet_name}",
                    "row_count": len(sanitized_rows),
                    "text": md_table,
                    "citation": citation,
                }
            )
            excerpt_parts.append(f"### Sheet: {sheet_name}\n{md_table}")
            citations.append({"source": name, "sheet": sheet_name, "citation": citation})

        return NormalizedSourcePayload(
            source_type="xlsx",
            title=name,
            file_hash=file_hash,
            raw_text_excerpt="\n\n".join(excerpt_parts),
            text_chunks=chunks,
            source_citations=citations,
            metadata={
                "filename": name,
                "sheet_names": sheet_names,
                "sheet_count": len(sheet_names),
                "size_bytes": len(data),
            },
            warnings=warnings,
        )
    finally:
        wb.close()


def parse_csv(
    source: str | Path | bytes | bytearray,
    filename: str | None = None,
    *,
    max_bytes: int = MAX_XLSX_BYTES,
    max_rows: int = 1000,
) -> NormalizedSourcePayload:
    """Parse CSV files using `csv.Sniffer`, neutralizing spreadsheet formula injections."""
    data, name = _read_source_bytes(source, filename or "table.csv", max_bytes)
    file_hash = hashlib.sha256(data).hexdigest()
    decoded = _decode_text_bytes(data, name)

    sample = "\n".join(decoded.splitlines()[:15])
    dialect: Any = csv.excel
    if sample.strip():
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

    reader = csv.reader(io.StringIO(decoded), dialect=dialect)
    rows: list[list[str]] = []
    warnings: list[str] = []

    for row_idx, row in enumerate(reader):
        if row_idx >= max_rows:
            warnings.append(f"CSV truncated at {max_rows} rows.")
            break
        cleaned = [sanitize_spreadsheet_cell(cell, for_markdown=True) for cell in row]
        if any(cleaned):
            rows.append(cleaned)

    if not rows:
        raise FileValidationError(f"CSV file '{name}' contains no rows.")

    headers = rows[0]
    md_table = _format_markdown_table(rows)
    citation = f"[From: {name}]"
    chunks = [
        {
            "index": 0,
            "label": f"CSV Table ({len(rows)} rows)",
            "headers": headers,
            "row_count": len(rows),
            "text": md_table,
            "citation": citation,
        }
    ]
    return NormalizedSourcePayload(
        source_type="csv",
        title=name,
        file_hash=file_hash,
        raw_text_excerpt=md_table,
        text_chunks=chunks,
        source_citations=[{"source": name, "citation": citation}],
        metadata={
            "filename": name,
            "headers": headers,
            "row_count": len(rows),
            "delimiter": getattr(dialect, "delimiter", ","),
            "size_bytes": len(data),
        },
        warnings=warnings,
    )


def _convert_legacy_ppt_if_available(data: bytes, name: str) -> bytes:
    """Attempt headless LibreOffice conversion for binary `.ppt` files if installed."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise FileValidationError(
            f"Legacy binary .ppt file '{name}' requires LibreOffice ('soffice') or saving as .pptx."
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = Path(tmpdir) / name
        src_path.write_bytes(data)
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "pptx", "--outdir", tmpdir, str(src_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        out_path = src_path.with_suffix(".pptx")
        if result.returncode != 0 or not out_path.is_file():
            raise FileValidationError(f"Failed to convert legacy .ppt '{name}' via LibreOffice.")
        return out_path.read_bytes()


def _normalize_image_to_jpeg(blob: bytes, ext: str) -> bytes | None:
    """Convert embedded slide images to validated JPEG <= 2.5 MB using PyMuPDF."""
    if blob.startswith(_JPEG_MAGIC) and len(blob) <= MAX_SLIDE_JPEG_BYTES:
        return blob
    try:
        import fitz

        pix = fitz.Pixmap(blob)
        if pix.alpha or (pix.colorspace and pix.colorspace.n > 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        try:
            jpg = pix.tobytes("jpeg", jpg_quality=85)
        except TypeError:
            jpg = pix.tobytes("jpeg")
        if jpg.startswith(_JPEG_MAGIC) and len(jpg) <= MAX_SLIDE_JPEG_BYTES:
            return jpg
    except Exception:
        return None
    return None


def parse_pptx(
    source: str | Path | bytes | bytearray,
    filename: str | None = None,
    *,
    max_bytes: int = MAX_PPTX_BYTES,
) -> NormalizedSourcePayload:
    """Parse `.pptx` (and optionally converted `.ppt`) decks: titles, bullets, tables, notes, images."""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    data, name = _read_source_bytes(source, filename or "slides.pptx", max_bytes)
    file_hash = hashlib.sha256(data).hexdigest()

    if data.startswith(_OLE2_MAGIC):
        data = _convert_legacy_ppt_if_available(data, name)

    _validate_ooxml_zip(data, name)

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise FileValidationError(f"Failed to open presentation '{name}': {exc}") from exc

    chunks: list[dict[str, Any]] = []
    visual_slides: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    excerpt_parts: list[str] = []
    warnings: list[str] = []
    stem = Path(name).stem

    for idx, slide in enumerate(prs.slides):
        slide_num = idx + 1
        title_shape = getattr(slide.shapes, "title", None)
        slide_title = (
            sanitize_plain_text(title_shape.text)
            if title_shape is not None and getattr(title_shape, "has_text_frame", False)
            else ""
        )
        slide_title = slide_title or f"Slide {slide_num}"

        body_lines: list[str] = []
        table_blocks: list[str] = []

        for shape in slide.shapes:
            if title_shape is not None and shape == title_shape:
                continue

            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    txt = sanitize_plain_text(para.text)
                    if txt:
                        indent = "  " * max(0, getattr(para, "level", 0))
                        body_lines.append(f"{indent}- {txt}")

            if getattr(shape, "has_table", False):
                t_rows: list[list[str]] = []
                for row in shape.table.rows:
                    t_rows.append(
                        [sanitize_spreadsheet_cell(cell.text, for_markdown=True) for cell in row.cells]
                    )
                if t_rows:
                    table_blocks.append(_format_markdown_table(t_rows))

            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE and hasattr(shape, "image"):
                try:
                    img_blob = shape.image.blob
                    img_ext = getattr(shape.image, "ext", "png")
                    jpg_bytes = _normalize_image_to_jpeg(img_blob, img_ext)
                    if jpg_bytes and len(visual_slides) < 80:
                        img_idx = len(visual_slides) + 1
                        visual_slides.append(
                            {
                                "filename": f"{stem}_s{slide_num}_{img_idx}.jpg",
                                "data_bytes": jpg_bytes,
                                "mime_type": "image/jpeg",
                                "label": f"{slide_title} (Slide {slide_num})",
                                "page": slide_num,
                                "citation": f"[From: {name} Slide {slide_num}]",
                            }
                        )
                except Exception:
                    warnings.append(f"Skipped unreadable embedded image on Slide {slide_num}.")

        notes_text = ""
        if getattr(slide, "has_notes_slide", False):
            notes_frame = slide.notes_slide.notes_text_frame
            if notes_frame is not None:
                notes_text = sanitize_plain_text(notes_frame.text)

        parts: list[str] = [f"## Slide {slide_num}: {slide_title}"]
        if body_lines:
            parts.append("\n".join(body_lines))
        if table_blocks:
            parts.extend(table_blocks)
        if notes_text:
            parts.append(f"Speaker Notes: {notes_text}")

        slide_block = "\n".join(parts)
        citation = f"[From: {name} Slide {slide_num}]"
        chunks.append(
            {
                "index": idx,
                "slide": slide_num,
                "label": f"Slide {slide_num}: {slide_title}",
                "title": slide_title,
                "text": slide_block,
                "notes": notes_text,
                "citation": citation,
            }
        )
        excerpt_parts.append(slide_block)
        citations.append({"source": name, "slide": slide_num, "citation": citation})

    return NormalizedSourcePayload(
        source_type="ppt",
        title=name,
        file_hash=file_hash,
        raw_text_excerpt="\n\n".join(excerpt_parts),
        text_chunks=chunks,
        visual_slides=visual_slides,
        source_citations=citations,
        metadata={
            "filename": name,
            "slide_count": len(prs.slides),
            "image_count": len(visual_slides),
            "size_bytes": len(data),
        },
        warnings=warnings,
    )


def parse_document(
    source: str | Path | bytes | bytearray,
    filename: str | None = None,
    explicit_type: str | None = None,
    **kwargs: Any,
) -> NormalizedSourcePayload:
    """Dispatch document bytes or file path to the matching parser."""
    inferred_name = filename or (Path(source).name if isinstance(source, (str, Path)) else "document")
    ext = Path(inferred_name).suffix.lower()
    kind = (explicit_type or "").lower().strip()

    if kind == "pdf" or ext == ".pdf":
        return parse_pdf(source, filename=inferred_name, **kwargs)
    if kind in {"txt", "md", "markdown"} or ext in {".txt", ".md", ".markdown"}:
        return parse_text(source, filename=inferred_name, **kwargs)
    if kind in {"xlsx", "xls"} or ext in {".xlsx", ".xls"}:
        return parse_xlsx(source, filename=inferred_name, **kwargs)
    if kind == "csv" or ext == ".csv":
        return parse_csv(source, filename=inferred_name, **kwargs)
    if kind in {"ppt", "pptx"} or ext in {".ppt", ".pptx"}:
        return parse_pptx(source, filename=inferred_name, **kwargs)

    raise FileValidationError(f"Unsupported document format: '{ext or kind}'.")


__all__ = [
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "MAX_PPTX_BYTES",
    "MAX_SLIDE_JPEG_BYTES",
    "MAX_TEXT_BYTES",
    "MAX_XLSX_BYTES",
    "neutralize_formula_injection",
    "parse_csv",
    "parse_document",
    "parse_pdf",
    "parse_pptx",
    "parse_text",
    "parse_xlsx",
    "safe_filename",
    "sanitize_formula_cell",
    "sanitize_plain_text",
    "sanitize_spreadsheet_cell",
]
