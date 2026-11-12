/* OmniLearn AI — Client-Side Exporters (Markdown, Anki CSV with Formula Injection Escaping, Exam TXT) */

import { requireCapability } from './edition-gate.js';

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
    lines.push('## Summary');
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

  if (Array.isArray(subject.key_concepts) && subject.key_concepts.length > 0) {
    lines.push('## Key Concepts');
    for (const concept of subject.key_concepts) {
      if (typeof concept === 'string') {
        lines.push(`- ${concept}`);
      } else {
        const title = concept.title || concept.name || 'Concept';
        const desc = concept.explanation || concept.description || concept.detail || '';
        lines.push(`### ${title}`);
        if (desc) lines.push(desc);
        if (concept.citation) lines.push(`> Source: ${concept.citation}`);
        lines.push('');
      }
    }
  }

  if (Array.isArray(subject.math_formulas) && subject.math_formulas.length > 0) {
    lines.push('## Key Formulas');
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

  if (subject.mermaid_diagram) {
    lines.push('## Concept Architecture Diagram');
    lines.push('```mermaid');
    lines.push(subject.mermaid_diagram);
    lines.push('```');
    lines.push('');
  }

  return lines.join('\n');
}

export function buildQuizAnkiCsv(subject, courseTitle = 'OmniLearn') {
  const rows = [escapeCsvRow(['Front', 'Back', 'Tags'])];
  const tag = String(subject.short_title || subject.title || courseTitle).replace(/\s+/g, '_');

  if (Array.isArray(subject.quizzes)) {
    for (const q of subject.quizzes) {
      const options = Array.isArray(q.options) ? q.options : [];
      const optionBlock = options.map((opt, idx) => `${String.fromCharCode(65 + idx)}. ${opt}`).join(' | ');
      const front = optionBlock ? `${q.question || ''}\n\nOptions: ${optionBlock}` : (q.question || '');
      const correctVal =
        typeof q.correct_index === 'number' && options[q.correct_index] !== undefined
          ? options[q.correct_index]
          : q.answer || '';
      const back = [correctVal ? `Answer: ${correctVal}` : '', q.explanation || ''].filter(Boolean).join('\n\n');
      rows.push(escapeCsvRow([front, back, `${tag} quiz`]));
    }
  }

  if (Array.isArray(subject.key_concepts)) {
    for (const c of subject.key_concepts) {
      if (c && typeof c === 'object') {
        const front = c.title || c.name || '';
        const back = c.explanation || c.description || c.detail || '';
        if (front && back) {
          rows.push(escapeCsvRow([front, back, `${tag} concept`]));
        }
      }
    }
  }

  return rows.join('\r\n');
}

export function buildExamPaperTxt(subject, courseTitle = 'OmniLearn AI') {
  const paper = subject.model_paper || {};
  const lines = [];
  lines.push('====================================================================');
  lines.push(`UNIVERSITY MODEL EXAMINATION PAPER — ${courseTitle.toUpperCase()}`);
  lines.push(`SUBJECT: ${subject.title || 'Subject'}`);
  lines.push(`TITLE: ${paper.title || 'Model Question Paper'}`);
  lines.push(`TIME ALLOWED: ${paper.time_allowed || '60 Minutes'}   |   MAX MARKS: ${paper.max_marks || 25}`);
  lines.push('====================================================================');
  lines.push('');

  const questions = Array.isArray(paper.questions) ? paper.questions : [];
  questions.forEach((q, idx) => {
    const sec = q.section ? `[${q.section}] ` : '';
    const marks = q.marks ? ` (${q.marks} Marks)` : '';
    lines.push(`Q${idx + 1}. ${sec}${q.question || q.prompt || ''}${marks}`);
    if (q.rubric) {
      const rubricStr = Array.isArray(q.rubric) ? q.rubric.join(' | ') : String(q.rubric);
      lines.push(`    Rubric Hint: ${rubricStr}`);
    }
    lines.push('');
  });

  lines.push('--------------------------------------------------------------------');
  lines.push('MODEL SOLUTIONS & MARKING SCHEME');
  lines.push('--------------------------------------------------------------------');
  questions.forEach((q, idx) => {
    lines.push(`Q${idx + 1} Solution:`);
    lines.push(q.model_answer || q.solution || 'See subject visual notes.');
    if (q.rubric) {
      const rubricStr = Array.isArray(q.rubric) ? q.rubric.join(' | ') : String(q.rubric);
      lines.push(`Marking Rubric: ${rubricStr}`);
    }
    lines.push('');
  });

  return lines.join('\n');
}

function slugifyFilename(name) {
  return String(name || 'omnilearn')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'omnilearn';
}

export function downloadTextFile(filename, content, mimeType = 'text/plain;charset=utf-8') {
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

export function exportNotesMarkdown(subject, courseTitle, edition) {
  if (!requireCapability(edition, 'exports')) return false;
  const md = buildNotesMarkdown(subject, courseTitle);
  downloadTextFile(`${slugifyFilename(subject.short_title || subject.title)}-notes.md`, md, 'text/markdown;charset=utf-8');
  return true;
}

export function exportQuizAnkiCsv(subject, courseTitle, edition) {
  if (!requireCapability(edition, 'exports')) return false;
  const csv = buildQuizAnkiCsv(subject, courseTitle);
  downloadTextFile(`${slugifyFilename(subject.short_title || subject.title)}-anki.csv`, csv, 'text/csv;charset=utf-8');
  return true;
}

export function exportExamPaperTxt(subject, courseTitle, edition) {
  if (!requireCapability(edition, 'exports')) return false;
  const txt = buildExamPaperTxt(subject, courseTitle);
  downloadTextFile(`${slugifyFilename(subject.short_title || subject.title)}-model-paper.txt`, txt, 'text/plain;charset=utf-8');
  return true;
}
