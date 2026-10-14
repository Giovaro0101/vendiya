"""
Capa de plataforma (SaaS) de VendiYA: negocios clientes, superadministradores y tasa global.

Datos en data/plataforma.db. Los datos de cada negocio (productos, ventas...) NO están
aquí: viven en su propia carpeta (ver database.py).

Uso desde la terminal:
    python plataforma.py superadmin                  crea (o cambia) un superadministrador
    python plataforma.py crear-negocio               crea un negocio cliente
    python plataforma.py listar                      lista los negocios
"""
import os
import re
import shutil
import sqlite3
import unicodedata
import logging
from datetime import datetime, timedelta, date

from werkzeug.security import generate_password_hash, check_password_hash

import database

log = logging.getLogger('vendiya.plataforma')

# Nombres que no pueden usarse como código de negocio
RESERVADOS = {
    'admin', 'administrador', 'plataforma', 'api', 'static', 'www', 'app', 'login', 'logout',
    'registro', 'soporte', 'ayuda', 'ventas', 'pos', 'root', 'sistema', 'vendiya', 'demo-vendiya',
}
ESTADOS = ('activo', 'suspendido', 'eliminado')
PLANES = ('prueba', 'basico', 'pro')
REGEX_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{1,28}[a-z0-9]')
REGEX_USUARIO = re.compile(r'[A-Za-z0-9_.-]{3,30}')


class ErrorPlataforma(Exception):
    """Dato inválido o regla incumplida; el mensaje es apto para mostrar al usuario."""


