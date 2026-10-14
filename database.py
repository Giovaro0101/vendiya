"""
Capa de datos de VendiYA (multi-negocio).

Cada negocio tiene su propia carpeta con sus propias bases SQLite:
    data/negocios/<id>/minisuper.db           datos del negocio
    data/negocios/<id>/ventas_archivadas.db   ventas antiguas
    data/negocios/<id>/backups/               respaldos
Los negocios, sus plazos y los superadministradores viven aparte, en
data/plataforma.db (ver plataforma.py).

Aislamiento: get_db() y compañía solo funcionan si hay un negocio activo
(ver negocio() / set_negocio()). Si no lo hay fallan, nunca "adivinan" una base.
"""
import sqlite3
import os
import contextlib
import contextvars
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
MASTER_PATH = os.path.join(DATA_DIR, 'plataforma.db')
MASTER_BACKUP_DIR = os.path.join(DATA_DIR, 'respaldos_plataforma')

# Archivos de la versión de un solo negocio (se importan la primera vez)
LEGACY_DB_PATH = os.path.join(BASE_DIR, 'minisuper.db')
LEGACY_ARCHIVE_PATH = os.path.join(BASE_DIR, 'ventas_archivadas.db')

# Columnas que comparten la BD principal y la de archivo (en este orden)
COLUMNAS_VENTAS = ['id', 'numero_factura', 'fecha', 'usuario_id', 'subtotal', 'iva', 'total',
                   'forma_pago', 'monto_recibido', 'cambio', 'ganancia', 'tasa', 'descuento',
                   'estado', 'cliente_id', 'deuda', 'caja_id', 'uuid',
                   'anulada_fecha', 'anulada_motivo', 'anulada_por']
COLUMNAS_DETALLE = ['id', 'venta_id', 'producto_id', 'cantidad', 'precio_unitario', 'subtotal',
                    'precio_compra', 'ganancia', 'nombre_producto']
COLUMNAS_PAGOS = ['id', 'venta_id', 'metodo', 'monto']

# Columnas añadidas después de la primera versión: se agregan solo si faltan
NUEVAS_COLUMNAS = {
    'usuarios': {'activo': 'INTEGER DEFAULT 1'},
    'ventas': {
        'tasa': 'REAL',
        'descuento': 'REAL DEFAULT 0',
        'estado': "TEXT DEFAULT 'completada'",
        'cliente_id': 'INTEGER',
        'deuda': 'REAL DEFAULT 0',
        'caja_id': 'INTEGER',
        'uuid': 'TEXT',
        'anulada_fecha': 'TEXT',
        'anulada_motivo': 'TEXT',
        'anulada_por': 'INTEGER',
    },
    'venta_detalle': {'nombre_producto': 'TEXT'},
    'movimientos': {'costo': 'REAL'},
}

TABLAS_NUEVAS = """
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    telefono TEXT,
    cedula TEXT,
    saldo REAL NOT NULL DEFAULT 0,
    activo INTEGER DEFAULT 1,
    creado TEXT
);

CREATE TABLE IF NOT EXISTS abonos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL,
    monto REAL NOT NULL,
    metodo TEXT NOT NULL,
    fecha TEXT NOT NULL,
    usuario_id INTEGER,
    nota TEXT,
    caja_id INTEGER,
    FOREIGN KEY (cliente_id) REFERENCES clientes(id)
);

CREATE TABLE IF NOT EXISTS gastos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    concepto TEXT NOT NULL,
    categoria TEXT,
    monto REAL NOT NULL,
    usuario_id INTEGER,
    pagado_de_caja INTEGER DEFAULT 0,
    caja_id INTEGER
);

CREATE TABLE IF NOT EXISTS cajas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    fecha_apertura TEXT NOT NULL,
    fondo_inicial REAL DEFAULT 0,
    fecha_cierre TEXT,
    efectivo_esperado REAL,
    efectivo_contado REAL,
    diferencia REAL,
    total_ventas REAL,
    notas TEXT,
    estado TEXT DEFAULT 'abierta'
);
"""

