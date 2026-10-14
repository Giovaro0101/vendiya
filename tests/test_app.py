"""
Pruebas de VendiYA. Usan una base de datos temporal: no tocan tus datos.

Ejecutar desde la carpeta del proyecto:
    venv\\Scripts\\python -m unittest discover -s tests -v
"""
import os
import sys
import shutil
import sqlite3
import tempfile
import threading
import unittest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import plataforma
import app as vendiya


def preparar_datos_temporales(tmp):
    """Apunta TODO el almacenamiento a una carpeta temporal (nunca a tus datos reales)."""
    database.DATA_DIR = os.path.join(tmp, 'data')
    database.MASTER_PATH = os.path.join(database.DATA_DIR, 'plataforma.db')
    database.MASTER_BACKUP_DIR = os.path.join(database.DATA_DIR, 'respaldos_plataforma')
    database.LEGACY_DB_PATH = os.path.join(tmp, 'legado', 'minisuper.db')
    database.LEGACY_ARCHIVE_PATH = os.path.join(tmp, 'legado', 'ventas_archivadas.db')
    database._archivos_listos.clear()
    database._esquemas_listos.clear()
    vendiya._cache_negocio.clear()
    vendiya._intentos_login.clear()
    vendiya._registros_por_ip.clear()
    plataforma.init_master()


class Base(unittest.TestCase):
    CODIGO = 'uno'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        preparar_datos_temporales(self.tmp)
        self.negocio = plataforma.crear_negocio('Bodega Uno', 'Dueño Uno', 'admin', 'admin123',
                                                slug=self.CODIGO, max_usuarios=10)
        self.nid = self.negocio['id']
        vendiya.app.config['TESTING'] = True
        self.c = vendiya.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def token(self, client=None):
        with (client or self.c).session_transaction() as s:
            return s.get('_csrf', '')

    def login(self, usuario='admin', clave='admin123', client=None, negocio=None):
        client = client or self.c
        client.get('/login')
        return client.post('/login', data={'negocio': self.CODIGO if negocio is None else negocio, 'usuario': usuario,
                                           'password': clave, 'csrf_token': self.token(client)})

    def post(self, url, data=None, client=None, **kw):
        client = client or self.c
        d = dict(data or {})
        d['csrf_token'] = self.token(client)
        return client.post(url, data=d, **kw)

    def api(self, url, payload, client=None):
        client = client or self.c
        return client.post(url, json=payload, headers={'X-CSRF-Token': self.token(client)})

    def db(self):
        return database.get_db(self.nid)

    def crear_producto(self, nombre='Harina', codigo='111', pc=1.0, pv=2.0, stock=10, iva=0, minimo=2):
        self.post('/productos/guardar', {
            'nombre': nombre, 'codigo_barras': codigo, 'precio_compra': pc, 'precio_venta': pv,
            'stock_inicial': stock, 'stock_minimo': minimo, 'iva': iva, 'unidad_medida': 'unidad'})
        conn = self.db()
        pid = conn.execute("SELECT id FROM productos WHERE nombre = ?", (nombre,)).fetchone()['id']
        conn.close()
        return pid

    def stock(self, pid):
        conn = self.db()
        s = conn.execute("SELECT stock_actual FROM productos WHERE id = ?", (pid,)).fetchone()[0]
        conn.close()
        return s

    def vender(self, pid, cant=1, monto=100, uuid=None, **extra):
        payload = {'items': [{'producto_id': pid, 'cantidad': cant}],
                   'pagos': [{'metodo': 'efectivo', 'monto': monto}]}
        if uuid:
            payload['uuid'] = uuid
        payload.update(extra)
        return self.api('/api/venta', payload)


