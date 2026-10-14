import os
import io
import re
import csv
import math
import hmac
import time
import sqlite3
import secrets
import logging
import threading
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for, session, jsonify,
                   flash, g, Response, send_from_directory, abort)
from werkzeug.security import check_password_hash, generate_password_hash
from flask_apscheduler import APScheduler
import requests
from bs4 import BeautifulSoup
import urllib3

import database
import plataforma
from database import (get_db, get_db_unificado, archivar_ventas_antiguas,
                      hacer_backup, listar_backups, hay_backup_de_hoy)
from rag import preguntar as rag_preguntar

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ============================================================
# Configuración y arranque seguro
# ============================================================
def _cargar_secret_key():
    """Clave de sesión: variable de entorno, o un archivo local generado la primera vez."""
    desde_entorno = os.environ.get('VENDIYA_SECRET_KEY')
    if desde_entorno:
        return desde_entorno
    ruta = os.path.join(BASE_DIR, '.secret_key')
    if os.path.exists(ruta):
        with open(ruta, 'r', encoding='utf-8') as f:
            clave = f.read().strip()
        if clave:
            return clave
    clave = secrets.token_hex(32)
    with open(ruta, 'w', encoding='utf-8') as f:
        f.write(clave)
    return clave


app = Flask(__name__)
app.secret_key = _cargar_secret_key()
# Opciones de la plataforma (variables de entorno)
REGISTRO_PUBLICO = os.environ.get('VENDIYA_REGISTRO_PUBLICO') == '1'    # permite que cualquiera cree su negocio
DIAS_PRUEBA = int(os.environ.get('VENDIYA_DIAS_PRUEBA', '14'))
SOPORTE = os.environ.get('VENDIYA_SOPORTE', '')                          # ej. "WhatsApp 0414-0000000"

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get('VENDIYA_HTTPS') == '1',        # cookie solo por HTTPS en producción
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

if os.environ.get('VENDIYA_PROXY') == '1':
    # Detrás de nginx/Caddy: toma la IP y el protocolo reales de los encabezados X-Forwarded-*
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

if not app.debug:
    _handler = RotatingFileHandler(os.path.join(BASE_DIR, 'vendiya.log'),
                                   maxBytes=512 * 1024, backupCount=3, encoding='utf-8')
    _handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    app.logger.addHandler(_handler)
    app.logger.setLevel(logging.INFO)

scheduler = APScheduler()

METODOS_PAGO = {
    'efectivo': 'Efectivo ($)',
    'efectivo_bs': 'Efectivo (Bs)',
    'pago_movil': 'Pago móvil',
    'divisas': 'Divisas ($)',
    'binance': 'Binance',
    'tarjeta': 'Tarjeta',
}
METODOS_EFECTIVO = ('efectivo', 'efectivo_bs')
CATEGORIAS_GASTO = ['Mercancía', 'Alquiler', 'Servicios', 'Sueldos', 'Transporte',
                    'Mantenimiento', 'Impuestos', 'Otros']
TIPOS_AJUSTE = ('entrada', 'salida', 'ajuste')


def ahora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ErrorNegocio(Exception):
    """Dato inválido o regla de negocio incumplida; el mensaje se muestra al usuario."""


def a_numero(valor, campo, minimo=0.0, maximo=1_000_000_000, defecto=None):
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        if defecto is not None:
            return defecto
        raise ErrorNegocio(f"{campo} es obligatorio")
    try:
        n = float(str(valor).strip().replace(',', '.'))
    except (TypeError, ValueError):
        raise ErrorNegocio(f"{campo} no es un número válido")
    if not math.isfinite(n):
        raise ErrorNegocio(f"{campo} no es un número válido")
    if n < minimo or n > maximo:
        raise ErrorNegocio(f"{campo} debe estar entre {minimo:g} y {maximo:g}")
    return n


def a_entero(valor, campo):
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ErrorNegocio(f"{campo} no es válido")


def es_api():
    return request.path.startswith('/api/')


# ============================================================
# Seguridad: CSRF, sesión, roles
# ============================================================
def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


@app.before_request
def verificar_csrf():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        enviado = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or ''
        esperado = session.get('_csrf', '')
        if not esperado or not hmac.compare_digest(enviado, esperado):
            app.logger.warning("CSRF rechazado en %s desde %s", request.path, request.remote_addr)
            if es_api():
                return jsonify({"ok": False, "error": "La sesión expiró. Recarga la página."}), 400
            flash("La sesión expiró. Intenta de nuevo.", "warning")
            return redirect(url_for('login'))


