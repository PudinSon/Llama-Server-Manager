"""Estado global (pausa/reanudación) de los dispatchers de la automatización.

Los cuatro dispatchers —webhook, filewatch, cron e imap— tienen su propio
registro y su propio bucle, pero el usuario quiere un único interruptor por
fuente: «deja de generar tareas desde aquí sin perder la configuración».

Este módulo guarda ese estado en `user_data/automation_state.json`, así que una
pausa **sobrevive a un reinicio del servicio** (si no, en un servicio 24/7 no
serviría de nada). Cada detector consulta `is_paused(kind)` en su bucle y se
salta la vuelta; las acciones manuales (escanear ahora, ejecutar ahora, sondear
el buzón) lo ignoran a propósito: pausar detiene la automática, no al usuario.

La pausa es por **tipo** de dispatcher, no por entrada: si quieres dejar de
vigilar una carpeta concreta, deshabilita esa entrada.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

logger = logging.getLogger("dispatcher_control")

CONFIG_FILENAME = "automation_state.json"

#: Los cuatro **dispatchers** (fuentes de eventos). El orden define el del panel.
SOURCE_KINDS = ("webhook", "filewatch", "cron", "imap")

#: El **ejecutor** no es una fuente: es el pool de workers que consume la cola.
#: Tiene su propia palanca porque pausarlo significa «no reclames tareas nuevas»
#: (las que ya están en curso terminan), lo que deja un backlog visible.
EXECUTOR_KIND = "executor"

#: Todo lo que se puede pausar.
KINDS = SOURCE_KINDS + (EXECUTOR_KIND,)

LABELS = {
    "webhook": "Webhooks",
    "filewatch": "FileWatch",
    "cron": "Cron",
    "imap": "Correo (IMAP)",
    EXECUTOR_KIND: "Ejecutor de la cola",
}


class DispatcherControl:
    """Estado persistente de pausa por tipo de dispatcher."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config_path = (
            Path(config_path)
            if config_path
            else Path(runtime.get_user_data_dir()) / CONFIG_FILENAME
        )
        self._lock = threading.RLock()
        self._state: Dict[str, Dict[str, Any]] = self._load()

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------
    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning(f"No se pudo leer {self.config_path.name}: {exc}")
            return {}
        entries = raw.get("dispatchers") if isinstance(raw, dict) else raw
        if not isinstance(entries, dict):
            return {}
        state: Dict[str, Dict[str, Any]] = {}
        for kind, value in entries.items():
            if kind not in KINDS or not isinstance(value, dict):
                continue
            state[kind] = {
                "paused": bool(value.get("paused")),
                "at": value.get("at"),
                "reason": str(value.get("reason") or ""),
            }
        return state

    def _save(self) -> None:
        payload = {"dispatchers": {kind: dict(value) for kind, value in self._state.items()}}
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.config_path)
        except Exception as exc:  # nunca debe tumbar un detector
            logger.error(f"No se pudo guardar {self.config_path.name}: {exc}")

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    @staticmethod
    def validate_kind(kind: str) -> str:
        slug = str(kind or "").strip().lower()
        if slug not in KINDS:
            raise ValueError(f"dispatcher desconocido: {kind!r} (usa {', '.join(KINDS)})")
        return slug

    def reload(self) -> None:
        """Vuelve a leer el fichero (por si otro proceso lo cambió)."""
        with self._lock:
            self._state = self._load()

    def state(self, kind: str) -> Dict[str, Any]:
        slug = self.validate_kind(kind)
        with self._lock:
            entry = self._state.get(slug) or {}
        return {
            "kind": slug,
            "label": LABELS.get(slug, slug),
            "paused": bool(entry.get("paused")),
            "at": entry.get("at"),
            "reason": str(entry.get("reason") or ""),
        }

    def is_paused(self, kind: str) -> bool:
        """¿Debe saltarse la vuelta automática este dispatcher?"""
        try:
            return self.state(kind)["paused"]
        except ValueError:
            return False

    def set_state(self, kind: str, paused: bool, reason: str = "") -> Dict[str, Any]:
        slug = self.validate_kind(kind)
        with self._lock:
            self._state[slug] = {
                "paused": bool(paused),
                "at": time.time(),
                "reason": str(reason or ""),
            }
            self._save()
        logger.info(f"{'⏸️  Pausado' if paused else '▶️  Reanudado'} dispatcher '{slug}'")
        return self.state(slug)

    def pause(self, kind: str, reason: str = "") -> Dict[str, Any]:
        return self.set_state(kind, True, reason)

    def resume(self, kind: str) -> Dict[str, Any]:
        return self.set_state(kind, False)

    def snapshot(self, kinds: Optional[tuple] = None) -> Dict[str, Dict[str, Any]]:
        """Estado de todos los tipos (por defecto, incluido el ejecutor)."""
        return {kind: self.state(kind) for kind in (kinds or KINDS)}

    def source_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Solo los cuatro dispatchers fuente (lo que pinta el panel)."""
        return self.snapshot(SOURCE_KINDS)


# ============================================================================
# Instancia global (una por proceso)
# ============================================================================
_control: Optional[DispatcherControl] = None
_lock = threading.Lock()


def get_dispatcher_control(config_path: Optional[Path] = None) -> DispatcherControl:
    """Devuelve (creando si hace falta) el control global del proceso."""
    global _control
    with _lock:
        if _control is None:
            _control = DispatcherControl(config_path=config_path)
        return _control


def reset_dispatcher_control() -> None:
    """Olvida la instancia global (tests)."""
    global _control
    with _lock:
        _control = None


def is_paused(kind: str) -> bool:
    """Atajo para los bucles de detección: nunca lanza, nunca los tumba."""
    try:
        return get_dispatcher_control().is_paused(kind)
    except Exception as exc:  # pragma: no cover - el detector debe seguir vivo
        logger.debug(f"No se pudo consultar el estado de '{kind}': {exc}")
        return False