class TestSeguridad(Base):
    def test_post_sin_csrf_es_rechazado(self):
        self.login()
        r = self.c.post('/api/venta', json={'items': [], 'pagos': []})
        self.assertEqual(r.status_code, 400)
        r = self.c.post('/productos/guardar', data={'nombre': 'X', 'precio_venta': 1})
        self.assertEqual(r.status_code, 302)           # vuelve al login sin ejecutar nada
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0], 0)

    def test_login_se_bloquea_tras_5_fallos(self):
        for _ in range(5):
            self.login(clave='mala')
        r = self.login(clave='admin123')               # ni con la clave correcta
        self.assertIn(b'Demasiados intentos', r.data)
        with self.c.session_transaction() as s:
            self.assertNotIn('usuario_id', s)

    def test_rutas_exigen_sesion(self):
        self.assertEqual(self.c.get('/pos').status_code, 302)
        r = self.c.get('/api/productos/buscar')
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()['ok'])

    def test_logout_solo_post(self):
        self.login()
        self.assertEqual(self.c.get('/logout').status_code, 405)

    def test_cajero_no_accede_a_administracion(self):
        self.login()
        self.post('/usuarios/guardar', {'nombre': 'Ana', 'usuario': 'ana', 'password': 'secreto1', 'rol': 'cajero'})
        cajero = vendiya.app.test_client()
        self.login('ana', 'secreto1', client=cajero)
        for ruta in ('/productos', '/inventario', '/reportes', '/gastos', '/usuarios', '/configuracion'):
            r = cajero.get(ruta)
            self.assertEqual(r.status_code, 302, ruta)
        self.assertEqual(cajero.get('/pos').status_code, 200)
        self.assertEqual(cajero.get('/clientes').status_code, 200)
        r = self.api('/api/asistente', {'pregunta': 'hola'}, client=cajero)
        self.assertEqual(r.status_code, 403)
        r = self.post('/productos/guardar', {'nombre': 'Hack', 'precio_venta': 1}, client=cajero)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM productos WHERE nombre='Hack'").fetchone()[0], 0)

    def test_usuario_desactivado_pierde_acceso_inmediato(self):
        self.login()
        self.post('/usuarios/guardar', {'nombre': 'Ana', 'usuario': 'ana', 'password': 'secreto1', 'rol': 'cajero'})
        cajero = vendiya.app.test_client()
        self.login('ana', 'secreto1', client=cajero)
        self.assertEqual(cajero.get('/pos').status_code, 200)
        conn = self.db()
        uid = conn.execute("SELECT id FROM usuarios WHERE usuario='ana'").fetchone()['id']
        conn.close()
        self.post(f'/usuarios/{uid}/estado')
        self.assertEqual(cajero.get('/pos').status_code, 302)

    def test_no_puede_desactivarse_a_si_mismo(self):
        self.login()
        r = self.post('/usuarios/1/estado', follow_redirects=True)
        self.assertIn('No puedes desactivar tu propia cuenta'.encode(), r.data)

    def test_cambio_de_clave(self):
        self.login()
        r = self.post('/cuenta', {'actual': 'incorrecta', 'nueva': 'nueva123', 'confirmar': 'nueva123'},
                      follow_redirects=True)
        self.assertIn(b'incorrecta', r.data)
        self.post('/cuenta', {'actual': 'admin123', 'nueva': 'nueva123', 'confirmar': 'nueva123'})
        otro = vendiya.app.test_client()
        self.assertNotIn(b'Usuario o contrase', self.login('admin', 'nueva123', client=otro).data)

    def test_descarga_de_respaldo_valida_el_nombre(self):
        self.login()
        self.post('/respaldos/crear')
        nombre = os.listdir(database.backup_dir(self.nid))[0]
        self.assertEqual(self.c.get(f'/respaldos/{nombre}').status_code, 200)
        self.assertEqual(self.c.get('/respaldos/..%2f..%2fapp.py').status_code, 404)
        self.assertEqual(self.c.get('/respaldos/minisuper.db').status_code, 404)


