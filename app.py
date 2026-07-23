#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py
======
Aplicación web 100% Python (Flask) que sirve un dashboard ejecutivo de
SonarQube consultando su API REST EN VIVO — sin generar archivos HTML
estáticos. Todo el backend vive en este único archivo (núcleo único):
cliente HTTP hacia SonarQube, procesamiento de datos y las rutas Flask.

Rutas:
  GET /                          -> redirige al proyecto por defecto (PROJECT_KEY)
  GET /project/<project_key>     -> dashboard del proyecto indicado (datos en vivo)
  GET /project/<project_key>?refresh=1  -> fuerza refresco ignorando la cache
  GET /project/<project_key>/pdf -> reporte formal en PDF (WeasyPrint + gráficos
                                     matplotlib), con maquetación propia para
                                     papel, distinta del dashboard interactivo

El selector de proyectos del dashboard navega de verdad entre proyectos:
al elegir uno distinto, el navegador pide /project/<nueva_key> y este
servidor vuelve a consultar SonarQube para ese proyecto.

Ejecución:
    pip install -r requirements.txt
    python app.py
    -> abrir http://localhost:5000

Configuración (únicos valores que deben modificarse):
    HOST, PROJECT_KEY  (ver más abajo). El acceso a SonarQube ya no usa un
    token fijo: cada persona se loguea en /login con su propia cuenta.
