// ============================================================
// VendiYA - Service Worker
// Soporta: caché offline + sincronización de ventas pendientes
// ============================================================

const CACHE_NAME = 'vendiya-v3';

// Assets que se cachean al instalar
const URLS_TO_CACHE = [
  '/',
  '/pos',
  '/productos',
  '/inventario',
  '/ventas',
  '/reportes',
  '/asistente',
  '/clientes',
  '/caja',
  '/static/css/estilo.css',
  '/static/app.js',
  '/static/pwa.js',
  '/static/favicon.png',
  '/static/favicon-32x32.png',
  '/static/android-chrome-192x192.png',
  'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css',
  'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css',
  'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js'
];

// ============================================================
// INSTALL: cachear assets estáticos
// ============================================================
self.addEventListener('install', (event) => {
  console.log('[SW] Instalando...');
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[SW] Cacheando assets...');
      return cache.addAll(URLS_TO_CACHE).catch((err) => {
        console.warn('[SW] Algunos assets fallaron al cachear:', err);
      });
    }).then(() => self.skipWaiting())
  );
});

// ============================================================
// ACTIVATE: limpiar cachés viejos
// ============================================================
self.addEventListener('activate', (event) => {
  console.log('[SW] Activando...');
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => {
            console.log('[SW] Borrando caché viejo:', name);
            return caches.delete(name);
          })
      );
    }).then(() => self.clients.claim())
  );
});

// ============================================================
// FETCH: estrategia de caché
// Network-first para HTML, Cache-first para assets
// ============================================================
self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // No interceptar peticiones a la API de ventas (necesitan red)
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // No interceptar POST (las ventas van por API y se manejan con sync)
  if (request.method !== 'GET') {
    return;
  }

  // Para navegación (HTML): Network-first, fallback a cache
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Guardar copia en caché
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(request, responseClone);
          });
          return response;
        })
        .catch(() => {
          // Sin red: servir del cache
          console.log('[SW] Offline, sirviendo del caché:', request.url);
          return caches.match(request).then((cached) => {
            return cached || caches.match('/pos');
          });
        })
    );
    return;
  }

  // Archivos propios (/static): red primero para recibir las actualizaciones,
  // con el caché como respaldo sin conexión
  if (url.origin === self.location.origin) {
    event.respondWith(
      fetch(request).then((response) => {
        if (response && response.status === 200) {
          const copia = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copia));
        }
        return response;
      }).catch(() => caches.match(request))
    );
    return;
  }

  // Librerías externas (CDN): caché primero
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) {
        return cached;
      }
      return fetch(request).then((response) => {
        if (response && response.status === 200) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(request, responseClone);
          });
        }
        return response;
      });
    })
  );
});


// ============================================================
// SYNC: sincronizar ventas pendientes cuando vuelve internet
// ============================================================
// Al cerrar sesión o cambiar de cuenta se descartan las páginas guardadas (pueden ser de otro negocio)
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'LIMPIAR_CACHE') {
    event.waitUntil(caches.delete(CACHE_NAME));
  }
});

self.addEventListener('sync', (event) => {
  console.log('[SW] Sync event:', event.tag);

  if (event.tag === 'sync-ventas') {
    event.waitUntil(sincronizarVentasPendientes());
  }
});

async function sincronizarVentasPendientes() {
  console.log('[SW] Sincronizando ventas pendientes...');
  // La lógica está en la página principal, aquí solo notificamos
  const clients = await self.clients.matchAll();
  clients.forEach((client) => {
    client.postMessage({ type: 'SYNC_VENTAS' });
  });

}