class TestVentas(Base):
    def setUp(self):
        super().setUp()
        self.login()
        self.pid = self.crear_producto(stock=10)

    def test_venta_basica_descuenta_stock_y_registra_movimiento(self):
        r = self.vender(self.pid, 3, monto=10)
        d = r.get_json()
        self.assertTrue(d['ok'])
        self.assertEqual(d['total'], 6.0)
        self.assertEqual(d['cambio'], 4.0)
        self.assertEqual(d['ganancia'], 3.0)
        self.assertEqual(self.stock(self.pid), 7)
        conn = self.db()
        v = conn.execute("SELECT * FROM ventas").fetchone()
        self.assertEqual(v['estado'], 'completada')
        self.assertEqual(conn.execute("SELECT nombre_producto FROM venta_detalle").fetchone()[0], 'Harina')
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM movimientos WHERE tipo='salida'").fetchone()[0], 1)

    def test_stock_insuficiente(self):
        r = self.vender(self.pid, 11)
        self.assertEqual(r.status_code, 400)
        self.assertIn('Stock insuficiente', r.get_json()['error'])
        self.assertEqual(self.stock(self.pid), 10)

    def test_venta_offline_se_registra_aunque_falte_stock(self):
        r = self.vender(self.pid, 12, monto=100, offline=True, fecha_local='2026-10-13 09:15:00')
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(self.stock(self.pid), -2)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT fecha FROM ventas").fetchone()[0], '2026-10-13 09:15:00')

    def test_misma_venta_dos_veces_se_registra_una_sola(self):
        r1 = self.vender(self.pid, 2, uuid='abc-123').get_json()
        r2 = self.vender(self.pid, 2, uuid='abc-123').get_json()
        self.assertTrue(r2['ok'])
        self.assertTrue(r2['duplicada'])
        self.assertEqual(r1['numero_factura'], r2['numero_factura'])
        self.assertEqual(self.stock(self.pid), 8)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ventas").fetchone()[0], 1)

    def test_envios_simultaneos_con_el_mismo_uuid(self):
        resultados = []

        def enviar():
            cli = vendiya.app.test_client()
            cli.get('/login')
            cli.post('/login', data={'negocio': self.CODIGO, 'usuario': 'admin', 'password': 'admin123',
                                     'csrf_token': self.token(cli)})
            r = self.vender(self.pid, 1, uuid='carrera-1', client=None) if False else self.api(
                '/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 1}],
                               'pagos': [{'metodo': 'efectivo', 'monto': 5}], 'uuid': 'carrera-1'}, client=cli)
            resultados.append(r.get_json())

        hilos = [threading.Thread(target=enviar) for _ in range(4)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertTrue(all(r['ok'] for r in resultados), resultados)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ventas").fetchone()[0], 1)
        self.assertEqual(self.stock(self.pid), 9)

    def test_datos_invalidos_son_rechazados_sin_tocar_nada(self):
        casos = [
            {'items': [{'producto_id': self.pid, 'cantidad': -1}], 'pagos': [{'metodo': 'efectivo', 'monto': 5}]},
            {'items': [{'producto_id': self.pid, 'cantidad': 0}], 'pagos': [{'metodo': 'efectivo', 'monto': 5}]},
            {'items': [{'producto_id': self.pid, 'cantidad': 'abc'}], 'pagos': [{'metodo': 'efectivo', 'monto': 5}]},
            {'items': [{'producto_id': self.pid, 'cantidad': 1}], 'pagos': [{'metodo': 'regalo', 'monto': 5}]},
            {'items': [{'producto_id': self.pid, 'cantidad': 1}], 'pagos': [{'metodo': 'efectivo', 'monto': -5}]},
            {'items': [{'producto_id': self.pid, 'cantidad': 1}], 'pagos': [{'metodo': 'efectivo', 'monto': 1}]},
            {'items': [{'producto_id': 9999, 'cantidad': 1}], 'pagos': [{'metodo': 'efectivo', 'monto': 5}]},
            {'items': 'hola', 'pagos': []},
            {'items': [1, 2], 'pagos': [3]},
            {'items': [{'producto_id': self.pid, 'cantidad': 1}], 'pagos': [{'metodo': 'efectivo', 'monto': 5}],
             'descuento': 99},
            {'items': [{'producto_id': self.pid, 'cantidad': 1e999}], 'pagos': [{'metodo': 'efectivo', 'monto': 5}]},
        ]
        for caso in casos:
            r = self.api('/api/venta', caso)
            self.assertEqual(r.status_code, 400, caso)
            self.assertFalse(r.get_json()['ok'])
        self.assertEqual(self.stock(self.pid), 10)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ventas").fetchone()[0], 0)

    def test_producto_repetido_en_el_carrito_suma_cantidades(self):
        r = self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 6},
                                              {'producto_id': self.pid, 'cantidad': 6}],
                                    'pagos': [{'metodo': 'efectivo', 'monto': 100}]})
        self.assertEqual(r.status_code, 400)           # 12 > 10: no se puede esquivar el control de stock

    def test_descuento_e_iva(self):
        pid = self.crear_producto('Aceite', '222', pc=2, pv=10, stock=5, iva=16)
        r = self.vender(pid, 2, monto=100, descuento=4).get_json()
        # subtotal 20, descuento 4 -> base 16, iva 16% = 2.56, total 18.56
        self.assertEqual(r['total'], 18.56)
        self.assertEqual(r['descuento'], 4.0)
        self.assertEqual(r['ganancia'], 12.0)           # (10-2)*2 - 4

    def test_pago_mixto(self):
        r = self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 5}],
                                    'pagos': [{'metodo': 'efectivo', 'monto': 4},
                                              {'metodo': 'pago_movil', 'monto': 7}]}).get_json()
        self.assertEqual(r['total'], 10.0)
        self.assertEqual(r['cambio'], 1.0)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT forma_pago FROM ventas").fetchone()[0], 'mixto')

    def test_buscar_con_caracteres_especiales(self):
        self.crear_producto('Jabón 100%', '333')
        self.assertEqual(len(self.c.get('/api/productos/buscar?q=100%25').get_json()), 1)
        self.assertEqual(len(self.c.get('/api/productos/buscar?q=%25').get_json()), 1)   # solo el que tiene %
        self.assertEqual(len(self.c.get('/api/productos/buscar?q=har').get_json()), 1)
        self.assertEqual(len(self.c.get('/api/productos/buscar?q=111').get_json()), 1)    # por código


