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
          if (event.data && event.data.type === 'SYNC_VENTAS') {
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
  actualizarChip();
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
    request.onsuccess = () => { actualizarChip(); resolve(request.result); };
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
    request.onsuccess = () => { actualizarChip(); resolve(); };
    request.onerror = () => reject(request.error);
  });
}

// Una venta que el servidor rechaza (p. ej. el producto ya no existe) se conserva
// marcada, para avisar al usuario en vez de reintentarla para siempre.
async function marcarRechazada(id, error) {
  const db = await abrirDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction([STORE_NAME], 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    const get = store.get(id);
    get.onsuccess = () => {
      const rec = get.result;
      if (!rec) return resolve();
      rec.rechazada = true;
      rec.error = error;
      store.put(rec);
    };
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// En un mismo dispositivo pueden entrar negocios distintos: cada venta en cola solo
// pertenece (y solo se envía) al negocio con el que se hizo.
function negocioActual() {
  const meta = document.querySelector('meta[name="negocio-id"]');
  return meta ? meta.content : '';
}
const esDeEsteNegocio = (v) => !v.negocio_id || String(v.negocio_id) === negocioActual();

// ---------- Indicador de ventas por sincronizar ----------
async function actualizarChip() {
  try {
    const pendientes = (await obtenerVentasPendientes()).filter((v) => !v.rechazada && esDeEsteNegocio(v));
    const chip = document.getElementById('chip-pendientes');
    if (chip) {
      chip.hidden = pendientes.length === 0;
      const n = chip.querySelector('[data-n]');
      if (n) n.textContent = pendientes.length;
    }
    const texto = document.getElementById('offline-texto');
    if (texto) {
      texto.textContent = pendientes.length
        ? `Sin conexión · ${pendientes.length} venta(s) pendiente(s) de sincronizar`
        : 'Sin conexión · las ventas se guardarán y sincronizarán al volver';
    }
  } catch (e) { /* IndexedDB no disponible */ }
}

// ---------- Sincronizar ventas pendientes ----------
let sincronizando = false;

async function sincronizarVentasPendientes() {
  if (!navigator.onLine || sincronizando) return;
  sincronizando = true;
  let enviadas = 0;
  try {
    const todas = (await obtenerVentasPendientes()).filter(esDeEsteNegocio);
    const pendientes = todas.filter((v) => !v.rechazada);

    if (pendientes.length) {
      console.log(`[PWA] Sincronizando ${pendientes.length} ventas pendientes...`);
      // El token de la página puede ser de una sesión anterior: se pide uno vigente
      const tk = await fetch('/api/csrf', { credentials: 'same-origin' });
      if (tk.ok) {
        const token = (await tk.json()).token;
        for (const venta of pendientes) {
          let res, data = null;
          try {
            res = await fetch('/api/venta', {
              method: 'POST',
              credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
              body: JSON.stringify({
                items: venta.items, pagos: venta.pagos, descuento: venta.descuento || 0,
                uuid: venta.uuid, fecha_local: venta.fecha_local, offline: true,
                negocio_id: venta.negocio_id
              })
            });
          } catch (err) {
            console.warn('[PWA] Sin red al sincronizar:', err);
            break;
          }
          try { data = await res.json(); } catch (e) { /* respuesta sin JSON */ }

          if (res.ok && data && data.ok) {
            await eliminarVentaPendiente(venta.id);   // el servidor la deduplica por uuid
            enviadas++;
          } else if (res.status === 400 && data && data.error) {
            await marcarRechazada(venta.id, data.error);
          } else {
            break;   // sesión vencida o error del servidor: se reintenta más tarde
          }
        }
      }
    }

    const rechazadas = (await obtenerVentasPendientes()).filter((v) => v.rechazada && esDeEsteNegocio(v));
    actualizarChip();
    if (enviadas || rechazadas.length) {
      window.dispatchEvent(new CustomEvent('vendiya:sync', { detail: { enviadas, rechazadas } }));
    }
  } catch (err) {
    console.warn('[PWA] Error en la sincronización:', err);
  } finally {
    sincronizando = false;
  }
}

// ---------- Exponer funciones globalmente ----------
window.VendiYA = {
  guardarVentaPendiente,
  obtenerVentasPendientes,
  eliminarVentaPendiente,
  sincronizarVentasPendientes
};

// Verificar estado al cargar
document.addEventListener('DOMContentLoaded', actualizarEstadoConexion);
