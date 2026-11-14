import { LitElement, html, nothing } from 'lit';
import { apiClient, readStorage, writeStorage } from './api-client.js';
import {
  isWebLite,
  normalizeCourseData,
  promptSystemAppDownload,
  requireCapability,
} from './edition-gate.js';

import './components/edition-banner.js';
import './components/download-modal.js';
import './components/login-view.js';
import './components/sources-view.js';
import './components/spatial-view.js';
import './components/notes-view.js';
import './components/plan-view.js';
import './components/quiz-view.js';
import './components/exam-view.js';
import './components/analytics-view.js';
import './components/tutor-chat.js';

const TABS = [
  { id: 'sources', icon: '🔍', label: 'Sources' },
  { id: 'spatial', icon: '🖥️', label: 'Visuals' },
  { id: 'notes', icon: '📚', label: 'Notes' },
  { id: 'plan', icon: '📅', label: 'Study Plan' },
  { id: 'quiz', icon: '🧠', label: 'Practice Quiz' },
  { id: 'exam', icon: '📑', label: 'Mock Exam' },
  { id: 'analytics', icon: '📊', label: 'Progress' },
];

export class OmnilearnApp extends LitElement {
  static properties = {
    booting: { type: Boolean, state: true },
    edition: { type: String, state: true },
    initialized: { type: Boolean, state: true },
    authenticated: { type: Boolean, state: true },
    role: { type: String, state: true },
    userEmail: { type: String, state: true },
    userName: { type: String, state: true },
    googleOauthEnabled: { type: Boolean, state: true },
    googleClientId: { type: String, state: true },
    googleCodeFlowEnabled: { type: Boolean, state: true },
    courses: { type: Array, state: true },
    activeCourseId: { type: String, state: true },
    courseData: { type: Object, state: true },
    activeTab: { type: String, state: true },
    activeSubjectId: { type: String, state: true },
    pool: { type: Object, state: true },
    targetDays: { type: Number, state: true },
    dailyHours: { type: Number, state: true },
    creatingCourse: { type: Boolean, state: true },
    newCourseTitle: { type: String, state: true },
    newCourseLayout: { type: String, state: true },
    creatingSubject: { type: Boolean, state: true },
    newSubjectTitle: { type: String, state: true },
    isOnline: { type: Boolean, state: true },
    ftsQuery: { type: String, state: true },
    ftsResults: { type: Array, state: true },
    ftsOpen: { type: Boolean, state: true },
    ftsLoading: { type: Boolean, state: true },
    drawerOpen: { type: Boolean, state: true },
    downloadModalOpen: { type: Boolean, state: true },
    downloadModalFeature: { type: String, state: true },
    downloadModalReason: { type: String, state: true },
    toast: { type: Object, state: true },
    bootError: { type: String, state: true },
  };

