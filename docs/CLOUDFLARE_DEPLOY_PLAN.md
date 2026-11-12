# ☁️ Deployment, Packaging & Hosting Plan ($0.00/Month)
# OmniLearn: Full System App Packaging + Cloudflare Free Plan Web Lite Portal

---

## 1. Free-Tier Architecture Split

| Deliverable | Hosting / Distribution Channel | Monthly Cost | Role |
|---|---|---|---|
| **Web Lite Portal & Sync API** | **Cloudflare Workers (`python_workers`) + Static Assets + D1 + R2 + KV** | **$0.00 / mo** | Serves `Web Lite` limited interactive preview, Admin encrypted sync, and the OS Installer Download Hub. |
| **Desktop Full System App** | **GitHub Releases + Cloudflare R2 (`omnilearn-releases`, $0 Egress)** | **$0.00 / mo** | Standalone native app (`pywebview` + Python 3.13 + FastAPI + SQLite WAL/FTS5) in `.exe`, `.dmg`, `.AppImage`. |
| **Android Full System App** | **GitHub Releases / R2 + PWA** | **$0.00 / mo** | Capacitor native Android `.apk` wrapping `frontend/` Google Lit UI + local Android WebView SQLite/PWA. |

---

## 2. Cloudflare Free Plan Resource Provisioning (`Web Lite` + `Releases`)

| Resource | Free Allowance | OmniLearn Binding |
|---|---|---|
| **Static Assets (`./frontend`)** | Unlimited bandwidth, 20,000 files | `ASSETS` (`run_worker_first = ["/api/*"]`) |
| **Python Worker (`backend/src/main.py`)** | 100,000 req/day, 10 ms CPU/req, 128 MB RAM | `python_workers` + `[vars] RUNTIME_MODE = "web_lite"` |
| **Cloudflare D1 (`omnilearn-db`)** | 5 GB storage, 5M row reads/day, 100k writes/day | `DB` (14 tables + `app_releases` manifest) |
| **Cloudflare R2 (`omnilearn-slides` & `omnilearn-releases`)** | 10 GB storage/mo, **$0.00 Egress Fees** | `SLIDES_BUCKET` + `RELEASES_BUCKET` |
| **Workers KV (`APP_CACHE`)** | 1 GB storage, 100k reads/day, 1k writes/day | `APP_CACHE` |

### Provisioning Sequence (When Ready to Build)
```powershell
# 1. Authenticate with free Cloudflare account
npx wrangler login

# 2. Provision D1, R2 buckets (slides + installers), and KV cache
npx wrangler d1 create omnilearn-db
npx wrangler r2 bucket create omnilearn-slides
npx wrangler r2 bucket create omnilearn-releases
npx wrangler kv namespace create APP_CACHE

# 3. Initialize D1 Remote & Local SQLite schemas
npx wrangler d1 execute omnilearn-db --remote --file=./database/schema.sql
npx wrangler d1 execute omnilearn-db --remote --file=./database/seed.sql
npx wrangler d1 execute omnilearn-db --local --file=./database/schema.sql

# 4. Set Server Master Encryption Secret & Optional Default Multi-API Pool Keys
npx wrangler secret put MASTER_SECRET
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put GROQ_API_KEY

# 5. Test Web Lite Locally & Deploy Globally
npx wrangler dev
npx wrangler deploy
```

---

## 3. Native System App Packaging Plan (`backend/local_app/` & `mobile_apk/`)

Once implemented and verified locally, the Full System Application is packaged cleanly without shipping any Chromium bloat:

1. **Windows (`.exe`)**:
   - Uses native **Edge WebView2** runtime built into Windows 10/11 via `pywebview`.
   - Packaged via `PyInstaller --noconfirm --windowed --add-data "frontend;frontend" --add-data "database;database" backend/local_app/app_launcher.py` and wrapped into a sleek single-file installer `OmniLearn-Setup-<version>.exe` via **Inno Setup**.
   - Associates `.pdf`, `.xlsx`, `.pptx`, `.csv`, `.txt`, and `.mp4` right-click `"Open with OmniLearn"` handlers optionally.
2. **macOS (`.dmg`)**:
   - Uses native **WKWebView** via `pywebview` built into `.app` bundle via `PyInstaller --windowed` and packaged into a universal `.dmg` disk image.
3. **Linux (`.AppImage`)**:
   - Uses `WebKitGTK` + PyInstaller packed cleanly into `OmniLearn-<version>-x86_64.AppImage` + desktop launcher entry.
4. **Android (`.apk`)**:
   - Wraps the exact same `frontend/` directory (`Google Lit` + `android.css`) using **Capacitor (`npx cap sync android && ./gradlew assembleRelease`)**, bundling native file picking (`PDF`/`TXT`/`CSV`/media), `@capacitor-community/sqlite` local database, and optional encrypted sync with desktop/Cloudflare.
5. **Zero-Egress Installer Publishing**:
   ```powershell
   # Upload installers to R2 (0 egress cost on free plan) & update D1 app_releases table
   npx wrangler r2 object put omnilearn-releases/v1.0.0/OmniLearn-Setup-1.0.0.exe --file=./dist/OmniLearn-Setup-1.0.0.exe
   ```

---

## 4. Automated CI/CD Pipeline Plan (GitHub Actions — Free Tier)

Planned dual workflow `.github/workflows/`:

1. **`.github/workflows/deploy.yml` (Triggered on `main` push — Cloudflare Web Lite & Tests)**:
   - **Job 1 (`test-and-verify`)**: Executes `pytest` (parser tests, SSRF guards, zero-leakage assertions, Web Lite `403` edition gates) and checks SQLite/D1 schema integrity.
   - **Job 2 (`cloudflare-deploy`)**: Runs `wrangler d1 execute --remote` migrations and `wrangler deploy` via `cloudflare/wrangler-action@v3` so the Web Lite Portal and Download Center stay continuously deployed.
2. **`.github/workflows/release-builds.yml` (Triggered on `v*.*.*` git tag — Native Installers)**:
   - Matrix build across `windows-latest` (`.exe`), `macos-latest` (`.dmg`), `ubuntu-latest` (`.AppImage`), plus Android Java/Gradle `.apk`.
   - Generates `SHA-256` checksums, publishes installers directly to **GitHub Releases** and mirrors them to **Cloudflare R2 (`omnilearn-releases`)**, then updates `app_releases` in D1 automatically so the Web Portal immediately offers the newest version.
