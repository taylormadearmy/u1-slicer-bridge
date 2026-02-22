// Minimal service worker for PWA install + Share Target support.
// Keep request handling conservative to avoid interfering with upload POSTs.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Never proxy non-GET requests through the service worker.
  // Some browsers can stall multipart/form-data upload streams when POST is
  // intercepted, even when we just forward to fetch().
  if (req.method !== 'GET') return;

  // Ignore cross-origin traffic.
  if (url.origin !== self.location.origin) return;

  event.respondWith(fetch(req));
});
