# 🖥️ Frontend Architecture & UI Specification
# OmniLearn: Shared Google Lit + Pure CSS3 UI (Full System App & Cloudflare Web Lite Portal)

---

## 1. Unified Frontend Stack Across System App, Android & Web

OmniLearn uses **one shared frontend codebase (`frontend/`)** built with **Google Lit (Web Components)** and **Pure CSS3** that renders across all three targets:

1. **Desktop Full System Application (`pywebview` Native Window)**: Loads `frontend/` over fast local HTTP inside the OS native WebView (**Edge WebView2 on Windows, WKWebView on macOS, WebKitGTK on Linux**)—0 Electron bloat, native OS drag-and-drop, OS menus, and unlocked full capabilities.
2. **Android Full System Application (Capacitor `.apk` + PWA)**: Loads `frontend/` inside **Android System WebView**, enhanced by `android.css` (`48px` touch targets, `16px` non-zoom inputs, `env(safe-area-inset-bottom)` gesture clearance, native slide-over drawer).
3. **Cloudflare Free-Plan Web Portal (`Web Lite` + Download Hub)**: Served straight from **Cloudflare Workers Static Assets (`./frontend`)** at edge speed, running with `edition = "web_lite"` (interactive limited demo + prominent OS installer downloads).

### 1.1 Google Frontend Ecosystem Decision
| Google Framework | Strengths | Decision for OmniLearn |
|---|---|---|
| **Angular** | Enterprise TypeScript SPA framework. | Heavier build toolchain and bundle footprint inside edge CDN and lightweight `pywebview` wrappers. |
| **Closure Tools** | Legacy Google Search/Gmail JS compiler. | Obsolete for modern greenfield Web Component apps. |
| **Flutter Web** | Dart compiled to `<canvas>` / WASM. | Does **not** use external CSS3 (`use css` rule), multi-MB WASM slows Android loading, breaks native DOM KaTeX math selection and Mermaid SVG crispness. |
| **Google Lit (Web Components)** ✅ | **~5 KB reactive Web Components library** by Google built on browser DOM standards. | **CHOSEN STACK**: Uses Light DOM (`createRenderRoot() { return this; }`) styled completely by **5 modular Pure CSS3 stylesheets**, embeds natively inside WebView2/Android/Cloudflare CDN, and shares 100% of code between Web Lite and the Downloaded System App. |

### 1.2 Pure CSS3 Architecture (`frontend/css/`)
1. `theme.css`: Palette tokens (`#1E3A8A` Deep Navy, `#3B82F6` Electric Blue, `#10B981` Emerald Unlocked, `#8B5CF6` Violet Grader, `#F59E0B` Amber Web-Lite Lock), elevation shadows, typography scales.
2. `layout.css`: Desktop workspace sidebar + fluid main grid, 2-column Sources workbench, sticky top bars.
3. `components.css`: Source cards, search result rows, file dropzones, discovered-PDF checkbox trees, terminal transcript boxes, KaTeX blocks, Mermaid SVGs, quiz pills, and examiner rubric callouts.
4. `android.css`: `@media (max-width: 899px)` mobile-first rules (`>= 48px` buttons/nav tabs, `16px` inputs preventing Android viewport auto-zoom, horizontal swipe pill rows, fixed bottom navigation bar above gesture bar).
5. `system-app.css`: Native OS desktop titlebar padding, edition comparison badges (`WEB LITE` vs `FULL SYSTEM APP`), dashed `🔒 System App Required` overlays, and download modal styling.

---

## 2. File Structure (`frontend/`)

