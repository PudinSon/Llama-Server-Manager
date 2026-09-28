"""Servidor web ligero para controlar el Llama Server Manager.

El servidor usa el mismo controlador en GUI y en modo headless. Los logs se
sirven por SSE para no añadir una dependencia WebSocket al ejecutable.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, request, render_template_string

try:
    import tools.runtime as runtime
    import tools.process_manager as process_manager
except ImportError:
    import runtime  # type: ignore
    import process_manager  # type: ignore

try:
    from tools.llama_params import LLAMA_PERFORMANCE_DEFAULTS, LlamaCommandMixin
except ImportError:  # ejecución directa: python tools/web_manager_server.py
    from llama_params import LLAMA_PERFORMANCE_DEFAULTS, LlamaCommandMixin  # type: ignore


WEB_MANAGER_PORT = 12000
ROUTER_MODE_TOKEN = "__router__"
# dev_agent escucha aquí; el panel le reenvía las llamadas añadiendo el token.
AGENT_BASE_URL = "http://127.0.0.1:15000"
AGENT_PROXY_TIMEOUT = 60.0


def _agent_token() -> str:
    """Token de API compartido con dev_agent (vacío = auth desactivada).

    Se lee del proceso (env o gestor de configuración). El navegador NUNCA ve el
    token: el proxy del panel lo añade en el servidor.
    """
    try:
        from tools.auth import get_configured_token
        return (get_configured_token() or "").strip()
    except Exception:
        return (os.environ.get("LLAMA_WEBUI_TOKEN") or "").strip()

# Claves de configuración que necesitan coerción de tipo al recibirse por HTTP.
_INT_CONFIG_KEYS = (
    "chat_port", "embed_port", "c", "threads_chat", "threads_embed",
    "batch_size", "ubatch_size", "parallel", "poll", "threads_http",
    "timeout", "spec_draft_n_max", "spec_draft_n_min", "n_cpu_moe",
    "ctx_checkpoints",
)
_NGL_CONFIG_KEYS = ("n_gpu_layers", "n_gpu_layers_draft")
_FLOAT_CONFIG_KEYS = ("temp", "repeat_penalty")
_OPTIONAL_INT_CONFIG_KEYS = ("spec_draft_threads", "spec_draft_threads_batch")
_BOOL_CONFIG_KEYS = (
    "perf_enabled", "no_kv_offload", "dspark_enabled", "gpu_for_dspark",
    "moe_enabled", "gpu_for_moe", "cpu_moe", "webui_mcp_proxy",
    "gpu_for_embeddings", "gpu_for_chat", "use_agent", "no_ui", "piper_enabled",
    "laya_enabled", "laya_preload", "enable_multimodal",
)


class WebManagerState(LlamaCommandMixin):
    """Estado y operaciones de servicios para el panel web headless."""

    def __init__(self):
        self.data_dir = runtime.get_data_dir()
        self.model_dir = os.path.join(self.data_dir, "models")
        self.embed_dir = os.path.join(self.data_dir, "embed")
        self.llama_exe = os.path.join(self.data_dir, "llama-cpp", "llama-server.exe")
        self.llama_cuda_exe = os.path.join(self.data_dir, "llama-cuda", "llama-server.exe")
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
            "webui_mcp_proxy": True,
            "embed_chunk_size": 512,
            "embed_max_fragments": 3,
            # Única fuente de verdad del proyector visual (la lee el mixin
            # compartido). La antigua `use_mmproj` era una clave MUERTA: nadie la
            # leía, así que el panel web no cargaba mmproj nunca.
            "enable_multimodal": True,
            "mmproj_path": os.path.join(self.embed_dir, "mmproj.gguf"),
            "workspace_path": str(Path.cwd() / "agente_workspace"),
            "global_mode": False,
            "auto_start_model": "",
            "auto_start_enabled": False,
            "workspace_history": [],
            # Mismo criterio que el manager de escritorio: la agencia nativa
            # de llama-server duplica la de `agent_core` y ensucia los prompts.
            # El proxy MCP (`--webui-mcp-proxy`) es independiente y sigue ON.
            "use_agent": False,
            "no_ui": False,
            "piper_enabled": True,
            "laya_enabled": True,
            "laya_preload": False,
            "interface_preference": "WebUI",
            "gpu_for_chat": False,
            "gpu_for_embeddings": True,
            # ── Rendimiento, DSpark y MoE (mismos defaults que el manager de escritorio) ──
            **LLAMA_PERFORMANCE_DEFAULTS,
        }
        self._supported_flags = None
        self.model_info: list[dict] = []
        self.model_labels: dict[str, str] = {}
        self.processes: dict[str, Any] = {}
        self.log_queue: queue.Queue[dict] = queue.Queue()
        self._load_config()
        self._scan_models()

    def _load_config(self) -> None:
        path = Path(runtime.get_user_data_dir()) / "config.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.model_dir = data.get("model_dir", self.model_dir)
            self.embed_dir = data.get("embed_dir", self.embed_dir)
            self.defaults.update(data.get("defaults", {}))
        except FileNotFoundError:
            pass
        except Exception as error:
            self.log(f"Error cargando configuración: {error}", "ERROR")

        # Migración mínima de n_gpu_layers: admite entero, "auto" o "all".
        try:
            raw_ngl = self.defaults.get("n_gpu_layers")
            if isinstance(raw_ngl, (int, float)) and not isinstance(raw_ngl, bool):
                if int(raw_ngl) == 0 and self.defaults.get("gpu_for_chat", False):
                    self.defaults["n_gpu_layers"] = "auto"
        except Exception:
            pass

    def save_config(self) -> None:
        path = Path(runtime.get_user_data_dir()) / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"model_dir": self.model_dir, "embed_dir": self.embed_dir, "defaults": self.defaults}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def log(self, message: str, tag: str = "WEB") -> None:
        entry = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": tag, "message": str(message)}
        self.log_queue.put(entry)
        log_dir = Path(runtime.get_user_data_dir()) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "llama_manager.log").open("a", encoding="utf-8") as stream:
            stream.write(f"[{entry['timestamp']}] [{tag}] {entry['message']}\n")

    def _scan_models(self) -> list[str]:
        root = Path(self.model_dir)
        if not root.exists():
            self.models = []
            self.model_info = []
            self.model_labels = {}
            return self.models
        ignored = ("embed", "nomic", "jina", "bge", "text-matching")
        self.models = sorted(
            file.name for file in root.glob("*.gguf")
            if not any(token in file.name.lower() for token in ignored)
        )
        # Etiquetas informativas (DSpark / MoE) sin alterar el nombre real del archivo.
        labels: dict[str, str] = {}
        info: list[dict] = []
        for name in self.models:
            try:
                suffixes = self._model_suffixes(name)
            except Exception:
                suffixes = []
            label = f"{name} {' '.join(suffixes)}" if suffixes else name
            labels[name] = label
            try:
                info.append({
                    "name": name,
                    "label": label,
                    "dspark": self._dspark_available(name),
                    "moe": self._is_moe_model(name),
                })
            except Exception:
                info.append({"name": name, "label": label, "dspark": False, "moe": False})
        self.model_labels = labels
        self.model_info = info
        return self.models

    def _running(self, name: str) -> bool:
        process = self.processes.get(name)
        if process and process.poll() is None:
            return True
        ports = {"chat": 8081, "embeddings": 8082, "dev_agent": 15000, "rag": 25000,
                 "piper": 8880, "workspace": 19000}
        port = ports.get(name)
        return bool(port and process_manager.is_port_in_use(port))

    def status(self) -> dict:
        return {
            "chat": {"running": self._running("chat"), "port": self.defaults.get("chat_port", 8081)},
            "embeddings": {"running": self._running("embeddings"), "port": 8082},
            "dev_agent": {"running": self._running("dev_agent"), "port": 15000},
            "rag": {"running": self._running("rag"), "port": 25000},
            "piper": {"running": self._running("piper"), "port": 8880},
            "workspace": {"running": self._running("workspace"), "port": 19000},
            "models": {
                "available": self.models,
                "labels": self.model_labels,
                "info": self.model_info,
                "model_dir": self.model_dir,
                "embed_dir": self.embed_dir,
            },
            "agent_auth": bool(_agent_token()),
            "config": self.defaults,
        }

    def _start_process(self, name: str, command: list[str], cwd: Optional[str] = None) -> dict:
        if self._running(name):
            return {"success": True, "status": "already_running"}
        try:
            process = process_manager.start_server(command, port=None, cwd=cwd)
            self.processes[name] = process
            self.log(f"✅ {name} iniciado (PID {process.pid})")
            return {"success": True, "status": "started", "pid": process.pid}
        except Exception as error:
            self.log(f"❌ Error iniciando {name}: {error}", "ERROR")
            return {"success": False, "error": str(error)}

    def start_chat(self, model: str) -> dict:
        model_path = Path(self.model_dir) / model
        if not model_path.is_file():
            return {"success": False, "error": "Modelo no encontrado"}
        self.stop("chat")
        gpu_for_chat = bool(self.defaults.get("gpu_for_chat", False))
        executable, _ = self._select_executable()
        if not Path(executable).exists():
            executable = self.llama_exe
        if not Path(executable).exists():
            return {"success": False, "error": f"Ejecutable no encontrado: {executable}"}
        # -ngl se inyecta siempre: 0 si el modelo va en CPU.
        ngl_value = self._normalize_ngl(self.defaults.get("n_gpu_layers"), "0")
        if not gpu_for_chat:
            ngl_value = "0"
        command = [
            executable, "-m", str(model_path),
            "--host", str(self.defaults.get("host")),
            "--port", str(self.defaults.get("chat_port", 8081)),
            "-c", str(self.defaults.get("c", 16192)),
            "--threads", str(self.defaults.get("threads_chat", 4)),
            "--temp", str(self.defaults.get("temp", 0.5)),
            "--repeat-penalty", str(self.defaults.get("repeat_penalty", 1.05)),
            "--cont-batching",
            "-ngl", ngl_value,
        ]
        if self.defaults.get("webui_mcp_proxy", True):
            command.append("--webui-mcp-proxy")
        mcp_config = Path(self.data_dir) / "mcp.json"
        if mcp_config.exists():
            command.extend(["--mcp-servers-config", str(mcp_config)])
        # Optimización, rendimiento, DSpark y MoE (lógica compartida con el manager).
        self._apply_llama_optimizations(command, model_name=model)
        # mmproj: misma puerta que el manager de escritorio (`enable_multimodal`).
        self.append_mmproj_args(command, model)
        self._log_llama_summary(ngl_value, model_name=model)
        command.append("--jinja")
        if self.defaults.get("use_agent", False):
            command.append("--agent")
        if self.defaults.get("no_ui", False):
            command.append("--no-ui")
        return self._start_process("chat", command, str(Path(self.defaults.get("workspace_path", Path.cwd())).resolve()))

    def start_router(self) -> dict:
        self.stop("chat")
        gpu_for_chat = bool(self.defaults.get("gpu_for_chat", False))
        executable, _ = self._select_executable()
        if not Path(executable).exists():
            executable = self.llama_exe
        if not Path(executable).exists():
            return {"success": False, "error": f"Ejecutable no encontrado: {executable}"}
        # -ngl se inyecta siempre: 0 si el modelo va en CPU.
        ngl_value = self._normalize_ngl(self.defaults.get("n_gpu_layers"), "0")
        if not gpu_for_chat:
            ngl_value = "0"
        command = [
            executable, "--models-dir", self.model_dir,
            "--host", str(self.defaults.get("host")),
            "--port", str(self.defaults.get("chat_port", 8081)),
            "-c", str(self.defaults.get("c", 16192)),
            "--temp", str(self.defaults.get("temp", 0.5)),
            "--cont-batching",
            "-ngl", ngl_value,
        ]
        if self.defaults.get("webui_mcp_proxy", True):
            command.append("--webui-mcp-proxy")
        # El router no carga un modelo concreto: sin DSpark ni MoE.
        self._apply_llama_optimizations(
            command, model_name=None, include_speculation=False, include_moe=False
        )
        command.append("--jinja")
        if self.defaults.get("use_agent", False):
            command.append("--agent")
        if self.defaults.get("no_ui", False):
            command.append("--no-ui")
        return self._start_process("chat", command, str(Path(self.defaults.get("workspace_path", Path.cwd())).resolve()))

    def start_service(self, name: str) -> dict:
        if name == "embeddings":
            model_path = Path(self.embed_dir) / "nomic-embed-text-v1.5.f16.gguf"
            if not model_path.exists():
                return {"success": False, "error": f"Modelo de embeddings no encontrado: {model_path}"}
            executable = self.llama_cuda_exe if self.defaults.get("gpu_for_embeddings") else self.llama_exe
            if not Path(executable).exists():
                return {"success": False, "error": f"Ejecutable no encontrado: {executable}"}
            command = [executable, "-m", str(model_path), "--host", str(self.defaults.get("host")), "--port", "8082", "--threads", str(self.defaults.get("threads_embed", 4)), "--embeddings", "-c", "2048", "--no-ui"]
            if self.defaults.get("gpu_for_embeddings"):
                command.extend(["-ngl", self._normalize_ngl(self.defaults.get("n_gpu_layers"), "6")])
            return self._start_process(name, command, str(runtime.get_base_dir()))
        if name == "dev_agent":
            if getattr(__import__("sys"), "frozen", False):
                command = [__import__("sys").executable, "--run-mcp-server", "--transport", "http", "--port", "15000"]
            else:
                command = [str(runtime.get_python_exe()), "-m", "tools.dev_agent", "--transport", "http"]
            return self._start_process(name, command, str(runtime.get_base_dir()))
        if name == "workspace":
            if getattr(__import__("sys"), "frozen", False):
                command = [__import__("sys").executable, "--run-workspace-server",
                           "--host", "0.0.0.0", "--port", "19000"]
            else:
                command = [str(runtime.get_python_exe()), "-m", "tools.workspace_server",
                           "--host", "0.0.0.0", "--port", "19000"]
            return self._start_process(name, command, str(runtime.get_base_dir()))
        if name == "rag":
            if getattr(__import__("sys"), "frozen", False):
                command = [__import__("sys").executable, "--run-rag-server"]
            else:
                command = [str(runtime.get_python_exe()), str(Path(runtime.get_base_dir()) / "tools" / "rag_web_server.py")]
            return self._start_process(name, command, str(runtime.get_base_dir()))
        if name == "piper":
            if getattr(__import__("sys"), "frozen", False):
                command = [__import__("sys").executable, "--run-piper-server"]
            else:
                piper_script = Path(runtime.get_base_dir()) / "tools" / "piper_service.py"
                command = [str(runtime.get_python_exe()), str(piper_script)]
            os.environ["PIPER_MODEL_DIR"] = str(Path(self.data_dir) / "piper" / "voices")
            os.environ["LLAMA_MANAGER_DATA_DIR"] = str(self.data_dir)
            return self._start_process(name, command, str(runtime.get_base_dir()))
        return {"success": False, "error": f"Servicio no soportado: {name}"}

    def stop(self, name: str) -> None:
        process = self.processes.get(name)
        if process:
            try:
                process_manager.kill_process_tree(process.pid, timeout=3.0)
            except Exception:
                pass
            self.processes.pop(name, None)
            self.log(f"⏹ {name} detenido")
            return
        ports = {"chat": 8081, "embeddings": 8082, "dev_agent": 15000, "rag": 25000,
                 "piper": 8880, "workspace": 19000}
        port = ports.get(name)
        if port and process_manager.is_port_in_use(port):
            process_manager.stop_server(port=port, wait=2.0)
            self.log(f"⏹ {name} detenido por puerto")

    def set_models_folder(self, path: str) -> dict:
        """Cambia la carpeta de modelos (réplica del selector de la GUI)."""
        path = str(path or "").strip()
        if not path:
            return {"success": False, "error": "path requerido"}
        p = Path(path)
        if not p.is_dir():
            return {"success": False, "error": f"No es un directorio: {path}"}
        old = self.model_dir
        self.model_dir = str(p)
        self._scan_models()
        self.save_config()
        self.log(f"📁 Carpeta de modelos cambiada: {old} → {self.model_dir}")
        return {"success": True, "model_dir": self.model_dir}

    def set_interface(self, preference: str) -> dict:
        """Cambia la preferencia de interfaz (WebUI/CLI)."""
        preference = str(preference or "WebUI").strip()
        if preference not in ("WebUI", "CLI"):
            preference = "WebUI"
        self.defaults["interface_preference"] = preference
        self.save_config()
        self.log(f"💻 Interfaz preferida: {preference}")
        return {"success": True, "interface_preference": preference}

    def set_auto_start(self, enabled: bool, model_token: str) -> dict:
        """Guarda ajustes de auto-inicio (checkbox + modelo/router)."""
        self.defaults["auto_start_enabled"] = bool(enabled)
        self.defaults["auto_start_model"] = str(model_token or "")
        self.save_config()
        sel = "Router Mode" if model_token == ROUTER_MODE_TOKEN else model_token
        self.log(f"Auto-inicio {'activado' if enabled else 'desactivado'} para: {sel}")
        return {"success": True}

    def restart_dev_agent(self) -> None:
        """Reinicia dev_agent si está corriendo para aplicar cambios (workspace/global)."""
        try:
            if self._running("dev_agent"):
                self.stop("dev_agent")
                time.sleep(0.5)
                self.start_service("dev_agent")
        except Exception:
            pass

    def open_interface(self) -> dict:
        """Abre la interfaz del chat en el navegador (equivalente al visor de la GUI)."""
        port = int(self.defaults.get("chat_port", 8081))
        url = f"http://127.0.0.1:{port}"
        threading.Thread(target=lambda: _open_browser(url), daemon=True).start()
        self.log(f"🌐 Abriendo interfaz: {url}")
        return {"success": True, "url": url}

    def launch_cli(self) -> dict:
        """Lanza el CLI del agente en una terminal (réplica del botón Abrir CLI)."""
        workspace = str(self.defaults.get("workspace_path", str(Path.cwd())))
        if getattr(sys, "frozen", False):
            exe_path = sys.executable
            inner = subprocess.list2cmdline([exe_path, "--run-cli", "--workspace", workspace])
            base_cmd = f'chcp 65001 >nul && start "" /b /wait {inner}'
        else:
            python_exe = str(runtime.get_python_exe())
            cmd = [python_exe, "-m", "tools.agent_cli", "--workspace", workspace]
        if os.name != "nt":
            import shutil as _sh
            terminal = (_sh.which("x-terminal-emulator") or _sh.which("gnome-terminal") or
                        _sh.which("konsole") or _sh.which("alacritty") or _sh.which("xfce4-terminal"))
            if terminal:
                if "gnome-terminal" in terminal:
                    subprocess.Popen([terminal, "--"] + cmd)
                else:
                    subprocess.Popen([terminal, "-e"] + cmd)
            else:
                subprocess.Popen(cmd)
            self.log(f"🌐 CLI lanzado (workspace: {workspace})")
            return {"success": True}
        return self._launch_cli_windows(base_cmd if getattr(sys, "frozen", False) else cmd)

    def _launch_cli_windows(self, payload) -> dict:
        """Lanza el CLI en Windows (Windows Terminal o consola CMD)."""
        if isinstance(payload, str):
            base_cmd = payload
            if getattr(sys, "frozen", False):
                wt_path = self._find_windows_terminal()
                if wt_path:
                    wt_cmd = [wt_path, "-w", "0", "nt", "cmd", "/k", base_cmd]
                    try:
                        subprocess.Popen(wt_cmd, shell=False)
                        self.log("✅ Windows Terminal lanzado (CLI)")
                        return {"success": True}
                    except Exception:
                        pass
                    try:
                        subprocess.Popen(subprocess.list2cmdline(wt_cmd), shell=True)
                        self.log("✅ Windows Terminal lanzado vía shell (CLI)")
                        return {"success": True}
                    except Exception:
                        pass
                self.log("⚠️ Fallback a consola CMD (CLI)")
                cf = subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0
                subprocess.Popen(["cmd", "/k", base_cmd], shell=False, creationflags=cf)
            else:
                try:
                    subprocess.Popen(["cmd", "/k"] + base_cmd.split(), shell=False)
                except Exception:
                    subprocess.Popen(["cmd", "/k", base_cmd], shell=False, creationflags=0)
            self.log("🌐 CLI lanzado en terminal")
            return {"success": True}
        cmd = payload
        try:
            subprocess.Popen(["wt", "-w", "0", "nt"] + cmd, shell=False)
        except Exception:
            cf = subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0
            subprocess.Popen(["cmd", "/k"] + cmd, shell=False, creationflags=cf)
        self.log("🌐 CLI lanzado en terminal (workspace activo)")
        return {"success": True}

    def _find_windows_terminal(self) -> Optional[str]:
        import glob
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
            if cand and "*" in cand:
                matches = glob.glob(cand)
                if matches:
                    return matches[0]
        return None


def _open_browser(url: str) -> None:
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            pass


HTML = r"""\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Llama Server Manager Pro</title>
<style>
:root{--bg:#10141b;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#238636;--red:#da3633;--blue:#2b6ef6;--purple:#6c5ce7;--gray:#3d444d}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{margin:0;font-size:22px}
header .sub{color:var(--muted);font-size:13px}
.wrap{max-width:1150px;margin:0 auto;padding:20px 24px}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:16px}
.toolbar .muted-label{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:480px}
.spacer{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px}
.card h2{margin:0 0 10px;font-size:16px}
.card h3.sec{margin:0 0 6px;font-size:14px;color:#87ceeb}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;margin:8px 0;flex-wrap:wrap}
.svc{display:flex;gap:6px;align-items:center}
button{background:var(--green);color:#fff;border:0;border-radius:6px;padding:8px 12px;cursor:pointer;font-size:13px}
button.stop{background:var(--red)}
button.blue{background:var(--blue)}
button.purple{background:var(--purple)}
button.gray{background:var(--gray)}
button.small{padding:5px 10px;font-size:12px}
button:hover{filter:brightness(1.1)}
input,select{background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:13px;outline:none}
input:focus,select:focus{border-color:var(--blue)}
select#model-list{width:100%;min-height:150px}
select#auto-model{width:100%;max-width:230px}
.status{font-size:13px}
.badge{display:inline-flex;gap:6px;align-items:center;padding:3px 10px;border-radius:12px;font-size:12px}
.badge.on{background:rgba(35,134,54,.18);color:#3fb950;border:1px solid rgba(35,134,54,.5)}
.badge.off{background:rgba(218,54,51,.15);color:#f85149;border:1px solid rgba(218,54,51,.5)}
.dot{width:9px;height:9px;border-radius:50%;background:#f85149;display:inline-block}
.badge.on .dot{background:#3fb950;box-shadow:0 0 6px #3fb950}
.muted{color:var(--muted)}
.action-bar{display:flex;gap:10px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:16px;align-items:center}
.log-box{background:#0d1117;border:1px solid var(--border);border-radius:10px;padding:12px;height:340px;overflow-y:auto;font-family:Consolas,Monaco,monospace;font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all}
.modal{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.55);display:none;align-items:flex-start;justify-content:center;padding:36px 16px;z-index:50;overflow-y:auto}
.modal.open{display:flex}
.modal-box{background:var(--panel);border:1px solid var(--border);border-radius:12px;width:100%;max-width:760px;padding:20px 24px;max-height:86vh;overflow-y:auto}
.modal-box h2{margin:0 0 12px}
.adv-grid{display:grid;grid-template-columns:1fr 1fr;gap:2px 22px}
.adv-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)}
.adv-row label{font-size:13px}
.adv-row input{width:190px}
.chk-row{display:flex;align-items:center;padding:5px 0}
.check-label{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px}
.check-label input{width:auto}
.adv-row label[title],.check-label[title]{cursor:help}
.help-dot{display:inline-block;margin-left:6px;width:14px;height:14px;line-height:14px;text-align:center;border-radius:50%;background:rgba(110,118,129,.25);color:#8b949e;font-size:10px;cursor:help;user-select:none;flex:none}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.toast{position:fixed;bottom:24px;right:24px;background:#2ea043;color:#fff;padding:10px 16px;border-radius:8px;opacity:0;transition:opacity .3s;z-index:60;box-shadow:0 4px 14px rgba(0,0,0,.4);max-width:420px}
.toast.show{opacity:1}
.toast.err{background:var(--red)}
@media (max-width:640px){ .adv-grid{grid-template-columns:1fr} .help-dot{display:none} }
</style>
</head>
<body>
<header>
  <h1>🦙 Llama Server Manager Pro</h1>
  <span class="sub">Web Manager · Puerto 12000</span>
</header>
<div class="wrap">
  <div id="app"></div>
  <div class="card" style="margin-top:16px">
    <h2>📋 Logs (tiempo real)</h2>
    <div id="logs" class="log-box"></div>
  </div>
</div>

<div id="config-modal" class="modal">
  <div class="modal-box">
    <h2>🔧 Configuración Avanzada</h2>
    <div class="adv-grid" id="adv-grid"></div>
    <div id="adv-checks"></div>
    <div class="modal-actions">
      <button class="gray" type="button" data-action="close-config">Cancelar</button>
      <button type="button" data-action="save-config">Guardar y Cerrar</button>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>
<script>
var statusData = null;

function api(path, opt) {
  return fetch(path, opt).then(function (r) { return r.json().catch(function () { return {}; }); });
}
function esc(x) {
  if (x === null || x === undefined) return '';
  return String(x).replace(/[&<>"']/g, function (m) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
  });
}
function toast(msg, isErr) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  setTimeout(function () { t.className = 'toast'; }, 2600);
}

function buildShell() {
  var app = document.getElementById('app');
  var cfg = statusData && statusData.config ? statusData.config : {};
  var dir = statusData && statusData.models ? statusData.models.model_dir : '...';
  app.innerHTML =
    '<div class="toolbar">' +
      '<button data-action="models-folder">Cambiar Carpeta de Modelos</button>' +
      '<span id="dir-label" class="muted-label">Modelos: ' + esc(dir) + '</span>' +
      '<span class="spacer"></span>' +
      '<label class="check-label">Interfaz preferida: <select id="interface">' +
        '<option value="WebUI">WebUI</option><option value="CLI">CLI</option>' +
      '</select></label>' +
    '</div>' +
    '<div class="grid">' +
      '<div class="card">' +
        '<h2>🤖 Chat</h2>' +
        '<span id="chat-status" class="status">Cargando...</span>' +
        ' <span class="muted" id="chat-port"></span>' +
        '<h3 class="sec" style="margin-top:12px">Modelos de Chat</h3>' +
        '<select id="model-list" size="7"></select>' +
        '<div style="display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap">' +
          '<label class="check-label"><input type="checkbox" id="auto-start-check"> Auto-iniciar modelo al arrancar</label>' +
          '<select id="auto-model"></select>' +
        '</div>' +
        '<div class="row">' +
          '<button data-action="start-chat">Iniciar Chat</button>' +
          '<button class="blue" data-action="start-router">Router Mode</button>' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<h2>⚙️ Servicios</h2>' +
        '<div id="services">Cargando...</div>' +
      '</div>' +
      '<div class="card">' +
        '<h2>🧠 Router Laya ONNX</h2>' +
        '<div class="row"><span class="svc"><span class="badge off" id="laya-status">...</span></span>' +
        '<span>' +
        '<button class="small" data-action="laya-load">Cargar</button> ' +
        '<button class="stop small" data-action="laya-unload">Descargar</button>' +
        '</span></div>' +
        '<div class="muted">~650 MB en RAM. Si el modelo no está, el orquestador usa el router de Gemma.</div>' +
      '</div>' +
      '<div class="card">' +
        '<h2>📁 Workspace</h2>' +
        '<input id="workspace-input" value="' + (cfg.workspace_path ? esc(cfg.workspace_path) : '') + '" style="width:100%">' +
        '<div class="row"><span class="muted" id="model-dir-under"></span>' +
        '<button data-action="change-workspace">Guardar</button></div>' +
      '</div>' +
    '</div>' +
    '<div class="action-bar">' +
      '<button class="stop" data-action="stop-chat">Detener Chat</button>' +
      '<button class="blue" data-action="open-interface">🌐 Abrir Interfaz</button>' +
      '<button class="purple" data-action="launch-cli">💻 Abrir CLI</button>' +
      '<span class="spacer"></span>' +
      '<button class="gray" data-action="open-config">Configuración Avanzada</button>' +
    '</div>';
}

function serviceCards(list) {
  return list.map(function (n) {
    var s = statusData ? statusData[n] : null;
    var on = !!(s && s.running);
    var cls = on ? 'on' : 'off';
    var st = on ? 'activo' : 'detenido';
    return '<div class="row"><span class="svc"><span class="badge ' + cls + '"><span class="dot"></span>' + n + ' · ' + st + '</span></span>' +
      '<span>' +
      '<button class="small" data-action="start-service" data-service="' + n + '">Iniciar</button> ' +
      '<button class="stop small" data-action="stop-service" data-service="' + n + '">Detener</button>' +
      '</span></div>';
  }).join('');
}
</script>
<script>
function selectPreserve(sel, values, saved, labels) {
  var cur = sel.value;
  labels = labels || {};
  sel.innerHTML = values.map(function (m) {
    var label = labels[m] ? labels[m] : m;
    return '<option value="' + esc(m) + '">' + esc(label) + '</option>';
  }).join('');
  var pick = saved != null && saved !== '' ? saved : cur;
  var found = false;
  var i;
  for (i = 0; i < sel.options.length; i++) { if (sel.options[i].value === pick) { found = true; break; } }
  if (found) { sel.value = pick; }
  else if (sel.options.length) { sel.value = sel.options[0].value; }
}

// dev_agent (Laya y automatización) va detrás del proxy del panel: el token de
// API (LLAMA_WEBUI_TOKEN) lo añade el servidor, así el navegador nunca lo ve.
function agentUrl(path) {
  return '/api/agent' + path;
}

function refreshLaya() {
  var el = document.getElementById('laya-status');
  if (!el) { return; }
  fetch(agentUrl('/api/laya/status')).then(function (r) { return r.json(); }).then(function (d) {
    var state;
    if (d.error) { state = 'error'; }
    else if (!d.enabled) { state = 'desactivado'; }
    else if (d.loaded) { state = 'cargado'; }
    else if (d.installed) { state = 'pendiente'; }
    else { state = 'no instalado'; }
    el.textContent = state;
    el.className = 'badge ' + (d.loaded ? 'on' : 'off');
  }).catch(function () { el.textContent = 'sin conexión'; });
}

function refreshStatus() {
  api('/api/status').then(function (s) {
    statusData = s;
    var d = document;
    var cfg = s.config || {};
    var chatEl = d.getElementById('chat-status');
    if (chatEl) {
      chatEl.className = 'status badge ' + (s.chat.running ? 'on' : 'off');
      chatEl.innerHTML = '<span class="dot"></span>' + (s.chat.running ? 'Activo' : 'Detenido');
    }
    var portEl = d.getElementById('chat-port');
    if (portEl) { portEl.textContent = 'puerto ' + s.chat.port; }
    var svcEl = d.getElementById('services');
    if (svcEl) { svcEl.innerHTML = serviceCards(['embeddings', 'dev_agent', 'rag', 'piper']); }
    refreshLaya();
    var ml = d.getElementById('model-list');
    if (ml) { selectPreserve(ml, s.models.available || [], null, s.models.labels || {}); }
    var am = d.getElementById('auto-model');
    if (am) {
      var saved = cfg.auto_start_model;
      var savedLabel = (saved === '__router__') ? 'Router Mode' : saved;
      var items = ['Router Mode'].concat(s.models.available || []);
      selectPreserve(am, items, savedLabel, s.models.labels || {});
    }
    var asc = d.getElementById('auto-start-check');
    if (asc) { asc.checked = !!(cfg.auto_start_enabled); }
    var iface = d.getElementById('interface');
    if (iface) { iface.value = cfg.interface_preference || 'WebUI'; }
    var dl = d.getElementById('dir-label');
    if (dl) { dl.textContent = 'Modelos: ' + (s.models.model_dir || '...'); }
    var mdu = d.getElementById('model-dir-under');
    if (mdu) { mdu.textContent = s.models.model_dir || ''; }
    var wi = d.getElementById('workspace-input');
    if (wi && document.activeElement !== wi && cfg.workspace_path != null) { wi.value = cfg.workspace_path; }
  });
}

function startChat() {
  var ml = document.getElementById('model-list');
  var model = ml ? ml.value : '';
  if (!model) { toast('Selecciona un modelo', true); return; }
  api('/api/chat/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: model }) })
    .then(function (d) {
      if (d && d.error) { toast(d.error, true); } else { toast('Chat iniciando: ' + model); }
      refreshStatus();
    });
}

function changeModelsFolder() {
  var cur = statusData && statusData.models ? statusData.models.model_dir : '';
  var path = prompt('Ruta de la carpeta de modelos:', cur);
  if (!path) return;
  api('/api/models-folder', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: path }) })
    .then(function (d) {
      if (d && d.success) { toast('Carpeta de modelos cambiada'); } else { toast((d && d.error) || 'Error', true); }
      refreshStatus();
    });
}

function changeWorkspace() {
  var v = document.getElementById('workspace-input').value;
  api('/api/workspace', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: v }) })
    .then(function (d) {
      if (d && d.success) { toast('Workspace actualizado'); } else { toast((d && d.error) || 'Error', true); }
      refreshStatus();
    });
}

