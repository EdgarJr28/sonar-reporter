# Dashboard Ejecutivo SonarQube (Flask)

App web 100% Python: un único servidor Flask consulta la API REST de SonarQube **en vivo** y renderiza un dashboard ejecutivo (estilo Apple, minimalista) con métricas, quality gate, todos los issues (paginación completa), gráficos Chart.js, tabla interactiva, conclusiones y recomendaciones automáticas. No genera archivos HTML estáticos: cada visita a `/project/<key>` vuelve a consultar SonarQube (con una cache corta en memoria) para tener datos frescos.

El selector de proyectos del header consulta `/api/projects/search` y navega de verdad entre proyectos: al elegir uno distinto, el navegador pide `/project/<nueva_key>` y Flask trae los datos de ese proyecto.

## Estructura

```
sonar-report/
├── app.py                 # Backend Flask — todo el núcleo en un solo archivo
├── requirements.txt         # Dependencias (flask, requests, weasyprint, matplotlib, python-dotenv)
├── .env                       # Configuración local (no versionar) — copia de .env.example
├── .env.example                # Plantilla comentada de todas las variables de entorno soportadas
├── Dockerfile                    # Imagen Linux con las librerías nativas de WeasyPrint ya instaladas
├── entrypoint.sh                   # Arranca como root, corrige permisos de logs/history y baja a appuser
├── docker-compose.yml              # Levanta la app en un contenedor (env_file, volúmenes, puerto)
├── templates/
│   ├── dashboard.html        # Plantilla Jinja2 servida en cada petición (dashboard interactivo)
│   ├── login.html              # Pantalla de login (credenciales de SonarQube)
│   ├── select_project.html      # Selector de proyectos (/select-project)
│   ├── compare.html           # Comparación lado a lado de varios proyectos (/compare)
│   ├── pdf_report.html         # Plantilla exclusiva del PDF formal (WeasyPrint)
│   └── error.html               # Página de error (SonarQube no disponible, etc.)
├── static/
│   ├── style.css               # Estilos del dashboard (tema claro/oscuro, cards, gráficos)
│   ├── pdf_report.css           # Estilos exclusivos del PDF formal (paged media)
│   └── script.js                # Gráficos Chart.js, tabla AG Grid, dark mode
├── public/
│   └── search.gif                # Icono animado del buscador / overlay de carga
├── logs/                          # Logs rotativos de errores (app.log, generate_report.log)
├── history/                        # Snapshot diario por proyecto (<key>.jsonl) para el sparkline de tendencia
└── .flask_secret_key                # Clave para firmar la cookie de sesión (se genera sola, no commitear)
```

---

## ⚠️ Antes de correrlo — checklist de configuración

Revisa estos puntos antes de ejecutar `app.py`; son la causa más común de errores al arrancar:

1. **Existe un archivo `.env`**: copiá `.env.example` a `.env` (`cp .env.example .env` o simplemente duplicá el archivo en Windows) antes de arrancar. Si no existe `.env`, la app arranca igual con los valores por defecto (SonarQube en `http://localhost:9000`, puerto `5000`, etc.).
2. **`SONARQUBE_HOST` debe estar corriendo y accesible** (por defecto `http://localhost:9000`). Verifica abriendo esa URL en el navegador antes de lanzar la app.
3. **Ya no hay un token fijo en el código**: cada persona inicia sesión en `/login` con su propia cuenta de SonarQube (usuario+contraseña, o un token personal como "usuario" con contraseña vacía). Esa cuenta debe tener permiso de **lectura** (Browse) sobre el/los proyecto(s) que quiera consultar; sin ese permiso, verá `502` al abrir un proyecto aunque el login haya sido exitoso.
4. **`DEFAULT_PROJECT_KEY` es opcional**: si lo dejás vacío, o coincide con un proyecto que no existe en esta instancia (por ejemplo, una instancia nueva o distinta a la que usaste para configurarlo), la ruta `/` no falla — muestra un selector (`/select-project`) con todos los proyectos que tu cuenta puede ver.
5. **El puerto configurado (`FLASK_PORT`, por defecto `5000`) debe estar libre** en tu máquina. Si algo más lo usa, cámbialo en `.env` o cierra el proceso que lo ocupa.
6. **Dependencias instaladas** en el mismo intérprete de Python que vas a usar para correr `app.py` (ver sección Instalación). El error típico si falta es `ModuleNotFoundError: No module named 'flask'` o `'requests'` o `'dotenv'`.
7. **HTTPS con certificado autofirmado**: si tu SonarQube usa HTTPS con un certificado no válido, las peticiones fallarán por verificación SSL. Ver la nota al final sobre `verify=False`.
8. **Proyectos muy grandes (>10.000 issues)**: la API de SonarQube limita la paginación a 10.000 resultados; si tu proyecto los supera, no se descargarán todos (se avisa por consola, no es un error fatal).
9. **Modo debug activo por defecto** (`FLASK_DEBUG=true`): cómodo para desarrollo (recarga automática al editar código), pero conviene poner `FLASK_DEBUG=false` en producción — ver nota final.