```
frontend/
├── index.html                        # App Shell + Lit 3 ImportMap + KaTeX + Mermaid + PWA links
├── manifest.json                     # Android Installable PWA Manifest
├── sw.js                             # Service Worker caching static CSS/JS shell (never /api/*)
├── icons/icon.svg                    # Adaptive App Vector Icon
├── css/
│   ├── theme.css
│   ├── layout.css
│   ├── components.css
│   ├── android.css
│   └── system-app.css                # Edition badges, locked state overlays & Download modal
└── js/
    ├── app.js                        # Root <omnilearn-app> shell, runtime detector, routing & drawer
    ├── api-client.js                 # Unified fetch wrapper targeting localhost (System App) or /api (Web)
    ├── edition-gate.js               # Capability matrix enforcing Web Lite vs Full System App states
    ├── video-extractor.js            # Browser <video> + <canvas> slicer (used in Web Lite 5m preview)
    ├── export-utils.js               # Markdown, Anki CSV, Printable Model Paper exporters
    └── components/
        ├── edition-banner.js         # <omnilearn-edition-banner> Web Lite notice + OS Download CTA
        ├── download-modal.js         # <omnilearn-download-modal> .exe / .dmg / .AppImage / .apk hub
        ├── login-view.js             # <omnilearn-login> Admin Auth + Zero-Leak API Key Input + Lite Demo
        ├── sources-view.js           # <omnilearn-sources> In-App Search + 9 Source Types + Website PDF Picker
        ├── spatial-view.js           # <omnilearn-spatial> Multi-Window Video Grid Inspector
        ├── notes-view.js             # <omnilearn-notes> Visual Notes + Source Citations + KaTeX + Mermaid
        ├── plan-view.js              # <omnilearn-plan> Spaced-Repetition Study Checklist
        ├── quiz-view.js              # <omnilearn-quiz> Active Recall Quiz + Anki Export
        ├── exam-view.js              # <omnilearn-exam> Model Papers + Multi-API Grader
        └── analytics-view.js         # <omnilearn-analytics> SVG Competency Radar & Readiness
```

---

## 3. Button-by-Button, View-by-View Control Specification

---

### View 0: Edition Header Banner & System App Download Center (`<omnilearn-edition-banner>` & `<omnilearn-download-modal>`)

Shown permanently at the top of the **Cloudflare Web Lite Portal** (automatically hidden inside the downloaded Desktop/Android System App, which displays an emerald `⚡ FULL SYSTEM APP • ONLINE CONNECTED` badge instead, and warns when internet is disconnected):

```
+-----------------------------------------------------------------------------------+
| 🌐 WEB LITE EDITION (Limited Preview) — File Uploads (PDF/XLSX/PPT), In-App Web   |
| PDF Crawler, YouTube & Unlimited Sources require the Downloaded System App.      |
|                           [ ⬇️ Download Full System App (Win / Mac / APK) ]      |
+-----------------------------------------------------------------------------------+
```

1. **Control 1 — `[ ⬇️ Download Full System App ]` Header Button** *(Web Portal Only)*:
   - *Appearance*: Amber-to-Gold gradient pill (`#F59E0B` -> `#D97706`) with pulsing subtle dot in the sticky top header.
   - *Action*: Opens `<omnilearn-download-modal>`, detecting the user's OS (`Windows`, `macOS`, `Linux`, `Android`) via `navigator.userAgentData` / `userAgent` and querying `GET /api/releases/latest`.
2. **Control 2 — Platform Download Cards (`.exe` / `.dmg` / `.AppImage` / `.apk`)**:
   - *Appearance*: 4 clean cards inside the modal showing filename, file size, SHA-256 verification badge, and primary blue `[ Download Installer ]` button pointing directly to Cloudflare R2 ($0 egress) / GitHub Releases.
3. **Control 3 — Edition Comparison Drawer Toggle (`Compare Web Lite vs Full App`)**:
   - *Appearance*: Expandable accordion inside the modal showing exact limits (`2 sources / 3 MCQs / no PDF/PPTX/YT` vs `Unlimited Local SQLite + All 9 Sources`).
4. **Control 4 — `[ 🚀 Continue in Limited Web Lite ]` Dismiss Button**:
   - *Appearance*: Neutral slate outline button closing the modal so visitors can test Web Lite immediately.

---

### View 1: Sign-In, Single-Admin Lock & Zero-Leak Key Gate (`<omnilearn-login>`)

```
+----------------------------------------------------------------------+
|  [Icon]  OmniLearn AI                                                |
|          Multi-Source Exam Mastery Engine                            |
|                                                                      |
|  +----------------------------------------------------------------+  |
|  | 🛡️ Single-Admin Protected Lock: Previously saved API keys are  |  |
|  | encrypted (AES-256-GCM / OS Keychain) and NEVER shown again.   |  |
|  +----------------------------------------------------------------+  |
|                                                                      |
|  Admin Master Password:   [ ••••••••••••••••••••••••••••••• ]        |
|  API Provider:            [ Google Gemini Free Tier         | v ]    |
|  Personal API Key (Opt.): [ Paste key to append (stays hidden)  ]    |
|                                                                      |
|  [                   🔒 Unlock Workspace                          ]  |
|                                                                      |
|  --- Only shown on Web Portal (when runtime === 'web_lite') ---      |
|  [ 🧪 Launch Limited Web Lite Preview (No Admin Account Needed) ]    |
|  [ ⬇️ Download Full System App (.exe / .dmg / .apk)             ]    |
+----------------------------------------------------------------------+
```