INDICES = """
CREATE INDEX IF NOT EXISTS idx_ventas_fecha ON ventas(fecha);
CREATE INDEX IF NOT EXISTS idx_ventas_caja ON ventas(caja_id);
CREATE INDEX IF NOT EXISTS idx_ventas_cliente ON ventas(cliente_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ventas_uuid ON ventas(uuid) WHERE uuid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_detalle_venta ON venta_detalle(venta_id);
CREATE INDEX IF NOT EXISTS idx_pagos_venta ON venta_pagos(venta_id);
CREATE INDEX IF NOT EXISTS idx_mov_producto ON movimientos(producto_id);
CREATE INDEX IF NOT EXISTS idx_abonos_cliente ON abonos(cliente_id);
CREATE INDEX IF NOT EXISTS idx_gastos_fecha ON gastos(fecha);
"""

_archivos_listos = set()      # archivos de archivo ya verificados en este proceso
_esquemas_listos = set()      # bases de negocio ya migradas en este proceso


# ============================================================
# Negocio activo
# ============================================================
_negocio_actual = contextvars.ContextVar('negocio_actual', default=None)


def negocio_actual():
    return _negocio_actual.get()


def _resolver(nid=None):
    n = nid if nid is not None else _negocio_actual.get()
    if n is None:
        raise RuntimeError("No hay un negocio activo: no se puede decidir qué base de datos usar")
    return int(n)


def negocio_dir(nid=None):
    return os.path.join(DATA_DIR, 'negocios', str(_resolver(nid)))


def db_path(nid=None):
    return os.path.join(negocio_dir(nid), 'minisuper.db')


def archive_path(nid=None):
    return os.path.join(negocio_dir(nid), 'ventas_archivadas.db')


def backup_dir(nid=None):
    return os.path.join(negocio_dir(nid), 'backups')


def asegurar_esquema(nid):
    """Migra la base del negocio la primera vez que se usa en este proceso."""
    nid = int(nid)
    ruta = db_path(nid)
    if ruta in _esquemas_listos:
        return
    if not os.path.exists(ruta):
        raise RuntimeError(f"El negocio {nid} no existe")
    _preparar_esquema(nid)


def set_negocio(nid):
    """Fija el negocio de la petición actual (None = ninguno). Devuelve el token para reset."""
    if nid is not None:
        asegurar_esquema(nid)
    return _negocio_actual.set(int(nid) if nid is not None else None)


@contextlib.contextmanager
def negocio(nid):
    """with negocio(3): ...  -> todo get_db() dentro de este bloque usa el negocio 3."""
    token = set_negocio(nid)
    try:
        yield
    finally:
        _negocio_actual.reset(token)