@app.after_request
def cabeceras_seguridad(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    return resp


_cache_negocio = {}        # id -> (expira, datos); se consulta la BD central como mucho cada 20 s


def _negocio_vigente(nid):
    """Datos del negocio si existe y puede usar el sistema; None si no."""
    ahora_ts = time.time()
    hit = _cache_negocio.get(nid)
    if hit and hit[0] > ahora_ts:
        return hit[1]
    n = plataforma.obtener_negocio(nid)
    datos = n if n and n['estado'] == 'activo' else None
    if n and not datos:
        datos = {'_bloqueado': n['estado'], 'vence': n['vence']}
    _cache_negocio[nid] = (ahora_ts + 20, datos)
    return datos


def _mensaje_bloqueo(estado, vence=None):
    contacto = f" Contacta a soporte: {SOPORTE}." if SOPORTE else " Contacta a soporte."
    if estado == 'vencido':
        return f"La suscripción de este negocio venció el {vence}.{contacto}"
    return "Esta cuenta está suspendida." + contacto


@app.before_request
def fijar_negocio():
    """Cada petición trabaja únicamente con la base del negocio de su sesión."""
    nid = session.get('negocio_id')
    g.negocio = None
    if nid is None:
        database.set_negocio(None)
        return
    n = _negocio_vigente(nid)
    if not n or n.get('_bloqueado'):
        session.clear()                     # primero limpiar: clear() también borra los avisos pendientes
        if n:
            flash(_mensaje_bloqueo(n['_bloqueado'], n.get('vence')), "danger")
        database.set_negocio(None)
        return
    database.set_negocio(nid)
    g.negocio = n


@app.teardown_request
def liberar_negocio(exc):
    database.set_negocio(None)


def _usuario_actual():
    """Usuario de la sesión validado contra la BD (cuenta activa y rol vigente)."""
    if 'usuario_id' not in session or database.negocio_actual() is None:
        return None
    if hasattr(g, '_usuario'):
        return g._usuario
    conn = get_db()
    u = conn.execute("SELECT id, nombre, rol, activo FROM usuarios WHERE id = ?",
                     (session['usuario_id'],)).fetchone()
    conn.close()
    if not u or u['activo'] == 0:
        session.clear()
        u = None
    else:
        session['rol'] = u['rol']
        session['usuario_nombre'] = u['nombre']
    g._usuario = u
    return u


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _usuario_actual():
            if es_api():
                return jsonify({"ok": False, "error": "Sesión expirada. Inicia sesión de nuevo."}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get('rol') != 'admin':
            if es_api():
                return jsonify({"ok": False, "error": "No tienes permiso para esto"}), 403
            flash("Esta sección es solo para administradores", "danger")
            return redirect(url_for('pos'))
        return f(*args, **kwargs)
    return wrapper


@app.errorhandler(ErrorNegocio)
def manejar_error_negocio(e):
    if es_api():
        return jsonify({"ok": False, "error": str(e)}), 400
    flash(str(e), "danger")
    if request.method == 'GET':
        return redirect(url_for('pos'))
    return redirect(request.referrer or url_for('pos'))


@app.errorhandler(plataforma.ErrorPlataforma)
def manejar_error_plataforma(e):
    return manejar_error_negocio(e)


@app.errorhandler(404)
def pagina_no_encontrada(e):
    if es_api():
        return jsonify({"ok": False, "error": "No encontrado"}), 404
    return render_template('error.html', codigo=404, mensaje="No encontramos esa página."), 404


@app.errorhandler(500)
def error_interno(e):
    app.logger.exception("Error interno en %s", request.path)
    if es_api():
        return jsonify({"ok": False, "error": "Error interno del servidor"}), 500
    return render_template('error.html', codigo=500,
                           mensaje="Algo salió mal. El error quedó registrado."), 500


@app.context_processor
def inyectar_globales():
    ctx = {
        'csrf_token': csrf_token,
        'es_admin': session.get('rol') == 'admin',
        'METODOS_PAGO': METODOS_PAGO,
        'negocio': {'nombre_negocio': 'VendiYA'},
        'alerta_stock': 0,
        'pass_default': session.get('pass_default', False),
        'REGISTRO_PUBLICO': REGISTRO_PUBLICO,
        'SOPORTE': SOPORTE,
        'es_superadmin': bool(session.get('superadmin_id')),
        'suscripcion': None,
        'negocio_slug': None,
    }
    if g.get('negocio'):
        ctx['negocio_slug'] = g.negocio['slug']
        ctx['suscripcion'] = {'dias': g.negocio['dias_restantes'], 'vence': g.negocio['vence'],
                              'plan': g.negocio['plan']}
    if 'usuario_id' in session and database.negocio_actual() is not None:
        conn = get_db()
        cfg = conn.execute("SELECT * FROM configuracion WHERE id = 1").fetchone()
        if cfg:
            ctx['negocio'] = dict(cfg)
        if ctx['es_admin']:
            ctx['alerta_stock'] = conn.execute(
                "SELECT COUNT(*) FROM productos WHERE activo = 1 AND stock_actual <= stock_minimo"
            ).fetchone()[0]
        conn.close()
    return ctx


# ============================================================
# Tasa de cambio
# ============================================================
def obtener_tasa_bcv():
    try:
        headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36')
        }
        # El sitio del BCV suele tener problemas con su certificado; por eso verify=False
        response = requests.get("https://www.bcv.org.ve/", headers=headers,
                                verify=False, timeout=10)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        dolar_div = soup.find("div", {"id": "dolar"})
        strong = dolar_div.find("strong") if dolar_div else None
        if not strong:
            logging.warning("No se encontró la tasa en la página del BCV")
            return None
        valor = float(strong.text.strip().replace(".", "").replace(",", "."))
        return valor if 0 < valor < 10_000_000 else None
    except Exception as e:
        logging.warning("Error al obtener la tasa de cambio: %s", e)
        return None


def guardar_tasa(valor, fuente):
    conn = get_db()
    fecha = datetime.now().strftime("%Y-%m-%d")
    conn.execute("DELETE FROM tasa_cambio WHERE fecha = ?", (fecha,))
    conn.execute("INSERT INTO tasa_cambio (fecha, valor, fuente) VALUES (?,?,?)",
                 (fecha, valor, fuente))
    conn.commit()
    conn.close()


def actualizar_tasa_diaria(forzar=False):
    """Trae la tasa del BCV. No pisa una tasa manual de hoy salvo que se fuerce."""
    if not forzar:
        conn = get_db()
        manual = conn.execute(
            "SELECT 1 FROM tasa_cambio WHERE fecha = ? AND fuente = 'Manual'",
            (datetime.now().strftime("%Y-%m-%d"),)).fetchone()
        conn.close()
        if manual:
            return False
    valor = obtener_tasa_bcv()
    if not valor:
        logging.warning("No se pudo obtener la tasa de cambio; se mantiene la anterior")
        return False
    plataforma.guardar_tasa_global(valor)
    guardar_tasa(valor, 'BCV')
    logging.info("Tasa de cambio actualizada: %s Bs/USD", valor)
    return True


def actualizar_tasa_global():
    """Trabajo programado: una sola consulta al BCV y la tasa se reparte a todos los negocios."""
    valor = obtener_tasa_bcv()
    if not valor:
        logging.warning("No se pudo obtener la tasa de cambio; se mantienen las anteriores")
        return False
    plataforma.guardar_tasa_global(valor)
    plataforma.distribuir_tasa(valor)
    logging.info("Tasa global actualizada: %s Bs/USD", valor)
    return True


def obtener_tasa_info():
    conn = get_db()
    row = conn.execute(
        "SELECT valor, fecha, fuente FROM tasa_cambio ORDER BY fecha DESC, id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def obtener_tasa_actual():
    info = obtener_tasa_info()
    return info['valor'] if info else None


# ============================================================
# Login
# ============================================================
_intentos_login = {}
MAX_INTENTOS = 5
BLOQUEO_SEGUNDOS = 300


def _clave_intento(texto):
    return f"{request.remote_addr}|{texto.lower()}"


_HASH_FALSO = generate_password_hash('no-existe-' + secrets.token_hex(4))


@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        slug = request.form.get('negocio', '').strip().lower()
        usuario = request.form.get('usuario', '').strip()
        password = request.form.get('password', '')
        clave = _clave_intento(f"{slug}|{usuario}")
        fallos, bloqueado_hasta = _intentos_login.get(clave, (0, 0))

        if bloqueado_hasta > time.time():
            minutos = math.ceil((bloqueado_hasta - time.time()) / 60)
            flash(f"Demasiados intentos fallidos. Espera {minutos} min e intenta de nuevo.", "danger")
            return render_template('login.html', negocio_codigo=slug)

        neg = plataforma.obtener_por_slug(slug)
        user = None
        if neg and neg['estado_guardado'] != 'eliminado':
            with database.negocio(neg['id']):
                conn = get_db()
                user = conn.execute("SELECT * FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
                conn.close()

        if user and user['activo'] != 0 and check_password_hash(user['password_hash'], password):
            # Credenciales correctas: ahora sí se informa si la cuenta está bloqueada
            if neg['estado'] != 'activo':
                flash(_mensaje_bloqueo(neg['estado'], neg['vence']), "danger")
                return render_template('login.html', negocio_codigo=slug)
            _intentos_login.pop(clave, None)
            session.clear()
            session.permanent = True
            session['negocio_id'] = neg['id']
            session['usuario_id'] = user['id']
            session['usuario_nombre'] = user['nombre']
            session['rol'] = user['rol']
            session['pass_default'] = (password == 'admin123')
            csrf_token()
            plataforma.registrar_acceso(neg['id'])
            return redirect(url_for('pos'))

        if not user:
            check_password_hash(_HASH_FALSO, password)      # mismo tiempo de respuesta exista o no
        fallos += 1
        _intentos_login[clave] = (fallos, time.time() + BLOQUEO_SEGUNDOS if fallos >= MAX_INTENTOS else 0)
        app.logger.warning("Login fallido (negocio '%s', usuario '%s') desde %s", slug, usuario, request.remote_addr)
        flash("Negocio, usuario o contraseña incorrectos", "danger")
        return render_template('login.html', negocio_codigo=slug)
    return render_template('login.html', negocio_codigo=request.args.get('n', '').strip().lower())


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/csrf')
@login_required
def api_csrf():
    return jsonify({"ok": True, "token": csrf_token(), "negocio_id": session.get('negocio_id')})


# ============================================================
# Caja (turnos)
# ============================================================
def caja_abierta(conn):
    return conn.execute(
        "SELECT * FROM cajas WHERE estado = 'abierta' ORDER BY id DESC LIMIT 1").fetchone()


def resumen_caja(conn, caja):
    cid = caja['id']
    v = conn.execute("""
        SELECT COUNT(*) AS n, COALESCE(SUM(total),0) AS total,
               COALESCE(SUM(deuda),0) AS credito,
               -- el cambio sale de la gaveta solo hasta lo que esa venta cobró en efectivo
               COALESCE(SUM(MIN(COALESCE(cambio,0), COALESCE((
                   SELECT SUM(p.monto) FROM venta_pagos p
                   WHERE p.venta_id = ventas.id AND p.metodo IN ('efectivo','efectivo_bs')),0))),0) AS cambio
        FROM ventas WHERE caja_id = ? AND estado = 'completada'
    """, (cid,)).fetchone()
    cobros = {r['metodo']: r['m'] for r in conn.execute("""
        SELECT p.metodo, SUM(p.monto) AS m FROM venta_pagos p
        JOIN ventas v ON v.id = p.venta_id
        WHERE v.caja_id = ? AND v.estado = 'completada' GROUP BY p.metodo
    """, (cid,))}
    abonos = {r['metodo']: r['m'] for r in conn.execute(
        "SELECT metodo, SUM(monto) AS m FROM abonos WHERE caja_id = ? GROUP BY metodo", (cid,))}
    gastos = conn.execute(
        "SELECT COALESCE(SUM(monto),0) FROM gastos WHERE caja_id = ? AND pagado_de_caja = 1",
        (cid,)).fetchone()[0]

    metodos = []
    for m in METODOS_PAGO:
        vm, am = cobros.get(m, 0), abonos.get(m, 0)
        if vm or am:
            metodos.append({'metodo': m, 'ventas': vm, 'abonos': am, 'total': vm + am})

    efectivo_in = sum(cobros.get(m, 0) + abonos.get(m, 0) for m in METODOS_EFECTIVO)
    fondo = caja['fondo_inicial'] or 0
    esperado = fondo + efectivo_in - v['cambio'] - gastos
    return {
        'num_ventas': v['n'], 'total_ventas': v['total'], 'cambio': v['cambio'],
        'credito': v['credito'], 'metodos': metodos, 'efectivo_cobrado': efectivo_in,
        'gastos_caja': gastos, 'fondo': fondo, 'esperado': esperado,
    }


@app.route('/caja')
@login_required
def caja():
    conn = get_db()
    abierta = caja_abierta(conn)
    resumen = resumen_caja(conn, abierta) if abierta else None
    historial = conn.execute("""
        SELECT c.*, u.nombre AS usuario FROM cajas c
        LEFT JOIN usuarios u ON u.id = c.usuario_id
        WHERE c.estado = 'cerrada' ORDER BY c.id DESC LIMIT 15
    """).fetchall()
    conn.close()
    return render_template('caja.html', abierta=dict(abierta) if abierta else None,
                           resumen=resumen, historial=[dict(h) for h in historial])


@app.route('/caja/abrir', methods=['POST'])
@login_required
def abrir_caja():
    fondo = a_numero(request.form.get('fondo_inicial'), 'El fondo inicial', defecto=0.0)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if caja_abierta(conn):
            raise ErrorNegocio("Ya hay una caja abierta")
        conn.execute("INSERT INTO cajas (usuario_id, fecha_apertura, fondo_inicial) VALUES (?,?,?)",
                     (session['usuario_id'], ahora(), fondo))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash("Caja abierta. ¡Buen turno!", "success")
    return redirect(url_for('pos'))


@app.route('/caja/cerrar', methods=['POST'])
@login_required
def cerrar_caja():
    contado = a_numero(request.form.get('efectivo_contado'), 'El efectivo contado')
    notas = request.form.get('notas', '').strip()[:300]
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        abierta = caja_abierta(conn)
        if not abierta:
            raise ErrorNegocio("No hay una caja abierta")
        r = resumen_caja(conn, abierta)
        conn.execute("""
            UPDATE cajas SET estado = 'cerrada', fecha_cierre = ?, efectivo_esperado = ?,
                efectivo_contado = ?, diferencia = ?, total_ventas = ?, notas = ?
            WHERE id = ?
        """, (ahora(), round(r['esperado'], 2), contado, round(contado - r['esperado'], 2),
              round(r['total_ventas'], 2), notas, abierta['id']))
        conn.commit()
        cid = abierta['id']
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash("Caja cerrada correctamente", "success")
    return redirect(url_for('caja_detalle', cid=cid))


@app.route('/caja/<int:cid>')
@login_required
def caja_detalle(cid):
    conn = get_db()
    c = conn.execute("""
        SELECT c.*, u.nombre AS usuario FROM cajas c
        LEFT JOIN usuarios u ON u.id = c.usuario_id WHERE c.id = ?
    """, (cid,)).fetchone()
    if not c:
        conn.close()
        abort(404)
    resumen = resumen_caja(conn, c)
    conn.close()
    return render_template('caja_detalle.html', c=dict(c), resumen=resumen)


# ============================================================
# POS
# ============================================================
@app.route('/pos')
@login_required
def pos():
    conn = get_db()
    abierta = caja_abierta(conn)
    conn.close()
    info = obtener_tasa_info()
    tasa_vieja = False
    if info:
        try:
            tasa_vieja = (datetime.now().date() -
                          datetime.strptime(info['fecha'], "%Y-%m-%d").date()).days >= 2
        except ValueError:
            pass
    return render_template('pos.html', tasa_bcv=info['valor'] if info else None,
                           tasa_info=info, tasa_vieja=tasa_vieja,
                           caja_abierta=bool(abierta))


@app.route('/api/productos/buscar')
@login_required
def api_buscar_productos():
    q = request.args.get('q', '').strip()[:80]
    conn = get_db()
    if q:
        condiciones, params = [], [q]
        for palabra in q.split():
            condiciones.append("nombre LIKE ? ESCAPE '\\'")
            escapada = palabra.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            params.append(f"%{escapada}%")
        productos = conn.execute(f"""
            SELECT id, codigo_barras, nombre, precio_venta, stock_actual, iva, unidad_medida
            FROM productos
            WHERE activo = 1 AND (codigo_barras = ? OR ({' AND '.join(condiciones)}))
            ORDER BY (codigo_barras = ?) DESC, nombre LIMIT 24
        """, params + [q]).fetchall()
    else:
        productos = conn.execute("""
            SELECT id, codigo_barras, nombre, precio_venta, stock_actual, iva, unidad_medida
            FROM productos WHERE activo = 1 ORDER BY nombre LIMIT 24
        """).fetchall()
    conn.close()
    return jsonify([dict(p) for p in productos])


def _respuesta_venta(v, duplicada=False):
    r = {
        "ok": True,
        "venta_id": v['id'],
        "numero_factura": v['numero_factura'],
        "subtotal": round(v['subtotal'], 2),
        "iva": round(v['iva'], 2),
        "descuento": round(v['descuento'] or 0, 2),
        "total": round(v['total'], 2),
        "cambio": round(v['cambio'] or 0, 2),
        "deuda": round(v['deuda'] or 0, 2),
        "duplicada": duplicada,
    }
    if session.get('rol') == 'admin':
        r["ganancia"] = round(v['ganancia'] or 0, 2)
    return r


def _fecha_venta(data):
    """Las ventas sincronizadas desde el modo offline conservan la hora en que se hicieron."""
    if data.get('offline'):
        try:
            f = datetime.strptime(str(data.get('fecha_local', '')), "%Y-%m-%d %H:%M:%S")
            if timedelta(minutes=-5) < datetime.now() - f < timedelta(days=30):
                return f
        except ValueError:
            pass
    return datetime.now()


@app.route('/api/venta', methods=['POST'])
@login_required
def api_registrar_venta():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ErrorNegocio("Solicitud inválida")

    if data.get('negocio_id') is not None and str(data['negocio_id']) != str(session.get('negocio_id')):
        raise ErrorNegocio("Esta venta fue hecha en otro negocio y no puede registrarse aquí")

    items_raw = data.get('items') or []
    pagos_raw = data.get('pagos') or []
    fiado = bool(data.get('fiado'))
    offline = bool(data.get('offline'))
    uuid_venta = str(data.get('uuid') or '').strip()[:64] or None

    if not isinstance(items_raw, list) or not isinstance(pagos_raw, list) \
            or not all(isinstance(x, dict) for x in items_raw + pagos_raw):
        raise ErrorNegocio("Solicitud inválida")
    if not items_raw:
        raise ErrorNegocio("El carrito está vacío")
    if not pagos_raw and not fiado:
        raise ErrorNegocio("Debes especificar al menos un método de pago")

    # Cantidades por producto (si un producto viene repetido se suman)
    items = {}
    for it in items_raw:
        pid = a_entero(it.get('producto_id'), 'El producto')
        items[pid] = items.get(pid, 0) + a_numero(it.get('cantidad'), 'La cantidad',
                                                  minimo=0.001, maximo=100000)

    pagos = []
    for p in pagos_raw:
        metodo = p.get('metodo')
        if metodo not in METODOS_PAGO:
            raise ErrorNegocio("Método de pago inválido")
        monto = a_numero(p.get('monto'), 'El monto del pago')
        if monto > 0:
            pagos.append({'metodo': metodo, 'monto': monto})
    descuento = a_numero(data.get('descuento'), 'El descuento', defecto=0.0)

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Idempotencia: si esta venta ya se registró (reintento tras un corte), no se duplica
        if uuid_venta:
            ya = conn.execute("SELECT * FROM ventas WHERE uuid = ?", (uuid_venta,)).fetchone()
            if ya:
                conn.rollback()
                return jsonify(_respuesta_venta(ya, duplicada=True))

        cliente = None
        if fiado:
            cliente = conn.execute("SELECT * FROM clientes WHERE id = ? AND activo = 1",
                                   (a_entero(data.get('cliente_id'), 'El cliente'),)).fetchone()
            if not cliente:
                raise ErrorNegocio("Selecciona un cliente para vender a crédito")

        subtotal = iva_bruto = ganancia_bruta = 0.0
        detalle = []
        for pid, cantidad in items.items():
            prod = conn.execute("SELECT * FROM productos WHERE id = ? AND activo = 1",
                                (pid,)).fetchone()
            if not prod:
                raise ErrorNegocio("Uno de los productos ya no existe o está desactivado")
            # Una venta ya hecha en modo offline se registra aunque el stock no alcance
            if not offline and prod['stock_actual'] < cantidad - 1e-9:
                raise ErrorNegocio(f"Stock insuficiente para {prod['nombre']} "
                                   f"(disponible: {prod['stock_actual']:g})")
            sub = prod['precio_venta'] * cantidad
            subtotal += sub
            iva_bruto += sub * (prod['iva'] / 100)
            ganancia_bruta += (prod['precio_venta'] - prod['precio_compra']) * cantidad
            detalle.append({
                'producto_id': pid, 'nombre': prod['nombre'], 'cantidad': cantidad,
                'precio': prod['precio_venta'], 'precio_compra': prod['precio_compra'],
                'subtotal': sub, 'ganancia': (prod['precio_venta'] - prod['precio_compra']) * cantidad,
            })

        if descuento > subtotal + 1e-9:
            raise ErrorNegocio("El descuento no puede ser mayor que el subtotal")
        factor = (subtotal - descuento) / subtotal if subtotal > 0 else 1
        iva_total = iva_bruto * factor
        total = round(subtotal - descuento + iva_total, 2)
        subtotal = round(subtotal, 2)
        iva_total = round(iva_total, 2)
        ganancia_total = round(ganancia_bruta - descuento, 2)

        suma_pagos = round(sum(p['monto'] for p in pagos), 2)
        deuda = 0.0
        if suma_pagos < total - 0.01:
            if not fiado:
                raise ErrorNegocio(f"Los pagos (${suma_pagos:.2f}) no cubren el total (${total:.2f})")
            deuda = round(total - suma_pagos, 2)
        cambio = round(max(0.0, suma_pagos - total), 2)

        if deuda > 0:
            forma_pago = 'credito'
        elif len(pagos) == 1:
            forma_pago = pagos[0]['metodo']
        else:
            forma_pago = 'mixto'

        fecha_dt = _fecha_venta(data)
        fecha = fecha_dt.strftime("%Y-%m-%d %H:%M:%S")
        base = "F" + fecha_dt.strftime("%Y%m%d%H%M%S")
        numero, n = base, 1
        while conn.execute("SELECT 1 FROM ventas WHERE numero_factura = ?", (numero,)).fetchone():
            n += 1
            numero = f"{base}-{n}"

        caja = caja_abierta(conn)
        tasa = conn.execute(
            "SELECT valor FROM tasa_cambio ORDER BY fecha DESC, id DESC LIMIT 1").fetchone()

        cur = conn.execute("""
            INSERT INTO ventas (numero_factura, fecha, usuario_id, subtotal, iva, total,
                                forma_pago, monto_recibido, cambio, ganancia, tasa, descuento,
                                estado, cliente_id, deuda, caja_id, uuid)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'completada',?,?,?,?)
        """, (numero, fecha, session['usuario_id'], subtotal, iva_total, total, forma_pago,
              suma_pagos, cambio, ganancia_total, tasa['valor'] if tasa else None, descuento,
              cliente['id'] if cliente else None, deuda, caja['id'] if caja else None, uuid_venta))
        venta_id = cur.lastrowid

        for p in pagos:
            conn.execute("INSERT INTO venta_pagos (venta_id, metodo, monto) VALUES (?,?,?)",
                         (venta_id, p['metodo'], p['monto']))

        nota = f'Venta {numero}' + (' (sincronizada offline)' if offline else '')
        for d in detalle:
            conn.execute("""
                INSERT INTO venta_detalle (venta_id, producto_id, cantidad, precio_unitario,
                                           subtotal, precio_compra, ganancia, nombre_producto)
                VALUES (?,?,?,?,?,?,?,?)
            """, (venta_id, d['producto_id'], d['cantidad'], d['precio'], d['subtotal'],
                  d['precio_compra'], d['ganancia'], d['nombre']))
            conn.execute("UPDATE productos SET stock_actual = stock_actual - ? WHERE id = ?",
                         (d['cantidad'], d['producto_id']))
            conn.execute("""
                INSERT INTO movimientos (producto_id, tipo, cantidad, motivo, fecha, usuario_id)
                VALUES (?,?,?,?,?,?)
            """, (d['producto_id'], 'salida', d['cantidad'], nota, fecha, session['usuario_id']))

        if deuda > 0:
            conn.execute("UPDATE clientes SET saldo = saldo + ? WHERE id = ?",
                         (deuda, cliente['id']))

        conn.commit()
        venta = conn.execute("SELECT * FROM ventas WHERE id = ?", (venta_id,)).fetchone()
        return jsonify(_respuesta_venta(venta))
    except ErrorNegocio:
        conn.rollback()
        raise
    except sqlite3.IntegrityError:
        # Carrera entre dos envíos de la misma venta: ya quedó registrada por el otro
        conn.rollback()
        ya = conn.execute("SELECT * FROM ventas WHERE uuid = ?", (uuid_venta,)).fetchone() \
            if uuid_venta else None
        if ya:
            return jsonify(_respuesta_venta(ya, duplicada=True))
        app.logger.exception("Integridad al registrar venta")
        return jsonify({"ok": False, "error": "No se pudo registrar la venta"}), 500
    except Exception:
        conn.rollback()
        app.logger.exception("Error al registrar venta")
        return jsonify({"ok": False, "error": "Error interno al registrar la venta"}), 500
    finally:
        conn.close()


# ============================================================
# Productos y categorías
# ============================================================
@app.route('/productos')
@admin_required
def productos():
    conn = get_db()
    lista = conn.execute("""
        SELECT p.*, c.nombre AS categoria FROM productos p
        LEFT JOIN categorias c ON p.categoria_id = c.id
        ORDER BY p.activo DESC, p.nombre
    """).fetchall()
    categorias = conn.execute("""
        SELECT c.*, (SELECT COUNT(*) FROM productos WHERE categoria_id = c.id) AS num_productos
        FROM categorias c ORDER BY c.nombre
    """).fetchall()
    conn.close()
    return render_template('productos.html',
                           productos=[dict(p) for p in lista],
                           categorias=[dict(c) for c in categorias])


@app.route('/productos/guardar', methods=['POST'])
@admin_required
def guardar_producto():
    d = request.form
    nombre = d.get('nombre', '').strip()
    if not nombre:
        raise ErrorNegocio("El nombre del producto es obligatorio")
    codigo = d.get('codigo_barras', '').strip() or None
    pc = a_numero(d.get('precio_compra'), 'El precio de compra', defecto=0.0)
    pv = a_numero(d.get('precio_venta'), 'El precio de venta')
    sm = a_numero(d.get('stock_minimo'), 'El stock mínimo', defecto=0.0)
    iva = a_numero(d.get('iva'), 'El IVA', maximo=100, defecto=0.0)
    um = d.get('unidad_medida', '').strip() or 'unidad'
    categoria = a_entero(d['categoria_id'], 'La categoría') if d.get('categoria_id') else None
    pid = d.get('id')

    conn = get_db()
    try:
        if pid:
            conn.execute("""
                UPDATE productos SET codigo_barras=?, nombre=?, categoria_id=?,
                    precio_compra=?, precio_venta=?, stock_minimo=?, unidad_medida=?, iva=?
                WHERE id=?
            """, (codigo, nombre, categoria, pc, pv, sm, um, iva, a_entero(pid, 'El producto')))
        else:
            stock = a_numero(d.get('stock_inicial'), 'El stock inicial', defecto=0.0)
            cur = conn.execute("""
                INSERT INTO productos (codigo_barras, nombre, categoria_id, precio_compra,
                    precio_venta, stock_actual, stock_minimo, unidad_medida, iva)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (codigo, nombre, categoria, pc, pv, stock, sm, um, iva))
            if stock > 0:
                conn.execute("""
                    INSERT INTO movimientos (producto_id, tipo, cantidad, motivo, fecha, usuario_id, costo)
                    VALUES (?,?,?,?,?,?,?)
                """, (cur.lastrowid, 'entrada', stock, 'Stock inicial', ahora(),
                      session['usuario_id'], pc or None))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ErrorNegocio("Ya existe un producto con ese código de barras")
    finally:
        conn.close()

    if pv < pc:
        flash("Producto guardado, pero el precio de venta es menor que el de compra", "warning")
    else:
        flash("Producto guardado", "success")
    return redirect(url_for('productos'))


@app.route('/productos/<int:pid>/<accion>', methods=['POST'])
@admin_required
def cambiar_estado_producto(pid, accion):
    if accion not in ('desactivar', 'reactivar'):
        abort(404)
    conn = get_db()
    conn.execute("UPDATE productos SET activo = ? WHERE id = ?",
                 (1 if accion == 'reactivar' else 0, pid))
    conn.commit()
    conn.close()
    flash("Producto reactivado" if accion == 'reactivar' else "Producto desactivado",
          "success" if accion == 'reactivar' else "warning")
    return redirect(url_for('productos'))


@app.route('/categorias/guardar', methods=['POST'])
@admin_required
def guardar_categoria():
    nombre = request.form.get('nombre', '').strip()[:60]
    if not nombre:
        raise ErrorNegocio("El nombre de la categoría es obligatorio")
    conn = get_db()
    try:
        conn.execute("INSERT INTO categorias (nombre) VALUES (?)", (nombre,))
        conn.commit()
    except sqlite3.IntegrityError:
        raise ErrorNegocio("Esa categoría ya existe")
    finally:
        conn.close()
    flash("Categoría creada", "success")
    return redirect(url_for('productos'))


@app.route('/categorias/<int:cid>/eliminar', methods=['POST'])
@admin_required
def eliminar_categoria(cid):
    conn = get_db()
    usados = conn.execute("SELECT COUNT(*) FROM productos WHERE categoria_id = ?", (cid,)).fetchone()[0]
    if usados:
        conn.close()
        raise ErrorNegocio("No puedes eliminar una categoría que tiene productos")
    conn.execute("DELETE FROM categorias WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    flash("Categoría eliminada", "warning")
    return redirect(url_for('productos'))


def _csv_response(nombre, encabezados, filas):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(encabezados)
    w.writerows(filas)
    # BOM para que Excel respete los acentos
    return Response('﻿' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename={nombre}'})


@app.route('/productos/exportar')
@admin_required
def exportar_productos():
    conn = get_db()
    filas = conn.execute("""
        SELECT p.codigo_barras, p.nombre, c.nombre, p.precio_compra, p.precio_venta,
               p.stock_actual, p.stock_minimo, p.unidad_medida, p.iva, p.activo
        FROM productos p LEFT JOIN categorias c ON c.id = p.categoria_id ORDER BY p.nombre
    """).fetchall()
    conn.close()
    return _csv_response(f"productos-{datetime.now():%Y%m%d}.csv",
                         ['Código', 'Nombre', 'Categoría', 'P. compra', 'P. venta', 'Stock',
                          'Stock mínimo', 'Unidad', 'IVA %', 'Activo'],
                         [list(r) for r in filas])


# ============================================================
# Inventario
# ============================================================
@app.route('/inventario')
@admin_required
def inventario():
    conn = get_db()
    lista = conn.execute("""
        SELECT p.*, c.nombre AS categoria FROM productos p
        LEFT JOIN categorias c ON p.categoria_id = c.id
        WHERE p.activo = 1 ORDER BY p.stock_actual ASC, p.nombre
    """).fetchall()
    movimientos = conn.execute("""
        SELECT m.*, p.nombre AS producto, u.nombre AS usuario FROM movimientos m
        LEFT JOIN productos p ON p.id = m.producto_id
        LEFT JOIN usuarios u ON u.id = m.usuario_id
        ORDER BY m.id DESC LIMIT 100
    """).fetchall()
    valor = conn.execute("""
        SELECT COALESCE(SUM(stock_actual * precio_compra), 0) AS costo,
               COALESCE(SUM(stock_actual * precio_venta), 0) AS venta
        FROM productos WHERE activo = 1 AND stock_actual > 0
    """).fetchone()
    conn.close()
    return render_template('inventario.html', productos=[dict(p) for p in lista],
                           movimientos=[dict(m) for m in movimientos], valor=dict(valor))


@app.route('/inventario/ajuste', methods=['POST'])
@admin_required
def ajuste_inventario():
    d = request.form
    producto_id = a_entero(d.get('producto_id'), 'El producto')
    tipo = d.get('tipo')
    if tipo not in TIPOS_AJUSTE:
        raise ErrorNegocio("Tipo de movimiento inválido")
    cantidad = a_numero(d.get('cantidad'), 'La cantidad',
                        minimo=0.001 if tipo != 'ajuste' else 0.0)
    costo = a_numero(d.get('costo'), 'El costo', defecto=0.0)
    motivo = d.get('motivo', '').strip()[:200]

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prod = conn.execute("SELECT stock_actual, nombre FROM productos WHERE id = ?",
                            (producto_id,)).fetchone()
        if not prod:
            raise ErrorNegocio("Producto no encontrado")

        if tipo == 'entrada':
            nuevo_stock, mov = prod['stock_actual'] + cantidad, cantidad
        elif tipo == 'salida':
            nuevo_stock, mov = prod['stock_actual'] - cantidad, cantidad
            if nuevo_stock < 0:
                raise ErrorNegocio(f"No puedes dejar el stock en negativo "
                                   f"(hay {prod['stock_actual']:g})")
        else:
            nuevo_stock, mov = cantidad, cantidad - prod['stock_actual']

        conn.execute("UPDATE productos SET stock_actual = ? WHERE id = ?", (nuevo_stock, producto_id))
        if tipo == 'entrada' and costo > 0:
            conn.execute("UPDATE productos SET precio_compra = ? WHERE id = ?", (costo, producto_id))
        conn.execute("""
            INSERT INTO movimientos (producto_id, tipo, cantidad, motivo, fecha, usuario_id, costo)
            VALUES (?,?,?,?,?,?,?)
        """, (producto_id, tipo, mov, motivo, ahora(), session['usuario_id'],
              costo if tipo == 'entrada' and costo > 0 else None))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash(f"Inventario actualizado: {prod['nombre']} ahora tiene {nuevo_stock:g}", "success")
    return redirect(url_for('inventario'))


# ============================================================
# Clientes y crédito (fiado)
# ============================================================
@app.route('/clientes')
@login_required
def clientes():
    conn = get_db()
    lista = conn.execute("""
        SELECT * FROM clientes WHERE activo = 1 ORDER BY saldo DESC, nombre
    """).fetchall()
    por_cobrar = conn.execute(
        "SELECT COALESCE(SUM(saldo),0) FROM clientes WHERE saldo > 0").fetchone()[0]
    conn.close()
    return render_template('clientes.html', clientes=[dict(c) for c in lista],
                           por_cobrar=por_cobrar)


@app.route('/clientes/guardar', methods=['POST'])
@login_required
def guardar_cliente():
    d = request.form
    nombre = d.get('nombre', '').strip()[:80]
    if not nombre:
        raise ErrorNegocio("El nombre del cliente es obligatorio")
    telefono = d.get('telefono', '').strip()[:30]
    cedula = d.get('cedula', '').strip()[:30]
    conn = get_db()
    if d.get('id'):
        conn.execute("UPDATE clientes SET nombre=?, telefono=?, cedula=? WHERE id=?",
                     (nombre, telefono, cedula, a_entero(d['id'], 'El cliente')))
    else:
        conn.execute("INSERT INTO clientes (nombre, telefono, cedula, creado) VALUES (?,?,?,?)",
                     (nombre, telefono, cedula, ahora()))
    conn.commit()
    conn.close()
    flash("Cliente guardado", "success")
    return redirect(request.referrer if request.referrer and '/clientes/' in request.referrer
                    else url_for('clientes'))


@app.route('/api/clientes', methods=['GET', 'POST'])
@login_required
def api_clientes():
    conn = get_db()
    try:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            nombre = str(data.get('nombre', '')).strip()[:80]
            if not nombre:
                raise ErrorNegocio("El nombre del cliente es obligatorio")
            cur = conn.execute(
                "INSERT INTO clientes (nombre, telefono, creado) VALUES (?,?,?)",
                (nombre, str(data.get('telefono', '')).strip()[:30], ahora()))
            conn.commit()
            return jsonify({"ok": True, "id": cur.lastrowid, "nombre": nombre, "saldo": 0})
        filas = conn.execute(
            "SELECT id, nombre, telefono, saldo FROM clientes WHERE activo = 1 ORDER BY nombre"
        ).fetchall()
        return jsonify([dict(f) for f in filas])
    finally:
        conn.close()


@app.route('/clientes/<int:cid>')
@login_required
def cliente_detalle(cid):
    conn = get_db()
    cliente = conn.execute("SELECT * FROM clientes WHERE id = ?", (cid,)).fetchone()
    if not cliente:
        conn.close()
        abort(404)
    ventas_credito = conn.execute("""
        SELECT id, numero_factura, fecha, total, deuda, estado FROM ventas
        WHERE cliente_id = ? AND deuda > 0 ORDER BY id DESC LIMIT 100
    """, (cid,)).fetchall()
    abonos = conn.execute("""
        SELECT a.*, u.nombre AS usuario FROM abonos a
        LEFT JOIN usuarios u ON u.id = a.usuario_id
        WHERE a.cliente_id = ? ORDER BY a.id DESC LIMIT 100
    """, (cid,)).fetchall()
    conn.close()
    return render_template('cliente_detalle.html', cliente=dict(cliente),
                           ventas=[dict(v) for v in ventas_credito],
                           abonos=[dict(a) for a in abonos])


@app.route('/clientes/<int:cid>/abono', methods=['POST'])
@login_required
def registrar_abono(cid):
    monto = a_numero(request.form.get('monto'), 'El abono', minimo=0.01)
    metodo = request.form.get('metodo', 'efectivo')
    if metodo not in METODOS_PAGO:
        raise ErrorNegocio("Método de pago inválido")
    nota = request.form.get('nota', '').strip()[:200]

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cliente = conn.execute("SELECT * FROM clientes WHERE id = ?", (cid,)).fetchone()
        if not cliente:
            raise ErrorNegocio("Cliente no encontrado")
        if monto > cliente['saldo'] + 0.005:
            raise ErrorNegocio(f"El abono (${monto:.2f}) supera la deuda (${cliente['saldo']:.2f})")
        caja = caja_abierta(conn)
        conn.execute("""
            INSERT INTO abonos (cliente_id, monto, metodo, fecha, usuario_id, nota, caja_id)
            VALUES (?,?,?,?,?,?,?)
        """, (cid, monto, metodo, ahora(), session['usuario_id'], nota, caja['id'] if caja else None))
        conn.execute("UPDATE clientes SET saldo = ROUND(saldo - ?, 2) WHERE id = ?", (monto, cid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    flash(f"Abono de ${monto:.2f} registrado", "success")
    return redirect(url_for('cliente_detalle', cid=cid))


# ============================================================
# Ventas
# ============================================================
def _cargar_venta(conn, vid, archivado):
    venta = conn.execute("""
        SELECT v.*, u.nombre AS usuario, c.nombre AS cliente FROM v_ventas v
        LEFT JOIN usuarios u ON u.id = v.usuario_id
        LEFT JOIN clientes c ON c.id = v.cliente_id
        WHERE v.id = ? AND v.archivado = ?
    """, (vid, archivado)).fetchone()
    if not venta:
        return None, [], []
    detalle = conn.execute("""
        SELECT d.*, COALESCE(d.nombre_producto, p.nombre, 'Producto #' || d.producto_id) AS nombre
        FROM v_detalle d LEFT JOIN productos p ON p.id = d.producto_id
        WHERE d.venta_id = ? AND d.archivado = ?
    """, (vid, archivado)).fetchall()
    pagos = conn.execute("SELECT * FROM v_pagos WHERE venta_id = ? AND archivado = ?",
                         (vid, archivado)).fetchall()
    return dict(venta), [dict(d) for d in detalle], [dict(p) for p in pagos]


@app.route('/ventas')
@login_required
def ventas():
    es_admin = session.get('rol') == 'admin'
    # El cajero solo consulta las ventas de hoy
    r = _rango_fechas(defecto='hoy') if es_admin else _rango_fechas(defecto='hoy', forzar='hoy')
    q = request.args.get('q', '').strip()[:40]

    where = "v.fecha BETWEEN ? AND ?"
    params = [r['ini'], r['fin']]
    if q:
        where += " AND (v.numero_factura LIKE ? OR c.nombre LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]

    conn = get_db_unificado()
    lista = conn.execute(f"""
        SELECT v.*, u.nombre AS usuario, c.nombre AS cliente FROM v_ventas v
        LEFT JOIN usuarios u ON u.id = v.usuario_id
        LEFT JOIN clientes c ON c.id = v.cliente_id
        WHERE {where} ORDER BY v.fecha DESC, v.id DESC LIMIT 500
    """, params).fetchall()
    t = conn.execute(f"""
        SELECT COUNT(*) AS n, COALESCE(SUM(v.total),0) AS total,
               COALESCE(SUM(v.ganancia),0) AS ganancia, COALESCE(SUM(v.deuda),0) AS deuda
        FROM v_ventas v LEFT JOIN clientes c ON c.id = v.cliente_id
        WHERE {where} AND v.estado = 'completada'
    """, params).fetchone()
    conn.close()

    return render_template('ventas.html', ventas=[dict(v) for v in lista], totales=dict(t),
                           r=r, q=q, limitado=len(lista) >= 500)


@app.route('/ventas/<int:vid>')
@login_required
def ver_venta(vid):
    archivado = 1 if request.args.get('arch') == '1' else 0
    conn = get_db_unificado()
    venta, detalle, pagos = _cargar_venta(conn, vid, archivado)
    conn.close()
    if not venta:
        flash("Venta no encontrada", "danger")
        return redirect(url_for('ventas'))
    return render_template('detalle_venta.html', venta=venta, detalle=detalle, pagos=pagos)


@app.route('/ventas/<int:vid>/ticket')
@login_required
def ticket_venta(vid):
    archivado = 1 if request.args.get('arch') == '1' else 0
    conn = get_db_unificado()
    venta, detalle, pagos = _cargar_venta(conn, vid, archivado)
    conn.close()
    if not venta:
        abort(404)
    return render_template('ticket.html', venta=venta, detalle=detalle, pagos=pagos,
                           imprimir=request.args.get('auto') == '1')


@app.route('/ventas/<int:vid>/anular', methods=['POST'])
@admin_required
def anular_venta(vid):
    motivo = request.form.get('motivo', '').strip()[:200]
    if not motivo:
        raise ErrorNegocio("Indica el motivo de la anulación")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        v = conn.execute("SELECT * FROM ventas WHERE id = ?", (vid,)).fetchone()
        if not v:
            raise ErrorNegocio("Venta no encontrada (las ventas archivadas no se pueden anular)")
        if v['estado'] == 'anulada':
            raise ErrorNegocio("Esa venta ya está anulada")
        fecha = ahora()
        for d in conn.execute("SELECT * FROM venta_detalle WHERE venta_id = ?", (vid,)).fetchall():
            conn.execute("UPDATE productos SET stock_actual = stock_actual + ? WHERE id = ?",
                         (d['cantidad'], d['producto_id']))
            conn.execute("""
                INSERT INTO movimientos (producto_id, tipo, cantidad, motivo, fecha, usuario_id)
                VALUES (?,?,?,?,?,?)
            """, (d['producto_id'], 'entrada', d['cantidad'],
                  f"Anulación {v['numero_factura']}", fecha, session['usuario_id']))
        if (v['deuda'] or 0) > 0 and v['cliente_id']:
            conn.execute("UPDATE clientes SET saldo = ROUND(saldo - ?, 2) WHERE id = ?",
                         (v['deuda'], v['cliente_id']))
        conn.execute("""
            UPDATE ventas SET estado = 'anulada', anulada_fecha = ?, anulada_motivo = ?,
                anulada_por = ? WHERE id = ?
        """, (fecha, motivo, session['usuario_id'], vid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    app.logger.info("Venta %s anulada por usuario %s: %s", vid, session['usuario_id'], motivo)
    flash("Venta anulada: el stock fue devuelto al inventario", "warning")
    return redirect(url_for('ver_venta', vid=vid))


@app.route('/admin/archivar', methods=['POST'])
@admin_required
def admin_archivar():
    n = archivar_ventas_antiguas(15)
    flash(f"{n} ventas archivadas correctamente", "success")
    return redirect(url_for('ventas'))


# ============================================================
# Gastos
# ============================================================
def _rango_fechas(defecto='mes', forzar=None):
    hoy = datetime.now().date()
    desde_s, hasta_s = request.args.get('desde'), request.args.get('hasta')
    rango = forzar or request.args.get('rango', defecto)
    if desde_s and hasta_s and not forzar:
        try:
            d = datetime.strptime(desde_s, '%Y-%m-%d').date()
            h = datetime.strptime(hasta_s, '%Y-%m-%d').date()
        except ValueError:
            raise ErrorNegocio("Las fechas no son válidas")
        if d > h:
            d, h = h, d
        rango = 'personalizado'
    elif rango == 'hoy':
        d = h = hoy
    elif rango == 'semana':
        d, h = hoy - timedelta(days=6), hoy
    elif rango == '30':
        d, h = hoy - timedelta(days=29), hoy
    elif rango == 'todo':
        d, h = datetime(2000, 1, 1).date(), hoy
    else:
        rango, d, h = 'mes', hoy.replace(day=1), hoy
    return {'rango': rango, 'desde': d.isoformat(), 'hasta': h.isoformat(),
            'ini': f"{d.isoformat()} 00:00:00", 'fin': f"{h.isoformat()} 23:59:59"}


@app.route('/gastos')
@admin_required
def gastos():
    r = _rango_fechas()
    conn = get_db()
    lista = conn.execute("""
        SELECT g.*, u.nombre AS usuario FROM gastos g LEFT JOIN usuarios u ON u.id = g.usuario_id
        WHERE g.fecha BETWEEN ? AND ? ORDER BY g.fecha DESC, g.id DESC
    """, (r['ini'], r['fin'])).fetchall()
    por_categoria = conn.execute("""
        SELECT COALESCE(categoria, 'Otros') AS categoria, SUM(monto) AS total FROM gastos
        WHERE fecha BETWEEN ? AND ? GROUP BY 1 ORDER BY total DESC
    """, (r['ini'], r['fin'])).fetchall()
    abierta = caja_abierta(conn)
    conn.close()
    return render_template('gastos.html', gastos=[dict(x) for x in lista],
                           por_categoria=[dict(x) for x in por_categoria],
                           total=sum(x['monto'] for x in lista), r=r,
                           categorias=CATEGORIAS_GASTO, caja_abierta=bool(abierta))


@app.route('/gastos/guardar', methods=['POST'])
@admin_required
def guardar_gasto():
    d = request.form
    concepto = d.get('concepto', '').strip()[:120]
    if not concepto:
        raise ErrorNegocio("Describe el gasto")
    monto = a_numero(d.get('monto'), 'El monto', minimo=0.01)
    categoria = d.get('categoria') if d.get('categoria') in CATEGORIAS_GASTO else 'Otros'
    de_caja = d.get('pagado_de_caja') == '1'
    conn = get_db()
    caja = caja_abierta(conn) if de_caja else None
    if de_caja and not caja:
        conn.close()
        raise ErrorNegocio("No hay una caja abierta para descontar este gasto")
    conn.execute("""
        INSERT INTO gastos (fecha, concepto, categoria, monto, usuario_id, pagado_de_caja, caja_id)
        VALUES (?,?,?,?,?,?,?)
    """, (ahora(), concepto, categoria, monto, session['usuario_id'],
          1 if de_caja else 0, caja['id'] if caja else None))
    conn.commit()
    conn.close()
    flash("Gasto registrado", "success")
    return redirect(url_for('gastos'))


@app.route('/gastos/<int:gid>/eliminar', methods=['POST'])
@admin_required
def eliminar_gasto(gid):
    conn = get_db()
    conn.execute("DELETE FROM gastos WHERE id = ?", (gid,))
    conn.commit()
    conn.close()
    flash("Gasto eliminado", "warning")
    return redirect(url_for('gastos'))


# ============================================================
# Reportes
# ============================================================
@app.route('/reportes')
@admin_required
def reportes():
    r = _rango_fechas()
    p = (r['ini'], r['fin'])
    conn = get_db_unificado()

    base = "FROM v_ventas v WHERE v.fecha BETWEEN ? AND ? AND v.estado = 'completada'"
    resumen = dict(conn.execute(f"""
        SELECT COUNT(*) AS num, COALESCE(SUM(v.total),0) AS total,
               COALESCE(SUM(v.ganancia),0) AS ganancia, COALESCE(SUM(v.descuento),0) AS descuentos
        {base}
    """, p).fetchone())
    gastos_total = conn.execute(
        "SELECT COALESCE(SUM(monto),0) FROM gastos WHERE fecha BETWEEN ? AND ?", p).fetchone()[0]
    resumen['gastos'] = gastos_total
    resumen['neta'] = resumen['ganancia'] - gastos_total
    resumen['ticket'] = resumen['total'] / resumen['num'] if resumen['num'] else 0

    dias = [dict(x) for x in conn.execute(f"""
        SELECT substr(v.fecha, 1, 10) AS dia, COUNT(*) AS num, SUM(v.total) AS total,
               SUM(v.ganancia) AS ganancia {base} GROUP BY dia ORDER BY dia
    """, p)]
    maximo = max((d['total'] for d in dias), default=0) or 1
    for d in dias:
        d['pct'] = max(4, round(d['total'] / maximo * 100))
        d['corto'] = d['dia'][8:10] + '/' + d['dia'][5:7]

    metodos = [dict(x) for x in conn.execute("""
        SELECT pg.metodo, COUNT(*) AS num, SUM(pg.monto) AS total
        FROM v_pagos pg JOIN v_ventas v ON v.id = pg.venta_id AND v.archivado = pg.archivado
        WHERE v.fecha BETWEEN ? AND ? AND v.estado = 'completada'
        GROUP BY pg.metodo ORDER BY total DESC
    """, p)]

    categorias = [dict(x) for x in conn.execute("""
        SELECT COALESCE(c.nombre, 'Sin categoría') AS nombre, SUM(d.cantidad) AS cantidad,
               SUM(d.subtotal) AS total, SUM(d.ganancia) AS ganancia
        FROM v_detalle d JOIN v_ventas v ON v.id = d.venta_id AND v.archivado = d.archivado
        LEFT JOIN productos pr ON pr.id = d.producto_id
        LEFT JOIN categorias c ON c.id = pr.categoria_id
        WHERE v.fecha BETWEEN ? AND ? AND v.estado = 'completada'
        GROUP BY 1 ORDER BY total DESC
    """, p)]

    top_productos = [dict(x) for x in conn.execute("""
        SELECT COALESCE(MAX(pr.nombre), MAX(d.nombre_producto), 'Producto #' || d.producto_id) AS nombre,
               SUM(d.cantidad) AS cantidad, SUM(d.subtotal) AS total, SUM(d.ganancia) AS ganancia
        FROM v_detalle d JOIN v_ventas v ON v.id = d.venta_id AND v.archivado = d.archivado
        LEFT JOIN productos pr ON pr.id = d.producto_id
        WHERE v.fecha BETWEEN ? AND ? AND v.estado = 'completada'
        GROUP BY d.producto_id ORDER BY cantidad DESC LIMIT 10
    """, p)]

    stock_bajo = [dict(x) for x in conn.execute("""
        SELECT nombre, stock_actual, stock_minimo FROM productos
        WHERE activo = 1 AND stock_actual <= stock_minimo ORDER BY stock_actual ASC
    """)]
    inventario = dict(conn.execute("""
        SELECT COALESCE(SUM(stock_actual * precio_compra), 0) AS costo,
               COALESCE(SUM(stock_actual * precio_venta), 0) AS venta
        FROM productos WHERE activo = 1 AND stock_actual > 0
    """).fetchone())
    por_cobrar = conn.execute(
        "SELECT COALESCE(SUM(saldo), 0) FROM clientes WHERE saldo > 0").fetchone()[0]
    conn.close()

    return render_template('reportes.html', r=r, resumen=resumen, dias=dias, metodos=metodos,
                           categorias=categorias, top_productos=top_productos,
                           stock_bajo=stock_bajo, inventario=inventario, por_cobrar=por_cobrar)


@app.route('/reportes/exportar')
@admin_required
def exportar_ventas():
    r = _rango_fechas()
    conn = get_db_unificado()
    filas = conn.execute("""
        SELECT v.numero_factura, v.fecha, u.nombre, c.nombre, v.subtotal, v.descuento, v.iva,
               v.total, v.ganancia, v.forma_pago, v.deuda, v.estado
        FROM v_ventas v LEFT JOIN usuarios u ON u.id = v.usuario_id
        LEFT JOIN clientes c ON c.id = v.cliente_id
        WHERE v.fecha BETWEEN ? AND ? ORDER BY v.fecha
    """, (r['ini'], r['fin'])).fetchall()
    conn.close()
    return _csv_response(f"ventas-{r['desde']}_a_{r['hasta']}.csv",
                         ['Factura', 'Fecha', 'Usuario', 'Cliente', 'Subtotal', 'Descuento', 'IVA',
                          'Total', 'Ganancia', 'Pago', 'Deuda', 'Estado'],
                         [list(x) for x in filas])


# ============================================================
# Usuarios y cuenta
# ============================================================
def _validar_clave(clave):
    if len(clave) < 6:
        raise ErrorNegocio("La contraseña debe tener al menos 6 caracteres")


def _verificar_cupo_usuarios(conn):
    activos = conn.execute("SELECT COUNT(*) FROM usuarios WHERE activo != 0").fetchone()[0]
    tope = plataforma.obtener_negocio(session['negocio_id'])['max_usuarios']
    if activos >= tope:
        contacto = f" Escríbenos para ampliarlo: {SOPORTE}." if SOPORTE else ""
        raise ErrorNegocio(f"Tu plan permite hasta {tope} usuarios activos.{contacto}")


@app.route('/usuarios')
@admin_required
def usuarios():
    conn = get_db()
    lista = conn.execute("SELECT id, nombre, usuario, rol, activo FROM usuarios ORDER BY id").fetchall()
    conn.close()
    cupo = {'activos': sum(1 for u in lista if u['activo'] != 0),
            'tope': plataforma.obtener_negocio(session['negocio_id'])['max_usuarios']}
    return render_template('usuarios.html', usuarios=[dict(u) for u in lista], cupo=cupo)


@app.route('/usuarios/guardar', methods=['POST'])
@admin_required
def guardar_usuario():
    d = request.form
    nombre = d.get('nombre', '').strip()[:60]
    usuario = d.get('usuario', '').strip()
    rol = d.get('rol') if d.get('rol') in ('admin', 'cajero') else 'cajero'
    if not nombre:
        raise ErrorNegocio("El nombre es obligatorio")
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,30}', usuario):
        raise ErrorNegocio("El usuario debe tener 3-30 caracteres (letras, números, . _ -)")
    clave = d.get('password', '')
    _validar_clave(clave)
    conn = get_db()
    try:
        _verificar_cupo_usuarios(conn)
        conn.execute("INSERT INTO usuarios (nombre, usuario, password_hash, rol) VALUES (?,?,?,?)",
                     (nombre, usuario, generate_password_hash(clave), rol))
        conn.commit()
    except sqlite3.IntegrityError:
        raise ErrorNegocio("Ese nombre de usuario ya existe")
    finally:
        conn.close()
    flash(f"Usuario {usuario} creado", "success")
    return redirect(url_for('usuarios'))


@app.route('/usuarios/<int:uid>/estado', methods=['POST'])
@admin_required
def estado_usuario(uid):
    if uid == session['usuario_id']:
        raise ErrorNegocio("No puedes desactivar tu propia cuenta")
    conn = get_db()
    u = conn.execute("SELECT activo FROM usuarios WHERE id = ?", (uid,)).fetchone()
    if not u:
        conn.close()
        abort(404)
    nuevo = 0 if u['activo'] != 0 else 1
    if nuevo:
        try:
            _verificar_cupo_usuarios(conn)
        except ErrorNegocio:
            conn.close()
            raise
    conn.execute("UPDATE usuarios SET activo = ? WHERE id = ?", (nuevo, uid))
    conn.commit()
    conn.close()
    flash("Usuario activado" if nuevo else "Usuario desactivado", "success" if nuevo else "warning")
    return redirect(url_for('usuarios'))


@app.route('/usuarios/<int:uid>/clave', methods=['POST'])
@admin_required
def clave_usuario(uid):
    clave = request.form.get('password', '')
    _validar_clave(clave)
    conn = get_db()
    conn.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(clave), uid))
    conn.commit()
    conn.close()
    flash("Contraseña actualizada", "success")
    return redirect(url_for('usuarios'))


@app.route('/cuenta', methods=['GET', 'POST'])
@login_required
def cuenta():
    if request.method == 'POST':
        actual = request.form.get('actual', '')
        nueva = request.form.get('nueva', '')
        if nueva != request.form.get('confirmar', ''):
            raise ErrorNegocio("La confirmación no coincide con la nueva contraseña")
        _validar_clave(nueva)
        conn = get_db()
        u = conn.execute("SELECT password_hash FROM usuarios WHERE id = ?",
                         (session['usuario_id'],)).fetchone()
        if not check_password_hash(u['password_hash'], actual):
            conn.close()
            raise ErrorNegocio("La contraseña actual es incorrecta")
        conn.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?",
                     (generate_password_hash(nueva), session['usuario_id']))
        conn.commit()
        conn.close()
        session['pass_default'] = False
        flash("Contraseña cambiada", "success")
        return redirect(url_for('cuenta'))
    return render_template('cuenta.html')


# ============================================================
# Configuración, tasa y respaldos
# ============================================================
@app.route('/configuracion')
@admin_required
def configuracion():
    conn = get_db()
    cfg = conn.execute("SELECT * FROM configuracion WHERE id = 1").fetchone()
    historial = conn.execute(
        "SELECT * FROM tasa_cambio ORDER BY fecha DESC, id DESC LIMIT 7").fetchall()
    conn.close()
    return render_template('configuracion.html', cfg=dict(cfg), tasas=[dict(t) for t in historial],
                           respaldos=listar_backups())


@app.route('/configuracion/guardar', methods=['POST'])
@admin_required
def guardar_configuracion():
    d = request.form
    conn = get_db()
    conn.execute("""
        UPDATE configuracion SET nombre_negocio=?, rif=?, direccion=?, telefono=?, tasa_iva=?,
            mensaje_ticket=? WHERE id = 1
    """, (d.get('nombre_negocio', '').strip()[:80] or 'Mi Negocio', d.get('rif', '').strip()[:30],
          d.get('direccion', '').strip()[:150], d.get('telefono', '').strip()[:30],
          a_numero(d.get('tasa_iva'), 'El IVA', maximo=100, defecto=16.0),
          d.get('mensaje_ticket', '').strip()[:150]))
    conn.commit()
    conn.close()
    flash("Configuración guardada", "success")
    return redirect(url_for('configuracion'))


@app.route('/configuracion/tasa', methods=['POST'])
@admin_required
def tasa_manual():
    valor = a_numero(request.form.get('valor'), 'La tasa', minimo=0.01, maximo=10_000_000)
    guardar_tasa(valor, 'Manual')
    flash(f"Tasa establecida en {valor:.2f} Bs por dólar", "success")
    return redirect(url_for('configuracion'))


@app.route('/configuracion/tasa/bcv', methods=['POST'])
@admin_required
def tasa_desde_bcv():
    if actualizar_tasa_diaria(forzar=True):
        flash("Tasa actualizada desde el BCV", "success")
    else:
        flash("No se pudo consultar el BCV. Puedes ingresar la tasa manualmente.", "danger")
    return redirect(url_for('configuracion'))


@app.route('/respaldos/crear', methods=['POST'])
@admin_required
def crear_respaldo():
    hacer_backup('manual')
    flash("Respaldo creado", "success")
    return redirect(url_for('configuracion'))


@app.route('/respaldos/<nombre>')
@admin_required
def descargar_respaldo(nombre):
    if not re.fullmatch(r'(minisuper|archivo)-[a-z]+-\d{8}-\d{6}\.db', nombre):
        abort(404)
    return send_from_directory(database.backup_dir(), nombre, as_attachment=True)


# ============================================================
# Asistente
# ============================================================
@app.route('/asistente')
@admin_required
def asistente():
    return render_template('asistente.html')


@app.route('/api/asistente', methods=['POST'])
@admin_required
def api_asistente():
    data = request.get_json(silent=True) or {}
    pregunta = str(data.get('pregunta', '')).strip()[:500]
    if not pregunta:
        return jsonify({'ok': False, 'error': 'Pregunta vacía'}), 400
    return jsonify(rag_preguntar(pregunta))


# ============================================================
# Plataforma (SaaS): registro de negocios y panel del superadministrador
# ============================================================
_registros_por_ip = {}      # ip -> [instantes]; limita altas masivas


def _limitar_registros(ip, maximo=5, ventana=3600):
    ahora_ts = time.time()
    recientes = [t for t in _registros_por_ip.get(ip, []) if ahora_ts - t < ventana]
    if len(recientes) >= maximo:
        _registros_por_ip[ip] = recientes
        raise ErrorNegocio("Demasiados registros desde esta conexión. Intenta más tarde.")
    recientes.append(ahora_ts)
    _registros_por_ip[ip] = recientes


@app.route('/registro', methods=['GET', 'POST'])
def registro():
    """Alta de un negocio nuevo (solo si VENDIYA_REGISTRO_PUBLICO=1)."""
    if not REGISTRO_PUBLICO:
        abort(404)
    if request.method == 'POST':
        d = request.form
        if d.get('sitio_web'):                      # campo trampa para bots: un humano no lo ve
            abort(400)
        if d.get('password') != d.get('confirmar'):
            raise ErrorNegocio("La confirmación no coincide con la contraseña")
        _limitar_registros(request.remote_addr)
        n = plataforma.crear_negocio(
            d.get('negocio', ''), d.get('nombre', ''), d.get('usuario', ''), d.get('password', ''),
            email=d.get('email', ''), telefono=d.get('telefono', ''), plan='prueba', dias=DIAS_PRUEBA,
            max_usuarios=3, actor='registro')
        flash(f"¡Listo! Tu negocio está creado. Tu código de acceso es «{n['slug']}». "
              f"Tienes {DIAS_PRUEBA} días de prueba.", "success")
        return redirect(url_for('login', n=n['slug']))
    return render_template('registro.html', dias_prueba=DIAS_PRUEBA)


def superadmin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('superadmin_id'):
            return redirect(url_for('plataforma_login'))
        return f(*args, **kwargs)
    return wrapper


@app.route('/plataforma/login', methods=['GET', 'POST'])
def plataforma_login():
    if request.method == 'POST':
        usuario = request.form.get('usuario', '').strip()
        clave = _clave_intento(f"sa|{usuario}")
        fallos, bloqueado_hasta = _intentos_login.get(clave, (0, 0))
        if bloqueado_hasta > time.time():
            flash("Demasiados intentos fallidos. Espera unos minutos.", "danger")
            return render_template('plataforma/login.html')
        sa = plataforma.verificar_superadmin(usuario, request.form.get('password', ''))
        if sa:
            _intentos_login.pop(clave, None)
            session.clear()
            session.permanent = True
            session['superadmin_id'] = sa['id']
            session['superadmin_nombre'] = sa['nombre']
            csrf_token()
            return redirect(url_for('plataforma_panel'))
        fallos += 1
        _intentos_login[clave] = (fallos, time.time() + BLOQUEO_SEGUNDOS if fallos >= MAX_INTENTOS else 0)
        app.logger.warning("Login de superadmin fallido para '%s' desde %s", usuario, request.remote_addr)
        flash("Usuario o contraseña incorrectos", "danger")
    return render_template('plataforma/login.html')


@app.route('/plataforma/logout', methods=['POST'])
def plataforma_logout():
    session.clear()
    return redirect(url_for('plataforma_login'))


@app.route('/plataforma')
@superadmin_required
def plataforma_panel():
    conn = plataforma.get_master()
    eventos = conn.execute("""
        SELECT e.*, n.slug FROM eventos e LEFT JOIN negocios n ON n.id = e.negocio_id
        ORDER BY e.id DESC LIMIT 12
    """).fetchall()
    conn.close()
    return render_template('plataforma/panel.html', negocios=plataforma.listar_negocios(),
                           resumen=plataforma.resumen_plataforma(), eventos=[dict(e) for e in eventos],
                           planes=plataforma.PLANES, dias_prueba=DIAS_PRUEBA,
                           tasa=plataforma.tasa_global(), registro_publico=REGISTRO_PUBLICO)


@app.route('/plataforma/negocios/crear', methods=['POST'])
@superadmin_required
def plataforma_crear():
    d = request.form
    dias = a_numero(d.get('dias'), 'Los días de suscripción', maximo=3650, defecto=DIAS_PRUEBA)
    n = plataforma.crear_negocio(
        d.get('nombre', ''), d.get('dueno', ''), d.get('usuario', ''), d.get('password', ''),
        slug=d.get('codigo', '').strip().lower() or None, email=d.get('email', ''),
        telefono=d.get('telefono', ''), plan=d.get('plan', 'prueba'), dias=int(dias),
        max_usuarios=a_entero(d.get('max_usuarios') or 3, 'El máximo de usuarios'),
        actor=session.get('superadmin_nombre', 'superadmin'))
    flash(f"Negocio «{n['nombre']}» creado. Código de acceso: {n['slug']} · usuario: {d.get('usuario')}", "success")
    return redirect(url_for('plataforma_negocio', nid=n['id']))


@app.route('/plataforma/negocios/<int:nid>')
@superadmin_required
def plataforma_negocio(nid):
    n = plataforma.obtener_negocio(nid)
    if not n:
        abort(404)
    conn = plataforma.get_master()
    eventos = conn.execute("SELECT * FROM eventos WHERE negocio_id = ? ORDER BY id DESC LIMIT 20",
                           (nid,)).fetchall()
    conn.close()
    return render_template('plataforma/negocio.html', n=n, stats=plataforma.estadisticas(nid),
                           usuarios=plataforma.usuarios_de(nid), eventos=[dict(e) for e in eventos],
                           planes=plataforma.PLANES,
                           enlace=url_for('login', n=n['slug'], _external=True))


def _accion_negocio(nid):
    """Valida que el negocio exista y devuelve el nombre del actor para el registro."""
    if not plataforma.obtener_negocio(nid):
        abort(404)
    _cache_negocio.pop(nid, None)       # el cambio se aplica de inmediato en este proceso
    return session.get('superadmin_nombre', 'superadmin')


@app.route('/plataforma/negocios/<int:nid>/estado', methods=['POST'])
@superadmin_required
def plataforma_estado(nid):
    actor = _accion_negocio(nid)
    accion = request.form.get('accion')
    plataforma.cambiar_estado(nid, 'suspendido' if accion == 'suspender' else 'activo', actor)
    _cache_negocio.pop(nid, None)
    flash("Negocio suspendido" if accion == 'suspender' else "Negocio reactivado",
          "warning" if accion == 'suspender' else "success")
    return redirect(url_for('plataforma_negocio', nid=nid))


@app.route('/plataforma/negocios/<int:nid>/extender', methods=['POST'])
@superadmin_required
def plataforma_extender(nid):
    actor = _accion_negocio(nid)
    if request.form.get('sin_limite') == '1':
        plataforma.quitar_vencimiento(nid, actor)
        flash("El negocio ya no tiene fecha de vencimiento", "success")
    elif request.form.get('fecha'):
        plataforma.extender(nid, fecha=request.form['fecha'], actor=actor)
        flash("Vencimiento actualizado", "success")
    else:
        dias = a_numero(request.form.get('dias'), 'Los días', minimo=1, maximo=3650)
        nuevo = plataforma.extender(nid, dias=int(dias), actor=actor)
        flash(f"Suscripción extendida hasta {nuevo}", "success")
    _cache_negocio.pop(nid, None)
    return redirect(url_for('plataforma_negocio', nid=nid))


@app.route('/plataforma/negocios/<int:nid>/datos', methods=['POST'])
@superadmin_required
def plataforma_datos(nid):
    actor = _accion_negocio(nid)
    d = request.form
    plataforma.actualizar_datos(nid, d.get('plan'), d.get('max_usuarios'), d.get('email'),
                                d.get('telefono'), d.get('notas'), actor)
    flash("Datos del negocio actualizados", "success")
    return redirect(url_for('plataforma_negocio', nid=nid))


@app.route('/plataforma/negocios/<int:nid>/clave', methods=['POST'])
@superadmin_required
def plataforma_clave(nid):
    actor = _accion_negocio(nid)
    plataforma.reset_clave(nid, request.form.get('usuario', ''), request.form.get('password', ''), actor)
    flash("Contraseña restablecida", "success")
    return redirect(url_for('plataforma_negocio', nid=nid))


@app.route('/plataforma/negocios/<int:nid>/eliminar', methods=['POST'])
@superadmin_required
def plataforma_eliminar(nid):
    actor = _accion_negocio(nid)
    n = plataforma.obtener_negocio(nid)
    if request.form.get('confirmar', '').strip().lower() != n['slug']:
        raise ErrorNegocio("Para eliminar escribe el código del negocio exactamente")
    plataforma.eliminar_negocio(nid, actor)
    _cache_negocio.pop(nid, None)
    flash(f"Negocio «{n['nombre']}» eliminado. Sus datos quedaron guardados en disco.", "warning")
    return redirect(url_for('plataforma_panel'))


# ============================================================
# Arranque
# ============================================================
def preparar_plataforma():
    """Crea/actualiza la base central, importa los datos de la versión anterior y migra cada negocio."""
    plataforma.init_master()
    importado = plataforma.importar_legado()
    if importado:
        print(f"[INICIO] Tus datos anteriores se importaron como negocio «{importado['nombre']}». "
              f"Código para iniciar sesión: {importado['slug']}")
    plataforma.migrar_todos()
    if not plataforma.hay_superadmin():
        print("[INICIO] Aún no hay superadministrador. Créalo con:  python plataforma.py superadmin")


def _backups_pendientes():
    """Al arrancar: un respaldo por negocio si hoy aún no se hizo."""
    plataforma.para_cada_negocio(lambda: None if hay_backup_de_hoy() else hacer_backup('auto'))
    try:
        plataforma.backup_master()
    except Exception:
        logging.exception("Falló el respaldo de la base central")


def _archivar_todos():
    plataforma.para_cada_negocio(lambda: archivar_ventas_antiguas(15))


def _servir(host, puerto):
    """Servidor de producción (waitress) si está instalado; si no, el de desarrollo de Flask."""
    if os.environ.get('VENDIYA_DEBUG') == '1':
        # debug=True permite ejecutar código desde el navegador: solo para desarrollo local
        app.run(debug=True, host=host, port=puerto, use_reloader=False)
        return
    try:
        from waitress import serve
    except ImportError:
        print("[AVISO] Instala waitress (pip install waitress) para un servidor apto para producción.")
        app.run(host=host, port=puerto, use_reloader=False)
        return
    print(f"[INICIO] Escuchando en http://{host}:{puerto}")
    serve(app, host=host, port=puerto, threads=int(os.environ.get('VENDIYA_HILOS', '8')))


if __name__ == '__main__':
    preparar_plataforma()
    _backups_pendientes()

    scheduler.init_app(app)
    scheduler.add_job(id='actualizar_tasa', func=actualizar_tasa_global,
                      trigger='cron', hour=8, minute=0, replace_existing=True)
    scheduler.add_job(id='archivar_ventas', func=_archivar_todos,
                      trigger='cron', hour=2, minute=0, replace_existing=True)
    scheduler.add_job(id='respaldo_diario', func=plataforma.backup_todos,
                      trigger='cron', hour=23, minute=30, replace_existing=True)
    scheduler.start()

    # La consulta al BCV no debe retrasar el arranque si no hay internet
    threading.Thread(target=actualizar_tasa_global, daemon=True).start()

    _servir(os.environ.get('VENDIYA_HOST', '0.0.0.0'), int(os.environ.get('VENDIYA_PUERTO', '5000')))