---

## Requisitos

- Python 3.8+
- Una cuenta de SonarQube (usuario/contraseña o token personal) con permiso de lectura sobre el proyecto

## Instalación

```bash
pip install -r requirements.txt
```

Si tienes varias versiones de Python instaladas, asegúrate de instalar en el mismo intérprete con el que ejecutarás `app.py` (por ejemplo `python -m pip install -r requirements.txt` o `py -m pip install -r requirements.txt` en Windows).

## Configuración

Toda la configuración vive en variables de entorno, no en `app.py` (igual que un `.env` con `dotenv` en Node). Para configurar:

```bash
cp .env.example .env
```

Y editar `.env`:

```dotenv
SONARQUBE_HOST=http://localhost:9000   # URL de tu instancia SonarQube
DEFAULT_PROJECT_KEY=WebLudycommerce2    # Proyecto que se muestra en la ruta "/" (opcional: si se deja vacío o no existe en esta instancia, "/" muestra un selector de proyectos en vez de fallar)
FLASK_PORT=5000                          # Puerto del servidor
FLASK_DEBUG=true                          # Recarga automática + tracebacks (poner "false" en producción)
FLASK_SECRET_KEY=                          # Opcional: fija la clave de sesión (si se deja vacío, se autogenera y persiste en .flask_secret_key)
CACHE_TTL_SECONDS=60                        # Segundos que se reutilizan los datos antes de re-consultar
PDF_MAX_ISSUES=300                           # Máximo de issues detallados en el PDF
HISTORY_MAX_POINTS=30                         # Puntos del sparkline de tendencia
ISSUES_PAGE_SIZE=500                           # Tamaño de página de /api/issues/search
REQUEST_TIMEOUT=60                              # Timeout (seg.) de las peticiones a SonarQube
```

`app.py` llama a `load_dotenv()` al arrancar, así que estos valores se cargan automáticamente sin tocar código. Si `.env` no existe o falta alguna variable, se usan los valores por defecto de arriba — la app nunca falla por eso.

Para producción/Docker, en vez de `.env` también podés pasar estas mismas variables como variables de entorno reales del sistema/contenedor (`docker run --env-file .env ...` o `export SONARQUBE_HOST=...`); `os.environ` tiene prioridad sobre lo que cargue `.env` si ya estuviera seteada antes.

Ya no hay un `TOKEN` fijo: la app pide login (`/login`) y usa las credenciales que cada persona ingresa para consultar SonarQube en su nombre (ver sección "Login" abajo).

## Ejecución

```bash
python app.py
```

Deberías ver en consola:

```
Servidor Flask iniciado. Abre http://localhost:5000/ en tu navegador.
```

Abrir en el navegador:

```
http://localhost:5000/
```

La primera vez te va a pedir loguearte en `http://localhost:5000/login` con tu usuario de SonarQube. Después de eso, redirige a `http://localhost:5000/project/<PROJECT_KEY>`. Para ver otro proyecto sin tocar el código, usa el selector del header o navega directamente a `http://localhost:5000/project/<otra_key>`.