5. **Control 5 — `Admin Master Password` & `Confirm Password` Inputs** (`type="password"`, `16px` Android-safe font).
6. **Control 6 — `API Provider` Select & `Add Personal API Key (Optional)` Input** (`autocomplete="new-password"`):
   - *Zero-Leakage Behavior*: Strictly blank; never fetches/reveals saved keys; processed server/engine-side only *after* Admin PBKDF2 password passes; cleared from DOM/JS state right after request returns. In Web Lite mode, helper note states `"Web Lite caps active pool at 1 key; System App unlocks unlimited Multi-API rotation."`
7. **Control 7 — `[ 🔒 Unlock Workspace ]` / `[ 🔑 Initialize Admin & Enter ]` Primary Button**: Authenticates Admin, stores `HttpOnly` JWT cookie / local token, enters workspace.
8. **Control 8 — `[ 🧪 Launch Limited Web Lite Preview ]` Button** *(Web Portal Only)*:
   - *Appearance*: Outlined button below login CTA.
   - *Action*: Opens a restricted read-only/sandbox `Web Lite` session preloaded with the 4-subject sample course plus limited 2-source sandbox allowance—without exposing the Admin's private database or encrypted keys!

---

### View 2: Navigation Shell (`Desktop Sidebar + Android Bottom Nav`)

9. **Control 9 — `Course Library` Dropdown & `[ ➕ New Course ]` Action**: Switches active course or creates a blank multi-source course shell (`POST /api/courses`).
10. **Control 10 — `FTS5 Quick Course Search` Input** *(Full System App)* / **`🔒` Search Disabled Badge** *(Web Lite)*:
    - *Use*: Instantly searches across every ingested PDF page, spreadsheet row, PPT slide, website excerpt, YouTube transcript, and video caption stored in local SQLite `sources_fts`.
11. **Control 11 — `Target Exam Days` & `Daily Study Hours` Sliders**: Dynamically recomputes 14-day study plan pacing.
12. **Control 12 — `Multi-API Pool Pill` (`⚡ N Keys Active` or `🌐 1/1 Slot (Web Lite)`)**: Shows active key count only (never key strings).
13. **Control 13 — `[ 🚪 Lock Workspace ]` Button**: Destroys session cookie/token and returns to Sign-In.
14. **Control 14 — `[ ☰ ]` Mobile Drawer & 7-Tab Navigation Bar**:
    - Tabs (Desktop horizontal / sidebar & Android bottom nav): **`🔍 Sources`** (default on new course), **`🖥️ Windows`**, **`📚 Notes`**, **`📅 Plan`**, **`🧠 Quiz`**, **`📑 Exam`**, **`📊 Stats`**.

---

### View 3: Sources Hub, In-App Search & Website PDF Crawler (`<omnilearn-sources>`)

Replaces single-purpose video upload with a complete **Sources & Research Workbench**:

