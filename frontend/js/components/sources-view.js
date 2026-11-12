import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';
import {
  WEB_LITE_LIMITS,
  checkCapability,
  isWebLite,
  promptSystemAppDownload,
  requireCapability,
  safeHttpUrl,
} from '../edition-gate.js';
import { extractVideoKeyframes } from '../video-extractor.js';

const SEARCH_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'web', label: 'Web Docs' },
  { id: 'pdf', label: 'Direct PDFs' },
  { id: 'academic', label: 'Academic' },
  { id: 'youtube', label: 'YouTube' },
];

const RICH_DOC_EXTS = new Set(['.pdf', '.xlsx', '.xls', '.ppt', '.pptx', '.mkv', '.webm']);
const TEXT_EXTS = new Set(['.txt', '.md', '.csv']);
const VIDEO_EXTS = new Set(['.mp4', '.mkv', '.webm']);

function getFileExtension(filename) {
  const idx = String(filename || '').lastIndexOf('.');
  return idx >= 0 ? filename.slice(idx).toLowerCase() : '';
}

function classifyUrlType(urlStr) {
  const lower = String(urlStr || '').toLowerCase();
  if (lower.includes('youtube.com/') || lower.includes('youtu.be/')) {
    return 'youtube';
  }
  if (lower.split('?')[0].endsWith('.pdf')) {
    return 'web_pdf';
  }
  return 'web_url';
}

export class OmnilearnSourcesView extends LitElement {
  static properties = {
    edition: { type: String },
    courseData: { type: Object },
    pool: { type: Object },
    targetDays: { type: Number },
    dailyHours: { type: Number },
    activeSubTab: { type: String, state: true },
    searchQuery: { type: String, state: true },
    searchFilter: { type: String, state: true },
    searchResults: { type: Array, state: true },
    searching: { type: Boolean, state: true },
    inspectedUrlMap: { type: Object, state: true },
    selectedPdfUrls: { type: Object, state: true },
    inspectingUrl: { type: String, state: true },
    dragOver: { type: Boolean, state: true },
    stagedVideoFile: { type: Object, state: true },
    videoLayout: { type: String, state: true },
    taskbarCropPx: { type: Number, state: true },
    videoProgress: { type: Number, state: true },
    targetSubjectName: { type: String, state: true },
    appendSubject: { type: Boolean, state: true },
    urlInput: { type: String, state: true },
    urlInspectResult: { type: Object, state: true },
    urlSelectedPdfs: { type: Array, state: true },
    busyMessage: { type: String, state: true },
    errorMessage: { type: String, state: true },
    synthesizing: { type: Boolean, state: true },
    synthProgress: { type: Number, state: true },
    expandedSourceIds: { type: Object, state: true },
    poolProvider: { type: String, state: true },
    poolApiKey: { type: String, state: true },
    poolBusy: { type: Boolean, state: true },
  };

  constructor() {
    super();
    this.edition = 'web_lite';
    this.courseData = { course: null, rois: [], subjects: [], sources: [] };
    this.pool = { total_active_keys: 0, max_slots: 1, slots: [] };
    this.targetDays = 14;
    this.dailyHours = 3.0;
    this.activeSubTab = 'search';
    this.searchQuery = '';
    this.searchFilter = 'all';
    this.searchResults = [];
    this.searching = false;
    this.inspectedUrlMap = {};
    this.selectedPdfUrls = {};
    this.inspectingUrl = '';
    this.dragOver = false;
    this.stagedVideoFile = null;
    this.videoLayout = '2x2';
    this.taskbarCropPx = 48;
    this.videoProgress = 0;
    this.targetSubjectName = '';
    this.appendSubject = false;
    this.urlInput = '';
    this.urlInspectResult = null;
    this.urlSelectedPdfs = [];
    this.busyMessage = '';
    this.errorMessage = '';
    this.synthesizing = false;
    this.synthProgress = 0;
    this.expandedSourceIds = {};
    this.poolProvider = 'gemini';
    this.poolApiKey = '';
    this.poolBusy = false;
  }

  createRenderRoot() {
    return this;
  }

  _notifyUpdated(toastMsg = '', switchToTab = '') {
    this.dispatchEvent(
      new CustomEvent('course-updated', {
        bubbles: true,
        composed: true,
        detail: { toastMsg, switchToTab },
      })
    );
  }

  _notifyPoolUpdated(toastMsg = '') {
    this.dispatchEvent(
      new CustomEvent('pool-updated', {
        bubbles: true,
        composed: true,
        detail: { toastMsg },
      })
    );
  }

  _getCourseId() {
    return this.courseData?.course?.id || '';
  }

  _getSources() {
    return Array.isArray(this.courseData?.sources) ? this.courseData.sources : [];
  }

  _checkCanAddSource() {
    return requireCapability(this.edition, 'unlimited_sources', {
      sourceCount: this._getSources().length,
    });
  }

  _selectSearchFilter(filterId) {
    if (!requireCapability(this.edition, 'full_source_search', { filter: filterId })) {
      return;
    }
    this.searchFilter = filterId;
  }

  async _runSearch(e) {
    e?.preventDefault();
    this.errorMessage = '';
    const q = this.searchQuery.trim();
    if (!q) {
      this.errorMessage = 'Enter a topic, paper title, or course keyword to search.';
      return;
    }
    if (!requireCapability(this.edition, 'full_source_search', { filter: this.searchFilter })) {
      return;
    }

    this.searching = true;
    try {
      const res = await apiClient.searchSources({
        query: q,
        filter: this.searchFilter,
        course_id: this._getCourseId(),
      });
      const hits = Array.isArray(res) ? res : Array.isArray(res?.results) ? res.results : [];
      this.searchResults = isWebLite(this.edition) ? hits.slice(0, WEB_LITE_LIMITS.maxSearchResults) : hits;
    } catch (err) {
      this.errorMessage = err?.message || 'In-app source search failed.';
    } finally {
      this.searching = false;
    }
  }