function saveInterface() {
  var v = document.getElementById('interface') ? document.getElementById('interface').value : 'WebUI';
  api('/api/interface', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ interface_preference: v }) })
    .then(function () { toast('Interfaz guardada: ' + v); });
}

function saveAutoStart() {
  var enabled = document.getElementById('auto-start-check') ? document.getElementById('auto-start-check').checked : false;
  var sel = document.getElementById('auto-model') ? document.getElementById('auto-model').value : '';
  var token = (sel === 'Router Mode') ? '__router__' : sel;
  api('/api/auto-start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ auto_start_enabled: enabled, auto_start_model: token }) })
    .then(function () { toast('Auto-inicio guardado'); });
}
</script>
<script>
// Textos de ayuda (tooltip) por clave de configuración: espejo de CONFIG_HELP
// en Llama_Server_Manager.py (pestañas Básico y Rendimiento y Avanzado del diálogo Tk).
var PERF_HELP_TIPS = {
  // ── Básico ──
  host: "Dirección de escucha del servidor. 0.0.0.0 acepta conexiones de otros equipos de la red; 127.0.0.1 solo permite acceso local.",
  chat_port: "Puerto HTTP del servidor de chat. Cámbialo solo si colisiona con otro servicio (por defecto 8081).",
  embed_port: "Puerto HTTP del servidor de embeddings (por defecto 8082). Debe ser distinto del puerto de chat.",
  c: "Tamaño del contexto en tokens (-c). Más contexto = conversaciones y tareas más largas en memoria, pero mucho más RAM/VRAM.",
  threads_chat: "Hilos de CPU para la generación del chat (-t). Un valor cercano al número de núcleos físicos suele ser óptimo; pasarse puede bajar el rendimiento.",
  threads_embed: "Hilos de CPU para el servidor de embeddings (-t). Puede compartirlos con el chat si no trabajan a la vez.",
  n_gpu_layers: "Capas del modelo que se cargan en GPU (-ngl). 'auto' decide según tu VRAM; 'all' sube todo; un número menor deja el resto en CPU.",
  temp: "Temperatura de muestreo: baja (0.1-0.4) = respuestas más deterministas; alta (>0.8) = más creativas e impredecibles.",
  repeat_penalty: "Penalización de repetición (1 = desactivada). 1.05-1.15 evita bucles sin coartar mucho el estilo; valores altos degradan la coherencia.",
  webui_mcp_proxy: "Expone el proxy MCP en la WebUI para que el agente use sus herramientas desde el navegador.",
  gpu_for_embeddings: "Carga el modelo de embeddings en GPU (por defecto ON): acelera el RAG a cambio de VRAM.",
  gpu_for_chat: "Carga el modelo de chat en GPU (por defecto OFF): mucha más velocidad si tienes VRAM suficiente para el modelo y su contexto.",
  use_agent: "Activa --agent en llama-server: herramientas integradas (archivos, código…) usables por AGENCY/PLAN.",
  no_ui: "Oculta el visor embebido (--no-ui): útil en servidores headless o si solo usas el panel web.",
  piper_enabled: "Servicio de voz Piper (TTS local). Consume ~200 MB de RAM; desactívalo si no usas salida de audio.",
  enable_multimodal: "Carga el proyector visual (mmproj) para procesar imágenes. Nota: desactiva --cache-reuse y ralentiza cada paso en CPU; desactívalo para tareas solo de texto.",
  laya_enabled: "Sustituye la llamada LLM del ToolRouter por un clasificador ONNX (~650 MB RAM, ~100× más rápido). Sin modelo, el orquestador usa Gemma.",
  laya_preload: "Carga Laya en memoria al arrancar el manager; si no, se carga en la primera ruta (primer uso algo más lento).",
  // ── Rendimiento ──
  batch_size: "Lógico de tokens procesados por pasada (-b). Más alto = más rapidez y más RAM. Recomendado: múltiplo del ubatch; en CPU, 512-2048 suele bastar.",
  ubatch_size: "Tamaño físico del sub-lote (-ub): el batch lógico (-b) se procesa en trozos de este tamaño. 512 es un valor equilibrado; súbelo con GPU y memoria de sobra.",
  parallel: "Solicitudes simultáneas (-np). 1 = secuencial (menos RAM; la KV cache no se reparte entre slots). Súbelo solo si atiendes varios clientes a la vez: cada slot consume memoria de contexto.",
  ctx_checkpoints: "Puntos de control de contexto (0 = desactivado). Guarda/restaura estados de la KV cache para acelerar prefijos repetidos (AGENCY/PLAN). 32 = recomendado; 0 lo apaga.",
  poll: "Intervalo de sondeo del servidor (--poll): cada cuánto comprueba trabajos/estado. Valores bajos reaccionan antes pero gastan más CPU.",
  threads_http: "Hilos del servidor HTTP integrado. Súbelo solo con muchas peticiones concurrentes; con 1-2 clientes no aporta nada.",
  timeout: "Tiempo máximo de espera por petición en segundos (-to). Con modelos lentos o contextos enormes, súbelo para evitar cortes prematuros.",
  perf_enabled: "Activa --perf: el servidor registra tiempos por fase (prompt eval, generación…) para diagnosticar cuellos de botella. Impacto mínimo en rendimiento.",
  load_mode: "Modo de carga del modelo (-lm). mmap: mapea el archivo (arranque rápido, RAM bajo demanda). mlock: fija el modelo en RAM (estable, necesita memoria libre). dio: E/S directa (menos caché de disco).",
  no_kv_offload: "Mantiene la caché KV en CPU (-nkvo) aunque haya GPU: libera VRAM del modelo a cambio de generación algo más lenta.",
  dspark_enabled: "Decodificación especulativa: el modelo draft (DSpark) propone tokens y el principal los verifica en bloque. Más tokens/segundo si el draft acierta.",
  gpu_for_dspark: "Carga el modelo draft en GPU (requiere DSpark activo). Libera CPU a cambio de consumir VRAM.",
  n_gpu_layers_draft: "Capas del draft que se suben a GPU (-ngld). 'auto' decide solo; reduce el número si te quedas sin VRAM.",
  spec_draft_n_max: "Máximo de tokens que el draft propone por paso (--spec-draft-n-max). Más = mayor aceleración potencial, pero más trabajo desperdiciado si falla.",
  spec_draft_n_min: "Mínimo de tokens propuestos por el draft (--spec-draft-n-min). Muy bajo reduce la aceleración; muy alto aumenta los descartes.",
  spec_draft_type_k: "Cuantización de la caché K del draft (-ctkd): q8_0/q4_0 ahorran memoria con pérdida mínima de precisión.",
  spec_draft_type_v: "Cuantización de la caché V del draft (-ctvd), igual criterio que la K: cuantizar ahorra RAM/VRAM.",
  spec_draft_threads: "Hilos CPU del modelo draft (-td). Vacío = automático; si compite con el modelo principal, dale menos hilos aquí.",
  spec_draft_threads_batch: "Hilos para el procesado por lotes del draft (-tbd). Vacío = automático (mismo criterio que -td).",
  moe_enabled: "Configuración específica para modelos Mezcla de Expertos (MoE): permite repartir los expertos entre CPU y GPU.",
  gpu_for_moe: "Coloca los expertos MoE en GPU: más velocidad, pero los expertos suelen ocupar mucha VRAM.",
  cpu_moe: "Fuerza TODOS los expertos a CPU (-cmoe). Útil cuando la atención del modelo cabe en GPU pero los expertos no.",
  n_cpu_moe: "Expertos que permanecen en CPU (-ncmoe). 0 = todos en GPU; súbelo si se queda corta de VRAM.",
  override_tensor: "Regla avanzada de tensores (-ot), p. ej. 'exps=CPU' para forzar un tensor a un dispositivo. Déjalo vacío salvo necesidad concreta."
};

var CFG_FIELDS = [
  ['Host', 'host'], ['Chat port', 'chat_port'], ['Embed port', 'embed_port'],
  ['Contexto (-c)', 'c'], ['Threads Chat', 'threads_chat'], ['Threads Embeds', 'threads_embed'],
  ['n_gpu_layers (auto/all/N)', 'n_gpu_layers'], ['Temp', 'temp'], ['Repeat penalty', 'repeat_penalty'],
  // Rendimiento
  ['Batch size (-b)', 'batch_size'], ['Ubatch size (-ub)', 'ubatch_size'],
  ['Parallel slots (-np)', 'parallel'], ['Context checkpoints (0=off, 32=recomendado)', 'ctx_checkpoints'],
  ['Poll (--poll)', 'poll'],
  ['Threads HTTP', 'threads_http'], ['Timeout (-to)', 'timeout'],
  ['Load mode (-lm: auto/mmap/mlock/mmap+mlock/dio)', 'load_mode'],
  // DSpark
  ['Capas GPU draft (-ngld: auto/all/N)', 'n_gpu_layers_draft'],
  ['Máx. tokens draft (--spec-draft-n-max)', 'spec_draft_n_max'],
  ['Mín. tokens draft (--spec-draft-n-min)', 'spec_draft_n_min'],
  ['Tipo K draft (-ctkd)', 'spec_draft_type_k'], ['Tipo V draft (-ctvd)', 'spec_draft_type_v'],
  ['Hilos draft (-td, vacío = auto)', 'spec_draft_threads'],
  ['Hilos batch draft (-tbd, vacío = auto)', 'spec_draft_threads_batch'],
  // MoE
  ['Capas MoE en CPU (-ncmoe)', 'n_cpu_moe'], ['Override tensor (-ot)', 'override_tensor']
];
var CFG_CHECKS = [
  ['webui_mcp_proxy', 'Activar --webui-mcp-proxy'],
  ['gpu_for_embeddings', 'GPU para Embeddings (por defecto ON)'],
  ['gpu_for_chat', 'GPU para Chat (por defecto OFF)'],
  ['use_agent', 'Activar --agent (herramientas integradas)'],
  ['no_ui', 'Activar --no-ui (ocultar visor embebido)'],
  ['piper_enabled', 'Activar servicio Piper'],
  ['dspark_enabled', 'Activar DSpark si está disponible'],
  ['gpu_for_dspark', 'GPU para DSpark'],
  ['moe_enabled', 'Activar configuración MoE'],
  ['gpu_for_moe', 'GPU para expertos MoE'],
  ['cpu_moe', 'Todos los expertos en CPU (-cmoe)'],
  ['perf_enabled', '--perf (diagnóstico de rendimiento)'],
  ['no_kv_offload', '--no-kv-offload (-nkvo)'],
  ['laya_enabled', 'Router Laya ONNX (~650 MB, ~100× más rápido)'],
  ['laya_preload', 'Precargar Laya al arranque'],
  ['enable_multimodal', 'Cargar proyector visual (mmproj)']
];

function openConfig() {
  api('/api/config').then(function (d) {
    var cfg = d.config || {};
    function tipAttr(key) {
      return PERF_HELP_TIPS[key] ? ' title="' + esc(PERF_HELP_TIPS[key]) + '"' : '';
    }
    function tipDot(key) {
      return PERF_HELP_TIPS[key] ? '<span class="help-dot" title="' + esc(PERF_HELP_TIPS[key]) + '">?</span>' : '';
    }
    document.getElementById('adv-grid').innerHTML = CFG_FIELDS.map(function (f) {
      return '<div class="adv-row"><label' + tipAttr(f[1]) + '>' + f[0] + tipDot(f[1]) + '</label><input type="text" id="cfg-' + f[1] + '" value="' + esc(cfg[f[1]]) + '"></div>';
    }).join('');
    document.getElementById('adv-checks').innerHTML = CFG_CHECKS.map(function (c) {
      var checked = !!cfg[c[0]];
      return '<div class="chk-row"><label class="check-label"' + tipAttr(c[0]) + '><input type="checkbox" id="chk-' + c[0] + '"' + (checked ? ' checked' : '') + '> ' + c[1] + tipDot(c[0]) + '</label></div>';
    }).join('');
    document.getElementById('config-modal').className = 'modal open';
  });
}
function closeConfig() {
  document.getElementById('config-modal').className = 'modal';
}
function saveConfig() {
  var payload = {};
  CFG_FIELDS.forEach(function (f) {
    var el = document.getElementById('cfg-' + f[1]);
    payload[f[1]] = el ? el.value : null;
  });
  CFG_CHECKS.forEach(function (c) {
    var el = document.getElementById('chk-' + c[0]);
    payload[c[0]] = el ? el.checked : false;
  });
  api('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    .then(function (d) {
      if (d && d.success !== false) { toast('Configuración guardada'); closeConfig(); refreshStatus(); }
      else { toast((d && d.error) || 'Error', true); }
    });
}