# ============================================================
# Base central
# ============================================================
def get_master():
    os.makedirs(database.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(database.MASTER_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_master():
    conn = get_master()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS negocios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            nombre TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'activo',
            plan TEXT NOT NULL DEFAULT 'prueba',
            max_usuarios INTEGER NOT NULL DEFAULT 3,
            creado TEXT NOT NULL,
            vence TEXT,
            email TEXT,
            telefono TEXT,
            notas TEXT,
            ultimo_acceso TEXT
        );
        CREATE TABLE IF NOT EXISTS superadmins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            usuario TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            creado TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasa_global (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            valor REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            negocio_id INTEGER,
            actor TEXT,
            tipo TEXT NOT NULL,
            detalle TEXT
        );
    """)
    conn.commit()
    conn.close()


def _ahora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hoy():
    return date.today()


def registrar_evento(negocio_id, actor, tipo, detalle=''):
    conn = get_master()
    conn.execute("INSERT INTO eventos (fecha, negocio_id, actor, tipo, detalle) VALUES (?,?,?,?,?)",
                 (_ahora(), negocio_id, actor, tipo, detalle))
    conn.commit()
    conn.close()


# ============================================================
# Códigos de negocio
# ============================================================
def slugify(texto):
    base = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore').decode()
    base = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-')[:30].strip('-')
    return base


def validar_slug(slug):
    if not REGEX_SLUG.fullmatch(slug or ''):
        raise ErrorPlataforma(
            "El código del negocio debe tener 3 a 30 caracteres: letras minúsculas, números y guiones "
            "(sin empezar ni terminar con guion)")
    if slug in RESERVADOS:
        raise ErrorPlataforma("Ese código está reservado, elige otro")
    return slug


def slug_disponible(nombre):
    """Propone un código único a partir del nombre del negocio."""
    base = slugify(nombre)
    if len(base) < 3 or base in RESERVADOS:
        base = (base + '-negocio').strip('-') if base else 'negocio'
    conn = get_master()
    try:
        candidato, n = base, 1
        while conn.execute("SELECT 1 FROM negocios WHERE slug = ?", (candidato,)).fetchone() \
                or candidato in RESERVADOS:
            n += 1
            candidato = f"{base[:27]}-{n}"
        return candidato
    finally:
        conn.close()


# ============================================================
# Negocios
# ============================================================
def estado_efectivo(n):
    """Estado real de un negocio: activo, suspendido, vencido o eliminado."""
    dias = None
    if n['vence']:
        dias = (datetime.strptime(n['vence'], "%Y-%m-%d").date() - _hoy()).days
    if n['estado'] == 'eliminado':
        estado = 'eliminado'
    elif n['estado'] == 'suspendido':
        estado = 'suspendido'
    elif dias is not None and dias < 0:
        estado = 'vencido'
    else:
        estado = 'activo'
    return {'estado': estado, 'dias_restantes': dias}


def _como_dict(fila):
    if not fila:
        return None
    d = dict(fila)
    efectivo = estado_efectivo(d)          # usa el estado guardado y la fecha de vencimiento
    d['estado_guardado'] = d['estado']
    d.update(efectivo)                     # 'estado' pasa a ser el efectivo (activo/vencido/...)
    return d


def obtener_negocio(nid):
    conn = get_master()
    fila = conn.execute("SELECT * FROM negocios WHERE id = ?", (nid,)).fetchone()
    conn.close()
    return _como_dict(fila)


def obtener_por_slug(slug):
    conn = get_master()
    fila = conn.execute("SELECT * FROM negocios WHERE slug = ?", ((slug or '').strip().lower(),)).fetchone()
    conn.close()
    return _como_dict(fila)


def listar_negocios(incluir_eliminados=False):
    conn = get_master()
    sql = "SELECT * FROM negocios"
    if not incluir_eliminados:
        sql += " WHERE estado != 'eliminado'"
    filas = conn.execute(sql + " ORDER BY id DESC").fetchall()
    conn.close()
    return [_como_dict(f) for f in filas]


def crear_negocio(nombre, admin_nombre, admin_usuario, admin_password, slug=None, email='',
                  telefono='', plan='prueba', dias=14, max_usuarios=3, actor='sistema'):
    """Crea un negocio cliente completo: registro, carpeta, base de datos y usuario dueño."""
    nombre = (nombre or '').strip()
    admin_nombre = (admin_nombre or '').strip() or nombre
    admin_usuario = (admin_usuario or '').strip()
    if not nombre or len(nombre) > 80:
        raise ErrorPlataforma("El nombre del negocio es obligatorio (máximo 80 caracteres)")
    if not REGEX_USUARIO.fullmatch(admin_usuario):
        raise ErrorPlataforma("El usuario debe tener 3-30 caracteres (letras, números, . _ -)")
    if len(admin_password or '') < 6:
        raise ErrorPlataforma("La contraseña debe tener al menos 6 caracteres")
    if plan not in PLANES:
        raise ErrorPlataforma("Plan inválido")
    slug = validar_slug((slug or '').strip().lower()) if slug else slug_disponible(nombre)
    vence = (_hoy() + timedelta(days=int(dias))).isoformat() if dias else None

    conn = get_master()
    try:
        try:
            cur = conn.execute("""
                INSERT INTO negocios (slug, nombre, estado, plan, max_usuarios, creado, vence, email, telefono)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (slug, nombre, 'activo', plan, int(max_usuarios), _ahora(), vence,
                  (email or '').strip()[:120], (telefono or '').strip()[:30]))
        except sqlite3.IntegrityError:
            raise ErrorPlataforma("Ya existe un negocio con ese código")
        nid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    try:
        database.crear_base_negocio(nid, nombre, {
            'nombre': admin_nombre, 'usuario': admin_usuario,
            'password_hash': generate_password_hash(admin_password)})
        tg = tasa_global()
        if tg:
            with database.negocio(nid):
                c = database.get_db()
                c.execute("INSERT INTO tasa_cambio (fecha, valor, fuente) VALUES (?,?,?)",
                          (tg['fecha'], tg['valor'], 'BCV'))
                c.commit()
                c.close()
    except Exception:
        # No dejar un negocio a medias
        conn = get_master()
        conn.execute("DELETE FROM negocios WHERE id = ?", (nid,))
        conn.commit()
        conn.close()
        shutil.rmtree(database.negocio_dir(nid), ignore_errors=True)
        raise
    registrar_evento(nid, actor, 'negocio_creado', f"{nombre} ({slug}), plan {plan}")
    return obtener_negocio(nid)


def cambiar_estado(nid, estado, actor='sistema'):
    if estado not in ('activo', 'suspendido'):
        raise ErrorPlataforma("Estado inválido")
    conn = get_master()
    conn.execute("UPDATE negocios SET estado = ? WHERE id = ? AND estado != 'eliminado'", (estado, nid))
    conn.commit()
    conn.close()
    registrar_evento(nid, actor, f'negocio_{estado}')


def extender(nid, dias=None, fecha=None, actor='sistema'):
    """Alarga la suscripción: +N días desde hoy (o desde el vencimiento si aún no venció) o a una fecha."""
    n = obtener_negocio(nid)
    if not n:
        raise ErrorPlataforma("Negocio no encontrado")
    if fecha:
        try:
            nuevo = datetime.strptime(fecha, "%Y-%m-%d").date()
        except ValueError:
            raise ErrorPlataforma("Fecha inválida")
    else:
        base = _hoy()
        if n['vence']:
            base = max(base, datetime.strptime(n['vence'], "%Y-%m-%d").date())
        nuevo = base + timedelta(days=int(dias))
    conn = get_master()
    conn.execute("UPDATE negocios SET vence = ? WHERE id = ?", (nuevo.isoformat(), nid))
    conn.commit()
    conn.close()
    registrar_evento(nid, actor, 'vencimiento', nuevo.isoformat())
    return nuevo.isoformat()