  async _inspectResultForPdfs(url) {
    this.errorMessage = '';
    if (!requireCapability(this.edition, 'inspect_url_pdfs')) {
      return;
    }
    this.inspectingUrl = url;
    try {
      const res = await apiClient.inspectUrl({ url, course_id: this._getCourseId() });
      const pdfLinks = Array.isArray(res?.pdf_links)
        ? res.pdf_links
        : Array.isArray(res?.discovered_pdfs)
        ? res.discovered_pdfs
        : [];
      const textExcerpt = res?.text_excerpt || res?.text || res?.excerpt || '';
      this.inspectedUrlMap = {
        ...this.inspectedUrlMap,
        [url]: { ...res, text_excerpt: textExcerpt, pdf_links: pdfLinks },
      };
      this.selectedPdfUrls = {
        ...this.selectedPdfUrls,
        [url]: pdfLinks.map((p) => (typeof p === 'string' ? p : p.url)).filter(Boolean),
      };
    } catch (err) {
      this.errorMessage = err?.message || 'Could not inspect webpage for PDF links.';
    } finally {
      this.inspectingUrl = '';
    }
  }

  _toggleResultPdfSelection(pageUrl, pdfUrl) {
    const existing = Array.isArray(this.selectedPdfUrls[pageUrl]) ? this.selectedPdfUrls[pageUrl] : [];
    const next = existing.includes(pdfUrl)
      ? existing.filter((u) => u !== pdfUrl)
      : [...existing, pdfUrl];
    this.selectedPdfUrls = { ...this.selectedPdfUrls, [pageUrl]: next };
  }

  async _addSelectedResultPdfs(pageUrl) {
    const urls = Array.isArray(this.selectedPdfUrls[pageUrl]) ? this.selectedPdfUrls[pageUrl] : [];
    if (urls.length === 0) {
      this.errorMessage = 'Select at least one PDF link first.';
      return;
    }
    if (!this._checkCanAddSource()) return;

    this.busyMessage = `Downloading & parsing ${urls.length} selected PDF(s)...`;
    this.errorMessage = '';
    try {
      await apiClient.ingestUrl({
        course_id: this._getCourseId(),
        urls,
        source_type: 'web_pdf',
        search_query: this.searchQuery.trim(),
      });
      this._notifyUpdated(`Added ${urls.length} website PDF(s) to course queue.`);
    } catch (err) {
      this.errorMessage = err?.message || 'Failed to ingest selected PDFs.';
    } finally {
      this.busyMessage = '';
    }
  }

  async _addSearchResultDirectly(item) {
    this.errorMessage = '';
    if (!this._checkCanAddSource()) return;

    const itemType = item.source_type || (item.is_direct_pdf ? 'web_pdf' : classifyUrlType(item.url));
    if (itemType === 'youtube' && !requireCapability(this.edition, 'youtube_ingest')) return;
    if ((itemType === 'web_pdf' || item.is_direct_pdf) && !requireCapability(this.edition, 'rich_doc_upload')) return;

    this.busyMessage = `Adding "${item.title || item.url}"...`;
    try {
      await apiClient.ingestUrl({
        course_id: this._getCourseId(),
        url: item.url,
        title: item.title || '',
        snippet: item.snippet || '',
        source_type: itemType,
        search_query: this.searchQuery.trim(),
      });
      this._notifyUpdated(`Added "${item.title || 'source'}" to course queue.`);
    } catch (err) {
      this.errorMessage = err?.message || 'Could not add source.';
    } finally {
      this.busyMessage = '';
    }
  }