# ============================================================
# Conexiones
# ============================================================
def _conectar(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_db(nid=None):
    return _conectar(db_path(nid))


def _columnas(conn, tabla):
    return {r['name'] for r in conn.execute(f"PRAGMA table_info({tabla})")}


def _asegurar_columnas(conn, tabla, columnas):
    existentes = _columnas(conn, tabla)
    for nombre, ddl in columnas.items():
        if nombre not in existentes:
            conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {ddl}")


def get_archive_db(nid=None):
    """Abre la BD de archivo del negocio, creándola (y actualizándola) si hace falta."""
    ruta = archive_path(nid)
    conn = _conectar(ruta)
    if ruta not in _archivos_listos:
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
        _asegurar_columnas(conn, 'ventas', NUEVAS_COLUMNAS['ventas'])
        _asegurar_columnas(conn, 'venta_detalle', NUEVAS_COLUMNAS['venta_detalle'])
        conn.commit()
        _archivos_listos.add(ruta)
    return conn


def get_db_unificado(nid=None):
    """
    Conexión a la BD principal con el archivo adjunto y tres vistas temporales
    (v_ventas, v_detalle, v_pagos) que unen ambas. La columna `archivado` (0/1)
    distingue el origen: los ids de venta solo son únicos junto con ella.
    """
    nid = _resolver(nid)
    get_archive_db(nid).close()
    conn = get_db(nid)
    conn.execute("ATTACH DATABASE ? AS arch", (archive_path(nid),))

    def vista(nombre, tabla, columnas):
        cols = ", ".join(columnas)
        conn.execute(
            f"CREATE TEMP VIEW {nombre} AS "
            f"SELECT {cols}, 0 AS archivado FROM main.{tabla} "
            f"UNION ALL SELECT {cols}, 1 AS archivado FROM arch.{tabla}"
        )

    vista('v_ventas', 'ventas', COLUMNAS_VENTAS)
    vista('v_detalle', 'venta_detalle', COLUMNAS_DETALLE)
    vista('v_pagos', 'venta_pagos', COLUMNAS_PAGOS)
    return conn


# ============================================================
# Migración / creación de la base de un negocio
# ============================================================
def _necesita_migracion(conn):
    existe = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ventas'").fetchone()
    return bool(existe) and 'uuid' not in _columnas(conn, 'ventas')


def migrar(conn):
    """Lleva cualquier base (nueva o antigua) al esquema actual. Es idempotente."""
    for tabla, columnas in NUEVAS_COLUMNAS.items():
        _asegurar_columnas(conn, tabla, columnas)
    conn.executescript(TABLAS_NUEVAS)
    conn.executescript(INDICES)

    # Un código de barras vacío no debe contar como valor único
    conn.execute("UPDATE productos SET codigo_barras = NULL WHERE TRIM(codigo_barras) = ''")

    # Datos históricos: nombre del producto y tasa vigente al momento de cada venta
    conn.execute("""
        UPDATE venta_detalle
        SET nombre_producto = (SELECT nombre FROM productos WHERE id = venta_detalle.producto_id)
        WHERE nombre_producto IS NULL
    """)
    conn.execute("""
        UPDATE ventas
        SET tasa = (SELECT valor FROM tasa_cambio
                    WHERE fecha <= date(ventas.fecha) ORDER BY fecha DESC, id DESC LIMIT 1)
        WHERE tasa IS NULL
    """)
    conn.execute("UPDATE ventas SET estado = 'completada' WHERE estado IS NULL")
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ventas_factura ON ventas(numero_factura)")
    except sqlite3.IntegrityError:
        print("[AVISO] Hay números de factura repetidos; no se pudo crear el índice único.")
    conn.commit()


def _preparar_esquema(nid):
    """Crea/actualiza el esquema de un negocio. Hace respaldo antes de migrar una base antigua."""
    nid = int(nid)
    ruta = db_path(nid)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)

    if os.path.exists(ruta):
        previo = _conectar(ruta)
        pendiente = _necesita_migracion(previo)
        previo.close()
        if pendiente:
            hacer_backup('premigracion', nid=nid)

    conn = get_db(nid)
    with open(os.path.join(BASE_DIR, 'schema.sql'), 'r', encoding='utf-8') as f:
        conn.executescript(f.read())
    migrar(conn)
    conn.close()

    # BD de archivo: crear/actualizar y completar nombres de productos históricos
    arch = get_archive_db(nid)
    arch.execute("ATTACH DATABASE ? AS principal", (ruta,))
    arch.execute("""
        UPDATE venta_detalle
        SET nombre_producto = (SELECT nombre FROM principal.productos
                               WHERE id = venta_detalle.producto_id)
        WHERE nombre_producto IS NULL
    """)
    arch.commit()
    arch.close()
    _esquemas_listos.add(ruta)


def crear_base_negocio(nid, nombre_negocio, admin):
    """
    Crea la base de un negocio nuevo con sus datos iniciales.
    admin = {'nombre', 'usuario', 'password_hash'} (el dueño, con rol admin).
    """
    _preparar_esquema(nid)
    conn = get_db(nid)
    conn.execute(
        "INSERT INTO usuarios (nombre, usuario, password_hash, rol) VALUES (?,?,?,'admin')",
        (admin['nombre'], admin['usuario'], admin['password_hash']))
    conn.execute(
        "INSERT INTO configuracion (id, nombre_negocio, tasa_iva, mensaje_ticket) VALUES (1,?,?,?)",
        (nombre_negocio, 16, "¡Gracias por su compra!"))
    for cat in ["Bebidas", "Lácteos", "Panadería", "Limpieza", "Snacks"]:
        conn.execute("INSERT INTO categorias (nombre) VALUES (?)", (cat,))
    conn.commit()
    conn.close()


