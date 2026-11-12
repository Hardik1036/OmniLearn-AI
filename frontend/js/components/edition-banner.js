import { LitElement, html, nothing } from 'lit';
import { isWebLite, promptSystemAppDownload } from '../edition-gate.js';

export class OmnilearnEditionBanner extends LitElement {
  static properties = {
    edition: { type: String },
  };

  constructor() {
    super();
    this.edition = 'web_lite';
  }

  createRenderRoot() {
    return this;
  }

  _openDownloadModal() {
    promptSystemAppDownload('', 'Unlock local SQLite FTS5, PDF/XLSX/PPTX parsers, Website PDF crawling, YouTube, and unlimited Multi-API rotation.');
  }

  render() {
    if (!isWebLite(this.edition)) {
      return nothing;
    }

    return html`
      <div class="edition-banner" role="region" aria-label="Web Lite Edition Notice">
        <div class="edition-banner-text">
          <span class="badge badge-amber">🌐 WEB LITE EDITION (Limited Preview)</span>
          <span>
            Local PDF/XLSX/PPTX parsing, In-App Web PDF Crawler, YouTube ingestion &amp; unlimited sources require the Downloaded System App.
          </span>
        </div>
        <button
          type="button"
          class="btn btn-amber btn-sm"
          @click=${this._openDownloadModal}
        >
          ⬇️ Download Full System App (Win / Mac / APK)
        </button>
      </div>
    `;
  }
}

customElements.define('omnilearn-edition-banner', OmnilearnEditionBanner);
