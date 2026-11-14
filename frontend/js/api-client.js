/* OmniLearn AI — Authenticated API Client Wrapper (Zero-Leakage + Edition Gate Interceptor) */

const SESSION_TOKEN_KEY = 'omnilearn_session_token';
const LOCAL_TOKEN_KEY = 'omnilearn_local_token';

let _memorySessionToken = '';
let _memoryLocalToken = '';

export class ApiError extends Error {
  constructor(message, status = 500, data = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = data?.error || data?.code || '';
    this.feature = data?.feature || '';
    this.data = data;
  }
}

export function readStorage(key) {
  try {
    if (typeof localStorage !== 'undefined') {
      const val = localStorage.getItem(key);
      if (val !== null && val !== undefined && val !== '') return val;
    }
    if (typeof sessionStorage !== 'undefined') {
      return sessionStorage.getItem(key) || '';
    }
  } catch (_err) {
    // Ignore restricted storage contexts
  }
  return '';
}

export function writeStorage(key, value) {
  try {
    if (typeof localStorage !== 'undefined') {
      if (value) {
        localStorage.setItem(key, value);
      } else {
        localStorage.removeItem(key);
      }
    }
    if (typeof sessionStorage !== 'undefined') {
      if (value) {
        sessionStorage.setItem(key, value);
      } else {
        sessionStorage.removeItem(key);
      }
    }
  } catch (_err) {
    // Ignore restricted storage contexts
  }
}

export function bootstrapLocalTokenFromEnvironment() {
  if (typeof window === 'undefined') {
    return '';
  }
  if (typeof window.__OMNILEARN_LOCAL_TOKEN__ === 'string' && window.__OMNILEARN_LOCAL_TOKEN__) {
    _memoryLocalToken = window.__OMNILEARN_LOCAL_TOKEN__;
    writeStorage(LOCAL_TOKEN_KEY, _memoryLocalToken);
    return _memoryLocalToken;
  }
  if (typeof document !== 'undefined') {
    const meta = document.querySelector('meta[name="local-app-token"]');
    const content = meta?.getAttribute('content') || '';
    if (content) {
      _memoryLocalToken = content;
      writeStorage(LOCAL_TOKEN_KEY, content);
      return content;
    }
  }
  try {
    const url = new URL(window.location.href);
    const paramToken = url.searchParams.get('local_token') || url.searchParams.get('token') || '';
    if (paramToken) {
      _memoryLocalToken = paramToken;
      writeStorage(LOCAL_TOKEN_KEY, paramToken);
      url.searchParams.delete('local_token');
      url.searchParams.delete('token');
      window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
      return paramToken;
    }
  } catch (_err) {
    // Ignore invalid URL environments
  }
  return getLocalAppToken();
}

export function getLocalAppToken() {
  return _memoryLocalToken || readStorage(LOCAL_TOKEN_KEY);
}

export function getSessionToken() {
  return _memorySessionToken || readStorage(SESSION_TOKEN_KEY);
}

export function setSessionToken(token) {
  _memorySessionToken = typeof token === 'string' ? token : '';
  writeStorage(SESSION_TOKEN_KEY, _memorySessionToken);
}

export function clearSessionToken() {
  _memorySessionToken = '';
  writeStorage(SESSION_TOKEN_KEY, '');
}

async function parseResponseBody(response) {
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    try {
      return await response.json();
    } catch (_err) {
      return {};
    }
  }
  try {
    const text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (_err) {
      return { message: text };
    }
  } catch (_err) {
    return {};
  }
}

