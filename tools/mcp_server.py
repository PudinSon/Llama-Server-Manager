"""Helpers del agente compartidos (SIN servidor HTTP propio).

Este módulo ya no expone endpoints: la app Flask del puerto 8089 se eliminó
(era un segundo servidor con rutas duplicadas de dev_agent). El API de agentes
vive en `tools/dev_agent.py` (FastMCP, puerto 15000).

Se conserva porque `dev_agent.py` importa de aquí el orquestador compartido:

* `get_orchestrator()`      -> instancia única de Orchestrator con estado persistido.
* `_run_selected_agents()`  -> wrapper del modo AGENCY sobre ese orquestador.

El resto de utilidades (persistencia de jobs, cola de eventos SSE, sensores del
sistema y estado del daemon) las usan esas dos funciones; ya no tienen rutas
HTTP asociadas.
"""
import threading
import time
import uuid
import os
from typing import Optional, Dict, List, Any
import json
import logging
from datetime import datetime
from pathlib import Path
import queue

try:
    from .agency_catalog import create_default_catalog
    from .parallel_executor import ParallelExecutor
except Exception:
    from tools.agency_catalog import create_default_catalog
    from tools.parallel_executor import ParallelExecutor

# =============================================================================
# IMPORTS DEL PROYECTO (compatibles con ejecucion directa y como modulo)
# =============================================================================
try:
    from .agent_core import Agent, EventBus, LLMClient, Orchestrator, AgentEvent
except Exception:
    from tools.agent_core import Agent, EventBus, LLMClient, Orchestrator, AgentEvent

try:
    from .checkpoint_manager import CheckpointManager
except Exception:
    from tools.checkpoint_manager import CheckpointManager

try:
    from .episodic_memory import EpisodicMemory
except Exception:
    EpisodicMemory = None

try:
    from .memory_skills import SkillManager
except Exception:
    SkillManager = None

try:
    from . import runtime
except Exception:
    import runtime

# Daemon imports (lazy para evitar errores si no existe)
_daemon_module = None
try:
    from . import daemon_agent as _daemon_module
except Exception:
    try:
        import daemon_agent as _daemon_module
    except Exception:
        _daemon_module = None

# System sensors (lazy import)
_system_sensors = None
try:
    from .sensors import SensorManager, SensorEvent
except Exception:
    try:
        from sensors import SensorManager, SensorEvent
    except Exception:
        SensorManager = None
        SensorEvent = None


# Detrás de nginx (si algún día se usa este servidor legacy): respeta X-Forwarded-*
def _resolve_workspace_from_config() -> Path:
    """Workspace activo, leído de config.json (misma fuente que dev_agent y
    workspace_server).

    Antes get_orchestrator() usaba `Path.cwd()` como fallback, y el manager
    arranca dev_agent con cwd = carpeta del exe. Resultado: el Orchestrator
    recibía la raíz del exe como workspace y los checkpoints AGENCY/PLAN
    copiaban user_data/webui_profiles/.../EBWebView/... paso a paso.

    Orden de precedencia:
      1. MCP_WORKSPACE (tests / orquestación externa).
      2. config.json → defaults.workspace_path (fuente de verdad).
      3. RuntimeConfig.get_workspace_abs().
      4. Path.cwd() como último recurso.
    """
    env_ws = os.environ.get("MCP_WORKSPACE", "").strip()
    if env_ws:
        return Path(env_ws).expanduser().resolve()

    try:
        cfg_path = Path(runtime.get_user_data_dir()) / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
            defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
            mode = defaults.get("workspace_mode") or data.get("workspace_mode") or "default"
            if mode == "global":
                return Path(os.path.abspath(os.sep))
            raw = defaults.get("workspace_path") or data.get("workspace")
            if raw:
                candidate = Path(str(raw)).expanduser()
                if not candidate.is_absolute():
                    candidate = Path(runtime.get_base_dir()) / candidate
                return candidate.resolve()
    except Exception as exc:
        logger.warning(f"No se pudo leer workspace de config.json: {exc}")

    try:
        config_cls = getattr(runtime, "RuntimeConfig", None)
        if config_cls is not None:
            return Path(str(config_cls.get_workspace_abs())).resolve()
    except Exception as exc:
        logger.warning(f"RuntimeConfig no disponible: {exc}")

    return Path.cwd()

