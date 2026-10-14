import ollama
from datetime import datetime, timedelta
from database import get_db, get_archive_db

# ---------- Consultas a la base de datos según la pregunta ----------
def obtener_contexto(pregunta):
    """Analiza la pregunta y devuelve datos relevantes de la BD como contexto."""
    pregunta_lower = pregunta.lower()
    conn = get_db()
    arch = get_archive_db()
    contexto_partes = []

    try:
        # --- Ventas de hoy ---
        if any(k in pregunta_lower for k in ['vendí hoy', 'ventas de hoy', 'vendido hoy', 'facturé hoy']):
            hoy = datetime.now().strftime("%Y-%m-%d")
            r = conn.execute("""
                SELECT COUNT(*) as num, COALESCE(SUM(total),0) as total,
                       COALESCE(SUM(ganancia),0) as ganancia
                FROM ventas WHERE date(fecha) = ?
            """, (hoy,)).fetchone()
            contexto_partes.append(
                f"VENTAS DE HOY ({hoy}): {r['num']} facturas, "
                f"total facturado ${r['total']:.2f}, ganancia ${r['ganancia']:.2f}."
            )

        # --- Ventas de la semana ---
        if any(k in pregunta_lower for k in ['semana', 'últimos 7 días', 'ultima semana']):
            desde = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            r = conn.execute("""
                SELECT COUNT(*) as num, COALESCE(SUM(total),0) as total,
                       COALESCE(SUM(ganancia),0) as ganancia
                FROM ventas WHERE date(fecha) >= ?
            """, (desde,)).fetchone()
            contexto_partes.append(
                f"VENTAS ÚLTIMA SEMANA (desde {desde}): {r['num']} facturas, "
                f"total facturado ${r['total']:.2f}, ganancia ${r['ganancia']:.2f}."
            )

        # --- Ventas de la quincena ---
        if any(k in pregunta_lower for k in ['quincena', 'últimos 15 días', '15 dias']):
            desde = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
            r = conn.execute("""
                SELECT COUNT(*) as num, COALESCE(SUM(total),0) as total,
                       COALESCE(SUM(ganancia),0) as ganancia
                FROM ventas WHERE date(fecha) >= ?
            """, (desde,)).fetchone()
            contexto_partes.append(
                f"VENTAS QUINCENA (desde {desde}): {r['num']} facturas, "
                f"total facturado ${r['total']:.2f}, ganancia ${r['ganancia']:.2f}."
            )

        # --- Stock bajo ---
        if any(k in pregunta_lower for k in ['stock bajo', 'stock mínimo', 'reponer', 'agotar', 'faltan', 'poco stock']):
            items = conn.execute("""
                SELECT nombre, stock_actual, stock_minimo FROM productos
                WHERE activo = 1 AND stock_actual <= stock_minimo
                ORDER BY stock_actual ASC LIMIT 20
            """).fetchall()
            if items:
                lista = ", ".join([f"{i['nombre']} (stock: {i['stock_actual']}, mín: {i['stock_minimo']})" for i in items])
                contexto_partes.append(f"PRODUCTOS CON STOCK BAJO: {lista}")
            else:
                contexto_partes.append("PRODUCTOS CON STOCK BAJO: Ninguno, todo el inventario está por encima del mínimo.")

        # --- Todos los productos ---
        if any(k in pregunta_lower for k in ['productos', 'inventario', 'catálogo', 'cuántos productos']):
            total = conn.execute("SELECT COUNT(*) as c FROM productos WHERE activo = 1").fetchone()['c']
            contexto_partes.append(f"TOTAL DE PRODUCTOS ACTIVOS: {total}")

        # --- Producto más vendido ---
        if any(k in pregunta_lower for k in ['más vendido', 'mas vendido', 'top productos', 'mejor vendido']):
            items = conn.execute("""
                SELECT p.nombre, SUM(d.cantidad) as cant, SUM(d.subtotal) as total
                FROM venta_detalle d
                JOIN productos p ON d.producto_id = p.id
                GROUP BY d.producto_id
                ORDER BY cant DESC LIMIT 5
            """).fetchall()
            if items:
                lista = ", ".join([f"{i['nombre']} ({i['cant']} unidades, ${i['total']:.2f})" for i in items])
                contexto_partes.append(f"TOP 5 PRODUCTOS MÁS VENDIDOS: {lista}")
            else:
                contexto_partes.append("TOP PRODUCTOS: Aún no hay ventas registradas.")

        # --- Ganancia ---
        if any(k in pregunta_lower for k in ['ganancia', 'gané', 'gane', 'margen']):
            r = conn.execute("""
                SELECT COALESCE(SUM(ganancia),0) as g, COALESCE(SUM(total),0) as t
                FROM ventas
            """).fetchone()
            arch_r = arch.execute("""
                SELECT COALESCE(SUM(ganancia),0) as g, COALESCE(SUM(total),0) as t
                FROM ventas
            """).fetchone()
            total_gan = r['g'] + arch_r['g']
            total_fac = r['t'] + arch_r['t']
            contexto_partes.append(
                f"GANANCIA HISTÓRICA TOTAL: ${total_gan:.2f} sobre facturación total de ${total_fac:.2f}"
            )

        # --- Mejores márgenes por producto ---
        if any(k in pregunta_lower for k in ['margen', 'rentabilidad', 'más rentable', 'mas rentable']):
            items = conn.execute("""
                SELECT nombre, precio_compra, precio_venta,
                       (precio_venta - precio_compra) as margen,
                       CASE WHEN precio_venta > 0
                            THEN ((precio_venta - precio_compra) / precio_venta * 100)
                            ELSE 0 END as pct
                FROM productos WHERE activo = 1
                ORDER BY pct DESC LIMIT 10
            """).fetchall()
            if items:
                lista = ", ".join([f"{i['nombre']} ({i['pct']:.1f}% margen, ${i['margen']:.2f})" for i in items])
                contexto_partes.append(f"PRODUCTOS CON MAYOR MARGEN: {lista}")

        # --- Tasa del BCV ---
        if any(k in pregunta_lower for k in ['tasa', 'dólar', 'dolar', 'bcv', 'bolívares', 'bolivares']):
            r = conn.execute("SELECT valor, fecha FROM tasa_cambio ORDER BY fecha DESC LIMIT 1").fetchone()
            if r:
                contexto_partes.append(f"TASA BCV ACTUAL ({r['fecha']}): 1 USD = {r['valor']:.2f} Bs")

        # Si no detectamos nada específico, damos un resumen general
        if not contexto_partes:
            hoy = datetime.now().strftime("%Y-%m-%d")
            r = conn.execute("""
                SELECT COUNT(*) as num, COALESCE(SUM(total),0) as total,
                       COALESCE(SUM(ganancia),0) as ganancia
                FROM ventas WHERE date(fecha) = ?
            """, (hoy,)).fetchone()
            total_prod = conn.execute("SELECT COUNT(*) as c FROM productos WHERE activo = 1").fetchone()['c']
            contexto_partes.append(
                f"RESUMEN GENERAL: {total_prod} productos activos. "
                f"Hoy: {r['num']} ventas, ${r['total']:.2f} facturado, ${r['ganancia']:.2f} de ganancia."
            )

    finally:
        conn.close()
        arch.close()

    return "\n".join(contexto_partes)


# ---------- Preguntar al LLM ----------
def preguntar(pregunta):
    """Recibe la pregunta, arma el contexto y consulta a Qwen3 via Ollama."""
    contexto = obtener_contexto(pregunta)

    prompt = f"""Eres un asistente experto para un mini supermercado en Venezuela.
Responde de forma clara, breve y en español, usando SOLO los datos del contexto.
Si la información no está en el contexto, di que no tienes ese dato.
No inventes cifras.

CONTEXTO (datos reales del negocio):
{contexto}

PREGUNTA DEL USUARIO: {pregunta}

RESPUESTA:"""

    try:
        respuesta = ollama.chat(
            model='qwen3:1.7b',
            messages=[{'role': 'user', 'content': prompt}],
            options={'temperature': 0.3}
        )
        return {
            'ok': True,
            'respuesta': respuesta['message']['content'].strip(),
            'contexto': contexto
        }
    except Exception as e:
        return {
            'ok': False,
            'error': f'Error al consultar el LLM: {e}'
        }