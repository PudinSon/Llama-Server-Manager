"""Servicio de automatización 24/7 **independiente** (puerto 15001).

Este proceso es el corazón de la autonomía: vive fuera del Manager y fuera de
`dev_agent`, así que la automatización sigue funcionando aunque el Manager esté
cerrado o el agente HTTP no se haya arrancado. Solo muere cuando el sistema se
apaga o el usuario lo detiene.

Responsabilidades:

1. Arrancar el **dispatcher** (cola persistente + pool de workers + janitor).
2. Arrancar el **listener IMAP** (si hay configuración) que alimenta la cola.
3. Exponer una **API HTTP** para que la pestaña de automatización de la UI nativa
   de llama.cpp consulte estado, tareas, aprobaciones, auditoría y dispatchers.
4. Servir la **ingesta de webhooks** (`POST /webhook/<nombre>`): cualquier sistema
   externo convierte su evento en una tarea sin conocer el token global.

Arranque manual:

    python -m tools.automation_service                # 127.0.0.1:15001
    python tools/automation_service.py --port 15001   # ejecución suelta

La UI **no** ejecuta nada por sí misma: todo el trabajo vive aquí.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from typing import Any, Callable, Dict, Optional, Tuple

from flask import Flask, Response, jsonify, request

try:  # paquete
    from .auth import _check_auth, cors_headers
    from .cron_dispatcher import start_cron_dispatcher, stop_cron_dispatcher
    from .filewatch_dispatcher import start_filewatch_dispatcher, stop_filewatch_dispatcher
    from .rate_limit import get_rate_limiter
except ImportError:  # pragma: no cover - ejecución suelta (python tools/automation_service.py)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.auth import _check_auth, cors_headers  # type: ignore
    from tools.cron_dispatcher import start_cron_dispatcher, stop_cron_dispatcher  # type: ignore
    from tools.filewatch_dispatcher import (  # type: ignore
        start_filewatch_dispatcher,
        stop_filewatch_dispatcher,
    )
    from tools.rate_limit import get_rate_limiter  # type: ignore

DEFAULT_HOST = os.environ.get("AUTOMATION_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("AUTOMATION_PORT", "15001"))
DEFAULT_WORKSPACE = os.environ.get("AUTOMATION_WORKSPACE", "")

logger = logging.getLogger("automation_service")

_rate_limiter = get_rate_limiter(limit=240, window_seconds=60)


# ----------------------------------------------------------------------------
# Acceso perezoso a los componentes (para que los tests no arranquen Chroma)
# ----------------------------------------------------------------------------
def get_dispatcher():
    from .automation_dispatcher import get_dispatcher as _get

    return _get(workspace_path=DEFAULT_WORKSPACE or None)


def get_imap_listener():
    from .imap_listener import get_imap_listener as _get

    return _get()


def get_webhook_dispatcher():
    from .webhook_dispatcher import get_webhook_dispatcher as _get

    return _get()


def get_filewatch_dispatcher():
    from .filewatch_dispatcher import get_filewatch_dispatcher as _get

    return _get()


def get_cron_dispatcher():
    from .cron_dispatcher import get_cron_dispatcher as _get

    return _get()


def get_dispatcher_control():
    from .dispatcher_control import get_dispatcher_control as _get

    return _get()


def dispatcher_providers():
    """Los cuatro **dispatchers** (fuentes de eventos) que ve el panel unificado.

    Se resuelven en cada llamada (no en un tuple a nivel de módulo) para que un
    test —o un futuro servicio con inyección— pueda sustituirlos.
    """
    return (
        ("webhook", get_webhook_dispatcher),
        ("filewatch", get_filewatch_dispatcher),
        ("cron", get_cron_dispatcher),
        ("imap", get_imap_listener),
    )


def _dispatcher_panel_entry(kind: str, provider: Callable[[], Any]) -> Dict[str, Any]:
    """Ficha normalizada de un dispatcher para el panel (mismo shape siempre)."""
    from .dispatcher_control import LABELS

    entry: Dict[str, Any] = {"kind": kind, "label": LABELS.get(kind, kind)}
    entry.update(get_dispatcher_control().state(kind))
    try:
        raw = provider().status() or {}
    except Exception as exc:
        raw = {"error": str(exc)}
    entry["status"] = raw
    entry["running"] = bool(raw.get("running", True))
    entry["configured"] = bool(raw.get("configured", True))
    entry["total"] = int(raw.get("total") or 0)
    entry["enqueued_total"] = int(raw.get("enqueued_total") or 0)
    entry["rejected_total"] = int(raw.get("rejected_total") or 0)
    entry["last_event"] = raw.get("last_event") or raw.get("last_poll")
    return entry


def _public_webhook(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Copia del webhook sin el secreto (solo revela si existe)."""
    public = dict(entry)
    public["has_token"] = bool(entry.get("token"))
    public["token"] = ""
    return public


