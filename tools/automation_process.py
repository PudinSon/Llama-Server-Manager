"""Ciclo de vida del proceso del servicio de automatización 24/7 (puerto 15001).

El servicio vive en su **propio proceso** para que sobreviva al cierre de la
web view: el visor embebido y el panel web solo son clientes HTTP, así que la
cola, los dispatchers y los webhooks siguen trabajando aunque nadie los mire.
Pero es **hijo del manager**: el manager lo arranca en su secuencia de
servicios y lo detiene al cerrarse, porque sin manager no hay modelo cargado y
sin modelo ningún dispatcher tendría algo que ejecutar. Si el manager muere de
golpe (kill, cierre forzado), el watchdog `--parent-pid` del propio servicio lo
cierra al detectar que su padre ya no existe.

Antes nadie arrancaba este proceso: había que lanzar a mano
`python -m tools.automation_service` y hasta entonces la pestaña de
automatización de la UI solo podía responder «failed to fetch». Este módulo es
la única puerta de arranque/parada, compartida por el manager de escritorio y
el panel web.

    from tools.automation_process import start, stop, status

    start(parent_pid=os.getpid())   # idempotente: si ya escucha, no lanza nada
    stop()                          # mata el árbol del proceso y libera el puerto
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # paquete
    from . import runtime
    from . import process_manager
except ImportError:  # pragma: no cover - ejecución suelta
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools import runtime  # type: ignore
    from tools import process_manager  # type: ignore

#: Puerto por defecto del servicio (el propio servicio lee el mismo entorno).
DEFAULT_PORT = int(os.environ.get("AUTOMATION_PORT", "15001"))
LOG_NAME = "automation_service.log"

#: Handle del proceso cuando lo arrancó ESTE proceso (permite matarlo por PID
#: aunque el sondeo del puerto falle).
_process: Optional[subprocess.Popen] = None


def port() -> int:
    """Puerto configurado (respeta `AUTOMATION_PORT`)."""
    return int(os.environ.get("AUTOMATION_PORT", str(DEFAULT_PORT)))


def log_path() -> Path:
    """Ruta del log del servicio (bajo `user_data`, visible para el usuario)."""
    return Path(runtime.get_user_data_dir()) / LOG_NAME


def build_command(
    port_number: Optional[int] = None,
    workspace: Optional[str] = None,
    parent_pid: Optional[int] = None,
) -> List[str]:
    """Comando para arrancar el servicio (fuente única para ambos managers).

    En build congelado (PyInstaller) se reusa el subcomando del propio
    ejecutable, igual que el resto de servicios del manager.
    """
    args = ["--port", str(int(port_number or port()))]
    if parent_pid:
        args += ["--parent-pid", str(int(parent_pid))]
    if workspace:
        args += ["--workspace", str(workspace)]

    if getattr(sys, "frozen", False):  # pragma: no cover - solo en el binario
        return [sys.executable, "--run-automation-service", *args]

    return [str(runtime.get_python_exe()), "-m", "tools.automation_service", *args]


def is_running(port_number: Optional[int] = None) -> bool:
    """¿Hay algo escuchando en el puerto del servicio?"""
    try:
        return bool(process_manager.is_port_in_use(int(port_number or port())))
    except Exception:
        return False


def status(port_number: Optional[int] = None) -> Dict[str, Any]:
    """Ficha del servicio para los managers y para el log de arranque."""
    number = int(port_number or port())
    running = is_running(number)
    alive = bool(_process is not None and _process.poll() is None)

    return {
        "log": str(log_path()),
        "pid": _process.pid if alive else None,
        "port": number,
        "running": running,
        "url": f"http://127.0.0.1:{number}",
    }


def log_tail(lines: int = 8, max_chars: int = 4000) -> str:
    """Últimas líneas del log: diagnóstico cuando el arranque falla."""
    try:
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    tail = "\n".join(text.splitlines()[-max(1, int(lines)) :])
    return tail[-max_chars:]


def start(
    port_number: Optional[int] = None,
    workspace: Optional[str] = None,
    parent_pid: Optional[int] = None,
    wait: float = 25.0,
) -> Dict[str, Any]:
    """Arranca el servicio si no está ya escuchando (idempotente).

    Devuelve `{"success": bool, "status": "started"|"already_running"|"starting"|"failed"}`
    (`starting` = el proceso vive pero el puerto todavía no responde).
    Nunca levanta una segunda instancia: si el puerto ya responde, se respeta la
    que esté corriendo (pudo arrancarla el manager o el panel web).
    """
    global _process

    number = int(port_number or port())

    if is_running(number):
        return {"port": number, "status": "already_running", "success": True}

    command = build_command(number, workspace=workspace, parent_pid=parent_pid)
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env["AUTOMATION_PORT"] = str(number)
    # El watchdog del servicio vigila a QUIEN lo arranca: sin esto, un manager
    # cerrado a lo bruto dejaría la automatización huérfana y sin modelo.
    if parent_pid:
        env["AUTOMATION_PARENT_PID"] = str(int(parent_pid))
    base = str(runtime.get_base_dir())
    env["PYTHONPATH"] = os.pathsep.join([p for p in (base, env.get("PYTHONPATH", "")) if p])

    log = log_path()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log, "ab", buffering=0)
    except Exception as error:
        return {"error": f"no se pudo abrir el log {log}: {error}", "port": number, "status": "failed", "success": False}

    kwargs: Dict[str, Any] = {
        "cwd": base,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": handle,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:  # pragma: no cover - POSIX
        kwargs["start_new_session"] = True

    try:
        _process = subprocess.Popen(command, **kwargs)
    except Exception as error:
        return {"error": str(error), "port": number, "status": "failed", "success": False}
    finally:
        try:
            handle.close()
        except Exception:
            pass

    deadline = time.time() + max(1.0, float(wait))
    while time.time() < deadline:
        if _process.poll() is not None:
            return {
                "error": f"el servicio terminó al arrancar (código {_process.returncode})",
                "exit_code": _process.returncode,
                "log": str(log),
                "log_tail": log_tail(),
                "pid": _process.pid,
                "port": number,
                "status": "failed",
                "success": False,
            }
        if is_running(number):
            return {
                "log": str(log),
                "pid": _process.pid,
                "port": number,
                "status": "started",
                "success": True,
                "url": f"http://127.0.0.1:{number}",
            }
        time.sleep(0.25)

    # El proceso vive pero aún no escucha: el dispatcher puede tardar (carga
    # Chroma, abre su base de datos…) y matarlo sería peor que esperarlo.
    return {
        "log": str(log),
        "pid": _process.pid,
        "port": number,
        "status": "starting",
        "success": True,
        "url": f"http://127.0.0.1:{number}",
    }


def stop(port_number: Optional[int] = None, wait: float = 3.0) -> Dict[str, Any]:
    """Detiene el servicio y libera su puerto (best-effort, nunca lanza)."""
    global _process

    number = int(port_number or port())

    if _process is not None:
        try:
            if _process.poll() is None:
                process_manager.kill_process_tree(_process.pid, timeout=wait)
        except Exception:
            pass
        _process = None

    if is_running(number):
        try:
            process_manager.stop_server(port=number, wait=wait)
        except Exception:
            pass

    stopped = not is_running(number)

    return {
        "port": number,
        "status": "stopped" if stopped else "failed",
        "success": stopped,
    }
