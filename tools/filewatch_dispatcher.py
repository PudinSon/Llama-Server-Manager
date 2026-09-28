"""Dispatcher de tipo **FileWatch**: vigila carpetas y encola lo que aparece.

Tercer módulo enchufable sobre la misma cola, hermano de
`tools/webhook_dispatcher.py` y `tools/cron_dispatcher.py`. Un FileWatch sí tiene
bucle propio: cada pocos segundos recorre la carpeta vigilada y, cuando un
archivo coincide con el patrón y ya no está creciendo, encola una tarea con su
ruta (y el contenido queda a un `read_file` de distancia para el agente).

Registro
--------
La configuración vive en `user_data/automation_filewatches.json`. Cada entrada:

===================  ==========================================================
`name`               identificador (slug: `a-z0-9_-`, 1-64)
`enabled`            si se vigila (por defecto `true`)
`path`               carpeta a vigilar (absoluta, o relativa al workspace)
`pattern`            glob(s) separados por comas — por defecto `*`
`ignore`             glob(s) a descartar, separados por comas
`recursive`          mirar también subcarpetas (por defecto `false`)
`event`              `created` | `modified` | `any` (por defecto `created`)
`stable_seconds`     esperar a que el archivo deje de crecer (por defecto 2 s)
`max_per_poll`       tope de archivos encolados por vuelta (por defecto 20)
`source`             etiqueta de la cola (por defecto `filewatch:<name>`)
`wake_prompt`        plantilla con `{file}`, `{name}`, `{path}`, `{size}`
`agent_template`     instrucciones fijas antepuestas al prompt
`requires_approval`  fuerza el gate humano para esta fuente
`priority`           prioridad en la cola (-1000..1000)
`dedupe`             no repetir el mismo archivo/versión (`true`)
===================  ==========================================================

`stable_seconds` evita procesar un archivo a medio escribir (una descarga, un
zip). La memoria de «ya lo encolé» no vive en este módulo sino en la cola
(`source_ref`), así que sobrevive a un reinicio del servicio.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

from .dispatcher_registry import BaseRegistry, is_kind_paused, render_template
from .event_queue import EventQueue

logger = logging.getLogger("filewatch_dispatcher")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [FILEWATCH] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

CONFIG_FILENAME = "automation_filewatches.json"
DEFAULT_TICK_SECONDS = 10.0
DEFAULT_STABLE_SECONDS = 2.0
DEFAULT_MAX_PER_POLL = 20
EVENTS = ("created", "modified", "any")

__all__ = [
    "CONFIG_FILENAME",
    "FileWatchDispatcher",
    "get_filewatch_dispatcher",
    "start_filewatch_dispatcher",
    "stop_filewatch_dispatcher",
    "reset_filewatch_dispatcher",
]


def _workspace() -> Optional[Path]:
    try:
        return Path(runtime.RuntimeConfig.get_workspace_abs())
    except Exception:
        return None


def resolve_watch_path(value: Any) -> Path:
    """Resuelve la carpeta vigilada: absoluta tal cual, relativa al workspace."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("falta 'path' (carpeta a vigilar)")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        base = _workspace() or Path.cwd()
        candidate = base / candidate
    return candidate


def _matches_glob(name: str, patterns: Iterable[str]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(name, pattern) for pattern in patterns if pattern)