def _json_response(payload: Any, status: int = 200) -> Response:
    response = jsonify(payload)
    response.status_code = status
    return response


def _register_registry(
    app: Flask,
    kind: str,
    provider: Callable[[], Any],
    runner: Optional[Callable[[str], Any]] = None,
    runner_action: str = "run",
) -> None:
    """Registra el CRUD HTTP de un registro de dispatchers.

    Genera el mismo contrato para cualquier fuente (webhooks ya tiene el suyo
    por su ingesta reactiva):

        GET    /automation/<kind>          listar + estadísticas
        POST   /automation/<kind>          crear o actualizar
        GET    /automation/<kind>/<name>   detalle
        PUT    /automation/<kind>/<name>   actualizar
        DELETE /automation/<kind>/<name>   eliminar
        POST   /automation/<kind>/<name>/<runner_action>   disparar/escanear ahora

    Al crear se devuelve una única vez el valor de los campos secretos.
    """
    base = f"/automation/{kind}"

    @app.route(base, methods=["GET", "POST", "OPTIONS"], endpoint=f"{kind}_list")
    def _registry_list_or_create():
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        registry = provider()
        if request.method == "GET":
            return _json_response({"success": True, **registry.status()})
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        if not name:
            return _json_response({"success": False, "error": "falta 'name'"}, 400)
        existed = registry.get(str(name)) is not None
        try:
            entry = registry.upsert(str(name), data)
        except ValueError as exc:
            return _json_response({"success": False, "error": str(exc)}, 400)
        payload: Dict[str, Any] = {"success": True, "item": registry._public(entry)}
        if not existed:
            for field in registry.secret_fields:
                if entry.get(field):
                    payload[field] = entry[field]
        return _json_response(payload)

    @app.route(
        f"{base}/<name>",
        methods=["GET", "PUT", "DELETE", "OPTIONS"],
        endpoint=f"{kind}_detail",
    )
    def _registry_detail(name: str):
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        registry = provider()
        if request.method == "GET":
            entry = registry.get(name)
            if entry is None:
                return _json_response({"success": False, "error": "no encontrado"}, 404)
            return _json_response({"success": True, "item": entry})
        if request.method == "DELETE":
            if not registry.delete(name):
                return _json_response({"success": False, "error": "no encontrado"}, 404)
            return _json_response({"success": True, "deleted": name})
        data = request.get_json(silent=True) or {}
        if registry.get(name) is None:
            return _json_response({"success": False, "error": "no encontrado"}, 404)
        try:
            entry = registry.upsert(name, data)
        except ValueError as exc:
            return _json_response({"success": False, "error": str(exc)}, 400)
        return _json_response({"success": True, "item": registry._public(entry)})

    if runner is not None:

        @app.route(
            f"{base}/<name>/{runner_action}",
            methods=["POST", "OPTIONS"],
            endpoint=f"{kind}_{runner_action}",
        )
        def _registry_run(name: str):
            """Fuerza un disparo/escaneo de una entrada, sin esperar al reloj."""
            if request.method == "OPTIONS":
                return ("", 204)
            denied = _guard()
            if denied:
                return denied
            try:
                return _json_response({"success": True, **runner(name)})
            except KeyError as exc:
                return _json_response({"success": False, "error": str(exc)}, 404)
            except ValueError as exc:
                return _json_response({"success": False, "error": str(exc)}, 400)
            except Exception as exc:
                return _json_response({"success": False, "error": str(exc)}, 500)


def _guard() -> Optional[Tuple[Response, int]]:
    """Auth opt-in + rate limit. Devuelve una respuesta de error, o `None`."""
    allowed, reason = _check_auth(request.headers)
    if not allowed:
        return _json_response({"success": False, "error": reason}, 401)
    client = request.remote_addr or "unknown"
    ok, retry_after = _rate_limiter.allow(client)
    if not ok:
        response = _json_response({"success": False, "error": "demasiadas peticiones"}, 429)
        response.headers["Retry-After"] = str(int(retry_after) + 1)
        return response
    return None


