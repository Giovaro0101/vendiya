import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), 'minisuper.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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

if __name__ == "__main__":
    init_db()
    print("Base de datos inicializada correctamente.")