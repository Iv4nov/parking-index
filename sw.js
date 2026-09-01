// Кэширует статичную оболочку (HTML/CSS/иконки), не данные API — зоны и
// индекс всегда должны запрашиваться свежими, кэшировать их было бы неверно
// с точки зрения продукта: устаревший индекс хуже, чем отсутствие индекса.
const SHELL_CACHE = 'parking-shell-v1';
const SHELL_FILES = ['index.html', 'manifest.json', 'icons/icon-192.png', 'icons/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Только собственная оболочка — не трогаем запросы к API бэкенда или к
  // сторонним картам/шрифтам, они должны идти в сеть напрямую.
  if (SHELL_FILES.some((f) => url.pathname.endsWith(f))) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});
