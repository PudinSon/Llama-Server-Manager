try:
    from pathlib import Path
    import sys
    _THIS_DIR = Path(__file__).resolve().parent
    _PROJECT_ROOT = _THIS_DIR.parent
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
except Exception:
    pass

import os

# --- Lightweight logging helper API expected by Llama_Server_Manager ---
# (La gestión de procesos vive en tools/process_manager.py: start_server,
#  stop_server, kill_process_tree, close_process_gracefully.)
import logging
from logging.handlers import RotatingFileHandler

try:
    import tools.runtime as runtime
except Exception:
    runtime = None

try:
    if runtime:
        LOG_DIR = os.path.join(runtime.get_user_data_dir(), "logs")
    else:
        LOG_DIR = os.path.join(os.getcwd(), "logs")
except Exception:
    LOG_DIR = os.path.join(os.getcwd(), "logs")

os.makedirs(LOG_DIR, exist_ok=True)

_GUI_CALLBACK = None

def attach_gui_callback(cb):
    """Register a GUI callback that will receive log lines (string).

    The manager calls this to display log lines in the UI.
    """
    global _GUI_CALLBACK
    _GUI_CALLBACK = cb

def _emit_to_gui(line: str):
    try:
        if _GUI_CALLBACK:
            _GUI_CALLBACK(line)
    except Exception:
        pass


def get_logger(name: str = "llama_manager", level: int = logging.INFO):
    """Return a configured logger instance. Safe to call multiple times."""
    logger = logging.getLogger(name)
    if getattr(logger, "_configured_by_logger_py", False):
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    # Stream handler
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # Rotating file handler (best-effort)
    try:
        fh = RotatingFileHandler(os.path.join(LOG_DIR, f"{name}.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass

    # GUI handler - forwards formatted lines to registered GUI callback
    class _GuiHandler(logging.Handler):
        def emit(self, record):
            try:
                msg = self.format(record)
                _emit_to_gui(msg)
            except Exception:
                pass

    gh = _GuiHandler()
    gh.setFormatter(fmt)
    logger.addHandler(gh)

    logger._configured_by_logger_py = True
    return logger
