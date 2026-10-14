from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta
from functools import wraps
from database import get_db, get_archive_db, init_db, archivar_ventas_antiguas
from flask_apscheduler import APScheduler
from rag import preguntar as rag_preguntar
import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = "cambia-esto-por-una-clave-secreta-larga"

scheduler = APScheduler()

# ---------- Decorador de login ----------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'usuario_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

# ---------- Tasa BCV ----------
def obtener_tasa_bcv():
    try:
        headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36')
        }
        response = requests.get("https://www.bcv.org.ve/", headers=headers,
                                verify=False, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        dolar_div = soup.find("div", {"id": "dolar"})
        if not dolar_div:
            print("No se encontró el div 'dolar' en el BCV")
            return None
        strong = dolar_div.find("strong")
        if not strong:
            print("No se encontró <strong> dentro del div 'dolar'")
            return None
        valor_text = strong.text.strip().replace(".", "").replace(",", ".")
        return float(valor_text)
    except Exception as e:
        print(f"Error al obtener la tasa de cambio: {e}")
        return None

def actualizar_tasa_diaria():
    valor = obtener_tasa_bcv()
    if valor:
        conn = get_db()
        fecha = datetime.now().strftime("%Y-%m-%d")
        conn.execute("DELETE FROM tasa_cambio WHERE fecha = ?", (fecha,))
        conn.execute("INSERT INTO tasa_cambio (fecha, valor, fuente) VALUES (?,?,?)",
                     (fecha, valor, 'BCV'))
        conn.commit()
        conn.close()
        print(f"[{fecha}] Tasa de cambio actualizada: {valor} Bs/USD")
    else:
        print("No se pudo obtener la tasa de cambio, se mantiene la anterior")

def obtener_tasa_actual():
    conn = get_db()
    row = conn.execute("SELECT valor FROM tasa_cambio ORDER BY fecha DESC LIMIT 1").fetchone()
    conn.close()
    return row['valor'] if row else None

# ---------- Login ----------
@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        usuario = request.form['usuario']
        password = request.form['password']
        conn = get_db()
        user = conn.execute("SELECT * FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['usuario_id'] = user['id']
            session['usuario_nombre'] = user['nombre']
            session['rol'] = user['rol']
            return redirect(url_for('pos'))
        flash("Usuario o contraseña incorrectos", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ---------- POS ----------
@app.route('/pos')
@login_required
def pos():
    tasa = obtener_tasa_actual()
    return render_template('pos.html', tasa_bcv=tasa)

@app.route('/api/productos/buscar')
@login_required
def api_buscar_productos():
    q = request.args.get('q', '').strip()
    conn = get_db()
    if q:
        productos = conn.execute("""
            SELECT id, codigo_barras, nombre, precio_venta, stock_actual, iva
            FROM productos
            WHERE activo = 1 AND (codigo_barras = ? OR nombre LIKE ?)
            LIMIT 20
        """, (q, f"%{q}%")).fetchall()
    else:
        productos = conn.execute("""
            SELECT id, codigo_barras, nombre, precio_venta, stock_actual, iva
            FROM productos WHERE activo = 1 LIMIT 20
        """).fetchall()
    conn.close()
    return jsonify([dict(p) for p in productos])

@app.route('/api/venta', methods=['POST'])
@login_required
def api_registrar_venta():
    data = request.get_json()
    items = data.get('items', [])
    pagos = data.get('pagos', [])

    if not items:
        return jsonify({"ok": False, "error": "El carrito está vacío"}), 400
    if not pagos:
        return jsonify({"ok": False, "error": "Debes especificar al menos un método de pago"}), 400

    conn = get_db()
    try:
        subtotal = 0
        iva_total = 0
        ganancia_total = 0
        detalle = []

        for item in items:
            prod = conn.execute("SELECT * FROM productos WHERE id = ? AND activo = 1",
                                (item['producto_id'],)).fetchone()
            if not prod:
                return jsonify({"ok": False, "error": "Producto no encontrado"}), 400
            if prod['stock_actual'] < item['cantidad']:
                return jsonify({"ok": False,
                                "error": f"Stock insuficiente para {prod['nombre']}"}), 400

            sub = prod['precio_venta'] * item['cantidad']
            iva_prod = sub * (prod['iva'] / 100)
            ganancia_prod = (prod['precio_venta'] - prod['precio_compra']) * item['cantidad']

            subtotal += sub
            iva_total += iva_prod
            ganancia_total += ganancia_prod

            detalle.append({
                'producto_id': prod['id'],
                'cantidad': item['cantidad'],
                'precio': prod['precio_venta'],
                'precio_compra': prod['precio_compra'],
                'subtotal': sub,
                'ganancia': ganancia_prod
            })

        total = subtotal + iva_total

        suma_pagos = sum(p['monto'] for p in pagos)
        if suma_pagos < total - 0.01:
            return jsonify({"ok": False,
                            "error": f"Los pagos (${suma_pagos:.2f}) no cubren el total (${total:.2f})"}), 400

        if len(pagos) == 1:
            forma_pago = pagos[0]['metodo']
            monto_recibido = pagos[0]['monto']
            cambio = monto_recibido - total
        else:
            forma_pago = 'mixto'
            monto_recibido = suma_pagos
            cambio = suma_pagos - total

        numero = "F" + datetime.now().strftime("%Y%m%d%H%M%S")
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.execute("""
            INSERT INTO ventas (numero_factura, fecha, usuario_id, subtotal, iva, total,
                                forma_pago, monto_recibido, cambio, ganancia)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (numero, fecha, session['usuario_id'], subtotal, iva_total, total,
              forma_pago, monto_recibido, cambio, ganancia_total))
        venta_id = cur.lastrowid

        for p in pagos:
            conn.execute("""
                INSERT INTO venta_pagos (venta_id, metodo, monto) VALUES (?,?,?)
            """, (venta_id, p['metodo'], p['monto']))

        for d in detalle:
            conn.execute("""
                INSERT INTO venta_detalle (venta_id, producto_id, cantidad,
                                           precio_unitario, subtotal,
                                           precio_compra, ganancia)
                VALUES (?,?,?,?,?,?,?)
            """, (venta_id, d['producto_id'], d['cantidad'], d['precio'],
                  d['subtotal'], d['precio_compra'], d['ganancia']))

            conn.execute("UPDATE productos SET stock_actual = stock_actual - ? WHERE id = ?",
                         (d['cantidad'], d['producto_id']))

            conn.execute("""
                INSERT INTO movimientos (producto_id, tipo, cantidad, motivo, fecha, usuario_id)
                VALUES (?,?,?,?,?,?)
            """, (d['producto_id'], 'salida', d['cantidad'], f'Venta {numero}', fecha,
                  session['usuario_id']))

        conn.commit()
        return jsonify({
            "ok": True,
            "venta_id": venta_id,
            "numero_factura": numero,
            "subtotal": round(subtotal, 2),
            "iva": round(iva_total, 2),
            "total": round(total, 2),
            "ganancia": round(ganancia_total, 2),
            "cambio": round(cambio, 2)
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()

# ---------- Productos ----------
@app.route('/productos')
@login_required
def productos():
    conn = get_db()
    lista = conn.execute("""
        SELECT p.*, c.nombre as categoria FROM productos p
        LEFT JOIN categorias c ON p.categoria_id = c.id
        ORDER BY p.nombre
    """).fetchall()
    categorias = conn.execute("SELECT * FROM categorias ORDER BY nombre").fetchall()
    conn.close()
    return render_template('productos.html',
                           productos=[dict(p) for p in lista],
                           categorias=[dict(c) for c in categorias])

@app.route('/productos/guardar', methods=['POST'])
@login_required
def guardar_producto():
    d = request.form
    conn = get_db()
    if d.get('id'):
        conn.execute("""
            UPDATE productos SET codigo_barras=?, nombre=?, categoria_id=?,
                precio_compra=?, precio_venta=?, stock_minimo=?, unidad_medida=?, iva=?
            WHERE id=?
        """, (d['codigo_barras'], d['nombre'], d['categoria_id'] or None,
              d['precio_compra'], d['precio_venta'], d['stock_minimo'],
              d['unidad_medida'], d['iva'], d['id']))
    else:
        conn.execute("""
            INSERT INTO productos (codigo_barras, nombre, categoria_id, precio_compra,
                precio_venta, stock_actual, stock_minimo, unidad_medida, iva)
            VALUES (?,?,?,?,?,0,?,?,?)
        """, (d['codigo_barras'], d['nombre'], d['categoria_id'] or None,
              d['precio_compra'], d['precio_venta'], d['stock_minimo'],
              d['unidad_medida'], d['iva']))
    conn.commit()
    conn.close()
    flash("Producto guardado", "success")
    return redirect(url_for('productos'))

@app.route('/productos/eliminar/<int:pid>')
@login_required
def eliminar_producto(pid):
    conn = get_db()
    conn.execute("UPDATE productos SET activo = 0 WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    flash("Producto desactivado", "warning")
    return redirect(url_for('productos'))

# ---------- Inventario ----------
@app.route('/inventario')
@login_required
def inventario():
    conn = get_db()
    lista = conn.execute("""
        SELECT p.*, c.nombre as categoria FROM productos p
        LEFT JOIN categorias c ON p.categoria_id = c.id
        WHERE p.activo = 1
        ORDER BY p.stock_actual ASC
    """).fetchall()
    conn.close()
    return render_template('inventario.html',
                           productos=[dict(p) for p in lista])

@app.route('/inventario/ajuste', methods=['POST'])
@login_required
def ajuste_inventario():
    d = request.form
    producto_id = d['producto_id']
    tipo = d['tipo']
    cantidad = float(d['cantidad'])
    motivo = d.get('motivo', '')

    conn = get_db()
    prod = conn.execute("SELECT stock_actual FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if not prod:
        conn.close()
        flash("Producto no encontrado", "danger")
        return redirect(url_for('inventario'))

    if tipo == 'entrada':
        nuevo_stock = prod['stock_actual'] + cantidad
        mov_cantidad = cantidad
    elif tipo == 'salida':
        nuevo_stock = prod['stock_actual'] - cantidad
        if nuevo_stock < 0:
            conn.close()
            flash("No puedes dejar el stock en negativo", "danger")
            return redirect(url_for('inventario'))
        mov_cantidad = cantidad
    else:
        nuevo_stock = cantidad
        mov_cantidad = abs(cantidad - prod['stock_actual'])

    conn.execute("UPDATE productos SET stock_actual = ? WHERE id = ?", (nuevo_stock, producto_id))
    conn.execute("""
        INSERT INTO movimientos (producto_id, tipo, cantidad, motivo, fecha, usuario_id)
        VALUES (?,?,?,?,?,?)
    """, (producto_id, tipo, mov_cantidad, motivo,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session['usuario_id']))
    conn.commit()
    conn.close()
    flash("Inventario actualizado", "success")
    return redirect(url_for('inventario'))

# ---------- Ventas ----------
@app.route('/ventas')
@login_required
def ventas():
    rango = request.args.get('rango', 'hoy')

    conn = get_db()
    arch = get_archive_db()

    hoy = datetime.now()
    if rango == 'hoy':
        desde = hoy.strftime("%Y-%m-%d 00:00:00")
    elif rango == 'semana':
        desde = (hoy - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
    elif rango == 'quincena':
        desde = (hoy - timedelta(days=15)).strftime("%Y-%m-%d 00:00:00")
    else:
        desde = "1970-01-01 00:00:00"

    ventas = conn.execute("""
        SELECT v.*, u.nombre as usuario FROM ventas v
        LEFT JOIN usuarios u ON v.usuario_id = u.id
        WHERE v.fecha >= ?
        ORDER BY v.id DESC LIMIT 500
    """, (desde,)).fetchall()

    ventas_arch = []
    if rango in ('todas', 'quincena'):
        ventas_arch = arch.execute("""
            SELECT v.*, '(archivado)' as usuario FROM ventas v
            WHERE v.fecha >= ?
            ORDER BY v.id DESC LIMIT 500
        """, (desde,)).fetchall()

    todas = [dict(v) for v in ventas] + [dict(v) for v in ventas_arch]
    todas.sort(key=lambda x: x['fecha'], reverse=True)

    t1 = conn.execute("SELECT COALESCE(SUM(total),0) as t, COALESCE(SUM(ganancia),0) as g FROM ventas").fetchone()
    t2 = arch.execute("SELECT COALESCE(SUM(total),0) as t, COALESCE(SUM(ganancia),0) as g FROM ventas").fetchone()
    totales = {
        'total': (t1['t'] or 0) + (t2['t'] or 0),
        'ganancia': (t1['g'] or 0) + (t2['g'] or 0),
        'num_principal': conn.execute("SELECT COUNT(*) as c FROM ventas").fetchone()['c'],
        'num_archivo': arch.execute("SELECT COUNT(*) as c FROM ventas").fetchone()['c']
    }

    conn.close()
    arch.close()

    return render_template('ventas.html',
                           ventas=todas,
                           totales=totales,
                           rango=rango)

@app.route('/ventas/<int:vid>')
@login_required
def ver_venta(vid):
    conn = get_db()
    venta = conn.execute("SELECT * FROM ventas WHERE id = ?", (vid,)).fetchone()
    db = conn
    if not venta:
        db = get_archive_db()
        venta = db.execute("SELECT * FROM ventas WHERE id = ?", (vid,)).fetchone()

    if not venta:
        conn.close()
        flash("Venta no encontrada", "danger")
        return redirect(url_for('ventas'))

    detalle = db.execute("""
        SELECT d.*, COALESCE(p.nombre, 'Producto #' || d.producto_id) as nombre
        FROM venta_detalle d
        LEFT JOIN productos p ON d.producto_id = p.id
        WHERE d.venta_id = ?
    """, (vid,)).fetchall()
    pagos = db.execute("SELECT * FROM venta_pagos WHERE venta_id = ?", (vid,)).fetchall()

    conn.close()
    if db is not conn:
        db.close()

    return render_template('detalle_venta.html',
                           venta=dict(venta),
                           detalle=[dict(d) for d in detalle],
                           pagos=[dict(p) for p in pagos])

# ---------- Reportes ----------
@app.route('/reportes')
@login_required
def reportes():
    conn = get_db()
    arch = get_archive_db()
    hoy = datetime.now().strftime("%Y-%m-%d")

    ventas_hoy = conn.execute("""
        SELECT COUNT(*) as num,
               COALESCE(SUM(total),0) as total,
               COALESCE(SUM(ganancia),0) as ganancia
        FROM ventas WHERE date(fecha) = ?
    """, (hoy,)).fetchone()

    top1 = conn.execute("""
        SELECT p.nombre, SUM(d.cantidad) as cantidad,
               SUM(d.subtotal) as total, SUM(d.ganancia) as ganancia
        FROM venta_detalle d
        JOIN productos p ON d.producto_id = p.id
        GROUP BY d.producto_id
    """).fetchall()

    top2 = arch.execute("""
        SELECT 'Producto #' || d.producto_id as nombre,
               SUM(d.cantidad) as cantidad,
               SUM(d.subtotal) as total, SUM(d.ganancia) as ganancia
        FROM venta_detalle d
        GROUP BY d.producto_id
    """).fetchall()

    combinado = {}
    for r in top1:
        combinado[r['nombre']] = {'nombre': r['nombre'], 'cantidad': r['cantidad'],
                                  'total': r['total'], 'ganancia': r['ganancia']}
    for r in top2:
        if r['nombre'] in combinado:
            combinado[r['nombre']]['cantidad'] += r['cantidad']
            combinado[r['nombre']]['total'] += r['total']
            combinado[r['nombre']]['ganancia'] += r['ganancia']
        else:
            combinado[r['nombre']] = dict(r)

    top_productos = sorted(combinado.values(), key=lambda x: x['cantidad'], reverse=True)[:10]

    stock_bajo = conn.execute("""
        SELECT nombre, stock_actual, stock_minimo FROM productos
        WHERE activo = 1 AND stock_actual <= stock_minimo
        ORDER BY stock_actual ASC
    """).fetchall()

    conn.close()
    arch.close()

    return render_template('reportes.html',
                           ventas_hoy=dict(ventas_hoy) if ventas_hoy else {'num':0,'total':0,'ganancia':0},
                           top_productos=top_productos,
                           stock_bajo=[dict(p) for p in stock_bajo])

# ---------- Archivado manual ----------
@app.route('/admin/archivar')
@login_required
def admin_archivar():
    if session.get('rol') != 'admin':
        flash("Solo admin puede archivar", "danger")
        return redirect(url_for('ventas'))
    n = archivar_ventas_antiguas(15)
    flash(f"{n} ventas archivadas correctamente", "success")
    return redirect(url_for('ventas'))

# ---------- Asistente RAG ----------
@app.route('/asistente')
@login_required
def asistente():
    return render_template('asistente.html')

@app.route('/api/asistente', methods=['POST'])
@login_required
def api_asistente():
    data = request.get_json()
    pregunta = data.get('pregunta', '').strip()
    if not pregunta:
        return jsonify({'ok': False, 'error': 'Pregunta vacía'}), 400
    resultado = rag_preguntar(pregunta)
    return jsonify(resultado)

# ---------- Main ----------
if __name__ == '__main__':
    init_db()

    scheduler.init_app(app)
    scheduler.add_job(
        id='actualizar_tasa',
        func=actualizar_tasa_diaria,
        trigger='cron', hour=8, minute=0,
        replace_existing=True
    )
    scheduler.add_job(
        id='archivar_ventas',
        func=archivar_ventas_antiguas,
        trigger='cron', hour=2, minute=0,
        kwargs={'dias': 15},
        replace_existing=True
    )
    scheduler.start()

    actualizar_tasa_diaria()

    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)