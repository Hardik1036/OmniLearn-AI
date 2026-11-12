import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';
import { isWebLite, promptSystemAppDownload } from '../edition-gate.js';

export class OmnilearnLoginView extends LitElement {
  static properties = {
    edition: { type: String },
    initialized: { type: Boolean },
    googleOauthEnabled: { type: Boolean },
    googleClientId: { type: String },
    googleCodeFlowEnabled: { type: Boolean },
    username: { type: String, state: true },
    password: { type: String, state: true },
    confirmPassword: { type: String, state: true },
    provider: { type: String, state: true },
    apiKey: { type: String, state: true },
    submitting: { type: Boolean, state: true },
    errorMessage: { type: String, state: true },
  };

  constructor() {
    super();
    this.edition = 'web_lite';
    this.initialized = true;
    this.googleOauthEnabled = false;
    this.googleClientId = '';
    this.googleCodeFlowEnabled = false;
    this.username = 'admin';
    this.password = '';
    this.confirmPassword = '';
    this.provider = 'gemini';
    this.apiKey = '';
    this.submitting = false;
    this.errorMessage = '';
  }

  createRenderRoot() {
    return this;
  }

  _clearSensitiveFields() {
    this.password = '';
    this.confirmPassword = '';
    this.apiKey = '';
    const keyInput = this.querySelector('input[name="personal_api_key"]');
    if (keyInput) keyInput.value = '';
    const pwInput = this.querySelector('input[name="admin_password"]');
    if (pwInput) pwInput.value = '';
    const confirmInput = this.querySelector('input[name="confirm_password"]');
    if (confirmInput) confirmInput.value = '';
  }

  async _handleSubmit(e) {
    e.preventDefault();
    this.errorMessage = '';

    const user = (this.username || '').trim();
    const pwd = this.password;
    const confirmPwd = this.confirmPassword;
    const prov = this.provider || 'gemini';
    const rawKey = this.apiKey;

    if (!user) {
      this.errorMessage = 'Please enter your Admin User ID.';
      return;
    }
    if (!pwd || pwd.length < 4) {
      this.errorMessage = 'Please enter an Admin Master Password (at least 4 characters).';
      return;
    }
    if (!this.initialized && pwd !== confirmPwd) {
      this.errorMessage = 'Passwords do not match.';
      return;
    }

    this.submitting = true;
    try {
      const response = this.initialized
        ? await apiClient.loginAdmin({ username: user, password: pwd, provider: prov, api_key: rawKey })
        : await apiClient.initAdmin({
            username: user,
            password: pwd,
            confirm_password: confirmPwd,
            provider: prov,
            api_key: rawKey,
          });

      this.dispatchEvent(
        new CustomEvent('auth-success', {
          bubbles: true,
          composed: true,
          detail: { response },
        })
      );
    } catch (err) {
      this.errorMessage = err?.message || 'Authentication failed.';
    } finally {
      // Zero-Leakage: purge plaintext password and API key immediately from JS memory & DOM
      this._clearSensitiveFields();
      this.submitting = false;
    }
  }

  async _launchGuestLite() {
    this.errorMessage = '';
    this.submitting = true;
    try {
      const response = await apiClient.launchGuestLite();
      this.dispatchEvent(
        new CustomEvent('auth-success', {
          bubbles: true,
          composed: true,
          detail: { response },
        })
      );
    } catch (err) {
      this.errorMessage = err?.message || 'Could not launch Web Lite preview.';
    } finally {
      this._clearSensitiveFields();
      this.submitting = false;
    }
  }

  async _handleGoogleCredential(credential) {
    if (!credential) return;
    this.errorMessage = '';
    this.submitting = true;
    try {
      const response = await apiClient.loginWithGoogle({
        id_token: credential,
        provider: this.provider || 'gemini',
        api_key: this.apiKey,
      });
      this.dispatchEvent(
        new CustomEvent('auth-success', {
          bubbles: true,
          composed: true,
          detail: { response },
        })
      );
    } catch (err) {
      this.errorMessage = err?.message || 'Google Sign-In failed.';
    } finally {
      this._clearSensitiveFields();
      this.submitting = false;
    }
  }