# =============================================================================
# CONFIGURACION Y PATHS
# =============================================================================
USER_DATA_DIR = Path(runtime.get_user_data_dir()) if hasattr(runtime, 'get_user_data_dir') else Path.cwd()
JOBS_FILE = str(USER_DATA_DIR / "mcp_jobs.json")
ORCHESTRATOR_STATE_FILE = str(USER_DATA_DIR / "orchestrator_state.json")

# Simple in-memory job store
JOBS: Dict[str, dict] = {}

# SSE queues por job_id: cada job tiene una queue de eventos para SSE
SSE_QUEUES: Dict[str, queue.Queue] = {}
SSE_LOCK = threading.Lock()

# Orchestrator singleton (persistente durante la vida del servidor)
_orchestrator: Optional[Orchestrator] = None
_orchestrator_lock = threading.Lock()

# =============================================================================
# LOGGER
# =============================================================================
logger = logging.getLogger("mcp_server")
if not logger.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    h.setFormatter(fmt)
    logger.addHandler(h)
logger.setLevel(logging.INFO)


def _run_selected_agents(
    task: str,
    selected_names: list,
    context: Any,
    bus: EventBus,
    cancel_event: Optional[threading.Event] = None,
    parallel: bool = True,
    selection_mode: str = "manual",
) -> str:
    """Compatibility wrapper around the central Orchestrator AGENCY mode."""
    orchestrator = get_orchestrator()
    if cancel_event is not None:
        orchestrator._cancelled = cancel_event
    return orchestrator.run_task_stream(
        task,
        bus=bus,
        reasoning=True,
        selected_agents=selected_names,
        context=context if isinstance(context, (dict, str)) else None,
        parallel=parallel,
        selection_mode=selection_mode,
    )


def _now_iso():
    return datetime.utcnow().isoformat() + "Z"


# =============================================================================
# PERSISTENCIA DE JOBS
# =============================================================================

