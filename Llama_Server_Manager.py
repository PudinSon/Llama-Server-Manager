# Llama_Server_Manager.py
# Versión con arranque secuencial inteligente, cierre robusto, gestión de RAG,
# y optimizaciones de rendimiento para MCP (flash-attn, cache-reuse, etc.)
# AHORA: Checkboxes en Configuración Avanzada para --agent y --no-ui.
# NUEVO: Selector de interfaz (WebUI/CLI) y botón para lanzar CLI.

try:
    from pathlib import Path
    import sys
    _THIS_DIR = Path(__file__).resolve().parent
    _PROJECT_ROOT = _THIS_DIR.parent
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
except Exception:
    pass

from venv import logger

import tools.runtime as runtime
import tools.web_viewer as web_viewer
runtime.add_app_site_packages()
import ctypes
import tempfile
import pystray
from PIL import Image, ImageTk
import tkinter as tk
import customtkinter as ctk
import subprocess
import psutil
import time
import socket
import errno
import os
import json
import threading
import queue
import sys
import shutil
import atexit
import signal
import webbrowser
import requests
from datetime import datetime
from tkinter import filedialog, simpledialog, ttk, messagebox

import importlib
import re
try:
    logmod = importlib.import_module("_internal.logger")
except Exception:
    import tools.logger as logmod
import tools.process_manager as process_manager
import tools.web_helper as web_helper
from tools.dev_agent import DEFAULT_SETTINGS
from tools.llama_params import (
    DEFAULT_REASONING_FORMAT,
    LLAMA_PERFORMANCE_DEFAULTS,
    REASONING_FORMATS,
    LlamaCommandMixin,
    migrate_reasoning_format,
)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

ROUTER_MODE_TOKEN = "__router__"
WEB_MANAGER_PORT = 12000
#: Puerto del servicio de automatización 24/7 (dispatchers, cola y webhooks).
#: Lo arranca este manager como proceso hijo: sin manager no hay modelo.
AUTOMATION_PORT = int(os.environ.get("AUTOMATION_PORT", "15001"))
try:
    from tools.env_detector import has_gui_environment
    HAS_GUI = has_gui_environment()
except Exception:
    HAS_GUI = True


