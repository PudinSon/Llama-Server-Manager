"""Envío de correo (SMTP) para cerrar el ciclo de la automatización (Bloque 7).

El listener IMAP encola correos entrantes; cuando el dispatcher termina una tarea
de origen `imap`, puede responder al remitente con el resultado usando este
módulo (opt-in con `automation_reply_email`).

Usa `smtplib` de la stdlib: la respuesta por correo no justifica añadir otra
dependencia. `send_email` **nunca lanza**: devuelve `{"success": ...}` para que un
fallo de SMTP no tumbe al worker que lo llama.

Configuración (en `user_data/config.json`, sección `defaults`):

    smtp_host, smtp_port, smtp_user, smtp_password, smtp_use_ssl,
    smtp_use_starttls, smtp_from, automation_reply_email
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

logger = logging.getLogger("imap_sender")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [SMTP] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

DEFAULT_SMTP_PORT = 587
DEFAULT_TIMEOUT = 30.0
MAX_BODY_CHARS = 10000


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = DEFAULT_SMTP_PORT
    user: str = ""
    password: str = ""
    use_ssl: bool = False
    use_starttls: bool = True
    sender: str = ""
    reply_enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.host and (self.sender or self.user))


def _settings_defaults() -> Dict[str, Any]:
    try:
        config_path = Path(runtime.get_user_data_dir()) / "config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    defaults = data.get("defaults")
    return defaults if isinstance(defaults, dict) else data


def load_smtp_config(defaults: Optional[Dict[str, Any]] = None) -> SmtpConfig:
    """Lee la configuración SMTP de `user_data/config.json` (sección defaults)."""
    if defaults is None:
        defaults = _settings_defaults()

    def _get(key: str, fallback: Any) -> Any:
        value = defaults.get(key, fallback)
        return fallback if value in (None, "") else value

    def _bool(key: str, fallback: bool) -> bool:
        value = defaults.get(key)
        if value is None:
            return fallback
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "sí", "si"}
        return bool(value)

    return SmtpConfig(
        host=str(_get("smtp_host", "")),
        port=int(_get("smtp_port", DEFAULT_SMTP_PORT)),
        user=str(_get("smtp_user", "")),
        password=str(_get("smtp_password", "")),
        use_ssl=_bool("smtp_use_ssl", False),
        use_starttls=_bool("smtp_use_starttls", True),
        sender=str(_get("smtp_from", "")),
        reply_enabled=_bool("automation_reply_email", False),
    )


def send_email(
    config: SmtpConfig,
    to: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Envía un correo. Nunca lanza; devuelve `{"success": ...}`."""
    if not config.configured:
        return {"success": False, "error": "SMTP sin configurar (smtp_host/smtp_from)"}
    if not to:
        return {"success": False, "error": "destinatario vacío"}

    message = EmailMessage()
    message["From"] = config.sender or config.user
    message["To"] = str(to)
    message["Subject"] = str(subject or "Automatización")
    if in_reply_to:
        message["In-Reply-To"] = str(in_reply_to)
        message["References"] = str(in_reply_to)
    message.set_content(str(body or "")[:MAX_BODY_CHARS])

    try:
        if config.use_ssl:
            with smtplib.SMTP_SSL(config.host, config.port,
                                  timeout=timeout, context=ssl.create_default_context()) as client:
                if config.user:
                    client.login(config.user, config.password)
                client.send_message(message)
        else:
            with smtplib.SMTP(config.host, config.port, timeout=timeout) as client:
                if config.use_starttls:
                    client.starttls(context=ssl.create_default_context())
                if config.user:
                    client.login(config.user, config.password)
                client.send_message(message)
    except Exception as exc:
        logger.warning(f"📧 No se pudo enviar el correo a {to}: {exc}")
        return {"success": False, "error": str(exc)}
    logger.info(f"📧 Respuesta enviada a {to}")
    return {"success": True, "to": str(to)}


def reply_to_task(
    task: Dict[str, Any],
    body: str,
    config: Optional[SmtpConfig] = None,
    defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Responde al remitente de una tarea de origen `imap`.

    Solo actúa si `automation_reply_email` está activo y el payload trae un
    remitente; en cualquier otro caso devuelve `{"success": False, "skipped": ...}`.
    """
    config = config or load_smtp_config(defaults)
    if not config.reply_enabled:
        return {"success": False, "skipped": "reply_email desactivado"}
    payload = task.get("payload")
    if not isinstance(payload, dict):
        return {"success": False, "skipped": "payload sin remitente"}
    to = str(payload.get("from") or "").strip()
    if not to:
        return {"success": False, "skipped": "payload sin remitente"}

    subject = str(payload.get("subject") or "").strip() or "Automatización"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return send_email(
        config,
        to=to,
        subject=subject,
        body=body,
        in_reply_to=payload.get("message_id") or None,
    )


def main(argv: Optional[list] = None) -> int:  # pragma: no cover - utilidad CLI
    import argparse

    parser = argparse.ArgumentParser(description="Envío SMTP de la automatización")
    parser.add_argument("--check", action="store_true", help="Muestra la configuración detectada")
    parser.add_argument("--to", help="Destinatario de un correo de prueba")
    args = parser.parse_args(argv)

    config = load_smtp_config()
    info = {
        "configured": config.configured,
        "host": config.host,
        "port": config.port,
        "use_ssl": config.use_ssl,
        "use_starttls": config.use_starttls,
        "sender": config.sender,
        "reply_enabled": config.reply_enabled,
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if args.to:
        result = send_email(config, args.to, "Prueba de automatización", "Correo de prueba.")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("success") else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