def quitar_vencimiento(nid, actor='sistema'):
    conn = get_master()
    conn.execute("UPDATE negocios SET vence = NULL WHERE id = ?", (nid,))
    conn.commit()
    conn.close()
    registrar_evento(nid, actor, 'vencimiento', 'sin vencimiento')


def actualizar_datos(nid, plan, max_usuarios, email, telefono, notas, actor='sistema'):
    if plan not in PLANES:
        raise ErrorPlataforma("Plan inválido")
    try:
        mu = int(max_usuarios)
    except (TypeError, ValueError):
        raise ErrorPlataforma("El máximo de usuarios debe ser un número")
    if not 1 <= mu <= 1000:
        raise ErrorPlataforma("El máximo de usuarios debe estar entre 1 y 1000")
    conn = get_master()
    conn.execute("UPDATE negocios SET plan=?, max_usuarios=?, email=?, telefono=?, notas=? WHERE id=?",
                 (plan, mu, (email or '').strip()[:120], (telefono or '').strip()[:30],
                  (notas or '').strip()[:500], nid))
    conn.commit()
    conn.close()
    registrar_evento(nid, actor, 'datos_actualizados', f'plan {plan}, {mu} usuarios')


def eliminar_negocio(nid, actor='sistema'):
    """Borrado lógico: el negocio deja de poder entrar; sus datos quedan en disco."""
    conn = get_master()
    conn.execute("UPDATE negocios SET estado = 'eliminado' WHERE id = ?", (nid,))
    conn.commit()
    conn.close()
    registrar_evento(nid, actor, 'negocio_eliminado')


def registrar_acceso(nid):
    conn = get_master()
    conn.execute("UPDATE negocios SET ultimo_acceso = ? WHERE id = ?", (_ahora(), nid))
    conn.commit()
    conn.close()


def usuarios_de(nid):
    with database.negocio(nid):
        conn = database.get_db()
        filas = conn.execute(
            "SELECT id, nombre, usuario, rol, activo FROM usuarios ORDER BY id").fetchall()
        conn.close()
    return [dict(f) for f in filas]


def reset_clave(nid, usuario, nueva, actor='sistema'):
    if len(nueva or '') < 6:
        raise ErrorPlataforma("La contraseña debe tener al menos 6 caracteres")
    with database.negocio(nid):
        conn = database.get_db()
        cur = conn.execute("UPDATE usuarios SET password_hash = ?, activo = 1 WHERE usuario = ?",
                           (generate_password_hash(nueva), usuario))
        conn.commit()
        conn.close()
    if cur.rowcount == 0:
        raise ErrorPlataforma("Ese usuario no existe en el negocio")
    registrar_evento(nid, actor, 'clave_restablecida', usuario)