```
+----------------------------------------------------------------------------------+
| 📂 COURSE SOURCES WORKBENCH                              [ 🔒 / ⚡ Edition Pill ] |
+----------------------------------------------------------------------------------+
| [1. Search Web/Papers/YT]  [2. Upload PDF/XLSX/PPT/CSV/TXT/MP4]  [3. URL / YT]  |
+----------------------------------------------------------------------------------+
|                                                                                  |
| 🔍 Search about source: [ "ARIES database crash recovery"     ] [Search In-App]  |
| Filters: (• All)  (○ Web Docs)  (○ Direct PDFs)  (○ Academic)  (○ YouTube)       |
|                                                                                  |
| +------------------------------------------------------------------------------+ |
| | 📄 Lecture 14: ARIES Recovery & Logging (cs.berkeley.edu/.../aries.pdf)      | |
| |    [Direct PDF • 28 Pages]  Snippet: "Repeating history, CLR records..."    | |
| |                                                 [ ➕ Add PDF Directly ]       | |
| +------------------------------------------------------------------------------+ |
| | 🌐 Database Systems Course Page (stanford-db.edu/lectures)                   | |
| |    [Webpage • Contains 4 Linked PDFs!]                                       | |
| |    [ 🔍 Find PDFs on This Page ]                [ ➕ Add Webpage Text ]       | |
| +------------------------------------------------------------------------------+ |
| | ▶️ CMU Database Group: Logging Schemes (youtube.com/watch?v=...)             | |
| |    [YouTube • 54m • Captions Ready]             [ ➕ Add YouTube Lecture ]    | |
| +------------------------------------------------------------------------------+ |
|                                                                                  |
| --- STAGED COURSE SOURCES (3 / Unlimited) ---                                    |
| • [🎬 MP4] MultiWindow_Course.mp4 (2x2 Grid, 4 Windows, 48px Taskbar Crop)  [🗑️] |
| • [📕 PDF] aries.pdf (28 pages, 9 diagrams extracted)                       [🗑️] |
| • [📊 XLSX] ER_Grading_Weights.xlsx (2 sheets)                              [🗑️] |
|                                                                                  |
|               [ 🚀 Synthesize Unified Course from All Sources ]                  |
+----------------------------------------------------------------------------------+
```

#### Sub-Panel A — In-App Source Search & Direct Doc Picker
15. **Control 15 — `Search Sources` Input Box**: Accepts keywords (`"selection sort"`, `"shell case esac"`), `filetype:pdf` hints, paper titles, or course topics.
16. **Control 16 — Source Type Filter Chips** (`All`, `Web Articles`, `Direct PDFs`, `Academic Papers`, `YouTube Videos`).
17. **Control 17 — `[ 🔍 Search In-App ]` Button**:
    - *System App*: Queries `POST /api/sources/search` across OpenAlex, arXiv, Wikipedia, DuckDuckGo Lite, and YouTube Search.
    - *Web Lite*: Limited to **3 OpenAlex paper abstract results**; `Direct PDFs`, `Web Articles`, and `YouTube` show `🔒 Full App Required`.
18. **Control 18 — Result Card `[ 🔍 Inspect Page for PDFs ]` Button** *(System App only; `🔒` in Web Lite)*:
    - *Use*: Crawls the chosen search result URL right inside the Sources tab (zero external browser hop!) and expands an inline drawer listing all downloadable `.pdf` files linked on that website with checkboxes (`[x] Notes_Ch4.pdf`).
19. **Control 19 — Result Card `[ ➕ Add Source Directly ]` Button**: Immediately fetches, parses, stores diagrams/text into SQLite `course_sources`, and attaches it to the active course!

#### Sub-Panel B — Local Document, Spreadsheet, Deck & Video Adder
20. **Control 20 — Multi-Format Dropzone & Picker** (`accept=".pdf,.txt,.md,.xlsx,.xls,.ppt,.pptx,.csv,.mp4,.mkv,.webm"`):
    - *Appearance*: Tactile dashed card displaying supported type badges (`PDF`, `TXT`, `XLSX`, `PPT/PPTX`, `CSV`, `MP4/Video`).
    - *Web Lite Rule*: Accepts only **`.txt`/`.csv` <= 500 KB** or short **`.mp4` <= 5 min**. Selecting `.pdf`, `.xlsx`, `.ppt`, `.pptx` in Web Lite triggers `<omnilearn-download-modal>` with clear notice: *"Native PDF, XLSX, PPT/PPTX, and OpenCV multi-hour video parsing requires the OmniLearn System App (internet required for AI)."*
21. **Control 21 — Conditional Multi-Window Video Controls** *(Revealed automatically when a video file is picked)*:
    - Includes `Target Subject` input (**all 4 quadrants in a `2x2` video are treated as 4 distinct lectures of this 1 subject**), `Multi-Window Layout` dropdown (`2x2 Grid`, `1x2`, `2x1`, `1x1`), and `Bottom Taskbar Crop Offset` slider (`0–100px`). In System App runs C++ OpenCV; in Web Lite slices max 8 keyframes in `<canvas>`.

