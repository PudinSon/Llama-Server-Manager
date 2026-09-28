# dev_agent.py
# Módulo de herramientas y servidor MCP con FastMCP
# Expone SOLO query_tools y execute_tool (más prompts).
# Todas las herramientas internas se ejecutan a través de execute_tool.
# Cada herramienta respeta los paths y la seguridad (safe_path, is_command_allowed).
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
import shlex
import sys
import io
import subprocess
import sqlite3
import json
import shutil
import tempfile
import threading
import time
import base64
import webbrowser
import psutil
import pyautogui
import pygetwindow as gw
import pyperclip
import re
import glob
import asyncio
import inspect
import uuid
from pathlib import Path
from typing import List, Dict, Optional, Callable, Any, get_type_hints
from functools import wraps
from datetime import datetime, timedelta
import logging
import numpy as np

# ---------- Importaciones necesarias para RAG y embeddings ----------
import chromadb
from chromadb.utils import embedding_functions
import requests

from tools.agent_core import Agent, LLMClient

try:
    from tools.memory_skills import get_skill_manager
except ImportError:
    from .memory_skills import get_skill_manager  # type: ignore

# ---------- Importación de runtime ----------
try:
    from . import runtime
except ImportError:
    try:
        import runtime
    except ImportError:
        class _DummyRuntime:
            @staticmethod
            def get_data_dir():
                return os.path.join(os.getcwd(), "app_data")
            @staticmethod
            def get_user_data_dir():
                return os.path.join(os.getcwd(), "user_data")
        runtime = _DummyRuntime()

# ---------- Jobs en segundo plano (procesos largos sin bloquear) ----------
try:
    from . import job_manager
except ImportError:
    try:
        from tools import job_manager
    except ImportError:  # pragma: no cover
        import job_manager  # type: ignore

# Forzar UTF-8 en Windows
if sys.platform == 'win32':
    if sys.stdout is not None:
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        except:
            pass
    if sys.stderr is not None:
        try:
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
        except:
            pass

# ---------- FastMCP ----------
FAST_MCP_AVAILABLE = False
try:
    from fastmcp import FastMCP
    FAST_MCP_AVAILABLE = True
except ImportError:
    FAST_MCP_AVAILABLE = False
except Exception:
    FAST_MCP_AVAILABLE = False

# ----- REDIRIGIR NLTK A APP_DATA -----
NLTK_DATA_DIR = Path(runtime.get_data_dir()) / "nltk_data"
NLTK_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["NLTK_DATA"] = str(NLTK_DATA_DIR)
os.environ["NLTK_DOWNLOAD_ATTEMPTS"] = "1"
try:
    import nltk
    nltk.data.path.insert(0, str(NLTK_DATA_DIR))
except:
    pass

try:
    import nltk
    nltk.download('punkt_tab', quiet=True)
except:
    pass

# ------------------------------------------------------------
# Rutas y configuración
# ------------------------------------------------------------
def get_base_dir():
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path.cwd()
    return base

try:
    USER_DATA_DIR = Path(runtime.get_user_data_dir())
except Exception:
    USER_DATA_DIR = get_base_dir() / "user_data"
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

ACTIVE_LIB_FILE = USER_DATA_DIR / "active_library.json"
CHROMA_DB_PATH = str(USER_DATA_DIR / "chroma_db")

