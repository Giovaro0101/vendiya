// ============================================================
// VendiYA - PWA Logic
// Registro del Service Worker + Cola de ventas offline
// ============================================================

// ---------- Registrar Service Worker ----------
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/service-worker.js')
      .then((reg) => {
        console.log('[PWA] Service Worker registrado:', reg.scope);

        // Escuchar mensajes del Service Worker
        navigator.serviceWorker.addEventListener('message', (event) => {
          if (event.data.type === 'SYNC_VENTAS') {
            console.log('[PWA] SW pide sincronizar ventas');
            sincronizarVentasPendientes();
          }
        });
      })
      .catch((err) => console.error('[PWA] Error registrando SW:', err));
  });
}

// ---------- Detección de conexión ----------
function actualizarEstadoConexion() {
  const online = navigator.onLine;
  const banner = document.getElementById('offline-banner');
  if (banner) {
    banner.style.display = online ? 'none' : 'block';
  }
  console.log('[PWA] Estado de conexión:', online ? 'online' : 'offline');

  // Si volvió internet, sincronizar pendientes
  if (online) {
    sincronizarVentasPendientes();
  }
}

window.addEventListener('online', actualizarEstadoConexion);
window.addEventListener('offline', actualizarEstadoConexion);

// ---------- IndexedDB para ventas pendientes ----------
const DB_NAME = 'vendiya-offline';
const DB_VERSION = 1;
const STORE_NAME = 'ventas-pendientes';

function abrirDB() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = (event) => {
      const db = event.target.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function guardarVentaPendiente(venta) {
  const db = await abrirDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction([STORE_NAME], 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    venta.timestamp = Date.now();
    venta.sincronizada = false;
    const request = store.add(venta);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function obtenerVentasPendientes() {
  const db = await abrirDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction([STORE_NAME], 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const request = store.getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function eliminarVentaPendiente(id) {
  const db = await abrirDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction([STORE_NAME], 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    const request = store.delete(id);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
}

// ---------- Sincronizar ventas pendientes ----------
async function sincronizarVentasPendientes() {
  if (!navigator.onLine) return;

  const pendientes = await obtenerVentasPendientes();
  if (pendientes.length === 0) return;

  console.log(`[PWA] Sincronizando ${pendientes.length} ventas pendientes...`);

  for (const venta of pendientes) {
    try {
      const res = await fetch('/api/venta', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: venta.items, pagos: venta.pagos })
      });

      if (res.ok) {
        console.log(`[PWA] Venta ${venta.id} sincronizada`);
        await eliminarVentaPendiente(venta.id);
      }
    } catch (err) {
      console.warn(`[PWA] Error sincronizando venta ${venta.id}:`, err);
    }
  }
}

// ---------- Exponer funciones globalmente ----------
window.VendiYA = {
  guardarVentaPendiente,
  obtenerVentasPendientes,
  sincronizarVentasPendientes
};

// Verificar estado al cargar
document.addEventListener('DOMContentLoaded', actualizarEstadoConexion);