def estadisticas(nid):
    """Actividad de un negocio (abre su base): para el panel del superadministrador."""
    with database.negocio(nid):
        conn = database.get_db_unificado()
        desde = (_hoy() - timedelta(days=30)).isoformat() + " 00:00:00"
        r = conn.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS total, MAX(fecha) AS ultima
            FROM v_ventas WHERE fecha >= ? AND estado = 'completada'
        """, (desde,)).fetchone()
        out = {
            'ventas_30d': r['n'], 'total_30d': r['total'], 'ultima_venta': r['ultima'],
            'productos': conn.execute("SELECT COUNT(*) FROM productos WHERE activo = 1").fetchone()[0],
            'usuarios': conn.execute("SELECT COUNT(*) FROM usuarios WHERE activo != 0").fetchone()[0],
            'clientes': conn.execute("SELECT COUNT(*) FROM clientes").fetchone()[0],
        }
        conn.close()
    carpeta = database.negocio_dir(nid)
    tam = 0
    for raiz, _, archivos in os.walk(carpeta):
        for a in archivos:
            tam += os.path.getsize(os.path.join(raiz, a))
    out['mb'] = round(tam / 1048576, 1)
    return out


def resumen_plataforma():
    negocios = listar_negocios()
    return {
        'total': len(negocios),
        'activos': sum(1 for n in negocios if n['estado'] == 'activo'),
        'vencidos': sum(1 for n in negocios if n['estado'] == 'vencido'),
        'suspendidos': sum(1 for n in negocios if n['estado'] == 'suspendido'),
        'por_vencer': sum(1 for n in negocios
                          if n['estado'] == 'activo' and n['dias_restantes'] is not None
                          and n['dias_restantes'] <= 7),
    }


# ============================================================
# Superadministradores
# ============================================================
def hay_superadmin():
    conn = get_master()
    r = conn.execute("SELECT COUNT(*) FROM superadmins").fetchone()[0]
    conn.close()
    return r > 0


def crear_superadmin(usuario, password, nombre='Superadministrador'):
    if not REGEX_USUARIO.fullmatch(usuario or ''):
        raise ErrorPlataforma("El usuario debe tener 3-30 caracteres (letras, números, . _ -)")
    if len(password or '') < 8:
        raise ErrorPlataforma("La contraseña del superadministrador debe tener al menos 8 caracteres")
    conn = get_master()
    existe = conn.execute("SELECT id FROM superadmins WHERE usuario = ?", (usuario,)).fetchone()
    if existe:
        conn.execute("UPDATE superadmins SET password_hash = ? WHERE id = ?",
                     (generate_password_hash(password), existe['id']))
    else:
        conn.execute("INSERT INTO superadmins (nombre, usuario, password_hash, creado) VALUES (?,?,?,?)",
                     (nombre, usuario, generate_password_hash(password), _ahora()))
    conn.commit()
    conn.close()


_HASH_FALSO = generate_password_hash('x' * 12)


def verificar_superadmin(usuario, password):
    conn = get_master()
    fila = conn.execute("SELECT * FROM superadmins WHERE usuario = ?", (usuario,)).fetchone()
    conn.close()
    if not fila:
        check_password_hash(_HASH_FALSO, password or '')     # mismo costo que un usuario real
        return None
    return fila if check_password_hash(fila['password_hash'], password or '') else None


# ============================================================
# Tasa de cambio global (la misma para todos los negocios)
# ============================================================
def tasa_global():
    conn = get_master()
    fila = conn.execute("SELECT fecha, valor FROM tasa_global ORDER BY fecha DESC, id DESC LIMIT 1").fetchone()
    conn.close()
    return dict(fila) if fila else None


def guardar_tasa_global(valor):
    conn = get_master()
    hoy = _hoy().isoformat()
    conn.execute("DELETE FROM tasa_global WHERE fecha = ?", (hoy,))
    conn.execute("INSERT INTO tasa_global (fecha, valor) VALUES (?,?)", (hoy, valor))
    conn.commit()
    conn.close()


def distribuir_tasa(valor):
    """Escribe la tasa del BCV en cada negocio (sin pisar una tasa manual de hoy)."""
    hoy = _hoy().isoformat()

    def aplicar():
        c = database.get_db()
        manual = c.execute("SELECT 1 FROM tasa_cambio WHERE fecha = ? AND fuente = 'Manual'",
                           (hoy,)).fetchone()
        if not manual:
            c.execute("DELETE FROM tasa_cambio WHERE fecha = ?", (hoy,))
            c.execute("INSERT INTO tasa_cambio (fecha, valor, fuente) VALUES (?,?,'BCV')", (hoy, valor))
            c.commit()
        c.close()

    para_cada_negocio(aplicar)


# ============================================================
# Tareas sobre todos los negocios
# ============================================================
def para_cada_negocio(fn, incluir_suspendidos=True):
    """Ejecuta fn() dentro del contexto de cada negocio. Un fallo no detiene a los demás."""
    hechos = 0
    for n in listar_negocios():
        if n['estado'] == 'suspendido' and not incluir_suspendidos:
            continue
        try:
            with database.negocio(n['id']):
                fn()
            hechos += 1
        except Exception:
            log.exception("Falló una tarea en el negocio %s", n['id'])
    return hechos


def migrar_todos():
    """Lleva la base de cada negocio al esquema actual (con respaldo previo si hace falta)."""
    return para_cada_negocio(lambda: None)


def backup_master(conservar=14):
    os.makedirs(database.MASTER_BACKUP_DIR, exist_ok=True)
    destino = os.path.join(database.MASTER_BACKUP_DIR,
                           f"plataforma-{datetime.now():%Y%m%d-%H%M%S}.db")
    origen = sqlite3.connect(database.MASTER_PATH)
    copia = sqlite3.connect(destino)
    try:
        origen.backup(copia)
    finally:
        copia.close()
        origen.close()
    archivos = sorted(os.listdir(database.MASTER_BACKUP_DIR), reverse=True)
    for viejo in archivos[conservar:]:
        try:
            os.remove(os.path.join(database.MASTER_BACKUP_DIR, viejo))
        except OSError:
            pass
    return destino


def backup_todos():
    para_cada_negocio(lambda: database.hacer_backup('auto'))
    backup_master()


# ============================================================
# Importar la base de la versión de un solo negocio
# ============================================================
def importar_legado():
    """
    Si existe la base de la versión anterior (minisuper.db en la carpeta del programa) y aún
    no hay negocios, la COPIA como primer negocio. Los archivos originales no se tocan.
    Devuelve el negocio creado o None.
    """
    if not os.path.exists(database.LEGACY_DB_PATH) or listar_negocios(incluir_eliminados=True):
        return None
    nombre = 'Mi negocio'
    try:
        c = sqlite3.connect(database.LEGACY_DB_PATH)
        fila = c.execute("SELECT nombre_negocio FROM configuracion WHERE id = 1").fetchone()
        c.close()
        if fila and fila[0]:
            nombre = fila[0]
    except sqlite3.Error:
        pass
    slug = slug_disponible(nombre)

    conn = get_master()
    cur = conn.execute("""
        INSERT INTO negocios (slug, nombre, estado, plan, max_usuarios, creado, vence)
        VALUES (?,?,?,?,?,?,NULL)
    """, (slug, nombre, 'activo', 'pro', 50, _ahora()))
    nid = cur.lastrowid
    conn.commit()
    conn.close()

    try:
        destino = database.negocio_dir(nid)
        os.makedirs(destino, exist_ok=True)
        for origen, nombre_arch in ((database.LEGACY_DB_PATH, 'minisuper.db'),
                                    (database.LEGACY_ARCHIVE_PATH, 'ventas_archivadas.db')):
            if os.path.exists(origen):
                src = sqlite3.connect(origen)
                dst = sqlite3.connect(os.path.join(destino, nombre_arch))
                try:
                    src.backup(dst)           # copia consistente aunque la base esté en uso
                finally:
                    dst.close()
                    src.close()
        database.asegurar_esquema(nid)
    except Exception:
        conn = get_master()
        conn.execute("DELETE FROM negocios WHERE id = ?", (nid,))
        conn.commit()
        conn.close()
        shutil.rmtree(database.negocio_dir(nid), ignore_errors=True)
        raise
    registrar_evento(nid, 'sistema', 'importado', f"Base anterior importada como '{slug}'")
    return obtener_negocio(nid)


# ============================================================
# Línea de comandos
# ============================================================
def _pedir(texto, secreto=False, defecto=None):
    import getpass
    sufijo = f" [{defecto}]" if defecto else ""
    valor = (getpass.getpass if secreto else input)(f"{texto}{sufijo}: ").strip()
    return valor or defecto or ''


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Administración de la plataforma VendiYA")
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('superadmin', help='crear o cambiar la contraseña de un superadministrador')
    s.add_argument('--usuario')
    s.add_argument('--password')
    c = sub.add_parser('crear-negocio', help='crear un negocio cliente')
    c.add_argument('--nombre')
    c.add_argument('--codigo')
    c.add_argument('--dueno')
    c.add_argument('--usuario')
    c.add_argument('--password')
    c.add_argument('--dias', type=int, default=14)
    sub.add_parser('listar', help='listar negocios')
    args = p.parse_args(argv)

    init_master()
    try:
        if args.cmd == 'superadmin':
            usuario = args.usuario or _pedir("Usuario del superadministrador", defecto='superadmin')
            clave = args.password or _pedir("Contraseña (mínimo 8 caracteres)", secreto=True)
            crear_superadmin(usuario, clave)
            print(f"Listo. Entra en /plataforma/login con el usuario '{usuario}'.")
        elif args.cmd == 'crear-negocio':
            nombre = args.nombre or _pedir("Nombre del negocio")
            n = crear_negocio(
                nombre,
                args.dueno or _pedir("Nombre del dueño", defecto=nombre),
                args.usuario or _pedir("Usuario del dueño", defecto='admin'),
                args.password or _pedir("Contraseña del dueño", secreto=True),
                slug=args.codigo, dias=args.dias)
            print(f"Negocio creado. Código para iniciar sesión: {n['slug']}")
        else:
            for n in listar_negocios(incluir_eliminados=True):
                print(f"{n['id']:>3}  {n['slug']:<25} {n['estado']:<10} plan {n['plan']:<7} vence {n['vence'] or '-'}")
    except ErrorPlataforma as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