function onAction(action, el) {
  if (action === 'start-service') {
    api('/api/services/' + el.getAttribute('data-service') + '/start', { method: 'POST' }).then(function () { toast('Servicio iniciado'); refreshStatus(); });
  } else if (action === 'stop-service') {
    api('/api/services/' + el.getAttribute('data-service') + '/stop', { method: 'POST' }).then(function () { toast('Servicio detenido'); refreshStatus(); });
  } else if (action === 'start-chat') { startChat(); }
  else if (action === 'start-router') {
    api('/api/router/start', { method: 'POST' }).then(function (d) { if (d && d.error) { toast(d.error, true); } else { toast('Router Mode iniciando'); } refreshStatus(); });
  } else if (action === 'stop-chat') {
    api('/api/chat/stop', { method: 'POST' }).then(function () { toast('Chat detenido'); refreshStatus(); });
  } else if (action === 'models-folder') { changeModelsFolder(); }
  else if (action === 'change-workspace') { changeWorkspace(); }
  else if (action === 'open-interface') { api('/api/open-interface', { method: 'POST' }).then(function () { toast('Abriendo interfaz'); }); }
  else if (action === 'launch-cli') { api('/api/launch-cli', { method: 'POST' }).then(function () { toast('CLI lanzado'); }); }
  else if (action === 'open-config') { openConfig(); }
  else if (action === 'laya-load') {
    fetch(agentUrl('/api/laya/load'), { method: 'POST' }).then(function (r) { return r.json(); }).then(function (d) {
      toast(d && d.loaded ? 'Laya cargado' : 'No se pudo cargar Laya (¿modelo descargado?)', !(d && d.loaded));
      refreshLaya();
    }).catch(function () { toast('No se pudo contactar con dev_agent', true); });
  } else if (action === 'laya-unload') {
    fetch(agentUrl('/api/laya/unload'), { method: 'POST' }).then(function () {
      toast('Laya descargado de RAM');
      refreshLaya();
    }).catch(function () { toast('No se pudo contactar con dev_agent', true); });
  }
  else if (action === 'save-config') { saveConfig(); }
  else if (action === 'close-config') { closeConfig(); }
}

