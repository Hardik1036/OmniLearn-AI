import { LitElement, html, nothing } from 'lit';

export class OmnilearnAnalyticsView extends LitElement {
  static properties = {
    courseData: { type: Object },
  };

  constructor() {
    super();
    this.courseData = { course: null, rois: [], subjects: [], sources: [] };
  }

  createRenderRoot() {
    return this;
  }

  _jumpToWeakestQuiz(subjectId) {
    this.dispatchEvent(
      new CustomEvent('jump-to-quiz', {
        bubbles: true,
        composed: true,
        detail: { subjectId },
      })
    );
  }

  _renderRadarSvg(subjects) {
    if (!subjects.length) {
      return nothing;
    }

    const size = 340;
    const cx = size / 2;
    const cy = size / 2;
    const radius = 108;
    const rings = [25, 50, 75, 100];
    const count = Math.max(3, subjects.length);

    const angleForIndex = (i) => -Math.PI / 2 + (i * 2 * Math.PI) / count;

    // Pad with synthetic baseline axes if < 3 subjects so polygon is visually sound
    const radarItems = [...subjects];
    while (radarItems.length < 3) {
      radarItems.push({
        short_title: `Target ${radarItems.length + 1}`,
        progress: { mastery_score: 0 },
      });
    }

    const dataPoints = radarItems.map((subj, idx) => {
      const score = Math.max(8, Math.min(100, Number(subj.progress?.mastery_score ?? 0)));
      const r = (score / 100) * radius;
      const angle = angleForIndex(idx);
      return {
        x: cx + r * Math.cos(angle),
        y: cy + r * Math.sin(angle),
        labelX: cx + (radius + 28) * Math.cos(angle),
        labelY: cy + (radius + 24) * Math.sin(angle),
        label: subj.short_title || subj.title || `Subj ${idx + 1}`,
        score: Math.round(Number(subj.progress?.mastery_score ?? 0)),
      };
    });

    const polygonPoints = dataPoints.map((pt) => `${pt.x.toFixed(1)},${pt.y.toFixed(1)}`).join(' ');

    return html`
      <svg
        viewBox="0 0 ${size} ${size}"
        width="100%"
        style="max-width: 360px; height: auto; display: block; margin: 0 auto;"
        role="img"
        aria-label="Subject Competency Radar Chart"
      >
        ${rings.map((ring) => {
          const r = (ring / 100) * radius;
          const ringPts = radarItems
            .map((_, idx) => {
              const a = angleForIndex(idx);
              return `${(cx + r * Math.cos(a)).toFixed(1)},${(cy + r * Math.sin(a)).toFixed(1)}`;
            })
            .join(' ');
          return html`
            <polygon
              points=${ringPts}
              fill="none"
              stroke="#CBD5E1"
              stroke-width="1"
              stroke-dasharray=${ring < 100 ? '3,3' : ''}
            />
          `;
        })}

        ${radarItems.map((_, idx) => {
          const a = angleForIndex(idx);
          const x2 = cx + radius * Math.cos(a);
          const y2 = cy + radius * Math.sin(a);
          return html`<line x1=${cx} y1=${cy} x2=${x2.toFixed(1)} y2=${y2.toFixed(1)} stroke="#CBD5E1" stroke-width="1" />`;
        })}

        <polygon
          points=${polygonPoints}
          fill="rgba(59, 130, 246, 0.25)"
          stroke="#1E3A8A"
          stroke-width="2.5"
        />

        ${dataPoints.map(
          (pt) => html`
            <circle cx=${pt.x.toFixed(1)} cy=${pt.y.toFixed(1)} r="5" fill="#10B981" stroke="#FFFFFF" stroke-width="1.5" />
            <text
              x=${pt.labelX.toFixed(1)}
              y=${pt.labelY.toFixed(1)}
              text-anchor="middle"
              dominant-baseline="middle"
              font-size="11"
              font-weight="700"
              fill="#0F172A"
            >
              ${pt.label} (${pt.score}%)
            </text>
          `
        )}
      </svg>
    `;
  }

