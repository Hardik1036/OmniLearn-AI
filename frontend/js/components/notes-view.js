import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';
import {
  WEB_LITE_LIMITS,
  isWebLite,
  promptSystemAppDownload,
} from '../edition-gate.js';
import { exportNotesPdf, exportNotesMarkdown } from '../export-utils.js';

export class OmnilearnNotesView extends LitElement {
  static properties = {
    edition: { type: String },
    courseData: { type: Object },
    activeSubjectId: { type: String },
    activeSlideModal: { type: Object, state: true },
    exportDropdownOpen: { type: Boolean, state: true },
  };

  constructor() {
    super();
    this.edition = 'web_lite';
    this.courseData = { course: null, rois: [], subjects: [], sources: [] };
    this.activeSubjectId = '';
    this.activeSlideModal = null;
    this.exportDropdownOpen = false;
  }

  createRenderRoot() {
    return this;
  }

  _openSlideModal(slide) {
    if (!slide) return;
    this.activeSlideModal = typeof slide === 'string' ? { url: slide } : slide;
  }

  _closeSlideModal() {
    this.activeSlideModal = null;
  }

  _selectSubject(subjectId) {
    this.dispatchEvent(
      new CustomEvent('select-subject', {
        bubbles: true,
        composed: true,
        detail: { subjectId },
      })
    );
  }

  async _handleExportPdf(subject, courseTitle, forcePrint = false) {
    if (!subject) return;
    const mermaidSvg = this.querySelector('[data-mermaid-diagram] svg')?.outerHTML || '';
    const formulaEls = this.querySelectorAll('[data-formula-latex]');
    const renderedFormulas = Array.from(formulaEls).map((el) => ({
      latex: el.getAttribute('data-formula-latex') || '',
      html: el.innerHTML,
    }));
    await exportNotesPdf(subject, courseTitle, this.edition, {
      renderedMermaidSvg: mermaidSvg,
      renderedFormulas,
      forcePrint,
    });
  }

  _getCurrentSubject() {
    const subjects = Array.isArray(this.courseData?.subjects) ? this.courseData.subjects : [];
    return subjects.find((s) => s.id === this.activeSubjectId) || subjects[0] || null;
  }

  async updated() {
    await this._renderMathAndDiagrams();
  }

