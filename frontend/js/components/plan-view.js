import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';

export class OmnilearnPlanView extends LitElement {
  static properties = {
    courseData: { type: Object },
    activeSubjectId: { type: String },
    targetDays: { type: Number },
    dailyHours: { type: Number },
    savingTaskId: { type: String, state: true },
    errorMessage: { type: String, state: true },
  };

  constructor() {
    super();
    this.courseData = { course: null, rois: [], subjects: [], sources: [] };
    this.activeSubjectId = '';
    this.targetDays = 14;
    this.dailyHours = 3.0;
    this.savingTaskId = '';
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

  async _toggleTask(subject, taskId) {
    this.errorMessage = '';
    const existing = Array.isArray(subject.progress?.completed_tasks)
      ? subject.progress.completed_tasks
      : [];
    const isCompleted = !existing.includes(taskId);
    const updatedTasks = isCompleted
      ? [...existing, taskId]
      : existing.filter((id) => id !== taskId);

    // Optimistic update on subject state
    subject.progress.completed_tasks = updatedTasks;
    this.savingTaskId = taskId;
    this.requestUpdate();

    try {
      await apiClient.saveTaskProgress({
        subject_id: subject.id,
        task_id: taskId,
        completed: isCompleted,
        completed_tasks: updatedTasks,
      });
      this.dispatchEvent(
        new CustomEvent('course-updated', {
          bubbles: true,
          composed: true,
          detail: { toastMsg: isCompleted ? 'Study task completed!' : 'Study task unchecked.' },
        })
      );
    } catch (err) {
      // Revert on failure
      subject.progress.completed_tasks = existing;
      this.errorMessage = err?.message || 'Could not save study task progress.';
      this.requestUpdate();
    } finally {
      this.savingTaskId = '';
    }
  }

  render() {
    const subjects = Array.isArray(this.courseData?.subjects) ? this.courseData.subjects : [];
    const subject = this._getCurrentSubject();

    if (!subject) {
      return html`<div class="card">No study plan available yet. Synthesize a course first.</div>`;
    }

    const schedule = Array.isArray(subject.study_plan?.schedule) ? subject.study_plan.schedule : [];
    const baseTargetDays = Math.max(1, Number(subject.study_plan?.target_days || 14));
    const scaleFactor = Math.max(1, Number(this.targetDays || 14)) / baseTargetDays;
    const completedSet = new Set(subject.progress?.completed_tasks || []);

    const allTaskIds = [];
    schedule.forEach((item, idx) => {
      if (Array.isArray(item.tasks) && item.tasks.length > 0) {
        item.tasks.forEach((t, tIdx) => {
          allTaskIds.push(t.id || `${item.id || `day-${idx + 1}`}-t${tIdx + 1}`);
        });
      } else {
        allTaskIds.push(item.id || `task-${idx + 1}`);
      }
    });
    const doneCount = allTaskIds.filter((tid) => completedSet.has(tid)).length;
    const totalTasks = allTaskIds.length;
    const pct = totalTasks > 0 ? Math.round((doneCount / totalTasks) * 100) : 0;

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
              <span class="badge badge-blue">📅 ${this.targetDays} Days Horizon</span>
              <span class="badge badge-emerald">⏱️ ${this.dailyHours} hrs/day</span>
            </div>
          </div>
        </div>

        ${this.errorMessage ? html`<div class="alert-error" role="alert">${this.errorMessage}</div>` : nothing}

        <div class="card stack-md">
          <div class="row-between">
            <div>
              <h2>📅 Spaced-Repetition Study Plan — ${subject.title}</h2>
              <div class="text-sm text-secondary">
                ${doneCount} of ${totalTasks} study tasks completed (${pct}%)
              </div>
            </div>
            <span class="badge badge-emerald">${pct}% Complete</span>
          </div>

          <div class="progress-track">
            <div class="progress-fill" style="width: ${pct}%;"></div>
          </div>

          ${schedule.length === 0
            ? html`<div class="text-sm text-muted">No scheduled tasks generated for this subject yet.</div>`
            : html`
                <div class="stack-md">
                  ${schedule.map((item, idx) => {
                    const rawDay = Number(item.day ?? idx + 1);
                    const pacedDay = Math.max(1, Math.min(this.targetDays, Math.round(rawDay * scaleFactor)));
                    const hours = item.hours || item.estimated_hours || item.duration_hours || Math.max(0.5, Number((this.dailyHours / 2).toFixed(1)));
                    const subTasks = Array.isArray(item.tasks) && item.tasks.length > 0 ? item.tasks : null;

                    if (subTasks) {
                      return html`
                        <div class="card stack-sm" style="background: var(--bg-page); padding: 14px;">
                          <div class="row-between">
                            <strong>${item.title || `Day ${pacedDay}: ${item.focus || 'Study Block'}`}</strong>
                            <div class="row-wrap">
                              <span class="badge badge-blue">Day ${pacedDay}</span>
                              <span class="badge">${hours}h</span>
                              ${item.phase ? html`<span class="badge badge-violet">${item.phase}</span>` : nothing}
                            </div>
                          </div>
                          <div class="stack-sm">
                            ${subTasks.map((t, tIdx) => {
                              const taskId = t.id || `${item.id || `day-${idx + 1}`}-t${tIdx + 1}`;
                              const checked = completedSet.has(taskId);
                              return html`
                                <label class="plan-task-row ${checked ? 'done' : ''}">
                                  <input
                                    type="checkbox"
                                    .checked=${checked}
                                    ?disabled=${this.savingTaskId === taskId}
                                    @change=${() => this._toggleTask(subject, taskId)}
                                  />
                                  <div style="flex: 1;">
                                    <div class="row-between">
                                      <span class="task-title">${t.text || t.title || 'Study Task'}</span>
                                      ${t.duration_min ? html`<span class="badge">${t.duration_min}m</span>` : nothing}
                                    </div>
                                  </div>
                                </label>
                              `;
                            })}
                          </div>
                        </div>
                      `;
                    }

                    const taskId = item.id || `task-${idx + 1}`;
                    const checked = completedSet.has(taskId);
                    return html`
                      <label class="plan-task-row ${checked ? 'done' : ''}">
                        <input
                          type="checkbox"
                          .checked=${checked}
                          ?disabled=${this.savingTaskId === taskId}
                          @change=${() => this._toggleTask(subject, taskId)}
                        />
                        <div style="flex: 1;">
                          <div class="row-between">
                            <strong class="task-title">${item.title || item.task || `Day ${pacedDay} Milestone`}</strong>
                            <div class="row-wrap">
                              <span class="badge badge-blue">Day ${pacedDay}</span>
                              <span class="badge">${hours}h</span>
                              ${item.priority ? html`<span class="badge badge-violet">${item.priority}</span>` : nothing}
                            </div>
                          </div>
                          ${item.description || item.focus
                            ? html`<div class="text-sm text-secondary" style="margin-top: 4px;">${item.description || item.focus}</div>`
                            : nothing}
                        </div>
                      </label>
                    `;
                  })}
                </div>
              `}
        </div>
      </div>
    `;
  }
}

customElements.define('omnilearn-plan', OmnilearnPlanView);
