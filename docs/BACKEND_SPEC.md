# ⚙️ Backend Architecture & Implementation Specification
# OmniLearn: Dual Python Architecture (Full Local System App + Cloudflare Web Lite Worker)

---

## 1. Dual-Runtime Backend Philosophy ($0.00 / Month)

Because heavy document rendering, arbitrary webpage PDF crawling, YouTube extraction, OpenCV multi-hour video processing, and local SQLite FTS5 indexing cannot fit inside Cloudflare's **10 ms CPU / 128 MB RAM** edge sandbox or survive browser CORS rules, OmniLearn uses **two clean Python backend packages sharing identical JSON API contracts**:

1. **`backend/local_app/` — Full Downloaded System Application Engine (Python 3.13 + FastAPI + `pywebview`)**:
   - Runs natively on **Windows (`.exe`), macOS (`.dmg`), Linux (`.AppImage`)** (and serves the exact API contract for LAN/local Android `.apk` sync).
   - Zero artificial limits: full CORS-free `httpx` crawling, **PyMuPDF (`fitz`)** PDF text+image rendering, **`openpyxl`** XLSX parsing, **`python-pptx`** slide extraction, **`trafilatura`** web article extraction + Website `.pdf` link harvesting, **`youtube-transcript-api` + `yt-dlp`** YouTube ingestion, **`cv2`** multi-window OpenCV video slicing, unlimited **Local SQLite (`WAL + FTS5`)**, unlimited Multi-API key pool, and optional encrypted sync to Cloudflare!
2. **`backend/src/` — Cloudflare Free-Plan Worker (`python_workers`, Web Lite Portal, Release Hub & Sync API)**:
   - Runs at **$0/month** on Cloudflare Workers Free (`<= 10 ms CPU`, `<= 128 MB RAM`).
   - Enforces `RUNTIME_MODE = "web_lite"` via `backend/src/edition_guard.py`: serves the OS-aware **Download Hub** (`/api/releases/latest`), allows small sandbox previews (`.txt`/`.csv` `<= 500 KB`, `<= 8` browser canvas slides, 3 OpenAlex search hits, 1 API slot), rejects locked heavy endpoints cleanly with `HTTP 403 EDITION_LIMIT_EXCEEDED`, serves the authenticated AI relay (`POST /api/ai/relay`) so downloaded System App users without personal API keys can use Worker secret defaults securely, and handles encrypted Admin backup sync!

---

## 2. File-by-File Specification: Part A — Local Full System Application (`backend/local_app/`)

```
backend/local_app/
├── app_launcher.py                 # pywebview native OS window + uvicorn loopback + tray icon
├── env_config.py                   # Stdlib .env loader (Admin User ID, Password, Multi-API keys, Google OAuth)
├── gemini_local.py                 # Online AI Pool client & 4-quadrant 1-subject / unlimited-subject synthesizer
├── google_auth.py                  # Google OAuth 2.0 & ID token verification for normal Gmail users
├── local_api.py                    # FastAPI router on 127.0.0.1:<port> mirroring /api/* routes
├── local_db.py                     # SQLite 3 (WAL + FTS5 full-text search) + Keychain/AES-GCM
├── source_search.py                # In-App Multi-Provider Search (OpenAlex, arXiv, Wiki, DDG, YT)
├── sync_client.py                  # Optional encrypted push/pull sync to Cloudflare D1 & R2
└── parsers/
    ├── __init__.py                 # Unified SourceDispatcher + NormalizedSourcePayload dataclass
    ├── doc_parsers.py              # PDF (PyMuPDF), TXT/MD, XLSX (openpyxl), PPT/PPTX, CSV
    ├── web_crawler.py              # Safe SSRF-guarded webpage scraper & Website PDF link harvester
    ├── youtube_parser.py           # YouTube transcript, chapters, metadata & thumbnail extractor
    └── video_parser.py             # High-speed OpenCV 2x2/1x2/2x1/1x1 slicer + CLI mode
```

### File A1: `backend/local_app/app_launcher.py` (Native Desktop System Wrapper)
- **Purpose**: Launches OmniLearn as a standalone desktop system application—not a browser tab.
- **Libraries Used**: `webview` (**pywebview** using Edge WebView2 on Windows, WKWebView on macOS, WebKitGTK on Linux), `uvicorn`, `threading`, `socket` (ephemeral loopback port binding), `sys`.
- **Capabilities**:
  - Finds an open ephemeral port on `127.0.0.1` with a per-launch random `X-Local-App-Token` preventing external local processes from querying the API.
  - Starts FastAPI cleanly inside a daemon thread serving `frontend/` static assets + `/api/*`.
  - Opens a native OS window (`"OmniLearn AI — System Edition"`, `1280x820`, persistent window bounds, native file dialogs & drag-and-drop).

