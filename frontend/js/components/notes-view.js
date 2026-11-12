import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';
import {
  WEB_LITE_LIMITS,
  isWebLite,
  promptSystemAppDownload,
} from '../edition-gate.js';
import { exportNotesMarkdown } from '../export-utils.js';

export class OmnilearnNotesView extends LitElement {
  static properties = {
    edition: { type: String },
    courseData: { type: Object },
    activeSubjectId: { type: String },
  };

  constructor() {
    super();
    this.edition = 'web_lite';
    this.courseData = { course: null, rois: [], subjects: [], sources: [] };
    this.activeSubjectId = '';
  }

  createRenderRoot() {
    return this;
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
      <div class="stack-md">
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

            <button
              type="button"
              class="btn btn-outline btn-sm"
              @click=${() => exportNotesMarkdown(subject, courseTitle, this.edition)}
            >
              📥 Download Notes (.MD) ${webLite ? '🔒' : ''}
            </button>
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
                <h3>🖼️ Visual Slides, PDF Diagrams &amp; Keyframes (${slides.length})</h3>
                <div class="slide-carousel">
                  ${slides.map((slide) => {
                    const rawPath = typeof slide === 'string' ? slide : slide.url || slide.path || '';
                    const src = apiClient.getSlideUrl(rawPath);
                    const caption = typeof slide === 'object' ? slide.caption || slide.label || '' : '';
                    const citation = typeof slide === 'object' ? slide.citation || '' : '';
                    return html`
                      <div class="slide-card">
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
                  ${visibleConcepts.map((c) => {
                    if (typeof c === 'string') {
                      return html`<div class="card" style="background: var(--bg-page);">${c}</div>`;
                    }
                    return html`
                      <div class="card stack-sm" style="background: var(--bg-page);">
                        <div class="row-between">
                          <strong>${c.title || c.name || 'Concept'}</strong>
                          ${c.citation ? html`<span class="citation-badge">${c.citation}</span>` : nothing}
                        </div>
                        <div class="text-sm text-secondary">
                          ${c.explanation || c.description || c.detail || ''}
                        </div>
                      </div>
                    `;
                  })}
                </div>
              `}

          ${lockedConceptsCount > 0
            ? html`
                <div class="locked-card">
                  <div class="locked-overlay">
                    <span class="lock-pill">🔒 +${lockedConceptsCount} More Concepts &amp; Formulas in System App</span>
                    <button
                      type="button"
                      class="btn btn-amber btn-sm"
                      @click=${() => promptSystemAppDownload('full_notes_concepts', 'Web Lite displays 2 sample concepts per subject. Download the System App for complete notes.')}
                    >
                      Download Full System App
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
                  ${visibleFormulas.map((f) => {
                    const label = typeof f === 'object' ? f.label || f.name || '' : '';
                    const latex = typeof f === 'string' ? f : f.latex || f.formula || '';
                    const exp = typeof f === 'object' ? f.explanation || f.note || '' : '';
                    return html`
                      <div class="katex-block stack-sm">
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
                <h3>🔀 Concept Architecture Diagram (Mermaid Strict)</h3>
                <div class="mermaid-container" data-mermaid-diagram=${subject.mermaid_diagram}></div>
              </div>
            `
          : nothing}
      </div>
    `;
  }
}

customElements.define('omnilearn-notes', OmnilearnNotesView);
