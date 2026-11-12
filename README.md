# 🎓 OmniLearn AI — Multi-Source Exam Mastery Engine

> **High-Performance Multi-Source Learning Engine:** Desktop System Application (Python 3.13 + FastAPI + SQLite WAL & FTS5) & \$0/month Cloudflare Free-Plan Web Lite Edge Portal.

[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Cloudflare Workers](https://img.shields.io/badge/Cloudflare-Workers%20%7C%20D1%20%7C%20R2%20%7C%20KV-orange.svg)](https://workers.cloudflare.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-69%20Passing-brightgreen.svg)]()

---

## 🌟 Overview

OmniLearn AI synthesizes study notes, dynamic Mermaid architecture diagrams, formulas, quizzes, and university-style exam papers from multiple diverse source formats simultaneously.

Built with a **Zero-Cost, Dual-Engine Architecture**:
1. **Desktop System Application (`backend/local_app/`)**: Full-capability offline-first local app with native OS windowing (`pywebview`), unlimited document processing, OpenCV 4-quadrant video slicing, and local SQLite 3 with **WAL** concurrency and **FTS5 Full-Text Search**.
2. **Cloudflare Free-Plan Web Edge Portal (`backend/src/`)**: Global lightweight web portal hosted at **\$0.00/month** on Cloudflare Workers (`python_workers`), D1 Database, R2 Object Storage, and Workers KV.

---

## 🚀 Key Features

### 1. 📂 9 Unified Source Ingestion Formats
- **Documents & Data:** Native PDF (`PyMuPDF`), TXT/Markdown, Excel spreadsheets (`openpyxl`), PowerPoint presentations (`python-pptx`), CSV (formula-injection protected).
- **Web & Academic Research:** Trafilatura web article extractor, in-page PDF harvester, and integrated OpenAlex research search.
- **Video & Spatial Quadrants:** YouTube transcripts & chapter extraction (`yt-dlp`), plus local OpenCV slicing for 4-quadrant multi-window lecture videos into a single unified subject.

### 2. 🔑 Zero-Leakage Multi-API Key Pool
- Load balances across **Google Gemini 2.0 Flash**, **Groq (Llama 3.3 70B)**, and **OpenRouter Free Tier**.
- **Least-Recently-Used (LRU)** rotation with automatic **65-second HTTP 429 rate-limit cooldowns**.
- **Cryptographic Zero-Leakage:** All API keys are encrypted at rest with **AES-256-GCM** (local OS Keychain wrapping or Cloudflare Secrets) and deduplicated via SHA-256 hashes. Plaintext keys or prefixes/suffixes are **never** returned in API responses or frontend DOM.
- **Personal Key Override:** Signed-in users can paste their own personal API key in Sign-In or Settings, which automatically overrides system `.env` defaults for their session.

### 3. 🌐 $0/Month Cloudflare AI Relay
- Local desktop users without private API keys can relay prompts securely through the hosted Cloudflare Worker (`POST /api/ai/relay`), utilizing Worker secret defaults without exposing keys to clients.

### 4. 🔒 Authentication & Normal User Login
- **Single-Admin Mode:** PBKDF2-SHA256 (100,000 iterations) with cryptographic salts for local system control.
- **Google OAuth 2.0 / Gmail Login:** Normal users sign in directly with their Gmail accounts via OpenID Connect (`users` table).
- **Web Lite Guest Preview:** Instant sandbox exploration without requiring credentials.

### 5. 📱 Responsive Google Lit Frontend
- High-performance, lightweight UI built with Google Lit Web Components and pure modern CSS.
- Works seamlessly on Desktop (Windows, macOS, Linux) and mobile Android browsers.

---

## 🏗️ Architecture & Directory Structure

```
.
├── backend/
│   ├── local_app/              # Native Desktop System Application (FastAPI + SQLite WAL/FTS5)
│   │   ├── app_launcher.py     # pywebview native OS window & FastAPI daemon runner
│   │   ├── env_config.py       # Zero-leakage .env configuration loader
│   │   ├── gemini_local.py     # AI synthesis engine & Cloudflare relay fallback
│   │   ├── google_auth.py      # Google OAuth 2.0 & OpenID Connect verifier
│   │   ├── local_api.py        # Local REST API endpoints
│   │   ├── local_db.py         # SQLite 3 repository (WAL + FTS5 + AES-256-GCM)
│   │   ├── parsers/            # PDF, XLSX, PPT, CSV, Web & Video parsers
│   │   └── sync_client.py      # Optional encrypted push/pull sync to Cloudflare D1
│   └── src/                    # Cloudflare Free-Plan Edge Worker (python_workers)
│       ├── main.py             # Pure async router for Cloudflare Workers
│       ├── auth.py             # PBKDF2 authentication & Google OAuth handlers
│       ├── api_pool.py         # Encrypted D1 Multi-API key pool manager
│       ├── edition_guard.py    # Free-tier sandbox guards & release hub
│       └── gemini_client.py    # Edge AI streaming & OpenAlex client
├── database/
│   ├── schema.sql              # Unified Cloudflare D1 & SQLite base DDL
│   ├── local_schema.sql        # Local SQLite WAL + FTS5 full-text search triggers
│   └── seed.sql                # Starter sample course dataset
├── frontend/                   # Shared Google Lit + CSS3 frontend assets
│   ├── index.html              # Single Page Application entry point
│   ├── js/                     # Lit Web Components & authenticated API client
│   └── css/                    # Pure responsive CSS design system
├── tests/                      # Full test suite (69 passing tests)
├── wrangler.toml               # Cloudflare Workers, D1, R2, and KV deployment config
└── pyproject.toml              # Packaging configuration & dependency specifications
```

---

## 🛠️ Quick Start

### Prerequisites
- Python 3.11, 3.12, or 3.13
- Git

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/Hardik1036/OmniLearn-AI.git
cd OmniLearn-AI

python -m venv .venv
# Windows:
.\.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment (`.env`)

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Configure your credentials in `.env`:
```dotenv
# Admin Master Login
OMNILEARN_ADMIN_USERNAME=admin
OMNILEARN_ADMIN_PASSWORD=your-secure-password

# AI Provider API Keys (Add any combination)
GEMINI_API_KEY=your_gemini_api_key
GROQ_API_KEY=your_groq_api_key

# Optional: Google OAuth for Gmail Sign-In
GOOGLE_CLIENT_ID=your_google_client_id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your_google_client_secret

# Optional: Cloudflare Worker AI Relay for desktop fallback
OMNILEARN_CLOUD_URL=https://your-worker.workers.dev
```

### 3. Launch the Desktop System App

```bash
python -m backend.local_app.app_launcher
```

Opens a native desktop application window with full PDF rendering, OpenCV video processing, and local SQLite search.

---

## ☁️ Cloudflare Free-Tier Deployment ($0.00 / month)

1. **Login with Wrangler:**
   ```bash
   npx wrangler login
   ```

2. **Create D1 Database, KV Namespace & R2 Buckets:**
   ```bash
   npx wrangler d1 create omnilearn-db
   npx wrangler kv namespace create APP_CACHE
   npx wrangler r2 bucket create omnilearn-slides
   npx wrangler r2 bucket create omnilearn-releases
   ```

3. **Update `wrangler.toml`** with the generated IDs and initialize the D1 schema:
   ```bash
   npx wrangler d1 execute omnilearn-db --file=database/schema.sql --remote
   ```

4. **Set Worker Secrets (Keys stay hidden in Cloudflare):**
   ```bash
   npx wrangler secret put MASTER_SECRET
   npx wrangler secret put GEMINI_API_KEY
   npx wrangler secret put GOOGLE_CLIENT_ID
   npx wrangler secret put GOOGLE_CLIENT_SECRET
   ```

5. **Deploy:**
   ```bash
   npx wrangler deploy
   ```

---

## 🧪 Testing & Verification

OmniLearn includes an automated test suite verifying route parity, security (zero key leakage, formula injection guards, path traversal blocks, SSRF protections), parsers, and Cloudflare Worker compatibility:

```bash
pytest -v
```

All 69 integration and unit tests execute in under 10 seconds.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
