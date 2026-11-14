import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';
import {
  WEB_LITE_LIMITS,
  isWebLite,
  promptSystemAppDownload,
} from '../edition-gate.js';
import { exportQuizAnkiCsv } from '../export-utils.js';

export class OmnilearnQuizView extends LitElement {
  static properties = {
    edition: { type: String },
    courseData: { type: Object },
    activeSubjectId: { type: String },
    selectedAnswers: { type: Object, state: true },
    submittedQuestions: { type: Object, state: true },
    saving: { type: Boolean, state: true },
    lastSavedScore: { type: Number, state: true },
    errorMessage: { type: String, state: true },
  };

  constructor() {
    super();
    this.edition = 'web_lite';
    this.courseData = { course: null, rois: [], subjects: [], sources: [] };
    this.activeSubjectId = '';
    this.selectedAnswers = {};
    this.submittedQuestions = {};
    this.saving = false;
    this.lastSavedScore = null;
    this.errorMessage = '';
    this._boundKeyDown = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._boundKeyDown = this._handleKeyDown.bind(this);
    window.addEventListener('keydown', this._boundKeyDown);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._boundKeyDown) {
      window.removeEventListener('keydown', this._boundKeyDown);
      this._boundKeyDown = null;
    }
  }

  _handleKeyDown(e) {
    const tag = (e.target?.tagName || '').toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target?.isContentEditable) {
      return;
    }

    const subject = this._getCurrentSubject();
    if (!subject) return;

    const allQuestions = Array.isArray(subject.quizzes) ? subject.quizzes : [];
    const webLite = isWebLite(this.edition);
    const visibleQuestions = webLite
      ? allQuestions.slice(0, WEB_LITE_LIMITS.maxQuizQuestions)
      : allQuestions;
    if (visibleQuestions.length === 0) return;

    // Find the current unanswered question
    const currentIdx = visibleQuestions.findIndex((_, idx) => !this.submittedQuestions[this._qKey(subject.id, idx)]);
    if (currentIdx === -1) return;

    const key = e.key.toUpperCase();
    const optMap = { '1': 0, '2': 1, '3': 2, '4': 3, 'A': 0, 'B': 1, 'C': 2, 'D': 3 };
    if (optMap[key] !== undefined) {
      const q = visibleQuestions[currentIdx];
      const opts = Array.isArray(q.options) ? q.options : [];
      if (optMap[key] < opts.length) {
        this._pickOption(subject.id, currentIdx, optMap[key]);
      }
    } else if (e.key === 'Enter') {
      const qKey = this._qKey(subject.id, currentIdx);
      if (this.selectedAnswers[qKey] !== undefined && !this.submittedQuestions[qKey]) {
        e.preventDefault();
        this._submitSingleQuestion(subject, currentIdx, visibleQuestions);
      }
    }
  }

  createRenderRoot() {
    return this;
  }

  _selectSubject(subjectId) {
    this.lastSavedScore = null;
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

  _qKey(subjectId, idx) {
    return `${subjectId}::${idx}`;
  }

  _resolveCorrectIndex(q) {
    if (typeof q.correct_index === 'number') {
      return q.correct_index;
    }
    const opts = Array.isArray(q.options) ? q.options : [];
    if (q.answer !== undefined) {
      const idx = opts.findIndex((o) => String(o).trim() === String(q.answer).trim());
      if (idx >= 0) return idx;
    }
    return 0;
  }

  _pickOption(subjectId, qIdx, optIdx) {
    const key = this._qKey(subjectId, qIdx);
    if (this.submittedQuestions[key]) return;
    this.selectedAnswers = { ...this.selectedAnswers, [key]: optIdx };
  }

  async _submitSingleQuestion(subject, qIdx, visibleQuestions) {
    const key = this._qKey(subject.id, qIdx);
    if (this.selectedAnswers[key] === undefined) {
      this.errorMessage = 'Select an option before submitting your answer.';
      return;
    }
    this.errorMessage = '';
    this.submittedQuestions = { ...this.submittedQuestions, [key]: true };

    // If all visible questions are now submitted, automatically persist quiz progress
    const allDone = visibleQuestions.every((_, idx) => this.submittedQuestions[this._qKey(subject.id, idx)]);
    if (allDone) {
      await this._saveQuizDeck(subject, visibleQuestions);
    }
  }

  async _saveQuizDeck(subject, visibleQuestions) {
    this.errorMessage = '';
    const nextSubmitted = { ...this.submittedQuestions };
    let correctCount = 0;
    const weakSpots = [];

    visibleQuestions.forEach((q, idx) => {
      const key = this._qKey(subject.id, idx);
      nextSubmitted[key] = true;
      const picked = this.selectedAnswers[key];
      const correctIdx = this._resolveCorrectIndex(q);
      if (picked === correctIdx) {
        correctCount += 1;
      } else if (q.topic || q.concept) {
        weakSpots.push(q.topic || q.concept);
      }
    });

    this.submittedQuestions = nextSubmitted;
    const total = Math.max(1, visibleQuestions.length);
    const pct = Math.round((correctCount / total) * 100);
    this.lastSavedScore = pct;
    this.saving = true;

    try {
      await apiClient.saveQuizProgress({
        subject_id: subject.id,
        score: pct,
        total,
        weak_spots: [...new Set(weakSpots)],
        answers: this.selectedAnswers,
      });
      this.dispatchEvent(
        new CustomEvent('course-updated', {
          bubbles: true,
          composed: true,
          detail: { toastMsg: `Quiz recorded: ${correctCount}/${total} (${pct}%)` },
        })
      );
    } catch (err) {
      this.errorMessage = err?.message || 'Could not persist quiz score.';
    } finally {
      this.saving = false;
    }
  }

  _resetDeck(subject, visibleQuestions) {
    const nextSel = { ...this.selectedAnswers };
    const nextSub = { ...this.submittedQuestions };
    visibleQuestions.forEach((_, idx) => {
      const key = this._qKey(subject.id, idx);
      delete nextSel[key];
      delete nextSub[key];
    });
    this.selectedAnswers = nextSel;
    this.submittedQuestions = nextSub;
    this.lastSavedScore = null;
  }

  render() {
    const subjects = Array.isArray(this.courseData?.subjects) ? this.courseData.subjects : [];
    const courseTitle = this.courseData?.course?.title || 'OmniLearn Course';
    const subject = this._getCurrentSubject();
    const webLite = isWebLite(this.edition);

    if (!subject) {
      return html`<div class="card">No active recall quizzes yet. Synthesize a course first.</div>`;
    }

    const allQuestions = Array.isArray(subject.quizzes) ? subject.quizzes : [];
    const visibleQuestions = webLite
      ? allQuestions.slice(0, WEB_LITE_LIMITS.maxQuizQuestions)
      : allQuestions;
    const lockedCount = Math.max(0, allQuestions.length - visibleQuestions.length);

    const answeredCount = visibleQuestions.filter((_, idx) => !!this.submittedQuestions[this._qKey(subject.id, idx)]).length;
    let correctSoFar = 0;
    visibleQuestions.forEach((q, idx) => {
      const qKey = this._qKey(subject.id, idx);
      if (this.submittedQuestions[qKey]) {
        const picked = this.selectedAnswers[qKey];
        const correctIdx = this._resolveCorrectIndex(q);
        if (picked === correctIdx) correctSoFar++;
      }
    });
    const progressPct = visibleQuestions.length > 0 ? Math.round((answeredCount / visibleQuestions.length) * 100) : 0;
    const accuracyPct = answeredCount > 0 ? Math.round((correctSoFar / answeredCount) * 100) : 0;

    return html`
      <div class="stack-md">
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

            <div class="row-wrap">
              <button
                type="button"
                class="btn btn-outline btn-sm"
                @click=${() => exportQuizAnkiCsv(subject, courseTitle, this.edition)}
              >
                📥 Export Anki (.CSV) ${webLite ? '🔒' : ''}
              </button>
            </div>
          </div>
        </div>

        <div class="card quiz-progress-card">
          <div class="row-between">
            <div class="row-wrap" style="gap: 8px; align-items: center;">
              <span class="badge badge-blue">Progress: ${answeredCount} / ${visibleQuestions.length} Answered</span>
              ${answeredCount > 0 ? html`<span class="badge ${accuracyPct >= 70 ? 'badge-emerald' : 'badge-amber'}">🎯 ${accuracyPct}% Accuracy (${correctSoFar}/${answeredCount})</span>` : nothing}
            </div>
            <div class="row-wrap" style="gap: 6px; align-items: center;">
              <span class="text-muted" style="font-size: 0.78rem;">Keys:</span>
              <kbd class="kbd-hint">1-4</kbd>
              <kbd class="kbd-hint">Enter</kbd>
            </div>
          </div>
          <div class="quiz-progress-track">
            <div class="quiz-progress-fill" style="width: ${progressPct}%;"></div>
          </div>
        </div>

        ${this.errorMessage ? html`<div class="alert-error" role="alert">${this.errorMessage}</div>` : nothing}

        ${this.lastSavedScore !== null
          ? html`
              <div class="alert-info">
                🎯 Latest Quiz Score for <strong>${subject.title}</strong>:
                <strong>${this.lastSavedScore}%</strong>
              </div>
            `
          : nothing}

        ${visibleQuestions.length === 0
          ? html`<div class="card text-muted">No MCQ questions available for this subject yet.</div>`
          : html`
              <div class="stack-md">
                ${visibleQuestions.map((q, idx) => {
                  const key = this._qKey(subject.id, idx);
                  const picked = this.selectedAnswers[key];
                  const isSubmitted = !!this.submittedQuestions[key];
                  const correctIdx = this._resolveCorrectIndex(q);
                  const options = Array.isArray(q.options) ? q.options : [];

                  return html`
                    <div class="card stack-sm">
                      <div class="row-between">
                        <div class="row-wrap" style="gap: 8px; align-items: center;">
                          <span class="badge badge-blue">Question ${idx + 1}</span>
                          ${q.concept || q.topic ? html`<span class="concept-pill">💡 ${q.concept || q.topic}</span>` : nothing}
                        </div>
                        ${q.difficulty ? html`<span class="badge">${q.difficulty}</span>` : nothing}
                      </div>

                      <h3 style="margin: 4px 0 8px;">${q.question}</h3>

                      <div class="stack-sm">
                        ${options.map((opt, optIdx) => {
                          let stateClass = '';
                          if (isSubmitted) {
                            if (optIdx === correctIdx) {
                              stateClass = 'correct';
                            } else if (picked === optIdx) {
                              stateClass = 'incorrect';
                            }
                          } else if (picked === optIdx) {
                            stateClass = 'selected';
                          }

                          return html`
                            <button
                              type="button"
                              class="mcq-option ${stateClass}"
                              ?disabled=${isSubmitted}
                              @click=${() => this._pickOption(subject.id, idx, optIdx)}
                            >
                              <span class="badge">${String.fromCharCode(65 + optIdx)}</span>
                              <span style="flex: 1;">${opt}</span>
                              <kbd class="kbd-hint">${optIdx + 1}</kbd>
                            </button>
                          `;
                        })}
                      </div>

                      <div class="row-between" style="margin-top: 6px;">
                        ${!isSubmitted
                          ? html`
                              <button
                                type="button"
                                class="btn btn-primary btn-sm"
                                @click=${() => this._submitSingleQuestion(subject, idx, visibleQuestions)}
                              >
                                Submit Answer <kbd class="kbd-hint" style="margin-left: 6px; background: rgba(255,255,255,0.25); color: #fff; border-color: rgba(255,255,255,0.4);">Enter</kbd>
                              </button>
                            `
                          : html`
                              <span class="badge ${picked === correctIdx ? 'badge-emerald' : 'badge-danger'}">
                                ${picked === correctIdx ? '✅ Correct' : '❌ Incorrect'}
                              </span>
                            `}
                      </div>

                      ${isSubmitted && q.explanation
                        ? html`
                            <div class="alert-info" style="margin-top: 6px;">
                              <strong>Explanation:</strong> ${q.explanation}
                            </div>
                          `
                        : nothing}
                    </div>
                  `;
                })}

                <div class="row-between">
                  <button
                    type="button"
                    class="btn btn-outline"
                    @click=${() => this._resetDeck(subject, visibleQuestions)}
                  >
                    🔄 Reset Deck
                  </button>
                  <button
                    type="button"
                    class="btn btn-emerald"
                    ?disabled=${this.saving}
                    @click=${() => this._saveQuizDeck(subject, visibleQuestions)}
                  >
                    ${this.saving ? 'Saving Score...' : '✅ Submit & Record Quiz Score'}
                  </button>
                </div>
              </div>
            `}

        ${lockedCount > 0
          ? html`
              <div class="locked-card">
                <div class="locked-overlay">
                  <span class="lock-pill">🔒 +${lockedCount} More Quiz Questions in System App</span>
                  <button
                    type="button"
                    class="btn btn-amber btn-sm"
                    @click=${() => promptSystemAppDownload('full_quiz', 'Web Lite previews 3 sample MCQs per subject. Download the System App for full question banks & Anki export.')}
                  >
                    Unlock Full Quiz Deck
                  </button>
                </div>
              </div>
            `
          : nothing}
      </div>
    `;
  }
}

customElements.define('omnilearn-quiz', OmnilearnQuizView);
