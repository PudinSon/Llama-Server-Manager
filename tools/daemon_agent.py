# tools/daemon_agent.py
# Módulo para el Modo Daemon y Planificación Dinámica.
# Permite al agente trabajar de forma autónoma en segundo plano,
# planificando, ejecutando, reflexionando y adaptándose continuamente.

import os
import json
import time
import threading
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta

# Importaciones del proyecto
try:
    from . import runtime
except ImportError:
    import runtime

from .agent_core import Orchestrator, Agent, LLMClient, EventBus, AgentEvent
from .checkpoint_manager import CheckpointManager
from .memory_skills import get_skill_manager
from .parallel_executor import ParallelExecutor
from .scheduled_chats import parse_local_datetime

# Importar componentes de autonomía (Fase 3)
try:
    from .sensors import SensorManager, SensorEvent
except ImportError:
    SensorManager = None
    SensorEvent = None

try:
    from .goals import GoalManager, Goal, GoalStatus
except ImportError:
    GoalManager = None
    Goal = None
    GoalStatus = None

try:
    from .notifications import NotificationManager, NotificationLevel
except ImportError:
    NotificationManager = None
    NotificationLevel = None

# Importar herramientas de dev_agent para notificaciones
try:
    from .dev_agent import notify, send_telegram, get_datetime
except ImportError:
    def notify(title, message):
        print(f"🔔 {title}: {message}")
    def send_telegram(message):
        print(f"📨 Telegram: {message}")
    def get_datetime():
        return datetime.now().isoformat()

logger = logging.getLogger("daemon_agent")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [DAEMON] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ============================================================================
# CONSTANTES
# ============================================================================

DAEMON_STATE_FILE = "daemon_state.json"
DEFAULT_INTERVAL = 60  # segundos entre ciclos
MAX_CYCLES = -1  # -1 = infinito

# ============================================================================
# CLASE DAEMONAGENT
# ============================================================================