def create_app() -> Flask:
    """Construye la app Flask del servicio."""
    app = Flask("automation_service")
    app.url_map.strict_slashes = False

    @app.after_request
    def _apply_cors(response: Response) -> Response:
        for key, value in cors_headers().items():
            response.headers.setdefault(key, value)
        return response

    # ---------------------------------------------------------------- salud
    @app.route("/health", methods=["GET", "OPTIONS"])
    def health():
        if request.method == "OPTIONS":
            return ("", 204)
        return _json_response({"success": True, "service": "automation", "status": "up"})

    # --------------------------------------------------------------- estado
    @app.route("/automation/status", methods=["GET", "OPTIONS"])
    def automation_status():
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            payload: Dict[str, Any] = {"success": True, "dispatcher": get_dispatcher().status()}
            for key, provider in (
                ("imap", get_imap_listener),
                ("webhooks", get_webhook_dispatcher),
                ("filewatches", get_filewatch_dispatcher),
                ("crons", get_cron_dispatcher),
            ):
                try:
                    payload[key] = provider().status()
                except Exception as exc:
                    payload[key] = {"error": str(exc)}
            return _json_response(payload)
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)

    # --------------------------------------------------------------- tareas
    @app.route("/automation/tasks", methods=["GET", "POST", "OPTIONS"])
    def automation_tasks():
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        queue = get_dispatcher().queue
        try:
            if request.method == "POST":
                data = request.get_json(silent=True) or {}
                wake_prompt = str(data.get("wake_prompt") or "").strip()
                payload = data.get("payload")
                if data.get("requires_approval") is not None:
                    if not isinstance(payload, dict):
                        payload = {"event": payload} if payload not in (None, "") else {}
                    payload.setdefault("requires_approval", bool(data["requires_approval"]))
                try:
                    priority = int(data.get("priority") or 0)
                except (TypeError, ValueError):
                    priority = 0
                task_id = queue.enqueue(
                    source=str(data.get("source") or "manual"),
                    source_ref=data.get("source_ref"),
                    payload=payload,
                    wake_prompt=wake_prompt or None,
                    agent_template=data.get("agent_template"),
                    priority=priority,
                )
                return _json_response(
                    {"success": True, "task_id": task_id, "task": queue.get_task(task_id)}
                )

            try:
                limit = int(request.args.get("limit", 100))
                offset = int(request.args.get("offset", 0))
            except (TypeError, ValueError):
                limit, offset = 100, 0
            tasks = queue.list_tasks(
                status=request.args.get("status"),
                source=request.args.get("source"),
                limit=limit,
                offset=offset,
            )
            return _json_response({"success": True, "tasks": tasks, "stats": queue.stats()})
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)

    @app.route("/automation/tasks/<int:task_id>", methods=["GET", "OPTIONS"])
    def automation_task_detail(task_id: int):
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        task = get_dispatcher().queue.get_task(int(task_id))
        if task is None:
            return _json_response({"success": False, "error": "Tarea no encontrada"}, 404)
        return _json_response({"success": True, "task": task})

    # ------------------------------------------------------- aprobaciones
    @app.route("/automation/approve/<token>", methods=["POST", "OPTIONS"])
    def automation_approve(token: str):
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            task_id = get_dispatcher().approve(token)
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)
        if task_id is None:
            return _json_response({"success": False, "error": "Token no válido o ya resuelto"}, 404)
        return _json_response({"success": True, "task_id": task_id, "status": "approved"})

    @app.route("/automation/reject/<token>", methods=["POST", "OPTIONS"])
    def automation_reject(token: str):
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        reason = str(data.get("reason") or "rechazada por el usuario")
        try:
            task_id = get_dispatcher().reject(token, reason)
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)
        if task_id is None:
            return _json_response({"success": False, "error": "Token no válido o ya resuelto"}, 404)
        return _json_response({"success": True, "task_id": task_id, "status": "rejected"})

    # ------------------------------------------------------- reparto/reintento
    @app.route("/automation/dispatch", methods=["POST", "OPTIONS"])
    def automation_dispatch():
        """Ejecuta un ciclo de reparto (una tarea) de forma inmediata."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            outcome = get_dispatcher().dispatch_once()
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)
        return _json_response({"success": True, "task": outcome})

    @app.route("/automation/retry/<int:task_id>", methods=["POST", "OPTIONS"])
    def automation_retry(task_id: int):
        """Devuelve a la cola una tarea fallida o cancelada (reintento manual)."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        data = request.get_json(silent=True) or {}
        try:
            delay = max(0.0, float(data.get("delay_seconds") or 0.0))
        except (TypeError, ValueError):
            delay = 0.0
        dispatcher = get_dispatcher()
        try:
            ok = dispatcher.retry_task(int(task_id), delay_seconds=delay)
        except KeyError as exc:
            return _json_response({"success": False, "error": str(exc)}, 404)
        except ValueError as exc:
            return _json_response({"success": False, "error": str(exc)}, 409)
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)
        return _json_response(
            {
                "success": bool(ok),
                "task_id": int(task_id),
                "status": "pending" if ok else "sin_cambios",
                "task": dispatcher.queue.get_task(int(task_id)),
            }
        )

    # -------------------------------------------------------------- auditoría
    @app.route("/automation/audit", methods=["GET", "OPTIONS"])
    def automation_audit():
        """Historial de acciones sensibles (encolar, aprobar, fallar, reintentar)."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        task_id = request.args.get("task_id")
        try:
            task_id = int(task_id) if task_id not in (None, "") else None
        except (TypeError, ValueError):
            task_id = None
        queue = get_dispatcher().queue
        try:
            entries = queue.list_audit(limit=limit, task_id=task_id)
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)
        for entry in entries:
            detail = entry.get("detail")
            if isinstance(detail, str):
                try:
                    entry["detail"] = json.loads(detail)
                except (TypeError, ValueError):
                    pass
        return _json_response({"success": True, "entries": entries})

    # ------------------------------------------------------------- WEBHOOKS
    @app.route("/automation/webhooks", methods=["GET", "POST", "OPTIONS"])
    def automation_webhooks():
        """Registro de webhooks: listar y crear/actualizar (con secretos ocultos)."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        registry = get_webhook_dispatcher()
        if request.method == "GET":
            return _json_response({"success": True, **registry.status()})
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        if not name:
            return _json_response({"success": False, "error": "falta 'name'"}, 400)
        existed = registry.get(name) is not None
        try:
            entry = registry.upsert(str(name), data)
        except ValueError as exc:
            return _json_response({"success": False, "error": str(exc)}, 400)
        payload = {
            "success": True,
            "webhook": _public_webhook(entry),
            "url": f"/webhook/{entry['name']}",
        }
        # El secreto se entrega una única vez: al crear el webhook. Después solo
        # se puede recuperar rotándolo (`POST /automation/webhooks/<name>/token`).
        if not existed:
            payload["token"] = entry.get("token") or ""
        return _json_response(payload)

    @app.route("/automation/webhooks/<name>", methods=["GET", "PUT", "DELETE", "OPTIONS"])
    def automation_webhook_detail(name: str):
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        registry = get_webhook_dispatcher()
        if request.method == "GET":
            entry = registry.get(name)
            if entry is None:
                return _json_response({"success": False, "error": "webhook no encontrado"}, 404)
            return _json_response({"success": True, "webhook": entry})
        if request.method == "DELETE":
            if not registry.delete(name):
                return _json_response({"success": False, "error": "webhook no encontrado"}, 404)
            return _json_response({"success": True, "deleted": name})
        data = request.get_json(silent=True) or {}
        try:
            entry = registry.upsert(name, data)
        except ValueError as exc:
            return _json_response({"success": False, "error": str(exc)}, 400)
        return _json_response({"success": True, "webhook": _public_webhook(entry)})

    @app.route("/automation/webhooks/<name>/token", methods=["POST", "OPTIONS"])
    def automation_webhook_rotate(name: str):
        """Rota el token de un webhook y lo devuelve una única vez."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        registry = get_webhook_dispatcher()
        if registry.get(name) is None:
            return _json_response({"success": False, "error": "webhook no encontrado"}, 404)
        entry = registry.rotate_token(name)
        return _json_response({"success": True, "name": entry["name"], "token": entry["token"]})

    @app.route("/webhook/<name>", methods=["POST", "OPTIONS"])
    @app.route("/automation/webhook/<name>", methods=["POST", "OPTIONS"])
    def webhook_ingest(name: str):
        """Ingesta pública: convierte la petición externa en una tarea de la cola.

        No usa el token global (`LLAMA_WEBUI_TOKEN`): un sistema externo no puede
        conocerlo. Cada webhook valida su propio token.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        body = request.get_json(silent=True)
        if body is None:
            body = request.get_data(as_text=True)
        try:
            outcome = get_webhook_dispatcher().handle(
                name,
                body=body,
                headers=dict(request.headers),
                query=dict(request.args),
                remote_addr=request.remote_addr,
            )
        except Exception as exc:
            status_code = int(getattr(exc, "status_code", 500))
            if status_code >= 500:
                logger.warning(f"Ingesta de webhook '{name}' falló: {exc}")
            return _json_response({"success": False, "error": str(exc)}, status_code)
        return _json_response(outcome, 202)

    # ------------------------------------------------- PANEL DE DISPATCHERS
    @app.route("/automation/dispatchers", methods=["GET", "OPTIONS"])
    def automation_dispatchers():
        """Ficha única de los cuatro dispatchers: estado, contadores y pausa."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            items = [
                _dispatcher_panel_entry(kind, provider) for kind, provider in dispatcher_providers()
            ]
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)
        return _json_response({"success": True, "dispatchers": items})

    @app.route("/automation/dispatchers/<kind>/<action>", methods=["POST", "OPTIONS"])
    def automation_dispatcher_control(kind: str, action: str):
        """Pausa o reanuda un dispatcher (el estado sobrevive a un reinicio)."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        if action not in ("pause", "resume"):
            return _json_response({"success": False, "error": f"acción inválida: {action}"}, 404)
        data = request.get_json(silent=True) or {}
        control = get_dispatcher_control()
        try:
            if action == "pause":
                state = control.pause(kind, str(data.get("reason") or ""))
            else:
                state = control.resume(kind)
        except ValueError as exc:
            return _json_response({"success": False, "error": str(exc)}, 400)
        provider = dict(dispatcher_providers()).get(state["kind"])
        entry = _dispatcher_panel_entry(state["kind"], provider) if provider else state
        return _json_response({"success": True, "dispatcher": entry})

    @app.route("/automation/dispatchers/history", methods=["GET", "OPTIONS"])
    def automation_dispatcher_history():
        """Historial unificado de eventos generados por los dispatchers."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        kind = request.args.get("kind")
        prefix: Optional[str] = None
        if kind:
            # El historial es de las **fuentes**; el ejecutor no genera eventos.
            from .dispatcher_control import SOURCE_KINDS

            slug = str(kind).strip().lower()
            if slug not in SOURCE_KINDS:
                return _json_response(
                    {"success": False, "error": f"dispatcher desconocido: {kind!r}"}, 400
                )
            prefix = slug
        queue = get_dispatcher().queue
        try:
            entries = queue.list_audit(limit=limit, actor_prefix=prefix)
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)
        for entry in entries:
            detail = entry.get("detail")
            if isinstance(detail, str):
                try:
                    entry["detail"] = json.loads(detail)
                except (TypeError, ValueError):
                    pass
            entry["kind"] = str(entry.get("actor") or "").split(":", 1)[0]
        return _json_response({"success": True, "kind": prefix, "entries": entries})

    # ------------------------------------------------------ EJECUTOR
    @app.route("/automation/executor", methods=["GET", "OPTIONS"])
    def automation_executor():
        """Estado del ejecutor: pausa, workers y **tareas represadas**."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            return _json_response({"success": True, "executor": get_dispatcher().status()})
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)

    @app.route("/automation/executor/<action>", methods=["POST", "OPTIONS"])
    def automation_executor_control(action: str):
        """Pausa o reanuda el ejecutor de la cola.

        Pausar no mata la tarea en curso: los workers terminan lo que tienen
        entre manos y dejan de reclamar. Al reanudar se les despierta de
        inmediato, así que el backlog sale al instante en vez de esperar al
        siguiente sondeo.
        """
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        if action not in ("pause", "resume"):
            return _json_response({"success": False, "error": f"acción inválida: {action}"}, 404)
        from .dispatcher_control import EXECUTOR_KIND

        dispatcher = get_dispatcher()
        control = get_dispatcher_control()
        data = request.get_json(silent=True) or {}
        try:
            if action == "pause":
                control.pause(EXECUTOR_KIND, str(data.get("reason") or ""))
            else:
                control.resume(EXECUTOR_KIND)
                dispatcher.wake()
        except ValueError as exc:
            return _json_response({"success": False, "error": str(exc)}, 400)
        return _json_response({"success": True, "executor": dispatcher.status()})

    # ------------------------------------------------- FILEWATCH Y CRON
    def _filewatch_scan(name: str) -> Dict[str, Any]:
        ids = get_filewatch_dispatcher().scan_now(name)
        return {"filewatch": name, "enqueued": len(ids), "task_ids": ids}

    def _cron_run(name: str) -> Dict[str, Any]:
        return get_cron_dispatcher().run_now(name)

    _register_registry(
        app,
        "filewatches",
        get_filewatch_dispatcher,
        runner=_filewatch_scan,
        runner_action="scan",
    )
    _register_registry(app, "crons", get_cron_dispatcher, runner=_cron_run, runner_action="run")

    # ------------------------------------------------------------------ IMAP
    @app.route("/automation/imap/status", methods=["GET", "OPTIONS"])
    def automation_imap_status():
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            return _json_response(get_imap_listener().status())
        except Exception as exc:
            return _json_response({"error": str(exc)}, 500)

    @app.route("/automation/imap/poll", methods=["POST", "OPTIONS"])
    def automation_imap_poll():
        """Fuerza un sondeo del buzón ahora mismo."""
        if request.method == "OPTIONS":
            return ("", 204)
        denied = _guard()
        if denied:
            return denied
        try:
            result = get_imap_listener().poll_now()
        except Exception as exc:
            return _json_response({"success": False, "error": str(exc)}, 500)
        if result is None:
            return _json_response(
                {"success": False, "error": "Sondeo fallido (revisa la configuración IMAP)"}, 502
            )
        return _json_response({"success": True, **result})

    return app