class TestCreditoYClientes(Base):
    def setUp(self):
        super().setUp()
        self.login()
        self.pid = self.crear_producto(stock=20, pv=5, pc=3)
        r = self.api('/api/clientes', {'nombre': 'Doña Rosa', 'telefono': '0414'})
        self.cid = r.get_json()['id']

    def saldo(self):
        conn = self.db()
        return conn.execute("SELECT saldo FROM clientes WHERE id = ?", (self.cid,)).fetchone()[0]

    def test_fiado_parcial_y_abonos(self):
        r = self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 4}], 'fiado': True,
                                    'cliente_id': self.cid, 'pagos': [{'metodo': 'efectivo', 'monto': 5}]}).get_json()
        self.assertTrue(r['ok'])
        self.assertEqual(r['total'], 20.0)
        self.assertEqual(r['deuda'], 15.0)
        self.assertEqual(self.saldo(), 15.0)

        self.post(f'/clientes/{self.cid}/abono', {'monto': 10, 'metodo': 'efectivo'})
        self.assertEqual(self.saldo(), 5.0)
        r = self.post(f'/clientes/{self.cid}/abono', {'monto': 50, 'metodo': 'efectivo'}, follow_redirects=True)
        self.assertIn('supera la deuda'.encode(), r.data)
        self.assertEqual(self.saldo(), 5.0)

    def test_fiado_sin_cliente_es_rechazado(self):
        r = self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 1}], 'fiado': True, 'pagos': []})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stock(self.pid), 20)

    def test_sin_fiado_no_se_puede_pagar_de_menos(self):
        r = self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 4}],
                                    'cliente_id': self.cid, 'pagos': [{'metodo': 'efectivo', 'monto': 5}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.saldo(), 0)

    def test_anular_venta_a_credito_revierte_stock_y_deuda(self):
        r = self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 4}], 'fiado': True,
                                    'cliente_id': self.cid, 'pagos': []}).get_json()
        self.assertEqual(self.saldo(), 20.0)
        self.assertEqual(self.stock(self.pid), 16)
        self.post(f"/ventas/{r['venta_id']}/anular", {'motivo': 'error'})
        self.assertEqual(self.saldo(), 0)
        self.assertEqual(self.stock(self.pid), 20)
        # no se puede anular dos veces
        r2 = self.post(f"/ventas/{r['venta_id']}/anular", {'motivo': 'otra vez'}, follow_redirects=True)
        self.assertIn(b'ya est', r2.data)
        self.assertEqual(self.stock(self.pid), 20)

    def test_anulacion_exige_motivo(self):
        r = self.vender(self.pid, 1, monto=10).get_json()
        resp = self.post(f"/ventas/{r['venta_id']}/anular", {'motivo': ''}, follow_redirects=True)
        self.assertIn(b'motivo', resp.data)
        self.assertEqual(self.stock(self.pid), 19)