class DaemonAgent:
    """
    Agente autónomo que opera en modo daemon.
    Planifica, ejecuta, reflexiona y se adapta continuamente para lograr un objetivo.
    """
    def __init__(self, objective: str, workspace_path: str,
                 interval: int = DEFAULT_INTERVAL,
                 max_cycles: int = MAX_CYCLES,
                 enable_notifications: bool = True,
                 enable_telegram: bool = False,
                 event_bus: Optional[EventBus] = None):
        """
        Inicializa el DaemonAgent.
        
        Args:
            objective: Objetivo general del agente.
            workspace_path: Ruta al workspace del proyecto.
            interval: Segundos entre ciclos de ejecución.
            max_cycles: Número máximo de ciclos (-1 = infinito).
            enable_notifications: Si debe enviar notificaciones del sistema.
            enable_telegram: Si debe enviar mensajes por Telegram.
        """
        self.objective = objective
        self.workspace_path = Path(workspace_path).resolve()
        self.interval = interval
        self.max_cycles = max_cycles
        self.enable_notifications = enable_notifications
        self.enable_telegram = enable_telegram
        self.event_bus = event_bus or EventBus()
        
        # Estado interno
        self.is_running = False
        self.is_paused = False
        self.current_cycle = 0
        self.cycle_start_time = None
        
        # Inicializar componentes
        self.orchestrator = Orchestrator()
        self.checkpoint_manager = CheckpointManager(str(self.workspace_path))
        self.skill_manager = get_skill_manager(str(self.workspace_path))
        
        # Inicializar componentes de autonomía (Fase 3)
        self.sensor_manager = SensorManager() if SensorManager else None
        self.goal_manager = GoalManager(str(self.workspace_path)) if GoalManager else None
        self.notification_manager = NotificationManager(str(self.workspace_path)) if NotificationManager else None
        
        # Registrar callback de sensores si están disponibles
        if self.sensor_manager:
            self.sensor_manager.on_event = self._on_sensor_event
        
        # Cargar estado persistente
        self.state = self._load_state()
        self._initialize_state()
        
        # Hilo principal
        self._thread = None
        self._stop_event = threading.Event()
        # Los subagentes se ejecutan en serie (ver ParallelExecutor.execute_wave),
        # pero el ejemplo conserva cancel_event para poder detenerlos al parar.
        self.parallel_executor = ParallelExecutor(cancel_event=self._stop_event)
        
        logger.info(f"🤖 DaemonAgent inicializado para objetivo: {objective[:80]}...")
        logger.info(f"📁 Workspace: {self.workspace_path}")
        logger.info(f"⏱️  Intervalo: {interval}s | Ciclos máximos: {'∞' if max_cycles == -1 else max_cycles}")

    # ========================================================================
    # GESTIÓN DEL ESTADO PERSISTENTE
    # ========================================================================

    def _get_state_path(self) -> Path:
        """Devuelve la ruta al archivo de estado del daemon."""
        return self.workspace_path / ".daemon" / DAEMON_STATE_FILE

    def _initialize_state(self):
        """Inicializa el estado si está vacío."""
        if not self.state:
            self.state = {
                "objective": self.objective,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "plan": [],
                "completed_tasks": [],
                "failed_tasks": [],
                "history": [],
                "context": {},
                "lessons": [],
                "checkpoints": []
            }
            self._save_state()

    def _load_state(self) -> Dict:
        """Carga el estado persistente desde el archivo."""
        state_path = self._get_state_path()
        if state_path.exists():
            try:
                with open(state_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error cargando estado: {e}")
        return {}

    def _save_state(self):
        """Guarda el estado persistente en el archivo.

        `scheduled_tasks` se conserva tal y como está en disco: la UI las
        gestiona vía dev_agent (/daemon_tasks/*) sobre el mismo fichero,
        mientras este proceso mantiene su copia en memoria. Si se escribiera la
        copia en memoria sin releer, el primer guardado borraría del disco las
        tareas recién creadas desde la interfaz.
        """
        disk_tasks = self._scheduled_tasks_on_disk()
        if disk_tasks is not None:
            self.state["scheduled_tasks"] = disk_tasks
        self.state["updated_at"] = datetime.now().isoformat()
        state_path = self._get_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        logger.debug("Estado guardado.")

    def _scheduled_tasks_on_disk(self) -> Optional[List[Dict[str, Any]]]:
        """Tareas programadas del fichero de estado, o None si no se puede leer."""
        state_path = self._get_state_path()
        if not state_path.exists():
            return None
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                disk_state = json.load(f) or {}
        except Exception as e:
            logger.warning(f"No se pudo releer el estado del daemon: {e}")
            return None
        tasks = disk_state.get("scheduled_tasks")
        return tasks if isinstance(tasks, list) else None

    def _refresh_scheduled_tasks(self) -> List[Dict[str, Any]]:
        """Sincroniza en memoria las tareas programadas que hay en disco."""
        disk_tasks = self._scheduled_tasks_on_disk()
        if disk_tasks is not None:
            self.state["scheduled_tasks"] = disk_tasks
        return self.state.get("scheduled_tasks", [])

    def _persist_scheduled_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        """Escribe la lista completa de tareas programadas en disco.

        El resto del estado se relee antes de escribir para no pisar cambios
        hechos por otros procesos (dev_agent/UI).
        """
        state_path = self._get_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        disk_state: Dict[str, Any] = {}
        if state_path.exists():
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    disk_state = json.load(f) or {}
            except Exception as e:
                logger.warning(f"Estado ilegible, se recreará: {e}")
                disk_state = {}
        disk_state["scheduled_tasks"] = tasks
        disk_state["updated_at"] = datetime.now().isoformat()
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(disk_state, f, indent=2, ensure_ascii=False)
        self.state["scheduled_tasks"] = tasks

    def _update_scheduled_task_on_disk(
        self, task_id: str, **fields: Any
    ) -> Optional[Dict[str, Any]]:
        """Actualiza una sola tarea en disco (leer-modificar-escribir)."""
        tasks = self._refresh_scheduled_tasks()
        task = next((t for t in tasks if isinstance(t, dict) and t.get("id") == task_id), None)
        if task is None:
            logger.warning(f"Tarea programada {task_id} no encontrada en disco")
            return None
        task.update(fields)
        self._persist_scheduled_tasks(tasks)
        return task

    # ========================================================================
    # MÉTODOS DE CONTROL DEL DAEMON
    # ========================================================================

    def start(self):
        """Inicia el daemon en un hilo separado."""
        if self.is_running:
            logger.warning("El daemon ya está en ejecución.")
            return
        
        self.is_running = True
        self.is_paused = False
        self._stop_event.clear()
        
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("✅ Daemon iniciado.")

    def pause(self):
        """Pausa el daemon."""
        if not self.is_running:
            logger.warning("El daemon no está en ejecución.")
            return
        self.is_paused = True
        self.orchestrator.stop()
        logger.info("⏸️ Daemon pausado.")

    def resume(self):
        """Reanuda el daemon."""
        if not self.is_running:
            logger.warning("El daemon no está en ejecución.")
            return
        self.is_paused = False
        logger.info("▶️ Daemon reanudado.")

    def stop(self):
        """Detiene el daemon de forma segura."""
        if not self.is_running:
            logger.warning("El daemon no está en ejecución.")
            return
        self.is_running = False
        self._stop_event.set()
        self.orchestrator.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("🛑 Daemon detenido.")
        self._save_state()

    def get_status(self) -> Dict:
        """Devuelve el estado actual del daemon."""
        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "current_cycle": self.current_cycle,
            "objective": self.objective,
            "plan_length": len(self.state.get("plan", [])),
            "completed_tasks": len(self.state.get("completed_tasks", [])),
            "failed_tasks": len(self.state.get("failed_tasks", [])),
            "last_updated": self.state.get("updated_at", "never")
        }

    # ========================================================================
    # SISTEMA DE TAREAS PROGRAMADAS
    # ========================================================================

    def schedule_task(self, description: str, when: str, repeat: Optional[str] = None) -> Dict[str, Any]:
        """
        Programa una nueva tarea para ejecutarse en una fecha/hora específica.
        
        Args:
            description: Descripción de la tarea
            when: ISO format datetime (ej: "2025-01-15T14:30:00")
            repeat: Patrón de repetición (opcional): "daily", "weekly", "monthly"
        
        Returns:
            Dict con los datos de la tarea creada
        """
        tasks = self._refresh_scheduled_tasks()
        task_id = f"task_{int(time.time())}_{len(tasks)}"
        
        task = {
            "id": task_id,
            "description": description,
            "when": when,
            "repeat": repeat,
            "created_at": datetime.now().isoformat(),
            "status": "pending",
            "last_executed": None,
            "next_execution": when,
            "result": None
        }
        
        tasks.append(task)
        
        self._persist_scheduled_tasks(tasks)
        
        logger.info(f"📅 Tarea programada: {task_id} - {description} a las {when}")
        return task

    def list_scheduled_tasks(self) -> List[Dict[str, Any]]:
        """Devuelve la lista de tareas programadas (releída del disco)."""
        return self._refresh_scheduled_tasks()

    def get_scheduled_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene una tarea programada por ID (releída del disco)."""
        for task in self._refresh_scheduled_tasks():
            if task["id"] == task_id:
                return task
        return None

    def delete_scheduled_task(self, task_id: str) -> bool:
        """Elimina una tarea programada."""
        tasks = self._refresh_scheduled_tasks()
        for i, task in enumerate(tasks):
            if task["id"] == task_id:
                removed = tasks.pop(i)
                self._persist_scheduled_tasks(tasks)
                logger.info(f"🗑️ Tarea eliminada: {task_id}")
                return True
        return False

    def update_scheduled_task(self, task_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Actualiza una tarea programada."""
        task = self.get_scheduled_task(task_id)
        if task:
            if "when" in kwargs:
                kwargs.setdefault("next_execution", kwargs["when"])
            task.update(kwargs)
            self._persist_scheduled_tasks(self.state.get("scheduled_tasks", []))
            logger.info(f"✏️ Tarea actualizada: {task_id}")
            return task
        return None

    def _check_scheduled_tasks(self):
        """Verifica y ejecuta tareas programadas que se han vencido."""
        if not self.is_running or self.is_paused:
            return
        
        now = datetime.now()
        # Releer del disco: la UI crea/edita/borra tareas vía dev_agent sobre el
        # mismo daemon_state.json, y sin refrescar nunca se ejecutarían.
        tasks = self._refresh_scheduled_tasks()
        
        for task in tasks:
            if task["status"] == "pending" or task.get("repeat"):
                try:
                    # La UI guarda `next_execution` en UTC ('...Z') y aquí se
                    # compara con `datetime.now()` (naive): sin normalizar, la
                    # comparación lanzaba TypeError y la tarea acababa "failed".
                    next_exec = parse_local_datetime(task.get("next_execution"))
                    if next_exec is None:
                        logger.warning(
                            f"Tarea {task.get('id')} sin fecha válida: {task.get('next_execution')!r}"
                        )
                        continue
                    if next_exec <= now:
                        # Ejecutar tarea
                        logger.info(f"⏰ Ejecutando tarea programada: {task['id']}")
                        result = self._execute_scheduled_task(task)
                        
                        # Actualizar tarea (se persiste con leer-modificar-escribir)
                        fields = {
                            "last_executed": datetime.now().isoformat(),
                            "result": result,
                        }
                        task.update(fields)
                        
                        # Si tiene repetición, programar la siguiente ejecución
                        if task.get("repeat"):
                            fields["next_execution"] = self._calculate_next_execution(
                                next_exec, task["repeat"]
                            ).isoformat()
                            logger.info(f"🔄 Siguiente ejecución: {fields['next_execution']}")
                        else:
                            fields["status"] = "completed"
                        self._update_scheduled_task_on_disk(task["id"], **fields)
                        
                        self._save_state()
                        
                        # Notificar
                        if self.notification_manager:
                            self.notification_manager.notify(
                                f"Tarea completada: {task['description']}",
                                level=NotificationLevel.INFO if result else NotificationLevel.WARNING
                            )
                
                except Exception as e:
                    logger.error(f"❌ Error ejecutando tarea {task['id']}: {e}")
                    task["status"] = "failed"
                    task["result"] = str(e)
                    self._update_scheduled_task_on_disk(
                        task["id"], status="failed", result=str(e)
                    )

    def _execute_scheduled_task(self, task: Dict[str, Any]) -> str:
        """Ejecuta una tarea programada y deja rastro (episódica + notificación)."""
        description = str(task.get("description", ""))
        try:
            # Usar el orquestador para ejecutar la tarea
            # La descripción se trata como un objetivo
            # Ejecución autónoma: sin agentes preseleccionados en la UI (una
            # tarea programada no puede depender de lo que el usuario tenga
            # marcado en el modal de agentes en ese momento).
            result = self.orchestrator.run_autonomous_task(
                description,
                bus=self.event_bus,
                reasoning=True,
            )
            text = str(result or "")
            self._record_scheduled_episode(task, text, "success")
            return text
        except Exception as e:
            logger.error(f"Error en ejecución de tarea: {e}")
            message = f"Error: {e}"
            self._record_scheduled_episode(task, message, "error")
            return message

    def _record_scheduled_episode(self, task: Dict[str, Any], result: str, outcome: str) -> None:
        """Registra la ejecución programada en la memoria episódica y notifica."""
        description = str(task.get("description", ""))
        try:
            from .episodic_memory import EpisodicMemory
            EpisodicMemory().record(
                session_id=getattr(self, "session_id", "daemon"),
                task=f"[tarea programada] {description}"[:300],
                result=str(result)[:1000],
                outcome=outcome,
                tags=["daemon", "scheduled"],
            )
        except Exception as e:
            logger.debug(f"No se pudo registrar el episodio de la tarea programada: {e}")

        try:
            if self.notification_manager:
                self.notification_manager.notify(
                    title=f"Tarea programada {outcome}: {description[:60]}",
                    message=str(result)[:240],
                    level=NotificationLevel.INFO if outcome == "success" else NotificationLevel.WARNING,
                )
        except Exception as e:
            logger.debug(f"No se pudo notificar la tarea programada: {e}")

    def _calculate_next_execution(self, current: datetime, repeat: str) -> datetime:
        """Calcula la siguiente ejecución según el patrón de repetición."""
        if repeat == "daily":
            return current + timedelta(days=1)
        elif repeat == "weekly":
            return current + timedelta(weeks=1)
        elif repeat == "monthly":
            return current + timedelta(days=30)  # Aproximado
        else:
            return current + timedelta(days=1)

    def _on_sensor_event(self, event: 'SensorEvent'): # type: ignore
        """Callback cuando un sensor dispara un evento."""
        if not event:
            return
        
        logger.info(f"📡 Evento de sensor: {event.sensor_type} - {event.trigger_type}")
        
        # Convertir evento de sensor a tarea si es apropiado
        task = {
            "id": f"sensor_{event.trigger_type}_{int(time.time())}",
            "description": f"Tarea disparada por sensor: {event.trigger_type}",
            "priority": "high",
            "source": "sensor",
            "sensor_type": event.sensor_type,
            "status": "pending"
        }
        
        self.state.setdefault("plan", []).insert(0, task)
        logger.info(f"✅ Tarea creada desde sensor: {task['id']}")
        
        # Notificar
        if self.notification_manager:
            self.notification_manager.notify_sensor_triggered(
                sensor_type=event.sensor_type,
                trigger_type=event.trigger_type,
                data=event.data
            )

    # ========================================================================
    # BUCLE PRINCIPAL DEL DAEMON
    # ========================================================================

    def _run_loop(self):
        """Bucle principal del daemon."""
        logger.info("🔄 Bucle del daemon iniciado.")
        
        while self.is_running and not self._stop_event.is_set():
            if self.is_paused:
                self._stop_event.wait(1)
                continue
            
            try:
                self.current_cycle += 1
                self.cycle_start_time = time.time()
                logger.info(f"🔄 Ciclo {self.current_cycle} iniciado.")
                
                # 0. VERIFICAR SENSORES (Fase 3)
                self._check_sensors()
                
                # 0.5. EVALUAR GOALS (Fase 3)
                self._evaluate_goals()
                
                # 0.7. EJECUTAR TAREAS PROGRAMADAS
                self._check_scheduled_tasks()
                
                # Verificar si el objetivo ya está cumplido
                if self._is_objective_completed():
                    logger.info("✅ Objetivo completado.")
                    self._notify("🎉 Objetivo completado", f"El agente ha completado el objetivo: {self.objective}")
                    break
                
                # 1. PLANIFICAR
                self._plan()
                
                # Verificar si hay tareas pendientes después de planificar
                pending = [t for t in self.state.get("plan", []) if t.get("status") == "pending"]
                if not pending:
                    logger.debug("No hay tareas pendientes, esperando...")
                    self._stop_event.wait(5)
                    continue
                
                # 2. EJECUTAR
                self._execute()
                
                # 3. OBSERVAR Y REFLEXIONAR
                self._reflect()
                
                # 4. ADAPTAR (replanificar si es necesario)
                self._adapt()
                
                # 5. GUARDAR ESTADO
                self._save_state()
                
                # 6. CREAR CHECKPOINT AUTOMÁTICO
                if self.current_cycle % 5 == 0:
                    self._auto_checkpoint()
                
                # 7. NOTIFICAR PROGRESO
                if self.current_cycle % 10 == 0:
                    self._report_progress()
                
                # Esperar hasta el siguiente ciclo
                elapsed = time.time() - self.cycle_start_time
                wait_time = max(0, self.interval - elapsed)
                logger.debug(f"Ciclo {self.current_cycle} completado en {elapsed:.1f}s. Esperando {wait_time:.1f}s.")
                self._stop_event.wait(wait_time)
                
                # Verificar límite de ciclos
                if self.max_cycles != -1 and self.current_cycle >= self.max_cycles:
                    logger.info(f"Límite de {self.max_cycles} ciclos alcanzado.")
                    break
                    
            except Exception as e:
                logger.error(f"Error en el ciclo {self.current_cycle}: {e}", exc_info=True)
                self._notify("⚠️ Error en el daemon", f"Error: {e}")
                self._stop_event.wait(self.interval * 2)  # Esperar más antes de reintentar
        
        self.is_running = False
        logger.info("🔄 Bucle del daemon finalizado.")

    # ========================================================================
    # MÉTODOS DEL CICLO DE VIDA (Plan → Act → Observe → Reflect → Adapt)
    # ========================================================================

    def _plan(self):
        """Genera o actualiza el plan si es necesario."""
        plan = self.state.get("plan", [])
        
        # Si no hay plan o está vacío, generar uno inicial
        if not plan:
            logger.info("📋 Generando plan inicial...")
            plan = self._generate_initial_plan()
            self.state["plan"] = plan
            self._save_state()
            if plan:
                self._notify("📋 Plan generado", f"Se ha generado un plan con {len(plan)} tareas.")
        
        # Si hay menos de 3 tareas pendientes, intentar expandir el plan
        pending = [t for t in plan if t.get("status") in ["pending", "in_progress"]]
        if len(pending) < 3 and len(plan) > 0:
            logger.info("📋 Expandiendo plan...")
            new_tasks = self._expand_plan()
            if new_tasks:
                self.state["plan"].extend(new_tasks)
                self._save_state()
                logger.info(f"✅ Plan expandido con {len(new_tasks)} tareas adicionales.")

    def _generate_initial_plan(self) -> List[Dict]:
        """Plan determinista por plantillas, con los agentes REALES del catálogo.

        Antes esto era una llamada al LLM con las especialidades hardcodeadas
        "Investigación", "Economía", "Código" y "General": agentes que ya no
        existen en el catálogo (se eliminaron en la limpieza del modo agente), así
        que el daemon planificaba con nombres fantasma y cada ciclo pagaba una
        generación completa para pedir un JSON que después había que reparar.

        Ahora el plan sale del mismo `pipeline_templates` que usa el modo AGENCY,
        con el sub-catálogo del dominio de la tarea. Si no hay catálogo, un único
        paso con el objetivo (el daemon sigue funcionando sin agentes Agency).
        """
        from . import agency_domains, pipeline_templates, task_router
        from .agency_catalog import create_default_catalog

        agents: List[Dict] = []
        root = None
        try:
            catalog = create_default_catalog()
            agents = catalog.load()
            root = getattr(catalog, "root", None)
        except Exception as exc:  # catálogo ausente o ilegible
            logger.warning(f"Sin catálogo Agency para planificar: {exc}")

        if not agents:
            logger.warning("Plan de un paso: no hay agentes en el catálogo")
            return [self._single_step_plan()]

        decision = task_router.decide(self.objective)
        candidates = agency_domains.select(agents, decision.get("domains"), root=root)
        plan = pipeline_templates.build_plan(
            self.objective,
            candidates,
            nature=decision["nature"],
            domains=decision.get("domains"),
            decompose=decision.get("requires_decomposition"),
        )
        steps = plan.get("steps") or []
        if not steps:
            logger.warning("Plan de un paso: la plantilla no devolvió pasos")
            return [self._single_step_plan()]

        logger.info(
            f"📋 Plan determinista ({plan.get('pipeline')}): {len(steps)} tareas"
        )
        return [
            {
                "id": str(step["id"]),
                "description": step["subtask"],
                "agent": step["agent"],
                "depends_on": list(step.get("depends_on") or []),
                "status": "pending",
            }
            for step in steps
        ]

    def _single_step_plan(self) -> Dict:
        """Plan mínimo y seguro para cuando no hay catálogo utilizable."""
        return {
            "id": "1",
            "description": self.objective,
            "agent": "General",
            "depends_on": [],
            "status": "pending",
        }

    def _expand_plan(self) -> List[Dict]:
        """Tarea correctiva determinista para el último fallo (sin LLM).

        Antes pedía al modelo una tarea de corrección y DESCARTABA la respuesta
        —el parseo quedaba en `pass`—, así que la "expansión" no hacía nada y
        costaba una llamada completa cada vez que quedaban menos de 3 pendientes.

        Acotado a propósito: como mucho una corrección por tarea fallida (id
        `<id>C`); si esa corrección también falla, no se encadena otra.
        """
        from . import pipeline_templates

        failed_tasks = self.state.get("failed_tasks", [])
        if not failed_tasks:
            return []
        last_failed = failed_tasks[-1]
        source_id = str(last_failed.get("id") or "")
        if not source_id:
            return []
        if f"{source_id}C" in {str(task.get("id")) for task in self.state.get("plan", [])}:
            return []
        recovery = pipeline_templates.recovery_step(
            {
                "id": source_id,
                "subtask": last_failed.get("description", ""),
                "agent_name": last_failed.get("agent", ""),
            },
            last_failed.get("error", ""),
        )
        return [{
            "id": f"{source_id}C",
            "description": recovery["subtask"],
            "agent": recovery["agent_name"] or last_failed.get("agent") or "General",
            "depends_on": [],
            "status": "pending",
        }]

    def _build_task_agent(self, agent_name: str):
        """Agente para un paso del plan, con el system prompt que usa el resto del sistema.

        Antes se creaba un `Agent` con una instrucción de una línea y sin
        `workspace_path`: el WorkspaceIndex y el gestor de skills caían a
        `Path.cwd()`, y el workspace no aparecía en el prompt. Se construye
        siempre un agente NUEVO (el historial de un agente registrado se
        compartiría entre el chat y el daemon) pero con las instrucciones del
        catálogo Agency cuando el nombre coincide.
        """
        instructions = None
        try:
            from .agency_catalog import create_default_catalog

            instructions = create_default_catalog().instructions(agent_name)
        except Exception as exc:  # catálogo ausente o nombre desconocido
            logger.debug(f"Sin catálogo Agency para '{agent_name}': {exc}")
        if not instructions:
            instructions = f"Eres un asistente especializado en {agent_name}."
        instructions += (
            f"\n\nWORKSPACE: {self.workspace_path}\n"
            "- Trabaja SIEMPRE con rutas relativas al workspace.\n"
            "- No escribas fuera del workspace.\n"
        )
        agent = Agent(
            name=agent_name,
            instructions=instructions,
            llm_client=self.orchestrator.llm_client,
            max_iterations=10,
            orchestrator=self.orchestrator,
            reasoning=self.orchestrator.reasoning,
            workspace_path=str(self.workspace_path),
        )
        return agent

    def _execute(self):
        """Ejecuta las tareas pendientes usando run_stream (no run bloqueante)
        para que los eventos lleguen en tiempo real al bus del daemon."""
        plan = self.state.get("plan", [])
        pending = [t for t in plan if t.get("status") == "pending"]
        
        if not pending:
            logger.debug("No hay tareas pendientes.")
            return
        
        tasks_to_run = pending[:3]
        logger.info(f"🔧 Ejecutando {len(tasks_to_run)} tarea(s)...")
        
        # El bus puede ser compartido con la CLI para publicar los eventos en
        # tiempo real; en ejecuciones independientes se usa el bus interno.
        daemon_bus = self.event_bus
        def _forward_to_daemon(evt: AgentEvent):
            logger.info(f"[Daemon Event] {evt.type}: {str(evt.payload)[:100]}")
        daemon_bus.on(_forward_to_daemon)
        
        for task in tasks_to_run:
            task_id = task.get("id")
            description = task.get("description", "")
            agent_name = task.get("agent", "General")
            dependencies = {str(item) for item in (task.get("depends_on") or [])}
            completed = {
                str(item.get("id")): item
                for item in self.state.get("completed_tasks", [])
                if isinstance(item, dict)
            }
            if not dependencies <= completed.keys():
                continue

            handoff = [
                {
                    "id": dependency,
                    "description": completed[dependency].get("description", ""),
                    "result": completed[dependency].get("result", ""),
                }
                for dependency in dependencies
            ]
            context = json.dumps(
                {"goal": self.objective, "dependencies": handoff},
                ensure_ascii=False,
            )
            
            try:
                agent = self._build_task_agent(agent_name)
                
                # Ejecutar con run_stream para recibir eventos en tiempo real
                step_bus = EventBus()
                def _bridge(evt: AgentEvent):
                    daemon_bus.emit(evt)
                step_bus.on(_bridge)
                
                result = agent.run_stream(
                    description,
                    context=context,
                    bus=step_bus,
                    reasoning=self.orchestrator.reasoning,
                )
                step_bus.off(_bridge)
                
                # Asegurar que el resultado nunca sea None
                result = result or ""
                
                task["status"] = "completed"
                self.state["completed_tasks"].append({
                    "id": task_id,
                    "description": description,
                    "result": result[:500],
                    "completed_at": datetime.now().isoformat()
                })
                logger.info(f"✅ Tarea {task_id} completada: {result[:100]}...")
                
            except Exception as e:
                task["status"] = "failed"
                self.state["failed_tasks"].append({
                    "id": task_id,
                    "description": description,
                    "error": str(e),
                    "failed_at": datetime.now().isoformat()
                })
                logger.error(f"❌ Tarea {task_id} falló: {e}")

            daemon_bus.off(_forward_to_daemon)
        
        self._save_state()

    def _reflect(self):
        """Analiza los resultados y genera lecciones aprendidas."""
        failed = self.state.get("failed_tasks", [])
        if not failed:
            return
        
        last_failed = failed[-1]
        lesson = {
            "task": last_failed.get("description"),
            "error": last_failed.get("error"),
            "timestamp": datetime.now().isoformat(),
            "suggestion": self._generate_suggestion(last_failed)
        }
        
        if "lessons" not in self.state:
            self.state["lessons"] = []
        self.state["lessons"].append(lesson)
        logger.info(f"📝 Lección registrada: {lesson['suggestion'][:80]}...")
        
        self._try_generate_skill(lesson)

    def _generate_suggestion(self, failed_task: Dict) -> str:
        """Genera una sugerencia de mejora basada en el error."""
        prompt = f"""
La siguiente tarea falló:
- Descripción: {failed_task.get('description')}
- Error: {failed_task.get('error')}

Genera una sugerencia breve (1-2 frases) de qué podría hacerse diferente la próxima vez.
"""
        messages = [{"role": "user", "content": prompt}]
        response = self.orchestrator.llm_client.chat(messages)
        if "error" not in response:
            return response.get("content", "Revisar el error y ajustar el enfoque.")
        return "Revisar el error y ajustar el enfoque."

    def _try_generate_skill(self, lesson: Dict):
        """Intenta generar un skill a partir de una lección."""
        similar = [l for l in self.state.get("lessons", []) if l.get("task") == lesson.get("task")]
        if len(similar) >= 3:
            skill_name = f"fix-{lesson['task'][:30].replace(' ', '-')}"
            steps = self._generate_skill_steps(lesson)
            if steps:
                self.skill_manager.create_skill(
                    name=skill_name,
                    description=f"Skill para resolver: {lesson['task'][:50]}...",
                    steps=steps,
                    tags=["auto-generated", "daemon"],
                    auto_generated=True
                )
                logger.info(f"🧠 Skill generado: {skill_name}")

    def _generate_skill_steps(self, lesson: Dict) -> List[Dict]:
        """Genera pasos para un skill a partir de una lección."""
        return [
            {
                "tool": "analyze_error",
                "arguments": {"error": lesson.get("error")},
                "description": "Analizar el error"
            },
            {
                "tool": "fix_issue",
                "arguments": {"error": lesson.get("error")},
                "description": "Aplicar corrección"
            }
        ]

    def _check_sensors(self):
        """Verifica sensores y actualiza el plan si hay nuevos eventos."""
        if not self.sensor_manager:
            return
        
        try:
            # Obtener eventos de sensores
            events = self.sensor_manager.get_pending_events()
            if events:
                logger.info(f"📡 {len(events)} eventos de sensores detectados")
                for event in events:
                    self._on_sensor_event(event)
        except Exception as e:
            logger.error(f"Error verificando sensores: {e}")

    def _evaluate_goals(self):
        """Evalúa el estado de los goals y dispara notificaciones si es necesario."""
        if not self.goal_manager:
            return
        
        try:
            # Obtener todos los goals
            goals = self.goal_manager.get_all_goals()
            
            for goal in goals:
                goal_id = goal.get("id")
                status = goal.get("status")
                
                # Evaluar progreso
                summary = self.goal_manager.get_goal_summary(goal_id)
                
                # Notificar si goal completado
                if status == "in_progress" and summary.get("average_progress", 0) >= 100:
                    if self.notification_manager:
                        self.notification_manager.notify_goal_reached(goal_id, goal.get("title"))
                    logger.info(f"✅ Goal completado: {goal.get('title')}")
                
                # Notificar si goal fallido
                elif status == "in_progress" and summary.get("stalled", False):
                    if self.notification_manager:
                        self.notification_manager.notify_goal_failed(goal_id, goal.get("title"))
                    logger.warning(f"❌ Goal atascado: {goal.get('title')}")
        
        except Exception as e:
            logger.error(f"Error evaluando goals: {e}")

    def _adapt(self):
        """Replanifica si es necesario."""
        failed = self.state.get("failed_tasks", [])
        if len(failed) >= 3:
            recent_failures = failed[-3:]
            if all(f.get("description") == recent_failures[0].get("description") for f in recent_failures):
                logger.warning("⚠️ Múltiples fallos consecutivos en la misma tarea. Replanificando...")
                self._replan()

    def _replan(self):
        """Replanifica el proyecto desde el estado actual."""
        logger.info("🔄 Replanificando...")
        self._auto_checkpoint()
        new_tasks = self._generate_initial_plan()
        completed_ids = [t.get("id") for t in self.state.get("completed_tasks", [])]
        filtered_plan = [t for t in new_tasks if t.get("id") not in completed_ids]
        self.state["plan"] = filtered_plan
        self.state["failed_tasks"] = []
        self._notify("🔄 Plan actualizado", "El plan ha sido replanificado debido a fallos consecutivos.")
        self._save_state()

    def _is_objective_completed(self) -> bool:
        """Verifica si el objetivo se ha completado (todas las tareas completadas)."""
        plan = self.state.get("plan", [])
        if not plan:
            return False
        all_done = all(t.get("status") == "completed" for t in plan)
        return all_done

    # ========================================================================
    # UTILIDADES
    # ========================================================================

    def _auto_checkpoint(self):
        """Crea un checkpoint automático."""
        checkpoint_id = self.checkpoint_manager.create_checkpoint(
            description=f"Daemon checkpoint - ciclo {self.current_cycle}",
            state=self.state
        )
        logger.debug(f"📸 Checkpoint creado: {checkpoint_id}")
        if "checkpoints" not in self.state:
            self.state["checkpoints"] = []
        self.state["checkpoints"].append(checkpoint_id)
        self._save_state()

    def _notify(self, title: str, message: str):
        """Envía notificaciones al usuario."""
        if self.enable_notifications:
            notify(title, message)
        if self.enable_telegram:
            send_telegram(f"*{title}*\n{message}")

    def _report_progress(self):
        """Genera y envía un informe de progreso."""
        completed = len(self.state.get("completed_tasks", []))
        failed = len(self.state.get("failed_tasks", []))
        total = len(self.state.get("plan", []))
        progress = f"Tareas: {completed} completadas, {failed} fallidas, {total - completed - failed} pendientes de {total}"
        self._notify("📊 Informe de progreso", progress)
        logger.info(f"📊 {progress}")

    # ========================================================================
    # MÉTODOS DE CONFIGURACIÓN
    # ========================================================================

    def set_interval(self, interval: int):
        """Actualiza el intervalo entre ciclos."""
        self.interval = interval
        logger.info(f"⏱️ Intervalo actualizado a {interval}s.")

    def set_max_cycles(self, max_cycles: int):
        """Actualiza el número máximo de ciclos."""
        self.max_cycles = max_cycles
        logger.info(f"🔄 Límite de ciclos actualizado a {'∞' if max_cycles == -1 else max_cycles}.")

