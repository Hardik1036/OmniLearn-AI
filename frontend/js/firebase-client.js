/* OmniLearn AI — Firebase Client Integration (Optional Auth & Firestore Sync) */

let _firebaseApp = null;
let _auth = null;
let _db = null;

export function getStoredFirebaseConfig() {
  if (typeof window === 'undefined') return null;
  if (window.__FIREBASE_CONFIG__) return window.__FIREBASE_CONFIG__;
  try {
    const raw = localStorage.getItem('omnilearn_firebase_config');
    return raw ? JSON.parse(raw) : null;
  } catch (_e) {
    return null;
  }
}

export function isFirebaseConfigured() {
  return Boolean(getStoredFirebaseConfig()?.apiKey);
}

export async function initFirebase(config = null) {
  const effectiveConfig = config || getStoredFirebaseConfig();
  if (!effectiveConfig || !effectiveConfig.apiKey) {
    return null;
  }

  if (_firebaseApp) {
    return { app: _firebaseApp, auth: _auth, db: _db };
  }

  try {
    // Dynamic import to avoid bundling overhead when Firebase is inactive
    const { initializeApp } = await import('https://www.gstatic.com/firebasejs/10.8.0/firebase-app.js');
    const { getAuth } = await import('https://www.gstatic.com/firebasejs/10.8.0/firebase-auth.js');
    const { getFirestore } = await import('https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js');

    _firebaseApp = initializeApp(effectiveConfig);
    _auth = getAuth(_firebaseApp);
    _db = getFirestore(_firebaseApp);

    return { app: _firebaseApp, auth: _auth, db: _db };
  } catch (err) {
    console.warn('[OmniLearn Firebase] Could not initialize Firebase SDK:', err);
    return null;
  }
}

export async function syncQuizProgressToFirebase(userId, subjectId, scoreData) {
  if (!isFirebaseConfigured() || !userId) return false;
  try {
    const fb = await initFirebase();
    if (!fb?.db) return false;

    const { doc, setDoc } = await import('https://www.gstatic.com/firebasejs/10.8.0/firebase-firestore.js');
    const ref = doc(fb.db, 'users', userId, 'quiz_progress', subjectId);
    await setDoc(
      ref,
      {
        ...scoreData,
        updated_at: new Date().toISOString(),
      },
      { merge: true }
    );
    return true;
  } catch (err) {
    console.warn('[OmniLearn Firebase] Firestore sync skipped:', err);
    return false;
  }
}
