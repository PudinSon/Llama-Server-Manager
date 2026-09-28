"""Base común de los registros de dispatchers de la automatización 24/7.

Cada fuente de eventos (webhook, carpeta vigilada, horario) es un módulo
independiente, pero todos comparten la misma mecánica:

1. Un **registro persistente** en `user_data/` con entradas nombradas.
2. La conversión de un evento detectado en una **tarea de la cola**.
3. Un `status()` con contadores para que la UI lo pinte igual.

Este módulo implementa (1) y (3) por completo y la mitad de (2)
(`BaseRegistry._enqueue`), de modo que añadir una fuente nueva es escribir el
detector y declarar sus campos por defecto. `tools/webhook_dispatcher.py`,
`tools/filewatch_dispatcher.py` y `tools/cron_dispatcher.py` se apoyan en él.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

from .event_queue import EventQueue, get_event_queue

logger = logging.getLogger("dispatcher_registry")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_BODY_BYTES = 256 * 1024


# ============================================================================
# Plantillas
# ============================================================================
class _Namespace(dict):
    """Dict con acceso por atributo y claves ausentes vacías.

    Permite plantillas naturales (`{repository.name}`, `{file.name}`) sin exigir
    el nombre aplanado y sin reventar si el evento no trae ese campo.
    """

    def __missing__(self, key):
        return ""

    def __getattr__(self, item):
        return self.get(item, "")


def to_namespace(value: Any) -> Any:
    """Convierte dicts anidados en `_Namespace` (recursivo)."""
    if isinstance(value, dict):
        return _Namespace({key: to_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [to_namespace(item) for item in value]
    return value


def render_template(template: str, payload: Any = None, extra: Optional[Dict[str, Any]] = None) -> str:
    """Formatea una plantilla contra el payload + `extra`.

    Claves ausentes → cadena vacía; una plantilla malformada se devuelve tal cual
    (nunca debe impedir que el evento se encole).
    """
    info = to_namespace(payload) if isinstance(payload, dict) else _Namespace()
    if extra:
        for key, value in extra.items():
            info[key] = value
    try:
        return template.format_map(info)
    except Exception:
        return template


def flatten(payload: Any, prefix: str = "") -> Dict[str, Any]:
    """Aplana un payload anidado (para pistas y plantillas simples)."""
    flat: Dict[str, Any] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            flat.update(flatten(value, f"{prefix}{key}."))
    elif isinstance(payload, list):
        for index, value in enumerate(payload[:10]):
            flat.update(flatten(value, f"{prefix}{index}."))
    else:
        flat[prefix.rstrip(".") or "value"] = payload
    return flat


def maybe_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped[0] in "[{":
        try:
            return json.loads(stripped)
        except (TypeError, ValueError):
            return {"raw": text}
    return {"raw": text}


def coerce_body(body: Any) -> Tuple[Any, Optional[str]]:
    """Devuelve `(payload, texto_crudo)` para poder medir el tamaño."""
    if body is None:
        return None, None
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body).decode("utf-8", errors="replace")
        return maybe_json(raw), raw
    if isinstance(body, str):
        return maybe_json(body), body
    try:
        return body, json.dumps(body, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return body, str(body)


# ============================================================================
# Registro
# ============================================================================
def default_config_path(filename: str) -> Path:
    return Path(runtime.get_user_data_dir()) / filename


def is_kind_paused(kind: str) -> bool:
    """¿Está este tipo de dispatcher en pausa? Nunca lanza."""
    try:
        from .dispatcher_control import is_paused
    except ImportError:  # pragma: no cover - ejecución suelta
        from tools.dispatcher_control import is_paused  # type: ignore
    return is_paused(kind)


class BaseRegistry:
    """Registro persistente de entradas nombradas + encolado de eventos.

    Subclases típicas: declarar `config_filename`, `default_fields`,
    `source_prefix`, `label` y `secret_fields`, y añadir el detector concreto.
    """

    config_filename: str = ""
    source_prefix: str = ""
    label: str = "dispatcher"
    collection_key: str = "items"
    #: Campos cuyo valor nunca se devuelve en listados (se resumen en `has_*`).
    secret_fields: Tuple[str, ...] = ()
    #: Campo→valor por defecto. El tipo del valor por defecto manda la coerción.
    default_fields: Dict[str, Any] = {}

    def __init__(
        self,
        queue: Optional[EventQueue] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        self.queue = queue or get_event_queue()
        self.config_path = (
            Path(config_path) if config_path else default_config_path(self.config_filename)
        )
        self._lock = threading.RLock()
        self._entries: Dict[str, Dict[str, Any]] = self._load()
        self._enqueued_total = 0
        self._rejected_total = 0
        self._last_event: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------
    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:  # configuración corrupta: mejor arrancar vacío
            logger.warning(f"No se pudo leer {self.config_path.name}: {exc}")
            return {}
        items = raw.get(self.collection_key) if isinstance(raw, dict) else raw
        if isinstance(items, dict):
            items = [dict(value, name=key) for key, value in items.items() if isinstance(value, dict)]
        if not isinstance(items, list):
            return {}
        registry: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            if not NAME_RE.match(name):
                continue
            registry[name] = self._normalize(name, item)
        return registry

    def _save(self) -> None:
        payload = {self.collection_key: [dict(entry) for entry in self._entries.values()]}
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.config_path)
        except Exception as exc:  # nunca debe tumbar el detector
            logger.error(f"No se pudo guardar {self.config_path.name}: {exc}")

    @classmethod
    def _coerce(cls, field: str, value: Any, default: Any) -> Any:
        if value is None:
            return default
        try:
            if isinstance(default, bool):
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "on", "sí", "si"}
                return bool(value)
            if isinstance(default, int):
                return int(value)
            if isinstance(default, float):
                return float(value)
            if isinstance(default, str):
                return str(value)
        except (TypeError, ValueError):
            return default
        return value

    @classmethod
    def _normalize(cls, name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"name": name}
        for field, default in cls.default_fields.items():
            entry[field] = cls._coerce(field, data.get(field, default), default)
        entry["source"] = str(entry.get("source") or f"{cls.source_prefix}:{name}")
        entry["created_at"] = data.get("created_at") or time.time()
        for field in cls.secret_fields:
            entry[field] = str(entry.get(field) or "")
        return entry

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @staticmethod
    def validate_name(name: str) -> str:
        slug = str(name or "").strip().lower()
        if not NAME_RE.match(slug):
            raise ValueError(
                "nombre inválido: usa minúsculas, dígitos, guion o guion bajo (1-64 caracteres)"
            )
        return slug

    def _public(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        public = dict(entry)
        for field in self.secret_fields:
            public[f"has_{field}"] = bool(entry.get(field))
            public[field] = ""
        return public

    def _prepare_create(self, data: Dict[str, Any]) -> None:
        """Hook: rellena valores que solo se generan al crear (p. ej. un token)."""

    def _validate_entry(self, data: Dict[str, Any]) -> None:
        """Hook: valida la entrada y lanza `ValueError` si no es utilizable."""

    def list(self, include_secrets: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            entries = [dict(entry) for entry in self._entries.values()]
        entries.sort(key=lambda item: item["name"])
        return [entry if include_secrets else self._public(entry) for entry in entries]

    def get(self, name: str, include_secrets: bool = False) -> Optional[Dict[str, Any]]:
        with self._lock:
            raw = self._entries.get(str(name).strip().lower())
            entry = dict(raw) if raw else None
        if entry is None or include_secrets:
            return entry
        return self._public(entry)

    def upsert(self, name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Crea o actualiza una entrada. Un secreto vacío no borra el existente."""
        slug = self.validate_name(name)
        with self._lock:
            current = self._entries.get(slug)
            data: Dict[str, Any] = dict(current) if current else {"name": slug}
            for field in self.default_fields:
                if field not in fields:
                    continue
                value = fields[field]
                if value is None:
                    continue
                if field in self.secret_fields and not str(value).strip():
                    continue
                data[field] = value
            if current is None:
                self._prepare_create(data)
            self._validate_entry(data)
            entry = self._normalize(slug, data)
            if current:
                entry["created_at"] = current.get("created_at") or entry["created_at"]
            self._entries[slug] = entry
            self._save()
        logger.info(f"🔌 {self.label} '{slug}' {'actualizado' if current else 'creado'}")
        return dict(entry)

    def delete(self, name: str) -> bool:
        slug = str(name).strip().lower()
        with self._lock:
            if slug not in self._entries:
                return False
            self._entries.pop(slug)
            self._save()
        logger.info(f"🗑️  {self.label} '{slug}' eliminado")
        return True

    # ------------------------------------------------------------------
    # Encolado
    # ------------------------------------------------------------------
    def _enqueue(
        self,
        entry: Dict[str, Any],
        *,
        payload: Any = None,
        wake_prompt: Optional[str] = None,
        source_ref: Optional[str] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Encola el evento de una entrada y actualiza contadores.

        Respeta `requires_approval` de la entrada inyectándolo en el payload, para
        que el gate humano del dispatcher funcione igual en todas las fuentes.
        """
        requires_approval = entry.get("requires_approval")
        if requires_approval is not None:
            if not isinstance(payload, dict):
                payload = {"event": payload} if payload not in (None, "") else {}
            payload = dict(payload)
            payload.setdefault("requires_approval", bool(requires_approval))
        if extra_payload:
            if not isinstance(payload, dict):
                payload = {"event": payload} if payload not in (None, "") else {}
            payload = dict(payload)
            for key, value in extra_payload.items():
                payload.setdefault(key, value)

        task_id = self.queue.enqueue(
            source=str(entry.get("source") or f"{self.source_prefix}:{entry['name']}"),
            source_ref=source_ref,
            payload=payload,
            wake_prompt=wake_prompt,
            agent_template=str(entry.get("agent_template") or "") or None,
            priority=int(entry.get("priority") or 0),
            dedupe=bool(entry.get("dedupe", True)),
        )
        self._enqueued_total += 1
        self._last_event = {
            "at": time.time(),
            self.source_prefix or "dispatcher": entry["name"],
            "name": entry["name"],
            "task_id": task_id,
        }
        return task_id

    def _reject(self) -> None:
        self._rejected_total += 1

    def mark_run(
        self,
        name: str,
        *,
        task_id: Optional[int] = None,
        status: str = "ok",
        at: Optional[float] = None,
    ) -> None:
        """Deja constancia del último disparo.

        `at` permite inyectar el instante del disparo (los detectores pueden
        recibir un reloj simulado en tests); por defecto se usa el reloj real.
        """
        slug = str(name).strip().lower()
        with self._lock:
            entry = self._entries.get(slug)
            if entry is None:
                return
            entry["last_run"] = float(at) if at is not None else time.time()
            entry["last_task_id"] = task_id
            entry["last_status"] = status
            self._save()

    # ------------------------------------------------------------------
    # Introspección
    # ------------------------------------------------------------------
    def status(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            entries = [dict(entry) for entry in self._entries.values()]
        payload: Dict[str, Any] = {
            "enabled": True,
            "paused": is_kind_paused(self.source_prefix),
            "total": len(entries),
            "active": sum(1 for entry in entries if entry.get("enabled", True)),
            "enqueued_total": self._enqueued_total,
            "rejected_total": self._rejected_total,
            "last_event": self._last_event,
            self.collection_key: [self._public(entry) for entry in sorted(entries, key=lambda i: i["name"])],
        }
        if extra:
            payload.update(extra)
        return payload


def is_loopback(addr: Optional[str]) -> bool:
    """¿La dirección de origen es la propia máquina?"""
    if not addr:
        return False
    host = str(addr).split("%")[0]
    if host in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return True
    return host.startswith("127.") or host == "::1"