def load_jobs():
    global JOBS
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            JOBS.clear()
            for k, v in data.items():
                if isinstance(v, dict):
                    restored = {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                    if restored.get("status") == "running":
                        restored["status"] = "interrupted"
                    JOBS[k] = restored
    except Exception:
        pass


def save_jobs():
    try:
        os.makedirs(os.path.dirname(JOBS_FILE) or ".", exist_ok=True)
        serializable = {}
        for k, v in JOBS.items():
            serializable[k] = {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# =============================================================================
# ORCHESTRATOR SINGLETON Y PERSISTENCIA
# =============================================================================

def get_orchestrator() -> Orchestrator:
    """Devuelve el Orchestrator singleton, creándolo si es necesario."""
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is None:
            # Antes: `os.environ.get("MCP_WORKSPACE", str(Path.cwd()))`.
            # El manager arranca dev_agent con cwd = carpeta del exe, así que
            # el fallback ponía `C:\LlamaManager` como workspace y
            # `_start_workspace_indexing()` empezaba a indexar el árbol ENTERO
            # del exe (app_data/, user_data/, cachés...). Ese hilo es el que
            # producía los picos de CPU/disco.
            #
            # `_resolve_workspace_from_config()` lee la MISMA fuente que
            # `dev_agent.get_workspace()` y `workspace_server.workspace_root()`
            # (config.json → defaults.workspace_path), así que los tres
            # componentes coinciden.
            workspace = str(_resolve_workspace_from_config())
            base_url = os.environ.get("MCP_LLM_URL", "http://127.0.0.1:8081")
            llm = LLMClient(base_url=base_url, timeout=120)
            _orchestrator = Orchestrator(
                llm_client=llm,
                workspace_path=workspace,
                enable_checkpoints=True,
                reasoning=True,
            )
            _load_orchestrator_state(_orchestrator)
            logger.info(f"Orchestrator creado: workspace={workspace}")
        return _orchestrator


def _load_orchestrator_state(orch: Orchestrator):
    try:
        if not os.path.exists(ORCHESTRATOR_STATE_FILE):
            return
        with open(ORCHESTRATOR_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f) or {}
        saved_workspace = state.get("workspace_path")
        if saved_workspace:
            saved_path = Path(saved_workspace)
            same_as_config = str(saved_path.resolve()) == str(orch.workspace_path.resolve())
            looks_like_exe_root = (
                str(saved_path.resolve()) == str(runtime.get_base_dir().resolve())
            )
            if saved_path.exists() and not same_as_config and not looks_like_exe_root:
                orch.workspace_path = saved_path
                logger.info(f"Workspace restaurado desde state: {saved_path}")
            elif looks_like_exe_root and not same_as_config:
                logger.warning(
                    f"Ignorado workspace sospechoso en state.json: {saved_path} "
                    f"(coincide con la raíz del exe). Se usa {orch.workspace_path}."
                )
    except Exception as e:
        logger.warning(f"No se pudo cargar estado del orchestrator: {e}")


def _save_orchestrator_state(orch: Orchestrator):
    """Guarda el estado actual del orchestrator."""
    try:
        os.makedirs(os.path.dirname(ORCHESTRATOR_STATE_FILE) or ".", exist_ok=True)
        state = {
            "workspace_path": str(orch.workspace_path),
            "reasoning": orch.reasoning,
            "enable_checkpoints": orch.enable_checkpoints,
            "agents": [agent.to_dict() for agent in orch.agents.values()],
            "saved_at": _now_iso(),
        }
        with open(ORCHESTRATOR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"No se pudo guardar estado del orchestrator: {e}")


# =============================================================================
# SYSTEM SENSORS (CPU, RAM, disco, GPU)
# =============================================================================

def _get_system_sensors() -> dict:
    """Devuelve metricas del sistema. Compatible Win/Linux."""
    result = {
        "timestamp": _now_iso(),
        "cpu_percent": None,
        "cpu_count": None,
        "memory": {},
        "disk": {},
        "gpu": [],
        "platform": os.name,
    }
    psutil = None
    try:
        import psutil
        result["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        result["cpu_count"] = psutil.cpu_count(logical=True)
        mem = psutil.virtual_memory()
        result["memory"] = {
            "total_gb": round(mem.total / (1024**3), 2),
            "available_gb": round(mem.available / (1024**3), 2),
            "percent": mem.percent,
            "used_gb": round(mem.used / (1024**3), 2),
        }
        disk = psutil.disk_usage(str(Path.cwd()))
        result["disk"] = {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "percent": round(disk.used / disk.total * 100, 1),
        }
    except Exception as e:
        logger.debug(f"psutil no disponible para sensores: {e}")

    # GPU (NVIDIA via pynvml)
    try:
        import pynvml
        pynvml.nvmlInit()
        device_count = pynvml.nvmlDeviceGetCount()
        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            result["gpu"].append({
                "index": i,
                "name": name,
                "memory_total_mb": round(mem_info.total / (1024**2), 1),
                "memory_used_mb": round(mem_info.used / (1024**2), 1),
                "memory_free_mb": round(mem_info.free / (1024**2), 1),
                "gpu_utilization": util.gpu,
                "memory_utilization": util.memory,
            })
        pynvml.nvmlShutdown()
    except Exception:
        pass

    # Temperaturas cuando el sistema las expone.
    if psutil is not None and hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                result["temperatures"] = {}
                for name, entries in temps.items():
                    result["temperatures"][name] = [
                        {"label": e.label, "current": e.current, "high": e.high, "critical": e.critical}
                        for e in entries
                    ]
        except Exception:
            pass

    return result


# =============================================================================
# UTILIDADES SSE
# =============================================================================

def _enqueue_event(job_id: str, ev_type: str, payload: Any, meta: Optional[dict] = None):
    """Pone un evento en la cola SSE del job."""
    with SSE_LOCK:
        q = SSE_QUEUES.get(job_id)
    if q is not None:
        event = {
            "seq": time.time(),
            "timestamp": _now_iso(),
            "type": ev_type,
            "payload": payload,
            "meta": meta or {},
        }
        try:
            q.put(event, block=False)
        except queue.Full:
            pass


def add_event_to_job(job: dict, ev_type: str, payload: dict, meta: Optional[dict] = None):
    meta = meta or {}
    seq = job.get("_seq", 0) + 1
    job["_seq"] = seq
    event = {
        "seq": seq,
        "timestamp": _now_iso(),
        "type": ev_type,
        "payload": payload,
        "meta": meta,
    }
    job.setdefault("events", []).append(event)
    job_id = job.get("id")
    if job_id:
        _enqueue_event(job_id, ev_type, payload, meta)
    try:
        save_jobs()
    except Exception:
        pass
    logger.info(f"Job {job.get('id')} event {ev_type} seq={seq}")
    return event


# =============================================================================
# EJECUCION DE TAREAS CON ORCHESTRATOR
# =============================================================================

def _run_agent_task_orchestrator(task: str, context: Optional[str], mode: str = "agent",
                                   bus: Optional[EventBus] = None,
                                   cancel_event: Optional[threading.Event] = None) -> dict:
    """Ejecuta una tarea usando el Orchestrator singleton."""
    local_bus = bus or EventBus()
    orch = get_orchestrator()

    if cancel_event is not None:
        orch._cancelled = cancel_event
        orch.reset_cancel()

    if cancel_event is not None:
        def _watch_cancel():
            cancel_event.wait()
            try:
                orch.stop()
            except Exception:
                pass
        twatch = threading.Thread(target=_watch_cancel, daemon=True)
        twatch.start()

    try:
        result = orch.run_task_stream(task, bus=local_bus, reasoning=True)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"Orchestrator task error: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        _save_orchestrator_state(orch)


def _run_agent_task_legacy(task: str, context: Optional[str], mode: str = "agent",
                             bus: Optional[EventBus] = None,
                             cancel_event: Optional[threading.Event] = None) -> dict:
    """Legacy: ejecuta via Agent aislado."""
    local_bus = bus or EventBus()
    agent = Agent(name="mcp-agent", instructions="Actua como asistente.", reasoning=True)
    try:
        from types import SimpleNamespace
        agent.orchestrator = SimpleNamespace(_cancelled=cancel_event or threading.Event())
    except Exception:
        agent.orchestrator = None
    base_url = os.environ.get("MCP_LLM_URL", "http://127.0.0.1:8081")
    try:
        agent.llm_client = LLMClient(base_url=base_url, timeout=120)
    except Exception:
        pass
    if cancel_event is not None:
        def _watch_cancel():
            cancel_event.wait()
            try:
                agent.llm_client.stop()
            except Exception:
                pass
            try:
                if hasattr(agent, 'orchestrator') and getattr(agent.orchestrator, '_cancelled', None) is not None:
                    agent.orchestrator._cancelled.set()
            except Exception:
                pass
        twatch = threading.Thread(target=_watch_cancel, daemon=True)
        twatch.start()
    content = agent.run_stream(task, context=context, bus=local_bus, reasoning=True, mode="default")
    return {"status": "ok", "result": content}


# =============================================================================
# RUTAS EXISTENTES (compatibilidad hacia atras)
# =============================================================================


def _get_daemon_status() -> dict:
    """Devuelve el estado del daemon sin importar el modulo."""
    status = {
        "initialized": False,
        "is_running": False,
        "is_paused": False,
        "current_cycle": 0,
        "objective": None,
        "plan_length": 0,
        "completed_tasks": 0,
        "failed_tasks": 0,
    }
    if _daemon_module is None:
        return status
    inst = _daemon_module.get_daemon_instance()
    if inst is None:
        return status
    try:
        s = inst.get_status()
        status.update({
            "initialized": True,
            "is_running": s.get("is_running", False),
            "is_paused": s.get("is_paused", False),
            "current_cycle": s.get("current_cycle", 0),
            "objective": s.get("objective"),
            "plan_length": s.get("plan_length", 0),
            "completed_tasks": s.get("completed_tasks", 0),
            "failed_tasks": s.get("failed_tasks", 0),
            "last_updated": s.get("last_updated"),
        })
    except Exception:
        pass
    return status
