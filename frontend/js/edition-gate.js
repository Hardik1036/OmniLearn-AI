/* OmniLearn AI — Edition Capability Matrix & Defensive Data Normalizers */

export const EDITION_WEB_LITE = 'web_lite';
export const EDITION_SYSTEM_APP = 'system_app';

export const WEB_LITE_LIMITS = Object.freeze({
  maxSources: 2,
  maxPoolKeys: 1,
  maxSearchResults: 3,
  maxTextFileBytes: 500 * 1024,
  maxVideoDurationSec: 300,
  maxVideoKeyframes: 8,
  maxNotesConcepts: 2,
  maxQuizQuestions: 3,
  maxExamQuestions: 1,
});

export function isSystemApp(edition) {
  return edition === EDITION_SYSTEM_APP;
}

export function isWebLite(edition) {
  return !isSystemApp(edition);
}

export function checkCapability(edition, feature, context = {}) {
  if (isSystemApp(edition)) {
    return { allowed: true, feature };
  }

  switch (feature) {
    case 'fts_search':
      return {
        allowed: false,
        feature,
        message: 'Local SQLite FTS5 full-text search across PDFs, spreadsheets, and transcripts requires the Downloaded System App.',
      };
    case 'inspect_url_pdfs':
      return {
        allowed: false,
        feature,
        message: 'Website HTML crawling and batch PDF harvesting require the Downloaded System App.',
      };
    case 'youtube_ingest':
      return {
        allowed: false,
        feature,
        message: 'YouTube transcript, chapter, and keyframe extraction requires the Downloaded System App.',
      };
    case 'rich_doc_upload':
      return {
        allowed: false,
        feature,
        message: 'Native PDF, XLSX, PPT/PPTX, and OpenCV multi-hour video parsing requires the OmniLearn System App (internet required for AI).',
      };
    case 'unlimited_sources': {
      const count = Number(context.sourceCount ?? 0);
      if (count >= WEB_LITE_LIMITS.maxSources) {
        return {
          allowed: false,
          limit: WEB_LITE_LIMITS.maxSources,
          feature,
          message: `Web Lite caps courses at ${WEB_LITE_LIMITS.maxSources} sources. Download the System App for unlimited mixed sources.`,
        };
      }
      return { allowed: true, limit: WEB_LITE_LIMITS.maxSources, feature };
    }
    case 'unlimited_pool': {
      const slotCount = Number(context.slotCount ?? 0);
      if (slotCount >= WEB_LITE_LIMITS.maxPoolKeys) {
        return {
          allowed: false,
          limit: WEB_LITE_LIMITS.maxPoolKeys,
          feature,
          message: `Web Lite caps the Multi-API Pool at ${WEB_LITE_LIMITS.maxPoolKeys} active slot. System App unlocks unlimited key rotation.`,
        };
      }
      return { allowed: true, limit: WEB_LITE_LIMITS.maxPoolKeys, feature };
    }
    case 'full_source_search': {
      const filter = context.filter || 'all';
      if (filter !== 'all' && filter !== 'academic') {
        return {
          allowed: false,
          limit: WEB_LITE_LIMITS.maxSearchResults,
          feature,
          message: 'Web Docs, Direct PDFs, and YouTube search filters require the Downloaded System App. Web Lite previews 3 OpenAlex academic hits.',
        };
      }
      return { allowed: true, limit: WEB_LITE_LIMITS.maxSearchResults, feature };
    }
    case 'ai_grader':
      return {
        allowed: false,
        feature,
        message: 'AI Examiner Rubric Grading requires the Downloaded System App.',
      };
    case 'exports':
      return {
        allowed: false,
        feature,
        message: 'Exporting Markdown notes, Anki CSV decks, and printable Exam Papers requires the Downloaded System App.',
      };
    case 'full_notes_concepts':
      return { allowed: false, limit: WEB_LITE_LIMITS.maxNotesConcepts, feature };
    case 'full_quiz':
      return { allowed: false, limit: WEB_LITE_LIMITS.maxQuizQuestions, feature };
    case 'full_exam':
      return { allowed: false, limit: WEB_LITE_LIMITS.maxExamQuestions, feature };
    default:
      return {
        allowed: false,
        feature,
        message: 'Download the OmniLearn System App to unlock this capability.',
      };
  }
}

export function promptSystemAppDownload(feature = '', message = '') {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(
      new CustomEvent('omnilearn:open-download-modal', {
        detail: { feature, message },
      })
    );
  }
}

export function requireCapability(edition, feature, context = {}) {
  const result = checkCapability(edition, feature, context);
  if (!result.allowed) {
    promptSystemAppDownload(result.feature, result.message || '');
  }
  return result.allowed;
}

