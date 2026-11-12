const LEGACY_CACHE_PREFIX = 'omnilearn-shell-';

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key.startsWith(LEGACY_CACHE_PREFIX))
          .map((oldKey) => caches.delete(oldKey))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Security rule: NEVER intercept or cache /api/* responses, auth cookies, or slide JPEGs
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // Online-only policy: do not serve cached offline fallback responses
  event.respondWith(
    fetch(request).catch(() =>
      new Response('Internet connection required to use OmniLearn AI.', {
        status: 503,
        statusText: 'Service Unavailable',
        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      })
    )
  );
});
