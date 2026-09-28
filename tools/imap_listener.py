"""Listener IMAP de la automatización 24/7 (Bloque 7).

Sondea un buzón por correo entrante y encola cada mensaje no leído en la cola
persistente (`tools/event_queue.py`), de donde lo recoge el dispatcher.

Decisiones de diseño:

* Se usa `aioimaplib` (asíncrono) dentro de un hilo propio con su propio event
  loop, igual que hace `telegram_service`. El hilo del dispatcher nunca se
  bloquea con la red.
* La **deduplicación es por UID**: cada mensaje se encola una sola vez con
  `source_ref = "<carpeta>:<uid>"`. Tras encolarlo se marca como leído
  (`\\Seen`) por defecto, así el siguiente sondeo no lo vuelve a ver.
* La conexión se abre y se cierra en cada ciclo (sondeo), lo que evita sesiones
  colgadas durante horas. `IMAP IDLE` queda como mejora futura.
* Toda la lógica de red es inyectable (`_create_client`) para poder testear el
  parseo y el encolado sin tocar un servidor real.

Configuración (en `user_data/config.json`, sección `defaults`):

    imap_enabled, imap_host, imap_port, imap_user, imap_password,
    imap_use_ssl, imap_folder, imap_search, imap_poll_interval,
    imap_mark_seen, imap_max_per_poll, imap_wake_prompt, imap_agent_template

Si no hay `imap_host`/`imap_user`/`imap_password` el listener queda inactivo y lo
dice en el log; arrancarlo nunca revienta el proceso.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import logging
import threading
import time
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

from .event_queue import EventQueue, get_event_queue

logger = logging.getLogger("imap_listener")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [IMAP] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

DEFAULT_FOLDER = "INBOX"
DEFAULT_SEARCH = "UNSEEN"
DEFAULT_POLL_INTERVAL = 60.0
DEFAULT_MAX_PER_POLL = 10
MAX_BODY_CHARS = 4000
SOURCE_NAME = "imap"


# ============================================================================
# Configuración
# ============================================================================

@dataclass
class ImapConfig:
    host: str = ""
    port: int = 993
    user: str = ""
    password: str = ""
    use_ssl: bool = True
    folder: str = DEFAULT_FOLDER
    search: str = DEFAULT_SEARCH
    poll_interval: float = DEFAULT_POLL_INTERVAL
    mark_seen: bool = True
    max_per_poll: int = DEFAULT_MAX_PER_POLL
    wake_prompt: str = ""
    agent_template: str = ""
    enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)


def load_imap_config(defaults: Optional[Dict[str, Any]] = None) -> ImapConfig:
    """Lee la configuración IMAP de `user_data/config.json` (sección defaults)."""
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

    return ImapConfig(
        host=str(_get("imap_host", "")),
        port=int(_get("imap_port", 993)),
        user=str(_get("imap_user", "")),
        password=str(_get("imap_password", "")),
        use_ssl=_bool("imap_use_ssl", True),
        folder=str(_get("imap_folder", DEFAULT_FOLDER)),
        search=str(_get("imap_search", DEFAULT_SEARCH)),
        poll_interval=max(5.0, float(_get("imap_poll_interval", DEFAULT_POLL_INTERVAL))),
        mark_seen=_bool("imap_mark_seen", True),
        max_per_poll=max(1, int(_get("imap_max_per_poll", DEFAULT_MAX_PER_POLL))),
        wake_prompt=str(_get("imap_wake_prompt", "")),
        agent_template=str(_get("imap_agent_template", "")),
        enabled=_bool("imap_enabled", False),
    )


def _settings_defaults() -> Dict[str, Any]:
    import json

    try:
        config_path = Path(runtime.get_user_data_dir()) / "config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    defaults = data.get("defaults")
    return defaults if isinstance(defaults, dict) else data


# ============================================================================
# Parseo de correos
# ============================================================================

def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _extract_body(message) -> str:
    """Devuelve el primer cuerpo de texto disponible (plano o HTML)."""
    if message.is_multipart():
        # Primero texto plano; si no hay, HTML.
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return str(part.get_content())
                except Exception:
                    continue
        for part in message.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                try:
                    return str(part.get_content())
                except Exception:
                    continue
        return ""
    try:
        return str(message.get_content())
    except Exception:
        payload = message.get_payload(decode=True) or b""
        charset = message.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def parse_email(raw: bytes) -> Dict[str, Any]:
    """Convierte un mensaje RFC822 en el payload que consume el dispatcher."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    body = _extract_body(message)
    attachments = [
        part.get_filename()
        for part in (message.walk() if message.is_multipart() else [message])
        if part.get_filename()
    ]
    return {
        "attachments": attachments,
        "body": body[:MAX_BODY_CHARS],
        "date": _decode_header(message.get("Date")),
        "from": _decode_header(message.get("From")),
        "message_id": (message.get("Message-ID") or "").strip(),
        "subject": _decode_header(message.get("Subject")),
        "to": _decode_header(message.get("To")),
    }