class FileWatchDispatcher(BaseRegistry):
    """Registro de carpetas vigiladas y bucle que las recorre."""

    config_filename = CONFIG_FILENAME
    source_prefix = "filewatch"
    label = "FileWatch"
    collection_key = "filewatches"
    default_fields = {
        "enabled": True,
        "path": "",
        "pattern": "*",
        "ignore": "",
        "recursive": False,
        "event": "created",
        "stable_seconds": DEFAULT_STABLE_SECONDS,
        "max_per_poll": DEFAULT_MAX_PER_POLL,
        "source": "",
        "wake_prompt": "",
        "agent_template": "",
        "requires_approval": None,
        "priority": 0,
        "dedupe": True,
    }

    def __init__(
        self,
        queue: Optional[EventQueue] = None,
        config_path: Optional[Path] = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        super().__init__(queue=queue, config_path=config_path)
        self.tick_seconds = max(1.0, float(tick_seconds))
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        #: `ruta -> (tamaño, ts de primera observación)` para no leer a medias.
        self._seen: Dict[str, tuple] = {}
        self._files_total = 0

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "FileWatchDispatcher":
        if self.is_running:
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="filewatch-dispatcher", daemon=True)
        self._thread.start()
        logger.info(f"📁 FileWatch iniciado ({self.tick_seconds}s por vuelta)")
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("📁 FileWatch detenido")

    def _loop(self) -> None:
        while not self._stop_event.wait(self.tick_seconds):
            try:
                self.scan()
            except Exception as exc:  # pragma: no cover - nunca debe matar el hilo
                logger.error(f"Error en el ciclo de FileWatch: {exc}", exc_info=True)

    # ------------------------------------------------------------------
    # Detección
    # ------------------------------------------------------------------
    def _validate_entry(self, data: Dict[str, Any]) -> None:
        path = resolve_watch_path(data.get("path"))
        if not path.is_dir():
            raise ValueError(f"la carpeta vigilada no existe o no es un directorio: {path}")
        event = str(data.get("event") or "created").strip().lower()
        if event not in EVENTS:
            raise ValueError(f"event inválido: usa uno de {', '.join(EVENTS)}")
        try:
            raw_stable = data.get("stable_seconds")
            stable = 0.0 if raw_stable in (None, "") else float(raw_stable)
            raw_poll = data.get("max_per_poll")
            max_poll = (
                DEFAULT_MAX_PER_POLL if raw_poll in (None, "") else int(raw_poll)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"valor numérico inválido: {exc}") from exc
        if stable < 0:
            raise ValueError("stable_seconds no puede ser negativo")
        if max_poll < 1:
            raise ValueError("max_per_poll debe ser >= 1")
        if not [p for p in str(data.get("pattern") or "*").split(",") if p.strip()]:
            raise ValueError("pattern no puede quedar vacío")

    def _patterns(self, entry: Dict[str, Any]) -> List[str]:
        return [p.strip() for p in str(entry.get("pattern") or "*").split(",") if p.strip()]

    def _ignored(self, entry: Dict[str, Any], name: str) -> bool:
        globs = [g.strip() for g in str(entry.get("ignore") or "").split(",") if g.strip()]
        return _matches_glob(name, globs)

    def _is_stable(self, path: Path, entry: Dict[str, Any], now: datetime) -> bool:
        """¿El archivo lleva quieto lo suficiente como para procesarlo?"""
        try:
            stable = float(entry.get("stable_seconds") or 0)
        except (TypeError, ValueError):
            stable = DEFAULT_STABLE_SECONDS
        if stable <= 0:
            return True
        try:
            size = path.stat().st_size
        except OSError:
            return False
        key = str(path)
        record = self._seen.get(key)
        if record is None or record[0] != size:
            self._seen[key] = (size, now.timestamp())
            return False
        return (now.timestamp() - float(record[1])) >= stable

    @staticmethod
    def _relative(base: Path, path: Path) -> str:
        try:
            return str(path.relative_to(base))
        except ValueError:
            return path.name

    def _source_ref(
        self, entry: Dict[str, Any], rel: str, stat: Any, event: str
    ) -> Optional[str]:
        """Clave de deduplicación. `created` ignora la fecha para no repetirse."""
        if event == "created":
            return f"{entry['name']}:{rel}:{stat.st_size}"
        return f"{entry['name']}:{rel}:{stat.st_size}:{stat.st_mtime_ns}"

    def _prompt(self, entry: Dict[str, Any], path: Path, rel: str, stat: Any, event: str) -> str:
        modified = datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds")
        extra = {
            "watch": entry["name"],
            "file": rel,
            "name": path.name,
            "path": str(path),
            "size": stat.st_size,
            "modified": modified,
            "event": event,
        }
        template = str(entry.get("wake_prompt") or "")
        if not template:
            verb = "modificado" if event == "modified" else "nuevo"
            return f"Hay un archivo {verb} en la carpeta vigilada «{entry['name']}»: {rel}."
        return render_template(template, extra, extra)

    def scan(self, now: Optional[datetime] = None) -> List[int]:
        """Una vuelta de vigilancia. Devuelve los ids de las tareas encoladas.

        Si el dispatcher está en pausa global, la vuelta automática no hace nada.
        `scan_now()` (disparo manual) ignora la pausa a propósito.
        """
        if is_kind_paused(self.source_prefix):
            return []
        now = now or datetime.now()
        with self._lock:
            entries = [dict(entry) for entry in self._entries.values()]
        enqueued: List[int] = []
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            try:
                enqueued.extend(self._scan_entry(entry, now))
            except Exception as exc:
                logger.warning(f"📁 No se pudo vigilar «{entry.get('name')}»: {exc}")
        self._prune_seen()
        return enqueued

    def _scan_entry(self, entry: Dict[str, Any], now: datetime) -> List[int]:
        base = resolve_watch_path(entry.get("path"))
        if not base.is_dir():
            self._reject()
            raise FileNotFoundError(f"carpeta no disponible: {base}")
        event = str(entry.get("event") or "created").strip().lower()
        recursive = bool(entry.get("recursive"))
        try:
            limit = max(1, int(entry.get("max_per_poll") or DEFAULT_MAX_PER_POLL))
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_PER_POLL

        found: set[Path] = set()
        for pattern in self._patterns(entry):
            iterator = base.rglob(pattern) if recursive else base.glob(pattern)
            for candidate in iterator:
                try:
                    if candidate.is_file() and not self._ignored(entry, candidate.name):
                        found.add(candidate)
                except OSError:
                    continue

        source = str(entry.get("source") or f"filewatch:{entry['name']}")
        enqueued: List[int] = []
        for path in sorted(found):
            if len(enqueued) >= limit:
                logger.info(f"📁 «{entry['name']}»: tope de {limit} archivos por vuelta alcanzado")
                break
            if not self._is_stable(path, entry, now):
                continue
            try:
                stat = path.stat()
            except OSError:  # desapareció entre el listado y el stat
                continue
            rel = self._relative(base, path)
            source_ref = self._source_ref(entry, rel, stat, event)
            if source_ref and bool(entry.get("dedupe", True)):
                try:
                    if self.queue.exists_by_source_ref(source, source_ref):
                        continue
                except Exception as exc:  # pragma: no cover - la cola no debe frenar
                    logger.debug(f"No se pudo comprobar deduplicación: {exc}")
            try:
                task_id = self._enqueue(
                    entry,
                    payload={
                        "watch": entry["name"],
                        "path": str(path),
                        "file": rel,
                        "size": stat.st_size,
                        "event": event,
                    },
                    wake_prompt=self._prompt(entry, path, rel, stat, event),
                    source_ref=source_ref,
                )
            except (ValueError, OSError) as exc:
                self._reject()
                logger.warning(f"📁 No se pudo encolar {path}: {exc}")
                continue
            self._files_total += 1
            enqueued.append(task_id)
            self.mark_run(entry["name"], task_id=task_id, status="enqueued", at=now.timestamp())
            logger.info(f"📁 «{entry['name']}» → tarea {task_id} ({path.name})")
        return enqueued

    def scan_now(self, name: str, now: Optional[datetime] = None) -> List[int]:
        """Escanea una única entrada a mano (útil para probar sin esperar)."""
        entry = self.get(name, include_secrets=True)
        if entry is None:
            raise KeyError(f"filewatch '{name}' no encontrado")
        return self._scan_entry(entry, now or datetime.now())

    def _prune_seen(self) -> None:
        for key in [item for item in self._seen if not Path(item).exists()]:
            self._seen.pop(key, None)

    # ------------------------------------------------------------------
    # Introspección
    # ------------------------------------------------------------------
    def status(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = super().status(extra)
        payload["running"] = self.is_running
        payload["tick_seconds"] = self.tick_seconds
        payload["files_total"] = self._files_total
        return payload


# ============================================================================
# Instancia global (una por proceso)
# ============================================================================
_dispatcher: Optional[FileWatchDispatcher] = None
_lock = threading.Lock()


def get_filewatch_dispatcher(
    queue: Optional[EventQueue] = None,
    config_path: Optional[Path] = None,
) -> FileWatchDispatcher:
    global _dispatcher
    with _lock:
        if _dispatcher is None:
            _dispatcher = FileWatchDispatcher(queue=queue, config_path=config_path)
        return _dispatcher


def start_filewatch_dispatcher() -> Optional[FileWatchDispatcher]:
    try:
        return get_filewatch_dispatcher().start()
    except Exception as exc:  # pragma: no cover
        logger.error(f"No se pudo iniciar FileWatch: {exc}", exc_info=True)
        return None


def stop_filewatch_dispatcher() -> None:
    global _dispatcher
    with _lock:
        dispatcher = _dispatcher
    if dispatcher is not None:
        dispatcher.stop()


def reset_filewatch_dispatcher() -> None:
    """Olvida la instancia global (tests)."""
    global _dispatcher
    with _lock:
        _dispatcher = None
