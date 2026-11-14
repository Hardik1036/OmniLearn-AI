import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';

export class OmnilearnSpatialView extends LitElement {
  static properties = {
    courseData: { type: Object },
    activeSlideModal: { type: Object, state: true },
  };

  constructor() {
    super();
    this.courseData = { course: null, rois: [], subjects: [], sources: [] };
    this.activeSlideModal = null;
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

  _gridClass(layoutType, count = 0) {
    if (count === 1) return 'spatial-grid-1x1';
    if (count > 4) return 'grid-3';
    switch (layoutType) {
      case '1x2':
        return 'spatial-grid-1x2';
      case '2x1':
        return 'spatial-grid-2x1';
      case '1x1':
        return 'spatial-grid-1x1';
      default:
        return 'spatial-grid-2x2';
    }
  }

  _groupQuadrantLectures(slides) {
    const quadrantDefs = [
      { key: 'top_left', label: 'Lecture 1 • Top-Left' },
      { key: 'top_right', label: 'Lecture 2 • Top-Right' },
      { key: 'bottom_left', label: 'Lecture 3 • Bottom-Left' },
      { key: 'bottom_right', label: 'Lecture 4 • Bottom-Right' },
    ];
    const groups = quadrantDefs
      .map((q) => ({
        ...q,
        slides: (slides || []).filter((s) => {
          const text = typeof s === 'string' ? s : `${s.quadrant_key || ''} ${s.caption || ''} ${s.label || ''} ${s.url || ''}`;
          return text.toLowerCase().includes(q.key);
        }),
      }))
      .filter((g) => g.slides.length > 0);
    return groups;
  }

  render() {
    const course = this.courseData?.course;
    const rois = Array.isArray(this.courseData?.rois) ? this.courseData.rois : [];
    const subjects = Array.isArray(this.courseData?.subjects) ? this.courseData.subjects : [];
    const sources = Array.isArray(this.courseData?.sources) ? this.courseData.sources : [];

    if (!course) {
      return html`<div class="card">Select or create a course to inspect spatial windows and visual slides.</div>`;
    }

    const layoutType = course.layout_type || '2x2';

    return html`
      <div class="stack-md view-enter">
        <div class="card">
          <div class="row-between">
            <div>
              <h2>🖥️ Split-Screen Lecture &amp; Visual Slide Inspector</h2>
              <div class="text-sm text-secondary">
                Synchronized lecture sections, slide diagrams, and timestamped study materials.
              </div>
            </div>
            <div class="row-wrap">
              <span class="badge badge-blue">Layout: ${layoutType}</span>
              <span class="badge">${subjects.length} Subjects</span>
              <span class="badge">${sources.length} Sources</span>
            </div>
          </div>

          ${rois.length > 0
            ? html`
                <div class="row-wrap" style="margin-top: 12px;">
                  ${rois.map(
                    (roi) => html`
                      <span class="badge">
                        ${roi.roi_key}: ${roi.name} (${roi.xmin}..${roi.xmax}, ${roi.ymin}..${roi.ymax})
                      </span>
                    `
                  )}
                </div>
              `
            : nothing}
        </div>

        ${subjects.length === 0
          ? html`
              <div class="card stack-md">
                <p class="text-secondary" style="margin: 0;">
                  No synthesized subjects yet. Add sources in the <strong>Sources</strong> tab and click
                  <strong>Synthesize Unified Course</strong> to populate spatial frames and transcripts.
                </p>
                ${sources.some((s) => Array.isArray(s.visual_assets) && s.visual_assets.length > 0)
                  ? html`
                      <div class="slide-carousel">
                        ${sources.flatMap((src) =>
                          (src.visual_assets || []).map((asset) => {
                            const rawPath = typeof asset === 'string' ? asset : asset.url || asset.path || '';
                            const srcUrl = apiClient.getSlideUrl(rawPath);
                            const caption = typeof asset === 'object' ? asset.label || asset.caption || src.title : src.title;
                            return html`
                              <div class="slide-card">
                                ${srcUrl ? html`<img src=${srcUrl} alt=${caption} loading="lazy" />` : nothing}
                                <div class="slide-card-body">
                                  <span class="citation-badge">${src.title}</span>
                                  <div class="text-xs text-secondary">${caption}</div>
                                </div>
                              </div>
                            `;
                          })
                        )}
                      </div>
                    `
                  : nothing}
              </div>
            `
          : html`
              <div class=${this._gridClass(layoutType, subjects.length)}>
                ${subjects.map((subj) => {
                  const slides = Array.isArray(subj.slide_crops) ? subj.slide_crops : [];
                  const quadrantGroups = this._groupQuadrantLectures(slides);
                  const transcriptItems = Array.isArray(subj.subtitles_transcript)
                    ? subj.subtitles_transcript
                    : [];
                  const transcriptText = transcriptItems
                    .map((item) => {
                      if (typeof item === 'string') return item;
                      const ts = item.timestamp || item.time || (item.start !== undefined ? `${item.start}s` : '');
                      return ts ? `[${ts}] ${item.text || ''}` : item.text || '';
                    })
                    .filter(Boolean)
                    .join('\n');

                  return html`
                    <div class="card stack-sm" style="border-top: 4px solid ${subj.badge_color || '#3B82F6'};">
                      <div class="row-between">
                        <div>
                          <span class="badge badge-blue">${subj.quadrant_key || 'Module'}</span>
                          <h3 style="margin-top: 6px; margin-bottom: 2px;">${subj.title}</h3>
                          <div class="text-xs text-muted">
                            ${[subj.unit, subj.topic, subj.instructor].filter(Boolean).join(' • ')}
                          </div>
                        </div>
                        <span class="badge">${slides.length} Visuals</span>
                      </div>

                      ${quadrantGroups.length > 1
                        ? html`
                            <div class="stack-sm">
                              <div class="text-xs text-muted">Split-Screen Lecture Sections Unified Under <strong>${subj.title}</strong></div>
                              <div class="spatial-grid-2x2">
                                ${quadrantGroups.map(
                                  (qg) => html`
                                    <div class="card stack-sm" style="padding: 12px; background: var(--bg-subtle);">
                                      <div class="row-between">
                                        <span class="badge badge-blue">${qg.label}</span>
                                        <span class="badge">${qg.slides.length} frames</span>
                                      </div>
                                      <div class="slide-carousel">
                                        ${qg.slides.map((slide) => {
                                          const rawPath = typeof slide === 'string' ? slide : slide.url || slide.path || '';
                                          const slideSrc = apiClient.getSlideUrl(rawPath);
                                          const caption = typeof slide === 'object' ? slide.caption || slide.label || '' : '';
                                          const cite = typeof slide === 'object' ? slide.citation || '' : '';
                                          return html`
                                            <div
                                              class="slide-card"
                                              style="cursor: pointer;"
                                              @click=${() => this._openSlideModal({
                                                url: rawPath,
                                                caption: caption || qg.label,
                                                citation: cite,
                                              })}
                                            >
                                              ${slideSrc
                                                ? html`<img src=${slideSrc} alt=${caption || qg.label} loading="lazy" />`
                                                : nothing}
                                              <div class="slide-card-body">
                                                ${cite ? html`<span class="citation-badge">${cite}</span>` : nothing}
                                                ${caption ? html`<div class="text-xs text-secondary">${caption}</div>` : nothing}
                                              </div>
                                            </div>
                                          `;
                                        })}
                                      </div>
                                    </div>
                                  `
                                )}
                              </div>
                            </div>
                          `
                        : slides.length > 0
                        ? html`
                            <div class="slide-carousel">
                              ${slides.map((slide) => {
                                const rawPath = typeof slide === 'string' ? slide : slide.url || slide.path || '';
                                const slideSrc = apiClient.getSlideUrl(rawPath);
                                const caption = typeof slide === 'object' ? slide.caption || slide.label || '' : '';
                                const cite = typeof slide === 'object' ? slide.citation || '' : '';
                                return html`
                                  <div
                                    class="slide-card"
                                    style="cursor: pointer;"
                                    @click=${() => this._openSlideModal({
                                      url: rawPath,
                                      caption: caption || subj.title,
                                      citation: cite,
                                    })}
                                  >
                                    ${slideSrc
                                      ? html`<img src=${slideSrc} alt=${caption || subj.title} loading="lazy" />`
                                      : nothing}
                                    <div class="slide-card-body">
                                      ${cite ? html`<span class="citation-badge">${cite}</span>` : nothing}
                                      ${caption ? html`<div class="text-xs text-secondary">${caption}</div>` : nothing}
                                    </div>
                                  </div>
                                `;
                              })}
                            </div>
                          `
                        : html`<div class="text-xs text-muted">No slide diagrams extracted for this section yet.</div>`}

                      <div>
                        <div class="text-xs text-muted" style="margin-bottom: 4px;">Synchronized Notes &amp; Transcript Excerpts</div>
                        <div class="transcript-box">
                          ${transcriptText || subj.summary || 'No transcript lines attached to this subject yet.'}
                        </div>
                      </div>
                    </div>
                  `;
                })}
              </div>
            `}

        <!-- Lightbox Zoom Modal for Split-Screen Visuals -->
        ${this.activeSlideModal
          ? html`
              <div class="lightbox-backdrop" @click=${this._closeSlideModal}>
                <div class="lightbox-modal card" @click=${(e) => e.stopPropagation()}>
                  <div class="row-between" style="margin-bottom: 12px;">
                    <div>
                      <h3 style="margin: 0;">${this.activeSlideModal.caption || 'Split-Screen Lecture Visual'}</h3>
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
                    <span class="text-xs text-muted">Captured from synchronized split-screen lecture video</span>
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

customElements.define('omnilearn-spatial', OmnilearnSpatialView);