def get_active_library():
    if ACTIVE_LIB_FILE.exists():
        try:
            with open(ACTIVE_LIB_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("active_collection")
        except:
            return None
    return None

def _set_active_library_state(name):
    with open(ACTIVE_LIB_FILE, "w", encoding="utf-8") as f:
        json.dump({"active_collection": name}, f)

CONFIG_DIR = USER_DATA_DIR
CONFIG_FILE = CONFIG_DIR / "config.json"
SECRETS_FILE = CONFIG_DIR / "secrets.json"
DEFAULT_WORKSPACE = get_base_dir() / "agente_workspace"

# ------------------------------------------------------------
# Verificación de librerías opcionales
# ------------------------------------------------------------
def safe_import(package, attr=None):
    try:
        if attr:
            return __import__(package, fromlist=[attr])
        return __import__(package)
    except ImportError:
        return None

ZIPFILE_AVAILABLE = safe_import('zipfile') is not None
PATOOL_AVAILABLE = safe_import('patoolib') is not None
IMAP_AVAILABLE = safe_import('imaplib') and safe_import('smtplib')
PLYER_AVAILABLE = safe_import('plyer') is not None
PDF2IMAGE_AVAILABLE = safe_import('pdf2image') is not None
REQUESTS_AVAILABLE = safe_import('requests') is not None
FEEDPARSER_AVAILABLE = safe_import('feedparser') is not None
WATCHDOG_AVAILABLE = safe_import('watchdog') is not None
WIN32PRINT_AVAILABLE = os.name == 'nt' and safe_import('win32print') is not None
CUPS_AVAILABLE = os.name == 'posix' and safe_import('cups') is not None
PYAUTOGUI_AVAILABLE = safe_import('pyautogui') is not None
PYPERCLIP_AVAILABLE = safe_import('pyperclip') is not None
PYWINDOW_AVAILABLE = safe_import('pygetwindow') is not None
PSUTIL_AVAILABLE = safe_import('psutil') is not None


# ------------------------------------------------------------
# Configuración persistente (flags optimizados)
# ------------------------------------------------------------
DEFAULT_SETTINGS = {
    "enable_files": True,
    "enable_python": True,
    "enable_db": True,
    "enable_todo": True,
    "enable_reminders": True,
    "enable_system_integration": True,
    "require_confirmation": True,
    "enable_notifications": True,
    "enable_webbrowser": True,
    "enable_gui": True,
    "enable_camera": True,
    "enable_telegram": True,
    "enable_processes": True,
    "enable_printing": True,
    "enable_download": True,
    "enable_git": True,
    "enable_watchdog": True,
    "enable_document_conversion": True,
    "enable_drives": True,
    "enable_classify": True,
    "enable_rag": True,
    "enable_memory": True,
    "enable_core_tools": True,
    "workspace_mode": "default",
    "workspace_path": str(DEFAULT_WORKSPACE),
    # Proyector visual (mmproj). Con mmproj cargado, llama.cpp DESACTIVA
    # --cache-reuse (aviso: "cache_reuse is not supported by multimodal").
    # Cada paso de AGENCY/PLAN reprocesa el prompt completo desde el token 0
    # → 5-10× más lento en CPU. Desactivar si los agentes solo procesan
    # texto, RAG o código.
    "enable_multimodal": True,
    # Router Laya ONNX: clasificador de modo/naturaleza (~650 MB, ~100× más
    # rápido que el ToolRouter de Gemma). Si el modelo no está descargado o
    # falla la carga, el orquestador cae a Gemma sin romper nada.
    "laya_enabled": True,      # activar el router Laya
    "laya_preload": False,     # cargar al arranque en vez de en la 1ª ruta
    # Herramientas permitidas por `execute` (lista blanca aplicada por el wrapper)
    "execute_allowed_tools": [
        "view", "read_file", "list_directory", "search", "tree", "query"
    ],
    
    # ========== NUEVA CONFIGURACIÓN DE SEGURIDAD ==========
    "use_blacklist": True,  # Cambiamos de "use_whitelist" a "use_blacklist"
    "blacklist_commands": [
        # ---- Linux / Unix ----
        "rm", "dd", "mkfs", "shutdown", "reboot", "halt", "init", 
        "killall", "pkill", "kill", "chmod", "chown", "mount", 
        "umount", "fdisk", "parted", "cryptsetup", "systemctl", 
        "poweroff", "sudo", "su", "userdel", "usermod", "groupdel",
        "passwd", "chage", "visudo", "iptables", "ufw",
        
        # ---- Windows ----
        "del", "erase", "format", "rd", "rmdir", "taskkill", 
        "shutdown", "reg", "regedit", "takeown", "icacls", "cacls",
        "net", "wmic", "cscript", "wscript", "powershell", "cmd",
        "diskpart", "bcdedit", "bootrec"
    ],
    # ========================================================
    "blacklist_paths": ["/etc/shadow", "/boot", "C:\\Windows\\System32\\config", "/root", "~/.ssh"],
}

def load_settings():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = DEFAULT_SETTINGS.copy()
            if isinstance(data, dict):
                if "defaults" in data and isinstance(data["defaults"], dict):
                    merged.update(data["defaults"])
                else:
                    merged.update(data)
            return merged
        except Exception:
            return DEFAULT_SETTINGS.copy()
    return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

def load_secrets():
    if not SECRETS_FILE.exists():
        return {}
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result = {}
        for k, v in raw.items():
            if not isinstance(v, str):
                result[k] = v
                continue
            try:
                missing = len(v) % 4
                if missing:
                    v += '=' * (4 - missing)
                result[k] = base64.b64decode(v).decode('utf-8')
            except Exception:
                result[k] = v
        return result
    except Exception as e:
        return {}

def save_secrets(secrets):
    encoded = {k: base64.b64encode(v.encode('utf-8')).decode('utf-8') for k, v in secrets.items()}
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(encoded, f, indent=2)
    if os.name != 'nt':
        os.chmod(SECRETS_FILE, 0o600)

# Cargar configuración inicial
settings = load_settings()

# ------------------------------------------------------------
# FUNCIONES DINÁMICAS PARA OBTENER RUTAS (se actualizan en tiempo real)
# ------------------------------------------------------------
def get_workspace():
    """Return the execution root; global mode permits absolute paths on any drive.

    Path safety is still enforced by ``safe_path`` through blacklist checks.
    """
    global settings
    settings = load_settings()
    mode = settings.get("workspace_mode", "default")
    if mode == "global":
        return Path("/")
    workspace_path = settings.get("workspace_path") or str(DEFAULT_WORKSPACE)
    return Path(workspace_path).resolve()

def get_db_dir():
    if settings.get("workspace_mode", "default") == "global":
        return CONFIG_DIR / ".db"
    return get_workspace() / ".db"

def get_venvs_dir():
    if settings.get("workspace_mode", "default") == "global":
        return CONFIG_DIR / ".venvs"
    return get_workspace() / ".venvs"

def ensure_workspace_dirs():
    workspace = get_workspace()
    if settings.get("workspace_mode", "default") != "global":
        workspace.mkdir(parents=True, exist_ok=True)
    db_dir = get_db_dir()
    venvs_dir = get_venvs_dir()
    db_dir.mkdir(parents=True, exist_ok=True)
    venvs_dir.mkdir(parents=True, exist_ok=True)

ensure_workspace_dirs()

_PYTHON_CMD_RE = re.compile(r'^\s*["\']?((?:python|python3)(?:\.exe)?)["\']?(?=\s|$)', re.IGNORECASE)


def _quote_arg(token: str) -> str:
    """Cita un argumento para el shell NATIVO de esta plataforma.

    En Windows el shell por defecto es PowerShell: shlex.quote() genera comillas
    simples (POSIX) que PowerShell NO entiende, así que ahí usamos comillas dobles.
    """
    if os.name == "nt":
        if not token or re.search(r'[\s"\'`$&|<>^;()]', token):
            # ` escapa " y $ dentro de comillas dobles de PowerShell.
            inner = token.replace("`", "``").replace('"', '`"').replace("$", "`$")
            return '"' + inner + '"'
        return token
    return shlex.quote(token)


def _resolve_python_in_command(command: str) -> str:
    """
    Reescribe 'python'/'python3' al inicio del comando para que NUNCA se invoque
    el ejecutable del Manager como si fuera un intérprete (eso abriría una segunda
    GUI en modo onefile).

    * Si hay Python del sistema: se sustituye SÓLO el token del intérprete por su
      ruta absoluta (el resto del comando no se toca, así se respetan las comillas
      originales del usuario/modelo).
    * Si no lo hay y estamos congelados: se usa
      `<exe> --run-python-script <script>` (ejecuta sin abrir GUI).
    """
    if not command or not command.strip():
        return command
    match = _PYTHON_CMD_RE.match(command)
    if not match:
        return command
    rest = command[match.end():]

    system_py = runtime.get_system_python()
    if system_py:
        return _quote_arg(str(system_py)) + rest

    if not getattr(sys, "frozen", False):
        return command

    # Congelado y sin Python del sistema: usamos este ejecutable en modo script.
    try:
        parts = shlex.split(rest, posix=(os.name != "nt"))
    except ValueError:
        return command
    parts = [p.strip('"') for p in parts]

    if len(parts) >= 2 and parts[0] == "-c":
        import tempfile
        tmp_dir = Path(tempfile.gettempdir()) / "llama_webui_pycode"
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = tmp_dir / f"code_{uuid.uuid4().hex[:8]}.py"
            tmp_file.write_text(parts[1], encoding="utf-8")
        except Exception:
            return command
        new_parts = [sys.executable, "--run-python-script", str(tmp_file)] + parts[2:]
    elif parts:
        new_parts = [sys.executable, "--run-python-script"] + parts
    else:
        return command
    return " ".join(_quote_arg(p) for p in new_parts)
# ------------------------------------------------------------
# Seguridad
# ------------------------------------------------------------
def is_path_blacklisted(path: Path) -> bool:
    blacklist = settings.get("blacklist_paths", [])
    if not blacklist:
        return False
    resolved = path.resolve()
    for banned in blacklist:
        banned_path = Path(banned).expanduser().resolve()
        if resolved == banned_path or banned_path in resolved.parents:
            return True
    return False

def is_command_allowed(command: str) -> bool:
    # Si la lista negra está desactivada, permitir todo
    if not settings.get("use_blacklist", True):
        return True
    
    # Obtener la lista negra
    blacklist = settings.get("blacklist_commands", [])
    
    # Extraer el comando base (primer token)
    cmd_base = command.strip().split()[0].lower()
    cmd_base = os.path.basename(cmd_base)  # Quita rutas, ej. /usr/bin/rm -> rm
    
    # Si el comando base está en la lista negra, denegar
    if cmd_base in blacklist:
        return False
    
    # Si no está en la lista negra, permitir
    return True

def safe_path(relative_path: str, base: Path = None) -> Path:
    global settings
    settings = load_settings()
    mode = settings.get("workspace_mode", "default")
    
    # Normalizar la ruta de entrada para evitar problemas de barras/mayúsculas
    normalized_path = os.path.normpath(relative_path)
    if os.name == 'nt':
        normalized_path = os.path.normcase(normalized_path)
    
    if mode == "global":
        p = Path(normalized_path)
        if not p.is_absolute():
            p = Path.cwd() / p
        p = p.resolve()
        if is_path_blacklisted(p):
            raise ValueError(f"Acceso denegado: ruta en lista negra: {p}")
        return p
    else:
        if base is None:
            base = get_workspace()
        try:
            p = Path(normalized_path)
            if p.is_absolute():
                # Si es absoluta, resolver y verificar que esté dentro del workspace
                full = p.resolve()
            else:
                # Si es relativa, resolver contra el workspace
                full = (base / p).resolve()
        except Exception:
            full = (base / relative_path).resolve()
        
        # Normalizar la ruta base para comparación
        base_resolved = str(base.resolve())
        if os.name == 'nt':
            base_resolved = os.path.normcase(base_resolved)
            full_str = os.path.normcase(str(full))
        else:
            full_str = str(full)
        
        # Verificar que la ruta completa esté dentro del workspace
        if not full_str.startswith(base_resolved):
            raise ValueError(f"Acceso denegado: sale del workspace: {relative_path}")
        if is_path_blacklisted(full):
            raise ValueError(f"Acceso denegado: ruta en lista negra: {full}")
        return full

_pending_confirmations = {}

class ConfirmationRequired(Exception):
    def __init__(self, token, message, action_details=None):
        self.token = token
        self.message = message
        self.action_details = action_details or {}
        super().__init__(message)

def requires_confirmation(action: str, details: str = "", action_context: dict = None) -> None:
    if not settings.get("require_confirmation", True):
        return
    token = f"confirm_{int(time.time())}_{hash(action+details) % 10000}"
    _pending_confirmations[token] = {
        "action": action,
        "details": details,
        "action_context": action_context or {},
        "confirmed": False,
        "timestamp": time.time()
    }
    raise ConfirmationRequired(
        token,
        f"⚠️ Requiere confirmación: {action}\nDetalles: {details}\nLlama a la herramienta 'confirm_action' con el token '{token}'",
        action_details={"action": action, "details": details}
    )

def confirmable(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ConfirmationRequired as e:
            return e.message
    return wrapper

def confirm_action(token: str) -> str:
    if token in _pending_confirmations:
        entry = _pending_confirmations[token]
        entry["confirmed"] = True
        context = entry.get("action_context", {})
        details = entry.get("details", "")
        action = entry.get("action", "desconocida")
        return (
            f"✅ Acción confirmada. Reintenta la operación original.\n"
            f"   Acción: {action}\n"
            f"   Detalles: {details}\n"
            f"   Contexto: {json.dumps(context, ensure_ascii=False) if context else '(ninguno)'}\n"
            f"   (Puedes volver a llamar a la herramienta con los mismos argumentos)"
        )
    return "❌ Token inválido o expirado."


def confirm_and_execute(token: str) -> Dict[str, Any]:
    """Helper to confirm a pending action and execute it if confirmed.
    Returns execute-like result.
    """
    entry = _pending_confirmations.get(token)
    if not entry:
        return {"status": "error", "error": "Token inválido o expirado."}
    if not entry.get("confirmed"):
        return {"status": "error", "error": "Token aún no confirmado."}
    action = entry.get("action")
    ctx = entry.get("action_context", {})
    # For now only support bash
    if action == "bash":
        cmd_args = ctx.get("args", {})
        command = cmd_args.get("command") if isinstance(cmd_args, dict) else None
        timeout = cmd_args.get("timeout", 30) if isinstance(cmd_args, dict) else 30
        if not command:
            return {"status": "error", "error": "No command provided in confirmation context."}
        return bash(command, timeout=timeout)
    return {"status": "error", "error": f"Accion confirmada '{action}' no soportada para ejecución automática."}

# ============================================================
# HERRAMIENTAS INTERNAS (todas las funciones, sin exponer)
# ============================================================

# ---------- read_file (solo texto plano) ----------
def read_file(path: str, **kwargs) -> str:
    """
    Lee archivos de texto plano: .txt, .py, .json, .csv, .md, .html, etc.
    Si el contenido es JSON, lo formatea automaticamente para mayor legibilidad.
    No soporta archivos Office (.docx, .xlsx, .pptx).
    """
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: El archivo '{path}' no existe"
        if not target.is_file():
            return f"Error: '{path}' es un directorio"
        ext = target.suffix.lower()
        # Rechazar archivos Office
        if ext in ('.docx', '.xlsx', '.xls', '.pptx', '.ppt'):
            return f"Error: Los archivos Office ({ext}) no estan soportados. Solo se permiten archivos de texto plano."
        content = target.read_text(encoding="utf-8")
        # Si es JSON, formatear bonito para que sea legible
        try:
            parsed = json.loads(content)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except:
            return content
    except PermissionError:
        return f"Error: Permiso denegado para leer '{path}'"
    except Exception as e:
        return f"Error al leer: {e}"


def write_file(path: str, content: str, **kwargs) -> str:
    """
    Escribe archivos de texto plano: .txt, .py, .json, .csv, .md, .html, etc.
    Parametro mode: 'w' (por defecto) o 'a' (append).
    No soporta archivos Office (.docx, .xlsx, .pptx).
    """
    try:
        target = safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ext = target.suffix.lower()
        # Rechazar archivos Office
        if ext in ('.docx', '.xlsx', '.xls', '.pptx', '.ppt'):
            return f"Error: Los archivos Office ({ext}) no estan soportados. Solo se permiten archivos de texto plano."
        mode = kwargs.pop('mode', 'w')  # 'w' o 'a'
        if mode == 'a':
            # Modo append: añadir al final
            with open(str(target), 'a', encoding='utf-8') as f:
                f.write(content)
            return f"Añadido a: {target}"
        else:
            # Modo escritura: reemplazar contenido
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(content)
                os.replace(tmp, str(target))
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
            return f"Escrito: {target}"
    except PermissionError:
        return f"Error: Permiso denegado para escribir en '{path}'"
    except Exception as e:
        return f"Error: {e}"

# ---------- edit_file (solo texto plano) ----------
def edit_file(path: str, old_text: str = None, new_text: str = None,
              operation: str = None, **kwargs) -> str:
    """
    Edita archivos de texto plano sin perder formato.
    Operaciones: replace, append, insert_after, insert_before, delete.
    Si no se especifica operation, detecta automaticamente:
      - old_text + new_text -> replace
      - solo new_text -> append
    No soporta archivos Office (.docx, .xlsx, .pptx).
    """
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: El archivo '{path}' no existe"
        ext = target.suffix.lower()
        # Rechazar archivos Office
        if ext in ('.docx', '.xlsx', '.xls', '.pptx', '.ppt'):
            return f"Error: Los archivos Office ({ext}) no estan soportados. Solo se permiten archivos de texto plano."

        # ---------- DETECCIÓN INTELIGENTE DE OPERACIÓN ----------
        if operation is None:
            if old_text is not None and new_text is not None:
                operation = 'replace'
            elif new_text is not None and old_text is None:
                operation = 'append'

        if operation is None:
            return "Error: Se requiere operation (replace, append, insert_after, insert_before, delete)."

        # ---------- OPERACIONES DE TEXTO PLANO ----------
        content = target.read_text(encoding="utf-8")
        new_content = content

        if operation == 'append':
            if new_text is None:
                return "Error: Para append se requiere new_text."
            new_content = content + new_text
        elif operation == 'insert_after':
            if not old_text or new_text is None:
                return "Error: Para insert_after se requiere old_text y new_text."
            if old_text not in content:
                return f"Error: No se encontro '{old_text}' en '{path}'"
            new_content = content.replace(old_text, old_text + new_text, 1)
        elif operation == 'insert_before':
            if not old_text or new_text is None:
                return "Error: Para insert_before se requiere old_text y new_text."
            if old_text not in content:
                return f"Error: No se encontro '{old_text}' en '{path}'"
            new_content = content.replace(old_text, new_text + old_text, 1)
        elif operation == 'delete':
            if not old_text:
                return "Error: Para delete se requiere old_text."
            if old_text not in content:
                return f"Error: No se encontro '{old_text}' en '{path}'"
            new_content = content.replace(old_text, '', 1)
        elif operation == 'replace':
            if not old_text or new_text is None:
                return "Error: Para replace se requiere old_text y new_text."
            if old_text not in content:
                return f"Error: No se encontro el texto a reemplazar en '{path}'"
            new_content = content.replace(old_text, new_text, 1)
        else:
            return f"Error: Operacion '{operation}' no soportada. Usa: replace, append, insert_after, insert_before, delete."

        # Escribir cambios de forma segura
        bak = str(target) + ".bak"
        try:
            shutil.copy2(str(target), bak)
        except Exception:
            pass
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(new_content)
            os.replace(tmp, str(target))
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
        return f"Editado: {target} (operacion: {operation}, backup: {bak})"
    except Exception as e:
        return f"Error: {e}"

# ---------- apply_diff ----------
def apply_diff(path: str, diff: str) -> str:
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: El archivo '{path}' no existe"
        original = target.read_text(encoding="utf-8")
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as f:
            f.write(diff)
            diff_path = f.name
        try:
            result = _subprocess_run(['patch', str(target), diff_path], timeout=10)
            if result.returncode == 0:
                return f"✅ Diff aplicado correctamente a {path}"
            else:
                lines = original.splitlines(keepends=True)
                diff_lines = diff.splitlines()
                added = []
                removed = []
                for line in diff_lines:
                    if line.startswith('-'):
                        removed.append(line[1:])
                    elif line.startswith('+'):
                        added.append(line[1:])
                if removed and added:
                    new_content = original.replace(removed[0], added[0], 1)
                    target.write_text(new_content, encoding="utf-8")
                    return f"✅ Diff aplicado (fallback) a {path}"
                else:
                    return f"Error: No se pudo aplicar el diff. Verifica el formato."
        finally:
            os.unlink(diff_path)
    except Exception as e:
        return f"Error: {e}"

# ---------- file_glob_search ----------
def file_glob_search(pattern: str, path: str = ".") -> str:
    try:
        base = safe_path(path)
        if not base.is_dir():
            return f"Error: '{path}' no es un directorio"
        matches = [str(p.relative_to(base)) for p in base.glob(pattern) if p.is_file()]
        if not matches:
            return "No se encontraron archivos."
        return "\n".join(matches[:100])
    except Exception as e:
        return f"Error: {e}"

# ---------- grep_search ----------
def grep_search(pattern: str, path: str = ".") -> str:
    try:
        base = safe_path(path)
        if not base.is_dir():
            return f"Error: '{path}' no es un directorio"
        regex = re.compile(pattern)
        results = []
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if regex.search(content):
                        results.append(str(p.relative_to(base)))
                except:
                    continue
                if len(results) >= 50:
                    results.append("... y más (limitado a 50)")
                    break
        if not results:
            return "No se encontraron coincidencias."
        return "\n".join(results)
    except Exception as e:
        return f"Error: {e}"

# ---------- ejecución de subprocesos sin consola ----------
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _no_window_kwargs() -> Dict[str, Any]:
    """Evita que Windows abra una consola nueva por cada subproceso."""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _run_capture(cmd, timeout: float, cwd: str = None, env: dict = None,
                 input_text: str = None) -> Dict[str, Any]:
    """Ejecuta un comando capturando su salida SIN abrir consola externa.

    Devuelve {"status": "ok"|"timeout", "stdout", "stderr", "returncode"}.
    En timeout mata el proceso (y su árbol en Windows) y conserva la salida parcial.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        **_no_window_kwargs(),
    )
    try:
        out, err = proc.communicate(input=input_text, timeout=float(timeout))
        return {"status": "ok", "stdout": out or "", "stderr": err or "",
                "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        try:
            try:
                from . import process_manager
            except ImportError:
                import process_manager  # type: ignore
            process_manager.kill_process_tree(proc.pid, timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            out, err = proc.communicate(timeout=2)
        except Exception:
            out, err = "", ""
        return {"status": "timeout", "stdout": out or "", "stderr": err or "",
                "returncode": None, "timeout": timeout}


def _subprocess_run(cmd, **kwargs):
    """subprocess.run() SIN consolas externas (Windows) ni huérfanos (POSIX).

    Mantiene la firma y el retorno de subprocess.run para no tocar los call-sites.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    else:
        kwargs.setdefault("start_new_session", True)
    return subprocess.run(cmd, **kwargs)


def _apply_venv_env(env: dict, venv: str) -> str:
    """Añade los paths del venv indicado a `env`. Devuelve "" si todo va bien."""
    venv_path = get_venvs_dir() / venv
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    exe_dir = venv_path / bin_dir
    if not exe_dir.exists():
        return f"Entorno '{venv}' no encontrado en {venv_path}"
    env["VIRTUAL_ENV"] = str(venv_path)
    env["PATH"] = str(exe_dir) + os.pathsep + env.get("PATH", "")
    return ""


def _format_job_result(res: Dict[str, Any]) -> str:
    """Resultado de un job de `start_job` listo para el chat y para el modelo.

    Dos fallos reales que este formato corrige:

    * `start_job` no incluía `error` en los jobs terminados, así que un comando
      que salía con código != 0 devolvía «Error: None» (la clave no existía) y
      se tiraba la salida real: el modelo no veía el traceback de Python ni el
      mensaje del compilador, aunque la Consola y jobs sí lo mostrara.
    * La cola `[exit code: N]` usa el mismo formato que la tool nativa del
      servidor, así el chat la sustituye por su badge de estado (verde/rojo) en
      lugar de mostrar una línea duplicada o un «[exit code N]» no parseable.

    Los errores de arranque (comando vacío, ejecutable no encontrado, límite de
    jobs) no crean job: no llevan salida, solo su `error`.
    """
    status = res.get("status")
    finished = "output" in res or "exit_code" in res
    out = "\n".join(res.get("output") or [])
    exit_code = res.get("exit_code")
    tail = f"\n[exit code: {exit_code}]" if exit_code is not None else ""

    if not finished:
        return f"Error: {res.get('error') or 'no se pudo lanzar el proceso'}"

    body = out if out.strip() else "(sin salida)"

    if status == "error":
        reason = res.get("error") or (
            f"proceso terminado con código {exit_code}"
            if exit_code is not None
            else "fallo desconocido"
        )
        return f"Error: {reason}\n{body}{tail}"

    if status == "killed":
        return f"Proceso detenido (killed).\n{body}{tail}"

    return f"{body}{tail}"


# ---------- exec_shell_command ----------
def exec_shell_command(command: str, timeout: int = 10) -> str:
    command = _resolve_python_in_command(command)
    if not is_command_allowed(command):
        return f"Comando denegado: '{command}' no está en la lista blanca."
    try:
        # Async: pasa por job_manager (job_manager). Comandos cortos devuelven
        # su salida; los largos devuelven un job_id visible en la consola.
        res = job_manager.start_job(command, shell='auto', cwd=str(get_workspace()),
                                    wait_seconds=max(0.5, min(float(timeout), 2.5)))
        status = res.get('status')
        # Terminado —o error de arranque—: salida real + motivo + cola
        # `[exit code: N]`. Antes un `status == 'error'` temprano devolvía
        # «Error: None» (start_job no traía `error`) y descartaba stdout/stderr,
        # dejando al modelo sin ver el error real del comando.
        if status in ('exited', 'error', 'killed'):
            return _format_job_result(res)
        # Sigue en ejecución → job_id + preview
        preview = '\n'.join(res.get('preview') or [])
        return (f"[job {res.get('job_id')}] El comando sigue en ejecución. "
                f"{res.get('hint') or ''} Salida hasta ahora:\n{preview}")
    except Exception as e:
        return f"Error: {e}"

# ---------- get_datetime ----------
def get_datetime() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")

# ---------- create_directory ----------
def create_directory(path: str) -> str:
    try:
        target = safe_path(path)
        target.mkdir(parents=True, exist_ok=True)
        return f"Directorio creado: {target}"
    except Exception as e:
        return f"Error: {e}"

# ---------- list_directory ----------
def list_directory(path: str = ".", depth: int = 0) -> str:
    """Lista un directorio; ``depth`` controla cuántos niveles descendientes incluir."""
    try:
        target = safe_path(path)
        if not target.is_dir():
            return f"No es directorio: {target}"
        max_depth = max(0, int(depth))
        items = []

        def walk(directory: Path, current_depth: int, prefix: str = ""):
            for item in sorted(directory.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower())):
                marker = "[DIR]" if item.is_dir() else "[FILE]"
                items.append(f"{prefix}{marker} {item.name}")
                if item.is_dir() and current_depth < max_depth:
                    walk(item, current_depth + 1, prefix + "  ")

        walk(target, 0)
        return "\n".join(items) if items else "(vacío)"
    except Exception as e:
        return f"Error: {e}"

# ---------- delete_file ----------
def delete_file(path: str) -> str:
    try:
        target = safe_path(path)
        if not target.exists():
            return "No existe."
        try:
            from send2trash import send2trash
            send2trash(str(target))
            return f"✅ Movido a la papelera: {target}"
        except ImportError:
            if target.is_dir():
                if any(target.iterdir()):
                    return "Directorio no vacío. Instala send2trash para mover a papelera."
                target.rmdir()
            else:
                target.unlink()
            return f"⚠️ Eliminado permanentemente (send2trash no instalado): {target}"
    except Exception as e:
        return f"Error: {e}"

# ---------- move_file ----------
def move_file(source: str, destination: str) -> str:
    try:
        src = safe_path(source)
        dst = safe_path(destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Movido: {src} -> {dst}"
    except Exception as e:
        return f"Error: {e}"

# ---------- compress_files ----------
def compress_files(source_paths: List[str], archive_name: str, format: str = "zip") -> str:
    if isinstance(source_paths, str):
        source_paths = [p.strip() for p in source_paths.split(',') if p.strip()]
        if not source_paths:
            return "Error: No se especificaron rutas para comprimir."
    elif not isinstance(source_paths, list):
        return f"Error: source_paths debe ser una lista o una cadena separada por comas, recibido {type(source_paths)}"
    try:
        sources = [safe_path(p) for p in source_paths]
        archive_path = safe_path(archive_name)
        if format == "zip":
            import zipfile
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for src in sources:
                    if src.is_dir():
                        for f in src.rglob("*"):
                            zf.write(f, f.relative_to(src.parent))
                    else:
                        zf.write(src, src.name)
            return f"Comprimido en {archive_path}"
        else:
            if not PATOOL_AVAILABLE:
                return "Para rar/7z instala patool: pip install patool"
            import patoolib
            patoolib.create_archive(str(archive_path), sources)
            return f"Archivo {archive_name} creado."
    except Exception as e:
        return f"Error: {e}"

# ---------- extract_archive ----------
def extract_archive(archive_path: str, dest_dir: str) -> str:
    try:
        arch = safe_path(archive_path)
        dest = safe_path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        if arch.suffix == ".zip":
            import zipfile
            with zipfile.ZipFile(arch, 'r') as zf:
                zf.extractall(dest)
        else:
            if not PATOOL_AVAILABLE:
                return "Instala patool para extraer"
            import patoolib
            patoolib.extract_archive(str(arch), outdir=str(dest))
        return f"Extraído en {dest}"
    except Exception as e:
        return f"Error: {e}"

# ---------- create_venv ----------
def create_venv(name: str) -> str:
    venvs_dir = get_venvs_dir()
    venv_path = venvs_dir / name
    if venv_path.exists():
        return f"El entorno '{name}' ya existe."

    # Intentar usar Python del sistema
    python_exe = runtime.get_system_python()
    if python_exe is None:
        # Congelado y sin Python en el PATH: este ejecutable NO es un intérprete
        # (arrancaría el gestor), así que no se puede crear el venv. Antes se
        # lanzaba `manager.exe -m venv ...`, que sólo daba un error confuso.
        if getattr(sys, "frozen", False):
            return ("No se puede crear el entorno: no hay ningún Python en el PATH "
                    "y el ejecutable del Manager no actúa como intérprete. Instala "
                    "Python, añádelo al PATH y vuelve a intentarlo.")
        # Fallback al empaquetado (en desarrollo siempre hay intérprete).
        python_exe = Path(runtime.get_python_exe())
        # Podríamos registrar un warning
    try:
        _subprocess_run([str(python_exe), "-m", "venv", str(venv_path)], check=True)
        return f"Entorno virtual '{name}' creado en {venv_path} (Python: {python_exe})"
    except Exception as e:
        return f"Error: {e}"

# ---------- run_python_code (CORREGIDA) ----------
def run_python_code(code: str, venv: str = None, timeout: int = 30) -> str:
    venvs_dir = get_venvs_dir()
    workspace = get_workspace()
    
    if venv:
        bin_dir = "Scripts" if os.name == "nt" else "bin"
        python_exe = venvs_dir / venv / bin_dir / ("python.exe" if os.name == "nt" else "python")
        if not python_exe.exists():
            return f"Entorno '{venv}' no encontrado. Ruta buscada: {python_exe}"
    else:
        # Usar Python del sistema
        python_exe = runtime.get_system_python()
        if python_exe is None:
            python_exe = Path(runtime.get_python_exe())

    env = os.environ.copy()
    
    # Añadir la raíz del proyecto al PYTHONPATH del subproceso
    base_dir = str(_PROJECT_ROOT)  # definido al inicio del archivo
    pythonpath = env.get("PYTHONPATH", "")
    if base_dir not in pythonpath:
        if pythonpath:
            env["PYTHONPATH"] = base_dir + os.pathsep + pythonpath
        else:
            env["PYTHONPATH"] = base_dir
    
    if venv:
        venv_path = venvs_dir / venv
        env["VIRTUAL_ENV"] = str(venv_path)
        old_path = env.get("PATH", "")
        if os.name == "nt":
            new_path = str(venv_path / "Scripts") + os.pathsep + old_path
        else:
            new_path = str(venv_path / "bin") + os.pathsep + old_path
        env["PATH"] = new_path
        site_packages = venv_path / "Lib" / "site-packages" if os.name == "nt" else venv_path / "lib" / "python*" / "site-packages"
        if site_packages.exists():
            env["PYTHONPATH"] = str(site_packages) + os.pathsep + env.get("PYTHONPATH", "")
    
    # El código se ejecuta como job del `job_manager` (la misma tubería que
    # exec_shell_command) para que aparezca en la Consola y quede su log. El
    # job_manager lanza procesos sin stdin, así que el código va a un archivo
    # temporal y se ejecuta con `--run-python-script`.
    script_path = None
    try:
        tmp_dir = Path(tempfile.gettempdir()) / "llama_webui_pycode"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        script_path = tmp_dir / f"code_{time.time_ns()}.py"
        script_path.write_text(code, encoding="utf-8")
    except Exception as e:
        return f"Error: no se pudo preparar el script temporal: {e}"

    if venv:
        runner = [str(python_exe), str(script_path)]
    elif getattr(sys, "frozen", False):
        # Sin venv explícito: ESTE ejecutable actúa como intérprete Python
        # portable (equipos sin Python instalado) vía --run-python-script.
        runner = [sys.executable, "--run-python-script", str(script_path)]
    else:
        system_py = runtime.get_system_python()
        runner = [str(system_py or python_exe), str(script_path)]

    command = " ".join(f'"{part}"' for part in runner)
    res: Dict[str, Any] = {}
    try:
        # shell="none": el intérprete se ejecuta DIRECTO. Pasando por PowerShell,
        # el comando empieza por una ruta entrecomillada y PowerShell lo leía como
        # expresión ("Token '--run-python-script' inesperado"), así que run_python
        # nunca funcionaba en los equipos con el Manager congelado.
        res = job_manager.start_job(
            command,
            shell="none",
            cwd=str(workspace),
            env=env,
            label="run_python_code",
            wait_seconds=max(0.5, min(float(timeout), 30.0)),
        )
        status = res.get("status")
        # Mismo formato que exec_shell_command: si el script trazaba una
        # excepción (exit != 0), el traceback estaba en `output` y se perdía
        # en «Error: None» — el modelo no podía ver su propio error.
        if status in ("exited", "error", "killed"):
            return _format_job_result(res)
        preview = "\n".join(res.get("preview") or [])
        return (f"[job {res.get('job_id')}] El código sigue en ejecución. "
                f"{res.get('hint') or ''} Salida hasta ahora:\n{preview}")
    except Exception as e:
        return f"Error: {e}"
    finally:
        # El archivo ya no hace falta cuando el proceso terminó; si sigue vivo
        # lo deja el SO limpiar (está en el temp del sistema).
        if script_path is not None and not (res.get("status") in ("starting", "running")):
            try:
                script_path.unlink()
            except Exception:
                pass

# ---------- enlaces públicos al workspace (nginx / acceso directo) ----------
#: Última petición HTTP vista por el servidor MCP: permite construir URLs válidas
#: desde el navegador del usuario (incluido acceso remoto tras nginx).
_LAST_REQUEST_HOSTS: Dict[str, Any] = {"host": "", "proto": "", "prefix": "", "proxied": False}
WORKSPACE_SERVICE_PORT = 19000


def _remember_request(request) -> None:
    """Guarda Host/X-Forwarded-* de la petición para futuros enlaces."""
    try:
        headers = request.headers
        fwd_host = headers.get("x-forwarded-host") or ""
        _LAST_REQUEST_HOSTS["host"] = fwd_host or headers.get("host") or ""
        _LAST_REQUEST_HOSTS["proto"] = (headers.get("x-forwarded-proto") or
                                        getattr(request.url, "scheme", "http") or "http"
                                        ).split(",")[0].strip()
        _LAST_REQUEST_HOSTS["prefix"] = (headers.get("x-forwarded-prefix") or "").rstrip("/")
        _LAST_REQUEST_HOSTS["proxied"] = bool(fwd_host)
    except Exception:
        pass


def _workspace_public_base() -> str:
    """Base pública del servidor de workspace (sin barra final)."""
    override = (os.environ.get("LLAMA_WEBUI_PUBLIC_URL") or "").rstrip("/")
    if override:
        return override
    host = str(_LAST_REQUEST_HOSTS.get("host") or "")
    proto = str(_LAST_REQUEST_HOSTS.get("proto") or "http")
    if _LAST_REQUEST_HOSTS.get("prefix"):
        proto_host = f"{proto}://{host}{_LAST_REQUEST_HOSTS['prefix']}"
    else:
        proto_host = f"{proto}://{host}"
    if not host:
        return f"http://127.0.0.1:{WORKSPACE_SERVICE_PORT}"
    if _LAST_REQUEST_HOSTS.get("proxied"):
        # Detrás de nginx el puerto del servicio no se expone.
        return proto_host
    host_only = host.split(":")[0]
    return f"{proto}://{host_only}:{WORKSPACE_SERVICE_PORT}"


def workspace_download_link(path: str, as_attachment: bool = True) -> str:
    """Devuelve una URL absoluta para descargar/ver un archivo del workspace.

    Funciona también en acceso remoto: detrás de nginx devuelve
    `https://midominio.com/api/workspace/download?path=...`; en acceso directo
    `http://<host>:19000/api/workspace/download?path=...`.
    """
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: la ruta '{path}' no existe en el workspace."
        try:
            rel_path = target.relative_to(get_workspace()).as_posix()
        except Exception:
            rel_path = target.as_posix()
        endpoint = "download" if as_attachment else "raw"
        url = f"{_workspace_public_base()}/api/workspace/{endpoint}?path={rel_path}"
        return _json_out({
            "status": "ok",
            "path": rel_path,
            "url": url,
            "kind": "dir" if target.is_dir() else "file",
            "hint": "Incluye esta URL en tu respuesta para que el usuario abra o descargue el archivo.",
        })
    except Exception as e:
        return f"Error: {e}"


# ---------- jobs en segundo plano (run_command_async / job_*) ----------
def _json_out(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        return str(data)


def _sse_frame(event: Any) -> str:
    """Serializa un evento como frame SSE a prueba de payloads hostiles.

    Dos problemas reales del stream de agentes:

    * `ensure_ascii=False` obligaba a codificar el frame en UTF-8; un surrogado
      suelto (los modelos locales emiten bytes UTF-8 inválidos) hacía fallar la
      codificación a mitad de stream y el navegador veía un error de conexión
      que la UI reportaba como "Evento de agente inválido". Con escapes ASCII
      el JSON sigue siendo válido y el frame siempre es ASCII puro.
    * Un payload no serializable (Path, datetime, bytes...) tumbaba el
      generador entero. Ahora se degrada a un evento de error con el texto del
      valor y el stream continúa.
    """
    ev_type = re.sub(
        r"[^A-Za-z0-9_.:-]+", " ", str((event or {}).get("type") or "message")
    ).strip() or "message"
    try:
        data = json.dumps(event, ensure_ascii=True, default=str)
    except Exception:
        try:
            data = json.dumps(
                {
                    "type": ev_type,
                    "payload": str((event or {}).get("payload"))[:2000],
                    "meta": {"serialization": "fallback"},
                },
                ensure_ascii=True,
            )
        except Exception:
            return ""
    return f"event: {ev_type}\ndata: {data}\n\n"


def _is_stream_terminal(event: Any) -> bool:
    """True SOLO para el `done` final que el worker appendea en su `finally`.

    Ese evento se añade directamente a la cola del job (sin `meta`); todos los
    eventos del bus —incluidos los `done` intermedios de cada especialista, la
    síntesis del supervisor o los pasos del plan— viajan con `meta`
    (job_id/conversation_id). Cerrar el stream con cualquiera de esos `done`
    mataba el SSE antes de la síntesis final: el chat quedaba mudo y la UI
    reportaba conexión SSE perdida.
    """
    return (
        isinstance(event, dict)
        and event.get("type") == "done"
        and event.get("meta") is None
    )



def run_command_async(command: str, shell: str = "auto", cwd: str = None,
                      venv: str = None, label: str = None,
                      wait_seconds: float = 1.5) -> str:
    """Lanza un comando en segundo plano y devuelve un job_id inmediato.

    shell: auto | powershell | pwsh | cmd | bash | sh | none
    Si el comando termina dentro de `wait_seconds`, devuelve ya su salida.
    """
    if not command or not str(command).strip():
        return _json_out({"status": "error", "error": "Comando vacío"})
    if not is_command_allowed(command):
        return _json_out({"status": "denied",
                          "error": f"Comando denegado: '{command}' está en la lista negra."})
    command = _resolve_python_in_command(command)
    job_env = None
    if venv:
        job_env = os.environ.copy()
        err = _apply_venv_env(job_env, venv)
        if err:
            return _json_out({"status": "error", "error": err})
    try:
        workdir = cwd or str(get_workspace())
        res = job_manager.start_job(command, shell=shell, cwd=workdir, env=job_env,
                                    label=label, wait_seconds=wait_seconds)
        return _json_out(res)
    except Exception as e:
        return _json_out({"status": "error", "error": str(e)})


def job_status(job_id: str) -> str:
    """Estado de un job (running|exited|error|killed) con su exit code y tiempo."""
    return _json_out(job_manager.job_status(job_id))


def job_output(job_id: str, tail: int = 200, offset: int = 0) -> str:
    """Últimas `tail` líneas de la SALIDA del job (tail=0 para todas).

    Devuelve solo lo que imprimió el proceso. El log del visor añade el comando,
    el cwd y el pie de "proceso finalizado", y eso no debe llegar al modelo.
    """
    return _json_out(job_manager.job_output(job_id, tail=tail, offset=offset, raw=True))


def job_kill(job_id: str) -> str:
    """Detiene un job en ejecución (mata el árbol de procesos)."""
    return _json_out(job_manager.job_kill(job_id))


def job_list(include_finished: bool = True) -> str:
    """Lista los jobs lanzados en esta sesión."""
    return _json_out({"jobs": job_manager.list_jobs(include_finished=include_finished)})

# ---------- SQLite helpers ----------
def _get_db_path(database: str) -> Path:
    workspace = get_workspace()
    if os.sep in database or (os.altsep and os.altsep in database):
        return safe_path(database, base=workspace)
    else:
        path = workspace / database
        return path.resolve()

def sqlite_query(database: str, query: str) -> str:
    settings = load_settings()
    db_path = _get_db_path(database)
    if not db_path.exists():
        return f"Base '{database}' no existe."
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        queries = [q.strip() for q in query.split(';') if q.strip()]
        results = []
        for q in queries:
            cur.execute(q)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            if rows:
                result = " | ".join(cols) + "\n" + "-"*40 + "\n"
                result += "\n".join(" | ".join(str(v) for v in row) for row in rows)
                results.append(result)
            else:
                results.append("(sin resultados)")
        conn.close()
        return "\n\n".join(results) if results else "Sin resultados."
    except Exception as e:
        return f"Error: {e}"

def sqlite_execute(database: str, query: str) -> str:
    db_path = _get_db_path(database)
    # ----- NUEVO: Detección de consultas no-SELECT -----
    import re
    query_upper = query.strip().upper()
    if not query_upper.startswith('SELECT') and settings.get("require_confirmation", True):
        # Pedir confirmación para consultas que no son SELECT
        try:
            requires_confirmation(
                action="sqlite_execute",
                details=f"Base de datos: {database}\nConsulta: {query}",
                action_context={"database": database, "query": query}
            )
            # Si llegamos aquí, la confirmación fue exitosa (el wrapper la maneja)
        except Exception as e:
            return str(e)  # Devuelve el mensaje de confirmación requerida
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(query)
        conn.commit()
        conn.close()
        return f"Ejecutado correctamente en '{database}'."
    except Exception as e:
        return f"Error: {e}"

def get_db_schema(database: str) -> str:
    db_path = _get_db_path(database)
    if not db_path.exists():
        return f"Base '{database}' no existe."
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cur.fetchall()]
        schema = []
        for table in tables:
            cur.execute(f"PRAGMA table_info({table})")
            cols = cur.fetchall()
            col_info = ", ".join([f"{c[1]} ({c[2]})" for c in cols])
            schema.append(f"{table}: {col_info}")
        conn.close()
        return "\n".join(schema) if schema else "Sin tablas."
    except Exception as e:
        return f"Error: {e}"

# ---------- Procesos ----------
if PSUTIL_AVAILABLE:
    def list_processes(filter_name: str = "") -> str:
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                if filter_name.lower() in p.info['name'].lower():
                    procs.append(f"{p.info['pid']:6} {p.info['name']:20} CPU:{p.info['cpu_percent']:5}% MEM:{p.info['memory_percent']:.1f}%")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return "\n".join(procs) if procs else "No hay procesos con ese filtro."
    def kill_process(pid: int) -> str:
        try:
            p = psutil.Process(pid)
            p.terminate()
            return f"Proceso {pid} terminado."
        except Exception as e:
            return f"Error: {e}"
else:
    def list_processes(filter_name: str = "") -> str:
        return "❌ psutil no instalado."
    def kill_process(pid: int) -> str:
        return "❌ psutil no instalado."

# ---------- GUI y captura ----------
if PYAUTOGUI_AVAILABLE:
    def screenshot(save_path: str = "screenshot.png") -> str:
        try:
            target = safe_path(save_path)
            im = pyautogui.screenshot()
            im.save(str(target))
            return f"Captura guardada en {target}"
        except Exception as e:
            return f"Error: {e}"
else:
    def screenshot(save_path: str = "screenshot.png") -> str:
        return "❌ pyautogui no instalado."

# ---------- Cámara ----------
def take_photo(save_path: str = "photo.jpg") -> str:
    try:
        import cv2
    except ImportError:
        return "❌ OpenCV no instalado."
    try:
        target = safe_path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            cap = cv2.VideoCapture(1)
            if not cap.isOpened():
                return "❌ No se pudo abrir la cámara."
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return "❌ Error capturando fotograma."
        cv2.imwrite(str(target), frame)
        return f"✅ Foto guardada en: {target}"
    except Exception as e:
        return f"❌ Error: {e}"

# ---------- Notificaciones ----------
if PLYER_AVAILABLE:
    def notify(title: str, message: str) -> str:
        try:
            from plyer import notification
            notification.notify(title=title, message=message, timeout=5)
            return "Notificación enviada."
        except Exception as e:
            return f"Error: {e}"
else:
    def notify(title: str, message: str) -> str:
        return "❌ plyer no instalado."

# ---------- Navegador ----------
def open_url(url: str) -> str:
    try:
        webbrowser.open(url)
        return f"Abierto {url} en navegador."
    except Exception as e:
        return f"Error: {e}"

# ---------- Tareas ----------
def init_todo():
    db_dir = get_db_dir()
    db_dir.mkdir(parents=True, exist_ok=True)
    TODO_DB = db_dir / "todo.db"
    conn = sqlite3.connect(str(TODO_DB))
    conn.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY, task TEXT, priority INTEGER, done INTEGER, created TEXT)''')
    conn.close()
init_todo()

def add_task(task: str, priority: int = 0) -> str:
    db_dir = get_db_dir()
    TODO_DB = db_dir / "todo.db"
    conn = sqlite3.connect(str(TODO_DB))
    conn.execute("INSERT INTO tasks (task, priority, done, created) VALUES (?, ?, 0, datetime('now'))", (task, priority))
    conn.commit()
    conn.close()
    return f"Tarea añadida: {task}"

def list_tasks(show_done: bool = False) -> str:
    db_dir = get_db_dir()
    TODO_DB = db_dir / "todo.db"
    conn = sqlite3.connect(str(TODO_DB))
    cur = conn.execute("SELECT id, task, priority, done FROM tasks WHERE done = ? ORDER BY priority DESC", (1 if show_done else 0,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return "Sin tareas."
    return "\n".join(f"[{'X' if r[3] else ' '}] {r[0]}: {r[1]} (prio {r[2]})" for r in rows)

def complete_task(task_id: int) -> str:
    db_dir = get_db_dir()
    TODO_DB = db_dir / "todo.db"
    conn = sqlite3.connect(str(TODO_DB))
    conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return f"Tarea {task_id} completada."

# ---------- Recordatorios ----------
def init_reminders():
    db_dir = get_db_dir()
    db_dir.mkdir(parents=True, exist_ok=True)
    REMINDER_DB = db_dir / "reminders.db"
    conn = sqlite3.connect(str(REMINDER_DB))
    conn.execute('''CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, time INTEGER, message TEXT)''')
    conn.close()
init_reminders()

def set_reminder(seconds: int, message: str) -> str:
    def remind():
        time.sleep(seconds)
        if PLYER_AVAILABLE:
            try:
                from plyer import notification
                notification.notify(title="Recordatorio", message=message)
            except:
                pass
        else:
            print(f"🔔 {message}")
    threading.Thread(target=remind, daemon=True).start()
    return f"Recordatorio en {seconds} segundos: {message}"

# ---------- Impresión ----------
if WIN32PRINT_AVAILABLE or CUPS_AVAILABLE:
    def print_file(file_path: str, printer_name: str = "") -> str:
        try:
            path = safe_path(file_path)
            if os.name == 'nt' and WIN32PRINT_AVAILABLE:
                import win32print
                printer = printer_name or win32print.GetDefaultPrinter()
                win32print.ShellExecute(0, "print", str(path), f'/d:"{printer}"', ".", 0)
                return f"Enviado a impresora {printer}"
            elif os.name == 'posix' and CUPS_AVAILABLE:
                import cups
                conn = cups.Connection()
                printers = conn.getPrinters()
                if not printer_name:
                    printer_name = list(printers.keys())[0]
                conn.printFile(printer_name, str(path), "MCP print", {})
                return f"Enviado a {printer_name}"
            else:
                return "Impresión no soportada en este SO."
        except Exception as e:
            return f"Error: {e}"
else:
    def print_file(file_path: str, printer_name: str = "") -> str:
        return "❌ Impresión no soportada."

# ---------- Abrir archivo ----------
def open_file_with_default_app(file_path: str) -> str:
    try:
        target = safe_path(file_path)
        if not target.exists():
            return f"Error: El archivo '{file_path}' no existe."
        if os.name == 'nt':
            os.startfile(str(target))
        else:
            _subprocess_run(['xdg-open', str(target)], check=True)
        return f"Abriendo '{target.name}' con su aplicación predeterminada."
    except Exception as e:
        return f"Error al abrir: {e}"

# ---------- Reporte diario ----------
def daily_report() -> str:
    today = time.strftime("%Y-%m-%d")
    report = []
    db_dir = get_db_dir()
    workspace = get_workspace()
    if settings.get("enable_todo", True):
        conn = sqlite3.connect(str(db_dir / "todo.db"))
        cur = conn.execute("SELECT task FROM tasks WHERE done = 1 AND date(created) = date('now')")
        tasks = cur.fetchall()
        report.append(f"✅ Tareas completadas hoy: {len(tasks)}")
        for t in tasks[:10]:
            report.append(f"   - {t[0]}")
        conn.close()
    try:
        root = workspace
        modified = []
        for p in root.rglob("*"):
            if p.is_file() and time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)) == today:
                modified.append(str(p))
        report.append(f"\n📄 Archivos modificados hoy: {len(modified)} (primeros 10)")
        for p in modified[:10]:
            report.append(f"   - {p}")
    except Exception:
        pass
    return "\n".join(report)

# ============================================================
# RAG
# ============================================================
class NomicMCPEmbeddingFunction(embedding_functions.EmbeddingFunction):
    def __init__(self, server_url: str = "http://localhost:8082", batch_size: int = 10, timeout: int = 120):
        self.server_url = server_url
        self.batch_size = batch_size
        self.timeout = timeout

    def __call__(self, input: List[str]) -> List[List[float]]:
        prefixed = [f"search_document: {text}" for text in input]
        all_embeddings = []
        for i in range(0, len(prefixed), self.batch_size):
            batch = prefixed[i:i+self.batch_size]
            resp = requests.post(
                f"{self.server_url}/v1/embeddings",
                json={"input": batch},
                timeout=self.timeout
            )
            if resp.status_code != 200:
                raise Exception(f"Error HTTP {resp.status_code}: {resp.text}")
            data = resp.json()
            all_embeddings.extend([item["embedding"] for item in data["data"]])
        return all_embeddings

    def __getstate__(self):
        return {"server_url": self.server_url, "batch_size": self.batch_size, "timeout": self.timeout}
    def __setstate__(self, state):
        self.server_url = state["server_url"]
        self.batch_size = state.get("batch_size", 10)
        self.timeout = state.get("timeout", 120)

_chroma_client = None
_embed_fn = None

def get_chroma_client():
    global _chroma_client, _embed_fn
    if _chroma_client is not None:
        return _chroma_client

    # Reutilizar el cliente de agent_core si ya existe (mismo path).
    try:
        from . import agent_core as _core
        existing = getattr(_core, "_core_chroma_client", None)
        if existing is not None:
            _chroma_client = existing
            return _chroma_client
    except Exception:
        pass

    if _embed_fn is None:
        _embed_fn = NomicMCPEmbeddingFunction(server_url="http://localhost:8082")
    try:
        os.makedirs(CHROMA_DB_PATH, exist_ok=True)
    except Exception:
        pass
    try:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    except Exception:
        try:
            _chroma_client = chromadb.PersistentClient(path="./chroma_db")
        except Exception:
            _chroma_client = None
    return _chroma_client

def get_active_collection():
    client = get_chroma_client()
    lib_name = get_active_library()
    if not lib_name:
        return None
    try:
        return client.get_collection(name=lib_name, embedding_function=_embed_fn)
    except Exception:
        return None

def rag_search(query: str, n_results: int = 10) -> str:
    col = get_active_collection()
    if col is None:
        return "⚠️ No hay biblioteca activa."

    import re
    doc_match = re.search(r'(?:tema|documento|archivo)\s+(["\']?)([\w\s.-]+)(["\']?)', query, re.IGNORECASE)
    where_filter = None
    if doc_match:
        doc_query = doc_match.group(2).strip().lower()
        try:
            all_meta = col.get(limit=10000, include=["metadatas"])
            fuentes = set()
            for meta in all_meta['metadatas']:
                fuente = meta.get('fuente', '')
                if fuente:
                    fuentes.add(fuente)
            
            matched_doc = None
            for fuente in fuentes:
                base = re.sub(r'\.[^.]+$', '', fuente)
                if doc_query == base.lower() or doc_query in base.lower() or base.lower() in doc_query:
                    matched_doc = fuente
                    break
            
            if matched_doc:
                where_filter = {"fuente": {"$eq": matched_doc}}
        except Exception:
            pass

    try:
        resp_dense = requests.post(
            "http://localhost:8082/v1/embeddings",
            json={"input": [f"search_query: {query}"]},
            timeout=5
        )
        if resp_dense.status_code != 200:
            return f"❌ Error en embeddings densos: {resp_dense.text}"
        q_emb_dense = resp_dense.json()["data"][0]["embedding"]

        client = get_chroma_client()
        sparse_fn = getattr(client, '_sparse_fn', None)
        q_emb_sparse = None
        if sparse_fn is not None:
            q_emb_sparse = sparse_fn([query])[0]
    except Exception as e:
        return f"❌ Error al generar embeddings: {e}"

    results_dense = col.query(
        query_embeddings=[q_emb_dense],
        n_results=n_results * 2,
        where=where_filter
    )
    docs_dense = results_dense['documents'][0] if results_dense['documents'] else []
    metas_dense = results_dense['metadatas'][0] if results_dense['metadatas'] else []
    ids_dense = results_dense['ids'][0] if results_dense['ids'] else []

    results_sparse = {'documents': [], 'metadatas': [], 'ids': []}
    if q_emb_sparse is not None:
        try:
            results_sparse = col.query(
                query_embeddings=[q_emb_sparse],
                n_results=n_results * 2,
                where=where_filter
            )
        except Exception:
            pass
    docs_sparse = results_sparse['documents'][0] if results_sparse['documents'] else []
    metas_sparse = results_sparse['metadatas'][0] if results_sparse['metadatas'] else []
    ids_sparse = results_sparse['ids'][0] if results_sparse['ids'] else []

    scores = {}
    k = 60
    for rank, doc_id in enumerate(ids_dense):
        if doc_id not in scores:
            scores[doc_id] = {
                'document': docs_dense[rank] if rank < len(docs_dense) else '',
                'metadata': metas_dense[rank] if rank < len(metas_dense) else {},
                'score': 0
            }
        scores[doc_id]['score'] += 1.0 / (k + rank + 1)

    for rank, doc_id in enumerate(ids_sparse):
        if doc_id not in scores:
            scores[doc_id] = {
                'document': docs_sparse[rank] if rank < len(docs_sparse) else '',
                'metadata': metas_sparse[rank] if rank < len(metas_sparse) else {},
                'score': 0
            }
        scores[doc_id]['score'] += 1.0 / (k + rank + 1)

    sorted_items = sorted(scores.items(), key=lambda x: x[1]['score'], reverse=True)[:n_results]

    if not sorted_items:
        return "No se encontraron documentos relevantes."

    output = "📚 Resultados relevantes:\n\n"
    for doc_id, data in sorted_items:
        fuente = data['metadata'].get('fuente', 'desconocido')
        chunk = data['metadata'].get('chunk', '')
        doc_preview = data['document'][:400] + "..." if len(data['document']) > 400 else data['document']
        output += f"📄 **{fuente}** (fragmento {chunk})\n"
        output += f"   {doc_preview}\n"
        output += f"   🎯 Score: {data['score']:.4f}\n\n"

    return output

# ------------------ Herramientas críticas solicitadas por el Plan ------------------
def view(path: str, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
    """Devuelve líneas numeradas de un archivo.
    Retorna diccionario con keys: status, lines (list), total_lines, path
    """
    try:
        target = safe_path(path)
        if not target.exists() or not target.is_file():
            return {"status": "error", "error": f"No existe el archivo: {path}"}
        text = target.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        total = len(lines)
        start = max(0, int(offset))
        end = min(total, start + int(limit))
        numbered = [f"{i+1:4}: {lines[i]}" for i in range(start, end)]
        return {"status": "ok", "path": str(target), "lines": numbered, "total_lines": total}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def edit(path: str, old_string: str, new_string: str) -> Dict[str, Any]:
    """Reemplazo exacto de `old_string` por `new_string` en `path`. Devuelve diff simplificado."""
    try:
        res = edit_file(path, old_string, new_string)
        if res.startswith("Error"):
            return {"status": "error", "error": res}
        return {"status": "ok", "result": res}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def search(query: str, path: str = ".") -> Dict[str, Any]:
    """Buscar texto en proyecto. Retorna lista de dicts {file, line_no, snippet} (limitado)."""
    try:
        base = safe_path(path)
        if not base.is_dir():
            return {"status": "error", "error": f"'{path}' no es directorio"}
        regex = re.compile(query, re.IGNORECASE)
        results = []
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for idx, line in enumerate(content.splitlines()):
                    if regex.search(line):
                        snippet = line.strip()
                        # add context: up to 60 chars around match
                        m = regex.search(line)
                        if m:
                            s = max(0, m.start()-30)
                            e = min(len(line), m.end()+30)
                            ctx = line[s:e].strip()
                        else:
                            ctx = snippet
                        results.append({"file": str(p.relative_to(base)), "line": idx+1, "content": snippet, "context": ctx})
                        break
            if len(results) >= 200:
                break
        return {"status": "ok", "hits": results}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def bash(command: str, timeout: int = 30) -> Dict[str, Any]:
    command = _resolve_python_in_command(command)
    try:
        if not is_command_allowed(command):
            return {"status": "denied", "error": f"Comando denegado: '{command}' está en la lista negra de comandos peligrosos. "
                     f"Para ejecutar esta acción, usa herramientas especializadas como run_python_code, "
                     f"sqlite_execute, o edita la configuración de blacklist."}
        # Async: pasa por job_manager. Comandos cortos devuelven stdout/stderr;
        # los largos devuelven {status:'job', job_id} y aparecen en la consola.
        res = job_manager.start_job(command, shell='auto', cwd=str(get_workspace()),
                                    wait_seconds=max(0.5, min(float(timeout), 2.5)))
        status = res.get('status')
        if status in ('exited', 'error', 'killed'):
            out = '\n'.join(res.get('output') or [])
            if status == 'error':
                # Mismo criterio que las tools de texto: el motivo + la salida
                # real (stderr va mezclado en stdout) para que el modelo vea por
                # qué falló en lugar de un "error": None sin explicación.
                return {"status": "error",
                        "error": (res.get('error') or
                                  f"proceso terminado con código {res.get('exit_code')}"),
                        "stdout": out, "stderr": "",
                        "returncode": res.get('exit_code')}
            return {"status": "ok", "stdout": out, "stderr": "",
                    "returncode": res.get('exit_code')}
        # En ejecución → job_id; el modelo consulta con job_output.
        return {
            "status": "job",
            "job_id": res.get('job_id'),
            "message": (f"Comando en ejecución ({res.get('job_id')}). "
                        f"{res.get('hint') or ''} Usa job_output('{res.get('job_id')}') para leer la salida."),
            "preview": '\n'.join(res.get('preview') or []),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
    
def tree(path: str = ".", depth: int = 3) -> Dict[str, Any]:
    """Genera estructura tipo árbol en formato texto, ignorando .git y __pycache__."""
    try:
        base = safe_path(path)
        if not base.exists():
            return {"status": "error", "error": f"Ruta no encontrada: {path}"}
        def _walk(p: Path, curr_depth: int):
            if curr_depth < 0:
                return []
            out = []
            try:
                for child in sorted(p.iterdir()):
                    name = child.name
                    if name in ('.git', '__pycache__'):
                        continue
                    prefix = 'D' if child.is_dir() else 'F'
                    out.append((prefix, str(child.relative_to(base)), child))
                    if child.is_dir():
                        out.extend(_walk(child, curr_depth-1))
            except Exception:
                pass
            return out
        items = _walk(base, int(depth))
        # Format with indentation based on path depth
        lines = []
        for typ, rel, child in items:
            indent = "  " * (len(Path(rel).parts) - 1)
            symbol = "📁" if typ == 'D' else "📄"
            lines.append(f"{indent}{symbol} {rel}")
        return {"status": "ok", "tree": "\n".join(lines)}
    except Exception as e:
        return {"status": "error", "error": str(e)}

# ============================================================
# HERRAMIENTAS MCP PARA LA WEBUI (NO EXPUESTAS AL MODELO)
# ============================================================

def _get_runtime_orchestrator():
    """Obtiene el Orchestrator real si ya está inicializado en la misma instancia."""
    try:
        from .mcp_server import get_orchestrator as _get_orchestrator
        return _get_orchestrator()
    except Exception:
        try:
            from tools.mcp_server import get_orchestrator as _get_orchestrator
            return _get_orchestrator()
        except Exception:
            return None


def agent_status() -> str:
    """Devuelve el estado actual del agente y del orchestrator real.

    IMPORTANTE: este endpoint se pollea desde la WebUI cada pocos segundos.
    Antes llamaba a `_get_runtime_orchestrator()`, que creaba el Orchestrator
    (y arrancaba el indexer del workspace) con la primera petición, aunque
    el usuario no hubiera lanzado ninguna tarea. Ahora consulta el singleton
    existente sin forzar su creación.
    """
    try:
        from .daemon_agent import get_daemon_instance
        daemon = get_daemon_instance()
        # Consulta ligera: usa el singleton si ya existe; no lo crea.
        orchestrator = None
        try:
            from . import mcp_server as _mcp
            orchestrator = getattr(_mcp, "_orchestrator", None)
        except Exception:
            pass
        agent_names = list(getattr(orchestrator, "agents", {}).keys()) if orchestrator is not None else []
        if daemon and daemon.is_running:
            status = daemon.get_status()
            payload = {
                "mode": "DAEMON",
                "confidence": 0.9,
                "task": status.get("objective", "Trabajando..."),
                "memory": [],
                "skills": agent_names,
                "plan": None,
                "orchestrator": {
                    "workspace_path": str(getattr(orchestrator, "workspace_path", "")),
                    "agents": agent_names,
                    "agents_count": len(agent_names),
                },
            }
        else:
            payload = {
                "mode": "GENERAL",
                "confidence": 0.5,
                "task": "Esperando instrucciones...",
                "memory": [],
                "skills": agent_names,
                "plan": None,
                "orchestrator": {
                    "workspace_path": str(getattr(orchestrator, "workspace_path", "")),
                    "agents": agent_names,
                    "agents_count": len(agent_names),
                },
            }
        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"error": str(e)})

def agent_memory(limit: int = 5) -> str:
    """Devuelve las últimas memorias episódicas."""
    try:
        from .episodic_memory import EpisodicMemory
        mem = EpisodicMemory()
        recent = mem.fetch_recent(limit=limit)
        output = [{"task": item.get("task", "")[:100], "outcome": item.get("outcome", ""), "timestamp": item.get("timestamp", 0)} for item in recent]
        return json.dumps(output)
    except Exception as e:
        return json.dumps({"error": str(e)})

def agent_skills(query: str = "") -> str:
    """Lista los skills que el agente usa: los ACTIVOS (o los relevantes a `query`).

    Antes devolvía `search_skills(..., include_auto=True)`, lo que anulaba el
    filtro `active` de ChromaDB y hacía que la UI (y el bloque de contexto del
    chat) pasaran skills desactivados. Ahora el checkbox del modal es la única
    fuente de verdad y aquí se explica ese estado al modelo.
    """
    try:
        sm = get_skill_manager(str(get_workspace()))
        if query.strip():
            skills = sm.search_skills(query, top_k=10, only_active=True)
        else:
            skills = sm.get_active_skills()
        output = [
            {
                "name": s.get("name"),
                "description": (s.get("description") or "")[:200],
                "confidence": s.get("confidence", 0.5),
                "tags": s.get("tags") or [],
                "scope": s.get("scope"),
                "path": s.get("path"),
                "active": True,
            }
            for s in skills
        ]
        return json.dumps(output)
    except Exception as e:
        return json.dumps({"error": str(e)})

def agent_plan() -> str:
    """Devuelve el plan actual si existe."""
    try:
        state_path = get_workspace() / ".daemon" / "daemon_state.json"
        if state_path.exists():
            with open(state_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                plan = data.get("plan", [])
                active = [t for t in plan if t.get("status") in ["pending", "in_progress"]]
                return json.dumps({"steps": active[:10], "total": len(plan)})
        return json.dumps({"steps": [], "total": 0})
    except Exception as e:
        return json.dumps({"error": str(e)})

def agent_goal(text: str) -> str:
    """Establece un nuevo objetivo para el daemon."""
    try:
        from .daemon_agent import start_daemon
        result = start_daemon(text, str(get_workspace()))
        return json.dumps({"status": "success", "message": result})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def agent_pause() -> str:
    """Pausa el daemon."""
    from .daemon_agent import get_daemon_instance
    daemon = get_daemon_instance()
    if daemon:
        daemon.pause()
        return json.dumps({"status": "paused"})
    return json.dumps({"status": "error", "message": "Daemon no disponible"})

def agent_resume() -> str:
    """Reanuda el daemon."""
    from .daemon_agent import get_daemon_instance
    daemon = get_daemon_instance()
    if daemon:
        daemon.resume()
        return json.dumps({"status": "resumed"})
    return json.dumps({"status": "error", "message": "Daemon no disponible"})

# ------------------ Tool schemas, registry y ejecución con validación/retry ------------------
try:
    from jsonschema import validate as jsonschema_validate, ValidationError as JSONSchemaValidationError
except Exception:
    jsonschema_validate = None
    JSONSchemaValidationError = Exception

TOOL_SCHEMAS = {
    "view": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1}
        },
        "required": ["path"]
    },
    "edit": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"}
        },
        "required": ["path", "old_string", "new_string"]
    },
    "search": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "path": {"type": "string"}},
        "required": ["query"]
    },
    "bash": {
        "type": "object",
        "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1}},
        "required": ["command"]
    },
    "tree": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "depth": {"type": "integer", "minimum": 0}},
        "required": []
    }
    ,
    "read_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string"}
        },
        "required": ["path"]
    },
    "write_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["w", "a"], "description": "w=escribir, a=append"}
        },
        "required": ["path", "content"]
    },
    "edit_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "description": "Texto a buscar"},
            "new_text": {"type": "string", "description": "Texto de reemplazo"},
            "operation": {"type": "string", "enum": ["replace", "append", "insert_after", "insert_before", "delete"], "description": "Operacion a realizar"}
        },
        "required": ["path"]
    },
    "list_directory": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "depth": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": []
    },
    "query": {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "path": {"type": "string"},
            "mode": {"type": "string", "enum": ["search", "read", "rag"]}
        },
        "required": ["q"]
    },
    "execute": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "args": {"type": "object"}
        },
        "required": ["tool_name"]
    },
    "grep_search": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
        "required": ["pattern"]
    },
    "agent_status": {"type": "object", "properties": {}, "required": []},
    "agent_memory": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []},
    "agent_skills": {"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
    "agent_plan": {"type": "object", "properties": {}, "required": []},
    "agent_goal": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    "agent_pause": {"type": "object", "properties": {}, "required": []},
    "agent_resume": {"type": "object", "properties": {}, "required": []}
}

TOOL_IMPL = {
    "view": view,
    "edit": edit,
    "search": search,
    "bash": bash,
    "tree": tree,

    # convenience mappings
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
    "grep_search": grep_search,

    # jobs en segundo plano
    "workspace_download_link": workspace_download_link,
    "run_command_async": run_command_async,
    "job_status": job_status,
    "job_output": job_output,
    "job_kill": job_kill,
    "job_list": job_list,

    "agent_status": agent_status,
    "agent_memory": agent_memory,
    "agent_skills": agent_skills,
    "agent_plan": agent_plan,
    "agent_goal": agent_goal,
    "agent_pause": agent_pause,
    "agent_resume": agent_resume,
}

_tool_failures = {}
_tool_disabled_until = {}

def get_openai_tools() -> List[Dict[str, Any]]:
    """Devuelve la lista de herramientas en formato compatible con llama.cpp/OpenAI.

    Solo se exponen las 2 meta-herramientas del servidor MCP de dev_agent:
    query_tools (descubrir) y execute_tool (ejecutar), con los MISMOS nombres
    que expone el MCP en http://127.0.0.1:15000/mcp.
    """
    # El servidor llama.cpp espera schemas tipo OpenAI function-calling, no un dict plano con "parameters".
    query_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "La tarea o consulta en lenguaje natural."}
        },
        "required": ["query"],
    }
    execute_schema = {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "Nombre exacto de la herramienta interna."},
            "arguments": {"type": "object", "description": "Argumentos de la herramienta."},
        },
        "required": ["tool_name"],
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "query_tools",
                "description": (
                    "Descubre qué herramienta interna usar para una tarea. "
                    "Busca en el catálogo y devuelve las más relevantes. "
                    "NO lee archivos ni busca contenido en el workspace. "
                    "Las tools base (read_file, write_file, edit_file, file_glob_search, "
                    "grep_search, exec_shell_command, get_datetime, list_directory, "
                    "run_python_code) están disponibles directamente vía execute_tool. "
                    "Formato: query_tools(query='describe la tarea')."
                ),
                "parameters": query_schema,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "execute_tool",
                "description": (
                    "Ejecuta una herramienta interna por nombre. "
                    "Argumentos: tool_name y arguments. "
                    "Si no sabes el nombre exacto, llama primero a query_tools. "
                    "Usa {} cuando la herramienta no tenga argumentos.\n\n"
                    + BASE_TOOLS_CHEATSHEET
                ),
                "parameters": execute_schema,
            },
        },
    ]


def execute_tool(name: str, args: Dict[str, Any], max_retries: int = 3) -> Dict[str, Any]:
    # Normalizar parámetros que deben ser JSON string
    if name in TOOLS_JSON_PARAMS:
        for param in TOOLS_JSON_PARAMS[name]:
            if param in args and isinstance(args[param], (list, dict)):
                args[param] = json.dumps(args[param], ensure_ascii=False)
    """Ejecuta una herramienta validando argumentos y aplicando retry + circuit-breaker.

    Retorna dict con keys: status (success/error/denied/timeout), data o error.
    """
    now = time.time()
    # Circuit breaker: check disabled
    disabled_until = _tool_disabled_until.get(name)
    if disabled_until and now < disabled_until:
        return {"status": "disabled", "error": f"Tool '{name}' disabled until {disabled_until}"}

    if name not in TOOL_IMPL:
        return {"status": "error", "error": f"Tool '{name}' no registrada"}

    schema = TOOL_SCHEMAS.get(name)
    if schema and jsonschema_validate is not None:
        try:
            jsonschema_validate(instance=args or {}, schema=schema)
        except JSONSchemaValidationError as e:
            return {"status": "error", "error": f"Argument validation failed: {e}"}

    attempt = 0
    backoffs = [0, 2, 5]
    last_exc = None
    while attempt < max_retries:
        try:
            func = TOOL_IMPL[name]
            result = func(**(args or {}))
            # If tool returns a dict with its own status, respect it
            if isinstance(result, dict) and result.get('status') in ('denied', 'error', 'timeout'):
                return {"status": result.get('status'), "error": result.get('error', None), "data": result}
            # Success: reset failures counter
            _tool_failures[name] = 0
            return {"status": "success", "data": result}
        except Exception as e:
            last_exc = e
            attempt += 1
            _tool_failures[name] = _tool_failures.get(name, 0) + 1
            if attempt < max_retries:
                time.sleep(backoffs[min(attempt, len(backoffs)-1)])
            else:
                # If failures exceed threshold, disable temporarily
                if _tool_failures.get(name, 0) >= 3:
                    _tool_disabled_until[name] = time.time() + 300  # disable 5 minutes
                return {"status": "error", "error": f"Tool execution failed after {attempt} attempts: {e}"}



def list_libraries() -> str:
    client = get_chroma_client()
    if client is None:
        return "❌ ChromaDB no disponible."

    active = get_active_library()
    if not active:
        return "⚠️ No hay biblioteca activa. Usa set_active_library primero."

    try:
        col = client.get_collection(name=active, embedding_function=_embed_fn)
        total = col.count()
        if total == 0:
            return f"📚 Biblioteca '{active}' está vacía."

        limit = min(total, 10000)
        results = col.get(limit=limit, include=["metadatas"])
        if not results['metadatas']:
            return f"📚 Biblioteca '{active}' no contiene metadatos de fuente."

        fuentes = {}
        for meta in results['metadatas']:
            fuente = meta.get('fuente', 'desconocido')
            fuentes[fuente] = fuentes.get(fuente, 0) + 1

        output = f"📚 Documentos en biblioteca '{active}':\n"
        for fuente, count in sorted(fuentes.items()):
            output += f"  - {fuente} ({count} fragmentos)\n"

        return output
    except Exception as e:
        return f"❌ Error al listar documentos: {e}"

def set_active_library(name: str) -> str:
    client = get_chroma_client()
    try:
        client.get_collection(name)
        _set_active_library_state(name)
        return f"✅ Biblioteca activa cambiada a '{name}'."
    except Exception as e:
        return f"❌ Error: {e}"

# ============================================================
# MEMORIA SEMÁNTICA
# ============================================================
MEMORY_COLLECTION_NAME = "agent_semantic_memory"

def get_memory_collection():
    client = get_chroma_client()
    if client is None:
        raise RuntimeError("ChromaDB no disponible")
    global _embed_fn
    if _embed_fn is None:
        _embed_fn = NomicMCPEmbeddingFunction(server_url="http://localhost:8082")
    try:
        collection = client.get_collection(name=MEMORY_COLLECTION_NAME, embedding_function=_embed_fn)
    except:
        collection = client.create_collection(name=MEMORY_COLLECTION_NAME, embedding_function=_embed_fn)
    return collection

def store_memory(content: str, metadata: dict = None) -> str:
    try:
        col = get_memory_collection()
        doc_id = f"mem_{int(time.time())}_{hash(content) % 1000000}"
        meta = metadata or {}
        meta["timestamp"] = time.time()
        col.add(documents=[content], ids=[doc_id], metadatas=[meta])
        return f"🧠 Memoria guardada: {content[:100]}..."
    except Exception as e:
        return f"❌ Error guardando memoria: {e}"

def retrieve_memories(query: str, k: int = 10) -> str:
    try:
        resp = requests.post(
            "http://localhost:8082/v1/embeddings",
            json={"input": [f"search_query: {query}"]},
            timeout=60
        )
        if resp.status_code != 200:
            return f"Error en embeddings: {resp.text}"
        query_emb = resp.json()["data"][0]["embedding"]
        col = get_memory_collection()
        results = col.query(query_embeddings=[query_emb], n_results=k)
        if not results['documents'][0]:
            return "No se encontraron memorias relevantes."
        output = "🧠 **Memorias relevantes:**\n"
        for doc in results['documents'][0]:
            output += f"- {doc}\n"
        return output
    except Exception as e:
        return f"❌ Error: {e}"

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message: str) -> str:
    if not settings.get("enable_telegram", True):
        return "❌ Telegram está desactivado."
    try:
        secrets = load_secrets()
        token = secrets.get("telegram_bot_token")
        chat = secrets.get("telegram_chat_id")
        if not token or not chat:
            return "❌ Token o chat_id no configurados."
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        response = requests.post(url, json={"chat_id": chat, "text": message}, timeout=10)
        if response.status_code == 200:
            return f"✅ Mensaje enviado: {message[:50]}..."
        else:
            return f"❌ Error: {response.text}"
    except Exception as e:
        return f"❌ Error: {e}"

def setup_telegram(bot_token: str, chat_id: str) -> str:
    try:
        secrets = load_secrets()
        secrets["telegram_bot_token"] = bot_token
        secrets["telegram_chat_id"] = chat_id
        save_secrets(secrets)
        test_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        test_response = requests.post(test_url, json={"chat_id": chat_id, "text": "✅ Credenciales configuradas."}, timeout=10)
        if test_response.status_code != 200:
            return f"❌ Credenciales inválidas: {test_response.text}"
        return "✅ Credenciales guardadas y probadas."
    except Exception as e:
        return f"❌ Error: {e}"

# ============================================================
# NUEVAS HERRAMIENTAS (Descarga, Git, WebSearch, Watchdog, etc.)
# ============================================================
def download_file(url: str, save_path: str) -> str:
    try:
        target = safe_path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not REQUESTS_AVAILABLE:
            return "❌ requests no instalado."
        import requests
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0
        with open(target, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
        return f"✅ Descargado: {target} ({downloaded} bytes)"
    except Exception as e:
        return f"❌ Error: {e}"

def _run_git(args: List[str], cwd: str = None) -> str:
    if cwd is None:
        workspace = get_workspace()
        cwd = str(workspace)
    try:
        result = _subprocess_run(['git'] + args, cwd=cwd, timeout=30)
        if result.returncode != 0:
            return f"❌ Error git: {result.stderr.strip()}"
        return result.stdout.strip() or "(sin salida)"
    except subprocess.TimeoutExpired:
        return "❌ Timeout"
    except FileNotFoundError:
        return "❌ Git no instalado."

def git_status() -> str:
    return _run_git(['status', '--short'])

def git_commit(message: str, files: List[str] = None) -> str:
    if files is None:
        return ("Error: Debes especificar qué archivos añadir con el parámetro 'files' "
                "(ej. files=['archivo1.py', 'archivo2.py']). "
                "Para añadir todos (usa con precaución), pasa files=['.'].")
    # Añadir archivos específicos
    add_cmd = ['add'] + files
    add_out = _run_git(add_cmd)
    if "Error" in add_out:
        return add_out
    return _run_git(['commit', '-m', message])

def git_diff() -> str:
    return _run_git(['diff'])

def git_log(limit: int = 10) -> str:
    return _run_git(['log', f'-n{limit}', '--oneline'])

_WATCHDOG_EVENTS = []
_WATCHDOG_OBSERVER = None
_WATCHDOG_FOLDER = None
_WATCHDOG_STOP = False

if WATCHDOG_AVAILABLE:
    def start_watchdog(folder: str, recursive: bool = True) -> str:
        global _WATCHDOG_OBSERVER, _WATCHDOG_FOLDER, _WATCHDOG_EVENTS, _WATCHDOG_STOP
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            if _WATCHDOG_OBSERVER and _WATCHDOG_OBSERVER.is_alive():
                return "⚠️ Watchdog ya está en ejecución."
            folder_path = safe_path(folder)
            if not folder_path.is_dir():
                return f"❌ No es un directorio: {folder_path}"

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    global _WATCHDOG_EVENTS
                    if event.is_directory and not recursive:
                        return
                    _WATCHDOG_EVENTS.append({
                        "event_type": event.event_type,
                        "src_path": event.src_path,
                        "dest_path": event.dest_path if hasattr(event, 'dest_path') else None,
                        "is_directory": event.is_directory,
                        "timestamp": time.time()
                    })
                    if len(_WATCHDOG_EVENTS) > 1000:
                        _WATCHDOG_EVENTS = _WATCHDOG_EVENTS[-500:]

            _WATCHDOG_STOP = False
            _WATCHDOG_EVENTS = []
            _WATCHDOG_OBSERVER = Observer()
            _WATCHDOG_OBSERVER.schedule(Handler(), str(folder_path), recursive=recursive)
            _WATCHDOG_OBSERVER.start()
            _WATCHDOG_FOLDER = str(folder_path)
            return f"✅ Watchdog iniciado en {folder_path} (recursivo={recursive})"
        except ImportError:
            return "❌ watchdog no instalado."
        except Exception as e:
            return f"❌ Error: {e}"

    def stop_watchdog() -> str:
        global _WATCHDOG_OBSERVER, _WATCHDOG_STOP
        try:
            if _WATCHDOG_OBSERVER and _WATCHDOG_OBSERVER.is_alive():
                _WATCHDOG_STOP = True
                _WATCHDOG_OBSERVER.stop()
                _WATCHDOG_OBSERVER.join(timeout=2)
                _WATCHDOG_OBSERVER = None
                return "✅ Watchdog detenido."
            return "⚠️ Watchdog no estaba en ejecución."
        except Exception as e:
            return f"❌ Error: {e}"

    def get_watchdog_events(limit: int = 20) -> str:
        global _WATCHDOG_EVENTS
        if not _WATCHDOG_EVENTS:
            return "📭 No hay eventos."
        events = _WATCHDOG_EVENTS[-limit:]
        output = []
        for e in events:
            ts = time.strftime("%H:%M:%S", time.localtime(e['timestamp']))
            msg = f"[{ts}] {e['event_type']}: {e['src_path']}"
            if e['dest_path']:
                msg += f" -> {e['dest_path']}"
            output.append(msg)
        return "\n".join(output)
else:
    def start_watchdog(folder: str, recursive: bool = True) -> str:
        return "❌ watchdog no instalado."
    def stop_watchdog() -> str:
        return "❌ watchdog no instalado."
    def get_watchdog_events(limit: int = 20) -> str:
        return "❌ watchdog no instalado."

def convert_document(input_path: str, output_format: str, output_path: str = None) -> str:
    try:
        src = safe_path(input_path)
        if not src.exists():
            return f"❌ Archivo no encontrado."
        if output_path is None:
            output_path = src.with_suffix(f'.{output_format}').name
        dst = safe_path(output_path)
        try:
            cmd = ['pandoc', str(src), '-o', str(dst), '--wrap=preserve']
            result = _subprocess_run(cmd, timeout=60)
            if result.returncode == 0:
                return f"✅ Convertido: {src} -> {dst}"
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            return "❌ Timeout en pandoc."
        ext = src.suffix.lower()
        if ext == '.txt' and output_format == 'html':
            content = src.read_text(encoding='utf-8')
            html = f"<html><body><pre>{content}</pre></body></html>"
            dst.write_text(html, encoding='utf-8')
            return f"✅ Convertido (fallback): {src} -> {dst}"
        elif ext == '.md' and output_format == 'html':
            try:
                import markdown
                md_content = src.read_text(encoding='utf-8')
                html = markdown.markdown(md_content)
                html_full = f"<html><body>{html}</body></html>"
                dst.write_text(html_full, encoding='utf-8')
                return f"✅ Convertido (markdown): {src} -> {dst}"
            except ImportError:
                return "❌ Instala markdown para convertir MD a HTML."
        return f"❌ No se pudo convertir. Instala pandoc."
    except Exception as e:
        return f"❌ Error: {e}"

def list_drives() -> str:
    try:
        import psutil
        output = []
        for partition in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                total_gb = usage.total / (1024**3)
                used_gb = usage.used / (1024**3)
                free_gb = usage.free / (1024**3)
                output.append(
                    f"📁 {partition.device} ({partition.mountpoint})\n"
                    f"   Total: {total_gb:.2f} GB | Usado: {used_gb:.2f} GB | Libre: {free_gb:.2f} GB ({usage.percent}%)"
                )
            except PermissionError:
                output.append(f"📁 {partition.device} ({partition.mountpoint}) - Acceso denegado")
        return "\n\n".join(output) if output else "No se encontraron unidades."
    except Exception as e:
        return f"❌ Error: {e}"

def classify_text(text: str, categories: List[str]) -> str:
    try:
        if not text or len(text.strip()) < 10:
            return "❌ Texto demasiado corto."
        categories_str = ", ".join(categories)
        prompt = f"Clasifica el siguiente texto en una de estas categorías: {categories_str}.\n\nTexto: {text}\n\nResponde SOLO con la categoría y una breve justificación."
        import requests
        response = requests.post(
            "http://localhost:8081/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.1
            },
            timeout=30
        )
        if response.status_code != 200:
            return f"❌ Error del LLM: {response.text}"
        data = response.json()
        result = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        return f"🏷️ **Clasificación:**\n{result}"
    except Exception as e:
        return f"❌ Error: {e}"

# ============================================================
# HERRAMIENTAS DE ANÁLISIS Y CALIDAD DE CÓDIGO
# ============================================================

def lint_code(path: str) -> str:
    """Analiza el código fuente con flake8 en busca de errores de estilo y problemas comunes."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        if target.is_file() and not target.name.endswith('.py'):
            return "Error: Solo se pueden analizar archivos .py"
        cmd = ['flake8', str(target), '--max-line-length=120', '--count', '--statistics']
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if output.strip():
            return f"⚠️ Problemas de estilo encontrados:\n{output}"
        return "✅ El código pasa la verificación de estilo (flake8)."
    except FileNotFoundError:
        return ("❌ flake8 no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install flake8\")")
    except Exception as e:
        return f"❌ Error ejecutando flake8: {e}"

def check_security(path: str) -> str:
    """Analiza el código en busca de vulnerabilidades de seguridad con bandit."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        if target.is_file() and not target.name.endswith('.py'):
            return "Error: Solo se pueden analizar archivos .py"
        cmd = ['bandit', '-r', str(target), '-f', 'txt']
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if output.strip():
            return f"⚠️ Problemas de seguridad encontrados:\n{output}"
        return "✅ No se encontraron problemas de seguridad (bandit)."
    except FileNotFoundError:
        return ("❌ bandit no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install bandit\")")
    except Exception as e:
        return f"❌ Error ejecutando bandit: {e}"

def type_check(path: str) -> str:
    """Verifica la consistencia de tipos con pyright (alternativa moderna a mypy)."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        if target.is_file() and not target.name.endswith('.py'):
            return "Error: Solo se pueden analizar archivos .py"
        cmd = ['pyright', str(target), '--outputjson']
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            # pyright devuelve código 0 incluso con advertencias, pero capturamos errores
            if '"summary"' in output:
                # Intentar parsear JSON para extraer resumen
                import json
                try:
                    data = json.loads(output)
                    summary = data.get("summary", {})
                    errors = summary.get("errorCount", 0)
                    warnings = summary.get("warningCount", 0)
                    if errors > 0:
                        return f"⚠️ {errors} errores de tipos encontrados (pyright):\n{output[:500]}..."
                    elif warnings > 0:
                        return f"⚠️ {warnings} advertencias de tipos (pyright):\n{output[:500]}..."
                    else:
                        return "✅ La verificación de tipos es correcta (pyright)."
                except:
                    pass
            return f"⚠️ Problemas de tipos encontrados:\n{output[:500]}..."
        return "✅ La verificación de tipos es correcta (pyright)."
    except FileNotFoundError:
        return ("❌ pyright no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install pyright\")")
    except Exception as e:
        return f"❌ Error ejecutando pyright: {e}"

def measure_complexity(path: str) -> str:
    """Mide la complejidad ciclomática del código con radon."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        if target.is_file() and not target.name.endswith('.py'):
            return "Error: Solo se pueden analizar archivos .py"
        cmd = ['radon', 'cc', str(target), '-a', '-s']
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if output.strip():
            return f"📊 Complejidad ciclomática:\n{output}"
        return "✅ No se encontraron funciones con complejidad alta."
    except FileNotFoundError:
        return ("❌ radon no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install radon\")")
    except Exception as e:
        return f"❌ Error ejecutando radon: {e}"
    
# ============================================================
# HERRAMIENTAS DE PRUEBAS
# ============================================================

def run_tests(path: str, test_path: str = None) -> str:
    """Ejecuta pruebas unitarias con pytest."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        # Si se proporciona test_path, usarlo, sino buscar tests/ en el directorio
        if test_path:
            test_target = safe_path(test_path)
        else:
            # Buscar carpeta tests/ o archivos test_*.py
            possible_tests = list(target.glob("tests/")) + list(target.glob("test_*.py"))
            if not possible_tests:
                return "⚠️ No se encontraron pruebas. Crea una carpeta 'tests/' o archivos 'test_*.py'."
            test_target = possible_tests[0]
        cmd = ['pytest', str(test_target), '-v', '--tb=short']
        result = _subprocess_run(cmd, timeout=60)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return f"❌ Pruebas fallidas:\n{output}"
        return f"✅ Todas las pruebas pasaron:\n{output}"
    except FileNotFoundError:
        return ("❌ pytest no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install pytest\")")
    except Exception as e:
        return f"❌ Error ejecutando pytest: {e}"

def generate_unit_tests(path: str, function_name: str = None) -> str:
    """Genera pruebas unitarias para una función o archivo usando IA (requiere chat_completion)."""
    # Esta función usará chat_completion para generar el código de tests.
    # Por ahora, devolvemos un mensaje de que está en desarrollo.
    return "⚠️ La generación automática de pruebas está en desarrollo. Usa run_tests para ejecutar pruebas existentes."


# ============================================================
# HERRAMIENTAS DE DOCUMENTACIÓN
# ============================================================

def generate_docstrings(path: str) -> str:
    """Genera docstrings para funciones/métodos usando IA (requiere chat_completion)."""
    # Se implementará en el agente usando chat_completion + edit_file
    return "⚠️ La generación automática de docstrings está en desarrollo."

def generate_readme(path: str, project_name: str = "Proyecto") -> str:
    """Genera un archivo README.md para el proyecto usando IA."""
    # Se implementará en el agente usando chat_completion + write_file
    return "⚠️ La generación automática de README está en desarrollo."

def generate_api_docs(path: str) -> str:
    """Genera documentación API con sphinx o pdoc."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        # Usamos pdoc3 (más ligero)
        cmd = ['pdoc', str(target), '--html', '--output-dir', str(target / 'docs')]
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return f"❌ Error generando documentación:\n{output}"
        return f"✅ Documentación generada en {target / 'docs'}"
    except FileNotFoundError:
        return ("❌ pdoc no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install pdoc3\")")
    except Exception as e:
        return f"❌ Error ejecutando pdoc: {e}"

# ============================================================
# HERRAMIENTAS DE FORMATEO Y LIMPIEZA
# ============================================================

def format_code(path: str) -> str:
    """Formatea el código usando black."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        cmd = ['black', str(target)]
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return f"❌ Error formateando:\n{output}"
        return f"✅ Código formateado con black:\n{output}"
    except FileNotFoundError:
        return ("❌ black no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install black\")")
    except Exception as e:
        return f"❌ Error ejecutando black: {e}"

def sort_imports(path: str) -> str:
    """Ordena y agrupa los imports usando isort."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        cmd = ['isort', str(target)]
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return f"❌ Error ordenando imports:\n{output}"
        return f"✅ Imports ordenados con isort:\n{output}"
    except FileNotFoundError:
        return ("❌ isort no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install isort\")")
    except Exception as e:
        return f"❌ Error ejecutando isort: {e}"

def remove_unused_imports(path: str) -> str:
    """Elimina imports no utilizados usando autoflake."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        cmd = ['autoflake', '--in-place', '--remove-unused-variables', '--remove-all-unused-imports', str(target)]
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return f"❌ Error eliminando imports no usados:\n{output}"
        return f"✅ Imports no usados eliminados:\n{output}"
    except FileNotFoundError:
        return ("❌ autoflake no está instalado. Puedes instalarlo con:\n"
                "run_command_async(\"pip install autoflake\")")
    except Exception as e:
        return f"❌ Error ejecutando autoflake: {e}"

def clean_code(path: str) -> str:
    """Limpia el código: elimina imports no usados, ordena imports, formatea con black."""
    results = []
    results.append(remove_unused_imports(path))
    results.append(sort_imports(path))
    results.append(format_code(path))
    return "\n".join(results)

# ============================================================
# HERRAMIENTAS DE SEGURIDAD Y PROTECCIÓN
# ============================================================

def add_license(path: str, license_type: str = "MIT") -> str:
    """Añade un encabezado de licencia a los archivos .py."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        # Definir plantillas de licencia (simplificado)
        licenses = {
            "MIT": "MIT License\n\nCopyright (c) {year} {author}\n\nPermission is hereby granted...",
            "GPL": "GNU General Public License v3.0\n\nCopyright (C) {year} {author}\n\nThis program is free software...",
            "Apache": "Apache License 2.0\n\nCopyright (c) {year} {author}\n\nLicensed under the Apache License..."
        }
        # Obtener año y autor (del sistema o preguntar)
        year = time.strftime("%Y")
        author = os.getenv("USER", "usuario")
        license_text = licenses.get(license_type, licenses["MIT"]).format(year=year, author=author)
        # Buscar archivos .py y añadir el encabezado si no existe
        py_files = []
        if target.is_dir():
            py_files = list(target.glob("**/*.py"))
        else:
            py_files = [target]
        modified = []
        for py_file in py_files:
            content = py_file.read_text(encoding='utf-8')
            if "# License" in content or "Licensed under" in content:
                continue  # Ya tiene licencia
            new_content = f'"""\n{license_text}\n"""\n\n' + content
            py_file.write_text(new_content, encoding='utf-8')
            modified.append(str(py_file))
        if modified:
            return f"✅ Licencia {license_type} añadida a {len(modified)} archivos:\n" + "\n".join(modified)
        return "ℹ️ Los archivos ya tenían licencia o no se encontraron archivos .py."
    except Exception as e:
        return f"❌ Error añadiendo licencia: {e}"

def check_dependencies(path: str) -> str:
    """Revisa vulnerabilidades en las dependencias usando safety."""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: La ruta '{path}' no existe."
        # Buscar requirements.txt o pyproject.toml
        req_files = list(target.glob("requirements.txt")) + list(target.glob("pyproject.toml"))
        if not req_files:
            return "⚠️ No se encontró requirements.txt ni pyproject.toml."
        cmd = ['safety', 'check', '-r', str(req_files[0]), '--output', 'json']
        result = _subprocess_run(cmd, timeout=30)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return f"⚠️ Vulnerabilidades encontradas:\n{output}"
        return f"✅ No se encontraron vulnerabilidades conocidas en las dependencias."
    except FileNotFoundError:
        return "❌ safety no está instalado. Ejecuta: pip install safety"
    except Exception as e:
        return f"❌ Error ejecutando safety: {e}"
    
# ============================================================
# HERRAMIENTAS DE SISTEMA Y ENTORNO
# ============================================================

def check_system_tool(tool_name: str) -> str:
    """Verifica si una herramienta del sistema está instalada."""
    try:
        import shutil
        path = shutil.which(tool_name)
        if path:
            return f"✅ {tool_name} está instalado en: {path}"
        else:
            return f"❌ {tool_name} no está instalado en el sistema."
    except Exception as e:
        return f"❌ Error verificando {tool_name}: {e}"

def install_system_tool(tool_name: str) -> str:
    """Sugiere la instalación de una herramienta del sistema (no ejecuta automáticamente)."""
    # Esta función solo da instrucciones, no instala automáticamente por seguridad.
    if os.name == 'nt':
        return f"ℹ️ Para instalar {tool_name} en Windows, descárgalo desde la web oficial o usa un gestor de paquetes como Chocolatey."
    else:
        return f"ℹ️ Para instalar {tool_name} en Linux, ejecuta: sudo apt install {tool_name} (o brew install {tool_name} en macOS)."


# ============================================================
# HERRAMIENTAS DE REVISIÓN Y RESUMEN
# ============================================================

def review_code(path: str) -> str:
    """Genera un informe completo de calidad del código (lint, seguridad, complejidad, pruebas)."""
    results = []
    results.append(lint_code(path))
    results.append(check_security(path))
    results.append(type_check(path))
    results.append(measure_complexity(path))
    # Intentar ejecutar pruebas si existen
    test_result = run_tests(path)
    results.append(test_result)
    return "\n\n".join(results)

def summarize_changes(path: str) -> str:
    """Resume los cambios desde el último checkpoint (requiere integración con auto_dev_agent)."""
    return "⚠️ La función summarize_changes requiere un sistema de checkpoints. Se implementará en el auto_dev_agent."

# ============================================================
# HERRAMIENTA CHAT_COMPLETION (para uso interno)
# ============================================================

def chat_completion(messages: List[Dict]) -> str:
    """
    Envía una lista de mensajes al LLM y devuelve la respuesta (sin herramientas).
    Útil para generar texto, planificar, etc.
    """
    try:
        import requests
        payload = {
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0.0,
            "stream": False
        }
        # Añadir 'model' para compatibilidad con algunos servidores
        payload["model"] = "llama"
        response = requests.post(
            "http://localhost:8081/v1/chat/completions",
            json=payload,
            timeout=300
        )
        if response.status_code != 200:
            return f"Error HTTP {response.status_code}: {response.text}"
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return content
    except Exception as e:
        return f"Error en chat_completion: {e}"
    
# ============================================================
# HERRAMIENTAS DE SKILLS (MEMORIA PROCEDIMENTAL)
# ============================================================

def list_skills(query: str = "", include_auto: bool = False) -> str:
    """Lista los skills disponibles, opcionalmente filtrando por consulta."""
    try:
        workspace = get_workspace()
        sm = get_skill_manager(str(workspace))
        skills = (
            sm.search_skills(query, top_k=10, only_active=not include_auto)
            if query else
            sm.list_skills(include_auto=include_auto)
        )
        if not skills:
            return "📭 No se encontraron skills."
        output = "📚 **Skills disponibles:**\n\n"
        for skill in skills:
            auto = " (auto-generado)" if skill.get('auto_generated', False) else ""
            output += f"- **{skill['name']}**{auto}: {skill['description'][:80]}...\n"
        return output
    except Exception as e:
        return f"❌ Error listando skills: {e}"

def get_skill(skill_name: str) -> str:
    """Obtiene el contenido completo de un skill por su nombre."""
    try:
        workspace = get_workspace()
        sm = get_skill_manager(str(workspace))
        skill = sm.get_skill(skill_name)
        if not skill:
            return f"❌ Skill '{skill_name}' no encontrado."
        return f"📄 **{skill['name']}**\n\n{skill['content']}"
    except Exception as e:
        return f"❌ Error obteniendo skill: {e}"

def create_skill(name: str, description: str, steps_json: str, tags: str = "") -> str:
    """Crea un nuevo skill a partir de una descripción y pasos (formato JSON)."""
    try:
        workspace = get_workspace()
        sm = get_skill_manager(str(workspace))
        steps = json.loads(steps_json)
        if not isinstance(steps, list):
            return "❌ steps_json debe ser una lista de pasos."
        tags_list = [t.strip() for t in tags.split(',') if t.strip()] if tags else []
        path = sm.create_skill(name, description, steps, tags=tags_list, auto_generated=False)
        return f"✅ Skill '{name}' creado en {path}"
    except json.JSONDecodeError:
        return "❌ Error: steps_json no es un JSON válido."
    except Exception as e:
        return f"❌ Error creando skill: {e}"

def promote_skill(skill_name: str) -> str:
    """Promueve un skill auto-generado a skill permanente."""
    try:
        workspace = get_workspace()
        sm = get_skill_manager(str(workspace))
        if sm.promote_skill(skill_name):
            return f"✅ Skill '{skill_name}' promovido a permanente."
        else:
            return f"❌ No se pudo promover '{skill_name}'. ¿Existe en auto-generated?"
    except Exception as e:
        return f"❌ Error promoviendo skill: {e}"

def delete_skill(skill_name: str) -> str:
    """Elimina un skill (solo del workspace)."""
    try:
        workspace = get_workspace()
        sm = get_skill_manager(str(workspace))
        if sm.delete_skill(skill_name, scope="workspace"):
            return f"✅ Skill '{skill_name}' eliminado."
        else:
            return f"❌ No se pudo eliminar '{skill_name}'."
    except Exception as e:
        return f"❌ Error eliminando skill: {e}"


# ============================================================
# HERRAMIENTAS DE CHECKPOINTS (PERSISTENCIA AVANZADA)
# ============================================================

def create_checkpoint(description: str = "", state_json: str = "{}") -> str:
    """
    Crea un nuevo checkpoint del estado actual del proyecto.
    """
    try:
        from .checkpoint_manager import CheckpointManager
        workspace = get_workspace()
        cm = CheckpointManager(str(workspace))
        state = json.loads(state_json) if state_json else {}
        checkpoint_id = cm.create_checkpoint(description, state)
        return f"✅ Checkpoint creado: {checkpoint_id}\n📝 Descripción: {description or '(sin descripción)'}"
    except json.JSONDecodeError:
        return "❌ Error: state_json no es un JSON válido."
    except Exception as e:
        return f"❌ Error creando checkpoint: {e}"

def list_checkpoints(limit: int = 10) -> str:
    """
    Lista los checkpoints disponibles.
    """
    try:
        from .checkpoint_manager import CheckpointManager
        workspace = get_workspace()
        cm = CheckpointManager(str(workspace))
        checkpoints = cm.list_checkpoints(limit)
        if not checkpoints:
            return "📭 No hay checkpoints disponibles."
        output = "📸 **Checkpoints disponibles:**\n\n"
        for i, cp in enumerate(checkpoints, 1):
            output += f"{i}. **{cp['id']}**\n"
            output += f"   📅 {cp['timestamp']}\n"
            output += f"   📝 {cp.get('description', '(sin descripción)')}\n"
            output += f"   📄 {cp.get('files_count', 0)} archivos\n\n"
        return output
    except Exception as e:
        return f"❌ Error listando checkpoints: {e}"

def rollback_checkpoint(checkpoint_id: str, strategy: str = "full") -> str:
    """
    Restaura el estado desde un checkpoint.
    strategy: 'full' (archivos + estado), 'files' (solo archivos), 'state' (solo estado)
    """
    try:
        from .checkpoint_manager import CheckpointManager
        workspace = get_workspace()
        cm = CheckpointManager(str(workspace))
        
        # Verificar que el checkpoint existe
        info = cm.get_checkpoint_info(checkpoint_id)
        if not info:
            return f"❌ Checkpoint '{checkpoint_id}' no encontrado."
        
        # Mostrar diff antes de restaurar (opcional)
        diff = cm.get_file_diff(checkpoint_id)
        if diff:
            diff_msg = "📋 **Diferencias detectadas:**\n"
            for d in diff[:10]:
                diff_msg += f"   - {d['file']}: {d['status']}\n"
            if len(diff) > 10:
                diff_msg += f"   ... y {len(diff) - 10} más.\n"
            # Preguntar confirmación (en modo autónomo, se puede saltar)
        
        result = cm.rollback(checkpoint_id, strategy)
        if result["success"]:
            msg = f"✅ Rollback completado desde checkpoint '{checkpoint_id}'.\n"
            msg += f"   Restaurado: {', '.join(result['restored'])}\n"
            if result.get("state"):
                msg += f"   Estado del agente restaurado: {list(result['state'].keys())}"
            return msg
        else:
            return f"❌ Rollback fallido: {', '.join(result['errors'])}"
    except Exception as e:
        return f"❌ Error en rollback: {e}"

def delete_checkpoint(checkpoint_id: str) -> str:
    """
    Elimina un checkpoint específico.
    """
    try:
        from .checkpoint_manager import CheckpointManager
        workspace = get_workspace()
        cm = CheckpointManager(str(workspace))
        if cm.delete_checkpoint(checkpoint_id):
            return f"✅ Checkpoint '{checkpoint_id}' eliminado."
        else:
            return f"❌ No se pudo eliminar '{checkpoint_id}'. ¿Existe?"
    except Exception as e:
        return f"❌ Error eliminando checkpoint: {e}"

def get_checkpoint_info(checkpoint_id: str) -> str:
    """
    Obtiene información detallada de un checkpoint.
    """
    try:
        from .checkpoint_manager import CheckpointManager
        workspace = get_workspace()
        cm = CheckpointManager(str(workspace))
        info = cm.get_checkpoint_info(checkpoint_id)
        if not info:
            return f"❌ Checkpoint '{checkpoint_id}' no encontrado."
        output = f"📸 **Checkpoint: {info['id']}**\n"
        output += f"   📅 {info['timestamp']}\n"
        output += f"   📝 {info.get('description', '(sin descripción)')}\n"
        output += f"   📄 {info.get('files_count', 0)} archivos\n"
        output += f"   🔑 Estado: {', '.join(info.get('state_keys', []))}\n"
        output += f"   🔒 Hash: {info.get('file_hash', '')[:16]}...\n"
        return output
    except Exception as e:
        return f"❌ Error obteniendo información: {e}"
    
# ============================================================
# HERRAMIENTAS DE EJECUCIÓN SECUENCIAL
# ============================================================

def execute_parallel(tasks_json: str, context_json: str = "{}", max_workers: int = None) -> str:
    """
    Ejecuta tareas en serie por compatibilidad con el nombre histórico.
    tasks_json: JSON con lista de tareas (id, subtask, agent_name, depends_on).
    context_json: JSON con contexto compartido.
    """
    try:
        from .agent_core import Agent, LLMClient
        
        tasks = json.loads(tasks_json)
        if not isinstance(tasks, list):
            return "❌ tasks_json debe ser una lista de tareas."
        
        context = json.loads(context_json) if context_json else {}
        
        def agent_factory(task_context):
            return Agent(
                name=task_context.get("agent_name", "General"),
                instructions="Eres un asistente útil. Completa la tarea asignada.",
                llm_client=LLMClient(),
                max_iterations=10,
                dynamic_tools=True
            )
        
        results = []
        sequential_context = dict(context) if isinstance(context, dict) else {}
        for index, task in enumerate(tasks):
            task_id = task.get("id", f"task_{index}")
            agent_name = task.get("agent_name", "General")
            try:
                agent = agent_factory({"agent_name": agent_name})
                result = agent.run(
                    task.get("subtask", ""),
                    context=json.dumps(sequential_context, ensure_ascii=False),
                )
                results.append({"task_id": task_id, "agent_name": agent_name, "success": True, "result": result})
                sequential_context[agent_name] = result
            except Exception as error:
                results.append({"task_id": task_id, "agent_name": agent_name, "success": False, "error": str(error)})
        
        output = "📊 **Resultados de ejecución secuencial:**\n\n"
        for r in results:
            status = "✅" if r.get("success") else "❌"
            output += f"{status} **{r.get('task_id', 'unknown')}** ({r.get('agent_name', 'General')})\n"
            if r.get("success"):
                output += f"   Resultado: {r.get('result', '')[:200]}...\n"
            else:
                output += f"   Error: {r.get('error', 'desconocido')}\n"
            output += "\n"
        
        return output
    except json.JSONDecodeError:
        return "❌ Error: JSON inválido."
    except Exception as e:
        return f"❌ Error en ejecución paralela: {e}"
    
# ============================================================
# HERRAMIENTAS PARA EL MODO DAEMON
# ============================================================

def start_daemon_tool(objective: str, interval: int = 60, max_cycles: int = -1,
                      enable_notifications: bool = True, enable_telegram: bool = False) -> str:
    """
    Inicia el agente en modo daemon para trabajar de forma autónoma.
    """
    try:
        from .daemon_agent import start_daemon
        workspace = get_workspace()
        return start_daemon(
            objective=objective,
            workspace_path=str(workspace),
            interval=interval,
            max_cycles=max_cycles,
            enable_notifications=enable_notifications,
            enable_telegram=enable_telegram
        )
    except Exception as e:
        return f"❌ Error iniciando daemon: {e}"

def stop_daemon_tool() -> str:
    """Detiene el daemon en ejecución."""
    try:
        from .daemon_agent import stop_daemon
        return stop_daemon()
    except Exception as e:
        return f"❌ Error deteniendo daemon: {e}"

def daemon_status_tool() -> str:
    """Muestra el estado actual del daemon."""
    try:
        from .daemon_agent import daemon_status
        return daemon_status()
    except Exception as e:
        return f"❌ Error obteniendo estado: {e}"
    
# ============================================================
# FUNCIÓN PARA OBTENER TODAS LAS HERRAMIENTAS (compatibilidad)
# ============================================================
def get_all_tool_functions() -> Dict[str, Callable]:
    global settings
    settings = load_settings()
    tools = {}
    if settings.get("enable_core_tools", True):
        tools.update({
            "read_file": read_file,
            "write_file": write_file,
            "edit_file": edit_file,
            "apply_diff": apply_diff,
            "file_glob_search": file_glob_search,
            "grep_search": grep_search,
            "exec_shell_command": exec_shell_command,
            "get_datetime": get_datetime,
        })
    if settings.get("enable_files", True):
        tools.update({
            "create_directory": create_directory,
            "list_directory": list_directory,
            "delete_file": delete_file,
            "move_file": move_file,
            "compress_files": compress_files,
            "extract_archive": extract_archive,
        })
    if settings.get("enable_python", True):
        tools.update({
            "create_venv": create_venv,
            "run_python_code": run_python_code,
        })
    if settings.get("enable_jobs", True):
        tools.update({
            "workspace_download_link": workspace_download_link,
            "run_command_async": run_command_async,
            "job_status": job_status,
            "job_output": job_output,
            "job_kill": job_kill,
            "job_list": job_list,
        })
    if settings.get("enable_db", True):
        tools.update({
            "sqlite_query": sqlite_query,
            "sqlite_execute": sqlite_execute,
            "get_db_schema": get_db_schema,
        })
    if settings.get("enable_processes", True) and PSUTIL_AVAILABLE:
        tools.update({
            "list_processes": list_processes,
            "kill_process": kill_process,
        })
    if settings.get("enable_gui", True) and PYAUTOGUI_AVAILABLE:
        tools.update({
            "screenshot": screenshot,
        })
    if settings.get("enable_camera", True):
        tools["take_photo"] = take_photo
    if settings.get("enable_notifications", True) and PLYER_AVAILABLE:
        tools["notify"] = notify
    if settings.get("enable_webbrowser", True):
        tools["open_url"] = open_url
    if settings.get("enable_todo", True):
        tools.update({
            "add_task": add_task,
            "list_tasks": list_tasks,
            "complete_task": complete_task,
        })
    if settings.get("enable_reminders", True):
        tools["set_reminder"] = set_reminder
    if settings.get("enable_printing", True):
        tools["print_file"] = print_file
    if settings.get("enable_daily_report", True):
        tools["daily_report"] = daily_report
    if settings.get("enable_system_integration", True):
        tools["open_file_with_default_app"] = open_file_with_default_app
    if settings.get("enable_rag", True):
        tools.update({
            "rag_search": rag_search,
            "list_libraries": list_libraries,
            "set_active_library": set_active_library,
        })
    if settings.get("enable_memory", True):
        tools.update({
            "store_memory": store_memory,
            "retrieve_memories": retrieve_memories,
        })
    if settings.get("enable_telegram", True):
        tools.update({
            "send_telegram": send_telegram,
            "setup_telegram": setup_telegram,
        })
    if settings.get("enable_download", True):
        tools["download_file"] = download_file
    if settings.get("enable_git", True):
        tools.update({
            "git_status": git_status,
            "git_commit": git_commit,
            "git_diff": git_diff,
            "git_log": git_log,
        })
    if settings.get("enable_watchdog", True):
        tools.update({
            "start_watchdog": start_watchdog,
            "stop_watchdog": stop_watchdog,
            "get_watchdog_events": get_watchdog_events,
        })
    if settings.get("enable_document_conversion", True):
        tools["convert_document"] = convert_document
    if settings.get("enable_drives", True):
        tools["list_drives"] = list_drives
    if settings.get("enable_classify", True):
        tools["classify_text"] = classify_text
    

    # ========== HERRAMIENTAS DE DESARROLLO ==========
    if settings.get("enable_dev_tools", True):
        tools.update({
            # Análisis y calidad
            "lint_code": lint_code,
            "check_security": check_security,
            "type_check": type_check,
            "measure_complexity": measure_complexity,
            # Pruebas
            "run_tests": run_tests,
            "generate_unit_tests": generate_unit_tests,
            # Documentación
            "generate_docstrings": generate_docstrings,
            "generate_readme": generate_readme,
            "generate_api_docs": generate_api_docs,
            # Formateo y limpieza
            "format_code": format_code,
            "sort_imports": sort_imports,
            "remove_unused_imports": remove_unused_imports,
            "clean_code": clean_code,
            # Seguridad
            "add_license": add_license,
            "check_dependencies": check_dependencies,
            # Sistema
            "check_system_tool": check_system_tool,
            "install_system_tool": install_system_tool,
            # Revisión
            "review_code": review_code,
            "summarize_changes": summarize_changes,
            # Chat completion
            "chat_completion": chat_completion,
        })
    # Añadir delete_directory manualmente
    def delete_directory(path: str) -> str:
        try:
            target = safe_path(path)
            if not target.exists():
                return f"Error: El directorio '{path}' no existe."
            if target.is_file():
                return f"Error: '{path}' es un archivo, no un directorio."
            import shutil
            shutil.rmtree(target)
            return f"Directorio '{path}' eliminado correctamente."
        except Exception as e:
            return f"Error eliminando directorio: {e}"
    tools["delete_directory"] = delete_directory
    
    return tools

# ============================================================
# LÓGICA DE FILTRADO PARA QUERY_TOOLS (desde agent_core)
# ============================================================

TOOL_DESCRIPTIONS = {
    "read_file": "Lee archivos de texto plano (.txt, .py, .json, .csv, .md, .html, etc.). Si el contenido es JSON, lo formatea automaticamente para mayor legibilidad. No soporta archivos Office (.docx, .xlsx, .pptx). Ejemplo: read_file('datos.json')",
    "write_file": "Escribe archivos de texto plano (.txt, .py, .json, .csv, .md, .html, etc.). Parametro mode: 'w' (escribir, por defecto) o 'a' (append). No soporta archivos Office. Ejemplo: write_file('salida.txt', 'contenido')",
    "edit_file": "Edita archivos de texto plano sin perder formato. Operaciones: replace, append, insert_after, insert_before, delete. Si no se especifica operation, detecta automaticamente (old_text+new_text=replace, solo new_text=append). No soporta archivos Office. Ejemplo: edit_file('config.txt', old_text='valor_viejo', new_text='valor_nuevo')",
    "apply_diff": "Aplica un patch unificado (diff) a un archivo. Útil para aplicar cambios generados por otros sistemas. Sinónimos: patch, aplicar cambios.",
    "file_glob_search": "Busca archivos usando patrones glob (*, ?). Útil para encontrar archivos por nombre o extensión. Sinónimos: buscar archivos, find, ls **/*.",
    "grep_search": "Busca texto dentro de archivos usando expresiones regulares. Útil para encontrar código, palabras clave o configuraciones. Sinónimos: buscar texto, grep, findstr.",
    "exec_shell_command": "Ejecuta un comando en la shell del sistema. Poderoso pero restringido a comandos seguros. Sinónimos: ejecutar comando, run, sh, cmd.",
    "get_datetime": "Obtiene la fecha y hora actual del sistema. Útil para timestamps, logs, o saber qué hora es. Sinónimos: fecha, hora, timestamp, now.",
    "create_directory": "Crea un nuevo directorio o carpeta. Útil para organizar archivos o preparar espacio. Sinónimos: mkdir, crear carpeta, new folder.",
    "list_directory": "Lista un directorio y, opcionalmente, sus subdirectorios. Uso: execute_tool(tool_name='list_directory', arguments={'path': '.', 'depth': 2}). depth=0 muestra sólo el nivel indicado; depth=1 incluye hijos; depth=2 incluye nietos.",
    "delete_file": "Mueve un archivo o directorio a la papelera de reciclaje (seguro). Sinónimos: borrar, eliminar, trash, rm (seguro).",
    "move_file": "Mueve o renombra un archivo o directorio. Útil para reorganizar. Sinónimos: mv, renombrar, mover.",
    "compress_files": "Comprime archivos en un zip. Útil para empaquetar, reducir tamaño. Sinónimos: zip, comprimir, empaquetar.",
    "extract_archive": "Extrae archivos de un zip o rar. Útil para descomprimir. Sinónimos: unzip, descomprimir, extraer.",
    "create_venv": "Crea un entorno virtual de Python aislado. Útil para gestionar dependencias. Sinónimos: venv, virtualenv, entorno virtual.",
    "run_python_code": "Ejecuta código Python en un entorno virtual o sistema (síncrono, ~30s). Útil para automatizar, calcular, procesar datos. Sinónimos: python, ejecutar script.",
    "run_command_async": "Lanza un comando o proceso LARGO en segundo plano y devuelve un job_id al instante, sin bloquear la conversación ni abrir consola. Úsalo para pip install, builds, servidores, descargas y scripts largos. Ejemplo: run_command_async('pip install pandas')",
    "job_status": "Estado de un job lanzado con run_command_async: running/exited/error/killed, exit_code y tiempo transcurrido. Ejemplo: job_status('a1b2c3d4')",
    "job_output": "Lee las últimas líneas de la salida de un job. tail=0 devuelve todo. Ejemplo: job_output('a1b2c3d4', tail=200)",
    "job_kill": "Detiene un job en ejecución matando su árbol de procesos. Ejemplo: job_kill('a1b2c3d4')",
    "job_list": "Lista los jobs de la sesión (running/exited/error) con su comando y tiempos.",
    "workspace_download_link": "Devuelve una URL absoluta (válida también en remoto o tras nginx) para descargar o ver un archivo del workspace. Ejemplo: workspace_download_link('informe.pdf')",
    "sqlite_query": "Ejecuta consultas SQL (SELECT) en una base SQLite. Útil para extraer datos. Sinónimos: SQL, consulta, SELECT.",
    "sqlite_execute": "Ejecuta comandos SQL (INSERT, UPDATE, DELETE) en SQLite. Sinónimos: modificar SQL, INSERT, UPDATE.",
    "get_db_schema": "Obtiene el esquema de una base SQLite (tablas y columnas). Útil para conocer la estructura. Sinónimos: schema, estructura, tablas.",
    "list_processes": "Lista los procesos del sistema en ejecución. Sinónimos: ps, tasklist, procesos.",
    "kill_process": "Termina un proceso por su PID. Sinónimos: matar proceso, taskkill.",
    "screenshot": "Captura la pantalla completa y guarda una imagen. Sinónimos: print screen, captura de pantalla.",
    "take_photo": "Toma una foto con la cámara web. Sinónimos: foto, selfie, cámara.",
    "notify": "Envía una notificación del sistema al usuario. Sinónimos: alerta, popup, notificar.",
    "open_url": "Abre una URL en el navegador predeterminado. Sinónimos: navegar, abrir web, visitar.",
    "add_task": "Añade una tarea a la lista TODO. Sinónimos: tarea, pendiente, añadir tarea.",
    "list_tasks": "Lista las tareas pendientes o completadas. Sinónimos: todo, pendientes.",
    "complete_task": "Marca una tarea como completada. Sinónimos: terminar, finalizar.",
    "set_reminder": "Programa un recordatorio para después de N segundos. Sinónimos: recordar, alarma, timer.",
    "print_file": "Envía un archivo a la impresora (Windows o CUPS). Sinónimos: imprimir, imprimir archivo.",
    "open_file_with_default_app": "Abre un archivo con su aplicación predeterminada (ej. .pdf, .docx). Sinónimos: abrir, launch.",
    "rag_search": "Busca información en documentos indexados (RAG) usando búsqueda semántica híbrida. Útil para investigar, recuperar información de documentos. Sinónimos: buscar documento, investigar, buscar en documentos, RAG.",
    "list_libraries": "Lista los documentos indexados en la biblioteca RAG activa. Sinónimos: bibliotecas, documentos indexados.",
    "set_active_library": "Cambia la biblioteca activa de RAG. Sinónimos: cambiar biblioteca, activar.",
    "store_memory": "Guarda un fragmento en la memoria del agente para recordarlo después. Sinónimos: recordar, guardar contexto.",
    "retrieve_memories": "Recupera memorias relevantes del agente. Sinónimos: recordar, memoria.",
    "send_telegram": "Envía un mensaje por Telegram usando el bot configurado. Sinónimos: telegram, mensaje.",
    "setup_telegram": "Configura las credenciales del bot de Telegram. Sinónimos: configurar bot, token.",
    "download_file": "Descarga un archivo desde una URL. Sinónimos: descargar, wget, curl.",
    "git_status": "Muestra el estado del repositorio git (archivos modificados). Sinónimos: git status.",
    "git_commit": "Realiza un commit en git con los cambios añadidos. Sinónimos: git commit.",
    "git_diff": "Muestra las diferencias entre cambios en git. Sinónimos: git diff.",
    "git_log": "Muestra el historial de commits en git. Sinónimos: git log.",
    "start_watchdog": "Inicia el monitor de cambios en archivos (watchdog). Sinónimos: monitorear, vigilar cambios.",
    "stop_watchdog": "Detiene el monitor de cambios. Sinónimos: detener monitor.",
    "get_watchdog_events": "Obtiene los eventos registrados por el monitor. Sinónimos: eventos, cambios.",
    "convert_document": "Convierte documentos entre formatos (usando pandoc o fallback). Sinónimos: convertir, transformar, pandoc.",
    "list_drives": "Lista las unidades de disco disponibles (Windows/Linux). Sinónimos: unidades, discos.",
    "classify_text": "Clasifica un texto en categorías usando el LLM. Sinónimos: clasificar, categorizar.",
    "delegate_to_agent": "Delega una subtarea a otro agente especializado. Útil cuando necesitas ayuda de un experto. Sinónimos: delegar, handoff, pedir ayuda.",
    "delete_directory": "Elimina un directorio y todo su contenido de forma recursiva. Sinónimos: borrar directorio, eliminar carpeta, rmdir, rm -rf.",
    "lint_code": "Analiza el código Python en busca de errores de estilo y problemas comunes usando flake8.",
    "check_security": "Busca vulnerabilidades de seguridad en el código Python usando bandit.",
    "type_check": "Verifica la consistencia de tipos estáticos usando pyright.",
    "measure_complexity": "Mide la complejidad ciclomática del código usando radon.",
    "run_tests": "Ejecuta pruebas unitarias con pytest y muestra los resultados.",
    "generate_unit_tests": "Genera pruebas unitarias para una función o archivo usando IA.",
    "generate_docstrings": "Añade docstrings a funciones y métodos usando IA.",
    "generate_readme": "Genera un archivo README.md para el proyecto usando IA.",
    "generate_api_docs": "Genera documentación API con pdoc o sphinx.",
    "format_code": "Formatea el código Python usando black.",
    "sort_imports": "Ordena y agrupa los imports usando isort.",
    "remove_unused_imports": "Elimina imports no utilizados usando autoflake.",
    "clean_code": "Limpia el código: elimina imports no usados, ordena y formatea.",
    "add_license": "Añade un encabezado de licencia a los archivos .py.",
    "check_dependencies": "Revisa vulnerabilidades en las dependencias del proyecto.",
    "check_system_tool": "Verifica si una herramienta del sistema está instalada.",
    "install_system_tool": "Proporciona instrucciones para instalar una herramienta del sistema.",
    "review_code": "Genera un informe completo de calidad del código.",
    "summarize_changes": "Resume los cambios desde el último checkpoint.",
    "chat_completion": "Envía mensajes al LLM y devuelve la respuesta (sin herramientas).",
    "list_skills": "Lista los skills disponibles (memoria procedimental).",
    "get_skill": "Obtiene el contenido completo de un skill por su nombre.",
    "create_skill": "Crea un nuevo skill con pasos definidos.",
    "promote_skill": "Promueve un skill auto-generado a skill permanente.",
    "delete_skill": "Elimina un skill del workspace.",
    "create_checkpoint": "Crea un checkpoint del estado actual del proyecto.",
    "list_checkpoints": "Lista los checkpoints disponibles.",
    "rollback_checkpoint": "Restaura el proyecto y/o estado del agente desde un checkpoint.",
    "delete_checkpoint": "Elimina un checkpoint específico.",
    "get_checkpoint_info": "Obtiene información detallada de un checkpoint.",
    "execute_parallel": "Ejecuta múltiples tareas en paralelo. Útil cuando hay subtareas independientes que pueden resolverse simultáneamente.",
    "start_daemon_tool": "Inicia el agente en modo daemon para trabajar de forma autónoma en segundo plano.",
    "stop_daemon_tool": "Detiene el daemon en ejecución.",
    "daemon_status_tool": "Muestra el estado actual del daemon.",
}

BASE_TOOLS_CHEATSHEET = (
    "Tools base vía execute_tool(tool_name, arguments): "
    "read_file(path), write_file(path,content), edit_file(path,old_text,new_text), "
    "file_glob_search(pattern,path='.'), grep_search(pattern,path='.'), "
    "exec_shell_command(command), get_datetime(), list_directory(path,depth=0), "
    "run_python_code(code,venv=None), run_command_async(command), job_output(job_id), "
    "job_status(job_id), job_kill(job_id). Úsalas directamente; no necesitas query_tools para estas. "
    "Si dudas, usa query_tools."
)

TOOL_CATEGORIES = {
    "filesystem": ["list_directory", "create_directory", "delete_file", "delete_directory", "move_file", "read_file", "write_file", "edit_file", "apply_diff", "file_glob_search", "grep_search"],
    "system": ["exec_shell_command", "get_datetime", "list_processes", "kill_process", "list_drives"],
    "db": ["sqlite_query", "sqlite_execute", "get_db_schema"],
    "rag": ["rag_search", "list_libraries", "set_active_library"],
    "code": ["run_python_code", "create_venv"],
    "jobs": ["run_command_async", "job_status", "job_output", "job_kill", "job_list"],
    "git": ["git_status", "git_commit", "git_diff", "git_log"],
    "media": ["screenshot", "take_photo", "notify"],
    "telegram": ["send_telegram", "setup_telegram"],
    "todo": ["add_task", "list_tasks", "complete_task"],
    "network": ["open_url", "download_file"],
    "compression": ["compress_files", "extract_archive"],
    "printing": ["print_file"],
    "watchdog": ["start_watchdog", "stop_watchdog", "get_watchdog_events"],
    "conversion": ["convert_document", "classify_text"],
    "code_quality": ["lint_code", "check_security", "type_check", "measure_complexity"],
    "testing": ["run_tests", "generate_unit_tests"],
    "documentation": ["generate_docstrings", "generate_readme", "generate_api_docs"],
    "refactoring": ["format_code", "sort_imports", "remove_unused_imports", "clean_code"],
    "security": ["add_license", "check_dependencies"],
    "system": ["check_system_tool", "install_system_tool"],
    "review": ["review_code", "summarize_changes"],
    "skills": ["list_skills", "get_skill", "create_skill", "promote_skill", "delete_skill"],
    "checkpoints": ["create_checkpoint", "list_checkpoints", "rollback_checkpoint", "delete_checkpoint", "get_checkpoint_info"],
    "parallel": ["execute_parallel"],
    "daemon": ["start_daemon_tool", "stop_daemon_tool", "daemon_status_tool"],
}

CATEGORY_KEYWORDS = {
    "filesystem": ["directorio", "carpeta", "archivo", "file", "listar", "crear", "borrar", "mover", "editar", "buscar archivo", "renombrar", "eliminar", "borrar"],
    "system": ["sistema", "shell", "comando", "proceso", "fecha", "hora", "disco", "unidad", "ejecutar", "correr"],
    "db": ["base de datos", "sqlite", "sql", "tabla", "consulta", "schema", "esquema"],
    "rag": ["rag", "documento", "biblioteca", "indexado", "buscar documento", "listar biblioteca"],
    "code": ["python", "código", "ejecutar código", "entorno virtual", "venv"],
    "jobs": ["job", "segundo plano", "background", "async", "pip install", "instalar dependencia", "proceso largo", "servidor", "build", "compilar", "descarga"],
    "git": ["git", "commit", "pull", "push", "estado", "diff", "historial"],
    "media": ["captura", "pantalla", "screenshot", "foto", "cámara", "notificación"],
    "telegram": ["telegram", "bot", "mensaje", "chat"],
    "todo": ["tarea", "todo", "pendiente", "completar", "lista"],
    "network": ["url", "descargar", "navegador", "web"],
    "compression": ["comprimir", "zip", "descomprimir", "extraer"],
    "printing": ["imprimir"],
    "watchdog": ["monitor", "cambios", "vigilar"],
    "conversion": ["convertir", "formato", "clasificar"],
    "code_quality": ["calidad código", "lint", "estilo", "seguridad", "tipos", "complejidad"],
    "testing": ["pruebas", "tests", "pytest", "unit tests"],
    "documentation": ["documentación", "docstrings", "README", "API docs"],
    "refactoring": ["refactorizar", "formatear", "limpiar código", "ordenar imports"],
    "security": ["licencia", "dependencias", "vulnerabilidades"],
    "system": ["herramienta sistema", "instalar", "verificar"],
    "review": ["revisión", "informe", "resumen cambios"],
    "skills": ["skill", "memoria", "aprender", "lección", "procedimiento"],
    "checkpoints": ["checkpoint", "rollback", "restaurar", "guardar estado", "persistencia", "snapshot"],
    "parallel": ["paralelo", "simultáneo", "multiagente", "ejecutar en paralelo", "varias tareas"],
    "daemon": ["daemon", "fondo", "autónomo", "continuo", "24/7", "servicio"],
}

# Mapeo de herramientas y sus parámetros que esperan un string JSON
TOOLS_JSON_PARAMS = {
    "create_skill": ["steps_json"],
    "create_checkpoint": ["state_json"],
    "execute_parallel": ["tasks_json", "context_json"],
}

_tool_dense_cache = None

def _get_tool_embeddings_cache():
    global _tool_dense_cache
    if _tool_dense_cache is not None:
        return _tool_dense_cache

    dense_cache = {}
    tool_names = list(TOOL_DESCRIPTIONS.keys())

    try:
        resp = requests.post(
            "http://localhost:8082/v1/embeddings",
            json={"input": [f"search_document: {desc}" for desc in TOOL_DESCRIPTIONS.values()]},
            timeout=60
        )
        if resp.status_code == 200:
            data = resp.json()
            for i, name in enumerate(tool_names):
                if i < len(data["data"]):
                    dense_cache[name] = data["data"][i]["embedding"]
    except Exception as e:
        logging.warning(f"Error generando dense embeddings: {e}")

    _tool_dense_cache = dense_cache
    logging.info(f"Embeddings generados para {len(dense_cache)} herramientas (dense).")
    return dense_cache

def _cosine_similarity(a, b):
    a_norm = np.linalg.norm(a)
    b_norm = np.linalg.norm(b)
    if a_norm == 0 or b_norm == 0:
        return 0.0
    return np.dot(a, b) / (a_norm * b_norm)

def get_relevant_tools(query: str, top_k: int = 3) -> List[str]:
    dense_cache = _get_tool_embeddings_cache()
    if not dense_cache:
        return ["list_directory", "read_file", "get_datetime"]

    q_dense = None
    try:
        resp = requests.post(
            "http://localhost:8082/v1/embeddings",
            json={"input": [f"search_query: {query}"]},
            timeout=3
        )
        if resp.status_code == 200:
            q_dense = resp.json()["data"][0]["embedding"]
    except:
        pass

    if q_dense is None:
        return ["list_directory", "read_file", "get_datetime"]

    category_boost = {}
    query_lower = query.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in query_lower:
                for tool in TOOL_CATEGORIES.get(cat, []):
                    category_boost[tool] = category_boost.get(tool, 0) + 1.0
                break

    scores = {}
    for name, desc in TOOL_DESCRIPTIONS.items():
        score = 0.0
        if q_dense and name in dense_cache:
            score += _cosine_similarity(q_dense, dense_cache[name]) * 1.0
        if name in category_boost:
            score += category_boost[name] * 0.3
        scores[name] = score

    sorted_tools = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_tools = [name for name, _ in sorted_tools[:top_k]]

    # Siempre incluir las básicas aunque el embedding no las haya encontrado
    # (antes el tope top_k impedía añadirlas: read_file faltaba en el contexto
    # del modelo y este caía en query(mode=read) para intentar leer archivos).
    for tool in ("list_directory", "read_file", "write_file", "edit_file", "get_datetime", "exec_shell_command"):
        if tool not in top_tools:
            top_tools.append(tool)

    logging.info(f"Herramientas seleccionadas para '{query[:30]}...': {top_tools}")
    return top_tools


# ============================================================
# FUNCIONES GLOBALES PARA QUERY Y EXECUTE (usadas por MCP y por agent_core)
# ============================================================

_all_tools_cache = None

def _get_all_tools():
    """Cache para get_all_tool_functions() para no recalcular cada vez."""
    global _all_tools_cache
    if _all_tools_cache is None:
        _all_tools_cache = get_all_tool_functions()
    return _all_tools_cache

_query_tools_cache = {}
_QUERY_TOOLS_TTL = 30.0


def query_tools_global(query: str) -> str:
    """
    Busca y devuelve las herramientas más relevantes para una consulta.
    Esta es la versión global que puede ser importada directamente.
    Con caché TTL: inspeccionar ~90 tools en cada llamada del router es caro.
    """
    key = (query or "").strip()
    now = time.time()
    cached = _query_tools_cache.get(key)
    if cached and (now - cached[0]) < _QUERY_TOOLS_TTL:
        return cached[1]

    relevant = get_relevant_tools(query, top_k=3)
    if not relevant:
        return "No se encontraron herramientas relevantes."

    all_tools = _get_all_tools()
    output = "🔧 **Herramientas relevantes:**\n\n"
    
    for name in relevant:
        func = all_tools.get(name)
        if not func:
            continue

        # ----- DESCRIPCIÓN: desde TOOL_DESCRIPTIONS o docstring completo -----
        desc = TOOL_DESCRIPTIONS.get(name)
        if not desc:
            desc = inspect.getdoc(func) or "Sin descripción."
        # No truncamos: mostramos toda la descripción

        # ----- ARGUMENTOS: desde TOOL_SCHEMAS -----
        schema = TOOL_SCHEMAS.get(name)
        if schema and 'properties' in schema:
            props = schema['properties']
            required = schema.get('required', [])
            params = []
            for p_name, p_info in props.items():
                p_type = p_info.get('type', 'any')
                default = p_info.get('default')
                is_required = p_name in required
                default_str = f" (default: {default})" if default is not None else ""
                params.append(f"    - {p_name} ({p_type}){' [REQUIRED]' if is_required else ''}{default_str}")
            if schema.get('additionalProperties', False):
                params.append("    - [argumentos adicionales según formato]")
        else:
            # Fallback a inspect.signature (para herramientas sin esquema)
            sig = inspect.signature(func)
            params = []
            for p_name, p_param in sig.parameters.items():
                if p_name in ("self", "cls"):
                    continue
                p_type = p_param.annotation
                if p_type == inspect.Parameter.empty:
                    p_type = "any"
                elif hasattr(p_type, "__name__"):
                    p_type = p_type.__name__
                else:
                    p_type = str(p_type)
                if p_param.default == inspect.Parameter.empty:
                    required = True
                    default_str = ""
                else:
                    required = False
                    default_str = f" (default: {p_param.default})"
                params.append(f"    - {p_name} ({p_type}){' [REQUIRED]' if required else ''}{default_str}")

        params_str = "\n".join(params) if params else "    (sin argumentos)"
        output += f"**{name}**: {desc}\n"
        output += f"Argumentos:\n{params_str}\n\n"
    
    output += "📌 Usa `execute_tool` con `tool_name` y `arguments` para ejecutarlas."

    # Guardar en caché y limitar su tamaño
    _query_tools_cache[key] = (now, output)
    if len(_query_tools_cache) > 200:
        _query_tools_cache.clear()
    return output

def execute_tool_global(tool_name: Optional[str] = None, arguments: dict = None) -> str:
    """
    Ejecuta una herramienta específica con los argumentos proporcionados.
    Esta es la versión global que puede ser importada directamente.
    """
    if tool_name is None and isinstance(arguments, dict):
        tool_name = arguments.pop('tool_name', None)

    if isinstance(arguments, dict) and 'arguments' in arguments and isinstance(arguments['arguments'], dict):
        arguments = arguments['arguments']
    arguments = arguments or {}

    all_tools = _get_all_tools()
    func = all_tools.get(tool_name)
    if func is None:
        # Fallback a TOOL_IMPL: atajos de conveniencia (view/edit/search/bash/
        # tree/read_file/write_file/list_directory/grep_search) que no están en
        # get_all_tool_functions.
        func = TOOL_IMPL.get(tool_name)
    if not func:
        return f"Error: Herramienta '{tool_name}' no encontrada."
     
    try:
        result = func(**arguments)
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        return result
    except Exception as e:
        return f"Error ejecutando {tool_name}: {e}"
    
# ============================================================
# SERVIDOR MCP CON FASTMCP (SOLO QUERY_TOOLS Y EXECUTE_TOOL)
# ============================================================
_mcp_instance = None
_agency_jobs = {}
_agency_jobs_lock = threading.Lock()


def _parse_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"false", "0", "no", "off"}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True
    return default if value is None else bool(value)

def create_mcp_server(transport: str = "http"):
    """Crea el servidor MCP.

    - transport == 'http': implementación completa (WebUI, agent_core, tests).
    - transport == 'stdio': proxy ligero a la instancia HTTP en :15000
      (llama-server solo soporta stdio para su proxy MCP).
    """
    if transport == "stdio":
        return _create_stdio_proxy_server()
    global _mcp_instance, settings
    settings = load_settings()

    if not FAST_MCP_AVAILABLE:
        return None

    mcp = FastMCP("agente-mcp")

    from .agency_catalog import create_default_catalog

    agency_catalog = create_default_catalog()

    if hasattr(mcp, "custom_route"):
        try:
            from starlette.responses import JSONResponse, Response, StreamingResponse

            def cors_headers():
                return api_cors_headers()

            def tool_json_response(value):
                try:
                    payload = json.loads(value) if isinstance(value, str) else value
                except Exception:
                    payload = {"result": value}
                return JSONResponse(payload, headers=cors_headers())

            @mcp.custom_route("/{path:path}", methods=["OPTIONS"])
            async def fastmcp_cors_preflight(request):
                return Response(status_code=204, headers=cors_headers())

            @mcp.custom_route("/agent_execute", methods=["POST"])
            async def fastmcp_agent_execute(request):
                payload = await request.json()
                task = payload.get("task")
                selection_mode = payload.get("selection_mode", "manual")
                parallel = _parse_bool(payload.get("parallel", True))
                agents = payload.get("agents", [])
                conversation_id = payload.get("conversation_id")
                daemon = _parse_bool(payload.get("daemon", False), default=False)
                logging.info(
                    "[AgentMode] request selection_mode=%s parallel=%s agents=%s task=%s",
                    selection_mode,
                    parallel,
                    agents,
                    str(task)[:120],
                )
                if not task:
                    return JSONResponse({"error": "'task' required"}, status_code=400, headers=cors_headers())
                with _agency_jobs_lock:
                    existing = next(
                        (
                            job for job in _agency_jobs.values()
                            if job.get("status") == "running"
                            and conversation_id
                            and job.get("conversation_id") == conversation_id
                        ),
                        None,
                    )
                if existing:
                    return JSONResponse(
                        {"job_id": existing["id"], "status": "running", "reused": True},
                        status_code=202,
                        headers=cors_headers(),
                    )
                job_id = str(uuid.uuid4())
                job = {
                    "id": job_id,
                    "status": "running",
                    "events": [],
                    "result": None,
                    "task": task,
                    "agents": agents,
                    "parallel": parallel,
                    "selection_mode": selection_mode,
                    "conversation_id": conversation_id,
                    "daemon": daemon,
                }
                cancel_event = threading.Event()
                job["cancel_event"] = cancel_event
                with _agency_jobs_lock:
                    _agency_jobs[job_id] = job

                def worker():
                    try:
                        from .agent_core import EventBus
                        from .mcp_server import _run_selected_agents, get_orchestrator

                        bus = EventBus()
                        orchestrator = get_orchestrator()
                        job["orchestrator"] = orchestrator
                        # El singleton conserva el `_cancelled` del job anterior;
                        # si ese job se canceló, el nuevo abortaba en su primera
                        # iteración ("Cancelado por usuario" al instante).
                        try:
                            orchestrator.reset_cancel()
                        except Exception:
                            pass
                        bus.on(lambda event: job["events"].append({
                            "type": event.type,
                            "payload": event.payload,
                            "meta": {
                                **(event.meta or {}),
                                "job_id": job_id,
                                "conversation_id": conversation_id,
                                "daemon": daemon,
                            },
                        }))
                        if agents or selection_mode in ("automatic", "hybrid"):
                            result = _run_selected_agents(
                                task,
                                agents,
                                payload.get("context"),
                                bus,
                                cancel_event,
                                parallel=parallel,
                                selection_mode=selection_mode,
                            )
                        else:
                            logging.info("[AgentMode] manual without agents; using native orchestrator")
                            result = orchestrator.run_task_stream(task, bus=bus, reasoning=True)
                        job.update({"result": result, "status": "cancelled" if cancel_event.is_set() else "done"})
                    except Exception as error:
                        job.update({"error": str(error), "status": "error"})
                    finally:
                        job["events"].append({"type": "done", "payload": {"result": job.get("result"), "status": job.get("status")}})

                threading.Thread(target=worker, daemon=True).start()
                return JSONResponse({"job_id": job_id, "status": "running"}, status_code=202, headers=cors_headers())

            @mcp.custom_route("/agent_events/{job_id}", methods=["GET"])
            async def fastmcp_agent_events(request):
                job_id = request.path_params["job_id"]

                def _requested_from() -> int:
                    """Índice de cola desde el que el cliente quiere reanudar.

                    El EventSource del navegador no puede mandar cabeceras, así
                    que los reintentos pasan `?from=N` (se acepta también
                    `Last-Event-ID` por compatibilidad con clientes SSE). Sin
                    reanudación, un corte durante un tool largo reproducía la
                    cola entera y el chat re-aplicaba lo ya renderizado.
                    """
                    raw = (
                        request.query_params.get("from")
                        or request.headers.get("Last-Event-ID")
                        or ""
                    )
                    try:
                        return max(0, int(str(raw).strip()))
                    except (TypeError, ValueError):
                        return 0

                async def stream():
                    start = _requested_from()
                    with _agency_jobs_lock:
                        known = job_id in _agency_jobs
                    if not known:
                        yield 'event: error\ndata: {"error":"job not found"}\n\n'
                        return

                    # Primer frame de CADA conexión: el índice real desde el que
                    # empieza la cola. El chat lo toma como base y descarta lo ya
                    # aplicado; sin él, reconectar volvía a aplicar eventos y el
                    # flujo agencial salía duplicado o vacío.
                    yield (
                        "event: sse_start\n"
                        + "data: "
                        + json.dumps(
                            {"type": "sse_start", "payload": {"from": start}},
                            ensure_ascii=True,
                        )
                        + "\n\n"
                    )

                    index = start
                    idle_since = time.monotonic()
                    while True:
                        with _agency_jobs_lock:
                            job = _agency_jobs.get(job_id)
                        if not job:
                            yield 'event: error\ndata: {"error":"job not found"}\n\n'
                            return
                        events = job["events"]
                        while index < len(events):
                            event = events[index]
                            queue_index = index
                            index += 1
                            # El índice de cola viaja incrustado en el frame: el
                            # chat solo escucha unos tipos de evento y su recuento
                            # local se desincroniza de la cola si no lo lleva.
                            framed = (
                                _sse_frame({**event, "index": queue_index})
                                if isinstance(event, dict)
                                else _sse_frame(event)
                            )
                            # Un hueco sin frame propio (serialización fallida)
                            # descuadraría los índices del cliente: se manda un
                            # sustituto que ocupa su hueco exacto.
                            yield framed or (
                                "event: message\n"
                                + "data: "
                                + json.dumps(
                                    {"type": "message", "payload": None, "index": queue_index},
                                    ensure_ascii=True,
                                )
                                + "\n\n"
                            )
                            idle_since = time.monotonic()
                            # Solo el `done` que el worker appendea en su
                            # `finally` (sin meta) cierra el stream: los `done`
                            # del bus —especialista, síntesis del supervisor,
                            # paso del plan— viajan con meta y mataban el SSE
                            # antes de la síntesis final.
                            if _is_stream_terminal(event):
                                return
                        # Keepalive: durante un tool largo no circulan bytes y los
                        # proxies cortan la conexión ociosa («SSE connection
                        # lost»). El comentario no dispara ningún evento en el
                        # cliente; solo mantiene viva la línea.
                        if time.monotonic() - idle_since >= 10.0:
                            idle_since = time.monotonic()
                            yield ": keepalive\n\n"
                        await asyncio.sleep(0.1)

                return StreamingResponse(
                    stream(),
                    media_type="text/event-stream",
                    headers={
                        **cors_headers(),
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )

            @mcp.custom_route("/agent_jobs", methods=["GET"])
            async def fastmcp_agent_jobs(request):
                conversation_id = request.query_params.get("conversation_id")
                daemon_only = request.query_params.get("daemon", "").casefold() == "true"
                with _agency_jobs_lock:
                    jobs = [
                        {
                            "id": job["id"],
                            "status": job["status"],
                            "conversation_id": job.get("conversation_id"),
                            "daemon": job.get("daemon", False),
                            "parallel": job.get("parallel"),
                            "selection_mode": job.get("selection_mode"),
                            "task": job.get("task", ""),
                        }
                        for job in _agency_jobs.values()
                        if (not conversation_id or job.get("conversation_id") == conversation_id)
                        and (not daemon_only or job.get("daemon", False))
                    ]
                return JSONResponse({"jobs": jobs}, headers=cors_headers())

            @mcp.custom_route("/agent_execute/{job_id}", methods=["DELETE"])
            async def fastmcp_agent_cancel(request):
                job_id = request.path_params["job_id"]
                with _agency_jobs_lock:
                    job = _agency_jobs.get(job_id)
                if not job:
                    return JSONResponse({"error": "not found"}, status_code=404, headers=cors_headers())
                job["cancel_event"].set()
                job["cancel_requested"] = True
                try:
                    job.get("orchestrator").stop()
                except Exception:
                    pass
                job["status"] = "cancelled"
                return JSONResponse({"job_id": job_id, "status": "cancelled"}, headers=cors_headers())

            @mcp.custom_route("/agent_status", methods=["GET", "OPTIONS"])
            async def fastmcp_agent_status(request):
                status = json.loads(agent_status())
                with _agency_jobs_lock:
                    active_job = next(
                        (
                            {
                                "id": job["id"],
                                "status": job["status"],
                                "task": job.get("task", ""),
                                "agents": job.get("agents", []),
                                "parallel": job.get("parallel", True),
                            }
                            for job in _agency_jobs.values()
                            if job.get("status") == "running"
                        ),
                        None,
                    )
                orchestrator = status.get("orchestrator") or {"agents": status.get("skills", [])}
                return JSONResponse({
                    "daemon": {
                        "is_running": status.get("mode") == "DAEMON",
                        "is_paused": status.get("mode") == "PAUSED",
                        "objective": status.get("task"),
                    },
                    "jobs": {"running": sum(job.get("status") == "running" for job in _agency_jobs.values())},
                    "active_job": active_job,
                    "orchestrator": {
                        "workspace_path": orchestrator.get("workspace_path"),
                        "agents": orchestrator.get("agents") or status.get("skills", []),
                        "agents_count": orchestrator.get("agents_count", len(orchestrator.get("agents") or status.get("skills", []))),
                    },
                }, headers=cors_headers())

            @mcp.custom_route("/agent_memory", methods=["GET", "OPTIONS"])
            async def fastmcp_agent_memory(request):
                return JSONResponse({"episodes": json.loads(agent_memory(int(request.query_params.get("limit", 5))))}, headers=cors_headers())

            @mcp.custom_route("/agent_skills", methods=["GET", "OPTIONS"])
            async def fastmcp_agent_skills(request):
                return JSONResponse({"skills": json.loads(agent_skills(request.query_params.get("query", "")))}, headers=cors_headers())

            @mcp.custom_route("/agent_plan", methods=["GET", "OPTIONS"])
            async def fastmcp_agent_plan(request):
                return JSONResponse({"active_plan": json.loads(agent_plan())}, headers=cors_headers())

            @mcp.custom_route("/agent_sensors", methods=["GET", "OPTIONS"])
            async def fastmcp_agent_sensors(request):
                try:
                    memory = psutil.virtual_memory()
                    disk = psutil.disk_usage(str(get_workspace()))
                    gpu = []
                    try:
                        import pynvml
                        pynvml.nvmlInit()
                        for index in range(pynvml.nvmlDeviceGetCount()):
                            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                            name = pynvml.nvmlDeviceGetName(handle)
                            gpu.append({
                                "index": index,
                                "name": name.decode(errors="replace") if isinstance(name, bytes) else str(name),
                                "gpu_utilization": utilization.gpu,
                                "memory_utilization": utilization.memory,
                                "memory_total_mb": round(info.total / (1024 ** 2), 1),
                                "memory_used_mb": round(info.used / (1024 ** 2), 1),
                            })
                        pynvml.nvmlShutdown()
                    except Exception:
                        pass
                    sensors = {
                        "cpu_percent": psutil.cpu_percent(interval=0.1),
                        "memory": {
                            "percent": memory.percent,
                            "total_gb": round(memory.total / (1024 ** 3), 2),
                            "used_gb": round(memory.used / (1024 ** 3), 2),
                            "available_gb": round(memory.available / (1024 ** 3), 2),
                        },
                        "disk": {
                            "percent": disk.percent,
                            "total_gb": round(disk.total / (1024 ** 3), 2),
                            "used_gb": round(disk.used / (1024 ** 3), 2),
                            "free_gb": round(disk.free / (1024 ** 3), 2),
                        },
                        "gpu": gpu,
                        "temperatures": {},
                        "timestamp": time.time(),
                    }
                    try:
                        temperatures = psutil.sensors_temperatures()
                        sensors["temperatures"] = {
                            name: [
                                {"label": item.label, "current": item.current}
                                for item in entries
                            ]
                            for name, entries in temperatures.items()
                        }
                    except Exception:
                        pass
                except Exception as error:
                    sensors = {"error": str(error), "timestamp": time.time()}
                return JSONResponse(sensors, headers=cors_headers())

            @mcp.custom_route("/agent_checkpoints", methods=["GET", "OPTIONS"])
            async def fastmcp_agent_checkpoints(request):
                try:
                    from .checkpoint_manager import CheckpointManager
                    checkpoints = CheckpointManager(str(get_workspace())).list_checkpoints(
                        int(request.query_params.get("limit", 20))
                    )
                except Exception:
                    checkpoints = []
                return JSONResponse({"checkpoints": checkpoints}, headers=cors_headers())

            @mcp.custom_route("/daemon_tasks", methods=["GET", "OPTIONS"])
            async def fastmcp_daemon_tasks(request):
                state_path = get_workspace() / ".daemon" / "daemon_state.json"
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                except Exception:
                    state = {}
                return JSONResponse({
                    "completed": state.get("completed_tasks", []),
                    "failed": state.get("failed_tasks", []),
                    "lessons": state.get("lessons", []),
                    "tasks": state.get("scheduled_tasks", []),
                    "scheduled_tasks": state.get("scheduled_tasks", []),
                    "plan": state.get("plan", []),
                }, headers=cors_headers())

            @mcp.custom_route("/daemon_start", methods=["POST", "OPTIONS"])
            async def fastmcp_daemon_start(request):
                payload = await request.json()
                return tool_json_response(start_daemon_tool(str(payload.get("objective", ""))))

            @mcp.custom_route("/daemon_stop", methods=["POST", "OPTIONS"])
            async def fastmcp_daemon_stop(request):
                return tool_json_response(stop_daemon_tool())

            @mcp.custom_route("/agent_goal", methods=["POST", "OPTIONS"])
            async def fastmcp_agent_goal(request):
                payload = await request.json()
                return tool_json_response(agent_goal(str(payload.get("objective", ""))))

            @mcp.custom_route("/daemon_pause", methods=["POST", "OPTIONS"])
            async def fastmcp_daemon_pause(request):
                return tool_json_response(agent_pause())

            @mcp.custom_route("/daemon_resume", methods=["POST", "OPTIONS"])
            async def fastmcp_daemon_resume(request):
                return tool_json_response(agent_resume())

            @mcp.custom_route("/agent_rollback", methods=["POST", "OPTIONS"])
            async def fastmcp_agent_rollback(request):
                payload = await request.json()
                checkpoint_id = payload.get("checkpoint_id") if payload else None
                try:
                    from .checkpoint_manager import CheckpointManager
                    result = CheckpointManager(str(get_workspace())).restore_checkpoint(checkpoint_id)
                    return JSONResponse({"status": "restored", "checkpoint_id": checkpoint_id, "result": result}, headers=cors_headers())
                except Exception as e:
                    return JSONResponse({"error": str(e)}, status_code=500, headers=cors_headers())

            @mcp.custom_route("/daemon_status", methods=["GET", "OPTIONS"])
            async def fastmcp_daemon_status(request):
                state_path = get_workspace() / ".daemon" / "daemon_state.json"
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
                except Exception:
                    state = {}
                return JSONResponse({
                    "running": bool(state.get("running", False)),
                    "paused": bool(state.get("paused", False)),
                    "objective": state.get("objective", ""),
                    "cycle_count": state.get("cycle_count", 0),
                    "last_run": state.get("last_run"),
                }, headers=cors_headers())
        except ImportError:
            logging.warning("Starlette no disponible; endpoints HTTP de agente no registrados")

    if hasattr(mcp, "custom_route"):
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        # ---------- Agentes del catálogo (listar / ver / crear / borrar) ----------
        @mcp.custom_route("/api/agents", methods=["GET", "POST", "OPTIONS"])
        async def agency_agents_route(request: Request):
            """GET: lista o busca agentes. POST: crea un agente nuevo (.md)."""
            _remember_request(request)
            if request.method == "OPTIONS":
                return JSONResponse({}, headers=cors_headers())
            if request.method == "POST":
                try:
                    data = await request.json()
                except Exception:
                    data = {}
                name = str(data.get("name", "")).strip()
                if not name:
                    return JSONResponse({"success": False, "error": "'name' es obligatorio"},
                                        status_code=400, headers=cors_headers())
                # Importar/guardar markdown crudo (gestor de agentes).
                raw = data.get("content")
                if isinstance(raw, str) and raw.strip():
                    try:
                        saved = agency_catalog.save_raw(name, raw)
                        return JSONResponse({"success": True, "agent": saved}, headers=cors_headers())
                    except Exception as e:
                        return JSONResponse({"success": False, "error": str(e)},
                                            status_code=400, headers=cors_headers())
                try:
                    created = agency_catalog.create(
                        name=name,
                        description=str(data.get("description", "")),
                        category=str(data.get("category") or "custom"),
                        body=str(data.get("instructions") or data.get("body") or ""),
                        display_name_es=str(data.get("display_name_es", "")),
                        emoji=str(data.get("emoji", "")),
                        color=str(data.get("color", "")),
                        vibe=str(data.get("vibe", "")),
                        aliases=data.get("aliases") or [],
                        overwrite=bool(data.get("overwrite", False)),
                    )
                    return JSONResponse({"success": True, "agent": created}, headers=cors_headers())
                except FileExistsError as e:
                    return JSONResponse({"success": False, "error": str(e)},
                                        status_code=409, headers=cors_headers())
                except Exception as e:
                    return JSONResponse({"success": False, "error": str(e)},
                                        status_code=400, headers=cors_headers())
            return JSONResponse(
                {"success": True,
                 "agents": agency_catalog.search(request.query_params.get("query", ""))},
                headers=cors_headers(),
            )

        @mcp.custom_route("/api/agents/{name}", methods=["GET", "PUT", "DELETE", "OPTIONS"])
        async def api_agent_detail(request: Request):
            """GET: agente + markdown completo. PUT: guarda el markdown crudo. DELETE: elimina."""
            _remember_request(request)
            if request.method == "OPTIONS":
                return JSONResponse({}, headers=cors_headers())
            name = request.path_params.get("name", "")
            if request.method == "DELETE":
                deleted = agency_catalog.delete(name)
                return JSONResponse({"success": deleted, "deleted": name},
                                    status_code=200 if deleted else 404,
                                    headers=cors_headers())
            if request.method == "PUT":
                try:
                    data = await request.json()
                except Exception:
                    data = {}
                raw = data.get("content") if isinstance(data, dict) else None
                if not isinstance(raw, str) or not raw.strip():
                    return JSONResponse({"success": False, "error": "'content' es obligatorio"},
                                        status_code=400, headers=cors_headers())
                try:
                    saved = agency_catalog.save_raw(name, raw)
                    return JSONResponse({"success": True, "agent": saved}, headers=cors_headers())
                except Exception as e:
                    return JSONResponse({"success": False, "error": str(e)},
                                        status_code=400, headers=cors_headers())
            agent = agency_catalog.full(name)
            if not agent:
                return JSONResponse({"success": False, "error": f"Agente '{name}' no encontrado"},
                                    status_code=404, headers=cors_headers())
            return JSONResponse({"success": True, "agent": agent}, headers=cors_headers())

        # ---------- Cola de jobs (consola: visor de salida + control) ----------
        @mcp.custom_route("/api/jobs", methods=["GET", "OPTIONS"])
        async def api_jobs_list(request: Request):
            """Lista los jobs de la sesión (?all=0 para solo los activos)."""
            _remember_request(request)
            try:
                include_finished = request.query_params.get("all", "1") not in ("0", "false", "False")
                return JSONResponse(
                    {"success": True, "jobs": job_manager.list_jobs(include_finished)},
                    headers=cors_headers(),
                )
            except Exception as e:
                return JSONResponse({"success": False, "error": str(e)},
                                    status_code=500, headers=cors_headers())

        @mcp.custom_route("/api/jobs/start", methods=["POST", "OPTIONS"])
        async def api_jobs_start(request: Request):
            """Arranca un job: body {command, shell, cwd, venv, label}."""
            _remember_request(request)
            try:
                data = await request.json()
            except Exception:
                data = {}
            command = str(data.get("command", "")).strip()
            if not command:
                return JSONResponse({"success": False, "error": "'command' es obligatorio"},
                                    status_code=400, headers=cors_headers())
            if not is_command_allowed(command):
                return JSONResponse({"success": False, "error": "Comando en la lista negra"},
                                    status_code=403, headers=cors_headers())
            command = _resolve_python_in_command(command)
            job_env = None
            venv = data.get("venv")
            if venv:
                job_env = os.environ.copy()
                err = _apply_venv_env(job_env, str(venv))
                if err:
                    return JSONResponse({"success": False, "error": err},
                                        status_code=400, headers=cors_headers())
            try:
                res = job_manager.start_job(
                    command,
                    shell=str(data.get("shell", "auto")),
                    cwd=data.get("cwd") or None,
                    env=job_env,
                    label=data.get("label"),
                )
            except Exception as e:
                return JSONResponse({"success": False, "error": str(e)},
                                    status_code=500, headers=cors_headers())
            return JSONResponse({"success": res.get("status") != "error", **res},
                                headers=cors_headers())

        @mcp.custom_route("/api/jobs/clear", methods=["POST", "OPTIONS"])
        async def api_jobs_clear(request: Request):
            """Elimina del listado (y de disco) los jobs ya terminados."""
            if request.method == "OPTIONS":
                return JSONResponse({}, headers=cors_headers())
            include_failed = True
            try:
                payload = await request.json()
                if isinstance(payload, dict) and "include_failed" in payload:
                    include_failed = bool(payload.get("include_failed"))
            except Exception:
                pass
            res = job_manager.clear_finished_jobs(include_failed=include_failed)
            return JSONResponse({"success": True, **res}, headers=cors_headers())

        @mcp.custom_route("/api/jobs/{job_id}/status", methods=["GET", "OPTIONS"])
        async def api_jobs_status(request: Request):
            job_id = request.path_params.get("job_id", "")
            return JSONResponse(job_manager.job_status(job_id), headers=cors_headers())

        @mcp.custom_route("/api/jobs/{job_id}/output", methods=["GET", "OPTIONS"])
        async def api_jobs_output(request: Request):
            try:
                tail = int(request.query_params.get("tail", 200))
            except Exception:
                tail = 200
            job_id = request.path_params.get("job_id", "")
            return JSONResponse(job_manager.job_output(job_id, tail=tail), headers=cors_headers())

        @mcp.custom_route("/api/jobs/{job_id}/kill", methods=["POST", "OPTIONS"])
        async def api_jobs_kill(request: Request):
            job_id = request.path_params.get("job_id", "")
            return JSONResponse(job_manager.job_kill(job_id), headers=cors_headers())

        @mcp.custom_route("/api/jobs/{job_id}/stream", methods=["GET"])
        async def api_jobs_stream(request: Request):
            """SSE con las líneas nuevas del log (visor de salida en vivo)."""
            from starlette.responses import StreamingResponse
            job_id = request.path_params.get("job_id", "")

            async def _events():
                sent = 0
                deadline = time.time() + 3600
                while time.time() < deadline:
                    try:
                        if await request.is_disconnected():
                            break
                    except Exception:
                        pass
                    out = job_manager.job_output(job_id, tail=0)
                    lines = out.get("lines") or []
                    for line in lines[sent:]:
                        yield "data: " + json.dumps({"line": line}, ensure_ascii=False) + "\n\n"
                    sent = len(lines)
                    if out.get("status") in ("exited", "error", "killed"):
                        payload = json.dumps({"status": out.get("status"),
                                              "exit_code": out.get("exit_code")}, ensure_ascii=False)
                        yield "event: end\ndata: " + payload + "\n\n"
                        break
                    await asyncio.sleep(0.5)

            return StreamingResponse(
                _events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **cors_headers()},
            )
        
        # Endpoints adicionales para gestión de Agentes y Skills
        @mcp.custom_route("/api/skills", methods=["GET", "POST", "OPTIONS"])
        async def api_skills_list(request: Request):
            """GET: lista skills. POST: crea un skill nuevo."""
            _remember_request(request)
            if request.method == "OPTIONS":
                return JSONResponse({}, headers=cors_headers())
            if request.method == "POST":
                try:
                    data = await request.json()
                except Exception:
                    data = {}
                name = str(data.get("name", "")).strip()
                description = str(data.get("description", "")).strip()
                steps = data.get("steps") or []
                if not name:
                    return JSONResponse({"success": False, "error": "'name' es obligatorio"},
                                        status_code=400, headers=cors_headers())
                if isinstance(steps, str):
                    try:
                        steps = json.loads(steps)
                    except Exception:
                        steps = [{"action": steps}]
                if not isinstance(steps, list) or not steps:
                    steps = [{"action": description or f"Ejecutar {name}"}]
                tags = data.get("tags") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                try:
                    sm = get_skill_manager(str(get_workspace()))
                    path = sm.create_skill(
                        name, description, steps, tags=tags,
                        auto_generated=bool(data.get("auto_generated", False)),
                    )
                    return JSONResponse({"success": True, "name": name, "path": str(path)},
                                        status_code=201, headers=cors_headers())
                except Exception as e:
                    return JSONResponse({"success": False, "error": str(e)},
                                        status_code=400, headers=cors_headers())
            try:
                skill_mgr = get_skill_manager(str(get_workspace()))
                skills = skill_mgr.list_skills(include_auto=True)
                return JSONResponse(
                    {
                        "success": True,
                        "skills": skills,
                        "total": len(skills),
                        "active": list(skill_mgr._active_skills)
                    },
                    headers=cors_headers()
                )
            except Exception as e:
                return JSONResponse(
                    {"success": False, "error": str(e)},
                    status_code=500,
                    headers=cors_headers()
                )
        
        @mcp.custom_route("/api/skills/update", methods=["POST", "OPTIONS"])
        async def api_skills_update(request: Request):
            """Guarda la edición de un skill existente (SKILL.md completo).

            Body: {name, content, scope?}. `content` es el archivo tal cual se
            escribe en disco, frontmatter YAML incluido.
            """
            _remember_request(request)
            if request.method == "OPTIONS":
                return JSONResponse({}, headers=cors_headers())
            try:
                data = await request.json()
            except Exception:
                data = {}
            name = str(data.get("name", "")).strip()
            content = str(data.get("content", ""))
            if not name:
                return JSONResponse({"success": False, "error": "'name' es obligatorio"},
                                    status_code=400, headers=cors_headers())
            if not content.strip():
                return JSONResponse({"success": False, "error": "'content' es obligatorio"},
                                    status_code=400, headers=cors_headers())
            try:
                skill_mgr = get_skill_manager(str(get_workspace()))
                updated = skill_mgr.update_skill(name, content, scope=data.get("scope") or None)
            except Exception as e:
                return JSONResponse({"success": False, "error": str(e)},
                                    status_code=500, headers=cors_headers())
            if not updated:
                return JSONResponse({"success": False, "error": f"Skill '{name}' no se pudo actualizar"},
                                    status_code=404, headers=cors_headers())
            skills = skill_mgr.list_skills(include_auto=True)
            return JSONResponse(
                {
                    "success": True,
                    "name": name,
                    "skills": skills,
                    "total": len(skills),
                    "active": list(skill_mgr._active_skills),
                },
                headers=cors_headers()
            )

        @mcp.custom_route("/api/skills/import", methods=["POST", "OPTIONS"])
        async def api_skills_import(request: Request):
            """Importa skills desde archivos .md (multipart) o desde {name, content}."""
            _remember_request(request)
            if request.method == "OPTIONS":
                return JSONResponse({}, headers=cors_headers())
            try:
                from tools.memory_skills import get_global_skills_dir
            except Exception:
                from memory_skills import get_global_skills_dir  # type: ignore
            dest = Path(get_global_skills_dir()) / "imported"
            saved: list = []
            try:
                form = await request.form()
                uploads = [value for value in form.values() if hasattr(value, "read")]
            except Exception:
                uploads = []

            if not uploads:
                try:
                    data = await request.json()
                except Exception:
                    data = {}
                content = str(data.get("content", ""))
                name = str(data.get("name", "")).strip() or "skill_importado"
                if not content.strip():
                    return JSONResponse(
                        {"success": False, "error": "Envía un archivo .md o {name, content}"},
                        status_code=400, headers=cors_headers(),
                    )
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / (name if name.lower().endswith(".md") else f"{name}.md")
                target.write_text(content, encoding="utf-8")
                saved.append(str(target))
            else:
                dest.mkdir(parents=True, exist_ok=True)
                for upload in uploads:
                    filename = os.path.basename(getattr(upload, "filename", "") or "skill.md")
                    if not filename.lower().endswith(".md"):
                        filename += ".md"
                    target = dest / filename
                    target.write_bytes(await upload.read())
                    saved.append(str(target))

            return JSONResponse({"success": True, "imported": saved, "count": len(saved)},
                                headers=cors_headers())

        @mcp.custom_route("/api/skills/search", methods=["POST"])
        async def api_skills_search(request: Request):
            """Busca skills por query (semántica o texto)."""
            try:
                data = await request.json()
                query = data.get("query", "")
                top_k = int(data.get("top_k", 5))
                
                skill_mgr = get_skill_manager(str(get_workspace()))
                results = skill_mgr.search_skills(query, top_k=top_k, only_active=False)
                
                return JSONResponse(
                    {
                        "success": True,
                        "query": query,
                        "results": results,
                        "total": len(results)
                    },
                    headers=cors_headers()
                )
            except Exception as e:
                return JSONResponse(
                    {"success": False, "error": str(e)},
                    status_code=500,
                    headers=cors_headers()
                )
        
        @mcp.custom_route("/api/skills/activate", methods=["POST"])
        async def api_skills_activate(request: Request):
            """Activa un skill."""
            try:
                data = await request.json()
                skill_name = data.get("skill_name")
                
                skill_mgr = get_skill_manager(str(get_workspace()))
                success = skill_mgr.activate_skill(skill_name)
                
                return JSONResponse(
                    {
                        "success": success,
                        "skill_name": skill_name,
                        "message": f"Skill '{skill_name}' {'activado' if success else 'no encontrado'}"
                    },
                    headers=cors_headers()
                )
            except Exception as e:
                return JSONResponse(
                    {"success": False, "error": str(e)},
                    status_code=500,
                    headers=cors_headers()
                )
        
        @mcp.custom_route("/api/skills/deactivate", methods=["POST"])
        async def api_skills_deactivate(request: Request):
            """Desactiva un skill."""
            try:
                data = await request.json()
                skill_name = data.get("skill_name")
                
                skill_mgr = get_skill_manager(str(get_workspace()))
                success = skill_mgr.deactivate_skill(skill_name)
                
                return JSONResponse(
                    {
                        "success": success,
                        "skill_name": skill_name,
                        "message": f"Skill '{skill_name}' desactivado"
                    },
                    headers=cors_headers()
                )
            except Exception as e:
                return JSONResponse(
                    {"success": False, "error": str(e)},
                    status_code=500,
                    headers=cors_headers()
                )

    # ============================================================
    # TAREAS PROGRAMADAS (CRUD persistente en daemon_state.json)
    # El daemon ejecuta las tareas pendientes cuando corre.
    # ============================================================

    def _load_daemon_state() -> dict:
        state_path = get_workspace() / ".daemon" / "daemon_state.json"
        try:
            if state_path.exists():
                return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logging.warning(f"No se pudo leer daemon_state.json: {e}")
        return {}

    def _save_daemon_state(state: dict) -> None:
        try:
            state_path = get_workspace() / ".daemon" / "daemon_state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logging.warning(f"No se pudo guardar daemon_state.json: {e}")

    @mcp.custom_route("/daemon_tasks/list", methods=["GET"])
    async def fastmcp_daemon_tasks_list(request: Request):
        """Lista las tareas programadas del daemon."""
        try:
            state = _load_daemon_state()
            return JSONResponse({"tasks": state.get("scheduled_tasks", [])}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500, headers=cors_headers())

    @mcp.custom_route("/daemon_tasks/create", methods=["POST"])
    async def fastmcp_daemon_tasks_create(request: Request):
        """Crea una tarea programada (ISO datetime)."""
        try:
            data = await request.json()
            description = str(data.get("description", "")).strip()
            when = str(data.get("when", "")).strip()
            if not description or not when:
                return JSONResponse(
                    {"success": False, "error": "'description' y 'when' son obligatorios"},
                    status_code=400,
                    headers=cors_headers(),
                )
            state = _load_daemon_state()
            scheduled = state.setdefault("scheduled_tasks", [])
            task_id = f"task_{int(time.time())}_{len(scheduled)}"
            task = {
                "id": task_id,
                "description": description,
                "when": when,
                "repeat": data.get("repeat") or None,
                "created_at": datetime.now().isoformat(),
                "status": "pending",
                "last_executed": None,
                "next_execution": when,
                "result": None,
            }
            scheduled.append(task)
            _save_daemon_state(state)
            return JSONResponse({"success": True, "task": task}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500, headers=cors_headers())

    @mcp.custom_route("/daemon_tasks/update/{task_id}", methods=["POST"])
    async def fastmcp_daemon_tasks_update(request: Request):
        """Actualiza una tarea programada."""
        try:
            task_id = request.path_params["task_id"]
            data = await request.json()
            state = _load_daemon_state()
            task = next((t for t in state.get("scheduled_tasks", []) if t.get("id") == task_id), None)
            if not task:
                return JSONResponse({"success": False, "error": "Tarea no encontrada"}, status_code=404, headers=cors_headers())
            for key in ("description", "when", "repeat", "status"):
                if key in data:
                    task[key] = data[key]
                    if key == "when":
                        task["next_execution"] = data[key]
            _save_daemon_state(state)
            return JSONResponse({"success": True, "task": task}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500, headers=cors_headers())

    @mcp.custom_route("/daemon_tasks/delete/{task_id}", methods=["DELETE", "POST"])
    async def fastmcp_daemon_tasks_delete(request: Request):
        """Elimina una tarea programada."""
        try:
            task_id = request.path_params["task_id"]
            state = _load_daemon_state()
            tasks = state.get("scheduled_tasks", [])
            filtered = [t for t in tasks if t.get("id") != task_id]
            if len(filtered) == len(tasks):
                return JSONResponse({"success": False, "error": "Tarea no encontrada"}, status_code=404, headers=cors_headers())
            state["scheduled_tasks"] = filtered
            _save_daemon_state(state)
            return JSONResponse({"success": True, "task_id": task_id}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500, headers=cors_headers())

    # ============================================================
    # TAREAS PROGRAMADAS: scheduler autónomo y chats generados
    # ============================================================

    def _scheduler():
        from .scheduler_service import get_scheduler_service

        return get_scheduler_service(str(get_workspace()))

    @mcp.custom_route("/scheduler/status", methods=["GET", "OPTIONS"])
    async def fastmcp_scheduler_status(request: Request):
        """Estado del scheduler: activo, modelo listo, próxima tarea, último run."""
        _remember_request(request)
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=cors_headers())
        try:
            return JSONResponse({"success": True, **_scheduler().status()}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500,
                                headers=cors_headers())

    @mcp.custom_route("/scheduler/run/{task_id}", methods=["POST", "OPTIONS"])
    async def fastmcp_scheduler_run(request: Request):
        """Ejecuta una tarea programada ahora mismo (sin esperar su hora)."""
        _remember_request(request)
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=cors_headers())
        task_id = request.path_params["task_id"]
        try:
            started = _scheduler().run_now(task_id)
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500,
                                headers=cors_headers())
        if not started:
            return JSONResponse(
                {"success": False,
                 "error": "La tarea no existe, ya hay otra ejecución en curso o el modelo no está cargado"},
                status_code=409, headers=cors_headers(),
            )
        return JSONResponse({"success": True, "task_id": task_id, "status": "running"},
                            status_code=202, headers=cors_headers())

    @mcp.custom_route("/daemon_chats", methods=["GET", "OPTIONS"])
    async def fastmcp_daemon_chats(request: Request):
        """Chats generados por tareas programadas, para importarlos al historial."""
        _remember_request(request)
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=cors_headers())
        try:
            since = request.query_params.get("since")
            raw_limit = request.query_params.get("limit", "50")
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 50
            chats = _scheduler().chat_store.list(since=since, limit=limit)
            return JSONResponse({"success": True, "chats": chats}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500,
                                headers=cors_headers())

    @mcp.custom_route("/daemon_chats/{chat_id}", methods=["GET", "DELETE", "OPTIONS"])
    async def fastmcp_daemon_chat_detail(request: Request):
        """GET: conversación completa (`conv` + `messages`). DELETE: la descarta."""
        _remember_request(request)
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=cors_headers())
        chat_id = request.path_params["chat_id"]
        try:
            store = _scheduler().chat_store
            if request.method == "DELETE":
                deleted = store.delete(chat_id)
                return JSONResponse({"success": deleted, "deleted": chat_id},
                                    status_code=200 if deleted else 404, headers=cors_headers())
            chat = store.get(chat_id)
            if not chat:
                return JSONResponse({"success": False, "error": f"Chat '{chat_id}' no encontrado"},
                                    status_code=404, headers=cors_headers())
            return JSONResponse({"success": True, "chat": chat}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=500,
                                headers=cors_headers())

    # ---------- Router Laya ONNX (estado / cargar / liberar) ----------
    @mcp.custom_route("/api/laya/status", methods=["GET", "OPTIONS"])
    async def fastmcp_laya_status(request: Request):
        """Estado del router Laya: instalado/cargado/enabled, sin descargar nada."""
        _remember_request(request)
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=cors_headers())
        try:
            return JSONResponse(_laya_router().status(), headers=cors_headers())
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500, headers=cors_headers())

    @mcp.custom_route("/api/laya/load", methods=["POST", "OPTIONS"])
    async def fastmcp_laya_load(request: Request):
        """Fuerza la carga del modelo (útil para medir el tiempo de carga)."""
        _remember_request(request)
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=cors_headers())
        try:
            router = _laya_router()
            loaded = router.load()
            return JSONResponse({"loaded": loaded, **router.status()}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"loaded": False, "error": str(e)}, status_code=500,
                                headers=cors_headers())

    @mcp.custom_route("/api/laya/unload", methods=["POST", "OPTIONS"])
    async def fastmcp_laya_unload(request: Request):
        """Libera la RAM del modelo (~650 MB)."""
        _remember_request(request)
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=cors_headers())
        try:
            router = _laya_router()
            router.unload()
            return JSONResponse({"loaded": False, **router.status()}, headers=cors_headers())
        except Exception as e:
            return JSONResponse({"loaded": False, "error": str(e)}, status_code=500,
                                headers=cors_headers())

    # La automatización 24/7 vive en un servicio aparte (`tools/automation_service.py`,
    # puerto 15001). Este proceso ya no arranca el dispatcher ni expone /automation/*.

    # Obtener todas las herramientas internas (para execute_tool)
    all_tools = get_all_tool_functions()

    
    # ============================================================
    # HERRAMIENTA QUERY_TOOLS (descubrimiento semántico con esquemas)
    # ============================================================
    @mcp.tool()
    def query_tools(query: str) -> str:
        """Busca y devuelve las herramientas más relevantes para una tarea.
        Tools base disponibles vía execute_tool: read_file, write_file, edit_file,
        file_glob_search, grep_search, exec_shell_command, get_datetime,
        list_directory, run_python_code.
        """
        return query_tools_global(query)

    # ============================================================
    # HERRAMIENTA EXECUTE_TOOL (ejecución robusta)
    # ============================================================
    @mcp.tool()
    def execute_tool(tool_name: Optional[str] = None, arguments: dict = None) -> str:
        """Ejecuta una herramienta interna por nombre.
        Tools base: read_file(path), write_file(path,content),
        edit_file(path,old_text,new_text), file_glob_search(pattern,path='.'),
        grep_search(pattern,path='.'), exec_shell_command(command),
        get_datetime(), list_directory(path,depth=0),
        run_python_code(code,venv=None).
        """
        return execute_tool_global(tool_name, arguments)
    logging.info(f"📦 Herramientas expuestas: query_tools + execute_tool (acceso a {len(all_tools)} herramientas internas)")
    _mcp_instance = mcp
    return mcp


def _laya_router():
    """Router Laya singleton (import tolerante a ejecución suelta)."""
    try:
        from .laya_onnx_router import get_laya_router
    except ImportError:  # python tools/dev_agent.py
        from tools.laya_onnx_router import get_laya_router  # type: ignore
    return get_laya_router(lazy=True)


def api_cors_headers() -> Dict[str, str]:
    """CORS del API de agentes (una sola fuente con `tools/auth.py`).

    El preflight tenía una lista propia —`GET, POST, DELETE, OPTIONS`— que se
    quedó corta en cuanto apareció un endpoint `PUT` (guardar un agente): el
    navegador bloqueaba el guardado con «Method PUT is not allowed by
    Access-Control-Allow-Methods in preflight response». Delegando en
    `auth.cors_headers()` la lista no puede volver a desincronizarse, y con
    `Access-Control-Max-Age` se ahorra un preflight por petición.
    """
    headers: Dict[str, str] = {}
    try:
        try:
            from .auth import cors_headers as _auth_cors_headers
        except Exception:  # ejecución suelta (python tools/dev_agent.py)
            from tools.auth import cors_headers as _auth_cors_headers

        headers.update(_auth_cors_headers())
    except Exception:
        headers.update({
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Token, X-Requested-With",
        })
    headers.setdefault("Access-Control-Max-Age", "86400")
    headers.setdefault("Cache-Control", "no-cache")
    headers.setdefault("Vary", "Origin")
    return headers


class DevAgentHttpProxy:
    """Cliente JSON-RPC del dev_agent HTTP, usado por el proxy stdio.

    El transporte MCP Streamable HTTP **exige un handshake por sesión**:
    `initialize` (cuya respuesta trae `Mcp-Session-Id`) y la notificación
    `notifications/initialized`. Si se manda `tools/call` a pelo, el servidor
    contesta `400 Bad Request: Missing session ID` y ninguna tool del proxy
    funciona (era el fallo real: "proxy stdio no pudo alcanzar HTTP dev_agent:
    400 Client Error ... for url: http://127.0.0.1:15000/mcp").
    """

    def __init__(self, endpoint: str = "http://127.0.0.1:15000/mcp",
                 timeout: float = 60.0) -> None:
        import requests as _requests

        self.endpoint = endpoint
        self.timeout = timeout
        self.session = _requests.Session()
        self.session_id: Optional[str] = None
        self._ids = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------
    # Transporte
    # ---------------------------------------------------------------
    def _next_id(self) -> int:
        with self._lock:
            self._ids += 1
            return self._ids

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _dispatch(self, payload: Dict[str, Any]):
        return self.session.post(self.endpoint, json=payload, headers=self._headers(),
                                 timeout=self.timeout)

    def open_session(self) -> None:
        """Crea la sesión MCP (initialize + initialized)."""
        self.session_id = None
        response = self._dispatch({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "llama-webui-stdio-proxy", "version": "1.0"},
            },
        })
        response.raise_for_status()
        session_id = (response.headers.get("Mcp-Session-Id")
                      or response.headers.get("mcp-session-id"))
        if not session_id:
            raise RuntimeError("el servidor MCP no devolvió Mcp-Session-Id")
        self.session_id = session_id
        # Notificación sin `id`: el servidor responde 202 y sin cuerpo.
        self._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _jsonrpc_payload(response) -> Dict[str, Any]:
        """Cuerpo JSON-RPC de la respuesta, venga como SSE o como JSON plano."""
        body = response.text or ""
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" in content_type or body.lstrip().startswith("event:"):
            for block in body.split("\n\n"):
                for line in block.splitlines():
                    if line.startswith("data:"):
                        try:
                            return json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _session_expired(response) -> bool:
        """400/401/404 de sesión caducada (p.ej. el dev_agent se reinició)."""
        if response.status_code not in (400, 401, 404):
            return False
        try:
            error = (response.json() or {}).get("error") or {}
            message = str(error.get("message", "") if isinstance(error, dict) else error)
        except ValueError:
            message = response.text or ""
        return "session" in message.casefold()

    @staticmethod
    def extract_text(result: Any) -> str:
        """Concatena el contenido de texto de un resultado de tool MCP."""
        content = result.get("content", []) if isinstance(result, dict) else []
        texts = [str(c.get("text", "")) for c in content if isinstance(c, dict)]
        return "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)

    # ---------------------------------------------------------------
    # API
    # ---------------------------------------------------------------
    def call_tool(self, tool_name: str, arguments: Optional[dict] = None) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }
        for attempt in (1, 2):
            try:
                if not self.session_id:
                    self.open_session()
                response = self._dispatch(payload)
                if attempt == 1 and self._session_expired(response):
                    self.session_id = None
                    continue
                response.raise_for_status()
                data = self._jsonrpc_payload(response)
                error = data.get("error")
                if error:
                    message = error.get("message") if isinstance(error, dict) else error
                    return f"Error: {message}"
                return self.extract_text(data.get("result", {}))
            except Exception as e:
                return f"Error: proxy stdio no pudo alcanzar HTTP dev_agent: {e}"
        return "Error: proxy stdio no pudo abrir sesión con HTTP dev_agent"


def _create_stdio_proxy_server():
    """Servidor MCP stdio que forwardea a la instancia HTTP en :15000.

    No importa ChromaDB ni SQLite ni el catálogo de agentes. Solo expone
    las 2 meta-tools vía requests HTTP al JSON-RPC del dev_agent principal.
    """
    from fastmcp import FastMCP

    mcp = FastMCP("dev-agent-stdio-proxy")
    proxy = DevAgentHttpProxy()

    @mcp.tool()
    def query_tools(query: str) -> str:
        """Busca y devuelve las herramientas más relevantes para una tarea.
                Tools base disponibles vía execute_tool: read_file, write_file, edit_file,
                file_glob_search, grep_search, exec_shell_command, get_datetime,
                list_directory, run_python_code.
                """
        return proxy.call_tool("query_tools", {"query": query})

    @mcp.tool()
    def execute_tool(tool_name: str = None, arguments: dict = None) -> str:
        """Ejecuta una herramienta interna por nombre.
                Tools base: read_file(path), write_file(path,content),
                edit_file(path,old_text,new_text), file_glob_search(pattern,path='.'),
                grep_search(pattern,path='.'), exec_shell_command(command),
                get_datetime(), list_directory(path,depth=0),
                run_python_code(code,venv=None).
                """
        return proxy.call_tool("execute_tool", {"tool_name": tool_name, "arguments": arguments or {}})

    return mcp

def build_mcp_arg_parser():
    """Parser CLI del servidor MCP: fuente única de verdad.

    Lo comparten este módulo (`python -m tools.dev_agent`) y el subcomando
    `--run-mcp-server` del Manager. Antes cada camino declaraba sus propias
    banderas y el arranque en desarrollo rechazaba `--host`/`--port`
    (argparse abortaba con "unrecognized arguments").
    """
    import argparse

    parser = argparse.ArgumentParser(prog="dev_agent", description="Servidor MCP de dev_agent")
    parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Interfaz de escucha (sólo transporte http)")
    parser.add_argument("--port", type=int, default=15000,
                        help="Puerto de escucha (sólo transporte http)")
    return parser


def parse_mcp_args(argv=None):
    """Devuelve el namespace con transport/host/port del servidor MCP."""
    return build_mcp_arg_parser().parse_args(argv)


def run_mcp_server(host: str = "0.0.0.0", port: int = 15000, transport: str = "http"):
    # Configurar el logging raíz UNA vez, sin importar cómo se haya cargado el
    # módulo. Antes, basicConfig vivía dentro de `if __name__ == "__main__":`
    # y en el arranque onefile (`--run-mcp-server`) ese bloque NUNCA se ejecuta:
    # el módulo se importa, no se lanza como script. Consecuencia: todos los
    # logger.info de tools/* (Laya, scheduler, Orchestrator...) se descartaban.
    import logging as _logging
    if not _logging.getLogger().handlers:
        _logging.basicConfig(
            level=_logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    global _mcp_instance
    if _mcp_instance is None:
         _mcp_instance = create_mcp_server(transport=transport)
    if _mcp_instance is None:
        sys.stderr.write("No se pudo crear el servidor MCP.\n")
        return
    if transport == "stdio":
        _mcp_instance.run(transport="stdio")
    else:
        # --- precarga de Laya ---
        try:
            from .laya_onnx_router import preload_laya_if_enabled
            info = preload_laya_if_enabled()
            logging.info(
                "🧠 Preload Laya: enabled=%s preload=%s installed=%s loaded=%s seconds=%s err=%s",
                info["enabled"], info["preload"], info["installed"],
                info["loaded"], info["seconds"], info["error"],
            )
        except Exception as exc:
            logging.warning(f"🧠 Preload de Laya omitido: {exc}")

        # --- scheduler ---
        try:
            from .scheduler_service import start_scheduler_service
            start_scheduler_service()
        except Exception as exc:
            logging.warning(f"Scheduler de tareas no disponible: {exc}")

        # --- automatización 24/7 ---
        # Vive en su propio proceso (`tools/automation_service.py`, puerto 15001):
        # el agente HTTP no arranca el dispatcher para que la autonomía no
        # dependa de este server. Arráncalo con:
        #     python -m tools.automation_service

                # --- DIAGNÓSTICO TEMPORAL: dump de hilos cada 60 s a un FICHERO ---
        if os.environ.get("DEVAGENT_DEBUG_STACKS", "1") == "1":
            try:
                import threading as _t, traceback as _tb, sys as _sys, time as _time
                from pathlib import Path as _P
                _dump_path = _P(runtime.get_user_data_dir()) / "devagent_stacks.log"

                def _dumper():
                    while True:
                        _time.sleep(60)
                        try:
                            frames = _sys._current_frames()
                            with open(_dump_path, "a", encoding="utf-8") as _fh:
                                _fh.write(f"\n===== STACK dump ({_time.strftime('%H:%M:%S')}, {len(frames)} threads) =====\n")
                                for tid, frame in frames.items():
                                    _fh.write(f"\n--- Thread {tid} ---\n")
                                    _tb.print_stack(frame, file=_fh)
                                _fh.flush()
                        except Exception:
                            pass
                _t.Thread(target=_dumper, daemon=True, name="stack-dump").start()
                logging.info("📦 Dump de stacks → %s (cada 60s). DEVAGENT_DEBUG_STACKS=0 lo desactiva", _dump_path)
            except Exception as exc:
                logging.warning(f"📦 No se pudo activar el dump de stacks: {exc}")

        # *** ESTAS DOS LLAMADAS SON LAS QUE FALTABAN ***
        _mcp_instance.run(transport="http", host=host, port=port)
        try:
            from .scheduler_service import stop_scheduler_service
            stop_scheduler_service()
        except Exception:
            pass


if __name__ == "__main__":
    import logging
    args = parse_mcp_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    try:
        output = sys.stderr if args.transport == "stdio" else sys.stdout
        print(f"📁 Workspace actual: {get_workspace()}", file=output, flush=True)
        print(f"📁 Modo: {settings.get('workspace_mode', 'default')}", file=output, flush=True)
        if FAST_MCP_AVAILABLE:
            print("FastMCP disponible", file=output, flush=True)
        else:
            print("FastMCP no está disponible. El servidor no se iniciará.", file=output, flush=True)
        if args.transport == "stdio":
            print("Iniciando servidor MCP con FastMCP en modo stdio", file=sys.stderr, flush=True)
        else:
            print(f"Iniciando servidor MCP con FastMCP en {args.host}:{args.port}", flush=True)
    except Exception as e:
        sys.stderr.write(f"Error al imprimir: {e}\n")
        log_file = USER_DATA_DIR / "mcp_server_debug.log"
        sys.stderr = open(log_file, "a", encoding="utf-8")
        sys.stdout = sys.stderr
    if not FAST_MCP_AVAILABLE:
        sys.exit(1)
    time.sleep(0.2)
    run_mcp_server(host=args.host, port=args.port, transport=args.transport)