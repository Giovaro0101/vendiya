CREATE TABLE IF NOT EXISTS categorias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS productos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo_barras TEXT UNIQUE,
    nombre TEXT NOT NULL,
    categoria_id INTEGER,
    precio_compra REAL DEFAULT 0,
    precio_venta REAL NOT NULL,
    stock_actual REAL DEFAULT 0,
    stock_minimo REAL DEFAULT 0,
    unidad_medida TEXT DEFAULT 'unidad',
    iva REAL DEFAULT 0,
    activo INTEGER DEFAULT 1,
    FOREIGN KEY (categoria_id) REFERENCES categorias(id)
);

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    usuario TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    rol TEXT DEFAULT 'cajero'
);

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
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);

CREATE TABLE IF NOT EXISTS venta_detalle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venta_id INTEGER NOT NULL,
    producto_id INTEGER NOT NULL,
    cantidad REAL NOT NULL,
    precio_unitario REAL NOT NULL,
    subtotal REAL NOT NULL,
    FOREIGN KEY (venta_id) REFERENCES ventas(id),
    FOREIGN KEY (producto_id) REFERENCES productos(id)
);

CREATE TABLE IF NOT EXISTS movimientos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    producto_id INTEGER NOT NULL,
    tipo TEXT NOT NULL,          -- entrada / salida / ajuste
    cantidad REAL NOT NULL,
    motivo TEXT,
    fecha TEXT NOT NULL,
    usuario_id INTEGER,
    FOREIGN KEY (producto_id) REFERENCES productos(id)
);

CREATE TABLE IF NOT EXISTS configuracion (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    nombre_negocio TEXT,
    rif TEXT,
    direccion TEXT,
    telefono TEXT,
    tasa_iva REAL DEFAULT 16,
    mensaje_ticket TEXT
);

CREATE TABLE IF NOT EXISTS tasa_cambio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    valor REAL NOT NULL,
    fuente TEXT DEFAULT 'BCV'
);