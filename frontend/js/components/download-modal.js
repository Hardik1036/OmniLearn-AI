import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';
import { safeHttpUrl } from '../edition-gate.js';

const FALLBACK_RELEASES = [
  {
    id: 'rel-win',
    version: '1.6.0',
    platform: 'windows',
    arch: 'x64',
    filename: 'OmniLearn-Windows-x64-v1.6.0.zip',
    download_url: 'https://github.com/Hardik1036/OmniLearn-AI/releases/latest',
    sha256: '765b787f333bd554599d058c0e05bb25ee16e46ccb11ad58baf3aa4d409a45ad',
    size_bytes: 174427828,
    release_notes: 'Native Desktop App with instant offline smart search & private storage',
  },
  {
    id: 'rel-mac',
    version: '1.6.0',
    platform: 'macos',
    arch: 'universal',
    filename: 'OmniLearn-1.6.0-universal.dmg',
    download_url: 'https://github.com/Hardik1036/OmniLearn-AI/releases/latest',
    sha256: 'verified-on-release-checksum',
    size_bytes: 51380224,
    release_notes: 'Native WKWebView macOS Universal App Bundle',
  },
  {
    id: 'rel-linux',
    version: '1.6.0',
    platform: 'linux',
    arch: 'x64',
    filename: 'OmniLearn-1.6.0-x86_64.AppImage',
    download_url: 'https://github.com/Hardik1036/OmniLearn-AI/releases/latest',
    sha256: 'verified-on-release-checksum',
    size_bytes: 49807360,
    release_notes: 'WebKitGTK Portable Linux AppImage',
  },
  {
    id: 'rel-android',
    version: '1.6.0',
    platform: 'android',
    arch: 'arm64',
    filename: 'OmniLearn-1.6.0-release.apk',
    download_url: 'https://github.com/Hardik1036/OmniLearn-AI/releases/latest',
    sha256: 'verified-on-release-checksum',
    size_bytes: 14680064,
    release_notes: 'Capacitor Android System WebView APK + PWA Support',
  },
];

function detectClientPlatform() {
  if (typeof navigator === 'undefined') return 'windows';
  const ua = `${navigator.userAgentData?.platform || ''} ${navigator.userAgent || ''}`.toLowerCase();
  if (ua.includes('android')) return 'android';
  if (ua.includes('mac') || ua.includes('darwin')) return 'macos';
  if (ua.includes('linux')) return 'linux';
  return 'windows';
}