def _uids_from_search(lines: Any) -> List[str]:
    """Extrae los UID de la respuesta de `search(..., by_uid=True)`."""
    if not lines:
        return []
    text = ""
    for line in lines:
        if isinstance(line, (bytes, bytearray)):
            text += " " + line.decode("ascii", errors="replace")
        elif line is not None:
            text += " " + str(line)
    return [token for token in text.split() if token.isdigit()]


def _raw_message_from_fetch(lines: Any) -> Optional[bytes]:
    """Localiza el cuerpo RFC822 dentro de la respuesta de `uid fetch`."""
    if lines is None:
        return None
    if isinstance(lines, (bytes, bytearray)):
        return bytes(lines)
    for item in lines:
        if isinstance(item, (bytes, bytearray)):
            data = bytes(item)
            # El literal suele ser el mensaje completo; descarta líneas de
            # control del servidor (p. ej. "1 FETCH (UID 12 RFC822 {").
            if b":" in data and len(data) > 40:
                return data
    # Segundo intento: algunos servidores devuelven tuplas (nombre, datos).
    for item in lines:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


# ============================================================================
# Listener
# ============================================================================

class ImapListener:
    """Sondea IMAP y encola los correos entrantes."""

    def __init__(
        self,
        config: Optional[ImapConfig] = None,
        queue: Optional[EventQueue] = None,
        client_factory: Optional[Callable[[ImapConfig], Awaitable[Any]]] = None,
    ) -> None:
        self.config = config or load_imap_config()
        self.queue = queue or get_event_queue()
        self._client_factory = client_factory
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._enqueued_total = 0
        self._last_poll: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> "ImapListener":
        if self.is_running:
            return self
        if not self.config.configured:
            logger.info(
                "📧 Listener IMAP inactivo: faltan imap_host/imap_user/imap_password en la configuración"
            )
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="imap-listener", daemon=True)
        self._thread.start()
        logger.info(
            f"📧 Listener IMAP iniciado ({self.config.user}@{self.config.host}, "
            f"carpeta {self.config.folder}, cada {self.config.poll_interval:.0f}s)"
        )
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("📧 Listener IMAP detenido")

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # pragma: no cover - protección del hilo
            logger.error(f"El listener IMAP terminó con error: {exc}", exc_info=True)

    @property
    def paused(self) -> bool:
        """¿El dispatcher IMAP está en pausa global? (solo afecta al bucle)."""
        try:
            from .dispatcher_control import is_paused
        except ImportError:  # pragma: no cover - ejecución suelta
            from tools.dispatcher_control import is_paused  # type: ignore
        return is_paused(SOURCE_NAME)

    async def _main(self) -> None:
        while not self._stop_event.is_set():
            try:
                # En pausa se sigue durmiendo, pero no se toca el buzón. El
                # sondeo manual (`poll_now`) ignora la pausa a propósito.
                if not self.paused:
                    await self.poll_once()
            except Exception as exc:
                logger.error(f"Error sondeando el buzón: {exc}")
            await self._sleep_cancellable(self.config.poll_interval)

    async def _sleep_cancellable(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(0.5, max(0.01, remaining)))

    def poll_now(self) -> Optional[Dict[str, Any]]:
        """Ejecuta un ciclo de sondeo de forma síncrona (manual/tests)."""
        try:
            return asyncio.run(self.poll_once())
        except Exception as exc:
            logger.error(f"Error en el sondeo manual: {exc}")
            return None

    # ------------------------------------------------------------------
    # Sondeo
    # ------------------------------------------------------------------
    async def _create_client(self) -> Any:
        """Crea y autentica un cliente IMAP. Inyectable en tests."""
        import aioimaplib

        if self._client_factory is not None:
            return await self._client_factory(self.config)

        if self.config.use_ssl:
            client = aioimaplib.IMAP4_SSL(self.config.host, self.config.port)
        else:
            client = aioimaplib.IMAP4(self.config.host, self.config.port)
        await client.wait_hello_from_server()
        response = await client.login(self.config.user, self.config.password)
        if getattr(response, "result", "OK") != "OK":
            raise RuntimeError(f"Login IMAP rechazado: {getattr(response, 'result', '?')}")
        return client

    async def poll_once(self) -> Dict[str, Any]:
        """Un ciclo: busca, encola los nuevos y marca leídos."""
        client = await self._create_client()
        enqueued: List[int] = []
        try:
            await client.select(self.config.folder)
            response = await client.search(self.config.search, by_uid=True)
            uids = _uids_from_search(getattr(response, "lines", response))
            pending = uids[: self.config.max_per_poll]

            for uid in pending:
                fetched = await client.uid("fetch", uid, "(RFC822)")
                raw = _raw_message_from_fetch(getattr(fetched, "lines", fetched))
                if raw is None:
                    logger.debug(f"No se pudo leer el cuerpo del correo {uid}")
                    continue
                info = parse_email(raw)
                source_ref = f"{self.config.folder}:{uid}"
                if not self.queue.exists_by_source_ref(SOURCE_NAME, source_ref):
                    task_id = self.queue.enqueue(
                        source=SOURCE_NAME,
                        source_ref=source_ref,
                        payload=info,
                        wake_prompt=self._wake_prompt(info),
                        agent_template=self.config.agent_template or None,
                    )
                    enqueued.append(task_id)
                # Se marca leído tanto si se encoló como si ya estaba en cola:
                # así el siguiente sondeo no vuelve a traer el mismo correo.
                if self.config.mark_seen:
                    try:
                        await client.uid("store", uid, "+FLAGS", "(\\Seen)")
                    except Exception as exc:  # marcar leído no es crítico
                        logger.debug(f"No se pudo marcar como leído el correo {uid}: {exc}")
        finally:
            try:
                await client.logout()
            except Exception:
                pass

        self._enqueued_total += len(enqueued)
        self._last_poll = {
            "at": time.time(),
            "found": len(uids),
            "enqueued": len(enqueued),
            "task_ids": enqueued,
        }
        if enqueued:
            logger.info(f"📧 {len(enqueued)} correo(s) encolado(s): {enqueued}")
        return self._last_poll

    def _wake_prompt(self, info: Dict[str, Any]) -> str:
        if self.config.wake_prompt:
            try:
                return self.config.wake_prompt.format(**info)
            except Exception:
                return self.config.wake_prompt
        subject = info.get("subject") or "(sin asunto)"
        sender = info.get("from") or "(remitente desconocido)"
        return f"Procesa el correo entrante de {sender} — «{subject}»."

    # ------------------------------------------------------------------
    # Introspección
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running,
            "paused": self.paused,
            "configured": self.config.configured,
            "enabled": self.config.enabled,
            "host": self.config.host,
            "user": self.config.user,
            "folder": self.config.folder,
            "search": self.config.search,
            "poll_interval": self.config.poll_interval,
            "mark_seen": self.config.mark_seen,
            "enqueued_total": self._enqueued_total,
            "last_poll": self._last_poll,
        }


# ============================================================================
# Instancia global (una por proceso)
# ============================================================================

_listener: Optional[ImapListener] = None
_listener_lock = threading.Lock()


def get_imap_listener(queue: Optional[EventQueue] = None) -> ImapListener:
    global _listener
    with _listener_lock:
        if _listener is None:
            _listener = ImapListener(queue=queue)
        return _listener


def start_imap_listener() -> Optional[ImapListener]:
    """Arranca el listener una sola vez; no propaga errores de configuración."""
    try:
        return get_imap_listener().start()
    except Exception as exc:
        logger.error(f"No se pudo iniciar el listener IMAP: {exc}", exc_info=True)
        return None


def stop_imap_listener() -> None:
    global _listener
    with _listener_lock:
        listener = _listener
    if listener is not None:
        listener.stop()


def reset_imap_listener() -> None:
    global _listener
    with _listener_lock:
        _listener = None