  _handleDrop(e) {
    e.preventDefault();
    this.dragOver = false;
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) {
      this._processSelectedFiles(files);
    }
  }

  _handleFileInputChange(e) {
    const files = e.target.files;
    if (files && files.length > 0) {
      this._processSelectedFiles(files);
    }
    e.target.value = '';
  }

  async _processSelectedFiles(fileList) {
    this.errorMessage = '';
    const files = Array.from(fileList);

    for (const file of files) {
      if (!this._checkCanAddSource()) return;

      const ext = getFileExtension(file.name);
      if (isWebLite(this.edition)) {
        if (RICH_DOC_EXTS.has(ext)) {
          requireCapability(this.edition, 'rich_doc_upload');
          return;
        }
        if (TEXT_EXTS.has(ext) && file.size > WEB_LITE_LIMITS.maxTextFileBytes) {
          this.errorMessage = `${file.name} exceeds Web Lite 500 KB limit for text/CSV previews.`;
          promptSystemAppDownload(
            'rich_doc_upload',
            'Web Lite caps text/CSV uploads at 500 KB. Download the System App for unlimited file sizes.'
          );
          return;
        }
      }

      if (VIDEO_EXTS.has(ext)) {
        this.stagedVideoFile = file;
        this.videoProgress = 0;
        return;
      }

      await this._uploadDocumentFile(file);
    }
  }

  async _uploadDocumentFile(file) {
    const courseId = this._getCourseId();
    if (!courseId) {
      this.errorMessage = 'Please select or create a course first.';
      return;
    }

    this.busyMessage = `Parsing & ingesting ${file.name}...`;
    this.errorMessage = '';
    try {
      const formData = new FormData();
      formData.append('course_id', courseId);
      formData.append('file', file, file.name);
      await apiClient.ingestFile(formData);
      this._notifyUpdated(`Ingested "${file.name}" into course queue.`);
    } catch (err) {
      this.errorMessage = err?.message || `Failed to ingest ${file.name}.`;
    } finally {
      this.busyMessage = '';
    }
  }

  async _processStagedVideo() {
    const file = this.stagedVideoFile;
    const courseId = this._getCourseId();
    if (!file || !courseId) return;
    if (!this._checkCanAddSource()) return;

    this.errorMessage = '';
    this.videoProgress = 5;

    if (!isWebLite(this.edition)) {
      this.busyMessage = `Uploading ${file.name} for native C++ OpenCV ${this.videoLayout} slicing...`;
      try {
        const formData = new FormData();
        formData.append('course_id', courseId);
        formData.append('layout_type', this.videoLayout);
        formData.append('taskbar_crop', String(this.taskbarCropPx));
        formData.append('subject_name', (this.targetSubjectName || '').trim());
        formData.append('file', file, file.name);
        await apiClient.ingestFile(formData);
        this.stagedVideoFile = null;
        this._notifyUpdated(`Video "${file.name}" processed cleanly.`);
      } catch (err) {
        this.errorMessage = err?.message || 'Video ingestion failed.';
      } finally {
        this.busyMessage = '';
        this.videoProgress = 0;
      }
      return;
    }

    // Web Lite browser <canvas> slicer (<= 5 min, <= 8 slides)
    this.busyMessage = `Extracting keyframes from ${file.name} via browser <canvas>...`;
    try {
      const extracted = await extractVideoKeyframes(file, {
        layoutType: this.videoLayout,
        taskbarCropPx: this.taskbarCropPx,
        maxFrames: WEB_LITE_LIMITS.maxVideoKeyframes,
        maxDurationSec: WEB_LITE_LIMITS.maxVideoDurationSec,
        onProgress: (pct) => {
          this.videoProgress = pct;
        },
      });

      const uploadedSlideUrls = [];
      for (const slide of extracted.slides) {
        const slideForm = new FormData();
        slideForm.append('course_id', courseId);
        slideForm.append('roi_key', slide.roi_key);
        slideForm.append('timestamp_sec', String(slide.timestamp_sec));
        slideForm.append('file', slide.blob, slide.filename);
        const upRes = await apiClient.uploadSlide(slideForm);
        if (upRes?.url || upRes?.path || upRes?.key) {
          uploadedSlideUrls.push(upRes.url || upRes.path || upRes.key);
        }
      }

      const manifestLines = [
        `Video Preview: ${file.name}`,
        `Layout: ${extracted.layoutType} | Duration: ${Math.round(extracted.duration)}s | Resolution: ${extracted.resolution}`,
        `Taskbar Crop: ${this.taskbarCropPx}px`,
        `Extracted Keyframes (${uploadedSlideUrls.length}):`,
        ...extracted.slides.map(
          (s, i) => `- [${s.roi_key} @ ${s.timestamp_sec}s] ${uploadedSlideUrls[i] || s.filename}`
        ),
      ];
      const manifestBlob = new File([manifestLines.join('\n')], `${file.name}.preview.txt`, {
        type: 'text/plain',
      });
      const formData = new FormData();
      formData.append('course_id', courseId);
      formData.append('layout_type', this.videoLayout);
      formData.append('taskbar_crop', String(this.taskbarCropPx));
      formData.append('file', manifestBlob, manifestBlob.name);
      await apiClient.ingestFile(formData);

      this.stagedVideoFile = null;
      this._notifyUpdated(`Extracted ${extracted.slides.length} preview keyframes from "${file.name}".`);
    } catch (err) {
      this.errorMessage = err?.message || 'Browser video extraction failed.';
    } finally {
      this.busyMessage = '';
      this.videoProgress = 0;
    }
  }

  async _handleUrlAction(e) {
    e?.preventDefault();
    this.errorMessage = '';
    const raw = this.urlInput.trim();
    const safeUrl = safeHttpUrl(raw);
    if (!safeUrl || safeUrl.startsWith('/api/')) {
      this.errorMessage = 'Please enter a valid http:// or https:// URL.';
      return;
    }
    if (!this._checkCanAddSource()) return;

    const kind = classifyUrlType(safeUrl);
    if (kind === 'youtube') {
      if (!requireCapability(this.edition, 'youtube_ingest')) return;
      await this._ingestSingleUrl(safeUrl, 'youtube');
      return;
    }
    if (kind === 'web_pdf') {
      if (!requireCapability(this.edition, 'rich_doc_upload')) return;
      await this._ingestSingleUrl(safeUrl, 'web_pdf');
      return;
    }

    // Webpage URL: inspect first in System App to discover PDFs, or preview webpage in Web Lite
    if (isWebLite(this.edition)) {
      await this._ingestSingleUrl(safeUrl, 'web_url');
      return;
    }

    this.busyMessage = 'Inspecting webpage & discovering linked PDFs...';
    try {
      const res = await apiClient.inspectUrl({ url: safeUrl, course_id: this._getCourseId() });
      const pdfs = Array.isArray(res?.pdf_links)
        ? res.pdf_links
        : Array.isArray(res?.discovered_pdfs)
        ? res.discovered_pdfs
        : [];
      const textExcerpt = res?.text_excerpt || res?.text || res?.excerpt || '';
      this.urlInspectResult = { ...res, url: safeUrl, text_excerpt: textExcerpt, pdf_links: pdfs };
      this.urlSelectedPdfs = pdfs.map((p) => (typeof p === 'string' ? p : p.url)).filter(Boolean);
    } catch (err) {
      this.errorMessage = err?.message || 'Failed to inspect URL.';
    } finally {
      this.busyMessage = '';
    }
  }

  async _ingestSingleUrl(url, sourceType) {
    this.busyMessage = `Fetching ${sourceType === 'youtube' ? 'YouTube lecture' : sourceType === 'web_pdf' ? 'PDF document' : 'webpage'}...`;
    try {
      await apiClient.ingestUrl({
        course_id: this._getCourseId(),
        url,
        source_type: sourceType,
      });
      this.urlInput = '';
      this.urlInspectResult = null;
      this._notifyUpdated('Source added to course queue.');
    } catch (err) {
      this.errorMessage = err?.message || 'Failed to ingest URL.';
    } finally {
      this.busyMessage = '';
    }
  }

  async _ingestInspectedPageText() {
    if (!this.urlInspectResult?.url) return;
    await this._ingestSingleUrl(this.urlInspectResult.url, 'web_url');
  }

  async _ingestSelectedUrlPdfs() {
    if (!this.urlSelectedPdfs.length) {
      this.errorMessage = 'Select at least one PDF from the discovered list.';
      return;
    }
    this.busyMessage = `Downloading ${this.urlSelectedPdfs.length} website PDF(s)...`;
    try {
      await apiClient.ingestUrl({
        course_id: this._getCourseId(),
        urls: this.urlSelectedPdfs,
        source_type: 'web_pdf',
      });
      this.urlInput = '';
      this.urlInspectResult = null;
      this.urlSelectedPdfs = [];
      this._notifyUpdated('Website PDFs added to course queue.');
    } catch (err) {
      this.errorMessage = err?.message || 'Failed to ingest website PDFs.';
    } finally {
      this.busyMessage = '';
    }
  }

  async _removeSource(sourceId) {
    this.errorMessage = '';
    try {
      await apiClient.deleteSource(sourceId);
      this._notifyUpdated('Source removed.');
    } catch (err) {
      this.errorMessage = err?.message || 'Could not remove source.';
    }
  }

  async _synthesizeCourse() {
    const courseId = this._getCourseId();
    if (!courseId) return;
    this.errorMessage = '';
    this.synthesizing = true;
    this.synthProgress = 18;

    const timer = setInterval(() => {
      if (this.synthProgress < 88) {
        this.synthProgress += 10;
      }
    }, 650);

    try {
      const sourceIds = this._getSources().map((s) => s.id).filter(Boolean);
      await apiClient.synthesizeCourse({
        course_id: courseId,
        source_ids: sourceIds,
        target_days: this.targetDays,
        daily_hours: this.dailyHours,
        target_subject_name: (this.targetSubjectName || '').trim(),
        append_subject: Boolean(this.appendSubject),
      });
      this.synthProgress = 100;
      this._notifyUpdated('Subject study pack synthesized online via Multi-API Pool!', 'notes');
    } catch (err) {
      this.errorMessage = err?.message || 'Course synthesis failed.';
    } finally {
      clearInterval(timer);
      this.synthesizing = false;
      setTimeout(() => {
        this.synthProgress = 0;
      }, 600);
    }
  }

  async _addPoolKey(e) {
    e.preventDefault();
    this.errorMessage = '';
    const slots = Array.isArray(this.pool?.slots) ? this.pool.slots : [];
    if (!requireCapability(this.edition, 'unlimited_pool', { slotCount: slots.length })) {
      return;
    }

    const rawKey = this.poolApiKey.trim();
    if (!rawKey) {
      this.errorMessage = 'Paste a valid API key to add to the pool.';
      return;
    }

    this.poolBusy = true;
    try {
      await apiClient.addPoolKey({ provider: this.poolProvider, api_key: rawKey });
      this._notifyPoolUpdated('API key encrypted and added to Multi-API Pool.');
    } catch (err) {
      this.errorMessage = err?.message || 'Could not add API key.';
    } finally {
      // Zero-Leakage: purge key immediately
      this.poolApiKey = '';
      const keyEl = this.querySelector('input[name="pool_api_key"]');
      if (keyEl) keyEl.value = '';
      this.poolBusy = false;
    }
  }

  async _deletePoolKey(slotId) {
    this.errorMessage = '';
    try {
      await apiClient.deletePoolKey(slotId);
      this._notifyPoolUpdated('API pool slot removed.');
    } catch (err) {
      this.errorMessage = err?.message || 'Could not remove API key slot.';
    }
  }

  render() {
    const course = this.courseData?.course;
    const sources = this._getSources();
    const webLite = isWebLite(this.edition);
    const slots = Array.isArray(this.pool?.slots) ? this.pool.slots : [];

    if (!course) {
      return html`<div class="card">Select or create a course in the sidebar to begin adding sources.</div>`;
    }

    return html`
      <div class="stack-md">
        <div class="card">
          <div class="row-between">
            <div>
              <h2>📂 Course Sources Workbench</h2>
              <div class="text-sm text-secondary">
                Combine Multi-Window Video, PDF, XLSX, PPTX, CSV, TXT, Webpages, Website PDFs &amp; YouTube into one curriculum.
              </div>
            </div>
            <span class="badge ${webLite ? 'badge-amber' : 'badge-emerald'}">
              ${webLite
                ? `🌐 ${sources.length} / ${WEB_LITE_LIMITS.maxSources} Sources (Web Lite)`
                : `⚡ ${sources.length} / ∞ Sources (Unlocked)`}
            </span>
          </div>
        </div>

        ${this.errorMessage ? html`<div class="alert-error" role="alert">${this.errorMessage}</div>` : nothing}
        ${this.busyMessage ? html`<div class="alert-info">${this.busyMessage}</div>` : nothing}

        <div class="sources-workbench">
          <!-- Left Column: In-App Discovery, File Dropzone, URL/YouTube -->
          <div class="card stack-md">
            <div class="subtab-bar" role="tablist" aria-label="Add Source Mode">
              <button
                type="button"
                class="pill-tab ${this.activeSubTab === 'search' ? 'active' : ''}"
                @click=${() => {
                  this.activeSubTab = 'search';
                }}
              >
                1. Search Web / Papers / YT
              </button>
              <button
                type="button"
                class="pill-tab ${this.activeSubTab === 'upload' ? 'active' : ''}"
                @click=${() => {
                  this.activeSubTab = 'upload';
                }}
              >
                2. Upload PDF / XLSX / PPT / MP4
              </button>
              <button
                type="button"
                class="pill-tab ${this.activeSubTab === 'url' ? 'active' : ''}"
                @click=${() => {
                  this.activeSubTab = 'url';
                }}
              >
                3. Paste URL / YT
              </button>
            </div>

            ${this.activeSubTab === 'search' ? this._renderSearchPanel(webLite) : nothing}
            ${this.activeSubTab === 'upload' ? this._renderUploadPanel(webLite) : nothing}
            ${this.activeSubTab === 'url' ? this._renderUrlPanel(webLite) : nothing}
          </div>

          <!-- Right Column: Staged Course Sources Queue + Synthesize + Multi-API Pool -->
          <div class="stack-md">
            <div class="card stack-md">
              <div class="row-between">
                <h3>Staged Course Sources (${sources.length})</h3>
                <span class="badge">${course.title}</span>
              </div>

              ${sources.length === 0
                ? html`
                    <div class="text-sm text-muted">
                      No sources attached yet. Search OpenAlex/Web/YouTube, upload files, or paste a URL on the left.
                    </div>
                  `
                : html`
                    <div class="stack-sm">
                      ${sources.map((src) => {
                        const expanded = !!this.expandedSourceIds[src.id];
                        const safeOrigin = safeHttpUrl(src.origin_url);
                        const pageMeta = src.metadata?.page_count
                          ? `${src.metadata.page_count}p`
                          : src.metadata?.sheet_names?.length
                          ? `${src.metadata.sheet_names.length} sheets`
                          : src.metadata?.slide_count
                          ? `${src.metadata.slide_count} slides`
                          : src.metadata?.duration_sec
                          ? `${Math.round(src.metadata.duration_sec / 60)}m`
                          : '';
                        return html`
                          <div class="queue-item">
                            <div class="row-between">
                              <div class="stack-sm" style="gap: 4px; min-width: 0; flex: 1;">
                                <div class="row-wrap">
                                  <span class="badge badge-blue">${String(src.source_type || 'doc').toUpperCase()}</span>
                                  ${pageMeta ? html`<span class="badge">${pageMeta}</span>` : nothing}
                                  <span class="badge badge-emerald">${src.status || 'PARSED'}</span>
                                </div>
                                <strong style="word-break: break-word;">${src.title}</strong>
                                ${safeOrigin
                                  ? html`<a class="text-xs" href=${safeOrigin} target="_blank" rel="noopener noreferrer">${safeOrigin}</a>`
                                  : nothing}
                              </div>
                              <div class="row-wrap">
                                <button
                                  type="button"
                                  class="btn btn-outline btn-sm"
                                  @click=${() => {
                                    this.expandedSourceIds = {
                                      ...this.expandedSourceIds,
                                      [src.id]: !expanded,
                                    };
                                  }}
                                >
                                  ${expanded ? 'Hide Preview' : 'Preview'}
                                </button>
                                <button
                                  type="button"
                                  class="btn btn-danger-outline btn-sm"
                                  aria-label="Remove source"
                                  @click=${() => this._removeSource(src.id)}
                                >
                                  🗑️
                                </button>
                              </div>
                            </div>

                            ${expanded
                              ? html`
                                  <div class="transcript-box" style="max-height: 150px;">
                                    ${src.raw_text_excerpt || 'No text excerpt extracted.'}
                                  </div>
                                `
                              : nothing}
                          </div>
                        `;
                      })}
                    </div>
                  `}

              ${this.synthesizing
                ? html`
                    <div class="stack-sm">
                      <div class="row-between text-xs">
                        <span>Synthesizing unified notes, diagrams, plan, quiz &amp; model paper...</span>
                        <span>${this.synthProgress}%</span>
                      </div>
                      <div class="progress-track">
                        <div class="progress-fill" style="width: ${this.synthProgress}%;"></div>
                      </div>
                    </div>
                  `
                : nothing}

              <div class="stack-sm" style="padding-top: 6px; border-top: 1px solid var(--border-subtle);">
                <div class="field">
                  <label class="field-label" for="synth-target-subject">
                    Target Subject Name (Unlimited Subjects)
                  </label>
                  <input
                    id="synth-target-subject"
                    type="text"
                    class="input"
                    placeholder="e.g., Operating Systems (all 4 video quadrants merged as Lecture 1-4)"
                    .value=${this.targetSubjectName}
                    @input=${(e) => {
                      this.targetSubjectName = e.target.value;
                    }}
                  />
                </div>
                <label class="pdf-check-row" style="margin-bottom: 4px;">
                  <input
                    type="checkbox"
                    .checked=${this.appendSubject}
                    @change=${(e) => {
                      this.appendSubject = e.target.checked;
                    }}
                  />
                  <span class="text-xs text-secondary">
                    Append / update this subject alongside existing subjects (do not limit to 4 subjects)
                  </span>
                </label>
              </div>

              <button
                type="button"
                class="btn btn-emerald btn-block"
                ?disabled=${this.synthesizing}
                @click=${this._synthesizeCourse}
              >
                ${this.synthesizing
                  ? '⏳ Synthesizing Online via AI Pool...'
                  : '🚀 Synthesize Subject Notes & Exam Pack (Online)'}
              </button>
            </div>

            <!-- Multi-API Pool Manager (Zero-Leakage Anonymous Slots) -->
            <div class="card stack-md">
              <div class="row-between">
                <h3>🔑 Multi-API Key Pool</h3>
                <span class="badge ${webLite ? 'badge-amber' : 'badge-emerald'}">
                  ${webLite ? `${slots.length}/1 Slot (Web Lite)` : `${slots.length} Active Slots`}
                </span>
              </div>
              <div class="text-xs text-muted">
                ${this.pool?.using_source === 'personal'
                  ? 'Mode: Personal API Pool active (automatically overrides defaults). Keys are AES-256-GCM encrypted and never displayed.'
                  : this.pool?.using_source === 'cloud_relay'
                  ? 'Mode: Cloudflare Worker Default AI Relay active (no personal key required). Paste your own key below anytime to override.'
                  : this.pool?.using_source === 'default_env'
                  ? 'Mode: Default .env API keys active. Add your own personal key below to override defaults for your account.'
                  : 'Keys are loaded from .env defaults or encrypted at rest (AES-256-GCM). Zero key characters are ever shown.'}
              </div>

              ${slots.length === 0
                ? html`<div class="text-sm text-muted">No API keys active yet. Add keys in <code>.env</code> or paste a personal key below (internet required).</div>`
                : html`
                    <div class="stack-sm">
                      ${slots.map(
                        (slot, idx) => html`
                          <div class="pool-slot-row">
                            <div class="row-wrap">
                              <span class="badge badge-blue">Slot #${slot.slot ?? idx + 1}</span>
                              <strong>${String(slot.provider || 'gemini').toUpperCase()}</strong>
                              ${slot.is_default || slot.source === 'env_default' || slot.source === 'env'
                                ? html`<span class="badge badge-blue">.env Default</span>`
                                : html`<span class="badge badge-emerald">Personal</span>`}
                              ${slot.overridden_by_personal
                                ? html`<span class="badge badge-amber">Overridden</span>`
                                : html`
                                    <span class="badge ${slot.status === 'ACTIVE' ? 'badge-emerald' : 'badge-amber'}">
                                      ${slot.status || 'ACTIVE'}
                                    </span>
                                  `}
                              <span class="text-xs text-muted">
                                ${slot.requests_served ?? slot.request_count ?? 0} calls
                              </span>
                            </div>
                            ${slot.is_default || slot.source === 'env_default' || slot.source === 'env'
                              ? html`<span class="text-xs text-muted">Managed in .env</span>`
                              : html`
                                  <button
                                    type="button"
                                    class="btn btn-danger-outline btn-sm"
                                    @click=${() => this._deletePoolKey(slot.id)}
                                  >
                                    Remove
                                  </button>
                                `}
                          </div>
                        `
                      )}
                    </div>
                  `}

              ${webLite && slots.length >= WEB_LITE_LIMITS.maxPoolKeys
                ? html`
                    <div class="locked-card">
                      <div class="locked-overlay">
                        <span class="lock-pill">🔒 Slot #2+ • System App Required</span>
                        <button
                          type="button"
                          class="btn btn-amber btn-sm"
                          @click=${() => promptSystemAppDownload('unlimited_pool', 'System App unlocks unlimited Gemini + Groq API rotation with 65s 429 cooldown.')}
                        >
                          Unlock Unlimited Pool
                        </button>
                      </div>
                    </div>
                  `
                : html`
                    <form class="stack-sm" @submit=${this._addPoolKey} autocomplete="off">
                      <div class="grid-2">
                        <select
                          class="select"
                          aria-label="Pool API Provider"
                          .value=${this.poolProvider}
                          @change=${(e) => {
                            this.poolProvider = e.target.value;
                          }}
                        >
                          <option value="gemini">Gemini 2.0 Flash</option>
                          <option value="groq">Groq Fallback</option>
                          <option value="openrouter">OpenRouter</option>
                        </select>
                        <input
                          name="pool_api_key"
                          type="password"
                          class="input"
                          autocomplete="new-password"
                          placeholder="Paste API key (stays hidden)"
                          .value=${this.poolApiKey}
                          @input=${(e) => {
                            this.poolApiKey = e.target.value;
                          }}
                        />
                      </div>
                      <button type="submit" class="btn btn-primary btn-sm" ?disabled=${this.poolBusy}>
                        ${this.poolBusy ? 'Encrypting...' : '➕ Add Key to Pool'}
                      </button>
                    </form>
                  `}
            </div>
          </div>
        </div>
      </div>
    `;
  }

  _renderSearchPanel(webLite) {
    return html`
      <div class="stack-md">
        <form class="row-wrap" @submit=${this._runSearch}>
          <input
            type="search"
            class="input"
            style="flex: 1; min-width: 200px;"
            placeholder="Search topics, papers, lecture PDFs, or YouTube..."
            .value=${this.searchQuery}
            @input=${(e) => {
              this.searchQuery = e.target.value;
            }}
          />
          <button type="submit" class="btn btn-primary" ?disabled=${this.searching}>
            ${this.searching ? 'Searching...' : '🔍 Search In-App'}
          </button>
        </form>

        <div class="pill-strip" role="group" aria-label="Search Source Filter">
          ${SEARCH_FILTERS.map((f) => {
            const cap = checkCapability(this.edition, 'full_source_search', { filter: f.id });
            return html`
              <button
                type="button"
                class="filter-chip ${this.searchFilter === f.id ? 'active' : ''}"
                @click=${() => this._selectSearchFilter(f.id)}
              >
                ${f.label} ${!cap.allowed ? '🔒' : ''}
              </button>
            `;
          })}
        </div>

        ${webLite
          ? html`
              <div class="text-xs text-muted">
                Web Lite Search previews up to 3 OpenAlex academic paper abstracts. Web Docs, Direct PDFs &amp; YouTube search require the System App.
              </div>
            `
          : nothing}

        <div class="stack-sm">
          ${this.searchResults.map((item) => {
            const safeUrl = safeHttpUrl(item.url);
            const isDirectPdf = !!item.is_direct_pdf || String(item.url || '').toLowerCase().endsWith('.pdf');
            const isYoutube = item.source_type === 'youtube' || String(item.url || '').includes('youtube.com');
            const inspected = this.inspectedUrlMap[item.url];
            const selectedPdfs = this.selectedPdfUrls[item.url] || [];

            return html`
              <div class="search-result-card">
                <div class="row-between">
                  <div style="min-width: 0; flex: 1;">
                    <div class="row-wrap" style="margin-bottom: 4px;">
                      <span class="badge ${isDirectPdf ? 'badge-emerald' : isYoutube ? 'badge-violet' : 'badge-blue'}">
                        ${isDirectPdf ? 'Direct PDF' : isYoutube ? 'YouTube' : item.source_type || 'Source'}
                      </span>
                      ${item.provider ? html`<span class="badge">${item.provider}</span>` : nothing}
                      ${item.duration || item.metadata?.duration
                        ? html`<span class="badge">${item.duration || item.metadata.duration}</span>`
                        : item.metadata?.year
                        ? html`<span class="badge">${item.metadata.year}</span>`
                        : nothing}
                    </div>
                    <strong>${item.title || item.url}</strong>
                    ${safeUrl
                      ? html`<div><a class="text-xs" href=${safeUrl} target="_blank" rel="noopener noreferrer">${safeUrl}</a></div>`
                      : nothing}
                  </div>
                </div>

                ${item.snippet ? html`<div class="text-sm text-secondary">${item.snippet}</div>` : nothing}

                <div class="row-wrap">
                  ${!isDirectPdf && !isYoutube
                    ? html`
                        <button
                          type="button"
                          class="btn btn-outline btn-sm"
                          ?disabled=${this.inspectingUrl === item.url}
                          @click=${() => this._inspectResultForPdfs(item.url)}
                        >
                          ${this.inspectingUrl === item.url
                            ? 'Inspecting...'
                            : `🔍 Inspect Page for PDFs ${webLite ? '🔒' : ''}`}
                        </button>
                      `
                    : nothing}
                  <button
                    type="button"
                    class="btn btn-primary btn-sm"
                    @click=${() => this._addSearchResultDirectly(item)}
                  >
                    ➕ Add Source Directly
                  </button>
                </div>

                ${inspected
                  ? html`
                      <div class="pdf-discovery-drawer stack-sm">
                        ${inspected.text_excerpt || inspected.text || inspected.excerpt
                          ? html`
                              <div class="text-xs text-secondary">
                                ${(inspected.text_excerpt || inspected.text || inspected.excerpt).slice(0, 240)}...
                              </div>
                            `
                          : nothing}
                        <div class="row-between">
                          <strong>Discovered PDFs on Page (${inspected.pdf_links.length})</strong>
                          ${inspected.pdf_links.length > 0
                            ? html`
                                <button
                                  type="button"
                                  class="btn btn-emerald btn-sm"
                                  @click=${() => this._addSelectedResultPdfs(item.url)}
                                >
                                  📥 Add Selected Website PDFs (${selectedPdfs.length})
                                </button>
                              `
                            : nothing}
                        </div>
                        ${inspected.pdf_links.length === 0
                          ? html`<div class="text-xs text-muted">No direct .pdf links found on this page.</div>`
                          : inspected.pdf_links.map((pdf) => {
                              const pdfUrl = typeof pdf === 'string' ? pdf : pdf.url;
                              const pdfTitle = typeof pdf === 'string' ? pdf : pdf.title || pdf.filename || pdf.url;
                              const checked = selectedPdfs.includes(pdfUrl);
                              return html`
                                <label class="pdf-check-row">
                                  <input
                                    type="checkbox"
                                    .checked=${checked}
                                    @change=${() => this._toggleResultPdfSelection(item.url, pdfUrl)}
                                  />
                                  <span class="text-sm" style="word-break: break-all;">${pdfTitle}</span>
                                </label>
                              `;
                            })}
                      </div>
                    `
                  : nothing}
              </div>
            `;
          })}
        </div>
      </div>
    `;
  }

  _renderUploadPanel(webLite) {
    return html`
      <div class="stack-md">
        <div
          class="dropzone ${this.dragOver ? 'dragover' : ''}"
          tabindex="0"
          role="button"
          aria-label="Upload course files"
          @dragover=${(e) => {
            e.preventDefault();
            this.dragOver = true;
          }}
          @dragleave=${() => {
            this.dragOver = false;
          }}
          @drop=${this._handleDrop}
          @click=${() => this.querySelector('#omnilearn-file-picker')?.click()}
          @keydown=${(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              this.querySelector('#omnilearn-file-picker')?.click();
            }
          }}
        >
          <input
            id="omnilearn-file-picker"
            type="file"
            class="hidden"
            multiple
            accept=".pdf,.txt,.md,.xlsx,.xls,.ppt,.pptx,.csv,.mp4,.mkv,.webm"
            @change=${this._handleFileInputChange}
          />
          <div style="font-size: 1.9rem;">📁</div>
          <strong>Drop files here or click to browse</strong>
          <div class="row-wrap" style="justify-content: center;">
            <span class="badge badge-blue">PDF ${webLite ? '🔒' : ''}</span>
            <span class="badge badge-blue">XLSX ${webLite ? '🔒' : ''}</span>
            <span class="badge badge-blue">PPT/PPTX ${webLite ? '🔒' : ''}</span>
            <span class="badge badge-emerald">TXT / MD</span>
            <span class="badge badge-emerald">CSV</span>
            <span class="badge badge-violet">MP4 / Video</span>
          </div>
          <div class="text-xs text-muted">
            ${webLite
              ? 'Web Lite supports .txt/.csv <= 500 KB and .mp4 <= 5 min preview. System App parses full PDF/XLSX/PPTX online.'
              : 'Full System App unlocks unlimited PDF, XLSX, PPTX, CSV, TXT & OpenCV 4-quadrant video lecture slicing (internet required for AI).'}
          </div>
        </div>

        ${this.stagedVideoFile
          ? html`
              <div class="card stack-md" style="background: var(--bg-subtle);">
                <div class="row-between">
                  <strong>🎬 Multi-Window Lecture Slicer: ${this.stagedVideoFile.name}</strong>
                  <button
                    type="button"
                    class="btn btn-outline btn-sm"
                    @click=${() => {
                      this.stagedVideoFile = null;
                    }}
                  >
                    Cancel
                  </button>
                </div>

                <div class="text-xs text-secondary">
                  All 4 quadrant windows in a 2x2 video are treated as 4 distinct lectures of ONE subject.
                </div>

                <div class="field">
                  <label class="field-label" for="video-subject-name">Target Subject for These Quadrant Lectures</label>
                  <input
                    id="video-subject-name"
                    type="text"
                    class="input"
                    placeholder="e.g., Data Structures & Algorithms"
                    .value=${this.targetSubjectName}
                    @input=${(e) => {
                      this.targetSubjectName = e.target.value;
                    }}
                  />
                </div>

                <div class="grid-2">
                  <div class="field">
                    <label class="field-label" for="video-layout-select">Multi-Window Layout</label>
                    <select
                      id="video-layout-select"
                      class="select"
                      .value=${this.videoLayout}
                      @change=${(e) => {
                        this.videoLayout = e.target.value;
                      }}
                    >
                      <option value="2x2">2x2 Grid (4 Simultaneous Windows)</option>
                      <option value="1x2">1x2 Side-by-Side (2 Windows)</option>
                      <option value="2x1">2x1 Stacked (2 Windows)</option>
                      <option value="1x1">1x1 Single Screen</option>
                    </select>
                  </div>

                  <div class="field">
                    <label class="field-label" for="taskbar-crop-range">
                      Bottom Taskbar Crop Offset: ${this.taskbarCropPx}px
                    </label>
                    <input
                      id="taskbar-crop-range"
                      type="range"
                      min="0"
                      max="100"
                      class="slider-input"
                      .value=${String(this.taskbarCropPx)}
                      @input=${(e) => {
                        this.taskbarCropPx = Number(e.target.value);
                      }}
                    />
                  </div>
                </div>

                ${this.videoProgress > 0
                  ? html`
                      <div class="progress-track">
                        <div class="progress-fill" style="width: ${this.videoProgress}%;"></div>
                      </div>
                    `
                  : nothing}

                <button type="button" class="btn btn-primary" @click=${this._processStagedVideo}>
                  ${webLite
                    ? '🎞️ Slice Preview Keyframes (<= 8 Slides in Canvas)'
                    : '⚡ Process Multi-Window Video with OpenCV'}
                </button>
              </div>
            `
          : nothing}
      </div>
    `;
  }

  _renderUrlPanel(webLite) {
    return html`
      <div class="stack-md">
        <form class="stack-sm" @submit=${this._handleUrlAction}>
          <label class="field-label" for="paste-source-url">
            Paste Web Link / Website PDF / YouTube URL
          </label>
          <div class="row-wrap">
            <input
              id="paste-source-url"
              type="url"
              class="input"
              style="flex: 1; min-width: 220px;"
              placeholder="https://university.edu/lectures or https://youtube.com/watch?v=..."
              .value=${this.urlInput}
              @input=${(e) => {
                this.urlInput = e.target.value;
              }}
            />
            <button type="submit" class="btn btn-primary">
              🔗 Fetch &amp; Discover Linked PDFs
            </button>
          </div>
          <div class="text-xs text-muted">
            ${webLite
              ? 'Web Lite supports 1 public webpage text preview. Direct PDFs, batch PDF crawler & YouTube require the System App.'
              : 'Auto-detects YouTube transcripts/chapters, direct .pdf URLs, or extracts readable webpage text + all linked PDFs.'}
          </div>
        </form>

        ${this.urlInspectResult
          ? html`
              <div class="pdf-discovery-drawer stack-sm">
                <div class="row-between">
                  <strong>${this.urlInspectResult.title || this.urlInspectResult.url}</strong>
                  <button
                    type="button"
                    class="btn btn-primary btn-sm"
                    @click=${this._ingestInspectedPageText}
                  >
                    ➕ Add Webpage Article Text
                  </button>
                </div>

                ${this.urlInspectResult.text_excerpt || this.urlInspectResult.text || this.urlInspectResult.excerpt
                  ? html`
                      <div class="text-xs text-secondary">
                        ${(this.urlInspectResult.text_excerpt || this.urlInspectResult.text || this.urlInspectResult.excerpt).slice(0, 320)}...
                      </div>
                    `
                  : nothing}

                <div class="row-between" style="margin-top: 6px;">
                  <strong>Linked PDFs Found (${this.urlInspectResult.pdf_links.length})</strong>
                  ${this.urlInspectResult.pdf_links.length > 0
                    ? html`
                        <button
                          type="button"
                          class="btn btn-emerald btn-sm"
                          @click=${this._ingestSelectedUrlPdfs}
                        >
                          📥 Add Selected Website PDFs (${this.urlSelectedPdfs.length})
                        </button>
                      `
                    : nothing}
                </div>

                ${this.urlInspectResult.pdf_links.map((pdf) => {
                  const pdfUrl = typeof pdf === 'string' ? pdf : pdf.url;
                  const pdfTitle = typeof pdf === 'string' ? pdf : pdf.title || pdf.filename || pdf.url;
                  const checked = this.urlSelectedPdfs.includes(pdfUrl);
                  return html`
                    <label class="pdf-check-row">
                      <input
                        type="checkbox"
                        .checked=${checked}
                        @change=${() => {
                          this.urlSelectedPdfs = checked
                            ? this.urlSelectedPdfs.filter((u) => u !== pdfUrl)
                            : [...this.urlSelectedPdfs, pdfUrl];
                        }}
                      />
                      <span class="text-sm" style="word-break: break-all;">${pdfTitle}</span>
                    </label>
                  `;
                })}
              </div>
            `
          : nothing}
      </div>
    `;
  }
}

customElements.define('omnilearn-sources', OmnilearnSourcesView);