class TestCaja(Base):
    def setUp(self):
        super().setUp()
        self.login()
        self.pid = self.crear_producto(stock=50, pv=5, pc=3)

    def test_cuadre_de_caja(self):
        self.post('/caja/abrir', {'fondo_inicial': 20})
        self.vender(self.pid, 2, monto=20)                                        # total 10, cambio 10, efectivo
        self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 2}],
                                'pagos': [{'metodo': 'tarjeta', 'monto': 10}]})   # no es efectivo
        self.post('/gastos/guardar', {'concepto': 'Bolsas', 'monto': 3, 'categoria': 'Otros', 'pagado_de_caja': '1'})
        self.post('/caja/cerrar', {'efectivo_contado': 17, 'notas': 'ok'})
        conn = self.db()
        c = conn.execute("SELECT * FROM cajas").fetchone()
        # 20 fondo + 20 cobrado en efectivo - 10 cambio - 3 gasto = 27
        self.assertEqual(c['efectivo_esperado'], 27.0)
        self.assertEqual(c['diferencia'], -10.0)
        self.assertEqual(c['total_ventas'], 20.0)
        self.assertEqual(c['estado'], 'cerrada')

    def test_el_cambio_de_un_pago_con_tarjeta_no_sale_de_la_gaveta(self):
        self.post('/caja/abrir', {'fondo_inicial': 10})
        self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 1}],
                                'pagos': [{'metodo': 'tarjeta', 'monto': 50}]})      # total 5, "cambio" 45 sin efectivo
        conn = self.db()
        r = vendiya.resumen_caja(conn, vendiya.caja_abierta(conn))
        self.assertEqual(r['esperado'], 10.0)

    def test_no_se_abren_dos_cajas(self):
        self.post('/caja/abrir', {'fondo_inicial': 5})
        r = self.post('/caja/abrir', {'fondo_inicial': 5}, follow_redirects=True)
        self.assertIn(b'Ya hay una caja abierta', r.data)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM cajas").fetchone()[0], 1)

    def test_abonos_entran_a_la_caja(self):
        cid = self.api('/api/clientes', {'nombre': 'Pedro'}).get_json()['id']
        self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 2}], 'fiado': True,
                                'cliente_id': cid, 'pagos': []})
        self.post('/caja/abrir', {'fondo_inicial': 0})
        self.post(f'/clientes/{cid}/abono', {'monto': 4, 'metodo': 'efectivo'})
        conn = self.db()
        caja = conn.execute("SELECT * FROM cajas").fetchone()
        self.assertEqual(vendiya.resumen_caja(conn, caja)['esperado'], 4.0)


