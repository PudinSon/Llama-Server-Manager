"""Dispatcher de tipo **webhook** para el servicio de automatización 24/7.

A diferencia del listener IMAP (que sondea en un hilo propio) o de un cron (que
vigila el reloj), un webhook es *reactivo*: cualquier sistema externo —GitHub,
un formulario, otro script— hace `POST /webhook/<nombre>` y este módulo convierte
el cuerpo recibido en una tarea de la cola persistente. A partir de ahí el camino
es el de siempre: cola → worker → Orchestrator → aprobación (si toca) →
notificación.

El registro y el encolado los aporta `tools/dispatcher_registry.py`; aquí solo
vive lo específico de un webhook: el token compartido, la deduplicación por
cabecera de entrega y el formato del prompt.

Registro
--------
La configuración vive en `user_data/automation_webhooks.json`, así que añadir una
fuente no obliga a tocar el servicio ni a reiniciarlo. Cada webhook define:

===================  ==========================================================
`name`               identificador en la URL (slug: `a-z0-9_-`, 1-64)
`enabled`            si acepta peticiones (por defecto `true`)
`token`              secreto compartido; obligatorio salvo `allow_anonymous`
`allow_anonymous`    permitir sin token — solo si el origen es loopback
`source`             etiqueta de la cola (por defecto `webhook:<name>`)
`wake_prompt`        plantilla `{...}` sobre el cuerpo recibido
`agent_template`     instrucciones fijas antepuestas al prompt
`requires_approval`  fuerza el gate humano para esta fuente
`priority`           prioridad en la cola (-1000..1000)
`dedupe`             deduplicar por `source_ref` (por defecto `true`)
===================  ==========================================================

Seguridad
---------
El endpoint de ingesta **no** usa el token global (`LLAMA_WEBUI_TOKEN`): un
sistema externo no puede conocerlo. En su lugar cada webhook valida su propio
token (`X-Webhook-Token`, `Authorization: Bearer`, o `?token=`). Sin token
configurado, solo se aceptan peticiones desde loopback y con
`allow_anonymous: true`; en cualquier otro caso se responde 401.
"""

from __future__ import annotations

import logging
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .dispatcher_registry import (
    MAX_BODY_BYTES,
    BaseRegistry,
    coerce_body,
    flatten,
    is_kind_paused,
    is_loopback as _is_loopback,
    render_template,
)
from .event_queue import EventQueue, get_event_queue

logger = logging.getLogger("webhook_dispatcher")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [WEBHOOK] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

CONFIG_FILENAME = "automation_webhooks.json"

__all__ = [
    "CONFIG_FILENAME",
    "MAX_BODY_BYTES",
    "WebhookDispatcher",
    "WebhookError",
    "get_webhook_dispatcher",
    "reset_webhook_dispatcher",
]


