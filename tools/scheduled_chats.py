# tools/scheduled_chats.py
"""Transcripciones de ejecuciones de tareas programadas.

El historial de chat de la WebUI vive en IndexedDB (cliente), así que este
proceso no puede escribir conversaciones directamente. Cada ejecución de una
tarea programada se guarda aquí en el mismo formato que la interfaz ya sabe
importar (`{conv, messages}`) y se expone por HTTP, de forma que la WebUI la
incorpora al historial nativo como una conversación más.

Los ids son deterministas (`scheduled-<task>-<fecha-hora>`), así que reimportar
el mismo fichero es idempotente: `DatabaseService.importConversations` salta las
conversaciones cuyo id ya existe.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("scheduled_chats")

STORAGE_SUBDIR = "chats"
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500

# Límites defensivos: el resultado de un agente puede ser enorme y esto acaba
# en IndexedDB del navegador.
MAX_RESULT_CHARS = 20000
MAX_NAME_CHARS = 52


def _slug(value: str, fallback: str = "tarea") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return (slug or fallback)[:60]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n… (salida recortada)"


def parse_local_datetime(value: Any) -> Optional[datetime]:
    """Interpreta un ISO-8601 como hora local *naive*.

    La UI envía `when`/`next_execution` en UTC (`new Date(...).toISOString()` →
    "...Z"), mientras que el resto del backend compara contra
    `datetime.now()` (naive). Sin normalizar, la comparación revienta con
    «can't compare offset-naive and offset-aware datetimes» y la tarea nunca se
    ejecuta (el daemon la marcaba incluso como fallida).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw[-1] in ("Z", "z"):
        # Compatible también con versiones de Python que no aceptan la «Z».
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_iso(value: Any) -> Optional[datetime]:
    return parse_local_datetime(value)


class ScheduledChatStore:
    """Persiste en disco las conversaciones generadas por el scheduler."""

    def __init__(self, workspace_path: Union[str, Path]) -> None:
        self.workspace_path = Path(workspace_path).resolve()

    @property
    def chats_dir(self) -> Path:
        return self.workspace_path / ".daemon" / STORAGE_SUBDIR

    # ------------------------------------------------------------------
    # Identidad
    # ------------------------------------------------------------------
    def chat_id_for(self, task: Dict[str, Any], when: datetime) -> str:
        """Id determinista: una ejecución concreta siempre produce el mismo id."""
        return f"scheduled-{_slug(task.get('id'), 'task')}-{when.strftime('%Y%m%d-%H%M%S')}"

    def _path_for(self, chat_id: str) -> Path:
        return self.chats_dir / f"{_slug(chat_id)}.json"

    def chat_name_for(self, task: Dict[str, Any], when: datetime) -> str:
        description = str(task.get("description") or "Tarea programada").strip()
        description = description.replace("\n", " ")
        if len(description) > MAX_NAME_CHARS:
            description = description[: MAX_NAME_CHARS - 1].rstrip() + "…"
        return f"⏰ {description} · {when.strftime('%d/%m %H:%M')}"

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------
    def record(
        self,
        task: Dict[str, Any],
        result: str,
        *,
        status: str = "completed",
        error: Optional[str] = None,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Guarda la ejecución como conversación importable por la WebUI."""
        started_at = started_at or datetime.now()
        finished_at = finished_at or datetime.now()
        chat_id = self.chat_id_for(task, started_at)
        target = self._path_for(chat_id)
        # Dos ejecuciones del mismo segundo (por ejemplo un "ejecutar ahora"
        # inmediatamente después de la programada) no deben pisarse.
        suffix = 1
        while target.exists() and suffix < 100:
            suffix += 1
            chat_id = f"{self.chat_id_for(task, started_at)}-{suffix}"
            target = self._path_for(chat_id)

        description = str(task.get("description") or "Tarea programada")
        body = _truncate(str(result or "").strip(), MAX_RESULT_CHARS) or "(sin salida)"
        if status != "completed":
            prefix = "❌ La tarea no se completó" + (f": {error}" if error else "") + "\n\n"
            body = prefix + body

        base_ts = int(started_at.timestamp() * 1000)
        root_id = f"{chat_id}-root"
        user_id = f"{chat_id}-user"
        assistant_id = f"{chat_id}-assistant"
        messages: List[Dict[str, Any]] = [
            {
                "children": [user_id],
                "content": "",
                "convId": chat_id,
                "id": root_id,
                "parent": None,
                "role": "system",
                "timestamp": base_ts,
                "toolCalls": "",
                "type": "root",
            },
            {
                "children": [assistant_id],
                "content": description,
                "convId": chat_id,
                "id": user_id,
                "parent": root_id,
                "role": "user",
                "timestamp": base_ts + 1,
                "toolCalls": "",
                "type": "text",
            },
            {
                "children": [],
                "content": body,
                "convId": chat_id,
                "id": assistant_id,
                "model": "tarea programada",
                "parent": user_id,
                "role": "assistant",
                "timestamp": base_ts + 2,
                "toolCalls": "",
                "type": "text",
            },
        ]
        conversation = {
            "currNode": assistant_id,
            "id": chat_id,
            "lastModified": base_ts + 2,
            "name": self.chat_name_for(task, started_at),
        }
        record: Dict[str, Any] = {
            "conv": conversation,
            "created_at": finished_at.isoformat(),
            "description": description,
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            "error": error,
            "id": chat_id,
            "messages": messages,
            "started_at": started_at.isoformat(),
            "status": status,
            "task_id": task.get("id"),
        }

        try:
            self.chats_dir.mkdir(parents=True, exist_ok=True)
            # Escritura atómica: la WebUI puede estar leyendo esta carpeta a la
            # vez (sync cada 30 s) y no debe ver un JSON a medias.
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)
        except OSError as exc:
            logger.error(f"No se pudo guardar el chat de la tarea programada: {exc}")
            return record

        logger.info(f"💬 Chat de tarea programada guardado: {chat_id}")
        return record

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"Chat ilegible {path.name}: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def list(self, since: Optional[str] = None, limit: int = DEFAULT_LIST_LIMIT) -> List[Dict[str, Any]]:
        """Resúmenes (sin mensajes) ordenados del más reciente al más antiguo."""
        if not self.chats_dir.exists():
            return []
        since_dt = _parse_iso(since)
        try:
            limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIST_LIMIT

        summaries: List[Dict[str, Any]] = []
        for path in self.chats_dir.glob("*.json"):  # los .json.tmp quedan fuera
            record = self._read(path)
            if not record:
                continue
            created_at = _parse_iso(record.get("created_at"))
            if since_dt and created_at and created_at <= since_dt:
                continue
            conversation = record.get("conv") or {}
            summaries.append(
                {
                    "created_at": record.get("created_at"),
                    "description": record.get("description"),
                    "duration_ms": record.get("duration_ms"),
                    "error": record.get("error"),
                    "id": record.get("id") or path.stem,
                    "message_count": len(record.get("messages") or []),
                    "name": conversation.get("name"),
                    "status": record.get("status"),
                    "task_id": record.get("task_id"),
                }
            )
        summaries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return summaries[:limit]

    def get(self, chat_id: str) -> Optional[Dict[str, Any]]:
        if not chat_id:
            return None
        path = self._path_for(chat_id)
        if not path.exists():
            return None
        return self._read(path)

    def delete(self, chat_id: str) -> bool:
        path = self._path_for(chat_id)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as exc:
            logger.warning(f"No se pudo borrar el chat {chat_id}: {exc}")
            return False
        return True