class TestProductosEInventario(Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_codigo_duplicado_da_mensaje_claro(self):
        self.crear_producto('A', '999')
        r = self.post('/productos/guardar', {'nombre': 'B', 'codigo_barras': '999', 'precio_venta': 1},
                      follow_redirects=True)
        self.assertIn('código de barras'.encode(), r.data)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0], 1)

    def test_varios_productos_sin_codigo(self):
        self.crear_producto('Sin 1', '')
        self.crear_producto('Sin 2', '')
        self.crear_producto('Sin 3', '   ')
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0], 3)

    def test_precios_invalidos(self):
        for pv in ('abc', '-3', ''):
            self.post('/productos/guardar', {'nombre': 'Malo', 'precio_venta': pv})
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0], 0)

    def test_desactivar_y_reactivar(self):
        pid = self.crear_producto()
        self.post(f'/productos/{pid}/desactivar')
        self.assertEqual(self.c.get('/api/productos/buscar?q=Harina').get_json(), [])
        self.post(f'/productos/{pid}/reactivar')
        self.assertEqual(len(self.c.get('/api/productos/buscar?q=Harina').get_json()), 1)

    def test_entrada_con_costo_actualiza_precio_de_compra(self):
        pid = self.crear_producto(pc=1.0, stock=5)
        self.post('/inventario/ajuste', {'producto_id': pid, 'tipo': 'entrada', 'cantidad': 10, 'costo': 1.5})
        self.assertEqual(self.stock(pid), 15)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT precio_compra FROM productos").fetchone()[0], 1.5)

    def test_salida_no_deja_stock_negativo(self):
        pid = self.crear_producto(stock=5)
        r = self.post('/inventario/ajuste', {'producto_id': pid, 'tipo': 'salida', 'cantidad': 6}, follow_redirects=True)
        self.assertIn(b'negativo', r.data)
        self.assertEqual(self.stock(pid), 5)

    def test_tipo_de_movimiento_invalido(self):
        pid = self.crear_producto(stock=5)
        self.post('/inventario/ajuste', {'producto_id': pid, 'tipo': 'robo', 'cantidad': 1})
        self.assertEqual(self.stock(pid), 5)

    def test_categoria_con_productos_no_se_elimina(self):
        conn = self.db()
        cat = conn.execute("SELECT id FROM categorias LIMIT 1").fetchone()['id']
        self.post('/productos/guardar', {'nombre': 'Con cat', 'precio_venta': 1, 'categoria_id': cat})
        r = self.post(f'/categorias/{cat}/eliminar', follow_redirects=True)
        self.assertIn('tiene productos'.encode(), r.data)


