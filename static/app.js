// ============================================================
// VendiYA - utilidades compartidas por todas las páginas
// (token CSRF, notificaciones, confirmaciones, protección de formularios)
// ============================================================
(function () {
  'use strict';

  // ---------- CSRF ----------
  window.csrfToken = function () {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  };

  window.apiFetch = function (url, opciones) {
    const opts = Object.assign({ credentials: 'same-origin' }, opciones || {});
    opts.headers = Object.assign({ 'X-CSRF-Token': window.csrfToken() }, opts.headers || {});
    return fetch(url, opts);
  };

  // Cualquier formulario POST recibe su token aunque la plantilla lo olvide
  function protegerFormularios() {
    document.querySelectorAll('form').forEach((f) => {
      if ((f.getAttribute('method') || '').toLowerCase() !== 'post') return;
      if (f.querySelector('input[name="csrf_token"]')) return;
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'csrf_token';
      input.value = window.csrfToken();
      f.appendChild(input);
    });
  }

  // ---------- Toasts ----------
  // tipo: 'exito' | 'error' | 'advertencia'. Los errores no se cierran solos.
  const TOAST_TIPOS = {
    exito:       { clase: 'text-bg-success', icono: 'bi-check-circle-fill',         auto: 3500 },
    error:       { clase: 'text-bg-danger',  icono: 'bi-x-circle-fill',             auto: 0 },
    advertencia: { clase: 'toast-naranja',   icono: 'bi-exclamation-triangle-fill', auto: 7000 },
  };

  function contenedorToasts() {
    let c = document.getElementById('toastContainer');
    if (!c) {
      c = document.createElement('div');
      c.id = 'toastContainer';
      c.className = 'toast-container position-fixed top-0 end-0 p-3';
      c.style.zIndex = 1100;
      document.body.appendChild(c);
    }
    return c;
  }

  // opciones.acciones: [{ texto, fn }] botones dentro del aviso
  window.mostrarToast = function (mensaje, tipo, opciones) {
    const cfg = TOAST_TIPOS[tipo] || TOAST_TIPOS.error;
    const acciones = (opciones && opciones.acciones) || [];

    const el = document.createElement('div');
    el.className = `toast align-items-center border-0 ${cfg.clase}`;
    el.setAttribute('role', tipo === 'error' ? 'alert' : 'status');
    el.setAttribute('aria-live', tipo === 'error' ? 'assertive' : 'polite');
    el.setAttribute('aria-atomic', 'true');

    const flex = document.createElement('div');
    flex.className = 'd-flex';

    const body = document.createElement('div');
    body.className = 'toast-body d-flex align-items-start gap-2';
    const icono = document.createElement('i');
    icono.className = `bi ${cfg.icono}`;
    const texto = document.createElement('span');
    texto.textContent = mensaje;
    body.append(icono, texto);

    acciones.forEach((a) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-sm btn-light ms-2 flex-shrink-0 align-self-center';
      b.textContent = a.texto;
      b.addEventListener('click', () => { a.fn(); bootstrap.Toast.getOrCreateInstance(el).hide(); });
      body.appendChild(b);
    });

    const cerrar = document.createElement('button');
    cerrar.type = 'button';
    cerrar.className = 'btn-close btn-close-white me-2 m-auto';
    cerrar.setAttribute('data-bs-dismiss', 'toast');
    cerrar.setAttribute('aria-label', 'Cerrar');

    flex.append(body, cerrar);
    el.appendChild(flex);
    contenedorToasts().appendChild(el);

    const toast = new bootstrap.Toast(el, { autohide: cfg.auto > 0, delay: cfg.auto });
    el.addEventListener('hidden.bs.toast', () => el.remove());
    toast.show();
  };

  // ---------- Confirmación (reemplaza a confirm()) ----------
  function modalConfirmar() {
    let el = document.getElementById('modalConfirmar');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'modalConfirmar';
    el.className = 'modal fade';
    el.tabIndex = -1;
    el.innerHTML =
      '<div class="modal-dialog modal-dialog-centered modal-sm"><div class="modal-content">' +
      '<div class="modal-body p-4"><h6 class="mb-2" data-t></h6><p class="text-muted mb-4" data-m></p>' +
      '<div class="d-flex gap-2 justify-content-end">' +
      '<button type="button" class="btn btn-light" data-bs-dismiss="modal">Cancelar</button>' +
      '<button type="button" class="btn btn-danger" data-ok></button></div></div></div></div>';
    document.body.appendChild(el);
    return el;
  }

  window.confirmar = function (mensaje, opciones) {
    const o = Object.assign({ titulo: '¿Estás seguro?', textoOk: 'Confirmar', clase: 'btn-danger' }, opciones || {});
    return new Promise((resolve) => {
      const el = modalConfirmar();
      el.querySelector('[data-t]').textContent = o.titulo;
      el.querySelector('[data-m]').textContent = mensaje;
      const ok = el.querySelector('[data-ok]');
      ok.textContent = o.textoOk;
      ok.className = `btn ${o.clase}`;
      const modal = bootstrap.Modal.getOrCreateInstance(el);
      let aceptado = false;
      const alAceptar = () => { aceptado = true; modal.hide(); };
      ok.addEventListener('click', alAceptar);
      el.addEventListener('hidden.bs.modal', () => {
        ok.removeEventListener('click', alAceptar);
        resolve(aceptado);
      }, { once: true });
      modal.show();
    });
  };

  // Cerrar sesión: el service worker olvida las páginas guardadas de esta cuenta
  document.addEventListener('submit', (e) => {
    const action = e.target.getAttribute('action') || '';
    if (/\/(plataforma\/)?logout$/.test(action) && navigator.serviceWorker && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: 'LIMPIAR_CACHE' });
    }
  });

  // <form data-confirm="¿Eliminar?"> pide confirmación antes de enviar
  document.addEventListener('submit', (e) => {
    const f = e.target;
    if (f.dataset.confirm && f.dataset.confirmado !== '1') {
      e.preventDefault();
      window.confirmar(f.dataset.confirm, {
        titulo: f.dataset.confirmTitulo || '¿Estás seguro?',
        textoOk: f.dataset.confirmOk || 'Confirmar',
      }).then((ok) => {
        if (!ok) return;
        f.dataset.confirmado = '1';
        if (f.requestSubmit) f.requestSubmit(e.submitter || undefined); else f.submit();
      });
      return;
    }
    // Evita envíos dobles (dos clics seguidos = dos registros)
    setTimeout(() => {
      f.querySelectorAll('button[type="submit"], button:not([type])').forEach((b) => { b.disabled = true; });
    }, 0);
  });
  window.addEventListener('pageshow', (e) => {
    if (e.persisted) {
      document.querySelectorAll('form button:disabled').forEach((b) => { b.disabled = false; });
      document.querySelectorAll('form[data-confirmado]').forEach((f) => { delete f.dataset.confirmado; });
    }
  });

  // ---------- Resultado de la sincronización offline ----------
  window.addEventListener('vendiya:sync', (e) => {
    const { enviadas, rechazadas } = e.detail;
    if (enviadas) {
      window.mostrarToast(
        enviadas === 1 ? 'Se sincronizó 1 venta guardada sin conexión'
                       : `Se sincronizaron ${enviadas} ventas guardadas sin conexión`, 'exito');
    }
    rechazadas.forEach((r) => {
      window.mostrarToast(
        `Una venta hecha sin conexión fue rechazada (${r.resumen || 'sin detalle'}): ${r.error}. ` +
        'Regístrala de nuevo manualmente.', 'error',
        { acciones: [{ texto: 'Entendido', fn: () => window.VendiYA.eliminarVentaPendiente(r.id) }] });
    });
  });

  document.addEventListener('DOMContentLoaded', protegerFormularios);
})();