Para detener el servidor: `Ctrl + C` en la terminal.

## Login

La app ya no usa un token fijo compilado en `app.py`: cada persona se loguea con **su propia cuenta de SonarQube**.

- En `/login` se pide "Usuario o token" + "Contraseña".
- Se puede usar el usuario/contraseña real de SonarQube, o (recomendado) un **token personal** (Mi cuenta → Seguridad → Tokens) pegado en el campo "Usuario" dejando la contraseña vacía — así nunca se escribe la contraseña real en el formulario.
- Al enviar el formulario, la app valida esas credenciales contra `GET {HOST}/api/authentication/validate` (Basic Auth). Si son válidas, guarda una sesión en el servidor (cookie firmada con `app.secret_key`, que se genera y persiste en `.flask_secret_key`) y redirige al dashboard.
- Todas las consultas posteriores a SonarQube (medidas, issues, quality gate, PDF, comparación de proyectos) se hacen **con las credenciales de esa persona**, así que cada quien ve exactamente lo que sus permisos de SonarQube le permiten ver.
- El botón con el ícono de persona en el header (junto al selector de proyecto) muestra el usuario logueado y permite cerrar sesión (`/logout`).
- La sesión dura 8 horas (`app.permanent_session_lifetime`) o hasta que se cierre sesión manualmente.
- **Limitación conocida**: las credenciales se guardan en memoria del proceso de Flask mientras dura la sesión (no en disco ni en la cookie del navegador). Si corres la app con varios workers (ej. `gunicorn -w 4`), las sesiones no se comparten entre procesos y el login puede fallar de forma intermitente — con `python app.py` (un solo proceso) no hay problema.

## Desplegar bajo un sub-path (ej. `https://tu-dominio.com/reports/`)

Es común querer exponer esta app en el mismo dominio donde ya corre SonarQube (que ocupa la raíz `/`), montando el dashboard de reportes bajo un sub-path como `/reports/`. Para que esto funcione hacen falta **dos partes**, no alcanza con solo configurar el proxy:

1. **nginx tiene que avisarle a Flask bajo qué prefijo la está sirviendo**, mandando el header `X-Forwarded-Prefix`. Sin esto, la app genera (y redirige a) URLs absolutas desde la raíz — `/login`, `/project/x` — perdiendo el `/reports` apenas el navegador navega a cualquier lado (por eso terminabas en `https://tu-dominio.com/login?next=/` en vez de `https://tu-dominio.com/reports/login?next=/reports/`).
2. **`app.py` tiene que confiar en ese header** — ya está resuelto: `app.wsgi_app` está envuelto con `werkzeug.middleware.proxy_fix.ProxyFix(..., x_prefix=1)`, que lee `X-Forwarded-Prefix` y hace que `url_for()`/`redirect()` generen siempre URLs con el prefijo correcto.

Config de nginx (`/etc/nginx/sites-available/sonar.ludyorder.com` o donde tengas el server block):

```nginx
location /reports/ {
    # La barra final en proxy_pass es la que hace que nginx le quite el
    # prefijo "/reports/" a la URL antes de reenviarla a Flask.
    proxy_pass http://127.0.0.1:5000/;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Este es el header clave para que Flask sepa que está bajo /reports:
    proxy_set_header X-Forwarded-Prefix /reports;
}
```

(`5000` es `FLASK_PORT` del `.env` — ajustalo si usaste otro puerto. Si corrés la app con `docker compose`, `127.0.0.1:5000` sigue funcionando igual porque el `docker-compose.yml` mapea ese puerto al host.)

Reiniciá/recargá nginx después de este cambio (`sudo nginx -t && sudo systemctl reload nginx`), y entrá directamente a `https://tu-dominio.com/reports/` (con la barra final). Si entrás sin la barra final (`/reports`), nginx no lo va a matchear contra `location /reports/` y vas a caer en cualquier otro location que tengas configurado (típicamente el de SonarQube en la raíz) — considerá agregar un redirect de `/reports` a `/reports/` si querés cubrir ese caso.

