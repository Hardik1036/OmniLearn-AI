import { LitElement, html, nothing } from 'lit';
import { apiClient } from '../api-client.js';

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function safeRenderMarkdown(markdownText) {
  if (!markdownText) return '';
  if (typeof window !== 'undefined' && window.marked && typeof window.marked.parse === 'function') {
    try {
      return window.marked.parse(markdownText);
    } catch (_err) {
      // Fallback below
    }
  }

  // Safe fallback renderer
  let html = escapeHtml(markdownText);
  // Bold **text**
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  // Italic *text*
  html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
  // Inline code `code`
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bullet lists
  html = html.replace(/^\s*-\s+(.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
  // Numbered lists
  html = html.replace(/^\s*([0-9]+)\.\s+(.+)$/gm, '<li>$2</li>');
  // Line breaks
  html = html.replace(/\n\n/g, '<p></p>');
  html = html.replace(/\n/g, '<br/>');
  return html;
}

export class OmnilearnTutorChat extends LitElement {
  static properties = {
    courseData: { type: Object },
    activeSubjectId: { type: String },
    isOpen: { type: Boolean, state: true },
    messages: { type: Array, state: true },
    inputText: { type: String, state: true },
    isThinking: { type: Boolean, state: true },
    errorMessage: { type: String, state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.courseData = null;
    this.activeSubjectId = '';
    this.isOpen = false;
    this.inputText = '';
    this.isThinking = false;
    this.errorMessage = '';
    this.messages = [
      {
        role: 'assistant',
        content: `👋 **Welcome to OmniLearn AI Tutor!**\n\nI am your 24/7 academic tutor and study assistant. Ask me anything:\n* **Clear Doubts:** Ask me to explain difficult concepts, break down formulas, or test you with practice questions.\n* **App Help:** Ask how to upload & slice videos, download notes as PDF, configure API keys, or customize study plans.\n\n*Choose a suggested topic below or type your question:*`,
        time: this._formatCurrentTime(),
      },
    ];
  }

  updated(changedProps) {
    super.updated(changedProps);
    if (this.isOpen) {
      this._scrollToBottom();
      this._renderMathInBubbles();
    }
  }

  _formatCurrentTime() {
    const d = new Date();
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  _scrollToBottom() {
    setTimeout(() => {
      const container = this.querySelector('.tutor-messages');
      if (container) {
        container.scrollTop = container.scrollHeight;
      }
    }, 50);
  }

  _renderMathInBubbles() {
    if (typeof window !== 'undefined' && window.renderMathInElement) {
      setTimeout(() => {
        const msgs = this.querySelectorAll('.tutor-msg-ai');
        msgs.forEach((el) => {
          try {
            window.renderMathInElement(el, {
              delimiters: [
                { left: '$$', right: '$$', display: true },
                { left: '$', right: '$', display: false },
                { left: '\\[', right: '\\]', display: true },
                { left: '\\(', right: '\\)', display: false },
              ],
              throwOnError: false,
            });
          } catch (_e) {
            // KaTeX render fallback
          }
        });
      }, 100);
    }
  }

  _toggleOpen() {
    this.isOpen = !this.isOpen;
    if (this.isOpen) {
      setTimeout(() => {
        const input = this.querySelector('.tutor-input-field');
        if (input) input.focus();
      }, 100);
    }
  }

  _clearChat() {
    this.messages = [
      {
        role: 'assistant',
        content: `Chat history cleared. How can I help you with your course or OmniLearn today?`,
        time: this._formatCurrentTime(),
      },
    ];
    this.errorMessage = '';
  }

  _handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      this._sendMessage();
    }
  }

  _switchTab(tabId) {
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('omnilearn:switch-tab', {
          detail: { tabId },
          bubbles: true,
          composed: true,
        })
      );
    }
  }

  async _sendQuickPrompt(text) {
    this.inputText = text;
    await this._sendMessage();
  }

  async _sendMessage() {
    const text = this.inputText.trim();
    if (!text || this.isThinking) return;

    this.inputText = '';
    this.errorMessage = '';

    const userMsg = {
      role: 'user',
      content: text,
      time: this._formatCurrentTime(),
    };

    this.messages = [...this.messages, userMsg];
    this.isThinking = true;

    const courseId = this.courseData?.course?.id || '';
    const subjectId = this.activeSubjectId || '';

    // Build chat history for backend (excluding initial greeting)
    const historyPayload = this.messages.slice(1).map((m) => ({
      role: m.role,
      content: m.content,
    }));

    try {
      const res = await apiClient.chatWithTutor({
        message: text,
        course_id: courseId,
        subject_id: subjectId,
        history: historyPayload,
      });

      const replyText = res?.reply || 'I am here to help you study! What concept would you like to explore next?';
      this.messages = [
        ...this.messages,
        {
          role: 'assistant',
          content: replyText,
          time: this._formatCurrentTime(),
        },
      ];
    } catch (err) {
      this.errorMessage = err?.message || 'AI Tutor is temporarily unavailable.';
      this.messages = [
        ...this.messages,
        {
          role: 'assistant',
          content: `⚠️ **Connection Issue:** ${this.errorMessage}\n\nPlease check your internet connection or verify your API keys in Settings.`,
          time: this._formatCurrentTime(),
        },
      ];
    } finally {
      this.isThinking = false;
    }
  }

  _detectNavigationIntent(content) {
    const lower = content.toLowerCase();
    const actions = [];
    if (lower.includes('sources tab') || lower.includes('upload') || lower.includes('slice video')) {
      actions.push({ label: '📂 Open Sources Tab', tab: 'sources' });
    }
    if (lower.includes('notes tab') || lower.includes('pdf') || lower.includes('download notes')) {
      actions.push({ label: '📝 Open Notes Tab', tab: 'notes' });
    }
    if (lower.includes('quiz tab') || lower.includes('practice question') || lower.includes('test your')) {
      actions.push({ label: '❓ Open Quiz Tab', tab: 'quiz' });
    }
    if (lower.includes('exam tab') || lower.includes('mock exam')) {
      actions.push({ label: '🎓 Open Exam Tab', tab: 'exam' });
    }
    if (lower.includes('study plan tab') || lower.includes('adaptive plan')) {
      actions.push({ label: '📅 Open Study Plan', tab: 'plan' });
    }
    return actions;
  }

  render() {
    const courseTitle = this.courseData?.course?.title;
    const currentSubject = (this.courseData?.subjects || []).find((s) => s.id === this.activeSubjectId);
    const subjectTitle = currentSubject?.title;
    const contextLabel = subjectTitle
      ? `${courseTitle ? courseTitle + ' • ' : ''}${subjectTitle}`
      : courseTitle || 'General Tutor';

    return html`
      <!-- Floating Action Button (FAB) -->
      <button
        type="button"
        class="tutor-fab"
        aria-label="Open AI Tutor Chatbot"
        @click=${this._toggleOpen}
        title="Ask AI Tutor: Doubts & App Help"
      >
        <span>${this.isOpen ? '✕' : '✨'}</span>
        <span>${this.isOpen ? 'Close Tutor' : 'AI Tutor &amp; Guide'}</span>
        ${!this.isOpen
          ? html`<span class="tutor-fab-badge" aria-hidden="true">💬</span>`
          : nothing}
      </button>

      <!-- Chat Drawer / Window -->
      ${this.isOpen
        ? html`
            <div class="tutor-window">
              <!-- Header -->
              <div class="tutor-header">
                <div class="stack-xs" style="gap: 2px;">
                  <div class="tutor-title">
                    <span>✨</span>
                    <span>AI Study Tutor &amp; Assistant</span>
                  </div>
                  <div class="tutor-context-chip" title=${contextLabel}>
                    📚 ${contextLabel}
                  </div>
                </div>
                <div class="row-wrap" style="gap: 6px;">
                  <button
                    type="button"
                    class="tutor-header-btn"
                    @click=${this._clearChat}
                    title="Clear Chat Conversation"
                    aria-label="Clear Chat"
                  >
                    🧹
                  </button>
                  <button
                    type="button"
                    class="tutor-header-btn"
                    @click=${this._toggleOpen}
                    title="Close Tutor Window"
                    aria-label="Close Tutor"
                  >
                    ✕
                  </button>
                </div>
              </div>

              <!-- Message History -->
              <div class="tutor-messages">
                ${this.messages.map((msg, idx) => {
                  const isUser = msg.role === 'user';
                  const navActions = !isUser ? this._detectNavigationIntent(msg.content) : [];
                  return html`
                    <div class="tutor-msg ${isUser ? 'tutor-msg-user' : 'tutor-msg-ai'}">
                      <div class="tutor-msg-body">
                        ${isUser
                          ? msg.content
                          : html`<div .innerHTML=${safeRenderMarkdown(msg.content)}></div>`}
                      </div>

                      ${navActions.length > 0
                        ? html`
                            <div class="row-wrap" style="gap: 6px; margin-top: 8px;">
                              ${navActions.map(
                                (act) => html`
                                  <button
                                    type="button"
                                    class="tutor-action-btn"
                                    @click=${() => this._switchTab(act.tab)}
                                  >
                                    ${act.label}
                                  </button>
                                `
                              )}
                            </div>
                          `
                        : nothing}

                      <!-- Initial Quick Prompt Chips on first assistant greeting -->
                      ${idx === 0
                        ? html`
                            <div class="tutor-quick-chips">
                              <button
                                type="button"
                                class="tutor-chip"
                                @click=${() =>
                                  this._sendQuickPrompt(
                                    `Explain the core concepts and fundamental principles in this course.`
                                  )}
                              >
                                💡 Explain Key Concepts
                              </button>
                              <button
                                type="button"
                                class="tutor-chip"
                                @click=${() =>
                                  this._sendQuickPrompt(
                                    `Give me an exam-style practice quiz question on this topic with explanation.`
                                  )}
                              >
                                ❓ Practice Question
                              </button>
                              <button
                                type="button"
                                class="tutor-chip"
                                @click=${() =>
                                  this._sendQuickPrompt(
                                    `How do I upload and slice multi-window lecture videos into slide keyframes?`
                                  )}
                              >
                                📹 Video Lecture Slicing
                              </button>
                              <button
                                type="button"
                                class="tutor-chip"
                                @click=${() =>
                                  this._sendQuickPrompt(
                                    `How do I download my unified study notes and formulas as a PDF?`
                                  )}
                              >
                                📥 PDF Notes Download
                              </button>
                              <button
                                type="button"
                                class="tutor-chip"
                                @click=${() =>
                                  this._sendQuickPrompt(
                                    `How do I add my own Gemini, Groq, or OpenRouter API key?`
                                  )}
                              >
                                ⚙️ Configure API Keys
                              </button>
                            </div>
                          `
                        : nothing}

                      <span
                        style="font-size: 10px; margin-top: 4px; opacity: 0.65; align-self: ${isUser
                          ? 'flex-end'
                          : 'flex-start'};"
                      >
                        ${msg.time || ''}
                      </span>
                    </div>
                  `;
                })}

                <!-- Thinking Indicator -->
                ${this.isThinking
                  ? html`
                      <div class="tutor-msg tutor-msg-ai">
                        <div class="tutor-dots">
                          <span class="tutor-dot"></span>
                          <span class="tutor-dot"></span>
                          <span class="tutor-dot"></span>
                        </div>
                      </div>
                    `
                  : nothing}
              </div>

              <!-- Input Area -->
              <div class="tutor-input-area">
                <div class="tutor-input-box">
                  <input
                    type="text"
                    class="tutor-input-field"
                    placeholder="Ask a doubt or how to use a feature..."
                    .value=${this.inputText}
                    @input=${(e) => {
                      this.inputText = e.target.value;
                    }}
                    @keydown=${this._handleKeyDown}
                    ?disabled=${this.isThinking}
                  />
                  <button
                    type="button"
                    class="tutor-send-btn"
                    @click=${this._sendMessage}
                    ?disabled=${!this.inputText.trim() || this.isThinking}
                    title="Send message (Enter)"
                    aria-label="Send message"
                  >
                    ➤
                  </button>
                </div>
                <div style="font-size: 10px; color: var(--text-muted); margin-top: 4px; text-align: center;">
                  AI Tutor is grounded in your active course notes &amp; OmniLearn features.
                </div>
              </div>
            </div>
          `
        : nothing}
    `;
  }
}

customElements.define('omnilearn-tutor-chat', OmnilearnTutorChat);
