"""Helpers del agente compartidos. Sin servidor HTTP propio.

La app Flask del puerto 8089 se eliminó hace tiempo: era un segundo servidor
con rutas duplicadas de `dev_agent`. El API de agentes vive hoy en
`tools/dev_agent.py` (FastMCP, puerto 15000), con `/agent_execute`,
`/agent_events/{job_id}` y `/agent_jobs`.

Lo que QUEDA aquí es exactamente lo que el resto del proyecto importa:

* `get_orchestrator()`     -> instancia única de Orchestrator con su estado.
* `_run_selected_agents()` -> wrapper del modo AGENCY sobre ese orquestador.

Se eliminaron la cola de jobs en memoria, la cola SSE, los sensores del
sistema, el estado del daemon y la persistencia de jobs: tenían 0 llamadas
fuera del módulo y su equivalente vivo está en `dev_agent`, que mantiene sus
propios `_agency_jobs` y su propio framing SSE.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

try:
    from .agent_core import EventBus, LLMClient, Orchestrator
except Exception:
    from tools.agent_core import EventBus, LLMClient, Orchestrator

try:
    from . import runtime
except Exception:
    import runtime


logger = logging.getLogger("mcp_server")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def _resolve_workspace_from_config() -> Path:
    """Workspace activo, leído de config.json (misma fuente que dev_agent).

    Orden de precedencia:
      1. MCP_WORKSPACE (tests / orquestación externa).
      2. config.json -> defaults.workspace_path (fuente de verdad).
      3. RuntimeConfig.get_workspace_abs().
      4. Path.cwd() como último recurso.

    Sin esto el fallback `Path.cwd()` ponía la carpeta del ejecutable como
    workspace y `_start_workspace_indexing()` indexaba el árbol entero del exe
    (app_data/, user_data/, cachés...), que es de donde venían los picos de
    CPU y disco.
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
# ORCHESTRATOR SINGLETON Y PERSISTENCIA
# =============================================================================

USER_DATA_DIR = (
    Path(runtime.get_user_data_dir()) if hasattr(runtime, "get_user_data_dir") else Path.cwd()
)
ORCHESTRATOR_STATE_FILE = str(USER_DATA_DIR / "orchestrator_state.json")

# Orchestrator singleton (persistente durante la vida del servidor).
_orchestrator: Optional[Orchestrator] = None
_orchestrator_lock = threading.Lock()


def get_orchestrator() -> Orchestrator:
    """Devuelve el Orchestrator singleton, creándolo si es necesario."""
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is None:
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
    """Restaura el workspace guardado, ignorando valores sospechosos."""
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


# =============================================================================
# EJECUCION DE TAREAS CON ORCHESTRATOR
# =============================================================================

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