  render() {
    const subjects = Array.isArray(this.courseData?.subjects) ? this.courseData.subjects : [];
    const sources = Array.isArray(this.courseData?.sources) ? this.courseData.sources : [];

    const totalTasks = subjects.reduce((sum, s) => {
      const schedule = Array.isArray(s.study_plan?.schedule) ? s.study_plan.schedule : [];
      return (
        sum +
        schedule.reduce(
          (daySum, day) => daySum + (Array.isArray(day?.tasks) && day.tasks.length > 0 ? day.tasks.length : 1),
          0
        )
      );
    }, 0);
    const completedTasks = subjects.reduce(
      (sum, s) => sum + (Array.isArray(s.progress?.completed_tasks) ? s.progress.completed_tasks.length : 0),
      0
    );
    const avgMastery = subjects.length
      ? Math.round(
          subjects.reduce((sum, s) => sum + Number(s.progress?.mastery_score ?? 0), 0) / subjects.length
        )
      : 0;
    const avgQuiz = subjects.length
      ? Math.round(
          subjects.reduce((sum, s) => sum + Number(s.progress?.last_quiz_score ?? 0), 0) / subjects.length
        )
      : 0;

    const weakestSubject = subjects.length
      ? [...subjects].sort(
          (a, b) => Number(a.progress?.mastery_score ?? 0) - Number(b.progress?.mastery_score ?? 0)
        )[0]
      : null;

    return html`
      <div class="stack-md">
        <!-- Top 4 Metric Cards (Ctrl 42) -->
        <div class="grid-4">
          <div class="metric-card">
            <span class="text-xs text-muted">Ingested Sources</span>
            <span class="metric-value">${sources.length}</span>
            <span class="text-xs text-secondary">Unified course inputs</span>
          </div>
          <div class="metric-card">
            <span class="text-xs text-muted">Overall Readiness</span>
            <span class="metric-value">${avgMastery}%</span>
            <span class="text-xs text-secondary">Weighted subject mastery</span>
          </div>
          <div class="metric-card">
            <span class="text-xs text-muted">Study Tasks Done</span>
            <span class="metric-value">${completedTasks} / ${totalTasks}</span>
            <span class="text-xs text-secondary">Spaced-repetition milestones</span>
          </div>
          <div class="metric-card">
            <span class="text-xs text-muted">Avg Quiz Score</span>
            <span class="metric-value">${avgQuiz}%</span>
            <span class="text-xs text-secondary">Active recall retention</span>
          </div>
        </div>

        <div class="grid-2">
          <!-- SVG Competency Radar (Ctrl 43) -->
          <div class="card stack-md">
            <h3>📊 Subject Competency Radar</h3>
            ${subjects.length === 0
              ? html`<div class="text-sm text-muted">Synthesize a course to visualize subject readiness.</div>`
              : this._renderRadarSvg(subjects)}
          </div>

          <!-- Subject Breakdown & Jump to Weakest Quiz (Ctrl 44) -->
          <div class="card stack-md">
            <div class="row-between">
              <h3>🎯 Subject Mastery &amp; Weak Spots</h3>
              ${weakestSubject
                ? html`
                    <button
                      type="button"
                      class="btn btn-primary btn-sm"
                      @click=${() => this._jumpToWeakestQuiz(weakestSubject.id)}
                    >
                      ⚡ Jump to Weakest Subject Quiz
                    </button>
                  `
                : nothing}
            </div>

            ${subjects.length === 0
              ? html`<div class="text-sm text-muted">No subjects tracked yet.</div>`
              : html`
                  <div class="stack-md">
                    ${subjects.map((subj) => {
                      const mastery = Math.round(Number(subj.progress?.mastery_score ?? 0));
                      const weakSpots = Array.isArray(subj.progress?.weak_spots)
                        ? subj.progress.weak_spots
                        : [];
                      return html`
                        <div class="stack-sm">
                          <div class="row-between">
                            <strong>${subj.title}</strong>
                            <span class="badge ${mastery >= 75 ? 'badge-emerald' : mastery >= 40 ? 'badge-blue' : 'badge-amber'}">
                              ${mastery}%
                            </span>
                          </div>
                          <div class="progress-track">
                            <div class="progress-fill" style="width: ${mastery}%;"></div>
                          </div>
                          ${weakSpots.length > 0
                            ? html`
                                <div class="row-wrap">
                                  ${weakSpots.map((ws) => html`<span class="badge badge-danger">${ws}</span>`)}
                                </div>
                              `
                            : nothing}
                        </div>
                      `;
                    })}
                  </div>
                `}
          </div>
        </div>
      </div>
    `;
  }
}

customElements.define('omnilearn-analytics', OmnilearnAnalyticsView);