"""

import base64
import io
import json
import secrets
import sys
import time
from datetime import datetime, timedelta

import logging
import os
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv

# El servidor de desarrollo de Werkzeug (FLASK_DEBUG=true) agrega muchos
# frames extra a la pila de llamadas (reloader + debugger interactivo).
# Sumado a cómo Python 3.12+ cuenta la recursión internamente, eso puede
# hacer que el límite por defecto de Python (1000) se alcance DENTRO de
# Jinja2 (p. ej. al renderizar un {% include %} sobre _user_menu.html)
# aunque no exista ningún loop real en las plantillas -> RecursionError.
# Se sube el límite para darle margen de sobra al render normal.
sys.setrecursionlimit(5000)

from flask import (
    Flask, Response, render_template, redirect, url_for, request, abort,
    send_from_directory, session as flask_session,
)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

# Carga variables desde un archivo .env (si existe) hacia os.environ, igual
# que dotenv en Node. Busca el .env junto a este archivo, sin importar desde
# qué carpeta se ejecute `python app.py`. Si no hay .env, no falla: se usan
# los valores por defecto de cada _env_xxx() de abajo.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import matplotlib
matplotlib.use("Agg")  # backend sin GUI: necesario para generar imágenes en un servidor
import matplotlib.pyplot as plt

# WeasyPrint depende de librerías nativas (Pango/Cairo/GDK-PixBuf) que NO se
# instalan solas con pip. Si faltan (típico en Windows sin el runtime de
# GTK3), importar el paquete lanza un OSError en tiempo de carga. Se importa
# de forma perezosa/segura para que el resto de la app (dashboard, gráficos
# Chart.js, tabla AG Grid, impresión del navegador) siga funcionando aunque
# la exportación a PDF no esté disponible.
WEASYPRINT_IMPORT_ERROR = None
try:
    from weasyprint import HTML
except (ImportError, OSError) as exc:
    HTML = None
    WEASYPRINT_IMPORT_ERROR = exc

# ---------------------------------------------------------------------------
# CONFIGURACIÓN — todo se lee de variables de entorno (con valores por
# defecto razonables si no están definidas), igual que un .env de Node con
# dotenv. Copia .env.example a .env y editá ahí en vez de tocar este archivo.
# ---------------------------------------------------------------------------

def _env_str(key, default):
    val = os.environ.get(key)
    return val if val not in (None, "") else default


def _env_int(key, default):
    val = os.environ.get(key)
    if val in (None, ""):
        return default
    try:
        return int(val)
    except ValueError:
        print(f"[WARN] Variable de entorno {key}='{val}' no es un entero válido, "
              f"usando el valor por defecto {default}.", file=sys.stderr)
        return default


def _env_bool(key, default):
    val = os.environ.get(key)
    if val in (None, ""):
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


HOST = _env_str("SONARQUBE_HOST", "http://localhost:9000")          # URL base de la instancia de SonarQube
# A diferencia de _env_str, acá un valor vacío en el .env es válido e
# intencional (significa "sin proyecto por defecto, mostrar selector en /"),
# así que NO cae a ningún valor hardcodeado — solo usamos "" si la variable
# ni siquiera está definida.
PROJECT_KEY = os.environ.get("DEFAULT_PROJECT_KEY", "").strip()      # Proyecto que se muestra en la ruta "/" (opcional)
# ---------------------------------------------------------------------------
# Ya NO se usa un token fijo/global: cada persona inicia sesión en /login con
# su propio usuario y contraseña de SonarQube (o pega un token personal en el
# campo "usuario" dejando la contraseña vacía). Esas credenciales se validan
# contra /api/authentication/validate y luego se usan para todas las
# consultas de esa sesión — así cada usuario solo ve lo que sus permisos de
# SonarQube le permiten ver.
# ---------------------------------------------------------------------------

PORT = _env_int("FLASK_PORT", 5000)                     # Puerto donde corre el servidor Flask
FLASK_DEBUG = _env_bool("FLASK_DEBUG", True)              # Recarga automática + traceback en el navegador
CACHE_TTL_SECONDS = _env_int("CACHE_TTL_SECONDS", 60)      # Tiempo que se reutilizan los datos antes de re-consultar SonarQube

# Máximo de issues que se listan en el detalle del PDF. WeasyPrint es lento
# maquetando tablas HTML muy grandes (miles de filas pueden tardar varios
# minutos); el dashboard interactivo (AG Grid) sigue mostrando TODOS los
# issues sin este límite — esta cota solo aplica al documento PDF.
PDF_MAX_ISSUES = _env_int("PDF_MAX_ISSUES", 300)

# Cantidad máxima de puntos históricos que se muestran en el sparkline de
# tendencia del dashboard.
HISTORY_MAX_POINTS = _env_int("HISTORY_MAX_POINTS", 30)

# Métricas solicitadas a /api/measures/component
METRIC_KEYS = [
    "bugs",
    "vulnerabilities",
    "code_smells",
    "coverage",
    "duplicated_lines_density",
    "ncloc",
    "reliability_rating",
    "security_rating",
    "sqale_rating",
    "alert_status",
]

ISSUES_PAGE_SIZE = _env_int("ISSUES_PAGE_SIZE", 500)        # Tamaño de página máximo de /api/issues/search
REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 60)            # Timeout por defecto para las peticiones HTTP (segundos)

# Mapa de rating numérico (1-5) devuelto por SonarQube a letra A-E
RATING_LETTERS = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E",
                   "1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}

app = Flask(__name__)

# =============================================================================
# Detrás de un reverse proxy (nginx) bajo un sub-path — ej.
# https://sonar.ludyorder.com/reports/ — Flask por sí solo no sabe que está
# montado bajo "/reports": generaría (y redirigiría a) URLs absolutas desde
# la raíz ("/login", "/project/x"), perdiendo el prefijo apenas el navegador
# navega. ProxyFix lee los headers X-Forwarded-* que pone nginx (Host,
# esquema https, y el prefijo) y ajusta el WSGI environ (SCRIPT_NAME, etc.)
# para que url_for()/redirect() generen siempre URLs con el prefijo correcto.
# Ver la sección "Desplegar bajo un sub-path" del README para la config de
# nginx que tiene que acompañar esto (en particular X-Forwarded-Prefix).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# =============================================================================
# Login con credenciales de SonarQube
# =============================================================================
# La cookie de sesión de Flask solo guarda un id de sesión aleatorio (`sid`);
# las credenciales reales (usuario/contraseña o token) se guardan en memoria
# del servidor en _USER_SESSIONS, nunca en la cookie del navegador.
#
# app.secret_key firma esa cookie. Se persiste en un archivo local para que
# reiniciar el servidor (o el auto-reload de debug=True) no desloguee a todo
# el mundo en cada cambio de código.
_SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flask_secret_key")


def _load_or_create_secret_key():
    # Si se define FLASK_SECRET_KEY en el entorno/.env, tiene prioridad (útil
    # en Docker/producción, donde el filesystem del contenedor puede no
    # persistir entre despliegues).
    env_key = os.environ.get("FLASK_SECRET_KEY")
    if env_key:
        return env_key
    if os.path.exists(_SECRET_KEY_FILE):
        try:
            with open(_SECRET_KEY_FILE, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            if existing:
                return existing
        except OSError:
            pass
    new_key = secrets.token_hex(32)
    try:
        with open(_SECRET_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(new_key)
    except OSError:
        pass
    return new_key


app.secret_key = _load_or_create_secret_key()
app.permanent_session_lifetime = timedelta(hours=8)

# {sid: {"username": str, "password": str}} — vive solo en memoria del
# proceso. Nota: si en el futuro se corre la app con varios workers
# (ej. gunicorn -w 4), este diccionario NO se comparte entre procesos y las
# sesiones fallarían de forma intermitente; para eso haría falta un store
# compartido (Redis, etc.). Con `python app.py` (un solo proceso) funciona bien.
_USER_SESSIONS = {}

# Rutas que no requieren estar logueado.
_PUBLIC_ENDPOINTS = {"login", "public_file", "static"}

# Carpeta "public" para assets sueltos (gifs, imágenes) que no viven en static/
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")

# Carpeta donde se guarda un snapshot diario por proyecto (histórico de
# métricas) para poder graficar una tendencia en el dashboard.
HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")
os.makedirs(HISTORY_DIR, exist_ok=True)

# =============================================================================
# Logging — todos los errores (excepciones, timeouts hacia SonarQube, etc.)
# quedan registrados en logs/app.log con fecha, nivel y traceback completo.
# =============================================================================
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "app.log")

_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))

logger = logging.getLogger("sonar_dashboard")
logger.setLevel(logging.WARNING)
logger.addHandler(_file_handler)

app.logger.addHandler(_file_handler)
app.logger.setLevel(logging.WARNING)

# Captura cualquier excepción no manejada por una ruta (además del 502/404 explícitos)
@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        # Deja que Flask/werkzeug maneje los errores HTTP normales (404, 502, etc.)
        raise error
    logger.exception("Error no controlado procesando %s", request.path)
    return render_template("error.html", message=f"Error interno inesperado: {error}"), 500


# Cache muy simple en memoria: {project_key: (timestamp, report_data)}
_CACHE = {}


# =============================================================================
# Cliente SonarQube (API REST)
# =============================================================================

def _build_session(username, password):
    """
    Crea una sesión de requests autenticada con las credenciales dadas.
    SonarQube soporta autenticación básica tanto con usuario+contraseña como
    con un token personal como "usuario" y contraseña vacía.
    """
    s = requests.Session()
    s.auth = (username, password or "")
    s.headers.update({"Accept": "application/json"})
    return s


def _user_session():
    """
    Devuelve una sesión de requests autenticada con las credenciales de la
    persona actualmente logueada (según la cookie de Flask), o None si no
    hay una sesión válida.
    """
    sid = flask_session.get("sid")
    creds = _USER_SESSIONS.get(sid) if sid else None
    if not creds:
        return None
    return _build_session(creds["username"], creds["password"])


def get_server_version(session):
    """Obtiene la versión del servidor SonarQube (para mostrar en el footer)."""
    url = f"{HOST}/api/server/version"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text.strip()
    except requests.RequestException as exc:
        print(f"[WARN] No se pudo obtener la versión del servidor: {exc}", file=sys.stderr)
        return "N/D"


def get_measures(session, project_key):
    """Consulta /api/measures/component y devuelve (nombre_proyecto, dict métricas)."""
    url = f"{HOST}/api/measures/component"
    params = {"component": project_key, "metricKeys": ",".join(METRIC_KEYS)}
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    component = data.get("component", {})
    project_name = component.get("name", project_key)

    measures = {m["metric"]: m.get("value") for m in component.get("measures", [])}
    return project_name, measures


def get_quality_gate(session, project_key):
    """Consulta /api/qualitygates/project_status."""
    url = f"{HOST}/api/qualitygates/project_status"
    params = {"projectKey": project_key}
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("projectStatus", {})


def _fetch_projects_paginated(session, url, extra_params=None):
    """Pagina un endpoint de SonarQube que devuelve {"components": [...]}."""
    page = 1
    ps = 500
    projects = []
    total = None

    while True:
        params = {"p": page, "ps": ps}
        if extra_params:
            params.update(extra_params)
        resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        components = data.get("components", [])
        for c in components:
            projects.append({"key": c.get("key"), "name": c.get("name") or c.get("key")})

        paging = data.get("paging", {})
        if total is None:
            total = paging.get("total", len(projects))
        if not components or len(projects) >= total:
            break
        page += 1

    return projects


def get_all_projects(session):
    """
    Devuelve la lista de proyectos que la cuenta logueada puede ver, para
    alimentar el selector del dashboard y /select-project.

    Usa /api/components/search_projects — el mismo endpoint que consume la
    página "Projects" de SonarQube — porque solo devuelve lo que el usuario
    tiene permiso de "Browse" y funciona para cualquier cuenta autenticada.

    NO se usa /api/projects/search: ese endpoint exige permiso "Administer
    System", así que devolvía 403 a cualquier usuario normal. Se deja como
    fallback por si una instancia vieja no expone search_projects, pero si
    también falla se propaga el error original (más informativo).
    """
    try:
        projects = _fetch_projects_paginated(
            session, f"{HOST}/api/components/search_projects"
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status not in (400, 404):
            raise
        logger.warning(
            "search_projects no disponible (%s), probando /api/projects/search", status
        )
        projects = _fetch_projects_paginated(
            session, f"{HOST}/api/projects/search"
        )

    projects.sort(key=lambda p: (p["name"] or "").lower())
    return projects


def get_all_issues(session, project_key):
    """
    Descarga TODOS los issues del proyecto mediante paginación, sin
    limitarse a los 500 registros que devuelve una sola página.
    """
    url = f"{HOST}/api/issues/search"
    page = 1
    all_issues = []
    total = None

    while True:
        params = {"componentKeys": project_key, "ps": ISSUES_PAGE_SIZE, "p": page}
        resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if total is None:
            total = data.get("total", 0)

        issues = data.get("issues", [])
        all_issues.extend(issues)

        if not issues or len(all_issues) >= total:
            break

        # SonarQube limita a 10.000 resultados vía paginación offset (p*ps).
        if page * ISSUES_PAGE_SIZE >= 10000:
            print("[WARN] Se alcanzó el límite de 10.000 resultados de la API "
                  "de búsqueda. Puede haber issues adicionales no descargados.",
                  file=sys.stderr)
            break

        page += 1

    return all_issues


# =============================================================================
# Procesamiento / normalización de datos
# =============================================================================

def normalize_rating(value):
    if value is None:
        return "N/D"
    return RATING_LETTERS.get(str(value), str(value))


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def build_severity_distribution(issues):
    order = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
    counts = {sev: 0 for sev in order}
    for issue in issues:
        sev = issue.get("severity", "INFO")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def build_top_files(issues, top_n=10):
    counter = {}
    for issue in issues:
        component = issue.get("component", "desconocido")
        file_name = component.split(":")[-1]
        counter[file_name] = counter.get(file_name, 0) + 1
    return sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:top_n]


def build_top_rules(issues, top_n=10):
    counter = {}
    for issue in issues:
        rule = issue.get("rule", "desconocida")
        counter[rule] = counter.get(rule, 0) + 1
    return sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:top_n]


def build_type_distribution(issues):
    counts = {"BUG": 0, "VULNERABILITY": 0, "CODE_SMELL": 0}
    for issue in issues:
        t = issue.get("type")
        counts[t] = counts.get(t, 0) + 1
    return counts


def format_issue_for_table(issue):
    component = issue.get("component", "")
    file_name = component.split(":")[-1] if component else ""
    line = issue.get("line", "-")
    creation_date = issue.get("creationDate", "")
    try:
        dt = datetime.strptime(creation_date[:19], "%Y-%m-%dT%H:%M:%S")
        formatted_date = dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        formatted_date = creation_date or "-"

    return {
        "type": issue.get("type", "-"),
        "severity": issue.get("severity", "-"),
        "file": file_name,
        "line": line if line else "-",
        "rule": issue.get("rule", "-"),
        "message": issue.get("message", "-"),
        "status": issue.get("status", "-"),
        "author": issue.get("author") or "Sin asignar",
        "date": formatted_date,
    }


def build_conclusions(issues_count_by_type, coverage, duplication):
    conclusions = []
    bugs = issues_count_by_type.get("BUG", 0)
    vulnerabilities = issues_count_by_type.get("VULNERABILITY", 0)

    if bugs > 500:
        conclusions.append({"type": "danger", "text": "El proyecto presenta una cantidad elevada de bugs que pueden afectar la estabilidad del sistema."})
    elif bugs > 0:
        conclusions.append({"type": "warning", "text": f"Se detectaron {bugs} bugs que deben ser revisados y corregidos para mejorar la fiabilidad del sistema."})
    else:
        conclusions.append({"type": "success", "text": "No se detectaron bugs en el análisis."})

    if coverage < 50:
        conclusions.append({"type": "warning", "text": f"Se recomienda incrementar las pruebas unitarias, la cobertura actual es de {coverage:.1f}%."})
    else:
        conclusions.append({"type": "success", "text": f"La cobertura de pruebas ({coverage:.1f}%) se encuentra en un nivel aceptable."})

    if duplication > 10:
        conclusions.append({"type": "warning", "text": f"Existe una alta duplicación de código ({duplication:.1f}%)."})
    else:
        conclusions.append({"type": "success", "text": f"El nivel de duplicación de código ({duplication:.1f}%) es aceptable."})

    if vulnerabilities == 0:
        conclusions.append({"type": "success", "text": "No se detectaron vulnerabilidades."})
    else:
        conclusions.append({"type": "danger", "text": f"Se detectaron {vulnerabilities} vulnerabilidades que requieren atención inmediata por razones de seguridad."})

    return conclusions


def build_recommendations(issues_count_by_type, coverage, duplication):
    recommendations = []

    if issues_count_by_type.get("BUG", 0) > 0:
        recommendations.append("Priorizar la corrección de bugs de severidad BLOCKER y CRITICAL antes de continuar con nuevas funcionalidades.")
    if issues_count_by_type.get("VULNERABILITY", 0) > 0:
        recommendations.append("Realizar una revisión de seguridad enfocada en las vulnerabilidades detectadas, priorizando aquellas de mayor severidad.")
    if coverage < 80:
        recommendations.append("Establecer una meta de cobertura mínima del 80% para nuevo código e incorporar pruebas automatizadas en el pipeline de CI/CD.")
    if duplication > 5:
        recommendations.append("Refactorizar los bloques de código duplicado identificados para mejorar la mantenibilidad.")
    if issues_count_by_type.get("CODE_SMELL", 0) > 100:
        recommendations.append("Planificar sesiones de refactorización periódicas para reducir la deuda técnica acumulada (code smells).")

    if not recommendations:
        recommendations.append("El proyecto se encuentra en buen estado general. Mantener las prácticas actuales de calidad y revisión de código.")

    return recommendations


def rating_css_class(letter):
    return {"A": "rating-a", "B": "rating-b", "C": "rating-c", "D": "rating-d", "E": "rating-e"}.get(letter, "rating-na")


HEALTH_LETTER_POINTS = {"A": 100, "B": 80, "C": 60, "D": 40, "E": 20}
HEALTH_LABELS = {
    "A": "Excelente",
    "B": "Bueno",
    "C": "Aceptable",
    "D": "Necesita atención",
    "E": "Crítico",
}


def compute_health_score(reliability_letter, security_letter, maintainability_letter, quality_gate_status):
    """
    Combina los tres ratings (A-E) en un único puntaje 0-100 y una letra,
    para dar una jerarquía visual clara ("resumen ejecutivo") en vez de que
    el usuario tenga que promediar 9 tarjetas sueltas mentalmente. Si el
    Quality Gate falló, el puntaje se limita a un tope para que ese fallo
    siempre se refleje, incluso si los ratings individuales son buenos.
    """
    scores = [
        HEALTH_LETTER_POINTS.get(reliability_letter, 50),
        HEALTH_LETTER_POINTS.get(security_letter, 50),
        HEALTH_LETTER_POINTS.get(maintainability_letter, 50),
    ]
    avg = sum(scores) / len(scores)

    if quality_gate_status == "ERROR":
        avg = min(avg, 55)

    if avg >= 90:
        letter = "A"
    elif avg >= 70:
        letter = "B"
    elif avg >= 50:
        letter = "C"
    elif avg >= 30:
        letter = "D"
    else:
        letter = "E"

    return {
        "score": round(avg),
        "letter": letter,
        "label": HEALTH_LABELS[letter],
        "css": rating_css_class(letter),
    }


# =============================================================================
# Orquestación: arma el objeto unificado para un proyecto (con cache)
# =============================================================================

def fetch_report_data(project_key, session):
    """Consulta SonarQube en vivo y arma el objeto unificado para un proyecto.

    `session` es un requests.Session ya autenticado con las credenciales de
    SonarQube de la persona logueada (ver _user_session())."""
    project_name, measures = get_measures(session, project_key)
    quality_gate = get_quality_gate(session, project_key)
    all_projects = get_all_projects(session)
    raw_issues = get_all_issues(session, project_key)
    server_version = get_server_version(session)

    bugs = safe_int(measures.get("bugs"))
    vulnerabilities = safe_int(measures.get("vulnerabilities"))
    code_smells = safe_int(measures.get("code_smells"))
    coverage = safe_float(measures.get("coverage"))
    duplication = safe_float(measures.get("duplicated_lines_density"))
    ncloc = safe_int(measures.get("ncloc"))

    reliability_letter = normalize_rating(measures.get("reliability_rating"))
    security_letter = normalize_rating(measures.get("security_rating"))
    maintainability_letter = normalize_rating(measures.get("sqale_rating"))

    alert_status = measures.get("alert_status", quality_gate.get("status", "NONE"))

    type_distribution = build_type_distribution(raw_issues)
    severity_distribution = build_severity_distribution(raw_issues)
    top_files = build_top_files(raw_issues, 10)
    top_rules = build_top_rules(raw_issues, 10)
    issues_table = [format_issue_for_table(i) for i in raw_issues]

    conclusions = build_conclusions(type_distribution, coverage, duplication)
    recommendations = build_recommendations(type_distribution, coverage, duplication)

    health = compute_health_score(reliability_letter, security_letter, maintainability_letter, alert_status)

    measures_dict = {
        "bugs": bugs,
        "vulnerabilities": vulnerabilities,
        "code_smells": code_smells,
        "coverage": coverage,
        "duplication": duplication,
        "ncloc": ncloc,
        "reliability_rating": reliability_letter,
        "security_rating": security_letter,
        "maintainability_rating": maintainability_letter,
    }

    # Snapshot histórico (una vez por día por proyecto) para el sparkline
    # de tendencia del dashboard.
    save_history_snapshot(project_key, measures_dict, len(issues_table))
    history = load_history(project_key)

    now = datetime.now()

    return {
        "project": {"key": project_key, "name": project_name, "host": HOST},
        "generated_at": {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "iso": now.isoformat(),
        },
        "server_version": server_version,
        "health": health,
        "quality_gate": {"status": alert_status, "conditions": quality_gate.get("conditions", [])},
        "measures": measures_dict,
        "ratings_css": {
            "reliability": rating_css_class(reliability_letter),
            "security": rating_css_class(security_letter),
            "maintainability": rating_css_class(maintainability_letter),
        },
        "type_distribution": type_distribution,
        "severity_distribution": severity_distribution,
        "top_files": top_files,
        "top_rules": top_rules,
        "issues": issues_table,
        "issues_total": len(issues_table),
        "conclusions": conclusions,
        "recommendations": recommendations,
        "all_projects": all_projects,
        "history": history,
    }


def _history_file(project_key):
    # Sanitiza la clave del proyecto para usarla como nombre de archivo.
    safe_key = "".join(c if c.isalnum() or c in "-_." else "_" for c in project_key)
    return os.path.join(HISTORY_DIR, f"{safe_key}.jsonl")


def save_history_snapshot(project_key, measures, issues_total):
    """
    Agrega un snapshot diario (una línea JSON) con las métricas clave del
    proyecto. Si ya existe un snapshot de hoy, no duplica: como
    fetch_report_data se llama cada vez que vence la cache (CACHE_TTL_SECONDS),
    sin este control se acumularían decenas de snapshots idénticos por día.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    path = _history_file(project_key)

    last_date = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
            if lines:
                last_date = json.loads(lines[-1]).get("date")
        except (OSError, ValueError, json.JSONDecodeError):
            last_date = None

    if last_date == today:
        return

    snapshot = {
        "date": today,
        "bugs": measures["bugs"],
        "vulnerabilities": measures["vulnerabilities"],
        "code_smells": measures["code_smells"],
        "coverage": measures["coverage"],
        "duplication": measures["duplication"],
        "issues_total": issues_total,
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("No se pudo guardar el snapshot histórico de '%s'", project_key)


def load_history(project_key, limit=HISTORY_MAX_POINTS):
    """Devuelve los últimos `limit` snapshots diarios guardados para el proyecto."""
    path = _history_file(project_key)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        snapshots = [json.loads(ln) for ln in lines[-limit:]]
        return snapshots
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("No se pudo leer el histórico de '%s'", project_key)
        return []


def get_report_data(project_key, username, session, force_refresh=False):
    """
    Devuelve los datos del proyecto usando cache en memoria (TTL corto).
    La cache se guarda por (usuario, proyecto): cada persona ve los datos
    consultados con sus propios permisos de SonarQube, sin mezclarse con
    los de otra persona logueada al mismo tiempo.
    """
    now = time.time()
    cache_key = (username, project_key)
    cached = _CACHE.get(cache_key)

    if cached and not force_refresh and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    data = fetch_report_data(project_key, session)
    _CACHE[cache_key] = (now, data)
    return data


# =============================================================================
# Gráficos para el PDF (matplotlib) — generados en el servidor como imágenes
# estáticas, ya que WeasyPrint no ejecuta JavaScript y por lo tanto no puede
# dibujar los gráficos Chart.js del dashboard interactivo.
# =============================================================================

CHART_COLORS = {
    "bug": "#ff3b30",
    "vulnerability": "#ff9500",
    "code_smell": "#ffcc00",
    "coverage": "#34c759",
    "duplication": "#ff9500",
    "remaining": "#e5e5ea",
    "severities": {
        "BLOCKER": "#8b1a1a",
        "CRITICAL": "#ff3b30",
        "MAJOR": "#ff9500",
        "MINOR": "#ffcc00",
        "INFO": "#0071e3",
    },
}

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Arial", "DejaVu Sans"],
    "text.color": "#1d1d1f",
    "axes.edgecolor": "#d8d8dc",
    "axes.labelcolor": "#1d1d1f",
    "xtick.color": "#4a4a4d",
    "ytick.color": "#4a4a4d",
})