## Rutas

| Ruta | Descripción |
|---|---|
| `GET /login`, `POST /login` | Pantalla e inicio de sesión con credenciales de SonarQube. |
| `GET /logout` | Cierra la sesión actual y vuelve a `/login`. |
| `GET /` | Si `DEFAULT_PROJECT_KEY` está configurado y existe en esta instancia de SonarQube, redirige a su dashboard. Si no hay default, o el configurado no existe (ej. instancia nueva/distinta), redirige a `/select-project`. |
| `GET /select-project` | Lista todos los proyectos que la cuenta logueada puede ver en SonarQube, para elegir uno sin depender de un default fijo. |
| `GET /project/<project_key>` | Dashboard del proyecto indicado, con datos en vivo (usa cache de `CACHE_TTL_SECONDS`). Requiere login. |
| `GET /project/<project_key>?refresh=1` | Fuerza un refresco ignorando la cache (lo dispara el botón "Actualizar"). |
| `GET /project/<project_key>/pdf` | Genera y descarga el **reporte formal en PDF** (ver sección siguiente). Acepta `?detail=full` (por defecto, con detalle de issues) o `?detail=summary` (solo portada + métricas + gráficos). |
| `GET /compare` | Selector de proyectos a comparar (checkboxes). |
| `GET /compare?keys=a,b,c` | Tabla comparativa lado a lado de las métricas clave de esos proyectos. |

Todas las rutas salvo `/login` y los archivos estáticos exigen sesión iniciada; si no hay sesión válida, redirigen a `/login?next=<ruta original>`.

## Qué consulta en SonarQube

1. `/api/measures/component` — bugs, vulnerabilities, code_smells, coverage, duplicación, ncloc, ratings, alert_status.
2. `/api/qualitygates/project_status` — estado del Quality Gate.
3. `/api/projects/search` (paginado) — lista de proyectos para el selector.
4. `/api/issues/search` (paginado, sin límite de 500) — todos los issues del proyecto.
5. `/api/server/version` — versión mostrada en el footer.

## Funcionalidades del dashboard

- UI minimalista estilo Apple: tipografía del sistema, superficies blancas con hairline borders, sombras muy sutiles, colores semánticos HIG y header con efecto de vidrio esmerilado (blur).
- Selector de proyectos con navegación real (recarga `/project/<key>` en el propio servidor), con menú desplegable propio (no el nativo del navegador) estilo tarjeta flotante.
- **Health score**: tarjeta destacada arriba de todo que combina los ratings de Reliability/Security/Maintainability en un puntaje 0-100 y una letra A-E, con sparkline de tendencia de bugs (histórico diario, ver `history/`) una vez que hay al menos 2 días de datos.
- Tarjetas de resumen con contador animado, color según criticidad y un ícono de info con tooltip explicando qué significa cada métrica (bugs, vulnerabilidades, code smells, cobertura, duplicación, líneas de código, ratings A-E).
- Gráficos Chart.js: barras (bugs/code smells/vulnerabilities), doughnut (cobertura/duplicación), pie (severidades), barras horizontales (top 10 archivos), barras (top 10 reglas). Si un proyecto no tiene issues de cierto tipo (o ninguno en absoluto), se muestra un estado vacío positivo en vez de un gráfico en blanco.
- Tabla de issues con AG Grid (cargado por CDN, sin instalación): columnas con ancho automático/redimensionable, scroll horizontal e interno en vez de desbordar la página, orden por columna, paginación nativa (25/50/100), densidad compacta/cómoda configurable, y buscador + filtros por tipo/severidad reflejados en la URL (`?search=&type=&severity=`) para compartir/recargar una vista filtrada. Los badges de tipo/severidad llevan un ícono además del color (accesibilidad para daltonismo).
- Conclusiones y recomendaciones automáticas según umbrales (bugs > 500, cobertura < 50%, duplicación > 10%, vulnerabilidades = 0, etc.).
- Modo oscuro/claro persistente (`localStorage`), botón "volver arriba".
- Indicador "Actualizado hace Xs" junto al botón "Actualizar", y favicon/título de la pestaña que cambia según el estado del Quality Gate (para detectarlo sin tener la pestaña activa).
- Botón **"Comparar"**: lleva a `/compare` para ver varios proyectos lado a lado (bugs, vulnerabilidades, ratings, health score, Quality Gate).
- Botón **"Imprimir"**: usa el diálogo de impresión del navegador sobre la propia vista del dashboard (con estilos optimizados para impresión: paleta forzada a claro, cortes de página controlados, se listan todos los issues filtrados en vez de solo la página visible).
- Botón **"Exportar PDF"**: genera un **documento formal aparte** (no una captura de la pantalla), con un menú para elegir entre "Completo" o "Resumen ejecutivo" — ver sección "Reporte formal en PDF" más abajo. Mientras se genera, aparece un popup no bloqueante (esquina inferior izquierda) con mensajes de progreso; el usuario puede seguir viendo el reporte mientras tanto.
- Footer con versión de SonarQube, fecha de consulta, proyecto analizado y total de issues.

