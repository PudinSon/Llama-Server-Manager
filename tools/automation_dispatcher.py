"""Dispatcher de la automatización 24/7 (Bloque 7).

Toma las tareas de la cola persistente (`tools/event_queue.py`), aplica el
**gate de aprobación** y las despacha al `Orchestrator`, avisando por
escritorio y Telegram a través de `tools/notifications.py`.

Ciclo de una tarea:

1. `claim_next()` la reserva (atómico; nadie más la ejecuta).
2. Si el gate de aprobación la bloquea, se marca `awaiting_approval`, se genera
   un token y se notifica. Un humano la aprueba (`approve(token)`) o la rechaza
   (`reject(token)`).
3. Si no, se ejecuta en el `Orchestrator` con el `wake_prompt` (y el payload
   como contexto), con un hilo de latido que renueva `heartbeat`.
4. Se marca `done`/`failed` y se notifica el resultado.

El dispatcher es deliberadamente independiente de la UI y del daemon: vive en el
mismo proceso que `tools.dev_agent` (puerto 15000) junto al scheduler de tareas
programadas. Arranca un **pool de workers** (`num_workers`, configurable con
`automation_workers`): cada worker reclama y ejecuta tareas en paralelo y usa su
propio `Orchestrator` (no son thread-safe). Un hilo *janitor* devuelve
periódicamente a `pending` las tareas cuyo worker murió.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

try:
    from .dispatcher_control import (
        EXECUTOR_KIND,
        get_dispatcher_control,
        is_paused as _is_kind_paused,
    )
except ImportError:  # pragma: no cover - ejecución suelta
    from tools.dispatcher_control import (  # type: ignore
        EXECUTOR_KIND,
        get_dispatcher_control,
        is_paused as _is_kind_paused,
    )

from .event_queue import (
    AWAITING_APPROVAL,
    CANCELLED,
    DEFAULT_APPROVAL_TTL_MINUTES,
    DEFAULT_PRUNE_DAYS,
    FAILED,
    EventQueue,
    get_event_queue,
)

logger = logging.getLogger("automation_dispatcher")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [AUTOMATION] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_HEARTBEAT_INTERVAL = 30.0
DEFAULT_STALE_MINUTES = 15
MAX_RESULT_STORED = 4000
# Reintentos con backoff de las tareas fallidas.
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BASE_SECONDS = 30.0
MAX_RETRY_SECONDS = 3600.0


def _workspace_path(workspace_path: Optional[Union[str, Path]] = None) -> Path:
    if workspace_path is not None:
        return Path(workspace_path).resolve()
    try:
        return Path(runtime.RuntimeConfig.get_workspace_abs()).resolve()
    except Exception:  # pragma: no cover - configuración ilegible
        return Path.cwd()


def _settings_defaults() -> Dict[str, Any]:
    """Lee `defaults` de `user_data/config.json` (donde las guarda el Manager)."""
    try:
        config_path = Path(runtime.get_user_data_dir()) / "config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    defaults = data.get("defaults")
    return defaults if isinstance(defaults, dict) else data


class AutomationDispatcher:
    """Hilo que ejecuta las tareas de la cola de automatización."""

    def __init__(
        self,
        workspace_path: Optional[Union[str, Path]] = None,
        llm_url: Optional[str] = None,
        queue: Optional[EventQueue] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        stale_minutes: float = DEFAULT_STALE_MINUTES,
        require_approval: Optional[bool] = None,
        notify: bool = True,
        worker_id: Optional[str] = None,
        num_workers: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_base_seconds: Optional[float] = None,
    ) -> None:
        self.workspace_path = _workspace_path(workspace_path)
        self.llm_url = (llm_url or "http://127.0.0.1:8081").rstrip("/")
        self.queue = queue or get_event_queue()
        self.poll_interval = max(0.5, float(poll_interval))
        self.heartbeat_interval = max(0.1, float(heartbeat_interval))
        self.stale_minutes = float(stale_minutes)
        self.notify_enabled = bool(notify)
        self.worker_id = worker_id or f"dispatcher-{secrets.token_hex(3)}"

        defaults = _settings_defaults()
        if require_approval is None:
            self.require_approval = bool(defaults.get("automation_require_approval", False))
        else:
            self.require_approval = bool(require_approval)
        if num_workers is None:
            num_workers = defaults.get("automation_workers", 1)
        try:
            self.num_workers = max(1, int(num_workers))
        except (TypeError, ValueError):
            self.num_workers = 1
        # Cada cuánto revisa el janitor si algún worker murió sin terminar.
        self.stale_sweep_interval = max(30.0, self.stale_minutes * 60.0 / 4.0)
        # Endurecimiento: las aprobaciones caducan y las tareas terminales se podan.
        try:
            self.approval_ttl_minutes = float(
                defaults.get("automation_approval_ttl_minutes", DEFAULT_APPROVAL_TTL_MINUTES)
            )
        except (TypeError, ValueError):
            self.approval_ttl_minutes = float(DEFAULT_APPROVAL_TTL_MINUTES)
        try:
            self.prune_days = float(defaults.get("automation_prune_days", DEFAULT_PRUNE_DAYS))
        except (TypeError, ValueError):
            self.prune_days = float(DEFAULT_PRUNE_DAYS)

        if max_retries is None:
            max_retries = defaults.get("automation_max_retries", DEFAULT_MAX_RETRIES)
        try:
            self.max_retries = max(0, int(max_retries))
        except (TypeError, ValueError):
            self.max_retries = DEFAULT_MAX_RETRIES
        if retry_base_seconds is None:
            retry_base_seconds = defaults.get(
                "automation_retry_base_seconds", DEFAULT_RETRY_BASE_SECONDS
            )
        try:
            self.retry_base_seconds = max(1.0, float(retry_base_seconds))
        except (TypeError, ValueError):
            self.retry_base_seconds = DEFAULT_RETRY_BASE_SECONDS

        self._retried = 0
        self._threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        #: Se activa para despertar a los workers antes de su siguiente sondeo
        #: (p. ej. al reanudar el ejecutor: las tareas represadas salen al instante).
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._pool_lock = threading.Lock()
        self._orchestrator = None
        self._orchestrators: Dict[str, Any] = {}
        self._active: Dict[str, Dict[str, Any]] = {}
        self._last_result: Optional[Dict[str, Any]] = None
        self._processed = 0
        self._failed = 0

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    @property
    def paused(self) -> bool:
        """¿Está el ejecutor en pausa? (los workers no reclaman tareas nuevas)."""
        try:
            return _is_kind_paused(EXECUTOR_KIND)
        except Exception:  # pragma: no cover - la cola nunca debe morir por esto
            return False

    def wake(self) -> None:
        """Despierta a los workers para que no esperen el próximo sondeo."""
        self._wake_event.set()

    def _sleep_or_wake(self, timeout: float) -> None:
        """Espera como mucho `timeout` s, pero se corta si alguien llama a `wake()`."""
        self._wake_event.wait(timeout)
        self._wake_event.clear()

    def _worker_ids(self) -> List[str]:
        """Identificadores de los workers (uno solo salvo pool explícito)."""
        if self.num_workers <= 1:
            return [self.worker_id]
        return [f"{self.worker_id}-w{i}" for i in range(self.num_workers)]

    def start(self) -> "AutomationDispatcher":
        if self.is_running:
            return self
        self._stop_event.clear()
        # Recupera tareas cuyo worker murió antes de empezar a repartir.
        self.requeue_orphans()
        for worker_id in self._worker_ids():
            thread = threading.Thread(
                target=self._worker_loop,
                args=(worker_id,),
                name=f"automation-worker-{worker_id}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        janitor = threading.Thread(
            target=self._janitor_loop, name="automation-janitor", daemon=True
        )
        self._threads.append(janitor)
        janitor.start()
        logger.info(
            f"🤖 Dispatcher de automatización iniciado "
            f"({self.num_workers} worker(s), cada {self.poll_interval}s)"
        )
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        with self._pool_lock:
            orchestrators = list(self._orchestrators.values())
        if self._orchestrator is not None:
            orchestrators.append(self._orchestrator)
        for orchestrator in orchestrators:
            try:
                orchestrator.stop()
            except Exception:
                pass
        for thread in list(self._threads):
            if thread.is_alive():
                thread.join(timeout=timeout)
        self._threads.clear()
        logger.info("🤖 Dispatcher de automatización detenido")

    def requeue_orphans(self) -> int:
        """Devuelve a `pending` las tareas cuyo worker murió sin terminarlas."""
        try:
            count = self.queue.requeue_stale(timeout_minutes=self.stale_minutes)
        except Exception as exc:  # pragma: no cover - cola aún no lista
            logger.warning(f"No se pudo recuperar tareas huérfanas: {exc}")
            return 0
        return count

    def _worker_loop(self, worker_id: str) -> None:
        """Un worker del pool: reclama y procesa tareas hasta que se pare."""
        while not self._stop_event.is_set():
            # En pausa el worker sigue vivo pero no reclama nada: las tareas se
            # acumulan y salen todas al reanudar.
            if self.paused:
                self._sleep_or_wake(self.poll_interval)
                continue
            try:
                task = self.queue.claim_next(worker_id=worker_id)
            except Exception as exc:  # nunca debe matar el hilo
                logger.error(f"Error reclamando tareas ({worker_id}): {exc}", exc_info=True)
                self._sleep_or_wake(self.poll_interval)
                continue
            if task is None:
                self._sleep_or_wake(self.poll_interval)
                continue
            try:
                self.process_task(task, worker_id=worker_id)
            except Exception as exc:  # el error ya se registra en la tarea
                logger.error(f"Error procesando la tarea {task.get('id')}: {exc}", exc_info=True)

    def _janitor_loop(self) -> None:
        """Mantenimiento periódico: huérfanas, aprobaciones caducadas y poda."""
        while not self._stop_event.wait(self.stale_sweep_interval):
            self.requeue_orphans()
            try:
                self.queue.expire_approvals(timeout_minutes=self.approval_ttl_minutes)
            except Exception as exc:  # pragma: no cover
                logger.debug(f"No se pudieron caducar aprobaciones: {exc}")
            try:
                self.queue.prune(keep_terminal_days=self.prune_days)
            except Exception as exc:  # pragma: no cover
                logger.debug(f"No se pudo podar la cola: {exc}")

    # ------------------------------------------------------------------
    # Reparto
    # ------------------------------------------------------------------
    def dispatch_once(self, worker_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Reserva y procesa una sola tarea. `None` si no había pendientes.

        Es una acción **manual**, así que ignora la pausa del ejecutor (igual que
        `scan_now`, `run_now` o `poll_now` en los dispatchers fuente).
        """
        worker_id = worker_id or self.worker_id
        task = self.queue.claim_next(worker_id=worker_id)
        if task is None:
            return None
        return self.process_task(task, worker_id=worker_id)

    def process_task(self, task: Dict[str, Any], worker_id: Optional[str] = None) -> Dict[str, Any]:
        worker_id = worker_id or self.worker_id
        task_id = int(task["id"])
        with self._lock:
            self._active[worker_id] = {"id": task_id, "source": task.get("source")}
        try:
            if self.needs_approval(task):
                return self._request_approval(task)
            return self._execute(task, worker_id=worker_id)
        finally:
            with self._lock:
                self._active.pop(worker_id, None)

    # ------------------------------------------------------------------
    # Gate de aprobación
    # ------------------------------------------------------------------
    def needs_approval(self, task: Dict[str, Any]) -> bool:
        """¿Debe esperar aprobación humana antes de ejecutarse?

        `approval_token` presente ⇒ ya aprobada ⇒ no se vuelve a bloquear.
        El payload puede forzar `requires_approval` (true/false) por tarea.
        """
        if task.get("approval_token"):
            return False
        payload = task.get("payload")
        if isinstance(payload, dict) and "requires_approval" in payload:
            return bool(payload["requires_approval"])
        return bool(self.require_approval)

    def _request_approval(self, task: Dict[str, Any]) -> Dict[str, Any]:
        token = secrets.token_urlsafe(16)
        self.queue.mark_awaiting_approval(int(task["id"]), token)
        self._audit("approval_requested", task, detail={"token": token})
        logger.info(f"🔐 Tarea {task['id']} esperando aprobación (token {token})")
        self._notify(
            title="🔐 Aprobación pendiente",
            message=f"{self._describe(task)}\nToken: {token}",
            level="warning",
            event_type="automation_approval",
        )
        return {"id": int(task["id"]), "status": AWAITING_APPROVAL, "approval_token": token}

    def approve(self, token: str) -> Optional[int]:
        """Aprueba una tarea en espera. Devuelve su id o `None` si no existe."""
        task = self.queue.find_by_approval_token(token)
        if not task or task.get("status") != AWAITING_APPROVAL:
            return None
        task_id = int(task["id"])
        if not self.queue.mark_approved(task_id):
            return None
        self._audit("approved", task)
        logger.info(f"✅ Tarea {task_id} aprobada")
        self._notify(
            title="✅ Automatización aprobada",
            message=self._describe(task),
            level="success",
            event_type="automation_approved",
        )
        return task_id

    def reject(self, token: str, reason: str = "rechazada por el usuario") -> Optional[int]:
        """Rechaza una tarea en espera. Devuelve su id o `None` si no existe."""
        task = self.queue.find_by_approval_token(token)
        if not task or task.get("status") != AWAITING_APPROVAL:
            return None
        task_id = int(task["id"])
        if not self.queue.mark_rejected(task_id, reason):
            return None
        self._audit("rejected", task, detail={"reason": reason})
        logger.info(f"⛔ Tarea {task_id} rechazada")
        self._notify(
            title="⛔ Automatización rechazada",
            message=f"{self._describe(task)}\n{reason}",
            level="warning",
            event_type="automation_rejected",
        )
        return task_id

    # ------------------------------------------------------------------
    # Ejecución
    # ------------------------------------------------------------------
    def _execute(self, task: Dict[str, Any], worker_id: Optional[str] = None) -> Dict[str, Any]:
        worker_id = worker_id or self.worker_id
        task_id = int(task["id"])
        prompt = self.build_prompt(task)
        logger.info(f"▶️  Ejecutando tarea {task_id} ({task.get('source')}): {prompt[:80]}")

        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(task_id, worker_id, heartbeat_stop),
            name=f"automation-heartbeat-{task_id}",
            daemon=True,
        )
        heartbeat_thread.start()

        result = ""
        error: Optional[str] = None
        try:
            result = str(
                self._orchestrator_for(worker_id).run_autonomous_task(
                    prompt,
                    reasoning=True,
                    context=self.build_context(task),
                ) or ""
            )
        except Exception as exc:
            error = str(exc)
            result = f"Error: {exc}"
            logger.error(f"❌ Error ejecutando la tarea {task_id}: {exc}")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)

        if error:
            self.queue.mark_failed(task_id, error=error, result=result[:MAX_RESULT_STORED])
            self._audit("failed", task, detail={"error": error[:500]})
            with self._lock:
                self._failed += 1
            status = "failed"
            delay = self._schedule_retry(task, error)
            if delay is not None:
                status = "retrying"
                with self._lock:
                    self._retried += 1
                self._notify(
                    title="♻️ Automatización reprogramada",
                    message=(
                        f"{self._describe(task)}\n{error}\n"
                        f"Reintento en {int(delay)}s"
                    ),
                    level="warning",
                    event_type="automation_retry",
                )
            else:
                self._notify(
                    title="❌ Automatización fallida",
                    message=f"{self._describe(task)}\n{error}",
                    level="error",
                    event_type="automation_failed",
                )
        else:
            self.queue.mark_done(task_id, result=result[:MAX_RESULT_STORED])
            self._audit("done", task)
            self._notify(
                title="✅ Automatización completada",
                message=f"{self._describe(task)}\n{result[:240]}",
                level="success",
                event_type="automation_completed",
            )
            status = "done"
            self._maybe_reply_email(task, result)

        with self._lock:
            self._processed += 1
        outcome = {"id": task_id, "status": status, "result": result[:MAX_RESULT_STORED], "error": error}
        self._last_result = outcome
        return outcome

    def _schedule_retry(self, task: Dict[str, Any], error: str) -> Optional[float]:
        """Reprograma una tarea fallida con backoff. `None` si ya no quedan intentos.

        `attempts` en la fila ya cuenta el intento recién fallido (lo incrementa
        `requeue_for_retry`), así que el número de reintentos consumidos es el
        valor actual de `attempts`.
        """
        task_id = int(task["id"])
        try:
            attempts = int(task.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if attempts >= self.max_retries:
            return None
        delay = self.queue.retry_delay_seconds(
            task_id, base_seconds=self.retry_base_seconds, max_seconds=MAX_RETRY_SECONDS
        )
        if not self.queue.requeue_for_retry(task_id, delay_seconds=delay, error=error):
            return None
        self._audit(
            "retry_scheduled",
            task,
            detail={"attempt": attempts + 1, "max_retries": self.max_retries, "delay_seconds": round(delay, 1)},
        )
        logger.info(
            f"♻️  Tarea {task_id} reprogramada para reintento {attempts + 1}/{self.max_retries} en {int(delay)}s"
        )
        return delay

    def retry_task(self, task_id: int, delay_seconds: float = 0.0) -> bool:
        """Reintento manual: devuelve a la cola una tarea fallida o cancelada.

        No reprograma tareas en curso ni completadas. Devuelve `True` si la
        tarea volvió a `pending`.
        """
        task = self.queue.get_task(int(task_id))
        if task is None:
            raise KeyError(f"tarea {task_id} no encontrada")
        if task.get("status") not in ("failed", "cancelled", FAILED, CANCELLED):
            raise ValueError(
                f"solo se pueden reintentar tareas fallidas o canceladas "
                f"(estado actual: {task.get('status')})"
            )
        if not self.queue.requeue_for_retry(
            int(task_id), delay_seconds=delay_seconds, error=task.get("error")
        ):
            return False
        self._audit("retry_manual", task, detail={"delay_seconds": delay_seconds})
        logger.info(f"♻️  Tarea {task_id} reintentada manualmente")
        return True

    def _audit(self, action: str, task: Dict[str, Any], detail: Any = None) -> None:
        """Registro de auditoría best-effort (nunca debe tumbar el worker)."""
        try:
            self.queue.record_audit(
                action,
                task_id=int(task.get("id")) if task.get("id") is not None else None,
                actor=str(task.get("source") or "dispatcher"),
                detail=detail,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug(f"No se pudo auditar '{action}': {exc}")

    def _maybe_reply_email(self, task: Dict[str, Any], result: str) -> None:
        """Responde por correo si la tarea vino de IMAP (opt-in)."""
        if str(task.get("source") or "") != "imap":
            return
        try:
            from .imap_sender import reply_to_task

            outcome = reply_to_task(task, result)
        except Exception as exc:  # pragma: no cover - el correo no es crítico
            logger.debug(f"Respuesta por correo omitida: {exc}")
            return
        if outcome.get("success"):
            self._audit("email_reply", task)

    def _heartbeat_loop(self, task_id: int, worker_id: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(self.heartbeat_interval):
            try:
                self.queue.heartbeat(task_id, worker_id=worker_id)
            except Exception as exc:  # pragma: no cover - la cola no debe tumbar el hilo
                logger.debug(f"Latido falló para la tarea {task_id}: {exc}")

    def _get_orchestrator(self):
        """Orchestrator único (caso `num_workers=1`, y monkeypatch en tests)."""
        if self._orchestrator is not None:
            return self._orchestrator
        self._orchestrator = self._create_orchestrator()
        return self._orchestrator

    def _create_orchestrator(self):
        from .agent_core import LLMClient, Orchestrator

        return Orchestrator(
            llm_client=LLMClient(base_url=self.llm_url, timeout=600),
            workspace_path=str(self.workspace_path),
            enable_checkpoints=True,
            reasoning=True,
        )

    def _orchestrator_for(self, worker_id: str):
        """Cada worker del pool usa su propio Orchestrator (no son thread-safe)."""
        if self.num_workers <= 1 or worker_id == self.worker_id:
            return self._get_orchestrator()
        with self._pool_lock:
            orchestrator = self._orchestrators.get(worker_id)
            if orchestrator is None:
                orchestrator = self._create_orchestrator()
                self._orchestrators[worker_id] = orchestrator
            return orchestrator

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------
    def build_prompt(self, task: Dict[str, Any]) -> str:
        """Compone la instrucción para el agente a partir de la tarea."""
        wake_prompt = str(task.get("wake_prompt") or "").strip()
        template = str(task.get("agent_template") or "").strip()

        prompt = wake_prompt
        if not prompt:
            prompt = f"Procesa el evento {task.get('source')}:{task.get('source_ref') or task.get('id')}"
        if template:
            prompt = f"{template}\n\n{prompt}"
        return prompt

    def build_context(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Mantiene payload de eventos fuera del objetivo y las subtareas."""
        context: Dict[str, Any] = {}
        payload = task.get("payload")
        if payload not in (None, "", {}, []):
            context["event"] = payload
        source = task.get("source")
        source_ref = task.get("source_ref")
        if source or source_ref:
            context["event_metadata"] = {"source": source, "source_ref": source_ref}
        return context

    def _describe(self, task: Dict[str, Any]) -> str:
        base = str(task.get("wake_prompt") or "").strip() or str(task.get("source") or "evento")
        ref = task.get("source_ref")
        if ref:
            base = f"{base} [{ref}]"
        return base[:160]

    def _notify(self, title: str, message: str, level: str, event_type: str) -> None:
        """Avisa por escritorio y Telegram reutilizando `NotificationManager`."""
        if not self.notify_enabled:
            return
        try:
            from .notifications import (
                NotificationChannel,
                NotificationLevel,
                NotificationManager,
            )

            manager = NotificationManager(str(self.workspace_path))
            # Escritorio y Telegram pueden venir deshabilitados en la config; la
            # automatización los activa explícitamente para no perder el aviso.
            manager.enable_channel(NotificationChannel.DESKTOP)
            manager.enable_channel(NotificationChannel.TELEGRAM)
            levels = {
                "info": NotificationLevel.INFO,
                "success": NotificationLevel.SUCCESS,
                "warning": NotificationLevel.WARNING,
                "error": NotificationLevel.ERROR,
            }
            manager.notify(
                title=title,
                message=message,
                level=levels.get(level, NotificationLevel.INFO),
                event_type=event_type,
                channels=[
                    NotificationChannel.DESKTOP,
                    NotificationChannel.TELEGRAM,
                    NotificationChannel.LOG,
                ],
            )
        except Exception as exc:
            logger.debug(f"Sin notificación para la automatización: {exc}")

    # ------------------------------------------------------------------
    # Introspección
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            active = dict(self._active)
            processed = self._processed
            failed = self._failed
            retried = self._retried
        current = active.get(self.worker_id)
        if current is None and active:
            current = next(iter(active.values()))
        try:
            control = get_dispatcher_control().state(EXECUTOR_KIND)
        except Exception:  # pragma: no cover
            control = {"paused": False, "at": None, "reason": ""}
        try:
            backlog = self.queue.backlog_counts()
        except Exception:  # pragma: no cover
            backlog = {"ready": 0, "waiting_backoff": 0, "awaiting_approval": 0, "total": 0}
        return {
            "running": self.is_running,
            "paused": bool(control.get("paused")),
            "paused_at": control.get("at"),
            "paused_reason": control.get("reason") or "",
            "backlog": backlog,
            "worker_id": self.worker_id,
            "num_workers": self.num_workers,
            "require_approval": self.require_approval,
            "current": current,
            "active": active,
            "last_result": self._last_result,
            "processed": processed,
            "failed": failed,
            "retried": retried,
            "max_retries": self.max_retries,
            "retry_base_seconds": self.retry_base_seconds,
            "poll_interval": self.poll_interval,
            "queue": self.queue.stats(),
            "workspace": str(self.workspace_path),
            "llm_url": self.llm_url,
        }


# ============================================================================
# Instancia global (una por proceso)
# ============================================================================

_dispatcher: Optional[AutomationDispatcher] = None
_dispatcher_lock = threading.Lock()


def get_dispatcher(
    workspace_path: Optional[Union[str, Path]] = None,
    queue: Optional[EventQueue] = None,
) -> AutomationDispatcher:
    """Devuelve (creando si hace falta) el dispatcher global del proceso."""
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            _dispatcher = AutomationDispatcher(workspace_path=workspace_path, queue=queue)
        return _dispatcher


def start_dispatcher_service() -> Optional[AutomationDispatcher]:
    """Arranca el dispatcher una sola vez. Nunca propaga errores de arranque."""
    try:
        return get_dispatcher().start()
    except Exception as exc:
        logger.error(f"No se pudo iniciar el dispatcher de automatización: {exc}", exc_info=True)
        return None


def stop_dispatcher_service() -> None:
    global _dispatcher
    with _dispatcher_lock:
        dispatcher = _dispatcher
    if dispatcher is not None:
        dispatcher.stop()


def reset_dispatcher() -> None:
    """Olvida la instancia global (tests)."""
    global _dispatcher
    with _dispatcher_lock:
        _dispatcher = None