  constructor() {
    super();
    this.booting = true;
    this.edition = 'web_lite';
    this.initialized = true;
    this.authenticated = false;
    this.role = null;
    this.userEmail = '';
    this.userName = '';
    this.googleOauthEnabled = false;
    this.googleClientId = '';
    this.googleCodeFlowEnabled = false;
    this.courses = [];
    this.activeCourseId = '';
    this.courseData = normalizeCourseData(null);
    this.activeTab = 'notes';
    this.activeSubjectId = '';
    this.pool = { total_active_keys: 0, max_slots: 1, slots: [] };
    this.targetDays = 14;
    this.dailyHours = 3.0;
    this.creatingCourse = false;
    this.newCourseTitle = '';
    this.newCourseLayout = 'mixed';
    this.creatingSubject = false;
    this.newSubjectTitle = '';
    this.isOnline = typeof navigator !== 'undefined' ? navigator.onLine !== false : true;
    this.ftsQuery = '';
    this.ftsResults = [];
    this.ftsOpen = false;
    this.ftsLoading = false;
    this.drawerOpen = false;
    this.downloadModalOpen = false;
    this.downloadModalFeature = '';
    this.downloadModalReason = '';
    this.toast = null;
    this.bootError = '';
    this._toastTimer = null;

    this._onOpenDownload = (e) => {
      this.downloadModalFeature = e?.detail?.feature || '';
      this.downloadModalReason = e?.detail?.message || '';
      this.downloadModalOpen = true;
    };

    this._onOnlineStatus = () => {
      this.isOnline = typeof navigator !== 'undefined' ? navigator.onLine !== false : true;
    };

    this._onAuthExpired = () => {
      this.authenticated = false;
      this.role = null;
      this._showToast('Session locked or expired. Please sign in again.', 'error');
    };

    this._onKeyShortcut = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (!this.authenticated) return;
        if (isWebLite(this.edition)) {
          requireCapability(this.edition, 'fts_search');
          return;
        }
        this.querySelector('#fts-quick-search')?.focus();
      }
    };
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    // Clear pre-hydration static fallback markup inside <omnilearn-app> cleanly
    this.replaceChildren();
    super.connectedCallback();

    apiClient.bootstrap();
    this._onSwitchTab = (e) => {
      if (e.detail?.tabId) this._selectTab(e.detail.tabId);
    };
    window.addEventListener('omnilearn:switch-tab', this._onSwitchTab);
    window.addEventListener('omnilearn:edition-limit', this._onOpenDownload);
    window.addEventListener('omnilearn:open-download-modal', this._onOpenDownload);
    window.addEventListener('omnilearn:auth-expired', this._onAuthExpired);
    window.addEventListener('keydown', this._onKeyShortcut);
    window.addEventListener('online', this._onOnlineStatus);
    window.addEventListener('offline', this._onOnlineStatus);

    this._bootstrap();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('omnilearn:switch-tab', this._onSwitchTab);
    window.removeEventListener('omnilearn:edition-limit', this._onOpenDownload);
    window.removeEventListener('omnilearn:open-download-modal', this._onOpenDownload);
    window.removeEventListener('omnilearn:auth-expired', this._onAuthExpired);
    window.removeEventListener('keydown', this._onKeyShortcut);
    window.removeEventListener('online', this._onOnlineStatus);
    window.removeEventListener('offline', this._onOnlineStatus);
  }

  _showToast(message, type = 'success') {
    if (!message) return;
    clearTimeout(this._toastTimer);
    this.toast = { message, type };
    this._toastTimer = setTimeout(() => {
      this.toast = null;
    }, 4200);
  }

  async _bootstrap() {
    this.booting = true;
    this.bootError = '';
    try {
      // 1. Immediately extract OAuth auth_token or errors from URL before querying auth status!
      try {
        const url = new URL(window.location.href);
        const authToken = url.searchParams.get('auth_token');
        if (authToken) {
          apiClient.setSessionToken(authToken);
          url.searchParams.delete('auth_token');
          window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
        }
        if (url.searchParams.get('auth_error')) {
          this.bootError = url.searchParams.get('auth_error_msg') || 'Google Sign-In was cancelled or failed. Please try again.';
          url.searchParams.delete('auth_error');
          url.searchParams.delete('auth_error_msg');
          window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
        }
      } catch (_err) {
        // Ignore URL parsing errors
      }

      // 2. Query health & auth status (session token attached via Authorization header if present)
      const [healthRes, authRes] = await Promise.allSettled([
        apiClient.getHealth(),
        apiClient.getAuthStatus(),
      ]);

      const health = healthRes.status === 'fulfilled' ? healthRes.value : {};
      const auth = authRes.status === 'fulfilled' ? authRes.value : {};

      this.edition = health?.edition || auth?.edition || 'web_lite';
      this.initialized = Boolean(auth?.initialized ?? auth?.admin_initialized ?? true);
      this.authenticated = Boolean(auth?.authenticated);
      this.role = auth?.role || null;
      this.userEmail = auth?.email || auth?.username || '';
      this.userName = auth?.name || '';
      this.googleOauthEnabled = Boolean(auth?.google_oauth_enabled);
      this.googleClientId = auth?.google_client_id || '';
      this.googleCodeFlowEnabled = Boolean(auth?.google_code_flow_enabled);
      if (auth?.pool) {
        this.pool = auth.pool;
      }
      if (auth?.token) {
        apiClient.setSessionToken(auth.token);
      }

      if (this.authenticated) {
        await this._loadWorkspace();
      }
    } catch (err) {
      this.bootError = err?.message || 'Unable to reach OmniLearn API.';
    } finally {
      this.booting = false;
    }
  }

  async _loadWorkspace(preferredCourseId = '') {
    const [coursesRes, poolRes] = await Promise.allSettled([
      apiClient.listCourses(),
      apiClient.getPool(),
    ]);

    if (coursesRes.status === 'fulfilled') {
      const val = coursesRes.value;
      this.courses = Array.isArray(val) ? val : Array.isArray(val?.courses) ? val.courses : [];
    }
    if (poolRes.status === 'fulfilled' && poolRes.value) {
      this.pool = poolRes.value;
    }

    const userKey = this.userEmail || this.userName || 'user';
    const storageKey = `omnilearn_active_course_id_${userKey}`;
    const savedActiveId = readStorage(storageKey) || readStorage('omnilearn_active_course_id');
    const targetId =
      preferredCourseId ||
      (this.courses.some((c) => c.id === this.activeCourseId)
        ? this.activeCourseId
        : (savedActiveId && this.courses.some((c) => c.id === savedActiveId))
        ? savedActiveId
        : this.courses[0]?.id || '');

    if (targetId) {
      writeStorage(storageKey, targetId);
      writeStorage('omnilearn_active_course_id', targetId);
      await this._loadCourseDetail(targetId);
    } else {
      this.activeCourseId = '';
      this.courseData = normalizeCourseData(null);
      this.activeSubjectId = '';
      this.activeTab = 'sources';
    }
  }

  async _loadCourseDetail(courseId) {
    if (!courseId) return;
    this.activeCourseId = courseId;
    const userKey = this.userEmail || this.userName || 'user';
    writeStorage(`omnilearn_active_course_id_${userKey}`, courseId);
    writeStorage('omnilearn_active_course_id', courseId);
    try {
      const raw = await apiClient.getCourse(courseId);
      const normalized = normalizeCourseData(raw);
      this.courseData = normalized;
      const subjects = normalized.subjects;
      if (subjects.length > 0) {
        if (!subjects.some((s) => s.id === this.activeSubjectId)) {
          this.activeSubjectId = subjects[0].id;
        }
        const plan = subjects[0].study_plan;
        if (plan?.target_days) this.targetDays = plan.target_days;
        if (plan?.daily_hours) this.dailyHours = plan.daily_hours;
      } else {
        this.activeSubjectId = '';
        this.activeTab = 'sources';
      }
    } catch (err) {
      this._showToast(err?.message || 'Failed to load course details.', 'error');
    }
  }

  async _handleAuthSuccess(e) {
    const res = e?.detail?.response || {};
    this.authenticated = true;
    this.role = res.role || 'admin';
    this.userEmail = res.email || res.username || '';
    this.userName = res.name || '';
    if (res.edition) {
      this.edition = res.edition;
    }
    if (res.pool) {
      this.pool = res.pool;
    }
    await this._loadWorkspace();
  }

  async _handleLogout() {
    try {
      await apiClient.logout();
    } catch (_err) {
      // Clear local UI state even if request fails
    }
    this.authenticated = false;
    this.role = null;
    this.userEmail = '';
    this.userName = '';
    this.userEmail = '';
    this.userName = '';
    this.googleOauthEnabled = false;
    this.googleClientId = '';
    this.googleCodeFlowEnabled = false;
    this.drawerOpen = false;
    this.ftsOpen = false;
    this.courses = [];
    this.courseData = normalizeCourseData(null);
  }

  async _createCourse(e) {
    e.preventDefault();
    const title = this.newCourseTitle.trim();
    if (!title) return;
    try {
      const res = await apiClient.createCourse({
        title,
        layout_type: this.newCourseLayout,
      });
      const created = res?.course || res;
      const newId = created?.id || '';
      this.creatingCourse = false;
      this.newCourseTitle = '';
      if (newId) {
        writeStorage('omnilearn_active_course_id', newId);
      }
      await this._loadWorkspace(newId);
      this.activeTab = 'sources';
      this.drawerOpen = false;
      this._showToast(`Created course "${title}". Stage sources to begin.`);
    } catch (err) {
      this._showToast(err?.message || 'Could not create course.', 'error');
    }
  }

  async _deleteActiveCourse() {
    if (!this.activeCourseId) return;
    try {
      await apiClient.deleteCourse(this.activeCourseId);
      writeStorage('omnilearn_active_course_id', '');
      this.activeCourseId = '';
      await this._loadWorkspace();
      this._showToast('Course deleted.');
    } catch (err) {
      this._showToast(err?.message || 'Could not delete course.', 'error');
    }
  }

  async _createSubject(e) {
    e.preventDefault();
    const title = this.newSubjectTitle.trim();
    if (!title || !this.activeCourseId) return;
    try {
      const res = await apiClient.createSubject(this.activeCourseId, { title });
      const createdId = res?.subject?.id || '';
      this.creatingSubject = false;
      this.newSubjectTitle = '';
      await this._loadCourseDetail(this.activeCourseId);
      if (createdId) {
        this.activeSubjectId = createdId;
      }
      this._showToast(`Added subject "${title}".`);
    } catch (err) {
      this._showToast(err?.message || 'Could not add subject.', 'error');
    }
  }

  async _handleFtsSearch(e) {
    e?.preventDefault();
    if (!requireCapability(this.edition, 'fts_search')) {
      return;
    }
    const q = this.ftsQuery.trim();
    if (!q) {
      this.ftsResults = [];
      this.ftsOpen = false;
      return;
    }
    this.ftsLoading = true;
    this.ftsOpen = true;
    try {
      const res = await apiClient.ftsSearch({
        q,
        course_id: this.activeCourseId,
      });
      this.ftsResults = Array.isArray(res) ? res : Array.isArray(res?.results) ? res.results : [];
    } catch (err) {
      this._showToast(err?.message || 'Smart search failed.', 'error');
      this.ftsResults = [];
    } finally {
      this.ftsLoading = false;
    }
  }

  async _refreshPool() {
    try {
      const poolRes = await apiClient.getPool();
      if (poolRes) this.pool = poolRes;
    } catch (_err) {
      // Ignore silent refresh error
    }
  }

  _selectTab(tabId) {
    this.activeTab = tabId;
    this.drawerOpen = false;
  }

  render() {
    const webLite = isWebLite(this.edition);

    return html`
      <div class="app-shell desktop-titlebar-pad">
        <omnilearn-edition-banner .edition=${this.edition}></omnilearn-edition-banner>

        ${this.booting
          ? html`
              <div class="boot-shell">
                <div class="boot-card">
                  <img src="/icons/icon.svg" alt="OmniLearn AI" width="52" height="52" />
                  <h2>OmniLearn AI</h2>
                  <p class="text-secondary">Connecting to workspace engine...</p>
                </div>
              </div>
            `
          : !this.authenticated
          ? html`
              ${this.bootError ? html`<div class="alert-error">${this.bootError}</div>` : nothing}
              <omnilearn-login
                .edition=${this.edition}
                .initialized=${this.initialized}
                .googleOauthEnabled=${this.googleOauthEnabled}
                .googleClientId=${this.googleClientId}
                .googleCodeFlowEnabled=${this.googleCodeFlowEnabled}
                @auth-success=${this._handleAuthSuccess}
              ></omnilearn-login>
            `
          : this._renderWorkspace(webLite)}

        <omnilearn-download-modal
          .open=${this.downloadModalOpen}
          .feature=${this.downloadModalFeature}
          .reason=${this.downloadModalReason}
          @close-download-modal=${() => {
            this.downloadModalOpen = false;
          }}
        ></omnilearn-download-modal>

        ${this.toast
          ? html`
              <div
                class="toast-banner ${this.toast.type === 'error' ? 'toast-error' : 'toast-success'}"
                role="status"
              >
                <span>${this.toast.message}</span>
                <button
                  type="button"
                  class="btn btn-sm btn-outline"
                  style="min-height: 28px; padding: 2px 8px; color: #FFF; border-color: rgba(255,255,255,0.4); background: transparent;"
                  @click=${() => {
                    this.toast = null;
                  }}
                >
                  ✕
                </button>
              </div>
            `
          : nothing}
      </div>
    `;
  }

  _renderWorkspace(webLite) {
    const poolCount = this.pool?.total_active_keys ?? (this.pool?.slots?.length || 0);
    const subjects = Array.isArray(this.courseData?.subjects) ? this.courseData.subjects : [];

    return html`
      ${!this.isOnline
        ? html`
            <div class="alert-error" role="alert" style="margin: 12px 16px 0;">
              🌐 <strong>Internet connection required:</strong> Reconnect to use OmniLearn AI synthesis, grading, search, and cloud databases.
            </div>
          `
        : nothing}
      <!-- Top Bar -->
      <header class="top-bar">
        <div class="top-bar-left">
          <button
            type="button"
            class="mobile-menu-btn"
            aria-label="Toggle workspace menu"
            @click=${() => {
              this.drawerOpen = !this.drawerOpen;
            }}
          >
            ☰
          </button>
          <div class="brand-mark">
            <img src="/icons/icon.svg" alt="" width="32" height="32" />
            <span>OmniLearn AI</span>
          </div>
          ${this.courseData?.course?.title
            ? html`<span class="badge badge-blue">${this.courseData.course.title}</span>`
            : nothing}
        </div>

        <!-- Smart Course Search (Ctrl 10) -->
        <div class="top-bar-center">
          ${webLite
            ? html`
                <button
                  type="button"
                  class="btn btn-outline btn-block btn-sm"
                  style="justify-content: space-between;"
                  @click=${() => requireCapability(this.edition, 'fts_search')}
                >
                  <span>🔍 Smart Search across Notes &amp; Transcripts</span>
                  <span class="badge badge-amber">🔒 Desktop App</span>
                </button>
              `
            : html`
                <form @submit=${this._handleFtsSearch}>
                  <input
                    id="fts-quick-search"
                    type="search"
                    class="input"
                    style="min-height: 40px;"
                    placeholder="Search across notes, slides, and transcripts... (Ctrl+K)"
                    .value=${this.ftsQuery}
                    @input=${(e) => {
                      this.ftsQuery = e.target.value;
                      if (!this.ftsQuery.trim()) this.ftsOpen = false;
                    }}
                  />
                </form>
                ${this.ftsOpen
                  ? html`
                      <div class="fts-popover">
                        <div class="row-between" style="margin-bottom: 8px;">
                          <strong class="text-sm">Search Results (${this.ftsResults.length})</strong>
                          <button
                            type="button"
                            class="btn btn-outline btn-sm"
                            @click=${() => {
                              this.ftsOpen = false;
                            }}
                          >
                            Close
                          </button>
                        </div>
                        ${this.ftsLoading
                          ? html`<div class="text-sm text-muted">Searching course materials...</div>`
                          : this.ftsResults.length === 0
                          ? html`<div class="text-sm text-muted">No matching excerpts found.</div>`
                          : html`
                              <div class="stack-sm">
                                ${this.ftsResults.map(
                                  (hit) => html`
                                    <div
                                      class="queue-item"
                                      style="cursor: pointer;"
                                      @click=${() => {
                                        this.ftsOpen = false;
                                        this.activeTab = 'sources';
                                      }}
                                    >
                                      <div class="row-between">
                                        <strong>${hit.title}</strong>
                                        <span class="badge badge-blue">${hit.source_type || 'source'}</span>
                                      </div>
                                      <div class="text-xs text-secondary">${hit.snippet || hit.raw_text_excerpt || ''}</div>
                                    </div>
                                  `
                                )}
                              </div>
                            `}
                      </div>
                    `
                  : nothing}
              `}
        </div>

        <div class="top-bar-right">
          ${this.role === 'user' && this.userEmail
            ? html`<span class="badge badge-blue" title="Signed in via Google">${this.userEmail}</span>`
            : this.role === 'admin'
            ? html`<span class="badge badge-emerald">Admin</span>`
            : nothing}
          ${webLite
            ? html`
                <button
                  type="button"
                  class="edition-badge-lite"
                  @click=${() => promptSystemAppDownload('', 'Upgrade to the native Desktop App for unlimited subjects, offline study & multi-API rotation.')}
                >
                  <span class="pulse-dot"></span>
                  <span>Cloud Preview • Get Desktop App</span>
                </button>
              `
            : html`
                <span class="edition-badge-full">
                  ${this.isOnline ? '⚡ Desktop App • Connected' : '⚠️ Offline'}
                </span>
              `}
          <button type="button" class="btn btn-outline btn-sm" @click=${this._handleLogout}>
            🚪 Lock
          </button>
        </div>
      </header>

      ${this.drawerOpen
        ? html`<div class="drawer-backdrop" @click=${() => {
            this.drawerOpen = false;
          }}></div>`
        : nothing}

      <div class="workspace-body">
        <!-- Sidebar / Mobile Drawer -->
        <aside class="sidebar ${this.drawerOpen ? 'drawer-open' : ''}" aria-label="Course Sidebar">
          <div class="sidebar-section">
            <div class="row-between">
              <span class="sidebar-label">Course Library</span>
              <button
                type="button"
                class="btn btn-primary btn-sm"
                @click=${() => {
                  this.creatingCourse = !this.creatingCourse;
                }}
              >
                ➕ New Course
              </button>
            </div>

            ${this.creatingCourse
              ? html`
                  <form class="stack-sm card" style="padding: 12px;" @submit=${this._createCourse}>
                    <input
                      type="text"
                      class="input"
                      required
                      placeholder="e.g., Operating Systems Midterm"
                      .value=${this.newCourseTitle}
                      @input=${(e) => {
                        this.newCourseTitle = e.target.value;
                      }}
                    />
                    <select
                      class="select"
                      .value=${this.newCourseLayout}
                      @change=${(e) => {
                        this.newCourseLayout = e.target.value;
                      }}
                    >
                      <option value="mixed">Mixed Multi-Source</option>
                      <option value="2x2">2x2 Multi-Window Video</option>
                      <option value="1x2">1x2 Split Screen</option>
                      <option value="1x1">Single Screen</option>
                    </select>
                    <div class="row-wrap">
                      <button type="submit" class="btn btn-emerald btn-sm" style="flex: 1;">Create</button>
                      <button
                        type="button"
                        class="btn btn-outline btn-sm"
                        @click=${() => {
                          this.creatingCourse = false;
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </form>
                `
              : nothing}

            <select
              class="select"
              aria-label="Active Course"
              .value=${this.activeCourseId}
              @change=${(e) => {
                this._loadCourseDetail(e.target.value);
                this.drawerOpen = false;
              }}
            >
              ${this.courses.length === 0
                ? html`<option value="">No courses yet</option>`
                : this.courses.map(
                    (c) => html`<option value=${c.id}>${c.title}</option>`
                  )}
            </select>

            ${this.activeCourseId
              ? html`
                  <button
                    type="button"
                    class="btn btn-danger-outline btn-sm"
                    @click=${this._deleteActiveCourse}
                  >
                    🗑️ Delete Selected Course
                  </button>
                `
              : nothing}
          </div>

          ${this.activeCourseId
            ? html`
                <div class="sidebar-section">
                  <div class="row-between">
                    <span class="sidebar-label">Subjects (${subjects.length})</span>
                    <button
                      type="button"
                      class="btn btn-outline btn-sm"
                      @click=${() => {
                        this.creatingSubject = !this.creatingSubject;
                      }}
                    >
                      ➕ Add Subject
                    </button>
                  </div>

                  ${this.creatingSubject
                    ? html`
                        <form class="stack-sm card" style="padding: 10px;" @submit=${this._createSubject}>
                          <input
                            type="text"
                            class="input"
                            required
                            placeholder="e.g., Operating Systems"
                            .value=${this.newSubjectTitle}
                            @input=${(e) => {
                              this.newSubjectTitle = e.target.value;
                            }}
                          />
                          <div class="row-wrap">
                            <button type="submit" class="btn btn-emerald btn-sm" style="flex: 1;">Add</button>
                            <button
                              type="button"
                              class="btn btn-outline btn-sm"
                              @click=${() => {
                                this.creatingSubject = false;
                              }}
                            >
                              Cancel
                            </button>
                          </div>
                        </form>
                      `
                    : nothing}

                  ${subjects.length === 0
                    ? html`<div class="text-xs text-muted">No subjects yet. Add one or synthesize sources.</div>`
                    : html`
                        <div class="stack-sm" style="max-height: 220px; overflow-y: auto;">
                          ${subjects.map(
                            (s) => html`
                              <button
                                type="button"
                                class="btn btn-sm ${this.activeSubjectId === s.id ? 'btn-navy' : 'btn-outline'}"
                                style="justify-content: flex-start; text-align: left; border-left: 4px solid ${s.badge_color || '#3B82F6'};"
                                @click=${() => {
                                  this.activeSubjectId = s.id;
                                  if (this.activeTab === 'sources') {
                                    this.activeTab = 'notes';
                                  }
                                  this.drawerOpen = false;
                                }}
                              >
                                ${s.title}
                              </button>
                            `
                          )}
                        </div>
                      `}
                </div>
              `
            : nothing}

          <!-- Exam Pacing Sliders (Ctrl 11) -->
          <div class="sidebar-section">
            <span class="sidebar-label">Adaptive Study Pacing</span>
            <label class="text-xs text-secondary" for="slider-target-days">
              Target Exam Days: <strong>${this.targetDays} days</strong>
            </label>
            <input
              id="slider-target-days"
              type="range"
              min="3"
              max="60"
              class="slider-input"
              .value=${String(this.targetDays)}
              @input=${(e) => {
                this.targetDays = Number(e.target.value);
              }}
            />

            <label class="text-xs text-secondary" for="slider-daily-hours">
              Daily Study Hours: <strong>${this.dailyHours} hrs/day</strong>
            </label>
            <input
              id="slider-daily-hours"
              type="range"
              min="1"
              max="12"
              step="0.5"
              class="slider-input"
              .value=${String(this.dailyHours)}
              @input=${(e) => {
                this.dailyHours = Number(e.target.value);
              }}
            />
          </div>

          <!-- AI Study Assistant Status -->
          <div class="sidebar-section">
            <span class="sidebar-label">AI Study Assistant</span>
            <div class="row-between">
              <span class="badge badge-emerald">
                🟢 Ready
              </span>
              <button
                type="button"
                class="btn btn-outline btn-sm"
                @click=${() => this._selectTab('sources')}
              >
                Settings
              </button>
            </div>
            <div class="text-xs text-muted">
              ${this.role === 'guest_lite' ? 'Guest Preview' : 'Signed In'}
            </div>
          </div>

          <div class="sidebar-section">
            <button type="button" class="btn btn-outline btn-block" @click=${this._handleLogout}>
              🚪 Lock Workspace
            </button>
          </div>
        </aside>

        <!-- Main Workspace Content -->
        <main
          class="main-workspace"
          @select-subject=${(e) => {
            this.activeSubjectId = e.detail?.subjectId || '';
          }}
          @jump-to-quiz=${(e) => {
            if (e.detail?.subjectId) this.activeSubjectId = e.detail.subjectId;
            this.activeTab = 'quiz';
          }}
          @course-updated=${async (e) => {
            if (e.detail?.toastMsg) this._showToast(e.detail.toastMsg);
            if (this.activeCourseId) await this._loadWorkspace(this.activeCourseId);
            if (e.detail?.switchToTab) this.activeTab = e.detail.switchToTab;
          }}
          @pool-updated=${async (e) => {
            if (e.detail?.toastMsg) this._showToast(e.detail.toastMsg);
            await this._refreshPool();
          }}
        >
          <!-- Desktop 7-Tab Strip -->
          <nav class="desktop-tabs" aria-label="Workspace Tabs">
            ${TABS.map(
              (tab) => html`
                <button
                  type="button"
                  class="desktop-tab-btn ${this.activeTab === tab.id ? 'active' : ''}"
                  @click=${() => this._selectTab(tab.id)}
                >
                  <span>${tab.icon}</span>
                  <span>${tab.label}</span>
                </button>
              `
            )}
          </nav>

          ${this.activeTab === 'sources'
            ? html`
                <div class="view-enter">
                  <omnilearn-sources
                    .edition=${this.edition}
                    .courseData=${this.courseData}
                    .pool=${this.pool}
                    .targetDays=${this.targetDays}
                    .dailyHours=${this.dailyHours}
                  ></omnilearn-sources>
                </div>
              `
            : nothing}

          ${this.activeTab === 'spatial'
            ? html`
                <div class="view-enter">
                  <omnilearn-spatial .courseData=${this.courseData}></omnilearn-spatial>
                </div>
              `
            : nothing}

          ${this.activeTab === 'notes'
            ? html`
                <div class="view-enter">
                  <omnilearn-notes
                    .edition=${this.edition}
                    .courseData=${this.courseData}
                    .activeSubjectId=${this.activeSubjectId}
                  ></omnilearn-notes>
                </div>
              `
            : nothing}

          ${this.activeTab === 'plan'
            ? html`
                <div class="view-enter">
                  <omnilearn-plan
                    .courseData=${this.courseData}
                    .activeSubjectId=${this.activeSubjectId}
                    .targetDays=${this.targetDays}
                    .dailyHours=${this.dailyHours}
                  ></omnilearn-plan>
                </div>
              `
            : nothing}

          ${this.activeTab === 'quiz'
            ? html`
                <div class="view-enter">
                  <omnilearn-quiz
                    .edition=${this.edition}
                    .courseData=${this.courseData}
                    .activeSubjectId=${this.activeSubjectId}
                  ></omnilearn-quiz>
                </div>
              `
            : nothing}

          ${this.activeTab === 'exam'
            ? html`
                <div class="view-enter">
                  <omnilearn-exam
                    .edition=${this.edition}
                    .courseData=${this.courseData}
                    .activeSubjectId=${this.activeSubjectId}
                  ></omnilearn-exam>
                </div>
              `
            : nothing}

          ${this.activeTab === 'analytics'
            ? html`
                <div class="view-enter">
                  <omnilearn-analytics .courseData=${this.courseData}></omnilearn-analytics>
                </div>
              `
            : nothing}
        </main>
      </div>

      <!-- Android Fixed Bottom Navigation Bar (Ctrl 14) -->
      <nav class="bottom-nav" aria-label="Mobile Bottom Navigation">
        ${TABS.map(
          (tab) => html`
            <button
              type="button"
              class="bottom-nav-btn ${this.activeTab === tab.id ? 'active' : ''}"
              @click=${() => this._selectTab(tab.id)}
            >
              <span class="nav-ico">${tab.icon}</span>
              <span>${tab.label}</span>
            </button>
          `
        )}
      </nav>

      <!-- AI Study Tutor & Application Assistant Chatbot -->
      ${this.authenticated
        ? html`
            <omnilearn-tutor-chat
              .courseData=${this.courseData}
              .activeSubjectId=${this.activeSubjectId}
            ></omnilearn-tutor-chat>
          `
        : nothing}
    `;
  }
}

customElements.define('omnilearn-app', OmnilearnApp);
