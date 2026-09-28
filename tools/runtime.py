# runtime.py
from pathlib import Path
import sys
import site
import os
import json
import shutil
import subprocess
import re
from typing import Optional
def get_base_dir():
    """Retorna el directorio raíz del proyecto.

    Si está congelado (PyInstaller .exe), devuelve la carpeta del ejecutable.
    Si se ejecuta desde código fuente, runtime.py está en `tools/`, por lo que
    el root del proyecto es el padre de `tools/`.
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent

def get_data_dir() -> str:
    data_dir = get_base_dir() / "app_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir)

def get_user_data_dir() -> str:
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    user_dir = base / "user_data"
    user_dir.mkdir(parents=True, exist_ok=True)
    return str(user_dir)

def get_app_dir() -> Path:
    return get_base_dir() / "app"

def get_system_python() -> Optional[Path]:
    """Busca el intérprete Python más reciente en el PATH del sistema.
    Retorna None si no se encuentra ninguno."""
    candidates = []
    for name in ["python", "python3"]:
        path = shutil.which(name)
        if path:
            candidates.append(Path(path))
    if not candidates:
        return None

    # Intentar obtener la versión y elegir la más reciente
    def get_version(py_path: Path) -> tuple:
        try:
            kwargs = {}
            if os.name == "nt":
                # Evita el destello de consola al sondear python.exe.
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            result = subprocess.run(
                [str(py_path), "--version"],
                capture_output=True,
                text=True,
                timeout=2,
                **kwargs,
            )
            output = result.stdout.strip() or result.stderr.strip()
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
            if match:
                return tuple(map(int, match.groups()))
        except Exception:
            pass
        return (0, 0, 0)

    # Ordenar por versión descendente y devolver la más alta
    candidates_sorted = sorted(candidates, key=get_version, reverse=True)
    return candidates_sorted[0]


def get_python_exe() -> Path:
	"""Retorna el intérprete Python a usar para subprocesos.
	- En desarrollo: devuelve el Python del venv si existe, o del sistema.
	- En compilado onefile: devuelve el propio ejecutable (que actúa como intérprete).
	"""
	if getattr(sys, 'frozen', False):
		return Path(sys.executable)
	
	# Prioridad 1: .venv del proyecto
	base = get_base_dir()
	if sys.platform == "win32":
		venv_python = base / ".venv" / "Scripts" / "python.exe"
	else:
		venv_python = base / ".venv" / "bin" / "python"
	if venv_python.exists():
		return venv_python
	
	# Prioridad 2: app/ (entorno empaquetado)
	app_dir = get_app_dir()
	if sys.platform == "win32":
		python_exe = app_dir / "Scripts" / "python.exe"
	else:
		python_exe = app_dir / "bin" / "python"
	if python_exe.exists():
		return python_exe
	
	# Prioridad 3: Python del sistema
	try:
		import shutil
		py_sh = shutil.which("python") or shutil.which("python3")
		if py_sh:
			return Path(py_sh)
	except Exception:
		pass
	
	if sys.executable:
		return Path(sys.executable)
	
	raise FileNotFoundError(f"No se encontró Python empaquetado ni en PATH; buscado: {venv_python}")

def get_open_webui_cmd() -> list:
    """
    Retorna el comando para iniciar open-webui serve.
    Prioriza el ejecutable directo (open-webui.exe en Windows) y fallback a python -m.
    """
    app_dir = get_app_dir()
    if sys.platform == "win32":
        exe = app_dir / "Scripts" / "open-webui.exe"
    else:
        exe = app_dir / "bin" / "open-webui"
    if exe.exists():
        return [str(exe), "serve"]
    else:
        # Fallback: usar el intérprete con -m open_webui
        return [str(get_python_exe()), "-m", "open_webui", "serve"]

def resource_path(*parts) -> str:
    """Retorna la ruta a un recurso empaquetado (dentro de _MEIPASS) o en desarrollo.

    Usa `get_base_dir()` para apuntar al root del proyecto cuando no está congelado.
    """
    # Prefer the base directory next to the executable (or project root).
    candidates = []
    try:
        candidates.append(Path(get_base_dir()))
    except Exception:
        candidates.append(Path.cwd())
    # If running from a bundled environment, also consider PyInstaller _MEIPASS
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        try:
            candidates.append(Path(sys._MEIPASS))
        except Exception:
            pass
    # Finally, consider the tools/ directory where this file lives
    try:
        tools_base = Path(__file__).resolve().parent
        candidates.append(tools_base)
    except Exception:
        pass

    for base in candidates:
        try:
            candidate = base.joinpath(*parts)
            if candidate.exists():
                return str(candidate)
        except Exception:
            continue

    # Fallback to first candidate even if missing
    try:
        return str(candidates[0].joinpath(*parts))
    except Exception:
        return os.path.join(*parts)

def add_app_site_packages() -> bool:
    sp = get_app_dir() / "Lib" / "site-packages"
    if sp.exists():
        try:
            site.addsitedir(str(sp))
            return True
        except Exception:
            return False
    return False

def get_embedding_config() -> dict:
    """
    Lee la configuración del servidor de embeddings desde user_data/config.json.
    """
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'user_data', 'config.json')
    config = {}
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            full_config = json.load(f)
            config = full_config.get('embedding', {})

    # Valores por defecto apuntando a tu infraestructura
    default_config = {
        'embedding_url': 'http://localhost:8082',  # Puerto donde corre llama-server
        'embedding_model': 'nomic-embed'           # El modelo que tienes en app_data/embed
    }
    default_config.update(config)
    return default_config

# ===== Gestión de workspace y modo global =====

class RuntimeConfig:
    _config = None
    _config_path = None

    @classmethod
    def _get_config_path(cls):
        if cls._config_path is None:
            cls._config_path = os.path.join(get_user_data_dir(), "config.json")
        return cls._config_path

    @classmethod
    def _load_config(cls):
        if cls._config is None:
            path = cls._get_config_path()
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        cls._config = json.load(f)
                except:
                    cls._config = {}
            else:
                cls._config = {}
        return cls._config

    @classmethod
    def _save_config(cls):
        path = cls._get_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cls._config, f, indent=2)

    @classmethod
    def get_workspace(cls) -> str:
        config = cls._load_config()
        return config.get('workspace', 'agente_workspace')

    @classmethod
    def set_workspace(cls, path: str):
        config = cls._load_config()
        config['workspace'] = path
        cls._save_config()

    @classmethod
    def get_global_mode(cls) -> bool:
        config = cls._load_config()
        return config.get('global_mode', False)

    @classmethod
    def set_global_mode(cls, enabled: bool):
        config = cls._load_config()
        config['global_mode'] = enabled
        cls._save_config()

    @classmethod
    def get_workspace_abs(cls) -> str:
        workspace = cls.get_workspace()
        if os.path.isabs(workspace):
            return workspace
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = get_base_dir()
        return os.path.abspath(os.path.join(base_dir, workspace))
    
    