# ----------------------------------------------------------------------------
# Ciclo de vida del proceso
# ----------------------------------------------------------------------------
class AutomationService:
    """Arranca el dispatcher + listener y sirve la API HTTP."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, with_imap: bool = True):
        self.host = host
        self.port = int(port)
        self.with_imap = bool(with_imap)
        self._stopped = False
        self._started = False

    def start_backends(self) -> None:
        from .automation_dispatcher import start_dispatcher_service

        start_dispatcher_service()
        self._started = True
        logger.info("Dispatcher 24/7 iniciado")
        # Dispatchers enchufables: cada uno con su registro y su bucle.
        for name, starter in (
            ("FileWatch", start_filewatch_dispatcher),
            ("Cron", start_cron_dispatcher),
        ):
            try:
                starter()
                logger.info(f"Dispatcher {name} iniciado")
            except Exception as exc:  # nunca debe impedir arrancar el servicio
                logger.warning(f"Dispatcher {name} no iniciado: {exc}")
        if self.with_imap:
            try:
                get_imap_listener().start()
                logger.info("Listener IMAP iniciado")
            except Exception as exc:
                logger.warning(f"Listener IMAP no iniciado: {exc}")

    def stop_backends(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            from .automation_dispatcher import stop_dispatcher_service

            stop_dispatcher_service()
        except Exception as exc:  # pragma: no cover - mejor esfuerzo
            logger.debug(f"No se pudo detener el dispatcher: {exc}")
        for name, stopper in (
            ("FileWatch", stop_filewatch_dispatcher),
            ("Cron", stop_cron_dispatcher),
        ):
            try:
                stopper()
            except Exception as exc:  # pragma: no cover
                logger.debug(f"No se pudo detener {name}: {exc}")
        if self.with_imap:
            try:
                get_imap_listener().stop()
            except Exception as exc:  # pragma: no cover
                logger.debug(f"No se pudo detener el listener IMAP: {exc}")
        logger.info("Servicio de automatización detenido")

    def run(self) -> None:
        self.start_backends()
        app = create_app()
        logger.info(f"API del servicio en http://{self.host}:{self.port}")
        app.run(host=self.host, port=self.port, threaded=True, use_reloader=False)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Servicio de automatización 24/7 independiente")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="Workspace que vigila el agente")
    parser.add_argument("--no-imap", action="store_true", help="No arrancar el listener IMAP")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    global DEFAULT_WORKSPACE
    if args.workspace:
        DEFAULT_WORKSPACE = args.workspace

    service = AutomationService(host=args.host, port=args.port, with_imap=not args.no_imap)

    def _handle_signal(signum, _frame):
        logger.info(f"Señal {signum} recibida; cerrando el servicio…")
        service.stop_backends()
        raise SystemExit(0)

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _handle_signal)
        except (AttributeError, ValueError):  # pragma: no cover - plataformas raras
            pass

    try:
        service.run()
    finally:
        service.stop_backends()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
