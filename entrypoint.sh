#!/bin/sh
# entrypoint.sh
# =============================================================================
# El contenedor arranca como root (a propósito) SOLO para poder arreglar el
# dueño de /app/logs y /app/history: cuando docker-compose monta esas
# carpetas como bind mount desde el host y no existían antes, Docker las crea
# como root, y el usuario sin privilegios que corre la app (appuser, UID
# 1000) no podría escribir ahí. Este script corrige eso en cada arranque —
# sin necesidad de que quien despliega corra "chown" a mano — y recién
# después baja privilegios con `su` para ejecutar la app como appuser.
# =============================================================================
set -e

mkdir -p /app/logs /app/history
chown -R appuser:appuser /app/logs /app/history

# -p preserva las variables de entorno (SONARQUBE_HOST, etc. cargadas por
# docker-compose vía env_file) al pasar de root a appuser. Las opciones de
# `su` van ANTES del nombre de usuario (appuser va al final).
exec su -p -s /bin/sh -c "exec $*" appuser
