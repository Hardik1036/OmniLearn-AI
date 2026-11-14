import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';
import {
  WEB_LITE_LIMITS,
  isWebLite,
  promptSystemAppDownload,
  requireCapability,
} from '../edition-gate.js';
import { exportExamPaperTxt } from '../export-utils.js';

export class OmnilearnExamView extends LitElement {
  static properties = {
    edition: { type: String },
    courseData: { type: Object },
    activeSubjectId: { type: String },
    answers: { type: Object, state: true },
    revealedSolutions: { type: Object, state: true },
    gradingMap: { type: Object, state: true },
    gradeResults: { type: Object, state: true },
    errorMessage: { type: String, state: true },
  };

  constructor() {
    super();
    this.edition = 'web_lite';
    this.courseData = { course: null, rois: [], subjects: [], sources: [] };
    this.activeSubjectId = '';
    this.answers = {};
    this.revealedSolutions = {};
    this.gradingMap = {};
    this.gradeResults = {};
    this.errorMessage = '';
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

  _qKey(subjectId, qId) {
    return `${subjectId}::${qId}`;
  }

  async _gradeAnswer(subject, q, qKey) {
    this.errorMessage = '';
    if (!requireCapability(this.edition, 'ai_grader')) {
      return;
    }

    const studentAnswer = String(this.answers[qKey] || '').trim();
    if (!studentAnswer) {
      this.errorMessage = 'Write your practice answer before requesting AI Examiner evaluation.';
      return;
    }

    this.gradingMap = { ...this.gradingMap, [qKey]: true };
    const rubricStr = Array.isArray(q.rubric) ? q.rubric.join('; ') : String(q.rubric || '');
    try {
      const res = await apiClient.gradeExamAnswer({
        course_id: this.courseData?.course?.id || '',
        subject_id: subject.id,
        question_id: q.id || qKey,
        question_text: q.question || q.prompt || '',
        max_marks: Number(q.marks || 5),
        rubric: rubricStr,
        model_answer: q.model_answer || q.solution || '',
        student_answer: studentAnswer,
      });
      this.gradeResults = { ...this.gradeResults, [qKey]: res };
    } catch (err) {
      this.errorMessage = err?.message || 'Could not evaluate answer.';
    } finally {
      this.gradingMap = { ...this.gradingMap, [qKey]: false };
    }
  }

  render() {
    const subjects = Array.isArray(this.courseData?.subjects) ? this.courseData.subjects : [];
    const courseTitle = this.courseData?.course?.title || 'OmniLearn Course';
    const subject = this._getCurrentSubject();
    const webLite = isWebLite(this.edition);

    if (!subject) {
      return html`<div class="card">No model exam papers yet. Synthesize a course first.</div>`;
    }

    const paper = subject.model_paper || {};
    const allQuestions = Array.isArray(paper.questions) ? paper.questions : [];
    const visibleQuestions = webLite
      ? allQuestions.slice(0, WEB_LITE_LIMITS.maxExamQuestions)
      : allQuestions;
    const lockedQuestionsCount = Math.max(0, allQuestions.length - visibleQuestions.length);

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

            <button
              type="button"
              class="btn btn-outline btn-sm"
              @click=${() => exportExamPaperTxt(subject, courseTitle, this.edition)}
            >
              📥 Download Paper (.TXT) ${webLite ? '🔒' : ''}
            </button>
          </div>
        </div>

        <div class="card">
          <div class="row-between">
            <div>
              <h2>📑 ${paper.title || `${subject.title} Model Exam Paper`}</h2>
              <div class="text-sm text-secondary">
                Time Allowed: ${paper.time_allowed || '60 Minutes'} • Max Marks: ${paper.max_marks || 25}
              </div>
            </div>
            <span class="badge badge-violet">AI Exam Tutor</span>
          </div>
        </div>

        ${this.errorMessage ? html`<div class="alert-error" role="alert">${this.errorMessage}</div>` : nothing}

        ${visibleQuestions.length === 0
          ? html`<div class="card text-muted">No exam questions generated for this subject yet.</div>`
          : visibleQuestions.map((q, idx) => {
              const qId = q.id || `exam-q-${idx + 1}`;
              const qKey = this._qKey(subject.id, qId);
              const isRevealed = !!this.revealedSolutions[qKey];
              const isGrading = !!this.gradingMap[qKey];
              const grade = this.gradeResults[qKey];

              return html`
                <div class="card stack-md">
                  <div class="row-between">
                    <div class="row-wrap">
                      <span class="badge badge-navy">${q.section || `Question ${idx + 1}`}</span>
                      <span class="badge badge-violet">${q.marks || 5} Marks</span>
                    </div>
                  </div>

                  <h3 style="margin: 0;">Q${idx + 1}. ${q.question || q.prompt}</h3>

                  <div class="field">
                    <label class="field-label" for=${`ans-${qKey}`}>Practice Answer</label>
                    <textarea
                      id=${`ans-${qKey}`}
                      class="textarea"
                      placeholder="Draft your structured exam answer here..."
                      .value=${this.answers[qKey] || ''}
                      @input=${(e) => {
                        this.answers = { ...this.answers, [qKey]: e.target.value };
                      }}
                    ></textarea>
                  </div>

                  <div class="row-wrap">
                    <button
                      type="button"
                      class="btn btn-violet"
                      ?disabled=${isGrading}
                      @click=${() => this._gradeAnswer(subject, q, qKey)}
                    >
                      ${isGrading
                        ? '⏳ Grading Answer...'
                        : `🤖 Evaluate & Grade Answer ${webLite ? '🔒' : ''}`}
                    </button>

                    <button
                      type="button"
                      class="btn btn-outline"
                      @click=${() => {
                        this.revealedSolutions = {
                          ...this.revealedSolutions,
                          [qKey]: !isRevealed,
                        };
                      }}
                    >
                      ${isRevealed ? '🙈 Hide Model Solution & Rubric' : '🔍 Reveal Model Solution & Rubric'}
                    </button>
                  </div>

                  ${grade
                    ? html`
                        <div class="rubric-callout stack-sm">
                          <div class="row-between">
                            <strong>Examiner Score</strong>
                            <span class="badge badge-violet">
                              ${grade.score ?? 0} / ${grade.max_marks ?? q.marks ?? 5} Marks
                            </span>
                          </div>
                          ${grade.feedback ? html`<p style="margin: 0;">${grade.feedback}</p>` : nothing}
                          ${Array.isArray(grade.strengths) && grade.strengths.length > 0
                            ? html`<div class="text-sm"><strong>Strengths:</strong> ${grade.strengths.join(' • ')}</div>`
                            : nothing}
                          ${Array.isArray(grade.improvements) && grade.improvements.length > 0
                            ? html`<div class="text-sm"><strong>To Improve:</strong> ${grade.improvements.join(' • ')}</div>`
                            : nothing}
                        </div>
                      `
                    : nothing}

                  ${isRevealed
                    ? html`
                        <div class="alert-info stack-sm">
                          <div>
                            <strong>Model Solution:</strong>
                            <div style="margin-top: 4px; white-space: pre-wrap;">
                              ${q.model_answer || q.solution || 'Refer to key concepts in Visual Notes.'}
                            </div>
                          </div>
                          ${q.rubric
                            ? html`
                                <div>
                                  <strong>Marking Rubric:</strong>
                                  ${Array.isArray(q.rubric)
                                    ? html`
                                        <ul style="margin: 4px 0 0 18px; padding: 0;">
                                          ${q.rubric.map((r) => html`<li>${r}</li>`)}
                                        </ul>
                                      `
                                    : html` <span>${q.rubric}</span>`}
                                </div>
                              `
                            : nothing}
                        </div>
                      `
                    : nothing}
                </div>
              `;
            })}

        ${lockedQuestionsCount > 0
          ? html`
              <div class="locked-card">
                <div class="locked-overlay">
                  <span class="lock-pill">🔒 +${lockedQuestionsCount} More Model Exam Questions &amp; AI Grader in Desktop App</span>
                  <button
                    type="button"
                    class="btn btn-amber btn-sm"
                    @click=${() => promptSystemAppDownload('full_exam', 'Cloud Preview displays 1 sample exam question. Get the Desktop App for complete 3-section University Papers and AI Rubric Grading.')}
                  >
                    Unlock Full Exam Paper
                  </button>
                </div>
              </div>
            `
          : nothing}
      </div>
    `;
  }
}

customElements.define('omnilearn-exam', OmnilearnExamView);
