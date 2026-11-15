"""Publish OmniLearn AI Desktop System App Release to GitHub."""

import os
import subprocess
import sys
from pathlib import Path
import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
ZIP_PATH = DIST_DIR / "OmniLearn-Windows-x64-v1.6.0.zip"
SHA_PATH = DIST_DIR / "OmniLearn-Windows-x64-v1.6.0.zip.sha256"


def get_github_token() -> str:
    proc = subprocess.Popen(
        ["git", "credential", "fill"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, _ = proc.communicate("protocol=https\nhost=github.com\n")
    for line in stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> None:
    token = get_github_token()
    if not token:
        print("Error: Could not retrieve GitHub token from git credential manager.")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "OmniLearn-Release-Publisher",
        "Accept": "application/vnd.github.v3+json",
    }

    print("Checking GitHub user...")
    user_res = httpx.get("https://api.github.com/user", headers=headers)
    if user_res.status_code != 200:
        print(f"Error authenticating with GitHub: {user_res.status_code} {user_res.text}")
        sys.exit(1)
    print(f"Authenticated as: {user_res.json().get('login')}")

    # Check if release v1.6.0 already exists
    existing = httpx.get("https://api.github.com/repos/Hardik1036/OmniLearn-AI/releases/tags/v1.6.0", headers=headers)
    if existing.status_code == 200:
        release = existing.json()
        print(f"Release v1.6.0 already exists (id: {release['id']})")
    else:
        release_data = {
            "tag_name": "v1.6.0",
            "target_commitish": "main",
            "name": "OmniLearn AI Desktop System App v1.6.0",
            "body": (
                "# OmniLearn AI Desktop System App v1.6.0 🚀\n\n"
                "## 📦 What's New in Version 1.6.0\n\n"
                "- **Standalone Native Windows System App**: Bundled portable executable (`OmniLearn.exe`) powered by Edge WebView2, local FastAPI backend, SQLite WAL/FTS5 full-text indexing, and OpenCV multi-window lecture video parsing.\n"
                "- **AI Study Tutor & Application Assistant Chatbot**: Integrated academic tutor for doubts clearing (KaTeX math, concepts, code) and application feature navigation.\n"
                "- **Academic PDF Notes Exporter**: Export unified study notes with extracted slide visuals and diagrams as clean formatted PDFs.\n"
                "- **Multi-Window Lecture Parsing**: Extract split-screen slides and transcripts from lecture videos (2x2 grid or 1x2 split screen).\n"
                "- **Offline Private Storage & Smart Search**: Instant full-text search across all notes, slides, and transcripts with zero cloud limits.\n\n"
                "---\n\n"
                "### 💻 Download & Run (Windows x64)\n\n"
                "1. Download **`OmniLearn-Windows-x64-v1.6.0.zip`** below.\n"
                "2. Extract the ZIP archive to your desired folder.\n"
                "3. Double-click **`OmniLearn.exe`**.\n\n"
                "### 🔒 Cryptographic Verification\n"
                "- **File**: `OmniLearn-Windows-x64-v1.6.0.zip`\n"
                "- **SHA-256 Checksum**: `765b787f333bd554599d058c0e05bb25ee16e46ccb11ad58baf3aa4d409a45ad`\n"
            ),
            "draft": False,
            "prerelease": False,
        }
        res = httpx.post("https://api.github.com/repos/Hardik1036/OmniLearn-AI/releases", headers=headers, json=release_data)
        if res.status_code not in (200, 201):
            print(f"Failed to create release: {res.status_code} {res.text}")
            sys.exit(1)
        release = res.json()
        print(f"Created release: {release['html_url']}")

    release_id = release["id"]
    upload_url_base = release["upload_url"].split("{")[0]

    # Check existing assets
    existing_assets = {a["name"]: a["id"] for a in release.get("assets", [])}

    # Upload files
    files_to_upload = [
        (ZIP_PATH, "OmniLearn-Windows-x64-v1.6.0.zip", "application/zip"),
        (SHA_PATH, "OmniLearn-Windows-x64-v1.6.0.zip.sha256", "text/plain"),
    ]

    for file_path, asset_name, content_type in files_to_upload:
        if not file_path.is_file():
            print(f"Warning: {file_path} does not exist, skipping.")
            continue

        if asset_name in existing_assets:
            print(f"Asset {asset_name} already exists. Deleting prior upload...")
            del_res = httpx.delete(
                f"https://api.github.com/repos/Hardik1036/OmniLearn-AI/releases/assets/{existing_assets[asset_name]}",
                headers=headers,
            )
            print(f"Delete status: {del_res.status_code}")

        file_size = file_path.stat().st_size
        print(f"Uploading {asset_name} ({file_size / (1024*1024):.1f} MB)...")

        upload_headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "OmniLearn-Release-Publisher",
            "Content-Type": content_type,
            "Content-Length": str(file_size),
        }

        with open(file_path, "rb") as f:
            data = f.read()

        upload_res = httpx.post(
            f"{upload_url_base}?name={asset_name}",
            headers=upload_headers,
            content=data,
            timeout=120.0,
        )

        if upload_res.status_code in (200, 201):
            print(f"Successfully uploaded {asset_name}!")
        else:
            print(f"Upload failed for {asset_name}: {upload_res.status_code} {upload_res.text}")

    print("\nRelease v1.6.0 is now live!")
    print(f"Visit: {release['html_url']}")
    print("Latest link: https://github.com/Hardik1036/OmniLearn-AI/releases/latest")


if __name__ == "__main__":
    main()
