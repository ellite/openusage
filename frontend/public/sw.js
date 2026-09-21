const CACHE_NAME = 'openusage-v1';
const STATIC_ASSETS = [
  '/',
  '/settings',
  '/manifest.json',
  '/favicon.svg',
  '/pwa-192x192.png',
  '/pwa-512x512.png',
  '/apple-touch-icon.png',
  '/fonts/InterVariable.woff2',
  '/icons/claude.svg',
  '/icons/gemini.svg',
  '/icons/deepseek.svg',
  '/icons/copilot-black.svg',
  '/icons/copilot-white.svg',
  '/icons/openai-black.svg',
  '/icons/openai-white.svg',
];

// Install: precache app shell and static assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch(() => {});
    }).then(() => self.skipWaiting())
  );
});

// Activate: clean up older cache versions and claim clients
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch: Network-First strategy (attempts live fetch first; falls back to cache when offline)
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const responseClone = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseClone);
          });
        }
        return networkResponse;
      })
      .catch(async () => {
        const cachedResponse = await caches.match(event.request);
        if (cachedResponse) {
          return cachedResponse;
        }

        if (event.request.mode === 'navigate') {
          const appShell = await caches.match('/');
          if (appShell) return appShell;
        }

        return new Response(
          JSON.stringify({ configured: true, status: 'error', error: 'Offline mode' }),
          {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      })
  );
});

self.addEventListener('push', (event) => {
  let data = { title: 'OpenUsage', body: '' };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch {
    if (event.data) data.body = event.data.text();
  }

  event.waitUntil(
    self.registration.showNotification(data.title || 'OpenUsage', {
      body: data.body || '',
      data: { url: data.url || '/' },
      icon: '/favicon.svg',
      badge: '/favicon.svg',
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  let target = new URL('/', self.location.origin);
  try {
    const requested = new URL(event.notification.data?.url || '/', self.location.origin);
    if (requested.origin === self.location.origin) target = requested;
  } catch {
    // Old notifications and malformed destinations fall back to the dashboard.
  }
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clients) {
      if (new URL(client.url).origin !== self.location.origin) continue;
      try {
        const navigated = await client.navigate(target.href);
        if (navigated) return await navigated.focus();
      } catch {
        // A closing window should not prevent opening the requested card.
      }
    }
    return self.clients.openWindow(target.href);
  })());
});
