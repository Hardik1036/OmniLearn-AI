"""High-speed OpenCV multi-window quadrant delta slicer and optional cloud sync CLI."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from backend.local_app.parsers import FileValidationError, NormalizedSourcePayload
from backend.local_app.parsers.doc_parsers import MAX_SLIDE_JPEG_BYTES, safe_filename

MAX_SLIDE_WIDTH = 960

_QUADRANT_SPECS: dict[str, list[tuple[str, str, int, int, int, int, str]]] = {
    "2x2": [
        ("top_left", "Window 1 (Top-Left)", 0, 500, 0, 500, "#3B82F6"),
        ("top_right", "Window 2 (Top-Right)", 0, 500, 500, 1000, "#10B981"),
        ("bottom_left", "Window 3 (Bottom-Left)", 500, 1000, 0, 500, "#8B5CF6"),
        ("bottom_right", "Window 4 (Bottom-Right)", 500, 1000, 500, 1000, "#F59E0B"),
    ],
    "1x2": [
        ("left", "Window 1 (Left)", 0, 1000, 0, 500, "#3B82F6"),
        ("right", "Window 2 (Right)", 0, 1000, 500, 1000, "#10B981"),
    ],
    "2x1": [
        ("top", "Window 1 (Top)", 0, 500, 0, 1000, "#3B82F6"),
        ("bottom", "Window 2 (Bottom)", 500, 1000, 0, 1000, "#8B5CF6"),
    ],
    "1x1": [
        ("full", "Full Frame", 0, 1000, 0, 1000, "#3B82F6"),
    ],
}


def _fmt_ts(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def compute_quadrant_rois(
    width: int,
    height: int,
    layout_type: str = "2x2",
    taskbar_crop_px: int = 48,
) -> list[dict[str, Any]]:
    """Compute normalized (0..1000) and pixel ROIs while cropping bottom OS taskbar height."""
    layout = layout_type if layout_type in _QUADRANT_SPECS else "2x2"
    safe_crop = max(0, min(taskbar_crop_px, max(0, height - 16)))
    effective_h = max(16, height - safe_crop)

    rois: list[dict[str, Any]] = []
    for key, label, ymin, ymax, xmin, xmax, color in _QUADRANT_SPECS[layout]:
        y1 = int(effective_h * ymin / 1000)
        y2 = int(effective_h * ymax / 1000)
        x1 = int(width * xmin / 1000)
        x2 = int(width * xmax / 1000)
        if y2 <= y1:
            y2 = min(effective_h, y1 + 1)
        if x2 <= x1:
            x2 = min(width, x1 + 1)
        rois.append(
            {
                "roi_key": key,
                "name": label,
                "ymin": ymin,
                "ymax": ymax,
                "xmin": xmin,
                "xmax": xmax,
                "color": color,
                "px_rect": (x1, y1, x2, y2),
            }
        )
    return rois


def _resize_if_wide(frame: np.ndarray, max_width: int = MAX_SLIDE_WIDTH) -> np.ndarray:
    h, w = frame.shape[:2]
    if w <= max_width or w <= 0:
        return frame
    scale = max_width / float(w)
    new_h = max(1, int(h * scale))
    return cv2.resize(frame, (max_width, new_h), interpolation=cv2.INTER_AREA)


def _encode_jpeg_within_limit(frame: np.ndarray) -> bytes | None:
    resized = _resize_if_wide(frame)
    for quality in (85, 72, 58):
        ok, buf = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok:
            blob = buf.tobytes()
            if len(blob) <= MAX_SLIDE_JPEG_BYTES:
                return blob
    return None


def _hash_file_head_and_size(path: Path) -> str:
    """Fast SHA-256 over file size + head/tail chunks so multi-GB videos hash in milliseconds."""
    size = path.stat().st_size
    h = hashlib.sha256()
    h.update(f"size:{size}:".encode("utf-8"))
    with path.open("rb") as fh:
        h.update(fh.read(1024 * 1024))
        if size > 1024 * 1024:
            fh.seek(max(0, size - 1024 * 1024))
            h.update(fh.read(1024 * 1024))
    return h.hexdigest()


def parse_video(
    video_path: str | Path,
    *,
    layout_type: str = "2x2",
    taskbar_crop_px: int = 48,
    sample_interval_sec: float = 2.0,
    delta_threshold: float = 0.035,
    max_slides_per_quadrant: int = 60,
    rois: list[dict[str, Any]] | None = None,
    title: str | None = None,
    subject_name: str = "",
) -> NormalizedSourcePayload:
    """Slice a multi-window video into quadrant lecture keyframes for one target subject."""
    path = Path(video_path)
    if not path.is_file():
        raise FileValidationError(f"Video file not found: {path}")

    safe_name = safe_filename(title or path.name, default="lecture.mp4")
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileValidationError(f"OpenCV could not open video '{safe_name}'.")

    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 0 or fps > 240:
            fps = 25.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if width <= 0 or height <= 0:
            raise FileValidationError(f"Video '{safe_name}' has invalid dimensions ({width}x{height}).")

        duration_sec = (frame_count / fps) if frame_count > 0 else 0.0
        layout = layout_type if layout_type in _QUADRANT_SPECS else "2x2"
        clean_subject = (subject_name or "").strip()
        active_rois = rois or compute_quadrant_rois(width, height, layout, taskbar_crop_px)
        lecture_names = {
            r["roi_key"]: (
                f"{clean_subject} — Lecture {idx + 1}" if clean_subject else f"Lecture {idx + 1} ({r['name']})"
            )
            for idx, r in enumerate(active_rois)
        }
        step_frames = max(1, int(round(fps * max(0.25, sample_interval_sec))))

        prev_thumbs: dict[str, np.ndarray] = {}
        slide_counts: dict[str, int] = {r["roi_key"]: 0 for r in active_rois}
        visual_slides: list[dict[str, Any]] = []
        text_chunks: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []

        frame_idx = 0
        total_limit = frame_count if frame_count > 0 else 10_000_000

        while frame_idx < total_limit:
            if frame_idx > 0 and frame_count > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                break

            ts_sec = frame_idx / fps
            ts_str = _fmt_ts(ts_sec)

            for roi in active_rois:
                qkey = roi["roi_key"]
                if slide_counts[qkey] >= max_slides_per_quadrant:
                    continue
                if "px_rect" in roi:
                    x1, y1, x2, y2 = roi["px_rect"]
                else:
                    eff_h = max(16, height - max(0, min(taskbar_crop_px, height - 16)))
                    x1 = int(width * roi["xmin"] / 1000)
                    x2 = int(width * roi["xmax"] / 1000)
                    y1 = int(eff_h * roi["ymin"] / 1000)
                    y2 = int(eff_h * roi["ymax"] / 1000)

                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue

                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                thumb = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
                prev = prev_thumbs.get(qkey)
                if prev is not None:
                    delta = float(np.mean(np.abs(thumb.astype(np.int16) - prev.astype(np.int16)))) / 255.0
                    if delta < delta_threshold:
                        continue

                jpg_bytes = _encode_jpeg_within_limit(crop)
                if not jpg_bytes:
                    continue

                prev_thumbs[qkey] = thumb
                slide_counts[qkey] += 1
                slide_num = slide_counts[qkey]
                lecture_label = lecture_names.get(qkey, roi["name"])
                fname = f"{Path(safe_name).stem}_{qkey}_{slide_num:03d}.jpg"
                citation = f"[From: {safe_name} • {lecture_label} {ts_str}]"

                visual_slides.append(
                    {
                        "filename": fname,
                        "data_bytes": jpg_bytes,
                        "mime_type": "image/jpeg",
                        "quadrant_key": qkey,
                        "lecture_label": lecture_label,
                        "label": f"{lecture_label} @ {ts_str}",
                        "timestamp": ts_str,
                        "timestamp_sec": round(ts_sec, 2),
                        "citation": citation,
                    }
                )
                text_chunks.append(
                    {
                        "index": len(text_chunks),
                        "quadrant_key": qkey,
                        "lecture_label": lecture_label,
                        "label": f"{lecture_label} ({ts_str})",
                        "timestamp": ts_str,
                        "timestamp_sec": round(ts_sec, 2),
                        "text": f"Lecture visual frame #{slide_num} from {lecture_label} at {ts_str}. Visual asset: {fname}",
                        "citation": citation,
                    }
                )
                citations.append({"source": safe_name, "quadrant": qkey, "lecture_label": lecture_label, "timestamp": ts_str, "citation": citation})

            if frame_count <= 0 and all(v >= max_slides_per_quadrant for v in slide_counts.values()):
                break
            frame_idx += step_frames

        if duration_sec == 0.0 and frame_idx > 0:
            duration_sec = frame_idx / fps

        serializable_rois = [
            {k: v for k, v in r.items() if k != "px_rect"} for r in active_rois
        ]
        summary_lines = [
            (
                f"Multi-window video '{safe_name}' for subject '{clean_subject or 'Course Subject'}' "
                f"({width}x{height}, {_fmt_ts(duration_sec)}, layout={layout}; "
                f"all {len(active_rois)} window(s) = distinct lectures)."
            ),
            "Extracted lecture keyframes per quadrant: "
            + ", ".join(f"{lecture_names.get(k, k)}={v}" for k, v in slide_counts.items()),
        ]

        return NormalizedSourcePayload(
            source_type="video",
            title=safe_name,
            file_hash=_hash_file_head_and_size(path),
            raw_text_excerpt="\n".join(summary_lines),
            text_chunks=text_chunks,
            visual_slides=visual_slides,
            source_citations=citations,
            metadata={
                "filename": safe_name,
                "layout_type": layout,
                "quadrant_mode": "multi_lecture_single_subject",
                "subject_name": clean_subject,
                "lecture_labels": lecture_names,
                "resolution": f"{width}x{height}",
                "width": width,
                "height": height,
                "fps": round(fps, 3),
                "duration_sec": round(duration_sec, 2),
                "taskbar_crop_px": taskbar_crop_px,
                "rois": serializable_rois,
                "slide_counts": slide_counts,
                "total_slides": len(visual_slides),
            },
        )
    finally:
        cap.release()


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OmniLearn OpenCV Multi-Window Slicer")
    parser.add_argument("--video", required=True, help="Path to local video file (.mp4/.mkv/.webm)")
    parser.add_argument("--layout", default="2x2", choices=["2x2", "1x2", "2x1", "1x1"])
    parser.add_argument("--taskbar-crop", type=int, default=48, help="Bottom taskbar crop in pixels")
    parser.add_argument("--interval", type=float, default=2.0, help="Sampling interval in seconds")
    parser.add_argument("--threshold", type=float, default=0.035, help="Keyframe pixel delta threshold")
    parser.add_argument("--course-id", default="", help="Target course ID when syncing")
    parser.add_argument("--title", default="", help="Override source title")
    parser.add_argument("--sync-to-cloud", action="store_true", help="Push sliced slides to Cloudflare Worker")
    parser.add_argument("--cloud-url", default=os.getenv("OMNILEARN_CLOUD_URL", ""), help="Cloudflare Worker base URL")
    parser.add_argument("--token", default=os.getenv("OMNILEARN_SYNC_TOKEN", ""), help="Admin JWT token for sync")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(argv)
    payload = parse_video(
        args.video,
        layout_type=args.layout,
        taskbar_crop_px=args.taskbar_crop,
        sample_interval_sec=args.interval,
        delta_threshold=args.threshold,
        title=args.title or None,
    )
    print(
        f"Sliced '{payload.title}': {payload.metadata.get('total_slides', 0)} keyframes "
        f"across {payload.metadata.get('layout_type')} layout."
    )
    if args.sync_to_cloud:
        if not args.cloud_url or not args.token:
            print("Error: --sync-to-cloud requires --cloud-url and --token.")
            return 2
        from backend.local_app.sync_client import CloudSyncClient

        client = CloudSyncClient(args.cloud_url, admin_token=args.token)
        result = client.push_video_slides_sync(args.course_id or payload.file_hash[:12], payload)
        print(f"Cloud sync result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_SLIDE_WIDTH",
    "compute_quadrant_rois",
    "main",
    "parse_video",
]
