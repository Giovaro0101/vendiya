"""
Pruebas de la plataforma multi-negocio: aislamiento de datos, superadministrador,
suspensión y vencimiento, límites de plan, registro público y respaldos por negocio.
"""
import os
import sqlite3
import unittest

import database
import plataforma
import app as vendiya
from test_app import Base


class MultiBase(Base):
    """Dos negocios ('uno' y 'dos') con un dueño llamado 'admin' en cada uno."""

    def setUp(self):
        super().setUp()
        self.negocio2 = plataforma.crear_negocio('Abasto Dos', 'Dueña Dos', 'admin', 'otraClave9',
                                                 slug='dos', max_usuarios=10)
        self.nid2 = self.negocio2['id']
        self.c2 = vendiya.app.test_client()

    def entrar_dos(self):
        return self.login('admin', 'otraClave9', client=self.c2, negocio='dos')

    def soy_superadmin(self, client=None):
        plataforma.crear_superadmin('jefe', 'superClave99')
        client = client or vendiya.app.test_client()
        client.get('/plataforma/login')
        client.post('/plataforma/login', data={'usuario': 'jefe', 'password': 'superClave99',
                                               'csrf_token': self.token(client)})
        return client


class TestAislamiento(MultiBase):
    def test_los_datos_de_un_negocio_no_aparecen_en_otro(self):
        self.login()
        self.entrar_dos()
        pid = self.crear_producto('Harina Uno', '111', stock=10)
        self.vender(pid, 2, monto=20)
        self.post('/gastos/guardar', {'concepto': 'Luz de Uno', 'monto': 9, 'categoria': 'Servicios'})
        cid = self.api('/api/clientes', {'nombre': 'Cliente de Uno'}).get_json()['id']

        # El negocio Dos no ve nada de Uno
        self.assertEqual(self.c2.get('/api/productos/buscar?q=').get_json(), [])
        self.assertEqual(self.c2.get('/api/clientes').get_json(), [])
        self.assertNotIn('Cliente de Uno', self.c2.get('/clientes').get_data(as_text=True))
        self.assertNotIn('Luz de Uno', self.c2.get('/gastos').get_data(as_text=True))
        self.assertNotIn('Harina Uno', self.c2.get('/reportes?rango=todo').get_data(as_text=True))
        self.assertEqual(self.c2.get('/ventas/1').status_code, 302)            # esa venta no existe para Dos
        self.assertEqual(self.c2.get(f'/clientes/{cid}').status_code, 404)
        self.assertEqual(self.c2.get('/caja/1').status_code, 404)

        # ...y Uno sí conserva lo suyo
        self.assertEqual(len(self.c.get('/api/productos/buscar?q=').get_json()), 1)

        # Cada negocio tiene sus propios archivos
        self.assertTrue(os.path.exists(database.db_path(self.nid)))
        self.assertTrue(os.path.exists(database.db_path(self.nid2)))
        self.assertNotEqual(database.db_path(self.nid), database.db_path(self.nid2))

    def test_no_se_puede_comprar_con_un_producto_de_otro_negocio(self):
        self.login()
        self.entrar_dos()
        pid_uno = self.crear_producto('Solo de Uno', '111', stock=10)
        # Dos intenta vender el id del producto de Uno
        r = self.api('/api/venta', {'items': [{'producto_id': pid_uno, 'cantidad': 1}],
                                    'pagos': [{'metodo': 'efectivo', 'monto': 50}]}, client=self.c2)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.stock(pid_uno), 10)

    def test_mismo_usuario_en_dos_negocios_son_cuentas_distintas(self):
        r = self.login('admin', 'otraClave9')                  # clave de Dos en el negocio Uno
        with self.c.session_transaction() as s:
            self.assertNotIn('usuario_id', s)
        self.login()                                           # la suya
        self.entrar_dos()
        for cli, esperado in ((self.c, self.nid), (self.c2, self.nid2)):
            with cli.session_transaction() as s:
                self.assertEqual(s['negocio_id'], esperado)

    def test_login_exige_el_codigo_correcto(self):
        for codigo in ('dos', 'no-existe', '', 'UNO ' ):
            vendiya._intentos_login.clear()
            r = self.login('admin', 'admin123', negocio=codigo)     # clave de Uno con código de otro
            with self.c.session_transaction() as s:
                if codigo.strip().lower() == 'uno':
                    self.assertIn('usuario_id', s)                  # mayúsculas/espacios se toleran
                else:
                    self.assertNotIn('usuario_id', s, codigo)
            self.c = vendiya.app.test_client()

    def test_el_error_de_login_no_revela_si_el_negocio_existe(self):
        a = self.login('admin', 'mala', negocio='uno').get_data(as_text=True)
        b = self.login('admin', 'mala', negocio='no-existe').get_data(as_text=True)
        self.assertIn('Negocio, usuario o contraseña incorrectos', a)
        self.assertIn('Negocio, usuario o contraseña incorrectos', b)

    def test_venta_en_cola_de_otro_negocio_es_rechazada(self):
        self.login()
        pid = self.crear_producto(stock=10)
        r = self.api('/api/venta', {'negocio_id': self.nid2, 'offline': True,
                                    'items': [{'producto_id': pid, 'cantidad': 1}],
                                    'pagos': [{'metodo': 'efectivo', 'monto': 50}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn('otro negocio', r.get_json()['error'])
        self.assertEqual(self.stock(pid), 10)
        # con el negocio correcto sí
        r = self.api('/api/venta', {'negocio_id': self.nid, 'items': [{'producto_id': pid, 'cantidad': 1}],
                                    'pagos': [{'metodo': 'efectivo', 'monto': 50}]})
        self.assertTrue(r.get_json()['ok'])

    def test_respaldos_por_negocio(self):
        self.login()
        self.entrar_dos()
        self.post('/respaldos/crear')
        archivos_uno = os.listdir(database.backup_dir(self.nid))
        self.assertTrue(archivos_uno)
        self.assertFalse(os.path.isdir(database.backup_dir(self.nid2))
                         and any('manual' in f for f in os.listdir(database.backup_dir(self.nid2))))
        nombre = next(f for f in archivos_uno if f.startswith('minisuper-manual'))
        self.assertEqual(self.c.get(f'/respaldos/{nombre}').status_code, 200)
        self.assertEqual(self.c2.get(f'/respaldos/{nombre}').status_code, 404)   # Dos no puede bajar el de Uno

    def test_tasa_global_se_reparte_sin_pisar_una_manual(self):
        self.login()
        self.entrar_dos()
        self.post('/configuracion/tasa', {'valor': '500'})              # Uno fija su tasa a mano
        plataforma.guardar_tasa_global(900)
        plataforma.distribuir_tasa(900)
        self.assertEqual(vendiya.obtener_tasa_info.__name__, 'obtener_tasa_info')
        for nid, esperado, fuente in ((self.nid, 500, 'Manual'), (self.nid2, 900, 'BCV')):
            c = database.get_db(nid)
            fila = c.execute("SELECT valor, fuente FROM tasa_cambio ORDER BY fecha DESC, id DESC LIMIT 1").fetchone()
            self.assertEqual((fila['valor'], fila['fuente']), (esperado, fuente))
        # un negocio nuevo nace con la última tasa global
        n3 = plataforma.crear_negocio('Tres', 'T', 'admin', 'clave123', slug='tres')
        c = database.get_db(n3['id'])
        self.assertEqual(c.execute("SELECT valor FROM tasa_cambio").fetchone()[0], 900)


class TestEstadoDeCuenta(MultiBase):
    def test_suspender_cierra_la_sesion_al_instante_y_reactivar_la_devuelve(self):
        self.login()
        self.assertEqual(self.c.get('/pos').status_code, 200)
        plataforma.cambiar_estado(self.nid, 'suspendido')
        vendiya._cache_negocio.clear()
        r = self.c.get('/pos', follow_redirects=True)
        self.assertIn('suspendida', r.get_data(as_text=True))
        self.assertEqual(self.c.get('/api/productos/buscar').status_code, 401)
        # con las credenciales correctas, el login informa el motivo
        r = self.login()
        self.assertIn('suspendida', r.get_data(as_text=True))
        # el otro negocio no se ve afectado
        self.entrar_dos()
        self.assertEqual(self.c2.get('/pos').status_code, 200)

        plataforma.cambiar_estado(self.nid, 'activo')
        vendiya._cache_negocio.clear()
        self.login()
        self.assertEqual(self.c.get('/pos').status_code, 200)

    def test_suscripcion_vencida_bloquea_y_extender_desbloquea(self):
        plataforma.extender(self.nid, fecha='2020-01-01')
        vendiya._cache_negocio.clear()
        r = self.login().get_data(as_text=True)
        self.assertIn('venció el 2020-01-01', r)
        # con credenciales equivocadas NO se revela el estado de la cuenta
        r = self.login('admin', 'mala').get_data(as_text=True)
        self.assertNotIn('venció', r)
        self.assertIn('incorrectos', r)

        plataforma.extender(self.nid, dias=30)
        vendiya._cache_negocio.clear()
        self.login()
        self.assertEqual(self.c.get('/pos').status_code, 200)

    def test_sesion_abierta_se_corta_cuando_vence(self):
        self.login()
        plataforma.extender(self.nid, fecha='2020-01-01')
        vendiya._cache_negocio.clear()
        self.assertEqual(self.c.get('/pos').status_code, 302)

    def test_extender_suma_desde_el_vencimiento_si_aun_no_vencio(self):
        plataforma.extender(self.nid, fecha='2099-01-01')
        self.assertEqual(plataforma.extender(self.nid, dias=10), '2099-01-11')
        plataforma.extender(self.nid, fecha='2020-01-01')
        nuevo = plataforma.extender(self.nid, dias=5)               # ya vencida: cuenta desde hoy
        self.assertGreater(nuevo, '2025-01-01')

    def test_aviso_de_proximo_vencimiento_solo_para_el_admin(self):
        from datetime import date, timedelta
        plataforma.extender(self.nid, fecha=(date.today() + timedelta(days=3)).isoformat())
        vendiya._cache_negocio.clear()
        self.login()
        html = self.c.get('/pos').get_data(as_text=True)
        self.assertIn('Tu suscripción vence en 3 días', html)

    def test_negocio_eliminado_no_puede_entrar_ni_aparece(self):
        plataforma.eliminar_negocio(self.nid)
        vendiya._cache_negocio.clear()
        r = self.login().get_data(as_text=True)
        self.assertIn('incorrectos', r)
        self.assertNotIn('uno', [n['slug'] for n in plataforma.listar_negocios()])
        self.assertTrue(os.path.exists(database.db_path(self.nid)))        # los datos siguen en disco

    def test_limite_de_usuarios_del_plan(self):
        conn = plataforma.get_master()
        conn.execute("UPDATE negocios SET max_usuarios = 2 WHERE id = ?", (self.nid,))
        conn.commit()
        conn.close()
        self.login()
        self.post('/usuarios/guardar', {'nombre': 'Ana', 'usuario': 'ana', 'password': 'secreto1', 'rol': 'cajero'})
        r = self.post('/usuarios/guardar', {'nombre': 'Luis', 'usuario': 'luis', 'password': 'secreto1',
                                            'rol': 'cajero'}, follow_redirects=True)
        self.assertIn('permite hasta 2 usuarios', r.get_data(as_text=True))
        c = self.db()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 2)
        # desactivar a uno libera un cupo
        ana = c.execute("SELECT id FROM usuarios WHERE usuario='ana'").fetchone()['id']
        self.post(f'/usuarios/{ana}/estado')
        self.post('/usuarios/guardar', {'nombre': 'Luis', 'usuario': 'luis', 'password': 'secreto1', 'rol': 'cajero'})
        self.assertEqual(c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 3)
        # reactivar a Ana supera el cupo
        r = self.post(f'/usuarios/{ana}/estado', follow_redirects=True)
        self.assertIn('permite hasta 2 usuarios', r.get_data(as_text=True))


class TestSuperadmin(MultiBase):
    def test_el_panel_exige_superadmin_y_el_admin_de_un_negocio_no_sirve(self):
        self.assertEqual(vendiya.app.test_client().get('/plataforma').status_code, 302)
        self.login()
        r = self.c.get('/plataforma')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/plataforma/login', r.headers['Location'])
        self.assertEqual(self.post('/plataforma/negocios/crear', {'nombre': 'X', 'usuario': 'a', 'password': '123456'}
                                   ).status_code, 302)
        self.assertEqual(len(plataforma.listar_negocios()), 2)

    def test_login_de_superadmin_y_separacion_de_sesiones(self):
        sa = self.soy_superadmin()
        self.assertEqual(sa.get('/plataforma').status_code, 200)
        # un superadmin no es un usuario de negocio
        self.assertEqual(sa.get('/pos').status_code, 302)
        self.assertEqual(sa.get('/api/productos/buscar').status_code, 401)
        # y mala contraseña no entra
        otro = vendiya.app.test_client()
        otro.get('/plataforma/login')
        otro.post('/plataforma/login', data={'usuario': 'jefe', 'password': 'mala',
                                             'csrf_token': self.token(otro)})
        self.assertEqual(otro.get('/plataforma').status_code, 302)

    def test_login_de_superadmin_se_bloquea_tras_5_fallos(self):
        plataforma.crear_superadmin('jefe', 'superClave99')
        cli = vendiya.app.test_client()
        for _ in range(5):
            cli.get('/plataforma/login')
            cli.post('/plataforma/login', data={'usuario': 'jefe', 'password': 'x', 'csrf_token': self.token(cli)})
        cli.get('/plataforma/login')
        r = cli.post('/plataforma/login', data={'usuario': 'jefe', 'password': 'superClave99',
                                                'csrf_token': self.token(cli)}, follow_redirects=True)
        self.assertIn('Demasiados intentos', r.get_data(as_text=True))

    def test_crear_negocio_desde_el_panel_y_entrar_con_sus_credenciales(self):
        sa = self.soy_superadmin()
        r = self.post('/plataforma/negocios/crear', {
            'nombre': 'Panadería Ñandú', 'dueno': 'Marta', 'usuario': 'marta', 'password': 'clave456',
            'plan': 'basico', 'dias': 30, 'max_usuarios': 4, 'email': 'm@x.com'}, client=sa, follow_redirects=True)
        self.assertIn('panaderia-nandu', r.get_data(as_text=True))          # código generado sin tildes ni eñes
        n = plataforma.obtener_por_slug('panaderia-nandu')
        self.assertEqual((n['plan'], n['max_usuarios'], n['email']), ('basico', 4, 'm@x.com'))
        self.assertTrue(n['vence'])

        cli = vendiya.app.test_client()
        self.login('marta', 'clave456', client=cli, negocio='panaderia-nandu')
        self.assertEqual(cli.get('/pos').status_code, 200)
        # su base está vacía: nada de los otros negocios
        self.assertEqual(cli.get('/api/clientes').get_json(), [])
        c = database.get_db(n['id'])
        self.assertEqual(c.execute("SELECT usuario, rol FROM usuarios").fetchall()[0]['rol'], 'admin')
        self.assertEqual(c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 1)

    def test_codigos_invalidos_reservados_y_repetidos(self):
        for malo in ('ab', 'Con Espacios', 'con_guion_bajo', '-empieza', 'termina-', 'admin', 'plataforma', 'x' * 31):
            with self.assertRaises(plataforma.ErrorPlataforma, msg=malo):
                plataforma.crear_negocio('N', 'D', 'admin', 'clave123', slug=malo)
        with self.assertRaises(plataforma.ErrorPlataforma):
            plataforma.crear_negocio('Otro', 'D', 'admin', 'clave123', slug='uno')
        self.assertEqual(len(plataforma.listar_negocios()), 2)             # no quedó nada a medias
        # El código automático nunca choca
        a = plataforma.crear_negocio('Bodega Uno', 'D', 'admin', 'clave123')
        b = plataforma.crear_negocio('Bodega Uno', 'D', 'admin', 'clave123')
        self.assertEqual(len({a['slug'], b['slug'], 'uno'}), 3)

    def test_validaciones_al_crear(self):
        for args in (('', 'D', 'admin', 'clave123'), ('N', 'D', 'a', 'clave123'),
                     ('N', 'D', 'admin', '123'), ('N', 'D', 'ad min', 'clave123')):
            with self.assertRaises(plataforma.ErrorPlataforma):
                plataforma.crear_negocio(*args)

    def test_acciones_del_panel_sobre_un_negocio(self):
        sa = self.soy_superadmin()
        base = f'/plataforma/negocios/{self.nid}'
        self.assertEqual(sa.get(base).status_code, 200)

        self.post(f'{base}/estado', {'accion': 'suspender'}, client=sa)
        self.assertEqual(plataforma.obtener_negocio(self.nid)['estado'], 'suspendido')
        self.post(f'{base}/estado', {'accion': 'activar'}, client=sa)
        self.assertEqual(plataforma.obtener_negocio(self.nid)['estado'], 'activo')

        self.post(f'{base}/extender', {'dias': 90}, client=sa)
        self.assertTrue(plataforma.obtener_negocio(self.nid)['vence'])
        self.post(f'{base}/extender', {'sin_limite': '1'}, client=sa)
        self.assertIsNone(plataforma.obtener_negocio(self.nid)['vence'])

        self.post(f'{base}/datos', {'plan': 'pro', 'max_usuarios': 7, 'email': 'a@b.com', 'telefono': '1', 'notas': 'vip'},
                  client=sa)
        n = plataforma.obtener_negocio(self.nid)
        self.assertEqual((n['plan'], n['max_usuarios'], n['notas']), ('pro', 7, 'vip'))

        # restablecer la clave del dueño: la vieja deja de servir y la nueva sirve
        self.post(f'{base}/clave', {'usuario': 'admin', 'password': 'nuevaClave1'}, client=sa)
        self.login('admin', 'admin123')
        with self.c.session_transaction() as s:
            self.assertNotIn('usuario_id', s)
        self.login('admin', 'nuevaClave1')
        self.assertEqual(self.c.get('/pos').status_code, 200)

        # eliminar exige escribir el código
        self.post(f'{base}/eliminar', {'confirmar': 'mal'}, client=sa)
        self.assertEqual(plataforma.obtener_negocio(self.nid)['estado'], 'activo')
        self.post(f'{base}/eliminar', {'confirmar': 'uno'}, client=sa)
        self.assertEqual(plataforma.obtener_negocio(self.nid)['estado'], 'eliminado')

    def test_el_panel_registra_el_historial(self):
        sa = self.soy_superadmin()
        self.post(f'/plataforma/negocios/{self.nid}/estado', {'accion': 'suspender'}, client=sa)
        html = sa.get(f'/plataforma/negocios/{self.nid}').get_data(as_text=True)
        self.assertIn('negocio_suspendido', html)
        self.assertIn('negocio_creado', html)

    def test_estadisticas_de_un_negocio(self):
        self.login()
        pid = self.crear_producto(stock=10, pv=5)
        self.vender(pid, 2, monto=20)
        e = plataforma.estadisticas(self.nid)
        self.assertEqual((e['ventas_30d'], e['total_30d'], e['productos'], e['usuarios']), (1, 10.0, 1, 1))
        self.assertEqual(plataforma.estadisticas(self.nid2)['ventas_30d'], 0)

    def test_cli_superadmin_y_listar(self):
        self.assertEqual(plataforma.main(['superadmin', '--usuario', 'jefe', '--password', 'superClave99']), 0)
        self.assertTrue(plataforma.hay_superadmin())
        self.assertEqual(plataforma.main(['superadmin', '--usuario', 'jefe', '--password', 'corta']), 1)
        self.assertEqual(plataforma.main(['crear-negocio', '--nombre', 'Desde CLI', '--usuario', 'admin',
                                          '--password', 'clave123', '--dueno', 'Yo']), 0)
        self.assertIsNotNone(plataforma.obtener_por_slug('desde-cli'))


class TestRegistroPublico(MultiBase):
    def datos(self, **extra):
        d = {'negocio': 'Mi Tienda', 'nombre': 'Pepe', 'usuario': 'pepe', 'password': 'clave123',
             'confirmar': 'clave123', 'email': 'p@x.com'}
        d.update(extra)
        return d

    def setUp(self):
        super().setUp()
        self._antes = vendiya.REGISTRO_PUBLICO

    def tearDown(self):
        vendiya.REGISTRO_PUBLICO = self._antes
        super().tearDown()

    def test_desactivado_por_defecto(self):
        vendiya.REGISTRO_PUBLICO = False
        cli = vendiya.app.test_client()
        self.assertEqual(cli.get('/registro').status_code, 404)
        cli.get('/login')                                   # da un token CSRF válido a la sesión
        r = cli.post('/registro', data=dict(self.datos(), csrf_token=self.token(cli)))
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(plataforma.obtener_por_slug('mi-tienda'))

    def test_registro_crea_el_negocio_con_prueba(self):
        vendiya.REGISTRO_PUBLICO = True
        cli = vendiya.app.test_client()
        cli.get('/registro')
        r = cli.post('/registro', data=dict(self.datos(), csrf_token=self.token(cli)), follow_redirects=True)
        self.assertIn('mi-tienda', r.get_data(as_text=True))
        n = plataforma.obtener_por_slug('mi-tienda')
        self.assertEqual(n['plan'], 'prueba')
        self.assertTrue(n['vence'])
        self.login('pepe', 'clave123', client=cli, negocio='mi-tienda')
        self.assertEqual(cli.get('/pos').status_code, 200)

    def test_contrasenas_distintas_y_campo_trampa(self):
        vendiya.REGISTRO_PUBLICO = True
        cli = vendiya.app.test_client()
        cli.get('/registro')
        cli.post('/registro', data=dict(self.datos(confirmar='otra'), csrf_token=self.token(cli)))
        self.assertIsNone(plataforma.obtener_por_slug('mi-tienda'))
        r = cli.post('/registro', data=dict(self.datos(sitio_web='http://spam'), csrf_token=self.token(cli)))
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(plataforma.obtener_por_slug('mi-tienda'))

    def test_limite_de_registros_por_conexion(self):
        vendiya.REGISTRO_PUBLICO = True
        cli = vendiya.app.test_client()
        cli.get('/registro')
        for i in range(5):
            cli.post('/registro', data=dict(self.datos(negocio=f'Tienda {i}', usuario=f'u{i}x'),
                                            csrf_token=self.token(cli)))
        self.assertEqual(len(plataforma.listar_negocios()), 2 + 5)
        cli.post('/registro', data=dict(self.datos(negocio='Tienda 99', usuario='u99x'), csrf_token=self.token(cli)))
        self.assertEqual(len(plataforma.listar_negocios()), 2 + 5)          # el sexto se rechaza


if __name__ == '__main__':
    unittest.main()