document.addEventListener('click', function (ev) {
  var el = ev.target && ev.target.closest ? ev.target.closest('[data-action]') : null;
  if (el) { onAction(el.getAttribute('data-action'), el); }
});
document.addEventListener('change', function (ev) {
  if (ev.target && ev.target.id === 'interface') { saveInterface(); }
  if (ev.target && (ev.target.id === 'auto-start-check' || ev.target.id === 'auto-model')) { saveAutoStart(); }
});

function loadLogs() {
  api('/api/logs').then(function (d) {
    var box = document.getElementById('logs');
    if (box && d && d.lines) { box.textContent = d.lines.join('\n'); box.scrollTop = box.scrollHeight; }
  });
}

buildShell();
loadLogs();
refreshStatus();
setInterval(refreshStatus, 5000);
setInterval(loadLogs, 10000);

var es = new EventSource('/api/logs/stream');
es.onmessage = function (e) {
  try {
    var data = JSON.parse(e.data);
    if (!data || !data.line) return;
    var box = document.getElementById('logs');
    box.textContent += data.line + '\n';
    box.scrollTop = box.scrollHeight;
  } catch (err) { }
};
es.onerror = function () { };
</script>
</body>
</html>
"""

def create_app(state: Optional[WebManagerState] = None) -> Flask:
    state = state or WebManagerState()
    app = Flask(__name__)

    # Detrás de nginx: confiar en X-Forwarded-* (host, proto, prefijo).
    try:
        from tools.auth import apply_proxy_fix
        apply_proxy_fix(app)
    except Exception:
        pass

    @app.after_request
    def cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    @app.get("/")
    def index():
        return render_template_string(HTML)

    @app.get("/api/status")
    def status():
        return jsonify(state.status())

    @app.get("/api/models")
    def models():
        return jsonify({"models": state._scan_models()})

    @app.post("/api/chat/start")
    def chat_start():
        return jsonify(state.start_chat(str((request.json or {}).get("model", ""))))

    @app.post("/api/chat/stop")
    def chat_stop():
        state.stop("chat")
        return jsonify({"success": True})

    @app.post("/api/router/start")
    def router_start():
        return jsonify(state.start_router())

    @app.post("/api/services/<name>/start")
    def service_start(name: str):
        return jsonify(state.start_service(name))

    @app.post("/api/services/<name>/stop")
    def service_stop(name: str):
        state.stop(name)
        return jsonify({"success": True})

    @app.post("/api/embeddings/start")
    def embeddings_start():
        return jsonify({"success": False, "error": "Use /api/services/embeddings/start en esta versión"}), 501

    @app.post("/api/dev_agent/start")
    def dev_agent_start():
        return jsonify(state.start_service("dev_agent"))

    @app.post("/api/rag/start")
    def rag_start():
        return jsonify(state.start_service("rag"))

    @app.post("/api/piper/start")
    def piper_start():
        return jsonify(state.start_service("piper"))

    @app.route("/api/agent/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    def agent_proxy(subpath: str):
        """Proxy hacia dev_agent (+token). Unifica origen y no expone el token."""
        url = f"{AGENT_BASE_URL}/{subpath}"
        if request.query_string:
            url += "?" + request.query_string.decode("utf-8", "replace")
        headers = {"Content-Type": request.headers.get("Content-Type", "application/json")}
        token = _agent_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = request.get_data() or None
        upstream = urllib.request.Request(url, data=body, headers=headers, method=request.method)
        try:
            with urllib.request.urlopen(upstream, timeout=AGENT_PROXY_TIMEOUT) as response:
                payload = response.read()
                return Response(
                    payload,
                    status=getattr(response, "status", 200),
                    content_type=response.headers.get("Content-Type", "application/json"),
                )
        except urllib.error.HTTPError as error:
            return Response(
                error.read() or b"{}",
                status=error.code,
                content_type="application/json",
            )
        except Exception as error:
            return jsonify({"success": False, "error": f"dev_agent no disponible: {error}"}), 502

    @app.get("/api/config")
    def config_get():
        return jsonify({"success": True, "config": state.defaults})

    @app.post("/api/config")
    def config_post():
        data = request.json or {}
        for key, value in data.items():
            if key not in state.defaults:
                continue
            if key == "workspace_history":
                state.defaults[key] = value
                continue
            if key in _NGL_CONFIG_KEYS:
                state.defaults[key] = state._normalize_ngl(value, "auto")
                continue
            if key in _INT_CONFIG_KEYS:
                state.defaults[key] = state._safe_int(value, state.defaults.get(key))
                continue
            if key in _FLOAT_CONFIG_KEYS:
                try:
                    state.defaults[key] = float(value)
                except (TypeError, ValueError):
                    pass
                continue
            if key in _OPTIONAL_INT_CONFIG_KEYS:
                text = str(value).strip() if value is not None else ""
                state.defaults[key] = state._safe_int(text, None) if text not in ("", "None", "null") else None
                continue
            if key in _BOOL_CONFIG_KEYS:
                if isinstance(value, str):
                    state.defaults[key] = value.strip().lower() in ("true", "1", "yes", "on", "si", "sí")
                else:
                    state.defaults[key] = bool(value)
                continue
            if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                state.defaults[key] = value.strip().lower() == "true"
            else:
                state.defaults[key] = value
        state.save_config()
        state.log("🔧 Configuración avanzada guardada")
        return jsonify({"success": True, "config": state.defaults})

    @app.post("/api/models-folder")
    def models_folder():
        path = str((request.json or {}).get("path", "")).strip()
        return jsonify(state.set_models_folder(path))

    @app.post("/api/interface")
    def interface():
        pref = str((request.json or {}).get("interface_preference", "WebUI"))
        return jsonify(state.set_interface(pref))

    @app.post("/api/auto-start")
    def auto_start():
        data = request.json or {}
        enabled = bool(data.get("auto_start_enabled", False))
        model_token = str(data.get("auto_start_model", ""))
        return jsonify(state.set_auto_start(enabled, model_token))

    @app.post("/api/open-interface")
    def open_interface():
        return jsonify(state.open_interface())

    @app.post("/api/launch-cli")
    def launch_cli():
        return jsonify(state.launch_cli())

    @app.post("/api/workspace")
    def workspace():
        path = str((request.json or {}).get("path", "")).strip()
        if not path:
            return jsonify({"success": False, "error": "path requerido"}), 400
        Path(path).mkdir(parents=True, exist_ok=True)
        state.defaults["workspace_path"] = path
        history = [p for p in state.defaults.get("workspace_history", []) if p != path]
        state.defaults["workspace_history"] = [path, *history[:9]]
        state.save_config()
        state.log(f"📁 Workspace actualizado a: {path}")
        state.restart_dev_agent()
        return jsonify({"success": True, "workspace": path})

    @app.get("/api/logs")
    def logs_get():
        log_file = Path(runtime.get_user_data_dir()) / "logs" / "llama_manager.log"
        lines = []
        if log_file.exists():
            try:
                raw = log_file.read_text(encoding="utf-8", errors="replace")
                lines = raw.splitlines()[-500:]
            except Exception:
                lines = []
        return jsonify({"lines": lines})

    @app.get("/api/logs/stream")
    def logs_stream():
        def generate():
            while True:
                try:
                    entry = state.log_queue.get(timeout=15)
                    line = f"[{entry['timestamp']}] [{entry['tag']}] {entry['message']}"
                    yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        return Response(generate(), mimetype="text/event-stream")

    return app


def start_web_server(host: str = "0.0.0.0", port: int = WEB_MANAGER_PORT, state: Optional[WebManagerState] = None) -> None:
    create_app(state).run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    start_web_server(host="0.0.0.0")