def _fig_to_data_uri(fig):
    """Convierte una figura de matplotlib en un data URI PNG embebible en HTML."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _style_axes(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.set_facecolor("#ffffff")
    ax.tick_params(labelsize=8.5)


def build_bar_comparison_chart(measures):
    labels = ["Bugs", "Code Smells", "Vulnerabilidades"]
    values = [
        measures.get("bugs", 0),
        measures.get("code_smells", 0),
        measures.get("vulnerabilities", 0),
    ]
    colors = [CHART_COLORS["bug"], CHART_COLORS["code_smell"], CHART_COLORS["vulnerability"]]

    fig, ax = plt.subplots(figsize=(5, 3))
    bars = ax.bar(labels, values, color=colors, width=0.5)
    for bar, value in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:,}",
            ha="center", va="bottom", fontsize=9,
        )
    ax.set_title("Bugs, Code Smells y Vulnerabilidades", fontsize=11, pad=12)
    _style_axes(ax)
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def build_doughnut_coverage_chart(coverage, duplication):
    fig, ax = plt.subplots(figsize=(3.6, 3.6))
    remaining = max(0.0, 100 - coverage)
    ax.pie(
        [coverage, remaining],
        colors=[CHART_COLORS["coverage"], CHART_COLORS["remaining"]],
        startangle=90,
        wedgeprops={"width": 0.35, "edgecolor": "#ffffff"},
    )
    ax.text(0, 0.06, f"{coverage:.1f}%", ha="center", va="center", fontsize=16, fontweight="bold")
    ax.text(0, -0.16, "Cobertura", ha="center", va="center", fontsize=9, color="#6e6e73")
    ax.set_title(f"Cobertura de pruebas  ·  Duplicación {duplication:.1f}%", fontsize=10, pad=16)
    ax.set_aspect("equal")
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def build_pie_severity_chart(severity_distribution):
    order = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
    values = [severity_distribution.get(s, 0) for s in order]
    colors = [CHART_COLORS["severities"][s] for s in order]
    total = sum(values)

    fig, ax = plt.subplots(figsize=(4.6, 3.6))
    if total == 0:
        ax.text(0.5, 0.5, "Sin issues registrados", ha="center", va="center", fontsize=11, color="#6e6e73")
        ax.axis("off")
    else:
        wedges, _ = ax.pie(values, colors=colors, startangle=90, wedgeprops={"edgecolor": "#ffffff"})
        ax.legend(
            wedges, [f"{s} ({v})" for s, v in zip(order, values)],
            loc="center left", bbox_to_anchor=(1, 0.5), fontsize=8.5, frameon=False,
        )
    ax.set_title("Distribución por severidad", fontsize=10, pad=16)
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def build_top_files_chart(top_files):
    items = top_files or [("(sin datos)", 0)]
    labels = [f[0] for f in items][::-1]
    values = [f[1] for f in items][::-1]

    fig, ax = plt.subplots(figsize=(5.6, 3.6))
    ax.barh(labels, values, color="#0071e3", height=0.6)
    for i, v in enumerate(values):
        ax.text(v, i, f" {v}", va="center", fontsize=8)
    ax.set_title("Top 10 archivos con más issues", fontsize=10, pad=12)
    _style_axes(ax)
    ax.tick_params(axis="y", labelsize=7.5)
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def build_top_rules_chart(top_rules):
    items = top_rules or [("(sin datos)", 0)]
    labels = [r[0] for r in items]
    values = [r[1] for r in items]

    fig, ax = plt.subplots(figsize=(5.6, 3.6))
    ax.bar(labels, values, color="#af52de", width=0.6)
    ax.set_title("Top 10 reglas incumplidas", fontsize=10, pad=12)
    _style_axes(ax)
    ax.tick_params(axis="x", labelrotation=40, labelsize=7)
    for label in ax.get_xticklabels():
        label.set_ha("right")
    fig.tight_layout()
    return _fig_to_data_uri(fig)


def build_pdf_charts(data):
    """Genera todos los gráficos del PDF y los devuelve como data URIs PNG."""
    measures = data["measures"]
    return {
        "bar_comparison": build_bar_comparison_chart(measures),
        "doughnut_coverage": build_doughnut_coverage_chart(measures["coverage"], measures["duplication"]),
        "pie_severity": build_pie_severity_chart(data["severity_distribution"]),
        "top_files": build_top_files_chart(data["top_files"]),
        "top_rules": build_top_rules_chart(data["top_rules"]),
    }


SEVERITY_ORDER = {"BLOCKER": 0, "CRITICAL": 1, "MAJOR": 2, "MINOR": 3, "INFO": 4}


def select_issues_for_pdf(issues, limit=PDF_MAX_ISSUES):
    """
    Selecciona los issues a listar en el PDF: los más severos primero, hasta
    `limit`. WeasyPrint es lento maquetando tablas HTML de miles de filas, así
    que en proyectos grandes el PDF solo detalla los más críticos (el dashboard
    interactivo con AG Grid sigue mostrando el listado completo sin límite).
    """
    ordered = sorted(issues, key=lambda i: SEVERITY_ORDER.get(i.get("severity"), 99))
    return ordered[:limit]


# =============================================================================
# Rutas Flask
# =============================================================================

def _is_safe_next_path(path):
    """
    Valida que un valor de ?next= sea una ruta relativa dentro de esta misma
    app (nunca una URL absoluta a otro sitio) — evita un open redirect si
    alguien arma a mano un link tipo /login?next=https://evil.example.
    """
    return bool(path) and path.startswith("/") and not path.startswith("//") and "://" not in path


@app.before_request
def require_login():
    """Exige haber iniciado sesión para cualquier ruta salvo login/estáticos."""
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    sid = flask_session.get("sid")
    if not sid or sid not in _USER_SESSIONS:
        flask_session.clear()
        # request.path no incluye el prefijo si la app corre detrás de un
        # reverse proxy bajo un sub-path (ej. "/reports"); hay que sumarle
        # request.script_root (resuelto por ProxyFix a partir de
        # X-Forwarded-Prefix) para que el "next" apunte al lugar correcto.
        next_path = request.script_root + request.full_path.rstrip("?")
        return redirect(url_for("login", next=next_path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    # Si ya hay sesión activa, no tiene sentido mostrar el login de nuevo.
    sid = flask_session.get("sid")
    if sid and sid in _USER_SESSIONS:
        return redirect(url_for("index"))

    error = None
    username_value = ""
    login_mode = "credentials"
    if request.method == "POST":
        username_value = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # El switch usuario/contraseña vs token en login.html manda este campo
        # oculto para que, si hay error y se vuelve a mostrar el form, quede
        # seleccionada la misma pestaña que el usuario había elegido.
        login_mode = request.form.get("login_mode", "").strip() or ("token" if not password else "credentials")

        if not username_value:
            error = "Ingresá tu usuario (o token) de SonarQube."
        else:
            try:
                probe = _build_session(username_value, password)
                resp = probe.get(f"{HOST}/api/authentication/validate", timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                valid = bool(resp.json().get("valid"))
            except requests.RequestException as exc:
                logger.warning("No se pudo validar login contra SonarQube (%s): %s", HOST, exc)
                error = f"No se pudo conectar a SonarQube en {HOST}. Verificá que esté corriendo."
                valid = False

            if valid:
                new_sid = secrets.token_urlsafe(24)
                _USER_SESSIONS[new_sid] = {"username": username_value, "password": password}
                flask_session.clear()
                flask_session["sid"] = new_sid
                flask_session["username"] = username_value
                flask_session.permanent = True
                requested_next = request.args.get("next") or request.form.get("next")
                next_url = requested_next if _is_safe_next_path(requested_next) else url_for("index")
                return redirect(next_url)
            elif not error:
                error = "Usuario/token o contraseña incorrectos."

    return render_template(
        "login.html", error=error, username_value=username_value, host=HOST, login_mode=login_mode
    )


@app.route("/logout")
def logout():
    sid = flask_session.pop("sid", None)
    flask_session.clear()
    if sid:
        _USER_SESSIONS.pop(sid, None)
    return redirect(url_for("login"))


@app.route("/public/<path:filename>")
def public_file(filename):
    """Sirve assets sueltos de la carpeta public/ (ej. search.gif)."""
    return send_from_directory(PUBLIC_DIR, filename)


@app.route("/")
def index():
    """
    Si hay un DEFAULT_PROJECT_KEY configurado (.env) Y ese proyecto existe en
    esta instancia de SonarQube, va directo a su dashboard (comportamiento de
    siempre). Si no hay default, o el que está configurado no existe en este
    servidor (ej. una instancia nueva/distinta donde ese proyecto todavía no
    se analizó), muestra un selector con todos los proyectos disponibles en
    vez de fallar con un 404/502 al intentar cargar uno que no existe.
    """
    sonar_session = _user_session()

    if not PROJECT_KEY:
        return redirect(url_for("select_project"))

    try:
        all_projects = get_all_projects(sonar_session)
    except requests.RequestException:
        # No se pudo ni listar proyectos (SonarQube caído, etc.) — dejamos
        # que /project/<key> intente igual y muestre su propio error 502
        # con el detalle de qué falló, en vez de duplicar el manejo acá.
        return redirect(url_for("project_dashboard", project_key=PROJECT_KEY))

    if any(p.get("key") == PROJECT_KEY for p in all_projects):
        return redirect(url_for("project_dashboard", project_key=PROJECT_KEY))

    logger.warning(
        "DEFAULT_PROJECT_KEY='%s' no existe en esta instancia de SonarQube; mostrando selector de proyectos.",
        PROJECT_KEY,
    )
    return redirect(url_for("select_project"))


@app.route("/select-project")
def select_project():
    """Selector de proyectos: lista todo lo que ve la cuenta logueada en SonarQube."""
    username = flask_session.get("username")
    sonar_session = _user_session()
    try:
        all_projects = get_all_projects(sonar_session)
    except requests.Timeout as exc:
        logger.error("Timeout consultando SonarQube (%s) para /select-project: %s", HOST, exc)
        abort(502, description=f"SonarQube no respondió a tiempo ({HOST}). Verifica que esté corriendo y accesible.")
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 401:
            logger.warning("Credenciales de SonarQube rechazadas al listar proyectos, cerrando sesión")
            return redirect(url_for("logout"))
        if status == 403:
            logger.warning("SonarQube devolvió 403 al listar proyectos para '%s'", username)
            abort(
                502,
                description=(
                    "Tu cuenta de SonarQube no tiene permiso para listar proyectos. "
                    "Pedile a un administrador que te dé permiso de 'Browse' sobre los "
                    "proyectos que necesitás ver."
                ),
            )
        logger.exception("Error HTTP listando proyectos para /select-project")
        abort(502, description=f"Error consultando SonarQube en {HOST}: {exc}")
    except requests.RequestException as exc:
        logger.exception("No se pudo listar proyectos para /select-project")
        abort(502, description=f"No se pudo conectar a SonarQube en {HOST}: {exc}")

    return render_template(
        "select_project.html",
        all_projects=all_projects,
        default_project_key=PROJECT_KEY,
        current_username=username,
    )


@app.route("/project/<path:project_key>")
def project_dashboard(project_key):
    force_refresh = request.args.get("refresh") == "1"
    username = flask_session.get("username")
    sonar_session = _user_session()
    try:
        data = get_report_data(project_key, username, sonar_session, force_refresh=force_refresh)
    except requests.Timeout as exc:
        logger.error("Timeout consultando SonarQube (%s) para el proyecto '%s': %s", HOST, project_key, exc)
        abort(502, description=f"SonarQube no respondió a tiempo ({HOST}). Verifica que esté corriendo y accesible.")
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 401:
            logger.warning("Credenciales de SonarQube rechazadas para '%s', cerrando sesión", username)
            return redirect(url_for("logout"))
        logger.exception("Error HTTP consultando SonarQube para el proyecto '%s'", project_key)
        abort(502, description=f"Error consultando SonarQube para '{project_key}': {exc}")
    except requests.RequestException as exc:
        logger.exception("No se pudo conectar a SonarQube (%s) para el proyecto '%s'", HOST, project_key)
        abort(502, description=f"No se pudo conectar a SonarQube en {HOST}: {exc}")

    return render_template(
        "dashboard.html",
        data=data,
        data_json=json.dumps(data, ensure_ascii=False),
        current_username=username,
    )


@app.route("/compare")
def compare_projects():
    """
    Compara varios proyectos lado a lado. Sin ?keys=, muestra un selector
    (checkboxes) con todos los proyectos de la instancia; con ?keys=a,b,c
    arma una tabla comparativa de métricas clave para esos proyectos.
    """
    # Acepta tanto ?keys=a,b,c (link compartible) como ?keys=a&keys=b (checkboxes de un form GET).
    keys_list_param = request.args.getlist("keys")
    if len(keys_list_param) > 1:
        selected_keys = [k.strip() for k in keys_list_param if k.strip()]
    else:
        selected_keys = [k.strip() for k in (keys_list_param[0] if keys_list_param else "").split(",") if k.strip()]

    username = flask_session.get("username")
    sonar_session = _user_session()
    try:
        all_projects = get_all_projects(sonar_session)
    except requests.RequestException as exc:
        logger.exception("No se pudo consultar la lista de proyectos para /compare")
        abort(502, description=f"No se pudo conectar a SonarQube en {HOST}: {exc}")

    if not selected_keys:
        return render_template(
            "compare.html",
            all_projects=all_projects,
            selected_keys=[],
            projects_data=[],
            failed_keys=[],
            current_username=username,
        )

    projects_data = []
    failed_keys = []
    for key in selected_keys:
        try:
            projects_data.append(get_report_data(key, username, sonar_session))
        except requests.RequestException:
            logger.exception("No se pudo cargar '%s' para /compare", key)
            failed_keys.append(key)

    return render_template(
        "compare.html",
        all_projects=all_projects,
        selected_keys=selected_keys,
        projects_data=projects_data,
        failed_keys=failed_keys,
        current_username=username,
    )


@app.route("/project/<path:project_key>/pdf")
def project_pdf(project_key):
    """
    Genera el reporte formal en PDF (WeasyPrint) para el proyecto indicado.
    Usa una plantilla propia (templates/pdf_report.html + static/pdf_report.css)
    pensada para papel, con gráficos renderizados como imágenes (matplotlib)
    en vez de reutilizar la vista interactiva del dashboard.
    """
    if HTML is None:
        logger.error("Exportar PDF no disponible: WeasyPrint no se pudo importar (%s)", WEASYPRINT_IMPORT_ERROR)
        abort(
            503,
            description=(
                "La exportación a PDF no está disponible: faltan las librerías nativas de "
                "WeasyPrint (Pango/Cairo/GDK-PixBuf). En Windows instala el runtime de GTK3 "
                "y reinicia la app — ver sección \"Reporte formal en PDF\" del README. "
                f"Detalle técnico: {WEASYPRINT_IMPORT_ERROR}"
            ),
        )

    force_refresh = request.args.get("refresh") == "1"
    # "summary" = portada + resumen ejecutivo + gráficos + conclusiones/
    # recomendaciones, SIN el detalle de issues (más rápido y liviano).
    # "full" (por defecto) agrega la tabla de issues (hasta PDF_MAX_ISSUES).
    detail = request.args.get("detail", "full")
    if detail not in ("summary", "full"):
        detail = "full"

    username = flask_session.get("username")
    sonar_session = _user_session()
    try:
        data = get_report_data(project_key, username, sonar_session, force_refresh=force_refresh)
    except requests.Timeout as exc:
        logger.error("Timeout consultando SonarQube (%s) al generar PDF de '%s': %s", HOST, project_key, exc)
        abort(502, description=f"SonarQube no respondió a tiempo ({HOST}). Verifica que esté corriendo y accesible.")
    except requests.HTTPError as exc:
        logger.exception("Error HTTP consultando SonarQube al generar PDF de '%s'", project_key)
        abort(502, description=f"Error consultando SonarQube para '{project_key}': {exc}")
    except requests.RequestException as exc:
        logger.exception("No se pudo conectar a SonarQube (%s) al generar PDF de '%s'", HOST, project_key)
        abort(502, description=f"No se pudo conectar a SonarQube en {HOST}: {exc}")

    try:
        charts = build_pdf_charts(data)
        pdf_issues = select_issues_for_pdf(data["issues"]) if detail == "full" else []

        pdf_css_path = os.path.join(app.static_folder, "pdf_report.css")
        with open(pdf_css_path, "r", encoding="utf-8") as f:
            pdf_css = f.read()

        html_string = render_template(
            "pdf_report.html",
            data=data,
            charts=charts,
            pdf_detail=detail,
            pdf_issues=pdf_issues,
            pdf_issues_truncated=len(data["issues"]) > len(pdf_issues),
            pdf_css=pdf_css,
        )

        pdf_bytes = HTML(string=html_string).write_pdf()
    except Exception:
        logger.exception("Error generando el PDF para el proyecto '%s'", project_key)
        abort(502, description="No se pudo generar el PDF del reporte. Revisa logs/app.log para más detalle.")

    suffix = "resumen" if detail == "summary" else "completo"
    filename = f"reporte-sonarqube-{project_key}-{suffix}-{data['generated_at']['date']}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.errorhandler(404)
def not_found(error):
    # Manejador dedicado para que un 404 (URL inexistente, favicon.ico, un
    # bookmark viejo, etc.) muestre la página de error con el estilo de la
    # app, sin ensuciar logs/app.log. Sin esto, caía en el
    # @app.errorhandler(Exception) genérico, que al re-lanzar el
    # HTTPException hacía que Flask le metiera un traceback completo al log
    # por algo que ni siquiera es un error real de la aplicación. logger.info
    # queda por debajo del nivel WARNING configurado, así que un 404 normal
    # no deja nada en el archivo (si algún día lo querés ver, bajá el nivel
    # del logger a INFO).
    logger.info("404 Not Found: %s", request.path)
    return render_template("error.html", message=f"La página \"{request.path}\" no existe."), 404


@app.errorhandler(502)
def bad_gateway(error):
    return render_template("error.html", message=error.description), 502


@app.errorhandler(503)
def service_unavailable(error):
    return render_template("error.html", message=error.description), 503


if __name__ == "__main__":
    if HTML is None:
        print(
            "[WARN] Exportar PDF deshabilitado: no se pudo importar WeasyPrint "
            f"({WEASYPRINT_IMPORT_ERROR}). El dashboard funciona normalmente; "
            "instala las librerías nativas de WeasyPrint para habilitarlo "
            "(ver sección \"Reporte formal en PDF\" del README)."
        )
    print(f"Servidor Flask iniciado. Abre http://localhost:{PORT}/ en tu navegador.")
    app.run(host="0.0.0.0", port=PORT, debug=FLASK_DEBUG)