class TestReportesYArchivo(Base):
    def setUp(self):
        super().setUp()
        self.login()
        self.pid = self.crear_producto(stock=100, pv=5, pc=3)

    def test_todas_las_paginas_cargan(self):
        self.vender(self.pid, 2, monto=20)
        conn = self.db()
        vid = conn.execute("SELECT id FROM ventas").fetchone()['id']
        cid = self.api('/api/clientes', {'nombre': 'Cli'}).get_json()['id']
        self.post('/caja/abrir', {'fondo_inicial': 1})
        for ruta in ['/pos', '/productos', '/inventario', '/ventas', '/ventas?rango=todo', '/reportes',
                     '/reportes?rango=hoy', '/gastos', '/usuarios', '/configuracion', '/cuenta', '/clientes',
                     f'/clientes/{cid}', '/caja', '/caja/1', f'/ventas/{vid}', f'/ventas/{vid}/ticket',
                     '/productos/exportar', '/reportes/exportar', '/asistente', '/api/csrf', '/api/clientes']:
            r = self.c.get(ruta)
            self.assertEqual(r.status_code, 200, ruta)

    def test_fechas_invalidas_no_rompen(self):
        r = self.c.get('/reportes?desde=hola&hasta=mundo')
        self.assertEqual(r.status_code, 302)

    def test_reporte_suma_ventas_ignora_anuladas_y_resta_gastos(self):
        v1 = self.vender(self.pid, 2, monto=20).get_json()      # 10 total, ganancia 4
        v2 = self.vender(self.pid, 4, monto=20).get_json()      # 20 total, ganancia 8
        self.post(f"/ventas/{v2['venta_id']}/anular", {'motivo': 'x'})
        self.post('/gastos/guardar', {'concepto': 'Luz', 'monto': 1.5, 'categoria': 'Servicios'})
        conn = database.get_db_unificado(self.nid)
        # misma consulta que usa el reporte
        r = vendiya.app.test_client()
        html = self.c.get('/reportes?rango=hoy').get_data(as_text=True)
        self.assertIn('$10.00', html)         # facturado
        self.assertIn('$4.00', html)          # ganancia bruta
        self.assertIn('$2.50', html)          # ganancia neta 4 - 1.5
        conn.close()

    def test_ventas_archivadas_siguen_en_reportes_y_se_pueden_abrir(self):
        self.vender(self.pid, 2, monto=20)
        conn = self.db()
        conn.execute("UPDATE ventas SET fecha = '2020-01-05 10:00:00'")
        conn.commit()
        conn.close()
        with database.negocio(self.nid):
            self.assertEqual(database.archivar_ventas_antiguas(15), 1)

        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ventas").fetchone()[0], 0)
        conn.close()

        html = self.c.get('/ventas?rango=todo').get_data(as_text=True)
        self.assertIn('archivada', html)
        self.assertIn('arch=1', html)
        html = self.c.get('/reportes?rango=todo').get_data(as_text=True)
        self.assertIn('Harina', html)         # el nombre del producto sobrevive al archivado
        self.assertIn('$10.00', html)
        r = self.c.get('/ventas/1?arch=1')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Harina', r.data)
        # los ids de la BD principal no se reutilizan, y ?arch=1 evita cualquier confusión
        nueva = self.vender(self.pid, 1, monto=5).get_json()
        self.assertEqual(nueva['venta_id'], 2)
        self.assertIn('$5.00', self.c.get('/ventas/2').get_data(as_text=True))
        self.assertIn('$10.00', self.c.get('/ventas/1?arch=1').get_data(as_text=True))
        self.assertEqual(self.c.get('/ventas/1').status_code, 302)     # la archivada no está en la principal

    def test_exportar_csv(self):
        self.vender(self.pid, 1, monto=5)
        r = self.c.get('/reportes/exportar?rango=hoy')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r.headers['Content-Type'])
        self.assertIn(b'Factura', r.data)
        self.assertIn(b'completada', r.data)

    def test_cajero_solo_ve_ventas_de_hoy_sin_ganancia(self):
        self.vender(self.pid, 1, monto=5)
        self.post('/usuarios/guardar', {'nombre': 'Ana', 'usuario': 'ana', 'password': 'secreto1', 'rol': 'cajero'})
        cajero = vendiya.app.test_client()
        self.login('ana', 'secreto1', client=cajero)
        r = cajero.get('/ventas?rango=todo&desde=2000-01-01&hasta=2099-01-01')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('Ganancia', r.get_data(as_text=True))
        # la API tampoco le entrega la ganancia
        d = self.api('/api/venta', {'items': [{'producto_id': self.pid, 'cantidad': 1}],
                                    'pagos': [{'metodo': 'efectivo', 'monto': 5}]}, client=cajero).get_json()
        self.assertNotIn('ganancia', d)


ESQUEMA_ANTIGUO = """
    CREATE TABLE productos (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo_barras TEXT UNIQUE,
        nombre TEXT NOT NULL, categoria_id INTEGER, precio_compra REAL DEFAULT 0,
        precio_venta REAL NOT NULL, stock_actual REAL DEFAULT 0, stock_minimo REAL DEFAULT 0,
        unidad_medida TEXT DEFAULT 'unidad', iva REAL DEFAULT 0, activo INTEGER DEFAULT 1);
    CREATE TABLE ventas (id INTEGER PRIMARY KEY AUTOINCREMENT, numero_factura TEXT NOT NULL,
        fecha TEXT NOT NULL, usuario_id INTEGER, subtotal REAL NOT NULL, iva REAL NOT NULL,
        total REAL NOT NULL, forma_pago TEXT NOT NULL, monto_recibido REAL, cambio REAL,
        ganancia REAL DEFAULT 0);
    CREATE TABLE venta_detalle (id INTEGER PRIMARY KEY AUTOINCREMENT, venta_id INTEGER NOT NULL,
        producto_id INTEGER NOT NULL, cantidad REAL NOT NULL, precio_unitario REAL NOT NULL,
        subtotal REAL NOT NULL, precio_compra REAL DEFAULT 0, ganancia REAL DEFAULT 0);
    CREATE TABLE usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL,
        usuario TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, rol TEXT DEFAULT 'cajero');
    CREATE TABLE configuracion (id INTEGER PRIMARY KEY CHECK (id = 1), nombre_negocio TEXT, rif TEXT,
        direccion TEXT, telefono TEXT, tasa_iva REAL DEFAULT 16, mensaje_ticket TEXT);
    INSERT INTO productos (codigo_barras, nombre, precio_venta, stock_actual) VALUES ('', 'A', 2, 5), (' ', 'B', 3, 5);
    INSERT INTO ventas (numero_factura, fecha, subtotal, iva, total, forma_pago, ganancia)
        VALUES ('F1', '2026-01-01 10:00:00', 2, 0, 2, 'efectivo', 1);
    INSERT INTO venta_detalle (venta_id, producto_id, cantidad, precio_unitario, subtotal)
        VALUES (1, 1, 1, 2, 2);
    INSERT INTO configuracion (id, nombre_negocio) VALUES (1, 'Bodega Vieja');
"""


