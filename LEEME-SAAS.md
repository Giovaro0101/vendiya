# VendiYA como SaaS (multi-negocio)

Cada negocio cliente tiene **su propia carpeta con sus propias bases de datos**. Un negocio nunca puede
ver datos de otro: no existe ninguna consulta compartida que pueda filtrarse por un error.

```
data/
  plataforma.db                  negocios clientes, estados, vencimientos, superadmins, tasa global
  negocios/1/minisuper.db        datos del negocio 1 (productos, ventas, clientes...)
  negocios/1/ventas_archivadas.db
  negocios/1/backups/            respaldos del negocio 1
  negocios/2/...
  respaldos_plataforma/          respaldo diario de plataforma.db
```

## Primer arranque

1. `venv\Scripts\python plataforma.py superadmin` → crea tu usuario de superadministrador
   (contraseña de 8+ caracteres).
2. `venv\Scripts\python app.py` → arranca el servidor.
   - Si existía la base de la versión de un solo negocio (`minisuper.db`), se **copia** como el primer
     negocio (el original no se toca). Se imprime su código de acceso (p. ej. `mi-mini-super`).
3. Entra a `http://localhost:5000/plataforma/login` con tu usuario de superadministrador.

## Cómo se entra

- **Clientes y sus empleados:** `/login` con **código del negocio** + usuario + contraseña.
  El enlace `/login?n=<codigo>` ya trae el código escrito (se muestra en *Configuración* del negocio
  y en el panel de plataforma). El mismo usuario (`admin`, `ana`…) puede existir en varios negocios.
- **Tú:** `/plataforma/login`. Es una cuenta aparte: no sirve en los negocios y viceversa.

## Dar de alta un cliente

- **A mano (recomendado al inicio):** panel → *Nuevo negocio* (nombre, usuario y contraseña del dueño,
  plan, días de prueba y máximo de usuarios). Le entregas el código, el usuario y la contraseña.
- **Por terminal:** `python plataforma.py crear-negocio`
- **Autoservicio:** `VENDIYA_REGISTRO_PUBLICO=1` activa `/registro` (cualquiera crea su negocio con
  `VENDIYA_DIAS_PRUEBA` días de prueba; máximo 5 altas por hora por conexión).

## Cobros y suscripciones (manual por ahora)

Cada negocio tiene **vencimiento**. Pasada la fecha no puede entrar (se le avisa 7 días antes).
Cuando te paga: panel → el negocio → **+30 / +90 / +365 días** (suma desde hoy o desde el vencimiento si
aún no pasó). *Suspender* corta el acceso al instante sin borrar nada; *Reactivar* lo devuelve.
El **máximo de usuarios** del plan se aplica automáticamente.

## Variables de entorno

| Variable | Para qué |
|---|---|
| `VENDIYA_SECRET_KEY` | Clave de sesiones (si no, se genera `.secret_key`). Con varios servidores debe ser la misma |
| `VENDIYA_HTTPS=1` | Cookies de sesión solo por HTTPS (**actívala en producción**) |
| `VENDIYA_PROXY=1` | Estás detrás de nginx/Caddy: toma la IP y el protocolo reales |
| `VENDIYA_REGISTRO_PUBLICO=1` | Activa el registro de negocios |
| `VENDIYA_DIAS_PRUEBA` | Días de prueba (14 por defecto) |
| `VENDIYA_SOPORTE` | Texto de contacto que ven los clientes bloqueados/por vencer (ej. `WhatsApp 0414-0000000`) |
| `VENDIYA_HOST` / `VENDIYA_PUERTO` / `VENDIYA_HILOS` | Dirección, puerto (5000) e hilos (8) |
| `VENDIYA_DEBUG=1` | **Solo desarrollo.** Nunca en un servidor accesible |

## Producción

1. Instala dependencias: `pip install -r requirements.txt` (incluye `waitress`, el servidor de producción;
   `app.py` lo usa solo si no está `VENDIYA_DEBUG`).
2. Pon un proxy con **HTTPS** delante (Caddy lo hace casi solo). Ejemplo `Caddyfile`:
   ```
   app.tudominio.com {
       reverse_proxy 127.0.0.1:5000
   }
   ```
3. Arranca con: `VENDIYA_HTTPS=1 VENDIYA_PROXY=1 VENDIYA_HOST=127.0.0.1 python app.py`
   (que el servicio se reinicie solo: NSSM en Windows, systemd en Linux).
4. **Un solo proceso:** el programador de tareas (respaldos, tasa, archivado) y el bloqueo de intentos de
   login viven en el proceso. Usa `VENDIYA_HILOS` para más concurrencia, no varios procesos.

## Respaldos

- Cada noche (23:30) y al arrancar: un respaldo por negocio en `data/negocios/<id>/backups/` (últimos 14)
  y de `plataforma.db` en `data/respaldos_plataforma/`.
- **Copia la carpeta `data/` fuera del servidor** (otro disco/nube): si el servidor muere, los respaldos
  dentro de él mueren con él.
- **Restaurar un negocio:** detén el programa, reemplaza `data/negocios/<id>/minisuper.db` por el respaldo
  (renombrado) y arranca.
- **Entregar/exportar los datos de un cliente:** su carpeta `data/negocios/<id>/` es todo lo suyo.
- **Eliminar un negocio** desde el panel es *lógico*: nadie entra, pero los archivos quedan en disco hasta
  que los borres tú.

## Pruebas

`venv\Scripts\python -m unittest discover -s tests` (74 pruebas; usan carpetas temporales, no tocan `data/`).

## Límites a tener presente

- SQLite por negocio va muy bien para cientos de bodegas pequeñas en un servidor. Si llegas a miles de
  negocios activos a la vez, el siguiente paso es PostgreSQL.
- El **Asistente IA** usa Ollama local: cada pregunta consume CPU/RAM del servidor. Considera dejarlo solo
  para el plan más alto.
- No hay cobro automático ni correos (recuperar contraseña): hoy el restablecimiento lo haces tú desde el panel.
- No hay términos de servicio ni política de privacidad: agrégalos antes de abrir el registro público.