class PerfTooltip:
    """Tooltip nativo sin dependencias (tk.Toplevel sin decoraciones).

    Muestra un texto de ayuda al pasar el ratón por un widget. Se enlaza también
    a los widgets hijos (los de CustomTkinter son compuestos y disparan Leave
    falsos al moverse entre padre e hijo): el ocultado se difiere HIDE_MS para
    que el tooltip no parpadee. Pensado para la pestaña "Rendimiento y
    Avanzado" del diálogo avanzado.
    """

    DELAY_MS = 600  # espera antes de mostrar
    HIDE_MS = 150   # gracia antes de ocultar (permite pasar a widgets hijos)
    WRAPLENGTH = 320

    def __init__(self, widget: tk.Misc, text: str):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        self._after_id = None
        self._hide_after_id = None
        self._bind_tree(widget)

    def _bind_tree(self, w: tk.Misc):
        try:
            w.bind("<Enter>", self._on_enter, add="+")
            w.bind("<Leave>", self._on_leave, add="+")
            w.bind("<ButtonPress>", self._on_leave, add="+")
            w.bind("<Destroy>", self._on_leave, add="+")
        except Exception:
            return
        try:
            for child in w.winfo_children():
                self._bind_tree(child)
        except Exception:
            pass

    def _on_enter(self, _event=None):
        self._cancel_hide()
        if self.tipwindow is not None:
            return
        self._cancel_show()
        try:
            self._after_id = self.widget.after(self.DELAY_MS, self._show)
        except Exception:
            pass

    def _on_leave(self, _event=None):
        self._cancel_show()
        if self.tipwindow is None:
            return
        self._cancel_hide()
        try:
            self._hide_after_id = self.widget.after(self.HIDE_MS, self._hide)
        except Exception:
            pass

    def _cancel_show(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _cancel_hide(self):
        if self._hide_after_id is not None:
            try:
                self.widget.after_cancel(self._hide_after_id)
            except Exception:
                pass
            self._hide_after_id = None

    def _hide(self):
        self._hide_after_id = None
        if self.tipwindow is not None:
            try:
                self.tipwindow.destroy()
            except Exception:
                pass
            self.tipwindow = None

    def _show(self):
        self._after_id = None
        if self.tipwindow is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            try:
                tw.attributes("-topmost", True)
            except Exception:
                pass
            tw.wm_geometry(f"+{x}+{y}")
            label = tk.Label(
                tw,
                text=self.text,
                justify="left",
                background="#2b2b2b",
                foreground="#f5f5f5",
                relief="solid",
                borderwidth=1,
                wraplength=self.WRAPLENGTH,
                padx=8,
                pady=6,
                font=("Segoe UI", 9),
            )
            label.pack(ipadx=1)
            self.tipwindow = tw
        except Exception:
            self.tipwindow = None


class LlamaServerManager(ctk.CTk, LlamaCommandMixin):
    def __init__(self):
        super().__init__()
        self._log_queue = queue.Queue()
        self._stop_log_thread = False
        self._reserved_ports = set()
        self._reserved_lock = threading.Lock()
        self.rag_server_process = None
        self.piper_process = None
        self._closing = False

        icon_path = runtime.resource_path("app.ico")
        if os.path.exists(icon_path):
            try:
                try:
                    self.iconbitmap(icon_path)
                except Exception:
                    pass
                try:
                    pil_img = Image.open(icon_path)
                    tk_img = ImageTk.PhotoImage(pil_img)
                    self.iconphoto(True, tk_img)
                    self._tk_icon_image = tk_img
                except Exception:
                    try:
                        self.iconphoto(True, tk.PhotoImage(file=icon_path))
                    except Exception as e:
                        self.log(f"No se pudo establecer icono: {e}")
            except Exception as e:
                self.log(f"No se pudo establecer icono: {e}")
        else:
            self.log(f"Icono no encontrado en {icon_path}")

        self.title("Llama Server Manager Pro")
        self.geometry("980x720")

        self.data_dir = runtime.get_data_dir()
        self.llama_exe = os.path.join(self.data_dir, "llama-cpp", "llama-server.exe")
        self.llama_cuda_exe = os.path.join(self.data_dir, "llama-cuda", "llama-server.exe")
        self.embed_dir = os.path.join(self.data_dir, "embed")
        self.model_dir = os.path.join(self.data_dir, "models")

        self.chat_process = None
        self.embedding_process = None
        self.log_text = None
        self.log_win = None
        self.viewer_process = None
        self.dev_agent_process = None
        self.workspace_server_process = None
        self.officecli_process = None

        self._nomic_in_progress_logged = False
        self._nomic_ready_logged = False
        self._opened_viewers = set()
        self._modal_opening = set()
        self._last_gui_log_line = None
        self._supported_flags = None
        self.chat_models = []
        self.logger = logmod.get_logger()
        logmod.attach_gui_callback(self._on_log_line)
        try:
            self._log_thread = threading.Thread(target=self._log_worker, daemon=True)
            self._log_thread.start()
        except Exception:
            self._log_thread = None

        self.tray_icon = None
        self.icon_image = None
        self.is_tray_minimized = False
        self._load_tray_icon()

        self.defaults = {
            "host": "0.0.0.0",
            "chat_port": 8081,
            "embed_port": 8082,
            "c": 16192,
            "threads_chat": 4,
            "threads_embed": 4,
            "n_gpu_layers": 24,
            "temp": 0.5,
            "repeat_penalty": 1.05,
            # Muestreo generalista (mismos defaults que llama.cpp). Van tras
            # --repeat-penalty en el comando; el panel web usa los mismos.
            "top_p": 0.95,
            "top_k": 40,
            "webui_mcp_proxy": True,
            "embed_chunk_size": 512,
            "embed_max_fragments": 3,
            "mmproj_path": os.path.join(self.embed_dir, "mmproj.gguf"),
            "workspace_path": str(Path.cwd() / "agente_workspace"),
            "global_mode": False,
            "auto_start_model": "",
            "auto_start_enabled": False,
            # IMPORTANTE: `--agent` activa la agencia NATIVA de llama-server.
            # Esta app tiene su propia agencia en `agent_core.Orchestrator`
            # (router + supervisor + catálogo). Si ambas están activas, el
            # servidor inyecta prompts de "agencia" (formato agencyStep) que se
            # pelean con los del orquestador y el modelo termina declarando
            # "✅ X completado" sin haber hecho nada. Desactivado por defecto.
            # El proxy MCP (`--webui-mcp-proxy`) es independiente y sigue ON.
            "use_agent": False,
            "no_ui": False,
            "piper_enabled": True,
            "interface_preference": "WebUI",  # NUEVO: WebUI o CLI

            # ── Rendimiento, DSpark y MoE (compartidos con el panel web) ──
            **LLAMA_PERFORMANCE_DEFAULTS,
        }

        for key, value in DEFAULT_SETTINGS.items():
            if key not in self.defaults:
                self.defaults[key] = value

        self.is_router_mode = False  # Flag para Router Mode

        self.create_widgets()
        self.load_config()
        # Forzar actualización de la etiqueta después de cargar configuración
        if hasattr(self, 'workspace_label'):
            workspace_path = self.defaults.get("workspace_path", str(Path.cwd() / "agente_workspace"))
            self.workspace_label.configure(text=workspace_path)
        # Actualizar selector de interfaz con valor guardado
        if hasattr(self, 'interface_selector'):
            self.interface_selector.set(self.defaults.get("interface_preference", "WebUI"))
        try:
            cfg_path = os.path.join(runtime.get_user_data_dir(), "config.json")
            if not os.path.exists(cfg_path):
                self.save_config()
        except Exception:
            pass

        self.profiles = {}
        self.profiles_file = os.path.join(logmod.LOG_DIR, "profiles.json")
        try:
            self.load_profiles()
        except Exception:
            pass

        try:
            self.after(50, self.scan_and_separate_models)
        except Exception:
            pass

        try:
            self.after(100, self.ensure_ffmpeg_in_path)
        except Exception:
            pass

        # Arrancar los componentes dentro de esta instancia del manager.
        self.after(60, self._start_app_components)

        try:
            self.protocol("WM_DELETE_WINDOW", self.on_close)
        except Exception:
            pass
        try:
            self._register_exit_handlers()
        except Exception:
            pass
        try:
            self.bind("<Unmap>", lambda e: self._on_window_unmap(e))
        except Exception:
            pass
        self.after(100, self.minimize_to_tray)
        self._start_web_manager_server()
        try:
            threading.Thread(target=self._get_supported_flags, daemon=True).start()
        except Exception:
            pass

    def _start_web_manager_server(self):
        """Inicia el panel web de administración en segundo plano."""
        try:
            from tools.web_manager_server import start_web_server

            thread = threading.Thread(
                target=start_web_server,
                kwargs={"host": "0.0.0.0", "port": WEB_MANAGER_PORT},
                daemon=True,
                name="web-manager-server",
            )
            thread.start()
            self.log(f"🌐 Web Manager disponible en http://localhost:{WEB_MANAGER_PORT}")
        except Exception as error:
            self.log(f"⚠️ No se pudo iniciar Web Manager: {error}")

    # ------------------------------------------------------------------
    # LOG
    # ------------------------------------------------------------------
    def log(self, message):
        try:
            self._enqueue_log("MAIN", message)
        except Exception:
            try:
                self.logger.info(message)
            except Exception:
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{timestamp}] {message}")

    # ------------------------------------------------------------------
    # ARRANQUE DE COMPONENTES DE LA APLICACION
    # ------------------------------------------------------------------
    def _start_app_components(self):
        """Inicia los componentes gestionados por esta instancia de la aplicación."""
        self._init_ipc()
        threading.Thread(target=self._start_services_thread, daemon=True).start()

    def _init_ipc(self):
        """Inicia el puente IPC Manager ↔ CLI y el estado compartido."""
        try:
            from tools.ipc import IPCBridge, SharedState, IPCMessageType
            self.shared_state = SharedState(
                state_file=Path(runtime.get_user_data_dir()) / "shared_state.json"
            )
            self.ipc_bridge = IPCBridge(role="manager")
            self.ipc_bridge.start()
            self.ipc_bridge.server.register_callback(IPCMessageType.TASK_COMPLETED, self._on_ipc_task_completed)
            self.ipc_bridge.server.register_callback(IPCMessageType.ERROR_CRITICAL, self._on_ipc_error_critical)
            self.log("🔌 IPC listo (Manager ↔ CLI)")
        except Exception as e:
            self.ipc_bridge = None
            self.shared_state = None
            self.log(f"⚠️ IPC no disponible: {e}")

    def _on_ipc_task_completed(self, msg):
        data = getattr(msg, "data", {}) or {}
        self.log(f"✅ [IPC] Tarea completada desde CLI: {data.get('task', data.get('task_id', ''))}")

    def _on_ipc_error_critical(self, msg):
        data = getattr(msg, "data", {}) or {}
        self.log(f"❌ [IPC] Error crítico desde CLI: {data.get('error', 'desconocido')}")

    def _start_services_thread(self):
        """Secuencia de arranque con espera activa de puertos."""
        self.log("🚀 Iniciando componentes de la aplicación...")
        
        # 0. Config Server (puerto 5001) - Primero para que esté disponible para la WebUI
        self._start_config_server()
        
        # 0.5 Telegram Service (si está configurado)
        self._start_telegram_service()

        # 1. Nomic embeddings (puerto 8082)
        self._start_nomic_with_wait()

        # 2. MCP HTTP (puerto 15000)
        self.start_dev_agent()
        # Opcional, dentro de _start_services_thread, tras start_dev_agent():
        try:
           from tools.laya_onnx_router import laya_enabled, laya_preload
           if laya_enabled() and laya_preload():
                self.log("🧠 Laya: precarga solicitada (dev_agent la aplicará al arrancar)")
        except Exception:
            pass
        # 2.5 Workspace HTTP (puerto 19000)
        self.start_workspace_server()

        # 2.6 Automatización 24/7 (puerto 15001): dispatchers, cola y webhooks.
        # Vive en su propio proceso para que cerrar la web view no la detenga,
        # pero es hija del manager: sin manager no hay modelo cargado y sin
        # modelo los dispatchers no tendrían nada que ejecutar.
        if self.defaults.get("automation_enabled", True):
            self.start_automation_service()
        else:
            self.log("⏭️ Automatización 24/7 desactivada en Configuración Avanzada.")

        # 3. OfficeCLI MCP (stdio)
        self._ensure_mcp_config()
        self.start_officecli()

        # 4. RAG Service (puerto 25000)
        self.start_rag_service()

        # 5. Piper FastAPI TTS Service (puerto 8880), opcional por consumo de RAM.
        if self.defaults.get("piper_enabled", True):
            self.start_piper_service()
        else:
            self.log("⏭️ Piper desactivado en Configuración Avanzada.")

        # 5. Auto-inicio de modelo/router
        self.after(0, self.auto_start_model_if_enabled)

        self.log("✅ Componentes de la aplicación iniciados.")

    def _wait_for_port(self, port, timeout=30, interval=0.5):
        """Espera a que el puerto esté disponible (escuchando). Retorna True si éxito."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                result = sock.connect_ex(('127.0.0.1', port))
                sock.close()
                if result == 0:
                    return True
            except:
                pass
            time.sleep(interval)
        return False

    def _start_nomic_with_wait(self):
        nomic_path = os.path.join(self.embed_dir, "nomic-embed-text-v1.5.f16.gguf")
        if os.path.exists(nomic_path):
            self.log("📦 Iniciando Nomic embeddings...")
            self.start_embedding(nomic_path)
            if self._wait_for_port(8082, timeout=60):
                self.log("✅ Nomic embeddings listo (puerto 8082)")
            else:
                self.log("⚠️ Nomic no respondió en 60s, continuando...")
        else:
            self.log("⚠️ Nomic no encontrado, omitiendo.")

    # ------------------------------------------------------------------
    # OFFICECLI (MCP Server para Office)
    # ------------------------------------------------------------------
    def _ensure_mcp_config(self):
        """Genera la configuración stdio solo para servidores que realmente lo requieren.

        dev_agent se gestiona por HTTP para la WebUI y por stdio para llama-server,
        que solo admite ese transporte en su proxy MCP. El proceso stdio usa un
        timeout amplio porque la carga inicial de herramientas es pesada.
        """
        mcp_config_path = os.path.join(self.data_dir, "mcp.json")
        officecli_path = os.path.join(self.data_dir, "office", "officecli.exe" if os.name == 'nt' else "officecli")

        if getattr(sys, "frozen", False):
            dev_agent_command = sys.executable
            dev_agent_args = ["--run-mcp-server", "--transport", "stdio"]
        else:
            dev_agent_command = str(runtime.get_python_exe())
            dev_agent_args = ["-m", "tools.dev_agent", "--transport", "stdio"]

        config = {"mcpServers": {
            "dev_agent": {
                "command": dev_agent_command,
                "args": dev_agent_args,
                "timeout_ms": 120000,
                "tool_prefix": False,
                "env": {
                    "PYTHONPATH": str(runtime.get_base_dir()),
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1"
                }
            }
        }}
        if os.path.exists(officecli_path):
            config["mcpServers"]["officecli"] = {
                "command": officecli_path,
                "args": ["mcp"],
                "env": {"RUST_LOG": "info"}
            }
        else:
            self.log("⚠️ OfficeCLI no encontrado; se registrará solo dev_agent en mcp.json.")
        
        try:
            with open(mcp_config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            self.log(f"✅ mcp.json generado en {mcp_config_path}")
            return mcp_config_path
        except Exception as e:
            self.log(f"❌ Error generando mcp.json: {e}")
            return None

    def start_officecli(self):
        """Inicia OfficeCLI como servidor MCP (modo stdio)."""
        officecli_path = os.path.join(self.data_dir, "office", "officecli.exe" if os.name == 'nt' else "officecli")
        if not os.path.exists(officecli_path):
            self.log("❌ OfficeCLI no encontrado. Descárgalo en app_data/office/")
            return
        
        if self.officecli_process and self.officecli_process.poll() is None:
            self.log("⚠️ OfficeCLI ya está en ejecución.")
            return
        
        # Asegurar ejecutable en Linux/Mac
        if os.name != 'nt':
            try:
                os.chmod(officecli_path, 0o755)
            except Exception:
                pass
        
        cmd = [officecli_path, "mcp"]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        
        try:
            proc = subprocess.Popen(
                cmd,
                creationflags=creationflags,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            self.officecli_process = proc
            self._monitor_process_output(proc, "OFFICECLI")
            self.log(f"✅ OfficeCLI MCP iniciado (PID {proc.pid})")
        except Exception as e:
            self.log(f"❌ Error iniciando OfficeCLI: {e}")

    def stop_officecli(self, wait=True):
        """Detiene el proceso OfficeCLI."""
        if self.officecli_process:
            try:
                pid = self.officecli_process.pid
                self.log(f"Deteniendo OfficeCLI (PID {pid})...")
                process_manager.kill_process_tree(pid, timeout=3.0)
                self.officecli_process = None
                self.log("✅ OfficeCLI detenido.")
            except Exception as e:
                self.log(f"Error deteniendo OfficeCLI: {e}")
                self.officecli_process = None

    # ------------------------------------------------------------------
    # BANDEJA
    # ------------------------------------------------------------------
    def _load_tray_icon(self):
        icon_path = runtime.resource_path("app.ico")
        self.icon_image = None
        if os.path.exists(icon_path):
            try:
                pil_img = Image.open(icon_path)
                try:
                    pil_img = pil_img.convert("RGBA")
                except Exception:
                    pil_img = pil_img.convert("RGB")
                try:
                    pil_img = pil_img.resize((64, 64), Image.LANCZOS)
                except Exception:
                    pass
                self.icon_image = pil_img
            except Exception as e:
                self.log(f"No se pudo cargar icono para bandeja: {e}")
                self.icon_image = None
        if self.icon_image is None:
            self.icon_image = Image.new('RGBA', (64, 64), color=(0, 120, 215, 255))

    def _tcp_port_free_via_bind(self, port: int) -> bool:
        try:
            hosts = []
            try:
                host_default = str(self.defaults.get('host', '')).strip()
                if host_default:
                    hosts.append(host_default)
            except Exception:
                pass
            for candidate in ('127.0.0.1', '0.0.0.0'):
                if candidate not in hosts:
                    hosts.append(candidate)
            any_success = False
            for h in hosts:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1.0)
                    s.bind((h, int(port)))
                    s.listen(1)
                    s.close()
                    any_success = True
                    break
                except OSError as e:
                    if getattr(e, 'errno', None) in (errno.EADDRINUSE, errno.EACCES):
                        continue
                    continue
                except Exception:
                    continue
            return any_success
        except Exception:
            return False

    def _build_tray_menu(self):
        def on_quit(*args):
            icon = args[0] if args else None
            try:
                if icon is not None:
                    try:
                        icon.visible = False
                    except Exception:
                        pass
                    try:
                        icon.stop()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self.after(0, self.on_close)
            except Exception:
                try:
                    self.on_close()
                except Exception:
                    pass

            # Red de seguridad: si por algún motivo el cierre ordenado no llega
            # a ejecutarse (p. ej. el bucle de Tk ya no procesa `after`),
            # forzamos la salida del exe. El tiempo es holgado para NO cortar la
            # parada secuencial de los servicios (que es lo que antes dejaba
            # procesos vivos).
            def _fallback_exit():
                try:
                    time.sleep(max(self.SHUTDOWN_GRACE_SECONDS + 30.0, 90.0))
                except Exception:
                    pass
                self._force_exit_process(code=0)
            try:
                threading.Thread(target=_fallback_exit, daemon=True).start()
            except Exception:
                pass

        def on_show(*args):
            try:
                self.after(0, self.restore_from_tray)
            except Exception:
                try:
                    self.restore_from_tray()
                except Exception:
                    pass

        def on_reopen_webview(*args):
            
            try:
                self.after(0, self._open_interface)
            except Exception:
                try:
                    self._open_interface()
                except Exception:
                    pass

        def on_status_clicked(*args):
            try:
                self.after(0, self._open_interface)
            except Exception:
                try:
                    self._open_interface()
                except Exception:
                    pass

        def on_start_model(*args):
            try:
                self.after(0, self.start_selected_chat)
            except Exception:
                try:
                    self.start_selected_chat()
                except Exception:
                    pass

        def on_start_router(*args):
            try:
                self.after(0, self.start_router_mode)
            except Exception:
                try:
                    self.start_router_mode()
                except Exception:
                    pass

        status_label = "🔎 Viewer — WebUI"
        model_items = []
        try:
            if getattr(self, 'chat_models', None):
                for m in list(self.chat_models):
                    try:
                        model_basename = os.path.splitext(m)[0].lower()
                        mmproj_exists = False
                        try:
                            per_model_dir = os.path.join(self.data_dir, "mmproj", model_basename)
                            if os.path.exists(per_model_dir):
                                for f in os.listdir(per_model_dir):
                                    if f.lower().endswith('.gguf'):
                                        mmproj_exists = True
                                        break
                        except Exception:
                            mmproj_exists = False
                        try:
                            if not mmproj_exists and os.path.exists(self.embed_dir):
                                for f in os.listdir(self.embed_dir):
                                    if f.lower().endswith('.gguf') and model_basename in f.lower():
                                        mmproj_exists = True
                                        break
                        except Exception:
                            pass
                        is_active = False
                        try:
                            current_ready = getattr(self, '_model_ready_name', None)
                            current_starting = getattr(self, '_current_model', None)
                            if current_ready:
                                if model_basename in str(current_ready).lower():
                                    is_active = True
                            elif current_starting:
                                if model_basename in str(current_starting).lower():
                                    is_active = True
                        except Exception:
                            pass
                        prefix = "🟢 " if is_active else ""
                        display = f"{prefix}{m}"
                        suffixes = self._model_suffixes(m, mmproj_exists=mmproj_exists)
                        if suffixes:
                            display = f"{display} {' '.join(suffixes)}"
                    except Exception:
                        display = m

                    def _mk_cb(name):
                        return lambda *a, n=name: (self.after(0, lambda: self._tray_start_model(n)))
                    try:
                        model_items.append(pystray.MenuItem(display, _mk_cb(m)))
                    except Exception:
                        model_items.append(pystray.MenuItem(display, lambda *a: None))
            else:
                model_items.append(pystray.MenuItem("(no models)", lambda *a: None, enabled=False))
        except Exception:
            model_items = [pystray.MenuItem("(error)", lambda *a: None, enabled=False)]

        try:
            menu = pystray.Menu(
                pystray.MenuItem(status_label, on_status_clicked),
                pystray.MenuItem("Iniciar modelo", pystray.Menu(*model_items)),
                pystray.MenuItem("Iniciar Router Mode", on_start_router),
                pystray.MenuItem("Abrir Interfaz", on_reopen_webview, default=True),
                pystray.MenuItem("Abrir Manager", on_show),
                pystray.MenuItem("Salir", on_quit)
            )
        except Exception:
            menu = pystray.Menu(
                pystray.MenuItem("Abrir Interfaz", on_reopen_webview, default=True),
                pystray.MenuItem("Abrir Manager", on_show),
                pystray.MenuItem("Salir", on_quit)
            )
        return menu

    def _update_tray_menu(self):
        try:
            if self.tray_icon is None:
                return
            try:
                self.tray_icon.menu = self._build_tray_menu()
            except Exception:
                try:
                    visible = getattr(self.tray_icon, 'visible', True)
                    self.tray_icon.stop()
                except Exception:
                    pass
                try:
                    self.tray_icon = pystray.Icon("LlamaManager", self.icon_image, "Llama Server Manager", self._build_tray_menu())
                    self.tray_icon.run_detached()
                    try:
                        self.tray_icon.visible = visible
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass

    def _tray_start_model(self, model_name: str):
        try:
            if not getattr(self, 'chat_models', None):
                self.log("⚠️ No hay modelos disponibles para iniciar desde la bandeja")
                return
            try:
                idx = self.chat_models.index(model_name)
            except Exception:
                idx = None
                for i, m in enumerate(self.chat_models):
                    if model_name == m or model_name in m or m in model_name:
                        idx = i
                        break
            if idx is None:
                self.log(f"⚠️ Modelo no encontrado en lista: {model_name}")
                return
            try:
                self.chat_listbox.selection_clear(0, tk.END)
                self.chat_listbox.selection_set(idx)
                self.chat_listbox.see(idx)
            except Exception:
                pass
            try:
                self.log(f"[tray] iniciando modelo desde bandeja: idx={idx} {model_name}")
                self.start_selected_chat()
            except Exception as e:
                try:
                    self.after(0, self.start_selected_chat)
                except Exception:
                    self.log(f"Error iniciando modelo desde bandeja: {e}")
        except Exception as e:
            self.log(f"Error en _tray_start_model: {e}")

    def minimize_to_tray(self):
        if self.is_tray_minimized:
            return
        self.is_tray_minimized = True
        self.withdraw()
        if self.tray_icon is None:
            menu = self._build_tray_menu()
            self.tray_icon = pystray.Icon("LlamaManager", self.icon_image, "Llama Server Manager", menu)
            try:
                self.tray_icon.run_detached()
            except Exception:
                try:
                    threading.Thread(target=lambda: self.tray_icon.run(), daemon=True).start()
                except Exception:
                    pass
        else:
            self.tray_icon.visible = True
        self.log("Aplicación minimizada a la bandeja del sistema.")

    def restore_from_tray(self):
        if not self.is_tray_minimized:
            return
        self.is_tray_minimized = False
        self.deiconify()
        self.lift()
        self.focus_force()
        if self.tray_icon is not None:
            self.tray_icon.visible = False
        self.log("Ventana restaurada desde la bandeja.")

    def _on_window_unmap(self, event):
        try:
            if getattr(self, 'is_tray_minimized', False):
                return
            try:
                st = self.state()
            except Exception:
                st = None
            if st == 'iconic':
                try:
                    self.after(50, self.minimize_to_tray)
                except Exception:
                    self.minimize_to_tray()
        except Exception:
            pass

    def _monitor_webview_process(self):
        proc = self.viewer_process
        if proc is None:
            return
        try:
            proc.wait()
            time.sleep(0.5)
            self.viewer_process = None
            self.log("Viewer cerrado")
        except Exception as e:
            self.log(f"Error monitoreando visor: {e}")

    def _on_log_line(self, line: str):
        try:
            if self.log_text and getattr(self.log_text, 'winfo_exists', lambda: False)():
                try:
                    trimmed = (line or "").strip()
                    if getattr(self, '_last_gui_log_line', None) == trimmed:
                        return
                    self._last_gui_log_line = trimmed
                except Exception:
                    pass

                def _ins():
                    try:
                        self.log_text.insert("end", line + "\n")
                        self.log_text.see("end")
                    except Exception:
                        pass

                try:
                    self.log_text.after(0, _ins)
                except Exception:
                    try:
                        _ins()
                    except Exception:
                        pass
        except Exception:
            pass

    def _update_start_buttons_state(self):
        try:
            state = "normal"
            if hasattr(self, 'start_chat_btn') and self.start_chat_btn:
                try:
                    self.start_chat_btn.configure(state=state)
                except Exception:
                    pass
            if hasattr(self, 'start_router_btn') and self.start_router_btn:
                try:
                    self.start_router_btn.configure(state=state)
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # WIDGETS
    # ------------------------------------------------------------------
    def create_widgets(self):
        ctk.CTkLabel(self, text="Llama Server Manager Pro", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=10)

        folder_frame = ctk.CTkFrame(self)
        folder_frame.pack(padx=25, pady=8, fill="x")
        ctk.CTkButton(folder_frame, text="Cambiar Carpeta de Modelos", width=220, command=self.select_model_folder).pack(
            side="left", padx=15)
        self.dir_label = ctk.CTkLabel(folder_frame, text=f"Modelos: {self.model_dir[:50]}...")
        self.dir_label.pack(side="left", padx=10)

        # ---------- INTERFAZ PREFERIDA (NUEVO) ----------
        interface_frame = ctk.CTkFrame(self)
        interface_frame.pack(padx=25, pady=8, fill="x")

        ctk.CTkLabel(interface_frame, text="Interfaz preferida:").pack(side="left", padx=8)
        self.interface_selector = ctk.CTkOptionMenu(
            interface_frame,
            values=["WebUI", "CLI"],
            command=self._on_interface_preference_change,
            width=120
        )
        self.interface_selector.set(self.defaults.get("interface_preference", "WebUI"))
        self.interface_selector.pack(side="left", padx=8)

        # ---------- LISTA DE MODELOS Y AUTO-INICIO ----------
        lists_frame = ctk.CTkFrame(self)
        lists_frame.pack(padx=25, pady=12, fill="both", expand=False)

        modelos_frame = ctk.CTkFrame(lists_frame)
        modelos_frame.grid(row=0, column=0, padx=(0, 0), sticky="nsew")

        ctk.CTkLabel(modelos_frame, text="Modelos de Chat", font=ctk.CTkFont(size=16, weight="bold"),
                     text_color="lightblue").pack(anchor="w", padx=5, pady=(6, 4))

        # Listbox
        self.chat_listbox = tk.Listbox(modelos_frame, height=8, width=60, bg="#1e1e1e", fg="white",
                                       selectbackground="#0066cc")
        self.chat_listbox.pack(padx=8, pady=4, fill="both", expand=False)

        # Frame para auto-inicio y botones
        auto_frame = ctk.CTkFrame(modelos_frame)
        auto_frame.pack(pady=6, padx=6, fill="x")

        self.auto_start_var = tk.IntVar(value=1 if self.defaults.get("auto_start_enabled", False) else 0)
        self.auto_start_check = ctk.CTkCheckBox(auto_frame, text="Auto-iniciar modelo al arrancar",
                                                variable=self.auto_start_var,
                                                command=self.save_auto_start_setting)
        self.auto_start_check.pack(side="left", padx=(0, 10))

        # Combobox para seleccionar modelo a auto-iniciar (incluye Router Mode)
        self.auto_model_combo = ctk.CTkComboBox(auto_frame, values=[], width=200)
        self.auto_model_combo.pack(side="left", padx=(0, 10))
        self.auto_model_combo.set("")

        # Botones de acción
        action_frame = ctk.CTkFrame(modelos_frame)
        action_frame.pack(pady=6, padx=6, fill="x")
        self.start_chat_btn = ctk.CTkButton(action_frame, text="Iniciar Chat", fg_color="green", height=36,
                                            command=self.start_selected_chat)
        self.start_chat_btn.pack(side="left", padx=(0, 8))
        self.start_router_btn = ctk.CTkButton(action_frame, text="Iniciar Router Mode", fg_color="#2b6ef6", height=36,
                                              command=self.start_router_mode)
        self.start_router_btn.pack(side="left")

        lists_frame.grid_columnconfigure(0, weight=1)

        # Botones inferiores
        bottom = ctk.CTkFrame(self)
        bottom.pack(pady=15, fill="x", padx=25)
        ctk.CTkButton(bottom, text="Detener Chat", fg_color="red", command=self.stop_chat).pack(side="left", padx=10)

        # Botón "Abrir Interfaz" (reemplaza "Reabrir WebView")
        self.reopen_btn = ctk.CTkButton(bottom, text="Abrir Interfaz", fg_color="#2b8fef",
                                        command=self._open_interface)
        self.reopen_btn.pack(side="left", padx=10)

        # Botón "Abrir CLI" (nuevo)
        cli_btn = ctk.CTkButton(bottom, text="💻 Abrir CLI", fg_color="#6c5ce7",
                                command=self._launch_cli_button)
        cli_btn.pack(side="left", padx=10)

        ctk.CTkButton(bottom, text="Configuración Avanzada", fg_color="gray25", command=self.open_advanced_config).pack(
            side="right", padx=10)

        logs_label = ctk.CTkLabel(self, text="Logs (persistentes, tiempo real):", font=ctk.CTkFont(size=13, weight="bold"))
        logs_label.pack(anchor="w", padx=20, pady=(6, 0))
        self.log_text = ctk.CTkTextbox(self, height=20, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_text.pack(padx=20, pady=(6, 18), fill="both", expand=True)
        try:
            log_file = os.path.join(logmod.LOG_DIR, "llama_manager.log")
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8", errors="replace") as lf:
                    data = lf.read()
                    if data:
                        try:
                            self.log_text.insert("end", data + "\n")
                            self.log_text.see("end")
                        except Exception:
                            pass
        except Exception:
            pass

        # Actualizar combo de auto-inicio
        self.after(100, self.populate_auto_model_combo)

    def populate_auto_model_combo(self):
        """Llena el combobox de auto-inicio con los modelos disponibles + Router Mode,
        y sincroniza el checkbox con la configuración guardada."""
        try:
            models = getattr(self, 'chat_models', [])
            items = ["Router Mode"] + models
            self.auto_model_combo.configure(values=items)

            # Seleccionar el valor guardado
            saved = self.defaults.get("auto_start_model", "")
            if saved == ROUTER_MODE_TOKEN:
                self.auto_model_combo.set("Router Mode")
            elif saved and saved in models:
                self.auto_model_combo.set(saved)
            else:
                self.auto_model_combo.set(items[0] if items else "")

            # Actualizar checkbox con el estado guardado
            if hasattr(self, 'auto_start_var'):
                self.auto_start_var.set(1 if self.defaults.get("auto_start_enabled", False) else 0)
        except Exception as e:
            self.log(f"Error poblando combo auto-inicio: {e}")

    def save_auto_start_setting(self):
        """Guarda la configuración de auto-inicio."""
        enabled = bool(self.auto_start_var.get())
        selected = self.auto_model_combo.get()
        if selected == "Router Mode":
            model_token = ROUTER_MODE_TOKEN
        else:
            model_token = selected
        self.defaults["auto_start_enabled"] = enabled
        self.defaults["auto_start_model"] = model_token
        self.save_config()
        self.log(f"Auto-inicio {'activado' if enabled else 'desactivado'} para: {selected}")

    def auto_start_model_if_enabled(self):
        """Si auto-inicio está activado, inicia el modelo o el router."""
        if self.defaults.get("auto_start_enabled", False):
            model_token = self.defaults.get("auto_start_model", "")
            if model_token == ROUTER_MODE_TOKEN:
                self.log("Auto-iniciando Router Mode...")
                self.start_router_mode()
            elif model_token and model_token in getattr(self, 'chat_models', []):
                try:
                    idx = self.chat_models.index(model_token)
                    self.chat_listbox.selection_clear(0, tk.END)
                    self.chat_listbox.selection_set(idx)
                    self.chat_listbox.see(idx)
                    self.log(f"Auto-iniciando modelo: {model_token}")   
                    self.start_selected_chat()
                except Exception as e:
                    self.log(f"Error en auto-inicio: {e}")
            else:
                self.log("Auto-inicio configurado pero modelo no disponible.")

    def connect_remote_server(self):
        """Abre el visor apuntando a la URL ingresada."""
        url = self.remote_url_entry.get().strip()
        if not url:
            messagebox.showwarning("URL vacía", "Por favor ingresa una URL válida.")
            return
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        self.log(f"Conectando a servidor remoto: {url}")
        self._launch_web_viewer(url, "remote")

    # ------------------------------------------------------------------
    # WORKSPACE Y GLOBAL MODE
    # ------------------------------------------------------------------
    def change_workspace_dialog(self):
         folder = filedialog.askdirectory(title="Seleccionar carpeta de workspace",
                                     initialdir=self.workspace_label.cget("text") if self.workspace_label else None)
         if folder:
            self.workspace_label.configure(text=folder)
            self.defaults["workspace_path"] = folder
            history = [item for item in self.defaults.get("workspace_history", []) if item != folder]
            self.defaults["workspace_history"] = [folder, *history[:9]]
            self.save_config()
            time.sleep(0.3)
            self.log(f"Workspace cambiado a: {folder}")
            # Reiniciar dev_agent para que tome la nueva configuración
            self.restart_dev_agent()

    def toggle_global_mode(self):
        mode = bool(self.global_mode_var.get())
        self.defaults["global_mode"] = mode
        self.defaults["workspace_mode"] = "global" if mode else "default"
        self.save_config()
        self.log(f"Modo global {'activado' if mode else 'desactivado'}")
        # Reiniciar dev_agent para que tome la nueva configuración
        self.restart_dev_agent()
    
    def restart_dev_agent(self):
        """Reinicia el proceso dev_agent para aplicar cambios de configuración."""
        self.stop_dev_agent(wait=True)
        time.sleep(0.5)  # Pequeña pausa para liberar recursos
        self.start_dev_agent()

    def select_model_folder(self):
        folder = filedialog.askdirectory(initialdir=self.model_dir)
        if folder:
            self.model_dir = folder
            self.dir_label.configure(text=f"Chat Models: {self.model_dir[:50]}...")
            self.scan_and_separate_models()
            self.save_config()

    def scan_and_separate_models(self):
        try:
            self.chat_listbox.delete(0, tk.END)
        except Exception:
            pass
        try:
            self.chat_models = []
        except Exception:
            self.chat_models = []
        if not os.path.exists(self.model_dir):
            self.log("❌ Carpeta de modelos no encontrada")
            return
        for file in sorted(os.listdir(self.model_dir)):
            if not file.lower().endswith(".gguf"):
                continue
            lower = file.lower()
            if any(k in lower for k in ["embed", "nomic", "jina", "bge", "text-matching"]):
                continue
            else:
                try:
                    model_basename = os.path.splitext(file)[0].lower()
                    per_model_dir = os.path.join(self.data_dir, "mmproj", model_basename)
                    os.makedirs(per_model_dir, exist_ok=True)
                except Exception:
                    pass
                mmproj_exists = False
                try:
                    if os.path.exists(per_model_dir):
                        for f in os.listdir(per_model_dir):
                            if f.lower().endswith('.gguf'):
                                mmproj_exists = True
                                break
                except Exception:
                    mmproj_exists = False
                try:
                    if not mmproj_exists and os.path.exists(self.embed_dir):
                        for f in os.listdir(self.embed_dir):
                            if f.lower().endswith('.gguf') and model_basename in f.lower():
                                mmproj_exists = True
                                break
                except Exception:
                    pass
                suffixes = self._model_suffixes(file, mmproj_exists=mmproj_exists)
                display_name = f"{file} {' '.join(suffixes)}" if suffixes else f"{file}"
                self.chat_listbox.insert(tk.END, display_name)
                self.chat_models.append(file)
        try:
            self.log(f"Modelos cargados → Chat: {self.chat_listbox.size()}")
            self.populate_auto_model_combo()
            try:
                self._update_tray_menu()
            except Exception:
                pass
        except Exception:
            pass

    def ensure_ffmpeg_in_path(self):
        try:
            import shutil
            ff = shutil.which('ffmpeg')
            if ff:
                try:
                    self.log(f"✅ ffmpeg encontrado en PATH: {ff}")
                except Exception:
                    pass
                return True
            try:
                ff_dir = os.path.join(runtime.get_data_dir(), 'ffmpeg-local')
            except Exception:
                try:
                    base = runtime.get_base_dir()
                    ff_dir = os.path.join(str(base), 'app_data', 'ffmpeg-local')
                except Exception:
                    ff_dir = os.path.join(os.getcwd(), 'app_data', 'ffmpeg-local')
            exe_name = 'ffmpeg.exe' if sys.platform.startswith('win') else 'ffmpeg'
            candidate = os.path.join(ff_dir, exe_name)
            if os.path.isdir(ff_dir):
                try:
                    old = os.environ.get('PATH', '')
                    if ff_dir not in old.split(os.pathsep):
                        os.environ['PATH'] = ff_dir + os.pathsep + old
                    if os.path.exists(candidate):
                        os.environ['FFMPEG_PATH'] = candidate
                        os.environ['WHISPER_FFMPEG_PATH'] = candidate
                except Exception:
                    pass
                try:
                    ff2 = shutil.which('ffmpeg')
                except Exception:
                    ff2 = None
                if ff2:
                    try:
                        self.log(f"✅ ffmpeg agregado a PATH: {ff2}")
                    except Exception:
                        pass
                    return True
                else:
                    try:
                        self.log(f"⚠️ ffmpeg no encontrado tras añadir {ff_dir} a PATH (candidate={candidate})")
                    except Exception:
                        pass
                    return False
            try:
                self.log(f"❌ ffmpeg no encontrado en PATH ni en carpeta local: {ff_dir}")
            except Exception:
                pass
            return False
        except Exception as e:
            try:
                self.log(f"Error verificando ffmpeg: {e}")
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # SERVICIOS INDEPENDIENTES (Embeddings, MCP)
    # ------------------------------------------------------------------
    def start_nomic_automatically(self):
        nomic_path = os.path.join(self.embed_dir, "nomic-embed-text-v1.5.f16.gguf")
        if os.path.exists(nomic_path):
            self.start_embedding(nomic_path)
        else:
            self.log(f"⚠️ Archivo Nomic no encontrado en carpeta '{self.embed_dir}'")

    def start_embedding(self, model_path):
        model_name = os.path.basename(model_path).lower()
        is_nomic = "nomic" in model_name
        port = 8082 if is_nomic else int(self.defaults.get("embed_port", 8082))
        host = "0.0.0.0" if is_nomic else str(self.defaults.get("host", "0.0.0.0"))
        if process_manager.is_port_in_use(port):
            if self.embedding_process:
                try:
                    self.log("Puerto de embeddings en uso, deteniendo proceso interno para liberar puerto...")
                    process_manager.stop_server(self.embedding_process, port=port, wait=2.0)
                    self.embedding_process = None
                except Exception:
                    self.log("No se pudo detener el proceso de embeddings existente. Abortando inicio.")
                    return
            else:
                self.log(f"Embeddings ya está activo en puerto {port}")
                return
        use_gpu = self.defaults.get("gpu_for_embeddings", True)
        exe = self.llama_cuda_exe if use_gpu else self.llama_exe
        if not os.path.exists(exe):
            self.log(f"❌ Ejecutable no encontrado: {exe}")
            return
        self.log(f"Embeddings → usando {'CUDA' if use_gpu else 'CPU'} ({exe})")
        cmd = [
            exe, "-m", model_path,
            "--host", host, "--port", str(port),
            "--threads", str(self.defaults.get("threads_embed", 4)),
            "--embeddings"
        ]
        if is_nomic:
            cmd.extend(["-c", "2048"])
            cmd.extend(["-b", "1024"])
            cmd.extend(["-ub", "1024"])
            cmd.extend(["--rope-scaling", "yarn"])
            cmd.extend(["--rope-freq-scale", "0.75"])
        if use_gpu:
            ngl_val = self._normalize_ngl(self.defaults.get("n_gpu_layers"), "6")
            cmd.extend(["-ngl", ngl_val])
            if not is_nomic:
                self.log(f"🖥️ GPU activada para embeddings: -ngl {ngl_val}")
        else:
            if not is_nomic:
                self.log("💡 GPU desactivada para embeddings (solo CPU)")
        if "jina" in model_name and "nomic" not in model_name:
            cmd.extend(["--pooling", "mean"])
            if not is_nomic:
                self.log("Jina detectado → --pooling mean")
        # Forzar --no-ui para embeddings (siempre)
        cmd.append("--no-ui")
        try:
            if not is_nomic:
                self.log(f"Comando embeddings: {' '.join(cmd)}")
        except Exception:
            pass
        try:
            cf = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') and os.name == 'nt' else 0
            try:
                self._embed_ready = False
            except Exception:
                pass
            proc = process_manager.start_server(cmd, port=port, creationflags=cf)
            self.embedding_process = proc
            self._monitor_process_output(proc, f"EMB-{os.path.basename(model_path)}")
            if not is_nomic:
                self.log(f"✅ Embeddings iniciado → {os.path.basename(model_path)}")
            self.update_status()
            return proc
        except Exception as e:
            self.log(f"❌ Error al iniciar embeddings: {e}")
            return None

    # ---------- DEV AGENT (MCP) ----------
    def start_dev_agent(self):
        port = 15000
        if process_manager.is_port_in_use(port):
            self.log(f"⚠️ Puerto {port} ocupado. Intentando liberar...")
            self.stop_dev_agent(wait=True)
            time.sleep(1.0)
            if process_manager.is_port_in_use(port):
                self.log(f"❌ No se pudo liberar el puerto {port}. Abortando inicio.")
                return

        env = os.environ.copy()
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(runtime.get_base_dir()) + os.pathsep + env.get("PYTHONPATH", "")
        })

        if getattr(sys, 'frozen', False):
            # Compilado: usar el propio ejecutable
            python_exe = sys.executable
            dev_script = '--run-mcp-server'  # Usamos el subcomando
            cmd = [python_exe, dev_script, "--host", "0.0.0.0", "--port", str(port)]
        else:
            # Desarrollo: arrancar como módulo del paquete para mantener el contexto
            # correcto de imports y evitar que el servidor se cierre por diferencia
            # entre ejecución como script y como módulo.
            try:
                python_exe = runtime.get_python_exe()
            except Exception as e:
                self.log(f"❌ No se encontró Python para ejecutar dev_agent: {e}")
                return
            cmd = [str(python_exe), "-m", "tools.dev_agent", "--host", "0.0.0.0", "--port", str(port)]

        cwd = str(runtime.get_base_dir())
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

        self.log(f"📂 Ejecutando: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                creationflags=creationflags,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=cwd,
                env=env
            )
            self.dev_agent_process = proc
            self._monitor_process_output(proc, "DEVAGENT")
            time.sleep(1.0)
            self.log(f"✅ dev_agent lanzado (PID {proc.pid}) en puerto {port}")
            time.sleep(2)
            if proc.poll() is not None:
                self.log(f"❌ dev_agent terminó prematuramente (código {proc.returncode}). " f"Revisa la línea '[DEVAGENT]' arriba con el error.")
                self.dev_agent_process = None
            else:
                self.log(f"✅ dev_agent en ejecución (PID {proc.pid})")
        except Exception as e:
            self.log(f"❌ Error iniciando dev_agent: {e}")
            self.dev_agent_process = None

    # ---------- WORKSPACE SERVER (puerto 19000) ----------
    def start_workspace_server(self):
        """Inicia el servidor HTTP del workspace (19000)."""
        port = 19000
        if process_manager.is_port_in_use(port):
            self.log(f"⚠️ Puerto {port} ocupado. Intentando liberar...")
            self.stop_workspace_server(wait=True)
            time.sleep(1.0)
            if process_manager.is_port_in_use(port):
                self.log(f"❌ No se pudo liberar el puerto {port}. Abortando inicio.")
                return

        env = os.environ.copy()
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(runtime.get_base_dir()) + os.pathsep + env.get("PYTHONPATH", ""),
        })
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, "--run-workspace-server", "--host", "0.0.0.0", "--port", str(port)]
        else:
            try:
                python_exe = runtime.get_python_exe()
            except Exception as e:
                self.log(f"❌ No se encontró Python para el workspace server: {e}")
                return
            cmd = [str(python_exe), "-m", "tools.workspace_server", "--host", "0.0.0.0", "--port", str(port)]

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            proc = subprocess.Popen(
                cmd,
                creationflags=creationflags,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=str(runtime.get_base_dir()),
                env=env,
            )
            self.workspace_server_process = proc
            self._monitor_process_output(proc, "WORKSPACE")
            # En onefile la primera ejecución extrae el bundle antes de bindear;
            # esperar activamente a que el puerto escuche (hasta ~20 s) en vez
            # de una sola comprobación a los 0.8 s.
            ready = False
            for _ in range(40):
                time.sleep(0.5)
                if proc.poll() is not None:
                    break
                if process_manager.is_port_in_use(port):
                    ready = True
                    break
            if proc.poll() is not None:
                self.log(f"❌ workspace server terminó prematuramente (código {proc.returncode}). " f"Revisa la línea '[WORKSPACE]' arriba con el error."    )
                self.workspace_server_process = None
            elif ready:
                self.log(f"✅ Workspace server escuchando en puerto {port} (PID {proc.pid})")
            else:
                self.log(f"⚠️ Workspace server (PID {proc.pid}) sigue arrancando; el puerto {port} aún no responde")
        except Exception as e:
            self.log(f"❌ Error iniciando workspace server: {e}")
            self.workspace_server_process = None

    # ------------------------------------------------------------------
    # AUTOMATIZACIÓN 24/7 (servicio de dispatchers en el puerto 15001)
    # ------------------------------------------------------------------
    def start_automation_service(self, wait=25.0):
        """Arranca el servicio 24/7 (dispatchers, cola y webhooks).

        El servicio corre en su PROPIO proceso: el visor y el panel web solo son
        clientes HTTP, así que cerrar la web view no lo detiene. Pero el manager
        es su dueño: lo arranca aquí y lo para en `on_close`, y si el manager
        muere de golpe el watchdog `--parent-pid` del servicio lo cierra al
        detectar que su padre desapareció. Sin manager no hay modelo y los
        dispatchers no tendrían nada que ejecutar.
        """
        port = AUTOMATION_PORT
        try:
            from tools.automation_process import start as start_automation

            result = start_automation(
                port_number=port,
                workspace=self.defaults.get("workspace_path"),
                parent_pid=os.getpid(),
                wait=wait,
            )
            if not result.get("success"):
                self.log(f"⚠️ Automatización 24/7 no arrancó: {result.get('error')}")
                for line in str(result.get("log_tail") or "").splitlines()[-5:]:
                    self.log(f"   [AUTO] {line}")
                return result
            if result.get("status") == "already_running":
                self.log(f"🤖 Automatización 24/7 ya estaba activa en el puerto {port}")
            elif result.get("status") == "starting":
                self.log(f"🤖 Automatización 24/7 arrancando (PID {result.get('pid')}); aún no escucha en {port}")
            else:
                self.log(f"🤖 Automatización 24/7 activa en {result.get('url')} (PID {result.get('pid')})")
            self.log(f"   log: {result.get('log')}")
            return result
        except Exception as e:
            self.log(f"⚠️ Automatización 24/7 no disponible: {e}")
            return {"success": False, "error": str(e)}

    def stop_automation_service(self, wait=3.0):
        """Detiene el servicio 24/7 y libera el puerto 15001."""
        port = AUTOMATION_PORT
        try:
            from tools.automation_process import stop as stop_automation

            result = stop_automation(port_number=port, wait=wait)
            if result.get("success"):
                self.log("⏹️ Automatización 24/7 detenida")
            return result
        except Exception as e:
            self.log(f"⚠️ No se pudo detener la automatización 24/7: {e}")
            return {"success": False, "error": str(e)}

    def stop_workspace_server(self, wait=True):
        """Detiene el servidor HTTP del workspace y libera el puerto 19000."""
        port = 19000
        proc = getattr(self, "workspace_server_process", None)
        if proc:
            try:
                process_manager.kill_process_tree(proc.pid, timeout=3.0)
            except Exception:
                pass
            self.workspace_server_process = None
        if process_manager.is_port_in_use(port):
            process_manager.stop_server(port=port, wait=2.0)

    def stop_dev_agent(self, wait=True):
        """Detiene el proceso dev_agent y libera el puerto 15000."""
        port = 15000
        if self.dev_agent_process:
            try:
                pid = self.dev_agent_process.pid
                self.log(f"Deteniendo dev_agent (PID {pid})...")
                killed = process_manager.kill_process_tree(pid, timeout=3.0)
                if not killed:
                    self.log("⚠️ Fallo al matar proceso, forzando...")
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
                    else:
                        os.kill(pid, signal.SIGKILL)
                self.dev_agent_process = None
            except Exception as e:
                self.log(f"Error deteniendo dev_agent: {e}")
                self.dev_agent_process = None
        # Liberar puerto si quedó ocupado
        if process_manager.is_port_in_use(port):
            self.log(f"Liberando puerto {port}...")
            process_manager.stop_server(port=port, wait=2.0)
        self.log(f"✅ dev_agent detenido (puerto {port} liberado)")

    # ---------- CONFIG SERVER (para WebUI) ----------
    def _start_config_server(self):
        """Inicia el servidor de configuración en puerto 5001."""
        try:
            from tools.config_server import start_config_server
            
            success = start_config_server(
                port=5001,
                get_config_callback=self._get_config_for_webui,
                update_config_callback=self._update_config_from_webui,
                select_workspace_callback=self._select_workspace_dialog,
                get_telegram_config_callback=self._get_telegram_config_for_webui,
                update_telegram_config_callback=self._update_telegram_config_from_webui,
                request_pairing_callback=self._request_telegram_pairing
            )
            
            if success:
                self.log("🔧 Servidor de configuración iniciado (puerto 5001)")
            else:
                self.log("❌ Error iniciando servidor de configuración")
        except Exception as e:
            self.log(f"⚠️ No se pudo iniciar config_server: {e}")
    
    def _get_config_for_webui(self) -> dict:
        """Devuelve la configuración actual para la WebUI."""
        return {
            "workspace_path": self.defaults.get("workspace_path", str(Path.cwd())),
            "global_mode": self.defaults.get("global_mode", False),
            "workspace_history": self.defaults.get("workspace_history", []),
            "telegram_system_prompt": self.defaults.get("telegram_system_prompt", ""),
        }
    
    def _update_config_from_webui(self, data: dict) -> dict:
        """Actualiza la configuración desde la WebUI."""
        try:
            if "workspace_path" in data:
                new_workspace = str(data["workspace_path"])
                # Validar que la ruta existe o crearla
                Path(new_workspace).mkdir(parents=True, exist_ok=True)
                self.defaults["workspace_path"] = new_workspace
                
                # Agregar al historial
                history = self.defaults.get("workspace_history", [])
                history = [item for item in history if item != new_workspace]
                history.insert(0, new_workspace)
                history = history[:10]
                self.defaults["workspace_history"] = history
                
                # Guardar configuración
                self.save_config()
                
                # Reiniciar dev_agent para que use el nuevo workspace
                self.restart_dev_agent()
                self.log(f"📁 Workspace actualizado a: {new_workspace}")
            
            if "global_mode" in data:
                enabled = bool(data["global_mode"])
                self.defaults["global_mode"] = enabled
                self.defaults["workspace_mode"] = "global" if enabled else "default"
                self.save_config()
                
                # Reiniciar dev_agent
                self.restart_dev_agent()
                self.log(f"🌍 Modo global: {'activado' if data['global_mode'] else 'desactivado'}")

            if "telegram_system_prompt" in data:
                self.defaults["telegram_system_prompt"] = str(data.get("telegram_system_prompt") or "")
                self.save_config()
            
            return {"success": True, "config": self._get_config_for_webui()}
        except Exception as e:
            self.log(f"❌ Error actualizando configuración: {e}")
            return {"success": False, "error": str(e)}
    
    def _select_workspace_dialog(self) -> str:
        """Abre un diálogo para seleccionar workspace y retorna la ruta."""
        try:
            path = filedialog.askdirectory(
                title="Seleccionar workspace",
                initialdir=self.defaults.get("workspace_path", str(Path.cwd()))
            )
            return path if path else None
        except Exception as e:
            self.log(f"❌ Error en diálogo de selección: {e}")
            return None
    
    # ---------- TELEGRAM SERVICE ----------
    def _start_telegram_service(self):
        """Inicia el servicio de Telegram si está configurado."""
        try:
            telegram_config = self.defaults.get("telegram", {})
            token = telegram_config.get("token")
            
            if not token:
                logger.debug("Telegram no configurado (sin token)")
                return
            
            from tools.telegram_service import start_telegram_service_in_thread
            
            allowed_users = telegram_config.get("allowed_users", [])
            pairing_required = telegram_config.get("pairing_required", True)
            
            success = start_telegram_service_in_thread(
                token=token,
                allowed_users=allowed_users,
                pairing_required=pairing_required
            )
            
            if success:
                self.log("🤖 Servicio de Telegram iniciado")
            else:
                self.log("⚠️ Error iniciando Telegram")
        
        except ImportError:
            self.log("⚠️ python-telegram-bot no instalado")
        except Exception as e:
            self.log(f"⚠️ Error iniciando Telegram: {e}")
    
    def _stop_telegram_service(self):
        """Detiene el servicio de Telegram."""
        try:
            from tools.telegram_service import stop_telegram_service_in_thread
            stop_telegram_service_in_thread(timeout=5.0)
            self.log("🛑 Servicio de Telegram detenido")
        except Exception as e:
            self.log(f"⚠️ Error deteniendo Telegram: {e}")
    
    # ------------------------------------------------------------------
    # TELEGRAM: callbacks para la WebUI (via config_server 5001)
    # ------------------------------------------------------------------
    def _get_telegram_config_for_webui(self) -> dict:
        """Devuelve la configuración de Telegram para la WebUI."""
        telegram_config = self.defaults.get("telegram", {})
        token_set = bool(telegram_config.get("token"))
        running = False
        try:
            from tools.telegram_service import is_telegram_running
            running = is_telegram_running()
        except Exception:
            pass
        return {
            "enabled": token_set,
            "token": "***" if token_set else None,
            "pairing_required": telegram_config.get("pairing_required", True),
            "allowed_users": list(telegram_config.get("allowed_users", [])),
            "status": "running" if running else "stopped",
            "active_users": len(telegram_config.get("allowed_users", [])),
        }
    
    def _update_telegram_config_from_webui(self, data: dict) -> dict:
        """Actualiza la configuración de Telegram desde la WebUI."""
        try:
            telegram_config = dict(self.defaults.get("telegram", {}))
            
            token = str(data.get("token") or "").strip()
            if token:
                telegram_config["token"] = token
            if "pairing_required" in data:
                telegram_config["pairing_required"] = bool(data["pairing_required"])
            if "allowed_users" in data:
                telegram_config["allowed_users"] = [str(u) for u in data["allowed_users"]]
            
            self.defaults["telegram"] = telegram_config
            self.save_config()
            
            # Decidir arranque/parada
            enabled = data.get("enabled")
            if enabled is None:
                enabled = bool(telegram_config.get("token"))
            
            if enabled and telegram_config.get("token"):
                self._start_telegram_service()
            else:
                self._stop_telegram_service()
            
            return self._get_telegram_config_for_webui()
        except Exception as e:
            self.log(f"❌ Error actualizando Telegram: {e}")
            return {"error": str(e)}
    
    def _request_telegram_pairing(self, data: dict) -> dict:
        """Aprueba el código generado por /pairing en Telegram."""
        try:
            code = str(data.get("code") or "").strip().upper()
            if not code:
                return {"error": "code requerido"}

            from tools.telegram_service import TelegramSessionDB
            user_id = TelegramSessionDB().approve_pairing_code(code)
            if not user_id:
                return {"error": "Código inválido, expirado o ya utilizado"}

            telegram_config = dict(self.defaults.get("telegram", {}))
            allowed_users = {str(value).strip() for value in telegram_config.get("allowed_users", [])}
            allowed_users.add(user_id)
            telegram_config["allowed_users"] = sorted(allowed_users)
            self.defaults["telegram"] = telegram_config
            self.save_config()

            if telegram_config.get("token"):
                self._stop_telegram_service()
                self._start_telegram_service()

            return {"success": True, "approved": True, "user_id": user_id, "code": code, "allowed_users": sorted(allowed_users)}
        except Exception as e:
            self.log(f"❌ Error generando pairing: {e}")
            return {"error": str(e)}
 
    # ---------- RAG SERVICE (gestionado por el Manager) ----------
    def start_rag_service(self):
        """Inicia el servidor RAG web (rag_web_server.py) si no está corriendo."""
        if self.rag_server_process and self.rag_server_process.poll() is None:
            return
        # Verificar si el puerto 25000 está en uso
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 25000))
        sock.close()
        if result == 0:
            self.log("📚 RAG ya está corriendo en puerto 25000")
            return

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

        if getattr(sys, 'frozen', False):
            # Compilado: usar el propio ejecutable con el subcomando
            cmd = [sys.executable, '--run-rag-server']
            cwd = str(runtime.get_base_dir())
        else:
            # Desarrollo: usar Python del sistema y el script
            try:
                py_exe = str(runtime.get_python_exe())
            except Exception as e:
                self.log(f"❌ No se encontró Python para ejecutar RAG: {e}")
                return
            rag_script = runtime.resource_path('tools', 'rag_web_server.py')
            if not os.path.exists(rag_script):
                self.log("❌ rag_web_server.py no encontrado")
                return
            cmd = [py_exe, rag_script]
            cwd = runtime.get_base_dir()

        self.log(f"📂 Ejecutando RAG: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=cwd,
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self.rag_server_process = proc
            self.log("📚 Servidor RAG iniciado en puerto 25000")
            # Esperar un momento y verificar
            time.sleep(2)
            sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result2 = sock2.connect_ex(('127.0.0.1', 25000))
            sock2.close()
            if result2 == 0:
                self.log("✅ RAG web escuchando en http://127.0.0.1:25000")
            else:
                self.log("⚠️ RAG no responde en puerto 25000 (puede estar iniciando)")
        except Exception as e:
            self.log(f"❌ Error iniciando RAG: {e}")

    def stop_rag_service(self, force=True):
        """Detiene el servidor RAG web, forzando si es necesario."""
        if self.rag_server_process:
            try:
                pid = self.rag_server_process.pid
                if pid:
                    self.log(f"Deteniendo RAG (PID {pid})...")
                    # Intentar terminar árbol
                    killed = process_manager.kill_process_tree(pid, timeout=3.0)
                    if killed:
                        self.log("✅ RAG detenido correctamente.")
                    else:
                        self.log("⚠️ No se pudo detener RAG, forzando kill...")
                        try:
                            if os.name == 'nt':
                                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
                            else:
                                os.kill(pid, signal.SIGKILL)
                            self.log("✅ RAG forzado con kill.")
                        except Exception as e:
                            self.log(f"Error forzando RAG: {e}")
                    self.rag_server_process = None
                else:
                    # Fallback: usar stop_server con puerto
                    process_manager.stop_server(self.rag_server_process, port=25000, wait=2.0)
                    self.rag_server_process = None
            except Exception as e:
                self.log(f"Error deteniendo RAG: {e}")
                self.rag_server_process = None
        else:
            # Intentar liberar el puerto por si acaso
            if process_manager.is_port_in_use(25000):
                process_manager.stop_server(port=25000, wait=2.0)
                self.log("📚 RAG detenido (puerto liberado)")

    # ---------- PIPER FASTAPI TTS SERVICE (gestionado por el Manager) ----------
    def start_piper_service(self):
        """Inicia el servidor Piper FastAPI desde tools/piper_service.py."""
        if self.piper_process and self.piper_process.poll() is None:
            return

        # Localizar el script (bundle tools/ o, por compatibilidad, app_data/piper/)
        script_path = Path(__file__).resolve().parent / "tools" / "piper_service.py"
        if not script_path.exists():
            try:
                script_path = Path(runtime.resource_path("tools", "piper_service.py"))
            except Exception:
                pass
        if not script_path.exists():
            fallback = Path(self.data_dir) / "piper" / "piper_service.py"
            if fallback.exists():
                script_path = fallback
            else:
                self.log("❌ piper_service.py no encontrado en tools/ ni en app_data/piper/")
                return
    
        voices_dir = Path(self.data_dir) / "piper" / "voices"
    
        # Verificar si el puerto 8880 ya está en uso
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 8880))
        sock.close()
        if result == 0:
            self.log("🎤 Piper FastAPI ya está corriendo en puerto 8880")
            return
    
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PIPER_PORT"] = "8880"
        # La clave que realmente lee piper_service.py:
        env["PIPER_MODEL_DIR"] = str(voices_dir)
        env["LLAMA_MANAGER_DATA_DIR"] = str(self.data_dir)
        env["PROJECT_ROOT"] = str(Path(self.data_dir).parent) if self.data_dir else str(Path.cwd())
    
        # Log dedicado
        log_path = Path(self.data_dir) / "piper" / "piper_subprocess.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8")
        log_file.write(f"\n--- Iniciando Piper (PID: {os.getpid()}) ---\n")
        log_file.flush()
    
        # *** CLAVE ***
        # En compilado, NUNCA hagas [sys.executable, script]; usa el subcomando.
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--run-piper-server"]
            cwd = str(runtime.get_base_dir())
        else:
            python_exe = runtime.get_python_exe()
            cmd = [str(python_exe), str(script_path)]
            cwd = str(script_path.parent)
    
        self.log(f"🎤 Iniciando Piper FastAPI: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=cwd,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
            )
            self.piper_process = proc
            self._monitor_process_output(proc, "PIPER")
            self.log("🎤 Servidor Piper FastAPI iniciado en puerto 8880")
            time.sleep(2)
            sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result2 = sock2.connect_ex(('127.0.0.1', 8880))
            sock2.close()
            if result2 == 0:
                self.log("✅ Piper FastAPI escuchando en http://127.0.0.1:8880")
            else:
                self.log("⚠️ Piper FastAPI no responde en puerto 8880 (puede estar iniciando)")
        except Exception as e:
            self.log(f"❌ Error iniciando Piper FastAPI: {e}")

    def stop_piper_service(self, force=True):
        """Detiene el servidor Piper FastAPI."""
        if self.piper_process:
            try:
                pid = self.piper_process.pid
                if pid:
                    self.log(f"Deteniendo Piper FastAPI (PID {pid})...")
                    killed = process_manager.kill_process_tree(pid, timeout=3.0)
                    if killed:
                        self.log("✅ Piper FastAPI detenido correctamente.")
                    else:
                        self.log("⚠️ No se pudo detener Piper FastAPI, forzando kill...")
                        try:
                            if os.name == 'nt':
                                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
                            else:
                                os.kill(pid, signal.SIGKILL)
                            self.log("✅ Piper FastAPI forzado con kill.")
                        except Exception as e:
                            self.log(f"Error forzando Piper FastAPI: {e}")
                    self.piper_process = None
                else:
                    process_manager.stop_server(self.piper_process, port=8880, wait=2.0)
                    self.piper_process = None
            except Exception as e:
                self.log(f"Error deteniendo Piper FastAPI: {e}")
                self.piper_process = None
        else:
            if process_manager.is_port_in_use(8880):
                process_manager.stop_server(port=8880, wait=2.0)
                self.log("🎤 Piper FastAPI detenido (puerto liberado)")

    # =================================================================
    # INICIO DE CHAT Y ROUTER CON OPTIMIZACIONES
    # =================================================================
    def start_selected_chat(self):
        selection = self.chat_listbox.curselection()
        if not selection:
            self.log("⚠️ Selecciona un modelo de Chat del listado")
            return
        idx = selection[0]
        try:
            model_name = self.chat_models[idx]
        except Exception:
            model_name = self.chat_listbox.get(idx)
        model_path = os.path.join(self.model_dir, model_name)
        self.stop_chat()
        gpu_for_chat = bool(self.defaults.get("gpu_for_chat", False))
        exe, gpu_requested = self._select_executable()
        if not os.path.exists(exe):
            exe = self.llama_exe
        if not os.path.exists(exe):
            self.log(f"❌ Ejecutable no encontrado: {exe}")
            return
        self.log(f"Chat → usando {'CUDA' if gpu_requested else 'CPU'} ({exe})")
        port = int(self.defaults.get("chat_port", 8081))
        host = str(self.defaults.get("host", "0.0.0.0"))
        cmd = [
            exe, "-m", model_path,
            "--host", host, "--port", str(port),
            "-c", str(self.defaults.get("c", 8192)),
            "--threads", str(self.defaults.get("threads_chat", 4)),
            "--temp", str(self.defaults.get("temp", 0.5)),
            "--repeat-penalty", str(self.defaults.get("repeat_penalty", 1.05))
        ]
        try:
            cmd.append("--cont-batching")
        except Exception:
            pass
        if self.defaults.get("webui_mcp_proxy", True):
            cmd.append("--webui-mcp-proxy")
        # Añadir mcp.json para OfficeCLI y otros servidores MCP
        mcp_config_path = os.path.join(self.data_dir, "mcp.json")
        if os.path.exists(mcp_config_path):
            cmd.extend(["--mcp-servers-config", mcp_config_path])
        # -ngl se inyecta SIEMPRE: 0 si el modelo va en CPU, valor configurado si va en GPU.
        ngl_value = self._normalize_ngl(self.defaults.get("n_gpu_layers"), "0")
        if not gpu_for_chat:
            ngl_value = "0"
        cmd.extend(["-ngl", ngl_value])

        # ---- OPTIMIZACIÓN, RENDIMIENTO, DSPARK Y MoE (lógica compartida) ----
        self._apply_llama_optimizations(cmd, model_name=model_name)
        self._log_llama_summary(ngl_value, model_name=model_name)

        # ---- PLANTILLA JINJA PERMANENTE ----
        cmd.append("--jinja")

        # ---- AÑADIR --agent y --no-ui SEGÚN CONFIGURACIÓN ----
        if self.defaults.get("use_agent", False):
            cmd.append("--agent")
        if self.defaults.get("no_ui", False):
            cmd.append("--no-ui")

        # ---- CONTINUAR CON EL RESTO DE LA LÓGICA ----
        try:
            cf = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') and os.name == 'nt' else 0
            self._model_ready = False
            try:
                self._update_tray_menu()
            except Exception:
                pass
            # mmproj: lógica COMPARTIDA con el panel web (`LlamaCommandMixin`).
            # Se carga solo si el usuario lo permite (`enable_multimodal`); con
            # mmproj activo, llama.cpp desactiva --cache-reuse y cada paso de
            # AGENCY/PLAN reprocesa el prompt desde el token 0.
            self.append_mmproj_args(cmd, model_name)
            # Ya no añadimos --agent aquí porque lo hemos hecho antes

            workspace_path = self.defaults.get("workspace_path", "agente_workspace")
            from pathlib import Path
            cwd = str(Path(workspace_path).resolve())
            self.log(f"📂 Estableciendo directorio de trabajo para llama-server: {cwd}")

            proc = process_manager.start_server(
                cmd,
                port=port,
                creationflags=cf,
                cwd=cwd
            )
            self.chat_process = proc
            try:
                self._current_model = model_name
            except Exception:
                pass
            self._monitor_process_output(proc, f"CHAT-{model_name}")  # type: ignore
            self.log(f"✅ Chat iniciado → {model_name} (Puerto {port}) {'(con GPU)' if gpu_for_chat else '(solo CPU)'}")
            self._write_status(model_name)

            try:
                try:
                    self._model_ready = False
                except Exception:
                    pass
                try:
                    self._update_tray_menu()
                except Exception:
                    pass
                host_val = str(self.defaults.get('host', '0.0.0.0'))
                port_val = int(self.defaults.get('chat_port', 8081))
                try:
                    mname = model_name or 'model'
                    if not hasattr(self, '_opened_viewers'):
                        self._opened_viewers = set()
                    if not hasattr(self, '_modal_opening'):
                        self._modal_opening = set()
                    if mname in self._opened_viewers:
                        pass
                    elif mname in self._modal_opening:
                        pass
                    else:
                        try:
                            self._modal_opening.add(mname)
                        except Exception:
                            pass
                        manager = self
                        threading.Thread(
                            target=lambda: manager._wait_for_model_and_open(mname, proc, webui_port=port_val),
                            daemon=True
                        ).start()
                except Exception:
                    pass
            except Exception:
                pass
            self.update_status()
        except Exception as e:
            self.log(f"❌ Error iniciando Chat: {e}")

    def start_router_mode(self):
        self.is_router_mode = True  # Marcar Router Mode
        try:
            self.stop_chat()
            gpu_for_chat = bool(self.defaults.get("gpu_for_chat", False))
            exe, gpu_requested = self._select_executable()
            if not os.path.exists(exe):
                exe = self.llama_exe
            if not os.path.exists(exe):
                self.log(f"❌ Ejecutable no encontrado: {exe}")
                return
            port = int(self.defaults.get("chat_port", 8081))
            host = str(self.defaults.get("host", "0.0.0.0"))
            cmd = [
                exe, "--models-dir", self.model_dir,
                "--host", host, "--port", str(port),
                "-c", str(self.defaults.get("c", 8192)),
                "--temp", str(self.defaults.get("temp", 0.5)),
                "--repeat-penalty", str(self.defaults.get("repeat_penalty", 1.05))
            ]
            try:
                cmd.append("--cont-batching")
            except Exception:
                pass
            if self.defaults.get("webui_mcp_proxy", True):
                cmd.append("--webui-mcp-proxy")
            # -ngl se inyecta SIEMPRE: 0 si el modelo va en CPU.
            ngl_value = self._normalize_ngl(self.defaults.get("n_gpu_layers"), "0")
            if not gpu_for_chat:
                ngl_value = "0"
            cmd.extend(["-ngl", ngl_value])

            # ---- OPTIMIZACIÓN Y RENDIMIENTO (el router no carga un modelo concreto) ----
            self._apply_llama_optimizations(
                cmd, model_name=None, include_speculation=False, include_moe=False
            )

            # ---- PLANTILLA JINJA PERMANENTE ----
            cmd.append("--jinja")

            # ---- AÑADIR --agent y --no-ui SEGÚN CONFIGURACIÓN ----
            if self.defaults.get("use_agent", False):
                cmd.append("--agent")
            if self.defaults.get("no_ui", False):
                cmd.append("--no-ui")

            # ---- CONTINUAR CON EL RESTO ----
            cf = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') and os.name == 'nt' else 0
            # Ya no añadimos --agent aquí porque lo hemos hecho antes
            workspace_path = self.defaults.get("workspace_path", "agente_workspace")
            from pathlib import Path
            cwd = str(Path(workspace_path).resolve())
            self.log(f"📂 Estableciendo directorio de trabajo para llama-server (router): {cwd}")

            proc = process_manager.start_server(
                cmd,
                port=port,
                creationflags=cf,
                cwd=cwd
            )
            self.chat_process = proc
            self._monitor_process_output(proc, f"CHAT-router")
            self.log(f"✅ Router Mode iniciado (Puerto {port}) {'(con GPU)' if gpu_requested else '(solo CPU)'}")
            self._write_status("router")
            self.update_status()
            # Router Mode abre WebUI solo si no se solicitó modo headless.
            if not self.defaults.get("no_ui", False):
                self._open_interface()
        except Exception as e:
            self.log(f"❌ Error iniciando Router Mode: {e}")
            self.is_router_mode = False

    def stop_chat(self):
        if self.chat_process:
            try:
                process_manager.stop_server(self.chat_process, port=int(self.defaults.get("chat_port", 8081)), wait=2.0)
            except Exception:
                pass
            self.chat_process = None
            try:
                if getattr(self, 'shared_state', None) is not None:
                    self.shared_state.update("server_status", "stopped")
            except Exception:
                pass
            try:
                self._current_model = None
            except Exception:
                pass
            self.log("Chat detenido")
            self._write_status("desconocido")
        if getattr(self, 'viewer_process', None):
            try:
                process_manager.close_process_gracefully(self.viewer_process, timeout=3.0)
            except Exception:
                pass
            self.viewer_process = None
        try:
            if hasattr(self, '_opened_viewers'):
                self._opened_viewers.clear()
        except Exception:
            pass
        self.is_router_mode = False  # Resetear flag
        self.update_status()

    def _wait_for_model_and_open(self, model_name, model_proc, webui_port=8080):
        state = {"win": None, "model_lbl": None, "progress": None}

        def create_modal():
            try:
                win = ctk.CTkToplevel(self)
                win.title("Cargando Modelo")
                win.geometry("540x120")
                win.transient(self)
                win.grab_set()
                ctk.CTkLabel(win, text="Cargando modelo, por favor espera...",
                             font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(10, 6))
                model_lbl = ctk.CTkLabel(win, text=f"Cargando modelo: {model_name}", anchor="w")
                model_lbl.pack(fill="x", padx=20)
                progress = ctk.CTkProgressBar(win, width=480)
                progress.pack(pady=12)
                progress.set(0.0)
                state.update({"win": win, "model_lbl": model_lbl, "progress": progress})
            except Exception as e:
                self.log(f"Error creando modal de carga (modelo): {e}")

        try:
            self.after(0, create_modal)
        except Exception:
            create_modal()

        def monitor():
            start_time = time.time()
            timeout = 300.0
            while True:
                m_ready = getattr(self, '_model_ready', False)

                def ui_update():
                    try:
                        elapsed = time.time() - start_time
                        base_fill = min(0.95, elapsed / 30.0)
                        prog = base_fill
                        if state.get('model_lbl'):
                            if m_ready:
                                state['model_lbl'].configure(text=f"Modelo cargado: {model_name}")  # type: ignore
                                prog = max(prog, 0.9)
                            else:
                                state['model_lbl'].configure(text=f"Cargando modelo: {model_name}")  # type: ignore
                        if state.get('progress'):
                            try:
                                state['progress'].set(prog)  # type: ignore
                            except Exception:
                                pass
                    except Exception:
                        pass

                try:
                    if state.get('win'):
                        self.after(0, ui_update)
                except Exception:
                    pass

                if m_ready:
                    def finish():
                        try:
                            if state.get('win'):
                                try:
                                    state['win'].grab_release()  # type: ignore
                                except Exception:
                                    pass
                                try:
                                    state['win'].destroy()  # type: ignore
                                except Exception:
                                    pass
                            if not self.defaults.get("no_ui", False):
                                self._open_interface()  # Usar nueva función
                            try:
                                if not hasattr(self, '_opened_viewers'):
                                    self._opened_viewers = set()
                                if model_name:
                                    self._opened_viewers.add(model_name)
                            except Exception:
                                pass
                            try:
                                if hasattr(self, '_modal_opening'):
                                    try:
                                        self._modal_opening.discard(model_name)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        except Exception:
                            pass

                    try:
                        self.after(200, finish)
                    except Exception:
                        finish()
                    break

                if time.time() - start_time > timeout:
                    def timeout_ui():
                        try:
                            if state.get('win'):
                                try:
                                    ctk.CTkButton(state['win'], text="Cerrar", command=state['win'].destroy).pack(pady=6)  # type: ignore
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    try:
                        if state.get('win'):
                            self.after(0, timeout_ui)
                    except Exception:
                        timeout_ui()
                    break
                time.sleep(0.4)

        threading.Thread(target=monitor, daemon=True).start()

    def reopen_webview(self):
        """Mantiene compatibilidad, ahora llama a _open_interface."""
        self._open_interface()

    def _write_status(self, model_name=None):
        try:
            status_path = os.path.join(runtime.get_user_data_dir(), "status.json")
            model = model_name or getattr(self, '_current_model', 'desconocido')
            data = {
                "model": model,
                "chat_port": self.defaults.get("chat_port", 8081),
                "embed_port": self.defaults.get("embed_port", 8082),
                "timestamp": time.time()
            }
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            # Compartir el modelo con el CLI vía SharedState (IPC)
            if getattr(self, 'shared_state', None) is not None:
                self.shared_state.update("current_model", model)
                self.shared_state.update("server_status", "running")
        except Exception:
            pass

    # ---------- WEB VIEWER ----------
    def _launch_web_viewer(self, url, profile_name='webui'):
        # Asegurar que el servidor RAG esté corriendo
        self.start_rag_service()

        profiles_dir = os.path.join(runtime.get_user_data_dir(), "webui_profiles")
        os.makedirs(profiles_dir, exist_ok=True)
        safe_name = ''.join(c if c.isalnum() else '_' for c in profile_name)
        profile_dir = os.path.join(profiles_dir, safe_name)
        env = os.environ.copy()
        env["WEBVIEW2_USER_DATA_FOLDER"] = profile_dir

        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, '--run-web-viewer', url, profile_dir]
        else:
            try:
                py_exe = str(runtime.get_python_exe())
            except Exception:
                import shutil
                py_sh = shutil.which("python") or shutil.which("python3")
                if py_sh:
                    py_exe = py_sh
                else:
                    self.log("Python no encontrado en PATH")
                    return
            web_viewer_script = runtime.resource_path("web_viewer.py")
            if not web_viewer_script or not os.path.exists(web_viewer_script):
                web_viewer_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "web_viewer.py")
            cmd = [py_exe, web_viewer_script, url, profile_dir]

        cf = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') and os.name == 'nt' else 0

        try:
            vp = process_manager.start_server(
                cmd,
                creationflags=cf,
                env=env,
                capture_output=False,
                cwd=runtime.get_base_dir()
            )
            self.viewer_process = vp
            self.log(f"🌐 Visor wxPython iniciado para {url} (perfil: {profile_dir})")
            threading.Thread(target=self._monitor_webview_process, daemon=True).start()
        except Exception as e:
            self.log(f"❌ Error iniciando visor wxPython: {e}")
            import webbrowser
            webbrowser.open_new_tab(url)
            self.log("🌐 Abriendo en navegador como fallback")

    def _open_viewer(self, host, port, profile_name):
        url = f"http://{host}:{port}"
        self._launch_web_viewer(url, profile_name)

    # ---------- NUEVOS MÉTODOS PARA INTERFAZ CLI ----------
    def _on_interface_preference_change(self, choice):
        """Actualiza la preferencia de interfaz."""
        self.interface_preference = choice
        self.defaults["interface_preference"] = choice
        self.save_config()
        self.log(f"Interfaz preferida cambiada a: {choice}")

    def _open_interface(self):
        """Abre la interfaz según la preferencia (respetando Router Mode)."""
        if not self.is_router_mode and not self._is_model_loaded():
            self._show_model_selection_dialog()
            return

        port = int(self.defaults.get("chat_port", 8081))
        if self.is_router_mode:
            self.log("Router Mode activo: abriendo WebUI")
            self._open_viewer('127.0.0.1', port, 'webui')
            return
        
        if self.defaults.get("interface_preference", "WebUI") == "CLI":
            if not self._is_model_loaded():
                self._show_model_selection_dialog()
                return
            self._launch_cli_in_terminal()
        else:
            self._open_viewer('127.0.0.1', port, 'webui')

    def _launch_cli_button(self):
        """Botón para abrir CLI en terminal (verifica modelo cargado)."""
        if not self._is_model_loaded():
            self._show_model_selection_dialog()
            return
        self._launch_cli_in_terminal()

    def _launch_cli_in_terminal(self):
        """Lanza el CLI en una terminal externa."""
        workspace = self.defaults.get("workspace_path", str(Path.cwd()))

        # Comando según modo (compilado vs desarrollo)
        if getattr(sys, 'frozen', False):
            # Compilado: lanzar .exe con --run-cli vía cmd para heredar consola de WT
            exe_path = sys.executable
            inner = subprocess.list2cmdline([exe_path, "--run-cli", "--workspace", workspace])
            base_cmd = f'chcp 65001 >nul && start "" /b /wait {inner}'
        else:
            # Desarrollo: ejecutar como módulo directamente con el intérprete del proyecto
            python_exe = str(runtime.get_python_exe())
            cmd = [python_exe, "-m", "tools.agent_cli", "--workspace", workspace]

        if os.name != 'nt':
            import shutil
            terminal = (shutil.which("x-terminal-emulator") or
                        shutil.which("gnome-terminal") or
                        shutil.which("konsole") or
                        shutil.which("alacritty") or
                        shutil.which("xfce4-terminal"))
            if terminal:
                if "gnome-terminal" in terminal:
                    subprocess.Popen([terminal, "--"] + cmd)
                else:
                    subprocess.Popen([terminal, "-e"] + cmd)
            else:
                subprocess.Popen(cmd)
            self.log(f"🌐 CLI lanzado (workspace: {workspace})")
            return

        # ─── Windows ───
        if getattr(sys, 'frozen', False):
            # Compilado: usar WT con cmd intermediario
            wt_path = self._find_windows_terminal()
            if wt_path:
                self.log(f"🌐 Usando Windows Terminal: {wt_path}")
                wt_cmd = [wt_path, "-w", "0", "nt", "cmd", "/k", base_cmd]
                try:
                    subprocess.Popen(wt_cmd, shell=False)
                    self.log("✅ Windows Terminal lanzado")
                    return
                except Exception as e:
                    self.log(f"⚠️ Fallo WT directo: {e}")
                try:
                    subprocess.Popen(subprocess.list2cmdline(wt_cmd), shell=True)
                    self.log("✅ Windows Terminal lanzado vía shell")
                    return
                except Exception as e2:
                    self.log(f"⚠️ Fallo WT vía shell: {e2}")

            # Fallback CMD propia
            self.log("⚠️ Fallback a consola CMD")
            cf = subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, 'CREATE_NEW_CONSOLE') else 0
            subprocess.Popen(["cmd", "/k", base_cmd], shell=False, creationflags=cf)
        else:
            # Desarrollo: WT directo (sin cmd intermediario)
            import shutil
            if shutil.which("wt"):
                subprocess.Popen(["wt", "-w", "0", "nt"] + cmd, shell=False)
            else:
                cf = subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, 'CREATE_NEW_CONSOLE') else 0
                subprocess.Popen(["cmd", "/k"] + cmd, shell=False, creationflags=cf)
            self.log(f"🌐 CLI lanzado en terminal (workspace: {workspace})")

    def _find_windows_terminal(self):
        import shutil, os, glob
        candidates = [
            shutil.which("wt"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"),
            os.path.expandvars(r"%ProgramFiles%\WindowsApps\Microsoft.WindowsTerminal_*\wt.exe"),
            r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminalPreview_*\wt.exe",
            r"C:\Users\*\AppData\Local\Microsoft\WindowsApps\wt.exe",
        ]
        for cand in candidates:
            if cand and os.path.exists(cand):
                return cand
            if cand and '*' in cand:
                matches = glob.glob(cand)
                if matches:
                    return matches[0]
        return None
    
    def _is_model_loaded(self) -> bool:
        try:
            resp = requests.get("http://localhost:8081/props", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    def _show_model_selection_dialog(self):
        try:
            win = ctk.CTkToplevel(self)
            win.title("Seleccionar modelo")
            win.geometry("560x420")
            win.transient(self)
            win.grab_set()

            ctk.CTkLabel(win, text="No hay modelo cargado en el puerto 8081.", wraplength=520).pack(padx=12, pady=(12, 6))
            ctk.CTkLabel(win, text="Selecciona un modelo para iniciar el chat o inicia en modo Router:", wraplength=520).pack(padx=12, pady=(0, 6))

            frame = ctk.CTkFrame(win)
            frame.pack(fill='both', expand=True, padx=10, pady=6)
            lb = tk.Listbox(frame, height=10, width=60)
            lb.pack(side='left', fill='both', expand=True)
            scrollbar = tk.Scrollbar(frame, orient='vertical', command=lb.yview)
            scrollbar.pack(side='right', fill='y')
            lb.config(yscrollcommand=scrollbar.set)

            models = getattr(self, 'chat_models', []) or []
            for m in models:
                lb.insert('end', m)

            btn_frame = ctk.CTkFrame(win)
            btn_frame.pack(fill='x', padx=10, pady=10)

            def _start_chat():
                try:
                    sel = lb.curselection()
                    if not sel:
                        messagebox.showwarning("Selecciona un modelo", "Selecciona un modelo para iniciar el chat.")
                        return
                    model_name = lb.get(sel[0])
                    idx = None
                    for i, m in enumerate(models):
                        if m == model_name or model_name in str(m):
                            idx = i
                            break
                    if idx is not None:
                        self.chat_listbox.selection_clear(0, tk.END)
                        self.chat_listbox.selection_set(idx)
                        self.chat_listbox.see(idx)
                    win.grab_release()
                    win.destroy()
                    self.after(100, self.start_selected_chat)
                except Exception as e:
                    self.log(f"Error iniciando chat desde diálogo: {e}")

            def _start_router():
                win.grab_release()
                win.destroy()
                self.after(100, self.start_router_mode)

            def _cancel():
                win.grab_release()
                win.destroy()

            ctk.CTkButton(btn_frame, text="Iniciar Chat", command=_start_chat).pack(side='left', padx=6)
            ctk.CTkButton(btn_frame, text="Iniciar Router Mode", fg_color="#2b6ef6", command=_start_router).pack(side='left', padx=6)
            ctk.CTkButton(btn_frame, text="Cancelar", command=_cancel).pack(side='right', padx=6)
        except Exception as e:
            self.log(f"Error mostrando diálogo de selección: {e}")

    # ---------- GESTOR DE HERRAMIENTAS ----------
    def open_tools_manager(self):
        self.log("ℹ️ Gestor de herramientas desactivado; la configuración se administra desde la WebUI.")
        return

        self.load_config()

        win = ctk.CTkToplevel(self)
        win.title("Gestor de Herramientas")
        win.geometry("600x500")
        win.transient(self)
        win.grab_set()

        main_frame = ctk.CTkFrame(win)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        scroll_frame = ctk.CTkScrollableFrame(main_frame, width=550, height=400)
        scroll_frame.pack(fill="both", expand=True)

        check_vars = {}

        tool_keys = [k for k in self.defaults.keys() if k.startswith("enable_") and isinstance(self.defaults[k], bool)]
        tool_keys.sort()

        for key in tool_keys:
            label = key.replace("enable_", "").replace("_", " ").title()
            var = tk.IntVar(value=1 if self.defaults.get(key, True) else 0)
            check_vars[key] = var
            chk = ctk.CTkCheckBox(scroll_frame, text=label, variable=var)
            chk.pack(anchor="w", padx=5, pady=2)

        btn_frame = ctk.CTkFrame(win)
        btn_frame.pack(fill="x", padx=10, pady=10)

        def save_tools():
            for key, var in check_vars.items():
                self.defaults[key] = bool(var.get())
            self.save_config()
            # Esperar un poco extra
            time.sleep(0.3)
            self.stop_dev_agent(wait=True)
            time.sleep(0.5)
            self.start_dev_agent()
            win.destroy()
            self.log("✅ Configuración de herramientas guardada y dev_agent reiniciado.")
        def cancel():
            win.destroy()

        ctk.CTkButton(btn_frame, text="Guardar y Reiniciar MCP", command=save_tools, fg_color="green").pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="Cancelar", command=cancel, fg_color="gray").pack(side="left", padx=10)

    # ---------- CONFIGURACIÓN AVANZADA ----------
    def open_advanced_config(self):
        try:
            win = ctk.CTkToplevel(self)
            win.title("Configuración Avanzada")
            win.geometry("920x900")
            win.minsize(840, 700)
            tabs = ctk.CTkTabview(win)
            tabs.pack(padx=12, pady=12, fill="both", expand=True)
            basic_tab = tabs.add("Básico")
            frame = ctk.CTkScrollableFrame(basic_tab)
            frame.pack(fill="both", expand=True, padx=4, pady=4)
            perf_tab_host = tabs.add("Rendimiento y Avanzado")
            # El contenido de esta pestaña no cabe en la ventana: frame con scroll.
            perf_scroll = ctk.CTkScrollableFrame(perf_tab_host)
            perf_scroll.pack(fill="both", expand=True)
            perf_tab = perf_scroll
            entries = {}
            tips = {}

            # Textos de ayuda (tooltip) por clave de configuración: comunes a las
            # pestañas Básico y Rendimiento y Avanzado.
            CONFIG_HELP = {
                # ── Básico ──
                "host": "Dirección de escucha del servidor. 0.0.0.0 acepta conexiones de otros equipos de la red; 127.0.0.1 solo permite acceso local.",
                "chat_port": "Puerto HTTP del servidor de chat. Cámbialo solo si colisiona con otro servicio (por defecto 8081).",
                "embed_port": "Puerto HTTP del servidor de embeddings (por defecto 8082). Debe ser distinto del puerto de chat.",
                "c": "Tamaño del contexto en tokens (-c). Más contexto = conversaciones y tareas más largas en memoria, pero mucho más RAM/VRAM.",
                "threads_chat": "Hilos de CPU para la generación del chat (-t). Un valor cercano al número de núcleos físicos suele ser óptimo; pasarse puede bajar el rendimiento.",
                "threads_embed": "Hilos de CPU para el servidor de embeddings (-t). Puede compartirlos con el chat si no trabajan a la vez.",
                "n_gpu_layers": "Capas del modelo que se cargan en GPU (-ngl). 'auto' decide según tu VRAM; 'all' sube todo; un número menor deja el resto en CPU.",
                "temp": "Temperatura de muestreo: baja (0.1-0.4) = respuestas más deterministas; alta (>0.8) = más creativas e impredecibles.",
                "repeat_penalty": "Penalización de repetición (1 = desactivada). 1.05-1.15 evita bucles sin coartar mucho el estilo; valores altos degradan la coherencia.",
                "top_p": "Top-p (nucleus sampling, --top-p): 0.95 = generalista; recorta la cola de tokens improbables sin perder variedad. 1.0 = desactivado. Evita combinarlo con un --top-k muy agresivo.",
                "top_k": "Top-k (--top-k): limita el muestreo a las N palabras más probables. 40 = generalista; 0 = desactivado (sin filtro, más diverso y más impredecible).",
                "webui_mcp_proxy": "Expone el proxy MCP en la WebUI para que el agente use sus herramientas desde el navegador.",
                "gpu_for_embeddings": "Carga el modelo de embeddings en GPU (por defecto ON): acelera el RAG a cambio de VRAM.",
                "gpu_for_chat": "Carga el modelo de chat en GPU (por defecto OFF): mucha más velocidad si tienes VRAM suficiente para el modelo y su contexto.",
                "use_agent": "Activa --agent en llama-server: herramientas integradas (archivos, código…) usables por AGENCY/PLAN.",
                "no_ui": "Oculta el visor embebido (--no-ui): útil en servidores headless o si solo usas el panel web.",
                "piper_enabled": "Servicio de voz Piper (TTS local). Consume ~200 MB de RAM; desactívalo si no usas salida de audio.",
                "automation_enabled": "Servicio de automatización 24/7 (puerto 15001): cola persistente, dispatchers (webhooks, cron, filewatch, IMAP), aprobaciones y auditoría. El manager lo arranca al iniciar y lo detiene al cerrarse — sin manager no hay modelo y los dispatchers no tendrían nada que ejecutar. Corre en su propio proceso, así que cerrar la web view no lo detiene; si el manager muere de golpe, el watchdog del servicio lo cierra.",
                "enable_multimodal": "Carga el proyector visual (mmproj) para procesar imágenes. Nota: desactiva --cache-reuse y ralentiza cada paso en CPU; desactívalo para tareas solo de texto.",
                "laya_enabled": "Clasificador ONNX local (~650 MB RAM, ~100× más rápido que una llamada LLM) que decide modo, naturaleza y dominio de cada tarea. Sin modelo, el orquestador usa las heurísticas léxicas.",
                "laya_preload": "Carga Laya en memoria al arrancar el manager; si no, se carga en la primera ruta (primer uso algo más lento).",
                # ── Rendimiento ──
                "batch_size": "Lógico de tokens procesados por pasada (-b). Más alto = más rapidez y más RAM. Recomendado: múltiplo del ubatch; en CPU, 512-2048 suele bastar.",
                "ubatch_size": "Tamaño físico del sub-lote (-ub): el batch lógico (-b) se procesa en trozos de este tamaño. 512 es un valor equilibrado; súbelo con GPU y memoria de sobra.",
                "parallel": "Solicitudes simultáneas (-np). 1 = secuencial (menos RAM; la KV cache no se reparte entre slots). Súbelo solo si atiendes varios clientes a la vez: cada slot consume memoria de contexto.",
                "ctx_checkpoints": "Puntos de control de contexto (0 = desactivado). Guarda/restaura estados de la KV cache para acelerar prefijos repetidos (AGENCY/PLAN). 32 = recomendado; 0 lo apaga.",
                "poll": "Intervalo de sondeo del servidor (--poll): cada cuánto comprueba trabajos/estado. Valores bajos reaccionan antes pero gastan más CPU.",
                "threads_http": "Hilos del servidor HTTP integrado. Súbelo solo con muchas peticiones concurrentes; con 1-2 clientes no aporta nada.",
                "timeout": "Tiempo máximo de espera por petición en segundos (-to). Con modelos lentos o contextos enormes, súbelo para evitar cortes prematuros.",
                "perf_enabled": "Activa --perf: el servidor registra tiempos por fase (prompt eval, generación…) para diagnosticar cuellos de botella. Impacto mínimo en rendimiento.",
                "reasoning_preserve": "Preserva la traza de razonamiento en TODO el historial (--reasoning-preserve), no solo en el último mensaje del asistente. Necesita una plantilla con soporte de preserve reasoning; si no lo tiene, llama.cpp lo ignora.",
                "reasoning_format": "Formato del razonamiento (--reasoning-format). none: deja los 'thoughts' dentro del contenido. deepseek: los extrae a message.reasoning_content (el formato que consume el agente). deepseek-legacy: mantiene las etiquetas <think> en el contenido y además rellena reasoning_content. auto: lo decide la plantilla del modelo y es el valor por defecto de llama-server, así que no se añade el flag. Solo se aplica si tu llama-server reconoce el flag.",
                "load_mode": "Modo de carga del modelo (-lm). mmap: mapea el archivo (arranque rápido, RAM bajo demanda). mlock: fija el modelo en RAM (estable, necesita memoria libre). dio: E/S directa (menos caché de disco).",
                "no_kv_offload": "Mantiene la caché KV en CPU (-nkvo) aunque haya GPU: libera VRAM del modelo a cambio de generación algo más lenta.",
                "dspark_enabled": "Decodificación especulativa: el modelo draft (DSpark) propone tokens y el principal los verifica en bloque. Más tokens/segundo si el draft acierta.",
                "gpu_for_dspark": "Carga el modelo draft en GPU (requiere DSpark activo). Libera CPU a cambio de consumir VRAM.",
                "n_gpu_layers_draft": "Capas del draft que se suben a GPU (-ngld). 'auto' decide solo; reduce el número si te quedas sin VRAM.",
                "spec_draft_n_max": "Máximo de tokens que el draft propone por paso (--spec-draft-n-max). Más = mayor aceleración potencial, pero más trabajo desperdiciado si falla.",
                "spec_draft_n_min": "Mínimo de tokens propuestos por el draft (--spec-draft-n-min). Muy bajo reduce la aceleración; muy alto aumenta los descartes.",
                "spec_draft_type_k": "Cuantización de la caché K del draft (-ctkd): q8_0/q4_0 ahorran memoria con pérdida mínima de precisión.",
                "spec_draft_type_v": "Cuantización de la caché V del draft (-ctvd), igual criterio que la K: cuantizar ahorra RAM/VRAM.",
                "spec_draft_threads": "Hilos CPU del modelo draft (-td). Vacío = automático; si compite con el modelo principal, dale menos hilos aquí.",
                "spec_draft_threads_batch": "Hilos para el procesado por lotes del draft (-tbd). Vacío = automático (mismo criterio que -td).",
                "moe_enabled": "Configuración específica para modelos Mezcla de Expertos (MoE): permite repartir los expertos entre CPU y GPU.",
                "gpu_for_moe": "Coloca los expertos MoE en GPU: más velocidad, pero los expertos suelen ocupar mucha VRAM.",
                "cpu_moe": "Fuerza TODOS los expertos a CPU (-cmoe). Útil cuando la atención del modelo cabe en GPU pero los expertos no.",
                "n_cpu_moe": "Expertos que permanecen en CPU (-ncmoe). 0 = todos en GPU; súbelo si se queda corta de VRAM.",
                "override_tensor": "Regla avanzada de tensores (-ot), p. ej. 'exps=CPU' para forzar un tensor a un dispositivo. Déjalo vacío salvo necesidad concreta.",
            }

            def _add_tip(widget, key):
                text = CONFIG_HELP.get(key)
                if text:
                    try:
                        tips[key] = PerfTooltip(widget, text)
                    except Exception:
                        pass

            def add_row(label, key, row):
                lbl = ctk.CTkLabel(frame, text=label)
                lbl.grid(row=row, column=0, sticky="w", padx=8, pady=6)
                ent = ctk.CTkEntry(frame)
                ent.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
                ent.insert(0, str(self.defaults.get(key)))
                _add_tip(lbl, key)
                _add_tip(ent, key)
                entries[key] = ent

            frame.grid_columnconfigure(1, weight=1)
            add_row("Host", "host", 0)
            add_row("Chat port", "chat_port", 1)
            add_row("Embed port", "embed_port", 2)
            add_row("Contexto (-c)", "c", 3)
            add_row("Threads Chat", "threads_chat", 4)
            add_row("Threads Embeds", "threads_embed", 5)
            add_row("n_gpu_layers", "n_gpu_layers", 6)
            add_row("Temp", "temp", 7)
            add_row("Repeat penalty", "repeat_penalty", 8)
            add_row("Top-p", "top_p", 9)
            add_row("Top-k", "top_k", 10)

            self.gpu_embed_var = tk.IntVar(value=1 if self.defaults.get("gpu_for_embeddings", True) else 0)
            self.gpu_chat_var = tk.IntVar(value=1 if self.defaults.get("gpu_for_chat", False) else 0)
            gpu_chk_e = ctk.CTkCheckBox(frame, text="GPU para Embeddings (por defecto ON)", variable=self.gpu_embed_var)
            gpu_chk_e.grid(row=12, column=0, columnspan=2, sticky="w", padx=8, pady=4)
            gpu_chk_c = ctk.CTkCheckBox(frame, text="GPU para Chat (por defecto OFF)", variable=self.gpu_chat_var)
            gpu_chk_c.grid(row=13, column=0, columnspan=2, sticky="w", padx=8, pady=4)
            _add_tip(gpu_chk_e, "gpu_for_embeddings")
            _add_tip(gpu_chk_c, "gpu_for_chat")

            webui_var = tk.IntVar(value=1 if self.defaults.get("webui_mcp_proxy", True) else 0)
            chk = ctk.CTkCheckBox(frame, text="Activar --webui-mcp-proxy", variable=webui_var)
            chk.grid(row=11, column=0, columnspan=2, sticky="w", padx=8, pady=8)
            _add_tip(chk, "webui_mcp_proxy")

            # NUEVOS CHECKBOXES: --agent y --no-ui
            self.agent_var = tk.IntVar(value=1 if self.defaults.get("use_agent", True) else 0)
            agent_chk = ctk.CTkCheckBox(frame, text="Activar --agent (herramientas integradas)", variable=self.agent_var)
            agent_chk.grid(row=15, column=0, columnspan=2, sticky="w", padx=8, pady=4)
            _add_tip(agent_chk, "use_agent")

            self.no_ui_var = tk.IntVar(value=1 if self.defaults.get("no_ui", False) else 0)
            no_ui_chk = ctk.CTkCheckBox(frame, text="Activar --no-ui (ocultar visor embebido)", variable=self.no_ui_var)
            no_ui_chk.grid(row=16, column=0, columnspan=2, sticky="w", padx=8, pady=4)
            _add_tip(no_ui_chk, "no_ui")

            self.piper_var = tk.IntVar(value=1 if self.defaults.get("piper_enabled", True) else 0)
            piper_chk = ctk.CTkCheckBox(
                frame,
                text="Activar servicio Piper (consume ~200 MB de RAM)",
                variable=self.piper_var,
            )
            piper_chk.grid(row=17, column=0, columnspan=2, sticky="w", padx=8, pady=4)
            _add_tip(piper_chk, "piper_enabled")

            # Servicio de automatización 24/7 (dispatchers, cola y webhooks).
            # Es la única "área de activación": el manager lo arranca al iniciar
            # y lo detiene al cerrarse, y sobrevive al cierre de la web view.
            self.automation_var = tk.IntVar(
                value=1 if self.defaults.get("automation_enabled", True) else 0
            )
            automation_chk = ctk.CTkCheckBox(
                frame,
                text="Activar servicio de automatización 24/7 (dispatchers, cola y webhooks)",
                variable=self.automation_var,
            )
            automation_chk.grid(row=19, column=0, columnspan=2, sticky="w", padx=8, pady=4)
            _add_tip(automation_chk, "automation_enabled")

            # ── Proyector visual (mmproj) ──────────────────────────────
            # Desactivar mmproj reactiva --cache-reuse: cada paso de
            # AGENCY/PLAN puede reutilizar el prefijo KV del paso anterior.
            self.multimodal_var = tk.IntVar(
                value=1 if self.defaults.get("enable_multimodal", True) else 0
            )
            multimodal_chk = ctk.CTkCheckBox(
                frame,
                text="Cargar proyector visual (mmproj)",
                variable=self.multimodal_var,
            )
            multimodal_chk.grid(row=18, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 2))
            _add_tip(multimodal_chk, "enable_multimodal")

            # ── Router Laya ONNX ──────────────────────────────────────
            # Clasificador de modo/naturaleza: sustituye la llamada LLM del
            # clasificación de tareas por una inferencia ONNX de ~650 MB, ~100×
            # más rápida. Opcional: sin modelo, el orquestador usa heurísticas.
            self.laya_var = tk.IntVar(
                value=1 if self.defaults.get("laya_enabled", True) else 0
            )
            laya_chk = ctk.CTkCheckBox(
                frame,
                text="Activar router Laya ONNX (~650 MB RAM, 100× más rápido)",
                variable=self.laya_var,
            )
            laya_chk.grid(row=20, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 2))
            _add_tip(laya_chk, "laya_enabled")

            self.laya_preload_var = tk.IntVar(
                value=1 if self.defaults.get("laya_preload", False) else 0
            )
            laya_preload_chk = ctk.CTkCheckBox(
                frame,
                text="Precargar Laya al arranque (si no, se carga en la primera ruta)",
                variable=self.laya_preload_var,
            )
            laya_preload_chk.grid(row=21, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))
            _add_tip(laya_preload_chk, "laya_preload")

            basic_actions = ctk.CTkFrame(frame)
            basic_actions.grid(row=22, column=0, columnspan=2, sticky="ew", padx=8, pady=(12, 8))
            basic_actions.grid_columnconfigure(0, weight=1)
            basic_actions.grid_columnconfigure(1, weight=1)
            ctk.CTkButton(
                basic_actions,
                text="Copiar mmproj al modelo seleccionado",
                command=self._copy_mmproj_to_model_dir_from_dialog,
                height=36,
            ).grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=8)
            startup_button = ctk.CTkButton(
                basic_actions,
                text=(
                    "Deshabilitar inicio con Windows"
                    if self._windows_task_installed()
                    else "Habilitar inicio con Windows"
                ),
                command=lambda: self._toggle_windows_task_wrapper(win, startup_button),
                height=36,
            )
            startup_button.grid(row=0, column=1, sticky="ew", padx=(4, 8), pady=8)

            # ================= PESTAÑA: RENDIMIENTO Y AVANZADO =================
            # Los textos de ayuda viven en CONFIG_HELP (definido arriba, común a
            # Básico y Rendimiento); _add_tip ya está disponible.
            perf_entries = {}
            perf_vars = {}
            perf_tab.grid_columnconfigure(1, weight=1)

            # Numerador automático de filas: las funciones suman sola, así que
            # insertar o reordenar opciones no exige retocar índices manuales.
            def _next_row() -> int:
                cur = perf_tab.grid_size()[1]
                return cur

            def add_perf_title(text, row=None):
                ctk.CTkLabel(perf_tab, text=text, font=ctk.CTkFont(weight="bold")).grid(
                    row=_next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=(10, 2)
                )

            def add_perf_row(label, key, row=None):
                lbl = ctk.CTkLabel(perf_tab, text=label)
                lbl.grid(row=_next_row(), column=0, sticky="w", padx=8, pady=4)
                ent = ctk.CTkEntry(perf_tab)
                ent.grid(row=_next_row(), column=1, sticky="ew", padx=8, pady=4)
                value = self.defaults.get(key)
                ent.insert(0, "" if value is None else str(value))
                _add_tip(lbl, key)
                _add_tip(ent, key)
                perf_entries[key] = ent
                return ent

            def add_perf_check(label, key, row=None, default=False):
                var = tk.IntVar(value=1 if self.defaults.get(key, default) else 0)
                chk = ctk.CTkCheckBox(perf_tab, text=label, variable=var)
                chk.grid(row=_next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=4)
                _add_tip(chk, key)
                perf_vars[key] = var
                return chk

            def add_perf_combo(label, key, row=None, options=None, default=None):
                lbl = ctk.CTkLabel(perf_tab, text=label)
                lbl.grid(row=_next_row(), column=0, sticky="w", padx=8, pady=4)
                var = tk.StringVar(value=str(self.defaults.get(key, default)))
                combo = ctk.CTkOptionMenu(perf_tab, values=options or [], variable=var)
                combo.grid(row=_next_row(), column=1, sticky="ew", padx=8, pady=4)
                _add_tip(lbl, key)
                _add_tip(combo, key)
                perf_vars[key] = var
                return combo

            add_perf_title("── Rendimiento ──")
            add_perf_row("Batch size (-b):", "batch_size")
            add_perf_row("Ubatch size (-ub):", "ubatch_size")
            add_perf_row("Parallel slots (-np):", "parallel")
            add_perf_row("Context checkpoints (0 = off, 32 = recomendado)", "ctx_checkpoints")
            add_perf_row("Poll (--poll):", "poll")
            add_perf_row("Threads HTTP:", "threads_http")
            add_perf_row("Timeout (-to):", "timeout")
            add_perf_check("--perf (diagnóstico)", "perf_enabled")
            add_perf_check("--reasoning-preserve", "reasoning_preserve")
            # Selector: llama-server acepta una lista cerrada de valores.
            add_perf_combo(
                "--reasoning-format:",
                "reasoning_format",
                options=list(REASONING_FORMATS),
                default=DEFAULT_REASONING_FORMAT,
            )

            add_perf_title("── Memoria ──")
            add_perf_combo("Load mode (-lm):", "load_mode", options=["auto", "mmap", "none", "mlock", "mmap+mlock", "dio"], default="auto")
            add_perf_check("--no-kv-offload (-nkvo)", "no_kv_offload")

            add_perf_title("── DSpark ──")
            dspark_chk = add_perf_check("Activar DSpark si está disponible", "dspark_enabled", default=True)
            gpu_dspark_chk = add_perf_check("GPU para DSpark", "gpu_for_dspark")
            ngld_entry = add_perf_row("Capas GPU draft (-ngld):", "n_gpu_layers_draft")
            add_perf_row("Máx. tokens draft (--spec-draft-n-max):", "spec_draft_n_max")
            add_perf_row("Mín. tokens draft (--spec-draft-n-min):", "spec_draft_n_min")
            ctkd_combo = add_perf_combo("Tipo K draft (-ctkd):", "spec_draft_type_k", options=["f16", "bf16", "q8_0", "q4_0"], default="f16")
            ctvd_combo = add_perf_combo("Tipo V draft (-ctvd):", "spec_draft_type_v", options=["f16", "bf16", "q8_0", "q4_0"], default="f16")
            add_perf_row("Hilos draft (-td):", "spec_draft_threads")
            add_perf_row("Hilos batch draft (-tbd):", "spec_draft_threads_batch")
            try:
                ctk.CTkButton(
                    perf_tab,
                    text="Copiar DSpark al modelo seleccionado",
                    command=self._copy_dspark_to_model_dir_from_dialog,
                ).grid(row=_next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=6)
            except Exception:
                pass

            add_perf_title("── MoE ──")
            moe_chk = add_perf_check("Activar configuración MoE", "moe_enabled")
            gpu_moe_chk = add_perf_check("GPU para expertos MoE", "gpu_for_moe")
            cpu_moe_chk = add_perf_check("Todos los expertos en CPU (-cmoe)", "cpu_moe")
            ncmoe_entry = add_perf_row("Capas MoE en CPU (-ncmoe):", "n_cpu_moe")
            ot_entry = add_perf_row("Override tensor (-ot):", "override_tensor")
            try:
                ctk.CTkButton(
                    perf_tab,
                    text="Detectar si el modelo es MoE",
                    command=self._detect_moe_from_dialog,
                ).grid(row=_next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=6)
            except Exception:
                pass

            def _refresh_perf_state(*_args):
                dspark_on = bool(perf_vars["dspark_enabled"].get())
                gpu_draft_on = dspark_on and bool(perf_vars["gpu_for_dspark"].get())
                for widget in (gpu_dspark_chk, ctkd_combo, ctvd_combo,
                               perf_entries["spec_draft_n_max"], perf_entries["spec_draft_n_min"],
                               perf_entries["spec_draft_threads"], perf_entries["spec_draft_threads_batch"]):
                    try:
                        widget.configure(state="normal" if dspark_on else "disabled")
                    except Exception:
                        pass
                # -ngld solo tiene efecto si el draft va en GPU
                try:
                    ngld_entry.configure(state="normal" if gpu_draft_on else "disabled")
                except Exception:
                    pass

                moe_on = bool(perf_vars["moe_enabled"].get())
                cpu_moe_on = bool(perf_vars["cpu_moe"].get())
                for widget in (gpu_moe_chk, cpu_moe_chk, ot_entry):
                    try:
                        widget.configure(state="normal" if moe_on else "disabled")
                    except Exception:
                        pass
                try:
                    ncmoe_entry.configure(state="normal" if (moe_on and not cpu_moe_on) else "disabled")
                except Exception:
                    pass
                if moe_on and cpu_moe_on:
                    try:
                        ncmoe_entry.delete(0, "end")
                        ncmoe_entry.insert(0, "0")
                    except Exception:
                        pass

            for key in ("dspark_enabled", "gpu_for_dspark", "moe_enabled", "gpu_for_moe", "cpu_moe"):
                try:
                    perf_vars[key].trace_add("write", _refresh_perf_state)
                except Exception:
                    pass
            try:
                _refresh_perf_state()
            except Exception:
                pass

            def _save():
                try:
                    for k, e in entries.items():
                        val = e.get()
                        if k == "n_gpu_layers":
                            # Admite entero, "auto" o "all".
                            self.defaults[k] = self._normalize_ngl(val, "auto")
                        elif k in ("chat_port", "embed_port", "c", "threads_chat", "threads_embed", "top_k"):
                            self.defaults[k] = int(val)
                        elif k in ("temp", "repeat_penalty", "top_p"):
                            self.defaults[k] = float(val)
                        else:
                            self.defaults[k] = val

                    # ---- Pestaña Rendimiento y Avanzado ----
                    for k in ("batch_size", "ubatch_size", "parallel", "ctx_checkpoints", "poll",
                              "threads_http", "timeout", "spec_draft_n_max",
                              "spec_draft_n_min", "n_cpu_moe"):
                        self.defaults[k] = self._safe_int(
                            perf_entries[k].get(), self.defaults.get(k)
                        )
                    self.defaults["n_gpu_layers_draft"] = self._normalize_ngl(
                        perf_entries["n_gpu_layers_draft"].get(), "auto"
                    )
                    for k in ("spec_draft_threads", "spec_draft_threads_batch"):
                        raw_threads = perf_entries[k].get().strip()
                        self.defaults[k] = self._safe_int(raw_threads, None) if raw_threads else None
                    self.defaults["override_tensor"] = perf_entries["override_tensor"].get().strip()
                    for k in ("load_mode", "spec_draft_type_k", "spec_draft_type_v",
                              "reasoning_format"):
                        self.defaults[k] = perf_vars[k].get()
                    for k in ("perf_enabled", "no_kv_offload", "reasoning_preserve",
                              "dspark_enabled",
                              "gpu_for_dspark", "moe_enabled", "gpu_for_moe", "cpu_moe"):
                        self.defaults[k] = bool(perf_vars[k].get())

                    self.defaults["webui_mcp_proxy"] = bool(webui_var.get())
                    self.defaults["gpu_for_embeddings"] = bool(self.gpu_embed_var.get())
                    self.defaults["gpu_for_chat"] = bool(self.gpu_chat_var.get())
                    # NUEVOS
                    self.defaults["use_agent"] = bool(self.agent_var.get())
                    self.defaults["no_ui"] = bool(self.no_ui_var.get())
                    self.defaults["enable_multimodal"] = bool(self.multimodal_var.get())
                    self.defaults["laya_enabled"] = bool(self.laya_var.get())
                    self.defaults["laya_preload"] = bool(self.laya_preload_var.get())
                    piper_enabled = bool(self.piper_var.get())
                    if piper_enabled:
                        available_ram = psutil.virtual_memory().available
                        required_ram = int(0.2 * 1024 ** 3)
                        if available_ram < required_ram:
                            messagebox.showwarning(
                                "RAM insuficiente",
                                "Piper requiere aproximadamente 200 MB de RAM disponible. Libera memoria antes de activarlo.",
                                parent=win,
                            )
                            return
                    self.defaults["piper_enabled"] = piper_enabled
                    self.defaults["automation_enabled"] = bool(self.automation_var.get())
                    self.save_config()
                    if piper_enabled:
                        if not (self.piper_process and self.piper_process.poll() is None):
                            self.start_piper_service()
                    else:
                        self.stop_piper_service()

                    # Automatización 24/7: si se activa aquí, arranca ya (en
                    # hilo aparte: cargar el dispatcher no debe congelar el
                    # diálogo); si se desactiva, se detiene y libera el puerto.
                    if self.defaults.get("automation_enabled", True):
                        threading.Thread(
                            target=self.start_automation_service, daemon=True
                        ).start()
                    else:
                        self.stop_automation_service()
                    self.log("Configuración avanzada guardada.")
                    win.destroy()
                    self.update_status()
                except Exception as e:
                    self.log(f"Error guardando configuración: {e}")

            ctk.CTkButton(win, text="Guardar y Cerrar", command=_save).pack(side="bottom", pady=8)
        except Exception as e:
            self.log(f"Error abriendo configuración avanzada: {e}")

    # ============================================================
    # GESTIÓN DE SERVICIOS (Windows y Linux)
    # ============================================================

    def _get_service_executable(self):
     """Retorna la ruta del ejecutable a usar para el servicio."""
     if getattr(sys, 'frozen', False):
        # Estamos en un ejecutable compilado por PyInstaller
        return sys.executable
     else:
        # En desarrollo, forzamos el intérprete del entorno virtual del proyecto.
        python_exe = runtime.get_python_exe()
        script = os.path.abspath(sys.argv[0])
        return f'"{python_exe}" "{script}"'

    def _run_elevated_windows(self, cmd_string=None, program=None, args=None):
        """
        Ejecuta un comando con privilegios de administrador en Windows.
        Puede usar cmd.exe /c (legacy) o un programa directo (preserva comillas).
        """
        try:
            if program and args:
                # Ejecutar programa directo (schtasks.exe) — preserva comillas en args
                result = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", program, args, None, 1
                )
            else:
                # Fallback: cmd.exe /c (legacy, no preserva comillas bien)
                result = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", "cmd.exe", f"/c {cmd_string}", None, 1
                )
            if result <= 32:
                raise Exception(f"ShellExecuteW devolvió {result}")
            return True
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo ejecutar con privilegios:\n{e}")
            return False

    def install_windows_task(self, parent_window=None):
        """Crea una tarea interactiva al iniciar sesión, sin almacenar credenciales."""
        if os.name != 'nt':
            messagebox.showinfo("Sistema no compatible", "Esta función solo está disponible en Windows.")
            return

        try:
            task_name = "LlamaServerManager"

            # Construir la lista de comandos que ejecutará la tarea
            if getattr(sys, 'frozen', False):
                cmd_parts = [sys.executable]
            else:
                py = runtime.get_python_exe()
                script = os.path.abspath(sys.argv[0])
                cmd_parts = [py, script]

            # list2cmdline pone comillas automáticamente donde hay espacios
            tr_quoted = subprocess.list2cmdline(cmd_parts)
            # schtasks necesita recibir las comillas LITERALMENTE dentro del valor /TR,
            # porque CommandLineToArgvW se las quita. Escapamos con \ para que lleguen.
            tr_inner = tr_quoted.replace('"', '\\"')

            args = f'/create /F /SC ONLOGON /RU SYSTEM /RL HIGHEST /TN "{task_name}" /TR "{tr_inner}"'
            self.log(f"Ejecutando schtasks con: {args}")
            success = self._run_elevated_windows(program="schtasks.exe", args=args)
            if success:
                messagebox.showinfo("Éxito", "Tarea programada creada correctamente.")
                self.log("Tarea programada creada correctamente.")
            else:
                messagebox.showerror("Error", "Fallo al crear la tarea programada.")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo crear la tarea programada:\n{e}")

    def uninstall_windows_task(self, parent_window=None):
        """Elimina la tarea programada en Windows usando schtasks /delete."""
        if os.name != 'nt':
            messagebox.showinfo("Sistema no compatible", "Esta función solo está disponible en Windows.")
            return

        try:
            task_name = "LlamaServerManager"
            args = f'/delete /F /tn "{task_name}"'
            self.log(f"Ejecutando schtasks con: {args}")
            success = self._run_elevated_windows(program="schtasks.exe", args=args)
            if success:
                messagebox.showinfo("Éxito", "Tarea programada eliminada correctamente.")
                self.log("Tarea programada eliminada correctamente.")
            else:
                messagebox.showerror("Error", "Fallo al eliminar la tarea programada.")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo eliminar la tarea programada:\n{e}")

    def install_linux_service(self):
     """Instala el servicio en Linux usando systemd."""
     if os.name == 'nt':
        return
     if not os.path.exists('/usr/bin/systemctl'):
        messagebox.showerror("Error", "systemd no encontrado. Este sistema no es compatible.")
        return

     exe_path = self._get_service_executable()
     service_name = "llama-server-manager"
     user = os.environ.get('USER', 'user')
     # Crear el contenido del archivo .service
     service_content = f"""[Unit]
    Description=Llama Server Manager Pro
    After=network.target

    [Service]
    ExecStart={exe_path} --service
    Restart=on-failure
    User={user}
    Environment="DISPLAY=:0"
    Environment="XAUTHORITY=/home/{user}/.Xauthority"
    WorkingDirectory={os.path.dirname(exe_path)}

    [Install]
    WantedBy=multi-user.target
    """
     import tempfile
     with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.service') as f:
        f.write(service_content)
        temp_path = f.name

     try:
        # Usamos pkexec para mover y habilitar
        subprocess.run(['pkexec', 'mv', temp_path, f'/etc/systemd/system/{service_name}.service'], check=True)
        subprocess.run(['pkexec', 'systemctl', 'daemon-reload'], check=True)
        subprocess.run(['pkexec', 'systemctl', 'enable', service_name], check=True)
        subprocess.run(['pkexec', 'systemctl', 'start', service_name], check=True)
        messagebox.showinfo("Éxito", "Servicio instalado e iniciado correctamente.")
        self.log("Servicio systemd instalado y habilitado.")
     except subprocess.CalledProcessError as e:
        messagebox.showerror("Error", f"Fallo al instalar el servicio:\n{e}")
     except Exception as e:
        messagebox.showerror("Error", f"Error inesperado:\n{e}")
     finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    def uninstall_linux_service(self):
     """Desinstala el servicio en Linux."""
     if os.name == 'nt':
        return
     service_name = "llama-server-manager"
     try:
        subprocess.run(['pkexec', 'systemctl', 'stop', service_name], check=False)
        subprocess.run(['pkexec', 'systemctl', 'disable', service_name], check=False)
        subprocess.run(['pkexec', 'rm', f'/etc/systemd/system/{service_name}.service'], check=True)
        subprocess.run(['pkexec', 'systemctl', 'daemon-reload'], check=True)
        messagebox.showinfo("Éxito", "Servicio desinstalado correctamente.")
        self.log("Servicio systemd eliminado.")
     except subprocess.CalledProcessError as e:
        messagebox.showerror("Error", f"Fallo al desinstalar el servicio:\n{e}")
     except Exception as e:
        messagebox.showerror("Error", f"Error inesperado:\n{e}")

    # ============================================================
    # WRAPPERS PARA EJECUCIÓN EN HILO (no bloquean la GUI)
    # ============================================================
    def _install_windows_task_wrapper(self, parent_win):
     threading.Thread(target=self.install_windows_task, args=(parent_win,), daemon=True).start()

    def _uninstall_windows_task_wrapper(self, parent_win):
     threading.Thread(target=self.uninstall_windows_task, args=(parent_win,), daemon=True).start()

    def _windows_task_installed(self):
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["schtasks.exe", "/Query", "/TN", "LlamaServerManager"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return result.returncode == 0
        except Exception:
            return False

    def _toggle_windows_task_wrapper(self, parent_win, button):
        def toggle():
            if self._windows_task_installed():
                self.uninstall_windows_task(parent_win)
            else:
                self.install_windows_task(parent_win)
            if parent_win and parent_win.winfo_exists():
                label = (
                    "Deshabilitar inicio con Windows"
                    if self._windows_task_installed()
                    else "Habilitar inicio con Windows"
                )
                parent_win.after(0, lambda: button.configure(text=label))

        threading.Thread(target=toggle, daemon=True).start()

    def _install_service_thread(self, parent_win):
     if os.name == 'nt':
        self.install_windows_task(parent_win)
     else:
        self.install_linux_service()
     if parent_win and parent_win.winfo_exists():
        parent_win.after(0, parent_win.lift)

    def save_config(self):
        """Persiste la configuración del manager en el directorio de usuario."""
        config_path = os.path.join(runtime.get_user_data_dir(), "config.json")
        temp_path = f"{config_path}.tmp"
        data = {
            "model_dir": self.model_dir,
            "embed_dir": self.embed_dir,
            "defaults": self.defaults,
        }
        try:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as config_file:
                json.dump(data, config_file, indent=2, ensure_ascii=False)
                config_file.flush()
                os.fsync(config_file.fileno())
            os.replace(temp_path, config_path)
            self.log(f"Configuración guardada en {config_path}")
        except Exception as error:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            self.log(f"Error guardando configuración: {error}")

    def load_config(self):
        config_path = os.path.join(runtime.get_user_data_dir(), "config.json")
        if not os.path.exists(config_path):
            config_path = "config.json" if os.path.exists("config.json") else None

        data = {}

        if config_path:
            try:
                with open(config_path, "r", encoding='utf-8') as f:
                    data = json.load(f)
                    self.model_dir = data.get("model_dir", self.model_dir)
                    self.embed_dir = data.get("embed_dir", self.embed_dir)
                    self.defaults.update(data.get("defaults", {}))
            except Exception:
                pass

        if "workspace_path" in data:
            self.defaults["workspace_path"] = data["workspace_path"]
        if "global_mode" in data:
            self.defaults["global_mode"] = data["global_mode"]
        if "auto_start_enabled" in data.get("defaults", {}):
            self.defaults["auto_start_enabled"] = data["defaults"]["auto_start_enabled"]
        if "auto_start_model" in data.get("defaults", {}):
            self.defaults["auto_start_model"] = data["defaults"]["auto_start_model"]

        # Razonamiento: el antiguo checkbox `reasoning_format_enabled` es ahora
        # el selector `reasoning_format` (la migración conserva su efecto).
        migrate_reasoning_format(self.defaults)

        # Migración mínima de n_gpu_layers: admite entero, "auto" o "all".
        try:
            raw_ngl = self.defaults.get("n_gpu_layers")
            if isinstance(raw_ngl, (int, float)) and not isinstance(raw_ngl, bool):
                if int(raw_ngl) == 0 and self.defaults.get("gpu_for_chat", False):
                    self.defaults["n_gpu_layers"] = "auto"
        except Exception:
            pass

        # Actualizar checkbox y combobox
        if hasattr(self, 'auto_start_var'):
            self.auto_start_var.set(1 if self.defaults.get("auto_start_enabled", False) else 0)
        if hasattr(self, 'auto_model_combo'):
            saved_model = self.defaults.get("auto_start_model", "")
            if saved_model == ROUTER_MODE_TOKEN:
                self.auto_model_combo.set("Router Mode")
            elif saved_model and saved_model in self.chat_models:
                self.auto_model_combo.set(saved_model)
            else:
                items = self.auto_model_combo.cget("values")
                if items:
                    self.auto_model_combo.set(items[0])

        # ===== NUEVO: Actualizar etiqueta de workspace =====
        if hasattr(self, 'workspace_label'):
            workspace_path = self.defaults.get("workspace_path", "")
            if workspace_path:
                self.workspace_label.configure(text=workspace_path)

        # ===== NUEVO: Actualizar checkbox de modo global =====
        if hasattr(self, 'global_mode_var'):
            self.global_mode_var.set(1 if self.defaults.get("global_mode", False) else 0)

        # ===== NUEVO: Actualizar selector de interfaz =====
        if hasattr(self, 'interface_selector'):
            pref = self.defaults.get("interface_preference", "WebUI")
            self.interface_selector.set(pref)

    def update_status(self):
        try:
            self.title(f"Llama Server Manager Pro — Chat:{self.defaults.get('chat_port')} Emb:{self.defaults.get('embed_port')}")
        except Exception:
            pass

    # ---------- PROCESOS Y LOGS ----------
    def _monitor_process_output(self, proc, tag: str):
        def _reader():
            if not proc or not getattr(proc, "stdout", None):
                return
            # Aceptar tanto bytes (Popen sin text=True, como los de process_manager)
            # como str (Popen con text=True, como dev_agent/workspace). Normalizamos
            # a str siempre antes de usarlo.
            while True:
                try:
                    raw = proc.stdout.readline()
                except Exception:
                    break
                if not raw:
                    break
                try:
                    line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
                    line = line.rstrip()
                    lower = line.lower()
                                        # Filtro para subprocesos verbosos: solo pasamos al log del manager
                    # las líneas que aportan valor (errores/warnings o prints con emoji
                    # del proyecto). Silencia el ruido INFO de FastMCP/uvicorn al arrancar.
                    QUIET_TAGS = {"DEVAGENT"}
                    if tag in QUIET_TAGS:
                        _PROJECT_MARKERS = ("✅", "❌", "⚠️", "🚀", "📁", "📦", "🔧", "🎤")
                        _ERROR_MARKERS = (
                            "error", "warning", "failed", "critical",
                            "traceback", "exception", "no se pudo", "no disponible",
                        )
                        has_marker = any(m in line for m in _PROJECT_MARKERS)
                        is_error = any(m in lower for m in _ERROR_MARKERS)
                        verbose = os.environ.get("DEVAGENT_VERBOSE", "").strip() not in ("", "0", "false", "False")
                        if not (verbose or has_marker or is_error):
                            continue
    
                    if tag.startswith("EMB-") and "nomic" in tag.lower():
                        if not getattr(self, '_nomic_in_progress_logged', False):
                            try:
                                self._nomic_in_progress_logged = True
                            except Exception:
                                pass
                            try:
                                self._enqueue_log('EMBED', f"{tag.split('-', 1)[1]} embeddings en proceso...")
                            except Exception:
                                try:
                                    self.logger.info(f"[EMBED] {tag.split('-', 1)[1]} embeddings en proceso...")
                                except Exception:
                                    pass
                        if any(k in lower for k in
                               ["listening", "ready", "server listening", "embeddings started",
                                "started server process", "model loaded", "listening on"]):
                            try:
                                self._embed_ready = True
                                self._embed_ready_name = tag.split('-', 1)[1] if '-' in tag else None
                            except Exception:
                                pass
                            if not getattr(self, '_nomic_ready_logged', False):
                                self._nomic_ready_logged = True
                                try:
                                    self._enqueue_log('EMBED', f"{tag.split('-', 1)[1]} embeddings listos")
                                except Exception:
                                    self.logger.info(f"[EMBED] {tag.split('-', 1)[1]} embeddings listos")
                        continue
    
                    try:
                        if f"[{tag}]" in line:
                            continue
                    except Exception:
                        pass
                    self._enqueue_log(tag, line)
    
                    if tag.startswith("CHAT-"):
                        if ("model loaded" in lower) or ("server is listening" in lower) or ("listening on" in lower):
                            mname = tag.split('-', 1)[1] if '-' in tag else None
                            self._model_ready = True
                            self._model_ready_name = mname
    
                except Exception:
                    # Un fallo en una línea NUNCA debe matar el reader ni silenciarse
                    # para siempre: si se repite, al menos veremos una pista.
                    try:
                        logger.debug(f"[{tag}] reader skip malformed line: {raw!r}")
                    except Exception:
                        pass
    
        t = threading.Thread(target=_reader, daemon=True)
        t.start()

    def _log_worker(self):
        try:
            while not getattr(self, '_stop_log_thread', False):
                try:
                    ts, tag, msg = self._log_queue.get(timeout=0.5)
                except Exception:
                    continue
                try:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        self.logger.info(f"[{tag}] {msg}")
                    except Exception:
                        print(f"[{timestamp}] [{tag}] {msg}")
                except Exception:
                    try:
                        print(f"[{tag}] {msg}")
                    except Exception:
                        pass
        except Exception:
            pass

    def _enqueue_log(self, tag: str, message: str):
        try:
            self._log_queue.put((time.time(), tag, message), block=False)
        except Exception:
            try:
                self.logger.info(f"[{tag}] {message}")
            except Exception:
                try:
                    print(f"[{tag}] {message}")
                except Exception:
                    pass

    def _reserve_port(self, port: int) -> bool:
        try:
            with self._reserved_lock:
                if port in self._reserved_ports:
                    return False
                if process_manager.is_port_in_use(port):
                    return False
                self._reserved_ports.add(port)
                return True
        except Exception:
            return False

    def _release_port(self, port: int):
        try:
            with self._reserved_lock:
                self._reserved_ports.discard(port)
        except Exception:
            pass

    # ---------- CIERRE ----------
    # Puertos que el manager debe dejar libres al cerrar. La duplicación de
    # AUTOMATION_PORT (15001) es intencional: si el entorno redefine la variable
    # seguimos cubriendo el valor por defecto.
    SHUTDOWN_PORTS = (15000, 8081, 8082, 25000, 5000, 5001, 8880, 19000, 15001)

    # Tiempo máximo que esperamos a que el cierre ordenado termine antes de
    # forzar la salida del ejecutable principal. Holgado respecto a la suma de
    # los timeouts de cada `stop_*` (~40 s en el peor caso).
    SHUTDOWN_GRACE_SECONDS = 60.0

    def _stop_all_services(self):
        """Detiene ordenadamente TODOS los procesos hijos gestionados por el manager.

        Es idempotente y seguro de invocar desde cualquier hilo (no toca la GUI).
        No cierra la ventana ni sale del proceso: de eso se encarga `on_close`.
        """
        # Orden de cierre: visor, chat, embeddings, MCP, workspace,
        # automatización 24/7, RAG, Piper, Config y Telegram.
        viewer = getattr(self, "viewer_process", None)
        if viewer:
            try:
                process_manager.close_process_gracefully(viewer, timeout=3.0)
            except Exception:
                pass
            self.viewer_process = None

        chat = getattr(self, "chat_process", None)
        if chat:
            try:
                process_manager.stop_server(chat, port=8081, wait=2.0)
            except Exception:
                pass
            self.chat_process = None

        embedding = getattr(self, "embedding_process", None)
        if embedding:
            try:
                process_manager.stop_server(embedding, port=8082, wait=2.5)
            except Exception:
                pass
            self.embedding_process = None

        # El servicio 24/7 es hijo del manager: se apaga con él (sin manager no
        # hay modelo, y sus dispatchers quedarían en el aire).
        stoppers = (
            ("stop_dev_agent", {"wait": True}),
            ("stop_officecli", {}),
            ("stop_workspace_server", {"wait": True}),
            ("stop_automation_service", {}),
            ("stop_rag_service", {"force": True}),
            ("stop_piper_service", {}),
        )
        for name, kwargs in stoppers:
            fn = getattr(self, name, None)
            if not callable(fn):
                continue
            try:
                fn(**kwargs)
            except Exception as e:
                try:
                    self.log(f"⚠️ Error en {name}: {e}")
                except Exception:
                    pass

        # Detener servidor de configuración
        try:
            from tools.config_server import stop_config_server
            stop_config_server()
        except Exception:
            pass

        # Detener servicio de Telegram
        try:
            from tools.telegram_service import stop_telegram_service_in_thread
            stop_telegram_service_in_thread(timeout=5.0)
        except Exception:
            pass

        # Detener el hilo de logs (sus mensajes ya no se escriben en la GUI)
        self._stop_log_thread = True
        log_thread = getattr(self, "_log_thread", None)
        if log_thread is not None:
            try:
                if log_thread.is_alive():
                    log_thread.join(timeout=1.0)
            except Exception:
                pass

        # Limpieza final de puertos: red de seguridad por si algún hijo quedó
        # huérfano (p. ej. un llama-server que no respondió al terminate).
        ports = list(self.SHUTDOWN_PORTS)
        if AUTOMATION_PORT not in ports:
            ports.append(AUTOMATION_PORT)
        for port in ports:
            try:
                if process_manager.is_port_in_use(port):
                    process_manager.stop_server(port=port, wait=1.0)
            except Exception:
                pass

    def _arm_shutdown_watchdog(self):
        """Red de seguridad: fuerza la salida del exe si el cierre ordenado se cuelga."""
        if getattr(self, "_shutdown_watchdog", False):
            return
        self._shutdown_watchdog = True

        def _watchdog():
            try:
                time.sleep(self.SHUTDOWN_GRACE_SECONDS)
            except Exception:
                return
            try:
                self.log("⚠️ El cierre excedió el tiempo límite; forzando salida.")
            except Exception:
                pass
            self._force_exit_process(code=1)

        try:
            threading.Thread(target=_watchdog, name="shutdown-watchdog", daemon=True).start()
        except Exception:
            pass

    def _force_exit_process(self, code=0):
        """Salida dura del ejecutable principal tras vaciar los buffers de salida.

        Se usa al FINAL de un cierre ordenado (y como red de seguridad del
        watchdog). Garantiza que el exe principal no se quede colgado con hilos
        no-daemon o el bucle de Tk aún vivo.
        """
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        try:
            import logging as _logging
            _logging.shutdown()
        except Exception:
            pass
        os._exit(code)

    def on_close(self):
        """Cierra la aplicación por completo: servicios, bandeja, GUI y proceso.

        Se invoca desde el botón de cerrar ventana (`WM_DELETE_WINDOW`), desde
        "Salir" de la bandeja, desde el handler de consola (CTRL_C/CTRL_CLOSE) y
        desde `atexit`. Es idempotente: las llamadas posteriores no repiten el
        cierre. Todo el trabajo de parada está acotado en el tiempo por
        `_arm_shutdown_watchdog`, de modo que el exe principal siempre termina.
        """
        if getattr(self, '_closing', False):
            return
        self._closing = True
        try:
            self.log("🛑 Cerrando aplicación, deteniendo todos los servicios...")
        except Exception:
            pass

        # Ocultar la ventana cuanto antes para dar feedback visual del cierre.
        try:
            self.withdraw()
        except Exception:
            pass

        # Red de seguridad desde el primer momento.
        self._arm_shutdown_watchdog()

        try:
            self._stop_all_services()
        except Exception as e:
            try:
                self.log(f"Error deteniendo servicios en on_close: {e}")
            except Exception:
                pass

        # Bandeja del sistema
        tray = getattr(self, "tray_icon", None)
        if tray is not None:
            try:
                tray.visible = False
            except Exception:
                pass
            try:
                tray.stop()
            except Exception:
                pass
            self.tray_icon = None

        try:
            self.log("✅ Aplicación cerrada correctamente.")
        except Exception:
            pass

        # Destruir la GUI (mejor esfuerzo; puede fallar si venimos de otro hilo).
        try:
            self.destroy()
        except Exception:
            pass

        # Salida definitiva del exe principal: sin hijos huérfanos ni procesos
        # zombis del propio manager.
        self._force_exit_process(code=0)

    # ---------- OTRAS UTILIDADES ----------
    # `_mmproj_args` ya NO se define aquí: vive en `LlamaCommandMixin`
    # (tools/llama_params.py) para que el panel web use exactamente la misma
    # lógica. El manager solo llama a `self.append_mmproj_args(cmd, model)`.
        #except Exception as e:
        #    try:
        #        self.log(f"Error determinando mmproj para {model_name_or_filename}: {e}")
        #    except Exception:
        #        pass
        #    return []

    def _copy_mmproj_to_model_dir_from_dialog(self):
        try:
            sel = self.chat_listbox.curselection()
            if not sel:
                self.log("⚠️ Selecciona un modelo en la lista antes de copiar mmproj")
                return
            try:
                model_name = self.chat_models[sel[0]]
            except Exception:
                model_name = self.chat_listbox.get(sel[0])
            src = filedialog.askopenfilename(title="Seleccionar archivo mmproj (.gguf)", filetypes=[("GGUF files", "*.gguf"), ("All files", "*.*")], initialdir=self.data_dir)
            if not src:
                return
            model_basename = os.path.splitext(os.path.basename(model_name))[0].lower()
            dest_dir = os.path.join(self.data_dir, "mmproj", model_basename)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            try:
                shutil.copy2(src, dest)
                self.log(f"✅ mmproj copiado a {dest}")
                try:
                    messagebox.showinfo("mmproj copiado", f"El mmproj se copió a:\n{dest}\n\nDebes reiniciar la carga del modelo (Detener e Iniciar Chat) para que el mmproj sea cargado.")
                except Exception:
                    pass
                try:
                    self.scan_and_separate_models()
                    try:
                        idx = self.chat_models.index(model_name)
                        self.chat_listbox.selection_clear(0, tk.END)
                        self.chat_listbox.selection_set(idx)
                        self.chat_listbox.see(idx)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception as e:
                self.log(f"❌ Error copiando mmproj: {e}")
                try:
                    messagebox.showerror("Error copiando mmproj", f"No se pudo copiar mmproj:\n{e}")
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Error en copia de mmproj: {e}")

    def _copy_dspark_to_model_dir_from_dialog(self):
        """Copia el .gguf del draft DSpark a la carpeta del modelo seleccionado."""
        try:
            sel = self.chat_listbox.curselection()
            if not sel:
                self.log("⚠️ Selecciona un modelo en la lista antes de copiar DSpark")
                return
            try:
                model_name = self.chat_models[sel[0]]
            except Exception:
                model_name = self.chat_listbox.get(sel[0])
            src = filedialog.askopenfilename(
                title="Seleccionar draft DSpark (.gguf)",
                filetypes=[("GGUF files", "*.gguf"), ("All files", "*.*")],
                initialdir=self.data_dir,
            )
            if not src:
                return
            model_basename = os.path.splitext(os.path.basename(model_name))[0].lower()
            dest_dir = self._dspark_dir(model_basename)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            try:
                shutil.copy2(src, dest)
                self.log(f"✅ DSpark copiado a {dest}")
                try:
                    messagebox.showinfo(
                        "DSpark copiado",
                        f"El draft DSpark se copió a:\n{dest}\n\nReinicia el modelo "
                        f"(Detener e Iniciar Chat) para que DSpark sea cargado.",
                    )
                except Exception:
                    pass
                try:
                    self.scan_and_separate_models()
                    try:
                        idx = self.chat_models.index(model_name)
                        self.chat_listbox.selection_clear(0, tk.END)
                        self.chat_listbox.selection_set(idx)
                        self.chat_listbox.see(idx)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception as e:
                self.log(f"❌ Error copiando DSpark: {e}")
                try:
                    messagebox.showerror("Error copiando DSpark", f"No se pudo copiar el draft:\n{e}")
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Error en copia de DSpark: {e}")

    def _detect_moe_from_dialog(self):
        """Informa si el modelo seleccionado parece MoE según su nombre."""
        try:
            sel = self.chat_listbox.curselection()
            if not sel:
                messagebox.showinfo("Detección MoE", "Selecciona un modelo en la lista primero.")
                return
            try:
                model_name = self.chat_models[sel[0]]
            except Exception:
                model_name = self.chat_listbox.get(sel[0])
            is_moe = self._is_moe_model(model_name)
            if is_moe:
                self.log(f"🧠 MoE: '{model_name}' parece un modelo MoE.")
                messagebox.showinfo(
                    "Detección MoE",
                    f"'{model_name}'\n\nParece un modelo MoE. Puedes activar la "
                    f"configuración MoE y usar -cmoe o -ncmoe.",
                )
            else:
                self.log(f"🧠 MoE: '{model_name}' no parece un modelo MoE.")
                messagebox.showinfo(
                    "Detección MoE",
                    f"'{model_name}'\n\nNo parece un modelo MoE. Los flags MoE no "
                    f"tendrán efecto si se activan.",
                )
        except Exception as e:
            self.log(f"Error detectando MoE: {e}")

    # ---------- REGISTRO DE SALIDA ----------
    def _register_exit_handlers(self):
        atexit.register(self._cleanup_on_exit)
        if os.name == 'nt':
            try:
                import win32api
                win32api.SetConsoleCtrlHandler(self._console_handler, True)
            except:
                pass
        else:
            signal.signal(signal.SIGTERM, self._signal_handler)
            signal.signal(signal.SIGINT, self._signal_handler)

    def _console_handler(self, event):
        if event in (0, 2):  # CTRL_CLOSE_EVENT, CTRL_C_EVENT
            self.on_close()
            return True
        return False

    def _signal_handler(self, signum, frame):
        self.on_close()
        sys.exit(0)

    def _cleanup_on_exit(self):
        try:
            self.on_close()
        except:
            pass


# ------------------------------------------------------------------
# SUBCOMMAND DISPATCHER
# ------------------------------------------------------------------
# Flags que NO deben interpretarse como "abrir la GUI".
KNOWN_STARTUP_FLAGS = ("--service",)


def _python_child_env() -> dict:
    """Entorno para subprocesos Python lanzados con este mismo ejecutable.

    Incluye el cwd (workspace) y la raíz del proyecto en PYTHONPATH para que
    `import tools...` y los módulos del workspace funcionen igual que en dev.
    """
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    paths = []
    for candidate in (os.getcwd(), getattr(sys, "_MEIPASS", None)):
        if candidate:
            paths.append(str(candidate))
    try:
        paths.append(str(runtime.get_base_dir()))
    except Exception:
        pass
    existing = env.get("PYTHONPATH", "")
    known = existing.split(os.pathsep) if existing else []
    ordered = [p for p in paths if p and p not in known]
    if ordered:
        env["PYTHONPATH"] = os.pathsep.join(ordered + known)
    return env


def _exec_user_code(code: str, filename: str = "<run-python-code>") -> int:
    """Ejecuta código Python con exec() en un namespace limpio.

    Redirigido a stdout/stderr del proceso actual: sin GUI, sin consola aparte.
    """
    import traceback
    workdir = os.getcwd()
    if workdir not in sys.path:
        sys.path.insert(0, workdir)
    namespace = {"__name__": "__main__", "__file__": filename}
    try:
        exec(compile(code, filename, "exec"), namespace)
        return 0
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code is None else 1
    except BaseException:
        traceback.print_exc()
        return 1


def _run_python_code_subcommand() -> int:
    """`--run-python-code`: lee el código de stdin (o de --code) y lo ejecuta."""
    code = ""
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            code = sys.stdin.read()
    except Exception:
        code = ""
    if not code.strip() and "--code" in sys.argv:
        try:
            code = sys.argv[sys.argv.index("--code") + 1]
        except Exception:
            code = ""
    if not code.strip():
        sys.stderr.write(
            "--run-python-code espera el código por stdin (recomendado) o con --code \"...\"\n"
        )
        return 2
    # Evitar que un stdout con encoding raro rompa la salida del hijo.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return _exec_user_code(code)


def _run_python_script_subcommand(args: list) -> int:
    """`--run-python-script <script.py> [args...]`: ejecuta un archivo .py."""
    if not args:
        sys.stderr.write("--run-python-script necesita la ruta de un script .py\n")
        return 2
    import runpy
    import traceback
    script = args[0]
    sys.argv = [script] + list(args[1:])
    workdir = os.getcwd()
    if workdir not in sys.path:
        sys.path.insert(0, workdir)
    try:
        runpy.run_path(script, run_name="__main__")
        return 0
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code is None else 1
    except BaseException:
        traceback.print_exc()
        return 1


def run_subcommand():
    import sys,io
    # En onefile los paquetes (p.ej. `tools`) viven en sys._MEIPASS como datos.
    # Sin esto, `from tools.workspace_server import main` falla y el proceso
    # hijo muere inmediatamente (síntoma: ERR_CONNECTION_REFUSED en 19000).
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and meipass not in sys.path:
        sys.path.insert(0, meipass)
    if len(sys.argv) < 2:
        return False
    cmd = sys.argv[1]
    if cmd == "--run-python-code":
        # Ejecuta código Python usando ESTE ejecutable como intérprete (modo
        # onefile). Así las tools no lanzan una segunda GUI del Manager.
        sys.exit(_run_python_code_subcommand())
    elif cmd == "--run-python-script":
        sys.exit(_run_python_script_subcommand(sys.argv[2:]))
    if cmd == "--run-web-viewer":
        from tools.web_viewer import main as web_viewer_main
        sys.exit(web_viewer_main())
    elif cmd == "--web-only":
        # Sin GUI no hay manager Tk: el propio panel web es el dueño del
        # servicio 24/7, así que lo arranca él (y su watchdog apunta a él).
        from tools.web_manager_server import start_web_server
        start_web_server(host="0.0.0.0", port=WEB_MANAGER_PORT, ensure_automation=True)
        sys.exit(0)
    elif cmd == "--run-mcp-server":
        # Se reutiliza el parser de tools/dev_agent.py para que las banderas del
        # arranque en desarrollo (`python -m tools.dev_agent`) y las de este
        # subcomando (onefile) no puedan desincronizarse.
        from tools.dev_agent import build_mcp_arg_parser, run_mcp_server
        mcp_args, _ = build_mcp_arg_parser().parse_known_args(sys.argv[2:])
        run_mcp_server(host=mcp_args.host, port=mcp_args.port, transport=mcp_args.transport)
        sys.exit(0)
    elif cmd == "--run-piper-server":
        # Buscar piper_service.py en el bundle (tools/) o, legacy, en app_data/piper/
        candidates: list[Path] = []
        try:
            candidates.append(Path(runtime.resource_path("tools", "piper_service.py")))
        except Exception:
            pass
        try:
            candidates.append(Path(runtime.get_base_dir()) / "tools" / "piper_service.py")
        except Exception:
            pass
        try:
            candidates.append(Path(runtime.get_data_dir()) / "piper" / "piper_service.py")
        except Exception:
            pass

        piper_script = next((p for p in candidates if p and p.exists()), None)
        if not piper_script:
            raise RuntimeError(
                "No se pudo encontrar piper_service.py. Buscado en: "
                + ", ".join(str(c) for c in candidates)
            )
    
        import runpy
        runpy.run_path(str(piper_script), run_name="__main__")
        sys.exit(0)
    elif cmd == "--run-rag-server":   # <-- NUEVO
        from tools.rag_web_server import main as rag_main
        rag_main()
        sys.exit(0)
    elif cmd == "--run-automation-service":
        # Servicio 24/7 (dispatchers, cola y webhooks). El manager lo arranca
        # con `--parent-pid`: el watchdog lo cierra si el manager desaparece.
        from tools.automation_service import main as automation_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        sys.exit(automation_main())
    elif cmd == "--run-workspace-server":
        from tools.workspace_server import main as workspace_main
        # argparse dentro de workspace_server debe ver SOLO sus propios args,
        # no el subcomando del Manager. Mismo patrón que --run-cli.
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        workspace_main()
        sys.exit(0)
    elif cmd == "--run-cli":
        os.environ['PYTHONIOENCODING'] = 'utf-8'
        os.environ.pop('TERM', None)

        if os.name == 'nt':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            if not kernel32.AttachConsole(-1):
                kernel32.AllocConsole()

            # Reconectar streams a CONOUT$/CONIN$ con UTF-8
            try:
                sys.stdin = open('CONIN$', 'r', encoding='utf-8', errors='replace')
            except Exception:
                sys.stdin = io.StringIO()
            try:
                sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='replace', buffering=1)
            except Exception:
                sys.stdout = open(os.devnull, 'w', encoding='utf-8')
            try:
                sys.stderr = open('CONOUT$', 'w', encoding='utf-8', errors='replace', buffering=1)
            except Exception:
                sys.stderr = open(os.devnull, 'w', encoding='utf-8')

            # Desactivar colorama (precargado por PyInstaller) antes de que rich lo use.
            # WT soporta ANSI nativamente; colorama solo rompe la codificación con cp1252.
            _fake = type(sys)('colorama')
            _fake.init = lambda *a, **k: None
            _fake.deinit = lambda: None
            _fake.reinit = lambda *a, **k: None
            _fake.just_fix_windows_console = lambda: None
            class _NoOpWrapper:
                def __init__(self, stream, **kw): self.stream = stream
                def write(self, t): return self.stream.write(t)
                def flush(self): return self.stream.flush()
            _fake.AnsiToWin32 = _NoOpWrapper
            _fake.ansitowin32 = type(sys)('colorama.ansitowin32')
            _fake.ansitowin32.AnsiToWin32 = _NoOpWrapper
            sys.modules['colorama'] = _fake
            sys.modules['colorama.ansitowin32'] = _fake.ansitowin32
            # Forzar rich a tratar esto como terminal real con colores
            os.environ['RICH_FORCE_TERMINAL'] = '1'

        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from tools.agent_cli import main as cli_main
        sys.exit(cli_main())
    else:
        return False
    return True

if __name__ == "__main__":
    if run_subcommand():
        pass
    elif len(sys.argv) > 1 and sys.argv[1].startswith("-") and sys.argv[1] not in KNOWN_STARTUP_FLAGS:
        # Guard: si nos invocan como intérprete (p.ej. `app.exe -c "..."`),
        # NO abrimos una segunda instancia de la GUI; avisamos y salimos.
        sys.stderr.write(
            f"❌ Argumento no reconocido: {sys.argv[1]}\n"
            "   Este ejecutable no actúa como intérprete Python por defecto.\n"
            "   Subcomandos: --run-python-code, --run-python-script <archivo.py>,\n"
            "   --run-cli, --run-mcp-server, --run-piper-server, --run-rag-server,\n"
            "   --run-workspace-server, --run-automation-service, --run-web-viewer, --web-only\n"
            "   (No se abre la GUI para evitar una segunda instancia.)\n"
        )
        sys.exit(2)
    elif not HAS_GUI:
        print(f"🖥️ Entorno sin GUI detectado. Abre http://127.0.0.1:{WEB_MANAGER_PORT}")
        from tools.web_manager_server import start_web_server
        start_web_server(host="0.0.0.0", port=WEB_MANAGER_PORT, ensure_automation=True)
    elif "--service" in sys.argv and os.name != 'nt':
        # Modo servicio: sin ventana, solo tray + procesos background
        import multiprocessing
        multiprocessing.freeze_support()
        import time

        # Crear instancia real pero evitando CTk.__init__ (sin ventana)
        class ServiceManager(LlamaServerManager):
            def __init__(self):
                # Saltar CTk.__init__ (no crear ventana)
                self._log_queue = queue.Queue()
                self._stop_log_thread = False
                self._reserved_ports = set()
                self._reserved_lock = threading.Lock()
                self.rag_server_process = None
                self.piper_process = None
                self.officecli_process = None
                self._closing = False
                self.is_tray_minimized = False

                # Rutas y defaults (copiar lógica del original)
                self.data_dir = runtime.get_data_dir()
                self.llama_exe = os.path.join(self.data_dir, "llama-cpp", "llama-server.exe")
                self.llama_cuda_exe = os.path.join(self.data_dir, "llama-cuda", "llama-server.exe")
                self.embed_dir = os.path.join(self.data_dir, "embed")
                self.model_dir = os.path.join(self.data_dir, "models")

                self.chat_process = None
                self.embedding_process = None
                self.dev_agent_process = None
                self.workspace_server_process = None
                self.viewer_process = None

                self.defaults = {
                    "host": "0.0.0.0", "chat_port": 8081, "embed_port": 8082,
                    "c": 16192, "threads_chat": 4, "threads_embed": 4,
                    "n_gpu_layers": 24, "temp": 0.5, "repeat_penalty": 1.05,
                    "webui_mcp_proxy": True, "use_agent": False, "no_ui": True,
                    "interface_preference": "WebUI",
                }
                self.is_router_mode = False

                # Logger básico
                self.logger = logmod.get_logger()
                logmod.attach_gui_callback(lambda line: print(line, flush=True))
                self._log_thread = threading.Thread(target=self._log_worker, daemon=True)
                self._log_thread.start()

                # Tray icon
                self.tray_icon = None
                self.icon_image = None
                self._load_tray_icon()

                # Cargar config real
                self.load_config()

                # Iniciar servicios y minimizar a bandeja inmediatamente
                self.log("🚀 Modo servicio headless iniciado")
                self._start_app_components()
                self.minimize_to_tray()

                # Registrar handlers de salida
                self._register_exit_handlers()

            def log(self, message):
                timestamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{timestamp}] {message}", flush=True)

            # Evitar que se cierre al destruir ventana (no hay ventana)
            def on_close(self):
                if getattr(self, '_closing', False):
                    return
                self._closing = True
                try:
                    self.log("🛑 Cerrando servicio...")
                except Exception:
                    pass
                # Misma parada ordenada y red de seguridad que la GUI.
                self._arm_shutdown_watchdog()
                try:
                    self._stop_all_services()
                except Exception as e:
                    try:
                        self.log(f"Error deteniendo servicios: {e}")
                    except Exception:
                        pass
                tray = getattr(self, 'tray_icon', None)
                if tray is not None:
                    try:
                        tray.visible = False
                    except Exception:
                        pass
                    try:
                        tray.stop()
                    except Exception:
                        pass
                    self.tray_icon = None
                try:
                    self.log("✅ Servicio detenido")
                except Exception:
                    pass
                self._force_exit_process(code=0)

            # Evitar métodos que requieren GUI
            def create_widgets(self): pass
            def scan_and_separate_models(self): pass
            def _on_window_unmap(self, event): pass
            def restore_from_tray(self):
                # Al restaurar desde tray en modo servicio, abrir GUI normal en nuevo proceso
                import subprocess
                subprocess.Popen([sys.executable], cwd=runtime.get_base_dir())
                self.log("🖥️ GUI lanzada en nueva instancia")

        # Ejecutar servicio
        service = ServiceManager()
        # Mantener vivo via tray icon (no via while True)
        try:
            while not service._closing:
                time.sleep(0.5)
        except KeyboardInterrupt:
            service.on_close()
    else:
        import multiprocessing
        multiprocessing.freeze_support()
        app = LlamaServerManager()
        app.mainloop()