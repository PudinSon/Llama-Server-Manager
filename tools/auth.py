# tools/auth.py
"""
Autenticación diferida (stub) + helpers para proxy inverso (nginx).

Hoy `requires_auth` es un **no-op**: los endpoints nuevos ya lo llevan aplicado,
así que cuando se implemente la autenticación real sólo hay que rellenar el
cuerpo de `_check_auth()` sin tocar ningún handler.

Para activarla basta con definir `LLAMA_WEBUI_TOKEN` (o pasarla por el gestor
de configuración) y todas las rutas decoradas pasarán a exigir
`Authorization: Bearer <token>` o `X-API-Token: <token>`.
"""

from __future__ import annotations

import inspect
import logging
import os
from functools import wraps
from typing import Any, Dict, Optional

logger = logging.getLogger("auth")

#: Variable de entorno con el token compartido. Vacío = autenticación desactivada.
TOKEN_ENV_VAR = "LLAMA_WEBUI_TOKEN"


# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
def get_configured_token() -> str:
    """Token actual (vacío = desactivada)."""
    return (os.environ.get(TOKEN_ENV_VAR) or "").strip()


def auth_enabled() -> bool:
    return bool(get_configured_token())


def _header(headers: Any, name: str) -> str:
    """Lee una cabecera de un dict, de `flask.request.headers` o de starlette."""
    if headers is None:
        return ""
    try:
        value = headers.get(name)
        if value:
            return str(value)
    except Exception:
        pass
    try:  # diccionarios case-insensitive de werkzeug/starlette
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
    except Exception:
        pass
    return ""


def _find_request_headers() -> Any:
    """Intenta obtener las cabeceras de la petición actual (Flask/FastAPI)."""
    try:
        from flask import request  # type: ignore
        return request.headers
    except Exception:
        return None


def _check_auth(headers: Any = None) -> tuple:
    """Devuelve (permitido, motivo). Aquí se implementará la auth real.

    Puntos de extensión ya previstos:
      * token compartido por cabecera (Bearer / X-API-Token)
      * futura sesión por cookie o usuario/contraseña
    """
    token = get_configured_token()
    if not token:
        return True, ""

    headers = headers if headers is not None else _find_request_headers()
    provided = _header(headers, "Authorization")
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    if not provided:
        provided = _header(headers, "X-API-Token")
    if provided and provided == token:
        return True, ""
    return False, "Token de API ausente o inválido"


def requires_auth(handler):
    """
    Decorador de endpoint. Hoy: no-op si no hay token configurado.

    Uso (Flask):
        @app.route("/api/workspace/list")
        @requires_auth
        def listar(): ...

    Uso (FastMCP / Starlette): igual, arriba del handler.
    """

    if inspect.iscoroutinefunction(handler):
        @wraps(handler)
        async def async_wrapper(*args, **kwargs):
            allowed, reason = _check_auth()
            if allowed:
                return await handler(*args, **kwargs)
            logger.warning("Petición denegada: %s", reason)
            return _denied_response(handler, reason)

        async_wrapper.__wrapped__ = handler  # type: ignore[attr-defined]
        return async_wrapper

    @wraps(handler)
    def wrapper(*args, **kwargs):
        allowed, reason = _check_auth()
        if allowed:
            return handler(*args, **kwargs)
        logger.warning("Petición denegada: %s", reason)
        return _denied_response(handler, reason)

    wrapper.__wrapped__ = handler  # type: ignore[attr-defined]
    return wrapper


def _denied_response(handler, reason: str):
    """Devuelve 401 en el framework disponible."""
    try:
        from flask import jsonify  # type: ignore
        return jsonify({"success": False, "error": reason}), 401
    except Exception:
        pass
    try:
        from starlette.responses import JSONResponse  # type: ignore
        return JSONResponse({"success": False, "error": reason}, status_code=401)
    except Exception:
        return {"success": False, "error": reason}


# ----------------------------------------------------------------------------
# Proxy inverso (nginx): URLs públicas y cabeceras X-Forwarded-*
# ----------------------------------------------------------------------------
def forwarded_host(headers: Any = None) -> str:
    headers = headers if headers is not None else _find_request_headers()
    return _header(headers, "X-Forwarded-Host") or _header(headers, "Host")