  async _startGoogleSignIn() {
    this.errorMessage = '';
    if (!this.googleOauthEnabled || !this.googleClientId) {
      this.errorMessage = 'Google Sign-In is not configured yet. Set GOOGLE_CLIENT_ID (and GOOGLE_CLIENT_SECRET) in `.env` or Worker Secrets.';
      return;
    }
    if (this.googleCodeFlowEnabled) {
      window.location.assign(apiClient.getGoogleOAuthStartUrl());
      return;
    }
    if (window.google?.accounts?.id) {
      window.google.accounts.id.initialize({
        client_id: this.googleClientId,
        callback: (resp) => this._handleGoogleCredential(resp?.credential),
      });
      window.google.accounts.id.prompt();
      return;
    }
    this.submitting = true;
    try {
      await new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = 'https://accounts.google.com/gsi/client';
        script.async = true;
        script.onload = resolve;
        script.onerror = () => reject(new Error('Could not load Google Sign-In script (internet required).'));
        document.head.appendChild(script);
      });
      if (window.google?.accounts?.id) {
        window.google.accounts.id.initialize({
          client_id: this.googleClientId,
          callback: (resp) => this._handleGoogleCredential(resp?.credential),
        });
        window.google.accounts.id.prompt();
      } else {
        this.errorMessage = 'Google Identity Services could not initialize.';
      }
    } catch (err) {
      this.errorMessage = err?.message || 'Unable to start Google Sign-In.';
    } finally {
      this.submitting = false;
    }
  }

  render() {
    const webLite = isWebLite(this.edition);

    return html`
      <div class="login-shell">
        <div class="login-card">
          <div class="row-between">
            <div class="row-wrap">
              <img src="/icons/icon.svg" alt="OmniLearn AI" width="44" height="44" />
              <div>
                <h1 style="font-size: 1.35rem; margin: 0;">OmniLearn AI</h1>
                <div class="text-sm text-muted">Multi-Source Exam Mastery Engine</div>
              </div>
            </div>
            ${webLite
              ? html`<span class="badge badge-amber">🌐 WEB LITE</span>`
              : html`<span class="badge badge-emerald">⚡ SYSTEM APP</span>`}
          </div>

          <div class="security-banner">
            🛡️ <strong>Single-Admin Protected Lock:</strong> Previously saved API keys are encrypted
            (AES-256-GCM / OS Keychain) and <strong>NEVER</strong> displayed or returned again.
          </div>

          ${this.errorMessage
            ? html`<div class="alert-error" role="alert">${this.errorMessage}</div>`
            : nothing}

          <form class="stack-md" @submit=${this._handleSubmit} autocomplete="off">
            <div class="field">
              <label class="field-label" for="admin_username">Admin User ID</label>
              <input
                id="admin_username"
                name="admin_username"
                type="text"
                class="input"
                required
                autocomplete="username"
                placeholder="Default: admin (editable in .env)"
                .value=${this.username}
                @input=${(e) => {
                  this.username = e.target.value;
                }}
              />
            </div>

            <div class="field">
              <label class="field-label" for="admin_password">Admin Master Password</label>
              <input
                id="admin_password"
                name="admin_password"
                type="password"
                class="input"
                required
                autocomplete="current-password"
                placeholder=${this.initialized ? 'Enter Admin password' : 'Create Admin master password (min 6 chars)'}
                .value=${this.password}
                @input=${(e) => {
                  this.password = e.target.value;
                }}
              />
            </div>

            ${!this.initialized
              ? html`
                  <div class="field">
                    <label class="field-label" for="confirm_password">Confirm Master Password</label>
                    <input
                      id="confirm_password"
                      name="confirm_password"
                      type="password"
                      class="input"
                      required
                      autocomplete="new-password"
                      placeholder="Re-enter Admin master password"
                      .value=${this.confirmPassword}
                      @input=${(e) => {
                        this.confirmPassword = e.target.value;
                      }}
                    />
                  </div>
                `
              : nothing}

            <div class="grid-2">
              <div class="field">
                <label class="field-label" for="api_provider">API Provider</label>
                <select
                  id="api_provider"
                  class="select"
                  .value=${this.provider}
                  @change=${(e) => {
                    this.provider = e.target.value;
                  }}
                >
                  <option value="gemini">Google Gemini Free Tier</option>
                  <option value="groq">Groq Cloud Fallback</option>
                  <option value="openrouter">OpenRouter Free</option>
                </select>
              </div>

              <div class="field">
                <label class="field-label" for="personal_api_key">Add Personal API Key (Optional)</label>
                <input
                  id="personal_api_key"
                  name="personal_api_key"
                  type="password"
                  class="input"
                  autocomplete="new-password"
                  placeholder="Paste key to append (stays hidden)"
                  .value=${this.apiKey}
                  @input=${(e) => {
                    this.apiKey = e.target.value;
                  }}
                />
              </div>
            </div>

            ${webLite
              ? html`
                  <div class="text-xs text-muted">
                    Web Lite caps active pool at 1 key; System App unlocks unlimited Multi-API rotation.
                  </div>
                `
              : nothing}

            <button type="submit" class="btn btn-navy btn-block" ?disabled=${this.submitting}>
              ${this.submitting
                ? 'Verifying...'
                : this.initialized
                ? '🔒 Unlock Workspace (Admin)'
                : '🔑 Initialize Admin & Enter'}
            </button>
          </form>

          <div class="stack-sm" style="padding-top: 10px; border-top: 1px solid var(--border-subtle);">
            <button
              type="button"
              class="btn btn-outline btn-block"
              ?disabled=${this.submitting}
              @click=${this._startGoogleSignIn}
            >
              🟦 Continue with Google (Gmail Normal User)
            </button>
            <div class="text-xs text-muted" style="text-align: center;">
              ${this.googleOauthEnabled
                ? 'Normal users can sign in with Gmail. Uses default .env API keys unless you add a personal key.'
                : 'To enable Gmail sign-in, set GOOGLE_CLIENT_ID & GOOGLE_CLIENT_SECRET in .env.'}
            </div>
          </div>

          ${webLite
            ? html`
                <div class="stack-sm" style="padding-top: 10px; border-top: 1px solid var(--border-subtle);">
                  <button
                    type="button"
                    class="btn btn-outline btn-block"
                    ?disabled=${this.submitting}
                    @click=${this._launchGuestLite}
                  >
                    🧪 Launch Limited Web Lite Preview (No Admin Account Needed)
                  </button>
                  <button
                    type="button"
                    class="btn btn-amber btn-block"
                    @click=${() => promptSystemAppDownload('', 'Download the native Desktop or Android System App for unlimited subjects, Multi-API rotation, and native file parsing (internet required).')}
                  >
                    ⬇️ Download Full System App (.exe / .dmg / .apk)
                  </button>
                </div>
              `
            : nothing}
        </div>
      </div>
    `;
  }
}

customElements.define('omnilearn-login', OmnilearnLoginView);
