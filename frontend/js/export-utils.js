/* OmniLearn AI — Client-Side Exporters (PDF with Visuals, Markdown, Anki CSV, Exam TXT) */

import { requireCapability } from './edition-gate.js';
import { apiClient } from './api-client.js';

/**
 * Neutralizes CSV/spreadsheet formula injection (=, +, -, @, tab, CR)
 * and formats according to RFC 4180.
 */
export function sanitizeCsvCell(value) {
  const raw = String(value ?? '');
  const trimmedLeft = raw.replace(/^ +/, '');
  const startsDangerous = /^[=+\-@\t\r]/.test(raw) || /^[=+\-@\t\r]/.test(trimmedLeft);
  const safeValue = startsDangerous ? `'${raw}` : raw;
  const escapedQuotes = safeValue.replace(/"/g, '""');
  return `"${escapedQuotes}"`;
}

export function escapeCsvRow(fields) {
  return fields.map((cell) => sanitizeCsvCell(cell)).join(',');
}

export function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

export function slugifyFilename(name) {
  return String(name || 'omnilearn')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'omnilearn';
}

/**
 * Builds clean, publication-grade academic Markdown notes.
 */
export function buildNotesMarkdown(subject, courseTitle = 'OmniLearn Course') {
  const lines = [];
  lines.push(`# ${subject.title || 'Subject Notes'}`);
  lines.push(`**Course:** ${courseTitle}`);
  if (subject.unit || subject.topic) {
    lines.push(`**Unit / Topic:** ${[subject.unit, subject.topic].filter(Boolean).join(' — ')}`);
  }
  if (subject.instructor) {
    lines.push(`**Instructor / Author:** ${subject.instructor}`);
  }
  lines.push('');

  if (subject.summary) {
    lines.push('## Executive Summary');
    lines.push(subject.summary);
    lines.push('');
  }

  if (Array.isArray(subject.source_citations) && subject.source_citations.length > 0) {
    lines.push('## Source Citations');
    for (const cite of subject.source_citations) {
      let label = '';
      if (typeof cite === 'string') {
        label = cite;
      } else if (cite && typeof cite === 'object') {
        const main = cite.label || cite.title || cite.source || '';
        label = cite.location ? `${main} (${cite.location})` : main || JSON.stringify(cite);
      }
      if (label) lines.push(`- ${label}`);
    }
    lines.push('');
  }

  if (subject.mermaid_diagram) {
    lines.push('## Concept Architecture Diagram');
    lines.push('```mermaid');
    lines.push(subject.mermaid_diagram);
    lines.push('```');
    lines.push('');
  }

  if (Array.isArray(subject.key_concepts) && subject.key_concepts.length > 0) {
    lines.push('## Key Concepts & Mechanisms');
    for (const concept of subject.key_concepts) {
      if (typeof concept === 'string') {
        lines.push(`- ${concept}`);
      } else {
        const title = concept.title || concept.name || 'Concept';
        const desc = concept.explanation || concept.description || concept.detail || '';
        lines.push(`### ${title}`);
        if (desc) lines.push(desc);
        if (concept.exam_tip) lines.push(`> **Exam Tip:** ${concept.exam_tip}`);
        if (concept.slide_caption) lines.push(`*Visual Reference: ${concept.slide_caption}*`);
        if (concept.citation) lines.push(`> Source: ${concept.citation}`);
        lines.push('');
      }
    }
  }

  if (Array.isArray(subject.math_formulas) && subject.math_formulas.length > 0) {
    lines.push('## Key Mathematical Formulations');
    for (const item of subject.math_formulas) {
      if (typeof item === 'string') {
        lines.push(`$$\n${item}\n$$`);
      } else {
        if (item.label || item.name) lines.push(`### ${item.label || item.name}`);
        if (item.latex || item.formula) lines.push(`$$\n${item.latex || item.formula}\n$$`);
        const noteText = item.explanation || item.note || '';
        if (noteText) lines.push(noteText);
      }
      lines.push('');
    }
  }

  return lines.join('\n');
}

/**
 * Builds a publication-grade academic printable HTML document complete with
 * vector diagrams, slide visual figures, mathematical equations, and structured cards.
 */
export function buildNotesPrintableHtml(subject, courseTitle = 'OmniLearn Course', options = {}) {
  const title = escapeHtml(subject.title || 'Subject Notes');
  const course = escapeHtml(courseTitle || 'OmniLearn Course');
  const topic = escapeHtml([subject.unit, subject.topic].filter(Boolean).join(' — ') || 'Exam Study Pack');
  const instructor = escapeHtml(subject.instructor || '');
  const summary = escapeHtml(subject.summary || '');
  const mermaidSvg = options.renderedMermaidSvg || '';
  const renderedFormulas = Array.isArray(options.renderedFormulas) ? options.renderedFormulas : [];

  const concepts = Array.isArray(subject.key_concepts) ? subject.key_concepts : [];
  const formulas = Array.isArray(subject.math_formulas) ? subject.math_formulas : [];
  const slides = Array.isArray(subject.slide_crops) ? subject.slide_crops : [];
  const citations = Array.isArray(subject.source_citations) ? subject.source_citations : [];
  const quizzes = Array.isArray(subject.quiz_questions) ? subject.quiz_questions : (Array.isArray(subject.quizzes) ? subject.quizzes : []);
  const examQuestions = Array.isArray(subject.exam_questions) ? subject.exam_questions : [];

  const now = new Date().toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>${title} — ${course} Study Pack</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" crossorigin="anonymous" />
  <style>
    @page {
      size: A4 portrait;
      margin: 14mm 16mm 16mm 16mm;
    }
    *, *::before, *::after {
      box-sizing: border-box;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: #0F172A;
      background: #FFFFFF;
      line-height: 1.6;
      font-size: 10pt;
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
    }
    .pdf-container {
      max-width: 100%;
      margin: 0 auto;
      padding: 10px;
    }
    .pdf-header {
      border-bottom: 2px solid #2563EB;
      padding-bottom: 12px;
      margin-bottom: 20px;
    }
    .pdf-brand {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 8.5pt;
      font-weight: 700;
      color: #2563EB;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 6px;
    }
    .pdf-badge-tag {
      background: #EFF6FF;
      color: #1D4ED8;
      border: 1px solid #BFDBFE;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 8pt;
      font-weight: 600;
      text-transform: none;
    }
    .pdf-title {
      font-size: 20pt;
      font-weight: 800;
      color: #0F172A;
      margin: 0 0 6px 0;
      line-height: 1.25;
    }
    .pdf-meta {
      font-size: 9.5pt;
      color: #475569;
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-top: 4px;
    }
    .pdf-meta span {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .pdf-section {
      margin-bottom: 22px;
      break-inside: auto;
    }
    .pdf-section-title {
      font-size: 13pt;
      font-weight: 700;
      color: #1E3A8A;
      border-bottom: 1.5px solid #E2E8F0;
      padding-bottom: 4px;
      margin: 0 0 12px 0;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .pdf-card {
      background: #F8FAFC;
      border: 1px solid #E2E8F0;
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 12px;
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .pdf-summary-box {
      background: #F0F7FF;
      border: 1px solid #BFDBFE;
      border-left: 5px solid #2563EB;
      border-radius: 6px;
      padding: 14px 18px;
      margin-bottom: 16px;
      font-size: 10.5pt;
      color: #1E293B;
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .pdf-summary-box p {
      margin: 0 0 8px 0;
    }
    .pdf-summary-box p:last-child {
      margin-bottom: 0;
    }
    .pdf-diagram-container {
      background: #FFFFFF;
      border: 1px solid #E2E8F0;
      border-radius: 8px;
      padding: 16px;
      text-align: center;
      overflow: hidden;
      break-inside: avoid;
      page-break-inside: avoid;
      margin-bottom: 16px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }
    .pdf-diagram-container svg {
      max-width: 100% !important;
      height: auto !important;
      display: block;
      margin: 0 auto;
    }
    .pdf-diagram-caption {
      font-size: 8.5pt;
      color: #64748B;
      margin-top: 8px;
      font-style: italic;
    }
    .pdf-concept-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    @media print {
      .pdf-concept-grid {
        display: block;
      }
      .pdf-concept-card {
        margin-bottom: 12px;
      }
    }
    .pdf-concept-card {
      background: #FFFFFF;
      border: 1px solid #E2E8F0;
      border-radius: 8px;
      padding: 12px 14px;
      break-inside: avoid;
      page-break-inside: avoid;
      box-shadow: 0 1px 2px rgba(0,0,0,0.02);
    }
    .pdf-concept-title {
      font-size: 11pt;
      font-weight: 700;
      color: #0F172A;
      margin-bottom: 4px;
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 8px;
    }
    .pdf-concept-desc {
      font-size: 9.5pt;
      color: #334155;
      margin-bottom: 6px;
    }
    .pdf-exam-tip {
      background: #FEF3C7;
      border-left: 3px solid #D97706;
      padding: 5px 10px;
      border-radius: 4px;
      font-size: 8.5pt;
      color: #92400E;
      margin-top: 6px;
      font-weight: 500;
    }
    .pdf-figure {
      margin: 10px 0 6px 0;
      text-align: center;
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .pdf-figure img {
      max-width: 100%;
      max-height: 220px;
      border-radius: 6px;
      border: 1px solid #CBD5E1;
      object-fit: contain;
    }
    .pdf-figure figcaption {
      font-size: 8.5pt;
      color: #64748B;
      margin-top: 4px;
      font-style: italic;
    }
    .pdf-formula-card {
      background: #F8FAFC;
      border: 1px solid #E2E8F0;
      border-left: 4px solid #10B981;
      border-radius: 6px;
      padding: 10px 14px;
      margin-bottom: 10px;
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .pdf-formula-name {
      font-size: 10pt;
      font-weight: 700;
      color: #0F172A;
      margin-bottom: 2px;
    }
    .pdf-formula-latex {
      font-size: 11.5pt;
      margin: 8px 0;
      text-align: center;
      color: #047857;
      font-family: "KaTeX_Main", "Times New Roman", serif;
    }
    .pdf-formula-note {
      font-size: 8.5pt;
      color: #475569;
      font-style: italic;
    }
    .pdf-quiz-item {
      background: #FFFFFF;
      border: 1px solid #E2E8F0;
      border-radius: 6px;
      padding: 10px 14px;
      margin-bottom: 10px;
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .pdf-quiz-q {
      font-weight: 600;
      font-size: 9.5pt;
      color: #0F172A;
      margin-bottom: 6px;
    }
    .pdf-quiz-options {
      margin: 0;
      padding-left: 18px;
      font-size: 9pt;
      color: #334155;
    }
    .pdf-quiz-expl {
      background: #F1F5F9;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 8.5pt;
      color: #475569;
      margin-top: 6px;
    }
    .pdf-exam-card {
      background: #FFFFFF;
      border: 1px solid #CBD5E1;
      border-radius: 6px;
      padding: 12px 14px;
      margin-bottom: 12px;
      break-inside: avoid;
      page-break-inside: avoid;
    }
    .pdf-exam-header {
      display: flex;
      justify-content: space-between;
      font-weight: 700;
      color: #1E3A8A;
      font-size: 10pt;
      margin-bottom: 6px;
    }
    .pdf-exam-answer {
      background: #F8FAFC;
      border-left: 3px solid #3B82F6;
      padding: 8px 12px;
      font-size: 9pt;
      color: #1E293B;
      margin-top: 6px;
      border-radius: 0 4px 4px 0;
    }
    .pdf-citation-badge {
      display: inline-block;
      font-size: 8pt;
      font-weight: 500;
      padding: 2px 7px;
      border-radius: 10px;
      background: #F1F5F9;
      color: #475569;
      border: 1px solid #E2E8F0;
      margin-right: 4px;
      margin-bottom: 4px;
    }
    .pdf-footer {
      border-top: 1px solid #E2E8F0;
      padding-top: 8px;
      margin-top: 24px;
      font-size: 8pt;
      color: #94A3B8;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
  </style>
</head>
<body>
  <div class="pdf-container">
    <!-- Header Masthead -->
    <header class="pdf-header">
      <div class="pdf-brand">
        <span>OmniLearn AI — Multi-Source Exam Mastery</span>
        <span class="pdf-badge-tag">Academic Study Pack</span>
      </div>
      <h1 class="pdf-title">${title}</h1>
      <div class="pdf-meta">
        <span>📚 <strong>Course:</strong> ${course}</span>
        ${topic ? `<span>🏷️ <strong>Topic:</strong> ${topic}</span>` : ''}
        ${instructor ? `<span>👨‍🏫 <strong>Instructor:</strong> ${instructor}</span>` : ''}
        <span>📅 <strong>Generated:</strong> ${now}</span>
      </div>
    </header>

    <!-- Executive Summary -->
    ${summary ? `
    <section class="pdf-section">
      <h2 class="pdf-section-title">📖 Executive Summary &amp; Theoretical Foundations</h2>
      <div class="pdf-summary-box">
        ${summary.split('\n\n').map((p) => `<p>${escapeHtml(p)}</p>`).join('')}
      </div>
    </section>
    ` : ''}

    <!-- Citations -->
    ${citations.length > 0 ? `
    <section class="pdf-section" style="margin-bottom: 14px;">
      <div style="font-size: 8.5pt; color: #64748B; margin-bottom: 4px; font-weight: 600;">SOURCES &amp; CITATIONS:</div>
      <div>
        ${citations.map((c) => {
          let label = typeof c === 'string' ? c : (c.label || c.title || c.source || '');
          if (typeof c === 'object' && c.location) label += ` (${c.location})`;
          return `<span class="pdf-citation-badge">📌 ${escapeHtml(label)}</span>`;
        }).join('')}
      </div>
    </section>
    ` : ''}

    <!-- Visual Architecture Diagram -->
    ${mermaidSvg ? `
    <section class="pdf-section">
      <h2 class="pdf-section-title">📊 Conceptual System Architecture Flowchart</h2>
      <div class="pdf-diagram-container">
        ${mermaidSvg}
        <div class="pdf-diagram-caption">Figure 1: Multi-Stage System Architecture &amp; State Transition Diagram</div>
      </div>
    </section>
    ` : (subject.mermaid_diagram ? `
    <section class="pdf-section">
      <h2 class="pdf-section-title">📊 Conceptual System Architecture Diagram</h2>
      <div class="pdf-card" style="font-family: monospace; font-size: 8.5pt; white-space: pre-wrap; background: #F8FAFC;">
        ${escapeHtml(subject.mermaid_diagram)}
      </div>
    </section>
    ` : '')}

    <!-- Key Concepts & Visuals -->
    ${concepts.length > 0 ? `
    <section class="pdf-section">
      <h2 class="pdf-section-title">🧠 Core Theoretical Concepts &amp; Visuals</h2>
      <div class="pdf-concept-grid">
        ${concepts.map((c, idx) => {
          if (typeof c === 'string') {
            return `<div class="pdf-concept-card"><p>${escapeHtml(c)}</p></div>`;
          }
          const cTitle = escapeHtml(c.title || c.name || `Concept ${idx + 1}`);
          const cDesc = escapeHtml(c.explanation || c.description || c.detail || '');
          const cCitation = escapeHtml(c.citation || '');
          const cTip = escapeHtml(c.exam_tip || '');
          const slideUrl = c.slide_url ? apiClient.getSlideUrl(c.slide_url) : '';
          const slideCaption = escapeHtml(c.slide_caption || cTitle);
          const slideTs = escapeHtml(c.slide_timestamp || '');

          return `
          <div class="pdf-concept-card">
            <div class="pdf-concept-title">
              <span>${cTitle}</span>
              ${cCitation ? `<span class="pdf-badge-tag">${cCitation}</span>` : ''}
            </div>
            <div class="pdf-concept-desc">${cDesc}</div>
            ${cTip ? `<div class="pdf-exam-tip">💡 <strong>Exam Tip:</strong> ${cTip}</div>` : ''}
            ${slideUrl ? `
            <figure class="pdf-figure">
              <img src="${slideUrl}" alt="${slideCaption}" />
              <figcaption>Slide Visual: ${slideCaption}${slideTs ? ` (@ ${slideTs})` : ''}</figcaption>
            </figure>
            ` : ''}
          </div>
          `;
        }).join('')}
      </div>
    </section>
    ` : ''}

    <!-- Visual Lecture Slides Gallery (if additional slides present) -->
    ${slides.length > 0 ? `
    <section class="pdf-section">
      <h2 class="pdf-section-title">🖼️ Lecture Slides &amp; Extracted Visual Figures</h2>
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
        ${slides.slice(0, 8).map((s, idx) => {
          const raw = typeof s === 'string' ? s : (s.url || s.path || '');
          const src = apiClient.getSlideUrl(raw);
          const cap = escapeHtml(typeof s === 'object' ? (s.caption || s.label || `Slide #${idx + 1}`) : `Slide #${idx + 1}`);
          const ts = escapeHtml(typeof s === 'object' ? (s.timestamp || '') : '');
          if (!src) return '';
          return `
          <div class="pdf-card" style="text-align: center; padding: 10px;">
            <img src="${src}" alt="${cap}" style="max-width: 100%; max-height: 180px; object-fit: contain; border-radius: 4px; border: 1px solid #CBD5E1;" />
            <div style="font-size: 8.5pt; color: #475569; margin-top: 4px; font-weight: 500;">
              ${cap}${ts ? ` <span style="color: #64748B;">(@ ${ts})</span>` : ''}
            </div>
          </div>
          `;
        }).join('')}
      </div>
    </section>
    ` : ''}

    <!-- Mathematical Formulations -->
    ${formulas.length > 0 ? `
    <section class="pdf-section">
      <h2 class="pdf-section-title">📐 Mathematical Formulations &amp; Derivations</h2>
      ${formulas.map((item, idx) => {
        const name = escapeHtml(item.label || item.name || `Equation (${idx + 1})`);
        const latex = item.latex || item.formula || (typeof item === 'string' ? item : '');
        const note = escapeHtml(item.explanation || item.note || '');
        const rendered = renderedFormulas.find((rf) => rf.latex === latex);

        return `
        <div class="pdf-formula-card">
          <div class="pdf-formula-name">${name}</div>
          <div class="pdf-formula-latex">
            ${rendered && rendered.html ? rendered.html : `$$\\displaystyle ${escapeHtml(latex)}$$`}
          </div>
          ${note ? `<div class="pdf-formula-note">Note / Intuition: ${note}</div>` : ''}
        </div>
        `;
      }).join('')}
    </section>
    ` : ''}

    <!-- Self-Assessment Practice MCQs -->
    ${quizzes.length > 0 ? `
    <section class="pdf-section">
      <h2 class="pdf-section-title">🎯 High-Yield Self-Assessment MCQs</h2>
      ${quizzes.slice(0, 8).map((q, idx) => {
        const qText = escapeHtml(q.question || `Question ${idx + 1}`);
        const opts = Array.isArray(q.options) ? q.options : [];
        const correctIdx = Number(q.correct_index ?? q.answer_index ?? 0);
        const expl = escapeHtml(q.explanation || '');

        return `
        <div class="pdf-quiz-item">
          <div class="pdf-quiz-q">Q${idx + 1}. ${qText}</div>
          <ol class="pdf-quiz-options" type="A">
            ${opts.map((opt, oIdx) => {
              const isCorrect = oIdx === correctIdx;
              return `<li style="${isCorrect ? 'font-weight: 600; color: #047857;' : ''}">${escapeHtml(opt)}${isCorrect ? ' ✓' : ''}</li>`;
            }).join('')}
          </ol>
          ${expl ? `<div class="pdf-quiz-expl"><strong>Explanation:</strong> ${expl}</div>` : ''}
        </div>
        `;
      }).join('')}
    </section>
    ` : ''}

    <!-- Examination Questions & Model Solutions -->
    ${examQuestions.length > 0 ? `
    <section class="pdf-section">
      <h2 class="pdf-section-title">🎓 University Examination Questions &amp; Rubrics</h2>
      ${examQuestions.slice(0, 6).map((eq, idx) => {
        const sec = escapeHtml(eq.section || 'Section B');
        const marks = Number(eq.marks || 10);
        const qText = escapeHtml(eq.question || '');
        const answer = escapeHtml(eq.model_answer || '');
        const rubric = Array.isArray(eq.rubric) ? eq.rubric.map(escapeHtml).join(' • ') : escapeHtml(eq.rubric || '');

        return `
        <div class="pdf-exam-card">
          <div class="pdf-exam-header">
            <span>[${sec}] Question ${idx + 1}</span>
            <span>(${marks} Marks)</span>
          </div>
          <div style="font-weight: 500; font-size: 9.5pt; color: #0F172A; margin-bottom: 6px;">${qText}</div>
          ${answer ? `<div class="pdf-exam-answer"><strong>Model Solution:</strong><br/>${answer}</div>` : ''}
          ${rubric ? `<div style="font-size: 8pt; color: #64748B; margin-top: 4px;"><strong>Scoring Rubric:</strong> ${rubric}</div>` : ''}
        </div>
        `;
      }).join('')}
    </section>
    ` : ''}

    <!-- Footer -->
    <footer class="pdf-footer">
      <span>OmniLearn AI — Verified Course Mastery Engine</span>
      <span>Published for Student Revision</span>
    </footer>
  </div>
</body>
</html>`;
}

/**
 * Downloads a plain text file (Markdown, CSV, TXT) via anchor download.
 */
export function downloadTextFile(filename, content, mimeType = 'text/plain;charset=utf-8') {
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    document.body.removeChild(anchor);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

/**
 * Opens a print dialog via hidden iframe or window.
 */
export function printNotesDocument(htmlContent) {
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  const iframe = document.createElement('iframe');
  iframe.style.position = 'fixed';
  iframe.style.right = '0';
  iframe.style.bottom = '0';
  iframe.style.width = '0';
  iframe.style.height = '0';
  iframe.style.border = '0';
  document.body.appendChild(iframe);

  try {
    const doc = iframe.contentWindow?.document || iframe.contentDocument;
    if (doc) {
      doc.open();
      doc.write(htmlContent);
      doc.close();
      setTimeout(() => {
        try {
          iframe.contentWindow?.focus();
          iframe.contentWindow?.print();
        } catch (_e) {
          window.print();
        } finally {
          setTimeout(() => {
            if (document.body.contains(iframe)) {
              document.body.removeChild(iframe);
            }
          }, 60000);
        }
      }, 500);
    }
  } catch (_e) {
    window.print();
  }
}

/**
 * Downloads the subject notes directly as a styled publication-grade PDF file.
 */
export async function exportNotesPdf(subject, courseTitle, edition, options = {}) {
  if (!requireCapability(edition, 'exports')) return false;
  if (typeof window === 'undefined' || typeof document === 'undefined') return false;

  const htmlContent = buildNotesPrintableHtml(subject, courseTitle, options);
  const filename = `${slugifyFilename(subject.short_title || subject.title)}-study-notes.pdf`;

  if (options.forcePrint) {
    printNotesDocument(htmlContent);
    return true;
  }

  // Load html2pdf dynamically if not present
  if (!window.html2pdf) {
    try {
      await new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = 'https://cdn.jsdelivr.net/npm/html2pdf.js@0.10.1/dist/html2pdf.bundle.min.js';
        s.async = true;
        s.onload = resolve;
        s.onerror = () => reject(new Error('Failed to load html2pdf'));
        document.head.appendChild(s);
      });
    } catch (_err) {
      // Fallback directly to print window
      printNotesDocument(htmlContent);
      return true;
    }
  }

  if (window.html2pdf) {
    try {
      const container = document.createElement('div');
      container.className = 'omnilearn-pdf-export-container';
      container.style.position = 'fixed';
      container.style.left = '-9999px';
      container.style.top = '0';
      container.style.width = '800px';
      container.style.background = '#FFFFFF';
      container.innerHTML = htmlContent;
      document.body.appendChild(container);

      const opt = {
        margin: [12, 12, 14, 12],
        filename: filename,
        image: { type: 'jpeg', quality: 0.98 },
        html2canvas: { scale: 2, useCORS: true, logging: false },
        jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
        pagebreak: { mode: ['avoid-all', 'css', 'legacy'] },
      };

      await window.html2pdf().set(opt).from(container).save();
      document.body.removeChild(container);
      return true;
    } catch (_err) {
      printNotesDocument(htmlContent);
      return true;
    }
  }

  printNotesDocument(htmlContent);
  return true;
}

export function exportNotesMarkdown(subject, courseTitle, edition) {
  if (!requireCapability(edition, 'exports')) return false;
  const md = buildNotesMarkdown(subject, courseTitle);
  downloadTextFile(`${slugifyFilename(subject.short_title || subject.title)}-notes.md`, md, 'text/markdown;charset=utf-8');
  return true;
}

export function buildQuizAnkiCsv(subject, courseTitle = 'OmniLearn') {
  const rows = [escapeCsvRow(['Front', 'Back', 'Tags'])];
  const tag = String(subject.short_title || subject.title || courseTitle).replace(/\s+/g, '_');

  const questions = Array.isArray(subject.quiz_questions) ? subject.quiz_questions : [];
  questions.forEach((q) => {
    const opts = (q.options || []).map((o, idx) => `${String.fromCharCode(65 + idx)}. ${o}`).join('<br>');
    const front = `<strong>${q.question || ''}</strong><br><br>${opts}`;
    const correctLetter = String.fromCharCode(65 + (q.correct_index || 0));
    const back = `Correct: <strong>${correctLetter}</strong><br>${q.explanation || ''}`;
    rows.push(escapeCsvRow([front, back, tag]));
  });

  return rows.join('\r\n');
}

export function exportQuizAnkiCsv(subject, courseTitle, edition) {
  if (!requireCapability(edition, 'exports')) return false;
  const csv = buildQuizAnkiCsv(subject, courseTitle);
  downloadTextFile(`${slugifyFilename(subject.short_title || subject.title)}-anki.csv`, csv, 'text/csv;charset=utf-8');
  return true;
}

export function buildExamPaperTxt(subject, courseTitle = 'OmniLearn AI') {
  const lines = [];
  lines.push('================================================================================');
  lines.push(`  ${(courseTitle || 'OMNILEARN EXAM AI').toUpperCase()}`);
  lines.push(`  SUBJECT: ${(subject.title || 'Mastery Pack').toUpperCase()}`);
  lines.push(`  UNIT/TOPIC: ${(subject.unit || subject.topic || 'Core Syllabus').toUpperCase()}`);
  lines.push('  TIME ALLOWED: 3 HOURS | MAXIMUM MARKS: 100');
  lines.push('================================================================================');
  lines.push('');

  const questions = Array.isArray(subject.exam_questions) ? subject.exam_questions : [];
  if (questions.length === 0) {
    lines.push('No formal university exam questions generated for this subject yet.');
    return lines.join('\n');
  }

  const sections = ['Section A', 'Section B', 'Section C'];
  sections.forEach((sec) => {
    const secQuestions = questions.filter((q) => q.section === sec);
    if (secQuestions.length === 0) return;

    lines.push(`--- ${sec.toUpperCase()} ---`);
    lines.push('');
    secQuestions.forEach((q, idx) => {
      lines.push(`Q${idx + 1}. [${q.marks || 5} MARKS]`);
      lines.push(q.question || '');
      lines.push('');
      if (q.rubric) {
        const rubricStr = Array.isArray(q.rubric) ? q.rubric.join(' | ') : String(q.rubric);
        lines.push(`Marking Rubric: ${rubricStr}`);
        lines.push('');
      }
      if (q.model_answer) {
        lines.push(`Model Answer:\n${q.model_answer}`);
        lines.push('');
      }
    });
  });

  return lines.join('\n');
}

export function exportExamPaperTxt(subject, courseTitle, edition) {
  if (!requireCapability(edition, 'exports')) return false;
  const txt = buildExamPaperTxt(subject, courseTitle);
  downloadTextFile(`${slugifyFilename(subject.short_title || subject.title)}-model-paper.txt`, txt, 'text/plain;charset=utf-8');
  return true;
}