def crear_base_antigua(ruta, hash_admin=None):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    con = sqlite3.connect(ruta)
    con.executescript(ESQUEMA_ANTIGUO)
    if hash_admin:
        con.execute("INSERT INTO usuarios (nombre, usuario, password_hash, rol) VALUES ('Dueño', 'admin', ?, 'admin')",
                    (hash_admin,))
    con.commit()
    con.close()


class TestMigracion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        preparar_datos_temporales(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_base_antigua_se_migra_sin_perder_datos_y_es_repetible(self):
        # Un negocio registrado cuya base quedó en el esquema de la primera versión
        conn = plataforma.get_master()
        conn.execute("INSERT INTO negocios (slug, nombre, creado) VALUES ('viejo', 'Viejo', '2026-01-01')")
        conn.commit()
        conn.close()
        crear_base_antigua(os.path.join(database.DATA_DIR, 'negocios', '1', 'minisuper.db'))

        plataforma.migrar_todos()
        database._esquemas_listos.clear()
        plataforma.migrar_todos()                       # repetir no debe fallar ni duplicar

        conn = database.get_db(1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ventas").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT estado FROM ventas").fetchone()[0], 'completada')
        self.assertEqual(conn.execute("SELECT nombre_producto FROM venta_detalle").fetchone()[0], 'A')
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM productos WHERE codigo_barras IS NULL").fetchone()[0], 2)
        conn.close()
        self.assertTrue(any('premigracion' in f for f in os.listdir(database.backup_dir(1))))

    def test_importar_la_base_de_la_version_anterior(self):
        crear_base_antigua(database.LEGACY_DB_PATH, generate_password_hash('claveVieja1'))
        antes = os.path.getsize(database.LEGACY_DB_PATH)

        n = plataforma.importar_legado()
        self.assertEqual(n['nombre'], 'Bodega Vieja')
        self.assertEqual(n['slug'], 'bodega-vieja')
        self.assertIsNone(plataforma.importar_legado())          # ya hay negocios: no vuelve a importar
        self.assertEqual(os.path.getsize(database.LEGACY_DB_PATH), antes)   # el original no se tocó

        cli = vendiya.app.test_client()
        cli.get('/login')
        with cli.session_transaction() as s:
            tk = s['_csrf']
        cli.post('/login', data={'negocio': 'bodega-vieja', 'usuario': 'admin',
                                 'password': 'claveVieja1', 'csrf_token': tk})
        r = cli.get('/api/productos/buscar?q=A')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()), 1)                    # sus productos siguen ahí
        conn = database.get_db(n['id'])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ventas").fetchone()[0], 1)

    def test_sin_negocio_activo_no_hay_base_de_datos(self):
        with self.assertRaises(RuntimeError):
            database.get_db()
        with self.assertRaises(RuntimeError):
            database.get_db_unificado()
        with self.assertRaises(RuntimeError):
            database.set_negocio(12345)                           # un negocio que no existe no se inventa


if __name__ == '__main__':
    unittest.main()
