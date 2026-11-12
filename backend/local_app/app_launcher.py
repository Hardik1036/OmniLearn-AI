"""Native OS Desktop launcher using pywebview + loopback FastAPI/uvicorn."""

from __future__ import annotations

import argparse
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from backend.local_app.local_api import create_app


def find_free_loopback_port(preferred_port: int = 0) -> int:
    """Bind an ephemeral or preferred port strictly on `127.0.0.1` and return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(preferred_port)))
        return int(sock.getsockname()[1])


def _wait_for_server(port: int, timeout_sec: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=0.5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def launch(
    *,
    headless: bool = False,
    port: int = 0,
    db_path: str | Path | None = None,
    require_token: bool = True,
) -> int:
    """Start loopback FastAPI engine and open pywebview window (or run headless)."""
    bound_port = find_free_loopback_port(port)
    local_token = secrets.token_urlsafe(32) if require_token else ""

    app = create_app(
        db_path=db_path,
        local_token=local_token,
        require_local_token=require_token,
    )

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=bound_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    if headless:
        print(f"OmniLearn System App (Headless) listening on http://127.0.0.1:{bound_port}/", flush=True)
        if local_token:
            print(f"Launch URL: http://127.0.0.1:{bound_port}/?local_token={local_token}", flush=True)
        server.run()
        return 0

    thread = threading.Thread(target=server.run, name="omnilearn-uvicorn", daemon=True)
    thread.start()

    if not _wait_for_server(bound_port, timeout_sec=12.0):
        server.should_exit = True
        raise RuntimeError("Local OmniLearn API server failed to start within 12 seconds.")

    launch_url = (
        f"http://127.0.0.1:{bound_port}/?local_token={local_token}"
        if local_token
        else f"http://127.0.0.1:{bound_port}/"
    )

    try:
        import webview

        webview.create_window(
            "OmniLearn AI — System Edition",
            launch_url,
            width=1280,
            height=820,
            min_size=(380, 640),
        )
        webview.start()
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OmniLearn AI — Full Local System Application")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the local FastAPI engine without launching a desktop GUI window.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Loopback port on 127.0.0.1 (0 picks a free ephemeral port).",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Optional override path for local SQLite database.",
    )
    parser.add_argument(
        "--no-token",
        action="store_true",
        help="Disable per-launch X-Local-App-Token guard (for manual local debugging only).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return launch(
        headless=bool(args.headless),
        port=int(args.port),
        db_path=args.db_path,
        require_token=not bool(args.no_token),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_arg_parser", "find_free_loopback_port", "launch", "main"]