class WebhookError(Exception):
    """Error de ingesta con su código HTTP asociado."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)


# ============================================================================
# Registro
# ============================================================================
class WebhookDispatcher(BaseRegistry):
    """Registro de webhooks + conversión de una petición en tarea de la cola."""

    config_filename = CONFIG_FILENAME
    source_prefix = "webhook"
    label = "Webhook"
    collection_key = "webhooks"
    secret_fields = ("token",)
    default_fields = {
        "enabled": True,
        "token": "",
        "allow_anonymous": False,
        "source": "",
        "wake_prompt": "",
        "agent_template": "",
        "requires_approval": None,
        "priority": 0,
        "dedupe": True,
    }

    # ------------------------------------------------------------------
    # Registro
    # ------------------------------------------------------------------
    def _prepare_create(self, data: Dict[str, Any]) -> None:
        """Un webhook nuevo nace con token salvo que se declare anónimo."""
        anonymous = bool(data.get("allow_anonymous"))
        if not str(data.get("token") or "").strip() and not anonymous:
            data["token"] = secrets.token_urlsafe(24)

    def list_webhooks(self, include_secrets: bool = False):
        """Listado (sin secretos salvo que se pidan explícitamente)."""
        return self.list(include_secrets=include_secrets)

    def rotate_token(self, name: str) -> Dict[str, Any]:
        return self.upsert(name, {"token": secrets.token_urlsafe(24)})

    # ------------------------------------------------------------------
    # Ingesta
    # ------------------------------------------------------------------
    def handle(
        self,
        name: str,
        *,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, str]] = None,
        remote_addr: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Valida la petición y la encola. Lanza `WebhookError` si no procede."""
        slug = str(name).strip().lower()
        webhook = self.get(slug, include_secrets=True)
        if webhook is None:
            self._reject()
            raise WebhookError(f"webhook '{slug}' no registrado", 404)
        if not webhook.get("enabled", True):
            self._reject()
            raise WebhookError(f"webhook '{slug}' deshabilitado", 409)
        if is_kind_paused(self.source_prefix):
            self._reject()
            raise WebhookError(
                "los webhooks están en pausa; reanúdalos en el panel de automatización", 409
            )

        self._check_auth(webhook, headers or {}, query or {}, remote_addr)

        payload, raw_text = coerce_body(body)
        if raw_text and len(raw_text.encode("utf-8")) > MAX_BODY_BYTES:
            self._reject()
            raise WebhookError(f"cuerpo demasiado grande (> {MAX_BODY_BYTES} bytes)", 413)

        event = flatten(payload)
        wake_prompt = self._wake_prompt(webhook, payload, event)
        source_ref = _source_ref(webhook, event, headers or {})

        try:
            task_id = self._enqueue(
                webhook, payload=payload, wake_prompt=wake_prompt, source_ref=source_ref
            )
        except ValueError as exc:
            self._reject()
            raise WebhookError(str(exc), 400) from exc

        outcome = {
            "success": True,
            "webhook": slug,
            "task_id": task_id,
            "source": str(webhook.get("source") or f"webhook:{slug}"),
            "deduplicated": bool(source_ref) and bool(webhook.get("dedupe", True)),
        }
        if self._last_event is not None:
            self._last_event["remote_addr"] = remote_addr
        logger.info(f"🔗 Webhook '{slug}' → tarea {task_id}")
        return outcome

    def _check_auth(
        self,
        webhook: Dict[str, Any],
        headers: Dict[str, str],
        query: Dict[str, str],
        remote_addr: Optional[str],
    ) -> None:
        expected = str(webhook.get("token") or "")
        provided = _extract_token(headers, query)
        if expected:
            if not provided or not secrets.compare_digest(provided, expected):
                self._reject()
                raise WebhookError("token de webhook inválido", 401)
            return
        if not webhook.get("allow_anonymous"):
            self._reject()
            raise WebhookError(
                "este webhook no tiene token y no permite peticiones anónimas", 401
            )
        if not _is_loopback(remote_addr):
            self._reject()
            raise WebhookError(
                "webhook anónimo rechazado: solo se permite desde loopback", 401
            )

    def _wake_prompt(
        self,
        webhook: Dict[str, Any],
        payload: Any,
        event: Dict[str, Any],
    ) -> str:
        template = str(webhook.get("wake_prompt") or "")
        if template:
            return render_template(template, payload, {"webhook": webhook["name"]})
        name = webhook["name"]
        hint = event.get("event") or event.get("action") or event.get("title") or ""
        suffix = f" ({hint})" if hint else ""
        return f"Procesa el evento recibido por el webhook «{name}»{suffix}."


# ============================================================================
# Utilidades
# ============================================================================
def _extract_token(headers: Dict[str, str], query: Dict[str, str]) -> str:
    lowered = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    token = lowered.get("x-webhook-token") or lowered.get("x-api-token") or ""
    if not token:
        authorization = lowered.get("authorization") or ""
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    if not token:
        token = str((query or {}).get("token") or "")
    return token.strip()


def _source_ref(
    webhook: Dict[str, Any],
    event: Dict[str, Any],
    headers: Dict[str, str],
) -> Optional[str]:
    """Clave de deduplicación, para no encolar el mismo evento dos veces."""
    if not webhook.get("dedupe", True):
        return None
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    candidate = (
        lowered.get("x-webhook-id")
        or lowered.get("x-github-delivery")
        or lowered.get("x-request-id")
        or (event.get("id") if isinstance(event.get("id"), (str, int)) else None)
        or (event.get("event_id") if isinstance(event.get("event_id"), (str, int)) else None)
    )
    if candidate in (None, ""):
        return None
    return str(candidate)[:120]


# ============================================================================
# Instancia global (una por proceso)
# ============================================================================
_dispatcher: Optional[WebhookDispatcher] = None
_dispatcher_lock = threading.Lock()


def get_webhook_dispatcher(
    queue: Optional[EventQueue] = None,
    config_path: Optional[Path] = None,
) -> WebhookDispatcher:
    """Devuelve (creando si hace falta) el registro de webhooks del proceso."""
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            _dispatcher = WebhookDispatcher(queue=queue, config_path=config_path)
        return _dispatcher


def reset_webhook_dispatcher() -> None:
    """Olvida la instancia global (tests)."""
    global _dispatcher
    with _dispatcher_lock:
        _dispatcher = None