### File A2: `backend/local_app/local_api.py` (Local FastAPI Server)
- **Purpose**: Exposes the unified REST endpoints expected by `frontend/js/api-client.js`, declaring `"edition": "system_app"` in `/api/health` and `/api/auth/status` so all UI locks unlock automatically.
- **Libraries Used**: `fastapi`, `pydantic`, `python-multipart` (streaming uploads), `uvicorn`.
- **Additional Full-App Endpoints**:
  - `POST /api/sources/search`: Runs in-app search across Web, Direct PDFs, Academic Papers, and YouTube.
  - `POST /api/sources/inspect-url`: Crawls any website URL, returns article summary AND a selectable list of every `.pdf` link hosted on that webpage.
  - `POST /api/sources/ingest-file`: Accepts multipart `.pdf`, `.txt`, `.xlsx`, `.ppt`/`.pptx`, `.csv`, or `.mp4/.mkv`.
  - `POST /api/sources/ingest-url`: Downloads a direct `.pdf` URL, webpage URL, multiple checked PDF URLs from a webpage, or YouTube URL directly into the course.
  - `GET /api/sources/fts?q=...&course_id=...`: Instant SQLite FTS5 keyword search across all ingested materials.
  - `POST /api/sync/push` & `POST /api/sync/pull`: Syncs workspace to Admin's Cloudflare D1/R2 account when online.

### File A3: `backend/local_app/source_search.py` (In-App Research & Discovery Aggregator)
- **Purpose**: Lets users search for study materials *inside the Sources tab itself* with **$0.00 paid search APIs**.
- **Libraries Used**: `httpx` (async HTTP/2), `beautifulsoup4`, `xml.etree.ElementTree`, `urllib.parse`, `cache`.
- **Free Data Sources Aggregated**:
  1. **OpenAlex REST API (`api.openalex.org/works`)**: Indexes millions of free academic papers and open-access PDF URLs (`open_access.oa_url`) — 100,000 req/day free, zero API key needed.
  2. **arXiv API (`export.arxiv.org/api/query`)**: Direct PDF links & abstracts for CS/Math/Physics/Engineering.
  3. **Wikipedia REST API (`en.wikipedia.org/api/rest_v1/page/summary/...`)**: Clean conceptual references.
  4. **DuckDuckGo Lite / HTML Search** *(plus optional user-supplied Brave/Tavily free-tier key)*: General web tutorial search & `filetype:pdf` lecture-note discovery, tagging every `.pdf` hit with `is_direct_pdf = True` and HTML pages with `can_inspect_pdfs = True`.
  5. **YouTube Discovery (`yt-dlp` `ytsearch8:<query>`)**: Returns verified YouTube lecture videos with duration, channel, thumbnail, and caption availability—completely free.

### File A4: `backend/local_app/parsers/doc_parsers.py` (Universal File Parsers)
- **Purpose**: Parses every document type into structured text chunks + diagram images cleanly in local memory.
- **Libraries Used**:
  - **PDF (`.pdf`)**: `pymupdf` (`fitz`) — extracts text per page, detects image/vector-heavy pages, renders key diagram pages at `150 DPI` as JPEG visual slides for Visual Notes, caps runaway files via configurable safety limit (e.g., max 250 pages / 100 MB).
  - **PowerPoint (`.pptx` / `.ppt`)**: `python-pptx` — extracts per-slide headings, bullet text, tables, **speaker notes**, and embedded slide pictures (plus cleanly prompts/invokes local LibreOffice headless conversion if legacy binary `.ppt` is encountered).
  - **Excel (`.xlsx` / `.xls`)**: `openpyxl` (`read_only=True, data_only=True`) — extracts workbook sheet names, headers, summary statistics, and clean Markdown tables (`<= 500` rows per sheet chunk for LLM token efficiency), stripping leading formula characters (`=`, `+`, `-`, `@`) on export to prevent CSV/Excel injection.
  - **CSV (`.csv`)**: Standard `csv.Sniffer` + delimiter detection -> Markdown tables & column schema summary.
  - **TXT / Markdown (`.txt`, `.md`)**: `charset-normalizer` UTF-8 decoder with header/paragraph chunking.

