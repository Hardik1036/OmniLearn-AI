"""Optional encrypted workspace push/pull sync client to Cloudflare Worker D1 & R2."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from backend.local_app.local_db import LocalDatabase
from backend.local_app.parsers import NormalizedSourcePayload


def _validate_cloud_base_url(cloud_url: str) -> str:
    raw = (cloud_url or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid Cloudflare Worker sync URL: '{cloud_url}'.")
    return raw


class CloudSyncClient:
    """Pushes and pulls sanitized workspace snapshots (never raw API keys) to Cloudflare Worker."""

    def __init__(self, cloud_url: str, *, admin_token: str = "", timeout: float = 20.0) -> None:
        self.cloud_url = _validate_cloud_base_url(cloud_url)
        self.admin_token = admin_token.strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.admin_token:
            headers["Authorization"] = f"Bearer {self.admin_token}"
        return headers

    @staticmethod
    def _parse_json_list(raw: Any) -> list[Any]:
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return raw if isinstance(raw, list) else []

    @classmethod
    def _iter_snapshot_slide_refs(cls, snapshot: dict[str, Any]) -> list[tuple[str, str]]:
        """Extract deduplicated `(course_id, slide_key)` pairs from a flat or nested workspace snapshot."""
        refs: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _collect(course_id: str, assets: list[Any]) -> None:
            for item in assets:
                url = str(item.get("url") if isinstance(item, dict) else item or "").strip()
                if not url.startswith("/api/slides/"):
                    continue
                slide_key = url[len("/api/slides/") :]
                if slide_key and slide_key not in seen:
                    seen.add(slide_key)
                    refs.append((course_id, slide_key))

        for src in snapshot.get("course_sources") or []:
            cid = str(src.get("course_id") or "").strip()
            assets = cls._parse_json_list(src.get("visual_assets_json") or src.get("visual_assets"))
            _collect(cid, assets)

        for sub in snapshot.get("subjects") or []:
            cid = str(sub.get("course_id") or "").strip()
            crops = cls._parse_json_list(sub.get("slide_crops"))
            _collect(cid, crops)

        for bundle in snapshot.get("courses") or []:
            if isinstance(bundle, dict) and isinstance(bundle.get("course"), dict):
                cid = str(bundle["course"].get("id") or "").strip()
                for src in bundle.get("sources") or []:
                    _collect(cid, cls._parse_json_list(src.get("visual_assets_json") or src.get("visual_assets")))
                for sub in bundle.get("subjects") or []:
                    _collect(cid, cls._parse_json_list(sub.get("slide_crops")))

        return refs

    async def push_workspace(
        self,
        db: LocalDatabase,
        *,
        course_id: str | None = None,
        upload_slides: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """Push local courses, sources, and slide JPEGs to `/api/sync/push` and `/api/slides/upload`."""
        snapshot = db.export_workspace_snapshot(course_id=course_id)
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)

        uploaded_slides = 0
        try:
            if upload_slides:
                for cid, slide_key in self._iter_snapshot_slide_refs(snapshot):
                    read_res = db.read_slide_bytes(slide_key)
                    if read_res is None:
                        continue
                    jpg_bytes, _ct = read_res
                    fname = slide_key.rsplit("/", 1)[-1]
                    files = {"file": (fname, jpg_bytes, "image/jpeg")}
                    data = {"course_id": cid, "key": slide_key, "filename": fname}
                    up_resp = await client.post(
                        f"{self.cloud_url}/api/slides/upload",
                        headers=self._headers(),
                        data=data,
                        files=files,
                    )
                    if up_resp.status_code < 300:
                        uploaded_slides += 1

            resp = await client.post(
                f"{self.cloud_url}/api/sync/push",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=snapshot,
            )
            resp.raise_for_status()
            payload = resp.json()
            return {
                "ok": True,
                "uploaded_slides": uploaded_slides,
                "remote": payload,
            }
        finally:
            if owns_client:
                await client.aclose()

    async def pull_workspace(
        self,
        db: LocalDatabase,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """Pull remote Cloudflare D1 snapshot from `POST /api/sync/pull` and merge into local SQLite."""
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)

        try:
            resp = await client.post(
                f"{self.cloud_url}/api/sync/pull",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={},
            )
            resp.raise_for_status()
            snapshot = resp.json()
            merge_res = db.import_workspace_snapshot(snapshot)
            return {"ok": True, **merge_res}
        finally:
            if owns_client:
                await client.aclose()

    async def push_video_slides(
        self,
        course_id: str,
        payload: NormalizedSourcePayload,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """Push OpenCV-sliced keyframe JPEGs directly to Cloudflare R2 `/api/slides/upload`."""
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)

        uploaded = 0
        try:
            for slide in payload.visual_slides:
                raw = slide.get("data_bytes")
                fname = slide.get("filename") or f"slide_{uploaded + 1}.jpg"
                if not isinstance(raw, (bytes, bytearray)) or not raw:
                    continue
                files = {"file": (fname, bytes(raw), "image/jpeg")}
                data = {"course_id": course_id, "key": f"courses/{course_id}/{fname}"}
                resp = await client.post(
                    f"{self.cloud_url}/api/slides/upload",
                    headers=self._headers(),
                    data=data,
                    files=files,
                )
                if resp.status_code < 300:
                    uploaded += 1
            return {"ok": True, "course_id": course_id, "uploaded_slides": uploaded}
        finally:
            if owns_client:
                await client.aclose()

    def push_video_slides_sync(self, course_id: str, payload: NormalizedSourcePayload) -> dict[str, Any]:
        """Synchronous CLI wrapper for `video_parser.py --sync-to-cloud`."""
        return asyncio.run(self.push_video_slides(course_id, payload))


__all__ = ["CloudSyncClient"]
