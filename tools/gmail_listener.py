"""Listener de Gmail mediante Gmail API y OAuth 2.0."""

from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Any, Dict, Optional

import requests

try:
    from . import runtime
    from .event_queue import EventQueue, get_event_queue
    from .google_mail import access_token, public_config
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore
    from tools.event_queue import EventQueue, get_event_queue  # type: ignore
    from tools.google_mail import access_token, public_config  # type: ignore

logger = logging.getLogger("gmail_listener")
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_BODY_CHARS = 4000


def _decode_body(value: str) -> str:
    try:
        raw = value.encode("ascii")
        return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return ""


def _message_content(payload: Dict[str, Any]) -> tuple[str, List[str]]:
    plain: List[str] = []
    html: List[str] = []
    attachments: List[str] = []
    pending = [payload]
    while pending:
        part = pending.pop()
        filename = part.get("filename")
        if filename:
            attachments.append(str(filename))
        mime_type = str(part.get("mimeType") or "")
        body_data = (part.get("body") or {}).get("data")
        if body_data and mime_type in ("text/plain", "text/html"):
            (plain if mime_type == "text/plain" else html).append(_decode_body(str(body_data)))
        pending.extend(part.get("parts") or [])
    return "\n".join(plain or html)[:MAX_BODY_CHARS], attachments


class GmailListener:
    def __init__(self, queue: Optional[EventQueue] = None, request=None) -> None:
        self.queue = queue or get_event_queue()
        self._request = request or requests.request
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_poll: Optional[Dict[str, Any]] = None
        self._last_error = ""
        self._enqueued_total = 0

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> "GmailListener":
        config = public_config()
        if self.is_running or not config["enabled"] or not config["connected"]:
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="gmail-listener", daemon=True)
        self._thread.start()
        logger.info("Gmail listener iniciado para %s", config["address"])
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            config = public_config()
            if config["enabled"]:
                try:
                    self.poll_once()
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.warning("Error sondeando Gmail: %s", exc)
            self._stop_event.wait(max(5, int(config["poll_interval"])))

    def _api(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        token = access_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        response = self._request(
            method,
            f"{GMAIL_API}/{path.lstrip('/')}",
            headers=headers,
            timeout=20,
            **kwargs,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def poll_once(self) -> Dict[str, Any]:
        config = public_config()
        if not config["connected"]:
            raise ValueError("Vincula una cuenta Google antes de sondear Gmail")
        listing = self._api(
            "GET",
            "messages",
            params={"q": config["search"], "maxResults": config["max_per_poll"]},
        )
        messages = listing.get("messages") or []
        enqueued: List[int] = []
        for item in messages[: config["max_per_poll"]]:
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            source_ref = f"gmail:{message_id}"
            message = self._api("GET", f"messages/{message_id}", params={"format": "full"})
            payload = message.get("payload") or {}
            headers = {
                str(header.get("name", "")).casefold(): str(header.get("value", ""))
                for header in payload.get("headers") or []
            }
            body, attachments = _message_content(payload)
            info = {
                "attachments": attachments,
                "body": body,
                "date": headers.get("date", ""),
                "from": headers.get("from", ""),
                "message_id": headers.get("message-id", message_id),
                "subject": headers.get("subject", ""),
                "to": headers.get("to", ""),
            }
            if not self.queue.exists_by_source_ref("gmail", source_ref):
                template = config["wake_prompt"]
                try:
                    wake_prompt = template.format(**info) if template else (
                        f"Procesa el correo entrante de {info['from'] or '(remitente desconocido)'}: "
                        f"{info['subject'] or '(sin asunto)'}"
                    )
                except (KeyError, ValueError):
                    wake_prompt = template
                task_id = self.queue.enqueue(
                    source="gmail",
                    source_ref=source_ref,
                    payload=info,
                    wake_prompt=wake_prompt,
                    agent_template=config["agent_template"] or None,
                )
                enqueued.append(task_id)
            if config["mark_seen"]:
                self._api(
                    "POST",
                    f"messages/{message_id}/modify",
                    json={"removeLabelIds": ["UNREAD"]},
                )

        self._enqueued_total += len(enqueued)
        self._last_error = ""
        self._last_poll = {
            "at": time.time(),
            "found": len(messages),
            "enqueued": len(enqueued),
            "task_ids": enqueued,
        }
        return self._last_poll

    def test_connection(self) -> Dict[str, Any]:
        try:
            profile = self._api("GET", "profile")
            return {"success": True, "email": profile.get("emailAddress", "")}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def status(self) -> Dict[str, Any]:
        config = public_config()
        return {
            **config,
            "running": self.is_running,
            "last_poll": self._last_poll,
            "last_error": self._last_error,
            "enqueued_total": self._enqueued_total,
        }


_listener: Optional[GmailListener] = None
_listener_lock = threading.Lock()


def get_gmail_listener(queue: Optional[EventQueue] = None) -> GmailListener:
    global _listener
    with _listener_lock:
        if _listener is None:
            _listener = GmailListener(queue=queue)
        return _listener