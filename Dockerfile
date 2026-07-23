# =============================================================================
# Dockerfile — Dashboard Ejecutivo SonarQube (Flask)
# =============================================================================
# Imagen basada en Debian con las librerías nativas que necesita WeasyPrint
# (Pango, Cairo, GDK-PixBuf, GObject) ya instaladas vía apt — el mismo
# problema que en Windows requiere el runtime de GTK3, aquí son simples
# paquetes del sistema. Así se evita por completo el dolor de cabeza de
# "OSError: cannot load library 'libgobject-2.0-0'".
#
# IMPORTANTE: la configuración (SONARQUBE_HOST, DEFAULT_PROJECT_KEY, etc.) ya
# NO se edita en app.py sino vía variables de entorno / .env (ver README,
# sección "Configuración"). El .env NO se copia a la imagen (.dockerignore),
# así que hay que pasarlo en tiempo de ejecución.
#
# Uso recomendado: docker compose up -d --build (ver docker-compose.yml).
#
# Alternativa manual, sin compose:
#   docker build -t sonar-dashboard .
#   docker run --rm -p 5000:5000 --env-file .env sonar-dashboard
#   -> abrir http://localhost:5000
# =============================================================================

FROM python:3.12-slim

# Librerías nativas requeridas por WeasyPrint (Pango, Cairo, GDK-PixBuf, etc.)
# Nota: python:3.12-slim se basa en Debian trixie (13), donde el paquete de
# GDK-PixBuf pasó de llamarse "libgdk-pixbuf2.0-0" a "libgdk-pixbuf-2.0-0"
# (con guion extra); en trixie el nombre viejo ya no existe como paquete
# instalable, por eso se usa el nuevo acá.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi8 \
    fonts-liberation \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/entrypoint.sh

# Directorio de logs/histórico — se crean también en tiempo de ejecución,
# pero los dejamos listos para que los volúmenes se puedan montar desde
# fuera (ver docker-compose.yml). La app corre como usuario sin privilegios
# (appuser, buena práctica de seguridad), pero el CONTENEDOR arranca como
# root a propósito: entrypoint.sh corrige el dueño de logs/history (por si
# el bind mount del host los creó como root) y recién ahí baja privilegios
# con `su` antes de ejecutar la app — así no hace falta correr ningún
# "chown" manual en el host antes de levantar el contenedor.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app

EXPOSE 5000

# Chequeo de salud: pega contra /login (ruta pública, no requiere sesión)
# usando el mismo FLASK_PORT que use la app dentro del contenedor.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('FLASK_PORT', '5000') + '/login', timeout=5)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

# En producción real, considera reemplazar esto por un WSGI server
# (gunicorn/waitress) y poner FLASK_DEBUG=false en el .env — ver README.
CMD ["python", "app.py"]