export async function apiRequest(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;

  let body = options.body;
  if (body !== undefined && body !== null && !isFormData && typeof body === 'object' && !(body instanceof Blob)) {
    if (!headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    body = JSON.stringify(body);
  }

  const localToken = getLocalAppToken();
  if (localToken && !headers.has('X-Local-App-Token')) {
    headers.set('X-Local-App-Token', localToken);
  }

  const sessionToken = getSessionToken();
  if (sessionToken && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${sessionToken}`);
  }

  const response = await fetch(path, {
    method,
    headers,
    body: method === 'GET' || method === 'HEAD' ? undefined : body,
    credentials: 'same-origin',
  });

  const data = await parseResponseBody(response);

  if (response.status === 403 && data?.error === 'EDITION_LIMIT_EXCEEDED') {
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('omnilearn:edition-limit', {
          detail: {
            feature: data.feature || '',
            message: data.message || 'Download OmniLearn System App to unlock this feature.',
          },
        })
      );
    }
    throw new ApiError(
      data.message || 'Download OmniLearn System App to unlock this feature.',
      403,
      data
    );
  }

  if (response.status === 401) {
    const isAuthAttempt =
      path.startsWith('/api/auth/login') ||
      path.startsWith('/api/auth/init') ||
      path.startsWith('/api/auth/status');
    if (!isAuthAttempt && typeof window !== 'undefined') {
      clearSessionToken();
      window.dispatchEvent(new CustomEvent('omnilearn:auth-expired'));
    }
  }

  if (!response.ok) {
    const msg = data?.detail || data?.message || data?.error || `Request failed (${response.status})`;
    throw new ApiError(typeof msg === 'string' ? msg : JSON.stringify(msg), response.status, data);
  }

  return data;
}

export const apiClient = {
  bootstrap: bootstrapLocalTokenFromEnvironment,

  getHealth() {
    return apiRequest('/api/health');
  },

  getLatestReleases() {
    return apiRequest('/api/releases/latest');
  },

  getAuthStatus() {
    return apiRequest('/api/auth/status');
  },

  async initAdmin({ username, password, confirm_password, provider, api_key }) {
    const payload = {
      username: (username || 'admin').trim() || 'admin',
      password,
      confirm_password: confirm_password ?? password,
    };
    if (api_key && api_key.trim()) {
      payload.provider = provider || 'gemini';
      payload.api_key = api_key.trim();
    }
    const res = await apiRequest('/api/auth/init', { method: 'POST', body: payload });
    if (res?.token) {
      setSessionToken(res.token);
    }
    return res;
  },

  async loginAdmin({ username, password, provider, api_key }) {
    const payload = {
      username: (username || 'admin').trim() || 'admin',
      password,
    };
    if (api_key && api_key.trim()) {
      payload.provider = provider || 'gemini';
      payload.api_key = api_key.trim();
    }
    const res = await apiRequest('/api/auth/login', { method: 'POST', body: payload });
    if (res?.token) {
      setSessionToken(res.token);
    }
    return res;
  },

  async logout() {
    try {
      return await apiRequest('/api/auth/logout', { method: 'POST' });
    } finally {
      clearSessionToken();
    }
  },

  async launchGuestLite() {
    const res = await apiRequest('/api/auth/guest-lite', { method: 'POST' });
    if (res?.token) {
      setSessionToken(res.token);
    }
    return res;
  },

  getGoogleAuthConfig() {
    return apiRequest('/api/auth/google/config');
  },

  getGoogleOAuthStartUrl() {
    const localToken = getLocalAppToken();
    return localToken
      ? `/api/auth/google/start?local_token=${encodeURIComponent(localToken)}`
      : '/api/auth/google/start';
  },

  async loginWithGoogle({ id_token, provider, api_key }) {
    const payload = { id_token };
    if (api_key && api_key.trim()) {
      payload.provider = provider || 'gemini';
      payload.api_key = api_key.trim();
    }
    const res = await apiRequest('/api/auth/google', { method: 'POST', body: payload });
    if (res?.token) {
      setSessionToken(res.token);
    }
    return res;
  },

  getPool() {
    return apiRequest('/api/pool');
  },

  addPoolKey({ provider, api_key }) {
    return apiRequest('/api/pool/add', {
      method: 'POST',
      body: { provider: provider || 'gemini', api_key },
    });
  },

  deletePoolKey(id) {
    return apiRequest(`/api/pool/${encodeURIComponent(id)}`, { method: 'DELETE' });
  },

  listCourses() {
    return apiRequest('/api/courses');
  },

  createCourse({ title, layout_type = 'mixed' }) {
    return apiRequest('/api/courses', {
      method: 'POST',
      body: { title, layout_type },
    });
  },

  getCourse(id) {
    return apiRequest(`/api/courses/${encodeURIComponent(id)}`);
  },

  deleteCourse(id) {
    return apiRequest(`/api/courses/${encodeURIComponent(id)}`, { method: 'DELETE' });
  },

  createSubject(courseId, { title, topic = '', unit = '', instructor = 'OmniLearn AI' }) {
    return apiRequest(`/api/courses/${encodeURIComponent(courseId)}/subjects`, {
      method: 'POST',
      body: { title, topic, unit, instructor },
    });
  },

  synthesizeCourse({
    course_id,
    source_ids,
    target_days,
    daily_hours,
    target_subject_name = '',
    subject_names = null,
    append_subject = false,
  }) {
    return apiRequest('/api/courses/synthesize', {
      method: 'POST',
      body: {
        course_id,
        source_ids,
        target_days,
        daily_hours,
        target_subject_name,
        subject_names,
        append_subject,
      },
    });
  },

  chatWithTutor({ message, course_id = '', subject_id = '', history = [] }) {
    return apiRequest('/api/tutor/chat', {
      method: 'POST',
      body: { message, course_id, subject_id, history },
    });
  },

  searchSources({ query, filter = 'all', course_id = '' }) {
    return apiRequest('/api/sources/search', {
      method: 'POST',
      body: { query, filter, provider_filter: filter, course_id },
    });
  },

  inspectUrl({ url, course_id = '' }) {
    return apiRequest('/api/sources/inspect-url', {
      method: 'POST',
      body: { url, course_id },
    });
  },

  ingestFile(formData) {
    return apiRequest('/api/sources/ingest-file', {
      method: 'POST',
      body: formData,
    });
  },

  ingestUrl(payload) {
    return apiRequest('/api/sources/ingest-url', {
      method: 'POST',
      body: payload,
    });
  },

  deleteSource(id) {
    return apiRequest(`/api/sources/${encodeURIComponent(id)}`, { method: 'DELETE' });
  },

  ftsSearch({ q, course_id = '' }) {
    const params = new URLSearchParams({ q: q || '' });
    if (course_id) {
      params.set('course_id', course_id);
    }
    return apiRequest(`/api/sources/fts?${params.toString()}`);
  },

  uploadSlide(formData) {
    return apiRequest('/api/slides/upload', {
      method: 'POST',
      body: formData,
    });
  },

  transcribeMedia(payload) {
    return apiRequest('/api/sources/transcribe', {
      method: 'POST',
      body: payload,
    });
  },

  getSlideUrl(slidePath) {
    if (!slidePath || typeof slidePath !== 'string') {
      return '';
    }
    const clean = slidePath.trim();
    if (!clean) return '';
    if (
      clean.startsWith('http://') ||
      clean.startsWith('https://') ||
      /^data:image\/(png|jpeg|jpg|webp|svg\+xml);base64,/i.test(clean)
    ) {
      return clean;
    }
    const normalized = clean.startsWith('/api/slides/')
      ? clean
      : `/api/slides/${clean.replace(/^\/+/, '')}`;
    const localToken = getLocalAppToken();
    if (localToken && !normalized.includes('local_token=')) {
      const sep = normalized.includes('?') ? '&' : '?';
      return `${normalized}${sep}local_token=${encodeURIComponent(localToken)}`;
    }
    return normalized;
  },

  saveTaskProgress({ subject_id, task_id, completed, completed_tasks }) {
    return apiRequest('/api/progress/task', {
      method: 'POST',
      body: { subject_id, task_id, completed, completed_tasks },
    });
  },

  saveQuizProgress({ subject_id, score, total, weak_spots, answers }) {
    return apiRequest('/api/progress/quiz', {
      method: 'POST',
      body: { subject_id, score, total, weak_spots, answers },
    });
  },

  gradeExamAnswer(payload) {
    return apiRequest('/api/exam/grade', {
      method: 'POST',
      body: payload,
    });
  },
};
