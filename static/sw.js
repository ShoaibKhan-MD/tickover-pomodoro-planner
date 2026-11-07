/* Tickover PWA shell. API and documentation requests are never cached. */
const CACHE = 'tickover-shell-v1.3.0';
const ROOT = new URL('./', self.registration.scope).pathname;
const SHELL = [ROOT, ROOT + 'manifest.webmanifest', ROOT + 'static/icons/icon-192.png', ROOT + 'static/icons/icon-512.png'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== 'GET') return;
  if (url.pathname.startsWith(ROOT + 'api/') || url.pathname.startsWith(ROOT + 'docs') || url.pathname.startsWith(ROOT + 'redoc')) return;
  if (url.pathname === ROOT || url.pathname.startsWith(ROOT + 'static/')) {
    event.respondWith(fetch(event.request).then(response => {
      if (response.ok) caches.open(CACHE).then(cache => cache.put(event.request, response.clone()));
      return response;
    }).catch(() => caches.match(event.request).then(cached => cached || caches.match(ROOT))));
  }
});
