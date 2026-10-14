import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), 'minisuper.db')
ARCHIVE_PATH = os.path.join(os.path.dirname(__file__), 'ventas_archivadas.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def get_archive_db():
    """Abre la BD de archivo, creándola si no existe con el mismo esquema de ventas."""
    conn = sqlite3.connect(ARCHIVE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ventas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_factura TEXT NOT NULL,
            fecha TEXT NOT NULL,
            usuario_id INTEGER,
            subtotal REAL NOT NULL,
            iva REAL NOT NULL,
            total REAL NOT NULL,
            forma_pago TEXT NOT NULL,
            monto_recibido REAL,
            cambio REAL,
            ganancia REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS venta_detalle (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venta_id INTEGER NOT NULL,
            producto_id INTEGER NOT NULL,
            cantidad REAL NOT NULL,
            precio_unitario REAL NOT NULL,
            subtotal REAL NOT NULL,
            precio_compra REAL DEFAULT 0,
            ganancia REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS venta_pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venta_id INTEGER NOT NULL,
            metodo TEXT NOT NULL,
            monto REAL NOT NULL
        )
    """)
    conn.commit()
    return conn

def init_db():
    conn = get_db()
    with open(os.path.join(os.path.dirname(__file__), 'schema.sql'), 'r', encoding='utf-8') as f:
        conn.executescript(f.read())

    cur = conn.execute("SELECT COUNT(*) FROM usuarios")
    if cur.fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO usuarios (nombre, usuario, password_hash, rol) VALUES (?,?,?,?)",
            ("Administrador", "admin", generate_password_hash("admin123"), "admin")
        )

    cur = conn.execute("SELECT COUNT(*) FROM configuracion")
    if cur.fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO configuracion (id, nombre_negocio, tasa_iva, mensaje_ticket) VALUES (1,?,?,?)",
            ("Mi Mini Súper", 16, "¡Gracias por su compra!")
        )

    cur = conn.execute("SELECT COUNT(*) FROM categorias")
    if cur.fetchone()[0] == 0:
        for cat in ["Bebidas", "Lácteos", "Panadería", "Limpieza", "Snacks"]:
            conn.execute("INSERT INTO categorias (nombre) VALUES (?)", (cat,))

    conn.commit()
    conn.close()

    # Crear BD de archivo desde el arranque
    get_archive_db().close()

# ---------- ARCHIVADO ----------
def archivar_ventas_antiguas(dias=15):
    """
    Mueve las ventas con más de `dias` días de antigüedad a la BD de archivo.
    Devuelve cuántas ventas se archivaron.
    """
    from datetime import datetime, timedelta

    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    arch = get_archive_db()

    try:
        ventas = conn.execute(
            "SELECT * FROM ventas WHERE fecha < ? ORDER BY id",
            (limite,)
        ).fetchall()

        if not ventas:
            return 0

        for v in ventas:
            cur = arch.execute("""
                INSERT INTO ventas (numero_factura, fecha, usuario_id, subtotal, iva, total,
                                    forma_pago, monto_recibido, cambio, ganancia)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (v['numero_factura'], v['fecha'], v['usuario_id'], v['subtotal'],
                  v['iva'], v['total'], v['forma_pago'], v['monto_recibido'],
                  v['cambio'], v['ganancia']))
            nuevo_id = cur.lastrowid

            detalles = conn.execute(
                "SELECT * FROM venta_detalle WHERE venta_id = ?", (v['id'],)
            ).fetchall()
            for d in detalles:
                arch.execute("""
                    INSERT INTO venta_detalle (venta_id, producto_id, cantidad,
                                               precio_unitario, subtotal,
                                               precio_compra, ganancia)
                    VALUES (?,?,?,?,?,?,?)
                """, (nuevo_id, d['producto_id'], d['cantidad'], d['precio_unitario'],
                      d['subtotal'], d['precio_compra'], d['ganancia']))

            pagos = conn.execute(
                "SELECT * FROM venta_pagos WHERE venta_id = ?", (v['id'],)
            ).fetchall()
            for p in pagos:
                arch.execute("""
                    INSERT INTO venta_pagos (venta_id, metodo, monto) VALUES (?,?,?)
                """, (nuevo_id, p['metodo'], p['monto']))

        arch.commit()

        ids_a_borrar = [v['id'] for v in ventas]
        placeholders = ','.join('?' * len(ids_a_borrar))
        conn.execute(f"DELETE FROM venta_pagos WHERE venta_id IN ({placeholders})", ids_a_borrar)
        conn.execute(f"DELETE FROM venta_detalle WHERE venta_id IN ({placeholders})", ids_a_borrar)
        conn.execute(f"DELETE FROM ventas WHERE id IN ({placeholders})", ids_a_borrar)
        conn.commit()

        print(f"[ARCHIVADO] {len(ventas)} ventas movidas al archivo (más de {dias} días).")
        return len(ventas)
    except Exception as e:
        conn.rollback()
        arch.rollback()
        print(f"[ERROR ARCHIVADO] {e}")
        return 0
    finally:
        conn.close()
        arch.close()

if __name__ == "__main__":
    init_db()
    print("Base de datos inicializada correctamente.")