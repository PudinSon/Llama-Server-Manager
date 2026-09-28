# tools/scheduler_service.py
"""Scheduler de tareas programadas, independiente del daemon y de la WebUI.

Hasta ahora las tareas programadas solo se ejecutaban dentro de
`DaemonAgent._check_scheduled_tasks()`, que sale inmediatamente si el daemon no
está corriendo (`not self.is_running`). Y como el daemon solo arranca a mano —y
desde la UI ni siquiera existe esa llamada—, las tareas podían quedarse
pendientes para siempre, con la webview abierta o cerrada.

Este servicio vive en el proceso `tools.dev_agent` (puerto 15000), que el
Manager arranca siempre al iniciar la aplicación, antes incluso de cargar el
modelo. No depende de la interfaz ni del daemon:

* Escanea `scheduled_tasks` de `.daemon/daemon_state.json` cada `interval` s.
* Ejecuta las vencidas con el `Orchestrator` (mismo agente que el chat normal).
* Si llama-server no está listo, deja la tarea pendiente y reintenta.
* Persiste el resultado en el estado y guarda la conversación completa
  (`ScheduledChatStore`) para que la WebUI la importe al historial nativo.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

from .scheduled_chats import ScheduledChatStore, parse_local_datetime

logger = logging.getLogger("scheduler_service")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [SCHEDULER] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

DAEMON_STATE_FILE = "daemon_state.json"
DEFAULT_INTERVAL = 20
MODEL_PROBE_TIMEOUT = 2.0
MODEL_PROBE_CACHE_S = 10.0
MAX_RESULT_STORED = 4000

REPEAT_INTERVALS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


def _parse_iso(value: Any) -> Optional[datetime]:
    """Alias local: la normalización de zonas vive en `scheduled_chats`."""
    return parse_local_datetime(value)


class ScheduledTaskService:
    """Hilo que ejecuta las tareas programadas sin depender de nadie."""

    def __init__(
        self,
        workspace_path: Optional[Union[str, Path]] = None,
        interval: int = DEFAULT_INTERVAL,
        llm_url: Optional[str] = None,
    ) -> None:
        if workspace_path is None:
            try:
                workspace_path = runtime.RuntimeConfig.get_workspace_abs()
            except Exception:  # pragma: no cover - configuración ilegible
                workspace_path = Path.cwd()
        self.workspace_path = Path(workspace_path).resolve()
        self.interval = max(5, int(interval or DEFAULT_INTERVAL))
        self.llm_url = (llm_url or os.environ.get("MCP_LLM_URL") or "http://127.0.0.1:8081").rstrip("/")
        self.chat_store = ScheduledChatStore(self.workspace_path)

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._busy = threading.Event()
        self._state_lock = threading.Lock()
        self._orchestrator = None
        self._current: Optional[Dict[str, Any]] = None
        self._last_run: Optional[Dict[str, Any]] = None
        self._last_skip: Optional[Dict[str, Any]] = None
        self._model_ready_cache = False
        self._model_probe_at = 0.0
        self._warned_no_model = False
        self._lock_handle = None

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _acquire_process_lock(self) -> bool:
        """Un solo scheduler por workspace.

        Dos procesos (por ejemplo el dev_agent del Manager y otro lanzado a
        mano) verían las mismas tareas vencidas y las ejecutarían por duplicado.
        El lock lo libera el sistema operativo si el proceso muere.
        """
        lock_path = self.workspace_path / ".daemon" / "scheduler.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
        except OSError as exc:
            logger.warning(f"Sin lock de scheduler ({exc}); se continúa sin protección")
            return True

        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        self._lock_handle = handle
        return True

    def _release_process_lock(self) -> None:
        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass

    def start(self) -> "ScheduledTaskService":
        if self.is_running:
            return self
        if not self._acquire_process_lock():
            logger.info(
                "⏭️  Otro proceso ya está ejecutando las tareas programadas de este workspace"
            )
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduled-task-service", daemon=True)
        self._thread.start()
        logger.info(
            f"⏰ Scheduler de tareas iniciado (cada {self.interval}s, workspace={self.workspace_path})"
        )
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._cancel_orchestrator()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._release_process_lock()
        logger.info("⏰ Scheduler de tareas detenido")

    def _cancel_orchestrator(self) -> None:
        orchestrator = self._orchestrator
        if orchestrator is None:
            return
        try:
            orchestrator.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Bucle
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._scan()
            except Exception as exc:  # nunca debe matar el hilo
                logger.error(f"Error en el escaneo de tareas programadas: {exc}", exc_info=True)
                self._stop_event.wait(self.interval)
                continue
            self._stop_event.wait(self.interval)

    def _scan(self) -> List[Dict[str, Any]]:
        """Ejecuta las tareas vencidas (una detrás de otra, sin solaparse)."""
        if self._busy.is_set():
            return []

        tasks = self._load_tasks()
        due = [task for task in tasks if self._is_due(task)]
        if not due:
            return []

        if not self._model_ready():
            self._last_skip = {
                "at": datetime.now().isoformat(),
                "reason": "llama-server no está listo",
                "task_ids": [task.get("id") for task in due],
            }
            if not self._warned_no_model:
                self._warned_no_model = True
                logger.info(
                    f"⏳ Hay {len(due)} tarea(s) vencidas esperando a que el modelo esté cargado "
                    f"({self.llm_url}/health)"
                )
            return []

        self._warned_no_model = False
        self._last_skip = None
        executed: List[Dict[str, Any]] = []
        for task in due:
            if self._stop_event.is_set():
                break
            self._busy.set()
            try:
                executed.append(self._execute(task))
            finally:
                self._busy.clear()
        return executed

    def run_now(self, task_id: str) -> bool:
        """Lanza una tarea concreta sin esperar a su hora programada."""
        task = next((item for item in self._load_tasks() if item.get("id") == task_id), None)
        if not task:
            return False
        if self._busy.is_set() or not self._model_ready():
            return False

        def worker() -> None:
            self._busy.set()
            try:
                self._execute(task)
            finally:
                self._busy.clear()

        threading.Thread(target=worker, name=f"scheduled-task-{task_id}", daemon=True).start()
        return True

    # ------------------------------------------------------------------
    # Estado en disco
    # ------------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.workspace_path / ".daemon" / DAEMON_STATE_FILE

    def _load_tasks(self) -> List[Dict[str, Any]]:
        try:
            if not self.state_path.exists():
                return []
            import json

            state = json.loads(self.state_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as exc:
            logger.warning(f"No se pudo leer el estado del daemon: {exc}")
            return []
        tasks = state.get("scheduled_tasks")
        return [task for task in tasks if isinstance(task, dict)] if isinstance(tasks, list) else []

    def _update_task(self, task_id: str, fields: Dict[str, Any]) -> None:
        """Leer-modificar-escribir: otro proceso (la UI) también edita este fichero."""
        import json

        with self._state_lock:
            state: Dict[str, Any] = {}
            try:
                if self.state_path.exists():
                    state = json.loads(self.state_path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                state = {}
            tasks = state.get("scheduled_tasks")
            if not isinstance(tasks, list):
                return
            for task in tasks:
                if isinstance(task, dict) and task.get("id") == task_id:
                    task.update(fields)
                    break
            else:
                return
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as exc:
                logger.error(f"No se pudo actualizar la tarea {task_id}: {exc}")

    @staticmethod
    def _is_due(task: Dict[str, Any]) -> bool:
        status = str(task.get("status") or "pending").casefold()
        if status in {"cancelled", "disabled"}:
            return False
        if task.get("enabled") is False:
            return False
        # Las repetitivas siguen vencidas aunque su último ciclo se completara.
        if status == "completed" and not task.get("repeat"):
            return False
        next_execution = _parse_iso(task.get("next_execution") or task.get("when"))
        if next_execution is None:
            return False
        try:
            return next_execution <= datetime.now()
        except TypeError:  # una tarea con fecha rara no debe tumbar el escaneo
            logger.warning(f"Tarea {task.get('id')} con fecha inválida: {task.get('next_execution')!r}")
            return False

    # ------------------------------------------------------------------
    # Ejecución
    # ------------------------------------------------------------------
    def _get_orchestrator(self):
        if self._orchestrator is not None:
            return self._orchestrator
        from .agent_core import LLMClient, Orchestrator

        self._orchestrator = Orchestrator(
            llm_client=LLMClient(base_url=self.llm_url, timeout=300),
            workspace_path=str(self.workspace_path),
            enable_checkpoints=True,
            reasoning=True,
        )
        return self._orchestrator

    def _execute(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Ejecuta una tarea, persiste el resultado y publica el chat."""
        started = datetime.now()
        task_id = str(task.get("id") or "")
        description = str(task.get("description") or "")
        self._current = {
            "description": description,
            "started_at": started.isoformat(),
            "task_id": task_id,
        }
        logger.info(f"⏰ Ejecutando tarea programada {task_id}: {description[:80]}")

        result = ""
        error: Optional[str] = None
        try:
            # Ejecución autónoma: sin agentes preseleccionados en la UI. El
            # propio orquestador decide entre respuesta directa (tarea simple) y
            # selección automática de agentes (tarea multi-paso).
            result = str(
                self._get_orchestrator().run_autonomous_task(
                    description,
                    reasoning=True,
                )
                or ""
            )
        except Exception as exc:
            error = str(exc)
            result = f"Error: {exc}"
            logger.error(f"❌ Error ejecutando la tarea {task_id}: {exc}")

        finished = datetime.now()
        status = "failed" if error else "completed"
        record = self.chat_store.record(
            task,
            result,
            status=status,
            error=error,
            started_at=started,
            finished_at=finished,
        )

        repeat = task.get("repeat")
        fields: Dict[str, Any] = {
            "last_chat_id": record.get("id"),
            "last_duration_ms": record.get("duration_ms"),
            "last_executed": finished.isoformat(),
            "last_status": status,
            "result": (result or "")[:MAX_RESULT_STORED],
            "run_count": int(task.get("run_count") or 0) + 1,
            # Las repetitivas vuelven a quedar pendientes para el siguiente ciclo.
            "status": "pending" if repeat else status,
        }
        if repeat:
            fields["next_execution"] = self._next_execution(finished, str(repeat)).isoformat()
        self._update_task(task_id, fields)

        self._last_run = {
            "chat_id": record.get("id"),
            "description": description,
            "duration_ms": record.get("duration_ms"),
            "finished_at": finished.isoformat(),
            "status": status,
            "task_id": task_id,
        }
        self._current = None

        self._record_episode(task, result, status)
        self._notify(task, result, status)
        return self._last_run

    @staticmethod
    def _next_execution(reference: datetime, repeat: str) -> datetime:
        """Siguiente ejecución futura (sin ráfagas si el equipo estuvo apagado)."""
        step = REPEAT_INTERVALS.get(repeat.casefold(), REPEAT_INTERVALS["daily"])
        current = reference + step
        now = datetime.now()
        while current <= now:
            current += step
        return current

    def _record_episode(self, task: Dict[str, Any], result: str, outcome: str) -> None:
        """Deja rastro en la memoria episódica (best-effort)."""
        try:
            from .episodic_memory import EpisodicMemory

            EpisodicMemory().record(
                session_id="scheduler",
                task=f"[tarea programada] {task.get('description', '')}"[:300],
                result=str(result)[:1000],
                outcome=outcome,
                tags=["daemon", "scheduled"],
            )
        except Exception as exc:
            logger.debug(f"Sin memoria episódica para la tarea programada: {exc}")

    def _notify(self, task: Dict[str, Any], result: str, status: str) -> None:
        """Notificación del sistema (best-effort)."""
        try:
            from .notifications import NotificationLevel, NotificationManager

            manager = NotificationManager(str(self.workspace_path))
            manager.notify(
                title=f"Tarea programada {'completada' if status == 'completed' else 'fallida'}: "
                f"{str(task.get('description', ''))[:60]}",
                message=str(result)[:240],
                level=NotificationLevel.INFO if status == "completed" else NotificationLevel.WARNING,
            )
        except Exception as exc:
            logger.debug(f"Sin notificación para la tarea programada: {exc}")

    # ------------------------------------------------------------------
    # Modelo
    # ------------------------------------------------------------------
    def _model_ready(self, cached: bool = False) -> bool:
        """¿Está llama-server listo para atender peticiones?"""
        now = time.monotonic()
        if cached and now - self._model_probe_at < MODEL_PROBE_CACHE_S:
            return self._model_ready_cache
        ready = False
        try:
            request = urllib.request.Request(f"{self.llm_url}/health", method="GET")
            with urllib.request.urlopen(request, timeout=MODEL_PROBE_TIMEOUT) as response:
                ready = 200 <= int(getattr(response, "status", 200)) < 300
        except urllib.error.HTTPError as exc:
            # llama-server responde 503 mientras carga el modelo.
            ready = 200 <= int(getattr(exc, "code", 0)) < 300
        except Exception:
            ready = False
        self._model_ready_cache = ready
        self._model_probe_at = now
        return ready

    # ------------------------------------------------------------------
    # Introspección
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        tasks = self._load_tasks()
        due = [task.get("id") for task in tasks if self._is_due(task)]
        upcoming: Optional[Dict[str, Any]] = None
        now = datetime.now()
        for task in tasks:
            next_execution = _parse_iso(task.get("next_execution") or task.get("when"))
            if next_execution is None:
                continue
            # "Próxima" es la siguiente futura: las vencidas ya van en due_task_ids.
            if next_execution <= now:
                continue
            if upcoming is None or next_execution < upcoming["_dt"]:
                upcoming = {
                    "_dt": next_execution,
                    "description": task.get("description"),
                    "id": task.get("id"),
                    "next_execution": next_execution.isoformat(),
                }
        if upcoming:
            upcoming.pop("_dt", None)
        return {
            "busy": self._busy.is_set(),
            "chats_dir": str(self.chat_store.chats_dir),
            "current": self._current,
            "due_task_ids": due,
            "interval_seconds": self.interval,
            "last_run": self._last_run,
            "llm_url": self.llm_url,
            "model_ready": self._model_ready(cached=True),
            "running": self.is_running,
            "task_count": len(tasks),
            "upcoming": upcoming,
            "waiting_for_model": self._last_skip,
            "workspace": str(self.workspace_path),
        }


# ============================================================================
# Instancia global (una por proceso)
# ============================================================================

_service: Optional[ScheduledTaskService] = None
_service_lock = threading.Lock()


def get_scheduler_service(workspace_path: Optional[Union[str, Path]] = None) -> ScheduledTaskService:
    """Devuelve (creando si hace falta) el servicio global del proceso."""
    global _service
    with _service_lock:
        if _service is None:
            _service = ScheduledTaskService(workspace_path)
        return _service


def start_scheduler_service(interval: int = DEFAULT_INTERVAL) -> Optional[ScheduledTaskService]:
    """Arranca el scheduler una sola vez. Nunca propaga errores de arranque."""
    try:
        return get_scheduler_service().start()
    except Exception as exc:
        logger.error(f"No se pudo iniciar el scheduler de tareas: {exc}", exc_info=True)
        return None


def stop_scheduler_service() -> None:
    global _service
    with _service_lock:
        service = _service
    if service is not None:
        service.stop()


def get_chat_store(workspace_path: Optional[Union[str, Path]] = None) -> ScheduledChatStore:
    """Store de conversaciones de tareas programadas del workspace actual."""
    return get_scheduler_service(workspace_path).chat_store