export function safeHttpUrl(rawUrl) {
  if (typeof rawUrl !== 'string') {
    return '';
  }
  const trimmed = rawUrl.trim();
  if (!trimmed) {
    return '';
  }
  if (trimmed.startsWith('/api/')) {
    return trimmed;
  }
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.href;
    }
  } catch (_err) {
    return '';
  }
  return '';
}

export function parseJsonField(value, fallback) {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  if (typeof value === 'string') {
    try {
      return JSON.parse(value);
    } catch (_err) {
      return fallback;
    }
  }
  return value;
}

export function normalizeSubject(raw) {
  const s = raw || {};
  const studyPlanRaw = s.study_plan || {};
  const schedule = parseJsonField(
    s.schedule ?? studyPlanRaw.schedule ?? studyPlanRaw.schedule_json ?? s.schedule_json,
    []
  );
  const modelPaperRaw = s.model_paper || {};
  const examQuestions = parseJsonField(
    modelPaperRaw.questions ?? modelPaperRaw.questions_json ?? s.exam_questions ?? s.questions_json,
    []
  );
  const quizQuestions = parseJsonField(
    s.quiz_questions ?? s.quizzes ?? s.questions ?? (s.quiz ? s.quiz.questions || s.quiz.questions_json : null),
    []
  );
  const progressRaw = s.progress || s.user_progress || {};

  return {
    id: s.id || '',
    course_id: s.course_id || '',
    quadrant_key: s.quadrant_key || 'q1',
    title: s.title || 'Untitled Subject',
    short_title: s.short_title || s.title || 'Subject',
    topic: s.topic || '',
    unit: s.unit || '',
    instructor: s.instructor || '',
    badge_color: s.badge_color || '#3B82F6',
    summary: s.summary || '',
    subtitles_transcript: parseJsonField(s.subtitles_transcript, []),
    slide_crops: parseJsonField(s.slide_crops, []),
    source_citations: parseJsonField(s.source_citations, []),
    mermaid_diagram: typeof s.mermaid_diagram === 'string' ? s.mermaid_diagram : '',
    key_concepts: parseJsonField(s.key_concepts ?? s.details?.key_concepts, []),
    math_formulas: parseJsonField(s.math_formulas ?? s.details?.math_formulas, []),
    study_plan: {
      target_days: Number(studyPlanRaw.target_days ?? s.target_days ?? 14),
      daily_hours: Number(studyPlanRaw.daily_hours ?? s.daily_hours ?? 3.0),
      schedule: Array.isArray(schedule) ? schedule : [],
    },
    quizzes: Array.isArray(quizQuestions) ? quizQuestions : [],
    model_paper: {
      title: modelPaperRaw.title || s.paper_title || `${s.title || 'Subject'} Model Exam Paper`,
      time_allowed: modelPaperRaw.time_allowed || s.time_allowed || '60 Minutes',
      max_marks: Number(modelPaperRaw.max_marks ?? s.max_marks ?? 25),
      questions: Array.isArray(examQuestions) ? examQuestions : [],
    },
    progress: {
      quiz_attempts: Number(progressRaw.quiz_attempts ?? 0),
      last_quiz_score: Number(progressRaw.last_quiz_score ?? 0),
      mastery_score: Number(progressRaw.mastery_score ?? 0),
      completed_tasks: parseJsonField(progressRaw.completed_tasks, []),
      weak_spots: parseJsonField(progressRaw.weak_spots, []),
    },
  };
}

export function normalizeSource(raw) {
  const src = raw || {};
  return {
    id: src.id || '',
    course_id: src.course_id || '',
    source_type: src.source_type || 'txt',
    title: src.title || 'Untitled Source',
    origin_url: src.origin_url || '',
    search_query: src.search_query || '',
    file_hash: src.file_hash || '',
    storage_key: src.storage_key || '',
    raw_text_excerpt: src.raw_text_excerpt || '',
    structured_chunks: parseJsonField(src.structured_chunks ?? src.structured_chunks_json, []),
    visual_assets: parseJsonField(src.visual_assets ?? src.visual_assets_json, []),
    discovered_links: parseJsonField(src.discovered_links ?? src.discovered_links_json, []),
    metadata: parseJsonField(src.metadata ?? src.metadata_json, {}),
    status: src.status || 'PARSED',
    created_at: src.created_at || '',
  };
}

export function normalizeCourseData(raw) {
  if (!raw) {
    return { course: null, rois: [], subjects: [], sources: [] };
  }
  const course = raw.course || (raw.id ? raw : null);
  const rois = Array.isArray(raw.rois) ? raw.rois : Array.isArray(raw.window_rois) ? raw.window_rois : [];
  const subjectsRaw = Array.isArray(raw.subjects) ? raw.subjects : [];
  const sourcesRaw = Array.isArray(raw.sources) ? raw.sources : [];
  return {
    course,
    rois,
    subjects: subjectsRaw.map(normalizeSubject),
    sources: sourcesRaw.map(normalizeSource),
  };
}