  async _renderMathAndDiagrams() {
    const subject = this._getCurrentSubject();
    if (!subject) return;

    // 1. Render KaTeX safely on textContent-backed hosts
    const formulaHosts = this.querySelectorAll('[data-formula-latex]');
    formulaHosts.forEach((host) => {
      const rawLatex = host.getAttribute('data-formula-latex') || '';
      const span = document.createElement('span');
      const wrapped =
        rawLatex.includes('$') ? rawLatex : `$$${rawLatex}$$`;
      span.textContent = wrapped;
      host.replaceChildren(span);

      if (typeof window !== 'undefined' && typeof window.renderMathInElement === 'function') {
        try {
          window.renderMathInElement(host, {
            delimiters: [
              { left: '$$', right: '$$', display: true },
              { left: '\\[', right: '\\]', display: true },
              { left: '$', right: '$', display: false },
              { left: '\\(', right: '\\)', display: false },
            ],
            throwOnError: false,
            trust: false,
          });
        } catch (_err) {
          // Graceful fallback displays plain text math
        }
      }
    });

    // 2. Render Mermaid diagram safely with securityLevel: 'strict'
    const mermaidHost = this.querySelector('[data-mermaid-diagram]');
    if (mermaidHost) {
      const diagramSrc = mermaidHost.getAttribute('data-mermaid-diagram') || '';
      const pre = document.createElement('pre');
      pre.className = 'mermaid';
      pre.textContent = diagramSrc;
      mermaidHost.replaceChildren(pre);

      if (typeof window !== 'undefined' && window.mermaid && typeof window.mermaid.run === 'function') {
        try {
          window.mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme: 'default',
          });
          await window.mermaid.run({ nodes: [pre] });
        } catch (_err) {
          // Fallback retains clean preformatted Mermaid text
        }
      }
    }
  }

  render() {
    const subjects = Array.isArray(this.courseData?.subjects) ? this.courseData.subjects : [];
    const courseTitle = this.courseData?.course?.title || 'OmniLearn Course';
    const subject = this._getCurrentSubject();
    const webLite = isWebLite(this.edition);

    if (!subject) {
      return html`
        <div class="card">
          <h3>📚 Visual Notes &amp; Diagrams</h3>
          <p class="text-secondary" style="margin: 0;">
            No subject notes found yet. Stage your course materials in the <strong>Sources</strong> tab and run
            <strong>Synthesize Unified Course</strong>.
          </p>
        </div>
      `;
    }

    const allConcepts = Array.isArray(subject.key_concepts) ? subject.key_concepts : [];
    const visibleConcepts = webLite
      ? allConcepts.slice(0, WEB_LITE_LIMITS.maxNotesConcepts)
      : allConcepts;
    const lockedConceptsCount = Math.max(0, allConcepts.length - visibleConcepts.length);

    const formulas = Array.isArray(subject.math_formulas) ? subject.math_formulas : [];
    const visibleFormulas = webLite ? formulas.slice(0, WEB_LITE_LIMITS.maxNotesConcepts) : formulas;
    const slides = Array.isArray(subject.slide_crops) ? subject.slide_crops : [];
    const citations = Array.isArray(subject.source_citations) ? subject.source_citations : [];

    return html`
      <div class="stack-md view-enter reading-measure" style="margin: 0 auto;">
        <!-- Subject Switcher Pills (Ctrl 29) & Export Markdown (Ctrl 30) -->
        <div class="card">
          <div class="row-between">
            <div class="pill-strip" role="tablist" aria-label="Subjects">
              ${subjects.map(
                (s) => html`
                  <button
                    type="button"
                    class="pill-tab ${s.id === subject.id ? 'active' : ''}"
                    @click=${() => this._selectSubject(s.id)}
                  >
                    ${s.short_title || s.title}
                  </button>
                `
              )}
            </div>

            <!-- Material 3 Split Button for Notes Export (PDF default with Visuals) -->
            <div class="split-button-container split-outline">
              <button
                type="button"
                class="btn btn-outline btn-sm split-button-action"
                @click=${() => this._handleExportPdf(subject, courseTitle, false)}
                title="Download publication-grade academic PDF with diagrams, slide visuals, and formulas"
              >
                📥 Download Notes (PDF) ${webLite ? '🔒' : ''}
              </button>
              <div class="split-button-divider"></div>
              <button
                type="button"
                class="btn btn-outline btn-sm split-button-trigger"
                aria-label="Export options"
                aria-expanded=${this.exportDropdownOpen}
                @click=${(e) => {
                  e.stopPropagation();
                  this.exportDropdownOpen = !this.exportDropdownOpen;
                }}
              >
                ▾
              </button>

              ${this.exportDropdownOpen
                ? html`
                    <div class="split-dropdown-menu">
                      <button
                        type="button"
                        class="split-dropdown-item"
                        @click=${() => {
                          this.exportDropdownOpen = false;
                          this._handleExportPdf(subject, courseTitle, false);
                        }}
                      >
                        📄 Download PDF (.pdf)
                      </button>
                      <button
                        type="button"
                        class="split-dropdown-item"
                        @click=${() => {
                          this.exportDropdownOpen = false;
                          this._handleExportPdf(subject, courseTitle, true);
                        }}
                      >
                        🖨️ Print / Save as PDF
                      </button>
                      <button
                        type="button"
                        class="split-dropdown-item"
                        @click=${() => {
                          this.exportDropdownOpen = false;
                          exportNotesMarkdown(subject, courseTitle, this.edition);
                        }}
                      >
                        📝 Download Markdown (.md)
                      </button>
                    </div>
                  `
                : nothing}
            </div>
          </div>
        </div>

        <!-- Subject Summary & Source Citations -->
        <div class="card stack-sm" style="border-left: 5px solid ${subject.badge_color || '#3B82F6'};">
          <div class="row-between">
            <div>
              <h2>${subject.title}</h2>
              <div class="text-sm text-muted">
                ${[subject.unit, subject.topic, subject.instructor].filter(Boolean).join(' • ')}
              </div>
            </div>
            <span class="badge badge-blue">${subject.quadrant_key || 'Unified Subject'}</span>
          </div>

          <p>${subject.summary || 'Synthesized summary ready.'}</p>

          ${citations.length > 0
            ? html`
                <div class="row-wrap">
                  ${citations.map((cite) => {
                    let label = '';
                    if (typeof cite === 'string') {
                      label = cite;
                    } else if (cite && typeof cite === 'object') {
                      const title = cite.label || cite.source || cite.title || '';
                      label = cite.location ? `${title} (${cite.location})` : title || JSON.stringify(cite);
                    }
                    return html`<span class="citation-badge">📌 ${label}</span>`;
                  })}
                </div>
              `
            : nothing}
        </div>

        <!-- Visual Asset Carousel (Ctrl 31) -->
        ${slides.length > 0
          ? html`
              <div class="card stack-sm">
                <h3>🖼️ Lecture Slides &amp; Diagrams (${slides.length})</h3>
                <div class="slide-carousel">
                  ${slides.map((slide) => {
                    const rawPath = typeof slide === 'string' ? slide : slide.url || slide.path || '';
                    const src = apiClient.getSlideUrl(rawPath);
                    const caption = typeof slide === 'object' ? slide.caption || slide.label || '' : '';
                    const citation = typeof slide === 'object' ? slide.citation || '' : '';
                    return html`
                      <div
                        class="slide-card"
                        style="cursor: pointer;"
                        @click=${() => this._openSlideModal({
                          url: rawPath,
                          caption: caption || subject.title,
                          citation,
                        })}
                      >
                        ${src ? html`<img src=${src} alt=${caption || subject.title} loading="lazy" />` : nothing}
                        <div class="slide-card-body">
                          ${citation ? html`<span class="citation-badge">${citation}</span>` : nothing}
                          ${caption ? html`<div class="text-xs text-secondary">${caption}</div>` : nothing}
                        </div>
                      </div>
                    `;
                  })}
                </div>
              </div>
            `
          : nothing}

        <!-- Key Concepts & KaTeX Formulas (Ctrl 32) -->
        <div class="card stack-md">
          <h3>🧠 Key Concepts</h3>
          ${visibleConcepts.length === 0
            ? html`<div class="text-sm text-muted">No key concepts extracted yet.</div>`
            : html`
                <div class="grid-2">
                  ${visibleConcepts.map((c, idx) => {
                    if (typeof c === 'string') {
                      return html`<div class="card stagger-card" style="background: var(--bg-page); --stagger-index: ${idx};">${c}</div>`;
                    }
                    const conceptSlideUrl = c.slide_url || '';
                    const conceptSlideSrc = conceptSlideUrl ? apiClient.getSlideUrl(conceptSlideUrl) : '';
                    return html`
                      <div class="card stack-sm stagger-card" style="background: var(--bg-page); --stagger-index: ${idx};">
                        <div class="row-between">
                          <strong>${c.title || c.name || 'Concept'}</strong>
                          ${c.citation ? html`<span class="citation-badge">${c.citation}</span>` : nothing}
                        </div>
                        <div class="text-sm text-secondary">
                          ${c.explanation || c.description || c.detail || ''}
                        </div>
                        ${c.exam_tip
                          ? html`
                              <div
                                style="margin-top: 6px; font-size: 0.8rem; background: #FEF3C7; color: #92400E; border-left: 3px solid #D97706; padding: 4px 8px; border-radius: 4px; font-weight: 500;"
                              >
                                💡 <strong>Exam Tip:</strong> ${c.exam_tip}
                              </div>
                            `
                          : nothing}
                        ${conceptSlideSrc
                          ? html`
                              <div
                                class="concept-slide-preview"
                                style="margin-top: 8px; border-radius: 6px; overflow: hidden; border: 1px solid var(--border-subtle); background: var(--bg-surface); cursor: pointer;"
                                @click=${() =>
                                  this._openSlideModal({
                                    url: conceptSlideUrl,
                                    caption: c.slide_caption || c.title,
                                    timestamp: c.slide_timestamp,
                                    citation: c.citation,
                                  })}
                              >
                                <img
                                  src=${conceptSlideSrc}
                                  alt=${c.slide_caption || c.title}
                                  loading="lazy"
                                  style="width: 100%; max-height: 140px; object-fit: cover; display: block;"
                                />
                                <div class="row-between text-xs" style="padding: 4px 8px; background: rgba(15, 23, 42, 0.7); color: var(--text-secondary);">
                                  <span>📸 ${c.slide_caption || 'Lecture Slide Visual'}</span>
                                  ${c.slide_timestamp ? html`<span class="badge badge-sm">⏱️ ${c.slide_timestamp}</span>` : nothing}
                                </div>
                              </div>
                            `
                          : nothing}
                      </div>
                    `;
                  })}
                </div>
              `}

          ${lockedConceptsCount > 0
            ? html`
                <div class="locked-card">
                  <div class="locked-overlay">
                    <span class="lock-pill">🔒 +${lockedConceptsCount} More Concepts &amp; Formulas in Desktop App</span>
                    <button
                      type="button"
                      class="btn btn-amber btn-sm"
                      @click=${() => promptSystemAppDownload('full_notes_concepts', 'Cloud Preview displays 2 sample concepts per subject. Get the Desktop App for complete notes.')}
                    >
                      Get Desktop App
                    </button>
                  </div>
                </div>
              `
            : nothing}
        </div>

        ${visibleFormulas.length > 0
          ? html`
              <div class="card stack-md">
                <h3>📐 Mathematical Formulas (KaTeX)</h3>
                <div class="stack-sm">
                  ${visibleFormulas.map((f, idx) => {
                    const label = typeof f === 'object' ? f.label || f.name || '' : '';
                    const latex = typeof f === 'string' ? f : f.latex || f.formula || '';
                    const exp = typeof f === 'object' ? f.explanation || f.note || '' : '';
                    return html`
                      <div class="katex-block stack-sm stagger-card" style="--stagger-index: ${idx};">
                        ${label ? html`<strong>${label}</strong>` : nothing}
                        <div data-formula-latex=${latex}></div>
                        ${exp ? html`<div class="text-xs text-secondary">${exp}</div>` : nothing}
                      </div>
                    `;
                  })}
                </div>
              </div>
            `
          : nothing}

        ${subject.mermaid_diagram
          ? html`
              <div class="card stack-sm">
                <h3>🔀 Interactive Concept Map</h3>
                <div class="mermaid-container" data-mermaid-diagram=${subject.mermaid_diagram}></div>
              </div>
            `
          : nothing}

        <!-- Visual Slide Zoom Lightbox Modal -->
        ${this.activeSlideModal
          ? html`
              <div class="lightbox-backdrop" @click=${this._closeSlideModal}>
                <div class="lightbox-modal card" @click=${(e) => e.stopPropagation()}>
                  <div class="row-between" style="margin-bottom: 12px;">
                    <div>
                      <h3 style="margin: 0;">${this.activeSlideModal.caption || 'Lecture Slide Visual'}</h3>
                      <div class="text-xs text-secondary" style="margin-top: 4px;">
                        ${this.activeSlideModal.citation || ''} ${this.activeSlideModal.timestamp ? `• ⏱️ ${this.activeSlideModal.timestamp}` : ''}
                      </div>
                    </div>
                    <button type="button" class="btn btn-outline btn-sm" @click=${this._closeSlideModal}>
                      ✕ Close
                    </button>
                  </div>
                  <div class="lightbox-frame">
                    <img
                      src=${apiClient.getSlideUrl(this.activeSlideModal.url)}
                      alt=${this.activeSlideModal.caption || 'Lecture Visual Frame'}
                      class="lightbox-img"
                    />
                  </div>
                  <div class="row-between" style="margin-top: 10px;">
                    <span class="text-xs text-muted">Captured from synchronized video lecture stream</span>
                    <a
                      href=${apiClient.getSlideUrl(this.activeSlideModal.url)}
                      target="_blank"
                      rel="noopener noreferrer"
                      class="btn btn-outline btn-sm"
                    >
                      ↗️ Open High-Res Frame
                    </a>
                  </div>
                </div>
              </div>
            `
          : nothing}
      </div>
    `;
  }
}

customElements.define('omnilearn-notes', OmnilearnNotesView);