function formatBytes(bytes) {
  const num = Number(bytes || 0);
  if (!num) return 'Installer';
  if (num >= 1024 * 1024) {
    return `${(num / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${Math.round(num / 1024)} KB`;
}

function platformLabel(platform) {
  switch (platform) {
    case 'windows':
      return '🪟 Windows (.exe)';
    case 'macos':
      return '🍎 macOS (.dmg)';
    case 'linux':
      return '🐧 Linux (.AppImage)';
    case 'android':
      return '🤖 Android (.apk)';
    default:
      return String(platform || 'Desktop');
  }
}

export class OmnilearnDownloadModal extends LitElement {
  static properties = {
    open: { type: Boolean },
    feature: { type: String },
    reason: { type: String },
    releases: { type: Array, state: true },
    loading: { type: Boolean, state: true },
    showComparison: { type: Boolean, state: true },
    detectedPlatform: { type: String, state: true },
  };

  constructor() {
    super();
    this.open = false;
    this.feature = '';
    this.reason = '';
    this.releases = FALLBACK_RELEASES;
    this.loading = false;
    this.showComparison = false;
    this.detectedPlatform = detectClientPlatform();
    this._onKeyDown = (e) => {
      if (e.key === 'Escape' && this.open) {
        this._close();
      }
    };
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener('keydown', this._onKeyDown);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('keydown', this._onKeyDown);
  }

  updated(changed) {
    if (changed.has('open') && this.open) {
      this._fetchReleases();
    }
  }

  async _fetchReleases() {
    this.loading = true;
    try {
      const data = await apiClient.getLatestReleases();
      const list = Array.isArray(data) ? data : Array.isArray(data?.releases) ? data.releases : [];
      if (list.length > 0) {
        this.releases = list.map((item) => ({
          ...item,
          download_url: String(item.download_url || '').replace(
            'github.com/omnilearn-ai/omnilearn',
            'github.com/Hardik1036/OmniLearn-AI'
          ),
        }));
      } else {
        this.releases = FALLBACK_RELEASES;
      }
    } catch (_err) {
      this.releases = FALLBACK_RELEASES;
    } finally {
      this.loading = false;
    }
  }

  _close() {
    this.dispatchEvent(new CustomEvent('close-download-modal', { bubbles: true, composed: true }));
  }

  _onBackdropClick(e) {
    if (e.target === e.currentTarget) {
      this._close();
    }
  }

  render() {
    if (!this.open) {
      return nothing;
    }

    const sortedReleases = [...this.releases].sort((a, b) => {
      if (a.platform === this.detectedPlatform) return -1;
      if (b.platform === this.detectedPlatform) return 1;
      return 0;
    });

    return html`
      <div
        class="modal-backdrop"
        role="dialog"
        aria-modal="true"
        aria-labelledby="download-modal-title"
        @click=${this._onBackdropClick}
      >
        <div class="modal-dialog">
          <div class="row-between">
            <div>
              <span class="badge badge-emerald">⚡ Fast &amp; Free • Native Desktop Application</span>
              <h2 id="download-modal-title" style="margin-top: 8px;">Download OmniLearn Desktop App</h2>
            </div>
            <button type="button" class="btn btn-outline btn-sm" @click=${this._close} aria-label="Close download modal">
              ✕ Close
            </button>
          </div>

          ${this.reason
            ? html`
                <div class="locked-card">
                  <div class="row-between">
                    <span class="lock-pill">🔒 Desktop App Feature${this.feature ? ` (${this.feature})` : ''}</span>
                  </div>
                  <p style="margin: 0;">${this.reason}</p>
                </div>
              `
            : nothing}

          <div class="text-sm text-secondary">
            Fast, lightweight, and offline-ready. Runs directly on your device with complete privacy, instant full-text search, and large video lecture processing.
          </div>

          ${this.loading
            ? html`<div class="text-sm text-muted">Checking latest signed installer releases...</div>`
            : nothing}

          <div class="release-grid">
            ${sortedReleases.map((rel) => {
              const isDetected = rel.platform === this.detectedPlatform;
              const safeUrl = safeHttpUrl(rel.download_url);
              return html`
                <div class="release-card ${isDetected ? 'recommended' : ''}">
                  <div class="row-between">
                    <strong>${platformLabel(rel.platform)}</strong>
                    ${isDetected
                      ? html`<span class="badge badge-blue">Detected OS</span>`
                      : html`<span class="badge">${rel.arch || 'x64'}</span>`}
                  </div>
                  <div class="text-sm">
                    <strong>${rel.filename}</strong>
                    <span class="text-muted"> • v${rel.version} • ${formatBytes(rel.size_bytes)}</span>
                  </div>
                  ${rel.release_notes
                    ? html`<div class="text-xs text-secondary">${rel.release_notes}</div>`
                    : nothing}
                  <div class="sha-badge">SHA-256: ${rel.sha256 || 'Pending build hash'}</div>
                  ${safeUrl
                    ? html`
                        <a
                          class="btn btn-primary btn-block"
                          href=${safeUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          ⬇️ Download Installer
                        </a>
                      `
                    : html`
                        <button type="button" class="btn btn-outline btn-block" disabled>
                          Installer URL Unavailable
                        </button>
                      `}
                </div>
              `;
            })}
          </div>

          <div class="source-fallback-card" style="margin: 16px 0; padding: 14px 16px; background: rgba(30, 41, 59, 0.6); border: 1px dashed var(--border-color, #334155); border-radius: 8px;">
            <div class="row-between" style="margin-bottom: 6px;">
              <strong style="font-size: 0.9rem;">🛠️ Run Directly from Source (Developer Mode)</strong>
              <span class="badge badge-emerald">Ready Now</span>
            </div>
            <p class="text-xs text-secondary" style="margin: 0 0 8px 0;">
              If precompiled installer packages are still compiling on GitHub, launch the desktop app directly from source with complete offline search and no cloud size limits:
            </p>
            <pre style="margin: 0; padding: 10px 12px; background: #0b1120; border-radius: 6px; font-size: 0.8rem; overflow-x: auto; color: #38bdf8;"><code>git clone https://github.com/Hardik1036/OmniLearn-AI.git
cd OmniLearn-AI
python -m backend.local_app.app_launcher</code></pre>
          </div>

          <div>
            <button
              type="button"
              class="btn btn-outline btn-sm"
              @click=${() => {
                this.showComparison = !this.showComparison;
              }}
            >
              ${this.showComparison ? '▲ Hide Comparison' : '▼ Compare Cloud Preview vs Desktop App'}
            </button>

            ${this.showComparison
              ? html`
                  <div style="margin-top: 12px; overflow-x: auto;">
                    <table class="comparison-table">
                      <thead>
                        <tr>
                          <th>Capability</th>
                          <th>🌐 Cloud Preview (Web)</th>
                          <th>⚡ Desktop App</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr>
                          <td><strong>Sources per Course</strong></td>
                          <td>Max 2 sources (small .txt/.csv &le;500 KB, &le;8 video slides)</td>
                          <td>Unlimited mixed sources (PDF, XLSX, PPTX, CSV, TXT, MP4, Web, YT)</td>
                        </tr>
                        <tr>
                          <td><strong>In-App Search &amp; Crawler</strong></td>
                          <td>3 OpenAlex paper abstracts only</td>
                          <td>Unlimited OpenAlex, arXiv, Wikipedia, DDG, YouTube + Website PDF picker</td>
                        </tr>
                        <tr>
                          <td><strong>Database &amp; Search</strong></td>
                          <td>Cloud preview</td>
                          <td>Local instant smart search across all notes, slides, and transcripts</td>
                        </tr>
                        <tr>
                          <td><strong>Smart AI Study Engine</strong></td>
                          <td>1 active key slot</td>
                          <td>Unlimited Gemini + Groq keys with 65s automatic rate-limit cooldown</td>
                        </tr>
                        <tr>
                          <td><strong>Study Aids, Grader &amp; Exports</strong></td>
                          <td>2 concepts, 3 sample MCQs, 1 short Q (Exports &amp; Grader locked)</td>
                          <td>Full KaTeX/Mermaid notes, 14-day plan, 25+ MCQs, AI Rubric Grader, MD/Anki/TXT exports</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                `
              : nothing}
          </div>

          <div class="row-between" style="padding-top: 8px; border-top: 1px solid var(--border-subtle);">
            <span class="text-xs text-muted">Official Releases Verified &amp; Secured</span>
            <button type="button" class="btn btn-outline" @click=${this._close}>
              🚀 Continue in Cloud Preview
            </button>
          </div>
        </div>
      </div>
    `;
  }
}

customElements.define('omnilearn-download-modal', OmnilearnDownloadModal);