#### Sub-Panel C — Paste Web URL, Direct Website PDF or YouTube Link
22. **Control 22 — `Paste Web Link / Website PDF / YouTube URL` Input** (`type="url"`): Auto-detects `youtube.com`/`youtu.be`, `.pdf` URLs, or general `http(s)://` websites.
23. **Control 23 — `[ 🔗 Fetch & Discover Linked PDFs ]` Button**:
    - On a webpage URL *(System App)*: Extracts readable article text AND displays every discovered `.pdf` link found on that page with multi-select checkboxes + **`[ 📥 Add Selected Website PDFs ]`** button.
    - On a `.pdf` URL *(System App)*: Downloads PDF directly via `POST /api/sources/ingest-url`, parses pages, renders diagram pages to images, adds to Course Queue.
    - On a YouTube URL *(System App)*: Fetches title, chapters, timed transcript, and keyframe thumbnails into Course Queue (`🔒 System App Required` in Web Lite).

#### Sub-Panel D — Unified Queue & Multi-API Pool Strip
24. **Control 24 — Source Queue Cards & `[ 🗑️ Remove ]` Buttons**: Lists each ingested source with badge (`PDF • 28p`, `XLSX • 3 sheets`, `PPTX • 32 slides`, `YT • 54m`, `WEB_PDF`), extracted preview drawer toggle, and delete action.
25. **Control 25 — `[ 🚀 Synthesize Unified Course ]` Primary CTA Button**: Sends staged source IDs to `/api/courses/synthesize`, invoking the Multi-API Pool across combined notes/slides/spreadsheets/transcripts and animating stage progress.
26. **Control 26 — Multi-API Pool Panel (`Add Key` & `[ Remove ]` Slots)**: Manages anonymous key slots (`Slot #1 • GEMINI • ACTIVE`). Web Lite caps at **1 active slot** (`Slot #2+` shows `🔒 System App Required`).

---

### Views 4–9: Learning, Exam Practice & Analytics Tabs

27. **View 4 (`<omnilearn-spatial>`) — Controls 27–28 (Multi-Window Grid Inspector)**: Quadrant cards displaying synced keyframes & dark monospace transcript boxes. (Displayed when course contains video sources; neatly summarizes visual extracted slides from PDFs/PPTs too).
28. **View 5 (`<omnilearn-notes>`) — Controls 29–32 (Visual Notes, KaTeX & Mermaid)**:
    - Horizontal subject pills (`Ctrl 29`), **`[ 📥 Download Notes (.MD) ]`** (`Ctrl 30` — *locked in Web Lite*), Visual Asset Carousel combining video frames + rendered PDF diagram pages + PPTX visuals + YouTube keyframes with **Source Citation Badges** (`[From: aries.pdf p.12]`, `[From: CMU YouTube 18:40]`) (`Ctrl 31`), and KaTeX + Mermaid Canvas (`Ctrl 32` — capped at 2 concepts in Web Lite).
29. **View 6 (`<omnilearn-plan>`) — Controls 33–34 (Study Plan)**: Subject switcher pills (`Ctrl 33`) & interactive `48px` daily checklist rows synced to SQLite/D1 (`Ctrl 34`).
30. **View 7 (`<omnilearn-quiz>`) — Controls 35–37 (Active Recall Quiz & Anki)**: **`[ 📥 Export Anki (.CSV) ]`** (`Ctrl 35` — *locked in Web Lite*), `46px+` MCQ cards (`Ctrl 36`), and **`[ Submit Answer ]`** (`Ctrl 37` — capped at 3 sample questions in Web Lite vs full deck in System App).
31. **View 8 (`<omnilearn-exam>`) — Controls 38–41 (Model Papers & Multi-API Grader)**: **`[ 📥 Download Paper (.TXT) ]`** (`Ctrl 38` — *locked in Web Lite*), **`Practice Answer` Textarea** (`Ctrl 39`), **`[ 🤖 Evaluate & Grade Answer ]`** (`Ctrl 40` — *locked in Web Lite, unlocked in System App*), and **`[ 🔍 Reveal Model Solution & Rubric ]`** (`Ctrl 41` — 1 short Q in Web Lite vs full 3-section paper in System App).
32. **View 9 (`<omnilearn-analytics>`) — Controls 42–44 (Analytics)**: Top 4 Metric Cards (`Ctrl 42`), SVG Competency Radar (`Ctrl 43`), and **`[ ⚡ Jump to Weakest Subject Quiz ]`** button (`Ctrl 44`).