## Reporte formal en PDF

El botón **"Exportar PDF"** del dashboard (y la ruta `GET /project/<key>/pdf`) generan un documento
**independiente de la vista web**, pensado para imprimir/archivar/enviar por correo, construido con
[WeasyPrint](https://weasyprint.org/) a partir de una plantilla propia (`templates/pdf_report.html` +
`static/pdf_report.css`). Incluye:

- Portada con nombre y clave del proyecto, estado del Quality Gate y métricas clave.
- Encabezado y pie de página reales con numeración ("Página X de Y") en cada hoja.
- Resumen ejecutivo en tablas formales (métricas, ratings, condiciones del Quality Gate).
- Los mismos gráficos que el dashboard, pero renderizados en el servidor con `matplotlib` como
  imágenes estáticas (WeasyPrint no ejecuta JavaScript, por lo que Chart.js no sirve aquí).
- Conclusiones y recomendaciones como listas numeradas.
- Tabla de issues paginada de verdad entre hojas (sin scroll ni virtualización), **limitada a los
  `PDF_MAX_ISSUES` (300 por defecto) más severos** en proyectos grandes — WeasyPrint es lento
  maquetando tablas HTML de miles de filas (puede tardar varios minutos), así que el PDF prioriza
  velocidad y muestra los más críticos primero; el dashboard interactivo (AG Grid) sigue mostrando
  el listado **completo**, sin este límite. Ajustable en `app.py` (constante `PDF_MAX_ISSUES`).

### Instalación de WeasyPrint (requiere librerías del sistema)

A diferencia de Flask/requests, **WeasyPrint depende de librerías nativas** (Pango, Cairo,
GDK-PixBuf) que no se instalan solas con `pip`. Si al generar el PDF ves un error tipo
`OSError: cannot load library 'gobject-2.0-0'` o similar:

- **Windows** (pasos oficiales, ver <https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows>):
  1. Instala [MSYS2](https://www.msys2.org/#installation) dejando las opciones por defecto.
  2. Abre la terminal de **MSYS2** (no la de Windows) y ejecuta:
     ```
     pacman -S mingw-w64-x86_64-pango
     ```
  3. Cierra la terminal de MSYS2. Vuelve a tu terminal normal (cmd/PowerShell) y define la
     variable de entorno `WEASYPRINT_DLL_DIRECTORIES` apuntando a donde MSYS2 dejó las DLLs
     (por defecto `C:\msys64\mingw64\bin`) **antes** de correr `python app.py`:
     - PowerShell: `$env:WEASYPRINT_DLL_DIRECTORIES = "C:\msys64\mingw64\bin"`
     - cmd.exe: `set WEASYPRINT_DLL_DIRECTORIES=C:\msys64\mingw64\bin`
  4. Vuelve a ejecutar `python app.py`. En consola ya no debería aparecer el aviso
     `[WARN] Exportar PDF deshabilitado...`, y el botón "Exportar PDF" debería funcionar.
- **macOS**: `brew install weasyprint` (o `brew install pango cairo gdk-pixbuf libffi` si prefieres solo las librerías).
- **Linux (Debian/Ubuntu ≥ 20.04)**: `sudo apt install libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0`.

Sin esas librerías, el resto de la app (dashboard, gráficos Chart.js, tabla AG Grid, impresión del
navegador) sigue funcionando normalmente: la app ya no se cae al arrancar si WeasyPrint no cargó,
solo la ruta `/project/<key>/pdf` responde con un error 503 explicando qué falta instalar.

### Alternativa: correr la app en Docker (evita el problema por completo)

En Windows, instalar el runtime de GTK3 puede ser molesto (hay que ubicar las DLLs correctas y
agregarlas al `PATH`). En Linux esas mismas librerías son paquetes normales de apt, así que
correr la app dentro de un contenedor Docker basado en Linux evita el problema de raíz. El
repositorio incluye `Dockerfile` y `docker-compose.yml` listos para esto.

**Con Docker Compose (recomendado):**

```bash
cp .env.example .env      # si todavía no existe; ajustá SONARQUBE_HOST, etc.
docker compose up -d --build
# -> abrir http://localhost:5000 (o el puerto que hayas puesto en FLASK_PORT)
```

`docker-compose.yml` ya se encarga de: pasar todas las variables de `.env` al contenedor
(`env_file`), mapear el puerto según `FLASK_PORT`, y montar `logs/` e `history/` como volúmenes
para que persistan aunque se recree el contenedor. Para parar: `docker compose down`. Para ver
logs en vivo: `docker compose logs -f`.

No hace falta crear ni ajustar permisos de `logs/`/`history/` a mano: el contenedor arranca como
root únicamente para que `entrypoint.sh` les dé el dueño correcto (la app corre igual como un
usuario sin privilegios, UID 1000) y recién ahí baja privilegios antes de ejecutar `app.py`. Esto
resuelve automáticamente el error `PermissionError: [Errno 13] Permission denied:
'/app/logs/app.log'` que aparecía si esas carpetas del host quedaban como root — si ya te pasó,
alcanza con `docker compose up -d --build` de nuevo (sin tocar nada a mano).

Importante: sin fijar `FLASK_SECRET_KEY` en el `.env`, cada vez que se recrea el contenedor se
pierde la clave que firma las cookies de sesión y todo el mundo queda deslogueado. Generá una fija
con `python -c "import secrets; print(secrets.token_hex(32))"` y pegala en el `.env` antes de usar
Docker en serio.

**Sin Compose (manual):**

```bash
docker build -t sonar-dashboard .
docker run --rm -p 5000:5000 --env-file .env sonar-dashboard
# -> abrir http://localhost:5000
```

El `.env` no se copia dentro de la imagen (está en `.dockerignore` por seguridad); se pasa en
tiempo de ejecución con `--env-file`. Si tu SonarQube corre en el mismo Windows/Mac (fuera del
contenedor), usa `SONARQUBE_HOST=http://host.docker.internal:9000` en vez de `localhost:9000` en
el `.env`, para que el contenedor pueda alcanzarlo (en Linux hay que además descomentar
`extra_hosts` en `docker-compose.yml`, o agregar `--add-host=host.docker.internal:host-gateway`
al `docker run`).

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `ModuleNotFoundError: No module named 'flask'` (o `requests`, `dotenv`) | Dependencias no instaladas en el intérprete usado | `pip install -r requirements.txt` en el mismo entorno |
| `502 Bad Gateway` al abrir la app | SonarQube no responde, `SONARQUBE_HOST` incorrecto, o `DEFAULT_PROJECT_KEY` no existe | Verifica en `.env` que la URL/clave sean correctas y que SonarQube esté corriendo |
| `401 Unauthorized` / `403 Forbidden` al ver un proyecto | La cuenta con la que se logueó no tiene permiso de lectura sobre ese proyecto | Pide acceso (Browse) sobre el proyecto, o inicia sesión con una cuenta que sí lo tenga |
| No puedo loguearme, "Usuario/token o contraseña incorrectos" | Credenciales inválidas, o el usuario no existe en SonarQube | Verifica usuario/contraseña, o genera un token nuevo en Mi cuenta → Seguridad |
| Se desloguea todo el mundo al reiniciar la app | Se borró/regeneró `.flask_secret_key` | No lo borres entre reinicios; si se pierde, todos deben loguearse de nuevo (no es un error, es esperado) |
| `PermissionError: [Errno 13] Permission denied: '/app/logs/app.log'` (Docker) | `logs/`/`history/` del host quedaron con otro dueño (ej. root) de una versión vieja del Dockerfile | Reconstruí la imagen (`docker compose up -d --build`): `entrypoint.sh` corrige el dueño de esas carpetas automáticamente en cada arranque, ya no hace falta hacerlo a mano |
| Al entrar a `/reports/` termina en `/login?next=/` (perdiendo el prefijo) | nginx no está mandando `X-Forwarded-Prefix`, o la app es de antes de agregar `ProxyFix` | Agregá `proxy_set_header X-Forwarded-Prefix /reports;` en el `location` de nginx — ver sección "Desplegar bajo un sub-path" — y actualizá `app.py` a la versión con `ProxyFix` |
| Error de certificado SSL | SonarQube con HTTPS autofirmado | Agregar `verify=False` a `session.get(...)` en `app.py` (solo para desarrollo) |
| El puerto 5000 ya está en uso | Otro proceso lo ocupa | Cambiar `FLASK_PORT` en `.env` o cerrar el proceso que lo usa |
| Datos "viejos" tras un cambio en SonarQube | Cache en memoria (`CACHE_TTL_SECONDS`) | Usa el botón "Actualizar" (`?refresh=1`) o baja el TTL |
| `Read timed out` / `ReadTimeoutError` | SonarQube tardó más de `REQUEST_TIMEOUT` (60s) en responder | Revisa `logs/app.log` (o `logs/generate_report.log`), verifica que SonarQube esté disponible y no esté sobrecargado; sube `REQUEST_TIMEOUT` si el proyecto es muy grande |
| `502` al pulsar "Exportar PDF" / `OSError: cannot load library` | Faltan las librerías nativas de WeasyPrint (Pango/Cairo/GDK-PixBuf) | Instálalas según tu sistema operativo — ver sección "Reporte formal en PDF" — y revisa `logs/app.log` para el detalle exacto |

## Logs de errores

Tanto `app.py` como `generate_report.py` escriben advertencias/errores (con traceback completo) en la carpeta `logs/`:

- `logs/app.log` — errores del servidor Flask (timeouts, fallos HTTP hacia SonarQube, excepciones no controladas).
- `logs/generate_report.log` — errores al generar el reporte estático con `generate_report.py`.

Los archivos rotan automáticamente (máx. ~2 MB, hasta 5 respaldos) para no crecer indefinidamente. Revisa estos archivos ante cualquier error en consola.

## Notas

- La cache en memoria es por proceso: si reinicias `app.py`, se pierde. Ajusta `CACHE_TTL_SECONDS` según qué tan "en vivo" necesitas los datos vs. cuántas peticiones quieres hacerle a SonarQube.
- Si tu instancia usa HTTPS con certificado autofirmado, agrega `verify=False` a las llamadas `session.get(...)` en `app.py` (no recomendado en producción).
- La API de issues de SonarQube limita la paginación a un máximo de 10.000 resultados (offset `p * ps`); si el proyecto supera ese número, el servidor lo indica por consola.
- Para producción, no uses el servidor de desarrollo de Flask (`app.run(debug=True)`); sirve la app con un WSGI server como `gunicorn` o `waitress` detrás de un proxy, y desactiva `debug`.