### File A5: `backend/local_app/parsers/web_crawler.py` (Web Link & Website PDF Harvester)
- **Purpose**: Turns any webpage or website directory into clean study text AND discovers downloadable PDFs hosted on that site.
- **Libraries Used**: `httpx`, `trafilatura`, `beautifulsoup4`, `ipaddress`, `socket`.
- **Two-Stage Workflow**:
  1. **Page Inspection (`inspect_url`)**: Validates URL via strict SSRF guard, fetches HTML (`<= 10 MB`, `15s` timeout), extracts main article content via `trafilatura` (removing ads/navbars), and scans `<a href>`, `<embed>`, and `<iframe>` tags resolving relative URLs with `urljoin` to find every `.pdf` document linked on the page.
  2. **Selective Batch PDF Fetch (`download_selected_pdfs`)**: Downloads user-checked PDF links directly (verifying `%PDF-` magic header and `<= 50 MB` limit) and feeds them straight into `doc_parsers.py`.

### File A6: `backend/local_app/parsers/youtube_parser.py` (YouTube Course Extractor)
- **Purpose**: Converts YouTube links into multimodal course sources without requiring paid YouTube Data API quotas.
- **Libraries Used**: `youtube_transcript_api`, `yt-dlp` (`skip_download=True`), `httpx`.
- **Workflow**: Extracts video ID (`v=...`), pulls timestamped transcripts (auto-generated or manual English/user language captions), pulls official **YouTube Chapters**, and downloads high-res thumbnail + key chapter frame previews (if storyboard manifests exist) so Notes include visual timestamps (`[YouTube 14:22]`).

### File A7: `backend/local_app/parsers/video_parser.py` (OpenCV Multi-Window Slicer)
- **Purpose**: High-speed C++ local slicer for multi-hour 4-window tiled lecture videos (up to 20+ GB) without desktop/mobile browser memory exhaustion, replacing standalone `extract_and_sync.py` while keeping an optional `--sync-to-cloud` CLI mode.
- **Libraries Used**: `cv2` (OpenCV), `numpy`.

### File A8: `backend/local_app/local_db.py` (Local SQLite WAL + FTS5 + OS Keychain)
- **Purpose**: Manages `~/.omnilearn/omnilearn_local.db`, initializes `database/local_schema.sql`, stores API keys encrypted with **AES-256-GCM** using a wrapping key kept in OS credential store (`keyring`: Windows DPAPI / macOS Keychain / Linux SecretService), and exposes fast FTS5 full-text queries.

---

## 3. File-by-File Specification: Part B — Cloudflare Python Worker (`backend/src/`)

Hosted on Cloudflare Free Plan (`$0.00/mo`) for **Web Lite preview**, **Installer Download Center**, and optional **Admin Cloud Sync**:

```
wrangler.toml                         # python_workers, ASSETS, D1 (DB), R2 (SLIDES_BUCKET + RELEASES), KV (APP_CACHE)
backend/src/
├── main.py                           # WorkerEntrypoint router + ASSETS fallback + CORS/CSP headers
├── edition_guard.py                  # NEW: Enforces Web Lite limits & serves /api/releases/latest
├── crypto_utils.py                   # Native C++ js.crypto.subtle FFI (<0.5ms CPU): PBKDF2, AES-GCM, JWT
├── db.py                             # Parameterized Cloudflare D1 async helpers
├── auth.py                           # Single-Admin Lock + Zero-Leak Sign-In Key Injection + Web Lite Guest Token
├── api_pool.py                       # Multi-API Key Load Balancer (capped at 1 slot in Web Lite, unlimited in Sync)
├── cache.py                          # Two-Tier Edge Cache (KV L1 + D1 L2)
├── storage.py                        # Cloudflare R2 Slide Upload/Stream & Release Binary links
└── gemini_client.py                  # Multi-Provider AI Client (Gemini 2.0 Flash + Groq Fallback) via js.fetch()
```

### Cloudflare Worker Additions & Web Lite Enforcement (`backend/src/edition_guard.py`)
- Reads `RUNTIME_MODE = "web_lite"` from `[vars]` in `wrangler.toml`:
  - **`GET /api/releases/latest`**: Queries D1 `app_releases` (`WHERE is_latest = 1`) returning version, filename, byte size, SHA-256, and zero-egress R2/GitHub download URLs per OS.
  - **`enforce_web_lite_limits(request, endpoint, payload, env)`**:
    - Allows **small `.txt`/`.csv` (`<= 500 KB`)**, **short browser video keyframes (`<= 8` slides)**, **`<= 3` OpenAlex paper search hits**, and **1 sample public webpage preview**.
    - Blocks heavy operations (`source_type in ['pdf', 'xlsx', 'ppt', 'youtube', 'web_pdf']`, `sources > 2`, `api_keys > 1`, `exports`, `ai_grader`) with **`HTTP 403`**:
      `{ "error": "EDITION_LIMIT_EXCEEDED", "feature": "pdf_xlsx_ppt_ingestion", "message": "Download OmniLearn System App to unlock full multi-source ingestion." }`.
    - Authenticated **Admin Session** (verified via PBKDF2 + `HttpOnly` JWT) bypasses the demo lock on Sync routes (`/api/sync/*`) so your downloaded Desktop/Android app can back up courses cleanly to your free D1/R2 tier!

