// Minimal service worker for PWA install + Share Target support.
// Network-first strategy — always fetch from server, no offline caching.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());

self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