def forwarded_proto(headers: Any = None) -> str:
    headers = headers if headers is not None else _find_request_headers()
    proto = _header(headers, "X-Forwarded-Proto").split(",")[0].strip()
    return proto or "http"


def forwarded_prefix(headers: Any = None) -> str:
    headers = headers if headers is not None else _find_request_headers()
    return _header(headers, "X-Forwarded-Prefix").rstrip("/")


def public_url(path: str, direct_port: Optional[int] = None,
               headers: Any = None) -> str:
    """
    Construye una URL absoluta usable desde el navegador del usuario.

    * Detrás de nginx: `https://midominio.com/<path>` (usa X-Forwarded-*).
    * Acceso directo:  `http://host:direct_port/<path>`.
    """
    headers = headers if headers is not None else _find_request_headers()
    host = forwarded_host(headers)
    prefix = forwarded_prefix(headers)
    clean = (path or "").lstrip("/")
    if not host:
        return f"{prefix}/{clean}" if prefix else f"/{clean}"

    host_only = host.split(":")[0]
    proto = forwarded_proto(headers)
    fwd_port = _header(headers, "X-Forwarded-Port").strip()
    # ¿La petición llegó por un proxy inverso (nginx)?
    proxied = bool(_header(headers, "X-Forwarded-Host"))
    if proxied:
        # Detrás de nginx el puerto del servicio NO se expone: la ruta /piper,
        # /api/workspace... ya enruta al puerto correcto en el dominio público.
        if ":" in host:
            base = f"{proto}://{host}"
        elif fwd_port and fwd_port not in ("80", "443"):
            base = f"{proto}://{host_only}:{fwd_port}"
        else:
            base = f"{proto}://{host_only}"
    elif direct_port:
        # Acceso directo: los servicios viven en OTRO puerto que la UI, así que
        # el puerto del servicio manda (aunque el Host traiga el de la UI).
        base = f"{proto}://{host_only}:{direct_port}"
    elif ":" in host:
        base = f"{proto}://{host}"
    else:
        base = f"{proto}://{host_only}"
    return f"{base}{prefix}/{clean}"


def apply_proxy_fix(app, x_for: int = 1, x_host: int = 1, x_proto: int = 1,
                    x_port: int = 1, x_prefix: int = 1) -> bool:
    """
    Aplica ProxyFix a una app Flask. Devuelve True si se aplicó.

    Necesario tras nginx para que `request.host_url`, `url_for(_external=True)`
    y los redirects usen el dominio público y no `127.0.0.1:puerto`.
    """
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix  # type: ignore
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=x_for, x_host=x_host, x_proto=x_proto,
            x_port=x_port, x_prefix=x_prefix,
        )
        logger.info("ProxyFix aplicado (confiando en X-Forwarded-*)")
        return True
    except Exception as e:
        logger.warning("No se pudo aplicar ProxyFix: %s", e)
        return False


def cors_headers() -> Dict[str, str]:
    """Cabeceras CORS estándar del proyecto (una sola fuente para todos los servicios).

     Incluye `Cross-Origin-Resource-Policy: cross-origin` además de las cabeceras
    CORS clásicas: CORS autoriza el *fetch*, pero NO la incrustación. La UI vive
    en otro origen que cada servicio (o detrás del proxy), así que un PDF que el
    modal de workspace carga en un `<iframe>` cross-origin se bloqueaba con
    `net::ERR_BLOCKED_BY_RESPONSE` por «Cross-Origin-Resource-Policy not set».

    `Cross-Origin-Embedder-Policy: require-corp` es la segunda capa: Chrome
    exige que la propia respuesta del PDF declare COEP cuando la página
    anfitriona está en modo `require-corp`, porque el visor nativo de PDF crea
    un contexto de navegación anidado que hereda esa política. En respuestas
    JSON/SSE el header es un no-op (no hay subrecursos que proteger).
    """
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Token, X-Requested-With",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Cross-Origin-Embedder-Policy": "require-corp",
    }