# ============================================================================
# FUNCIONES AUXILIARES PARA CONTROL DEL DAEMON DESDE EL MANAGER
# ============================================================================

_daemon_instance = None

def get_daemon_instance() -> Optional[DaemonAgent]:
    """Devuelve la instancia global del daemon."""
    return _daemon_instance

def start_daemon(objective: str, workspace_path: str,
                 interval: int = DEFAULT_INTERVAL,
                 max_cycles: int = MAX_CYCLES,
                 enable_notifications: bool = True,
                 enable_telegram: bool = False,
                 event_bus: Optional[EventBus] = None) -> str:
    """
    Inicia el daemon en segundo plano.
    Retorna un mensaje de confirmación.
    """
    global _daemon_instance
    
    # Si ya existe una instancia, detenerla primero
    if _daemon_instance and _daemon_instance.is_running:
        _daemon_instance.stop()
        time.sleep(1)
    
    _daemon_instance = DaemonAgent(
        objective=objective,
        workspace_path=workspace_path,
        interval=interval,
        max_cycles=max_cycles,
        enable_notifications=enable_notifications,
        enable_telegram=enable_telegram,
        event_bus=event_bus,
    )
    _daemon_instance.start()
    return f"✅ Daemon iniciado para objetivo: {objective[:100]}...\n⏱️ Intervalo: {interval}s"

def stop_daemon() -> str:
    """Detiene el daemon en ejecución."""
    global _daemon_instance
    if not _daemon_instance or not _daemon_instance.is_running:
        return "⚠️ No hay daemon en ejecución."
    _daemon_instance.stop()
    return "🛑 Daemon detenido."

def daemon_status() -> str:
    """Devuelve el estado actual del daemon."""
    global _daemon_instance
    if not _daemon_instance:
        return "⚠️ Daemon no inicializado."
    status = _daemon_instance.get_status()
    output = f"""
📊 **Estado del Daemon**
• Estado: {'🟢 En ejecución' if status['is_running'] else '🔴 Detenido'}
• Pausado: {'✅' if status['is_paused'] else '❌'}
• Ciclo actual: {status['current_cycle']}
• Tareas completadas: {status['completed_tasks']}
• Tareas fallidas: {status['failed_tasks']}
• Tareas planificadas: {status['plan_length']}
• Última actualización: {status['last_updated']}
"""
    return output