# ============================================================
# Respaldos
# ============================================================
def hacer_backup(etiqueta='auto', conservar=14, nid=None):
    """Copia consistente de las bases del negocio en su carpeta backups/."""
    nid = _resolver(nid)
    destino_dir = backup_dir(nid)
    os.makedirs(destino_dir, exist_ok=True)
    sello = datetime.now().strftime("%Y%m%d-%H%M%S")
    creados = []
    for prefijo, ruta in (('minisuper', db_path(nid)), ('archivo', archive_path(nid))):
        if not os.path.exists(ruta):
            continue
        destino = os.path.join(destino_dir, f"{prefijo}-{etiqueta}-{sello}.db")
        origen = sqlite3.connect(ruta)
        copia = sqlite3.connect(destino)
        try:
            origen.backup(copia)
        finally:
            copia.close()
            origen.close()
        creados.append(destino)
    _limpiar_backups(destino_dir, conservar)
    return creados


def _limpiar_backups(carpeta, conservar):
    """Conserva los últimos `conservar` respaldos automáticos/manuales de cada base."""
    for prefijo in ('minisuper-', 'archivo-'):
        archivos = sorted(
            (f for f in os.listdir(carpeta)
             if f.startswith(prefijo) and f.endswith('.db')
             and ('-auto-' in f or '-manual-' in f)),
            reverse=True
        )
        for viejo in archivos[conservar:]:
            try:
                os.remove(os.path.join(carpeta, viejo))
            except OSError:
                pass


def listar_backups(nid=None):
    carpeta = backup_dir(nid)
    if not os.path.isdir(carpeta):
        return []
    lista = []
    for f in sorted(os.listdir(carpeta), reverse=True):
        if f.endswith('.db') and f.startswith('minisuper-'):
            ruta = os.path.join(carpeta, f)
            lista.append({
                'nombre': f,
                'kb': round(os.path.getsize(ruta) / 1024, 1),
                'fecha': datetime.fromtimestamp(os.path.getmtime(ruta)).strftime("%Y-%m-%d %H:%M"),
            })
    return lista


def hay_backup_de_hoy(nid=None):
    carpeta = backup_dir(nid)
    if not os.path.isdir(carpeta):
        return False
    hoy = datetime.now().strftime("%Y%m%d")
    return any(f.startswith('minisuper-') and f'-{hoy}-' in f for f in os.listdir(carpeta))


# ============================================================
# ARCHIVADO (del negocio activo)
# ============================================================
def archivar_ventas_antiguas(dias=15):
    """
    Mueve las ventas con más de `dias` días de antigüedad a la BD de archivo.
    Devuelve cuántas ventas se archivaron.
    """
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    arch = get_archive_db()

    def insertar(tabla, columnas, fila, **extra):
        cols = [c for c in columnas if c != 'id']
        valores = [extra[c] if c in extra else fila[c] for c in cols]
        marcas = ",".join("?" * len(cols))
        return arch.execute(
            f"INSERT INTO {tabla} ({','.join(cols)}) VALUES ({marcas})", valores
        ).lastrowid

    try:
        ventas = conn.execute(
            "SELECT * FROM ventas WHERE fecha < ? ORDER BY id", (limite,)
        ).fetchall()
        if not ventas:
            return 0

        for v in ventas:
            nuevo_id = insertar('ventas', COLUMNAS_VENTAS, v)
            for d in conn.execute("SELECT * FROM venta_detalle WHERE venta_id = ?", (v['id'],)):
                insertar('venta_detalle', COLUMNAS_DETALLE, d, venta_id=nuevo_id)
            for p in conn.execute("SELECT * FROM venta_pagos WHERE venta_id = ?", (v['id'],)):
                insertar('venta_pagos', COLUMNAS_PAGOS, p, venta_id=nuevo_id)

        arch.commit()

        ids = [v['id'] for v in ventas]
        marcas = ','.join('?' * len(ids))
        conn.execute(f"DELETE FROM venta_pagos WHERE venta_id IN ({marcas})", ids)
        conn.execute(f"DELETE FROM venta_detalle WHERE venta_id IN ({marcas})", ids)
        conn.execute(f"DELETE FROM ventas WHERE id IN ({marcas})", ids)
        conn.commit()

        print(f"[ARCHIVADO] negocio {negocio_actual()}: {len(ventas)} ventas movidas al archivo "
              f"(más de {dias} días).")
        return len(ventas)
    except Exception as e:
        conn.rollback()
        arch.rollback()
        print(f"[ERROR ARCHIVADO] negocio {negocio_actual()}: {e}")
        return 0
    finally:
        conn.close()
        arch.close()