---

## 4. Defensive Coding, Security & Bottleneck Mitigation Matrix

| Threat / Edge Case | Defense Implemented Across System App & Worker |
|---|---|
| **SSRF on Web Crawler / Direct PDF Download** | `web_crawler.py` allows only `http://` and `https://`; resolves DNS first and strictly blocks private/loopback/link-local IPs (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16` metadata IP, `::1`); blocks non-standard redirects back to private IPs. |
| **Malicious / Oversized Files (PDF/ZIP/XLSX Bombs)** | Enforces strict streaming download size caps (`<= 50 MB` PDFs, `<= 25 MB` XLSX/PPTX, `<= 2.5 MB` slide JPEGs), validates magic bytes (`%PDF-`, `PK` ZIP container, `FF D8 FF` JPEG), caps PDF page rendering (`<= 250` pages), and prevents Decompression Bomb expansion in ZIP-based `.xlsx`/`.pptx`. |
| **API Key Leakage** | AES-256-GCM encryption at rest in D1 and Local SQLite; zero endpoints ever return plaintext, prefix, or suffix characters; OS Keychain (`keyring`) / Cloudflare Secrets protect wrapping keys. |
| **XSS from Scraped Webpages / Unsanitized Docs** | Extracted webpage/PDF/CSV strings are stored as plain text / structured JSON (rendered safely via Lit text interpolation or sanitized HTML entities); unsafe `<script>`/`<iframe>`/inline `on*` handlers are stripped before persistence; Mermaid runs with `securityLevel: 'strict'`. |
| **CSV / Spreadsheet Formula Injection** | When exporting Anki CSV or displaying XLSX cell strings, values starting with `=`, `+`, `-`, `@`, `\t`, `\r` are safely neutralized. |
| **Cloudflare 10 ms CPU & 128 MB RAM Limit** | Web Lite runs zero heavy Python parsing packages (`pymupdf`, `openpyxl`, `cv2`, `yt-dlp` stay strictly inside `backend/local_app/`); Cloudflare Worker uses pure standard library + `js.crypto.subtle` (<0.5 ms CPU) + non-blocking `js.fetch()`. |
| **Free-Tier 15 RPM Rate Limits** | Multi-API Pool distributes requests evenly across multiple Gemini + Groq keys with automatic 65s HTTP 429 cooldowns + SHA-256 response deduplication cache. |

---

## 5. Automated Testing & Debugging Plan (`tests/`)

| Test Suite | Target Modules | What It Verifies |
|---|---|---|
| **Parser Unit Tests (`tests/test_parsers.py`)** | `backend/local_app/parsers/*` | Fixture files (`.pdf`, `.txt`, `.xlsx`, `.pptx`, `.csv`, mock HTML with 3 PDF links, mock YouTube captions) parse cleanly into `NormalizedSourcePayload`; PDF diagram extraction, empty/corrupt file error handling, formula sanitization. |
| **SSRF & Security Tests (`tests/test_security.py`)** | `web_crawler.py`, `auth.py`, `api_pool.py`, `crypto_utils.py` | Blocks `http://127.0.0.1`, `http://169.254.169.254`, `file:///`; verifies PBKDF2 checks before key append; asserts raw API key or substrings **never** appear anywhere in login/pool/course JSON responses. |
| **Edition Gate Tests (`tests/test_edition_guard.py`)** | `backend/src/edition_guard.py` | Verifies `web_lite` enforces HTTP `403 EDITION_LIMIT_EXCEEDED` on locked endpoints (`.pdf`, `.xlsx`, `youtube`, `>2 sources`, `>1 key`), while `system_app` mode unlocks all routes cleanly. |
| **In-App Search Tests (`tests/test_source_search.py`)** | `backend/local_app/source_search.py` | Mocked `httpx` responses for OpenAlex, arXiv, DuckDuckGo, and YouTube ensuring deduplicated URLs, timeout resilience, and SQLite/KV search caching. |
| **Local Worker & Desktop Smoke Test** | `npx wrangler dev --local` & `app_launcher.py` | End-to-end verification of Cloudflare local D1/R2/KV + Playwright/Chrome DevTools responsive Android audit (`360x800` touch targets, `0` console errors, Service Worker isolation). |
