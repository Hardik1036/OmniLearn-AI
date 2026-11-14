import { LitElement, html, nothing } from 'lit';
import { isWebLite, promptSystemAppDownload } from '../edition-gate.js';

export class OmnilearnEditionBanner extends LitElement {
  static properties = {
    edition: { type: String },
    dismissed: { type: Boolean, state: true },
  };

  constructor() {
    super();
    this.edition = 'web_lite';
    this.dismissed = false;
  }

  createRenderRoot() {
    return this;
  }

  _openDownloadModal() {
    promptSystemAppDownload('', 'Get the native Desktop App for offline study, instant search, large video lectures, and private local storage.');
  }

  render() {
    if (!isWebLite(this.edition) || this.dismissed) {
      return nothing;
    }

    return html`
      <div class="edition-banner view-enter" role="region" aria-label="Cloud Preview Notice">
        <div class="edition-banner-text">
          <span class="badge badge-amber">🌐 Cloud Preview</span>
          <span>
            You are exploring the web preview. For offline study, large video lectures, and instant search, get the Desktop App.
          </span>
        </div>
        <div class="row-wrap" style="gap: 8px; align-items: center;">
          <button
            type="button"
            class="btn btn-amber btn-sm"
            @click=${this._openDownloadModal}
          >
            ⬇️ Get Desktop App
          </button>
          <button
            type="button"
            class="btn btn-outline btn-sm"
            style="min-height: 32px; padding: 2px 8px;"
            aria-label="Dismiss banner"
            @click=${() => {
              this.dismissed = true;
            }}
          >
            ✕
          </button>
        </div>
      </div>
    `;
  }
}

customElements.define('omnilearn-edition-banner', OmnilearnEditionBanner);
