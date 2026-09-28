# tools/agent_cli.py
# CLI v3.0 - Router-First + Reasoning Control + Windows ANSI Fix

# =============================================================================
# 0. WINDOWS ANSI FIX (ANTES que cualquier otro import)
# =============================================================================
import os
import sys
import logging

logger = logging.getLogger(__name__)

if sys.platform == 'win32':
    # Forzar UTF-8 en la consola
    os.system('chcp 65001 > nul')
    # Activar procesamiento de secuencias ANSI (colores, etc.)
    import ctypes
    kernel32 = ctypes.windll.kernel32
    # STD_OUTPUT_HANDLE = -11, STD_ERROR_HANDLE = -12
    for handle in (-11, -12):
        try:
            kernel32.SetConsoleMode(kernel32.GetStdHandle(handle), 7)
        except:
            pass
        
if os.name == "nt":
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        hStdOut = kernel32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        kernel32.GetConsoleMode(hStdOut, ctypes.byref(mode))
        mode.value |= 0x0004
        kernel32.SetConsoleMode(hStdOut, mode)
    except Exception:
        pass
    try:
        import colorama
        colorama.init(convert=True, strip=False, wrap=True)
        sys.stdout = colorama.AnsiToWin32(sys.stdout).stream
        sys.stderr = colorama.AnsiToWin32(sys.stderr).stream
    except ImportError:
        pass

# =============================================================================
# 0.5 SEGURIDAD DEL EVENT LOOP DE WINDOWS (WinError 1236)
# -----------------------------------------------------------------------------
# Cuando el sistema operativo detecta un cambio de red, Winsock aborta las
# operaciones pendientes con WSAECONNABORTED (WinError 1236). Si eso ocurre en
# el self-pipe del event loop de asyncio (el que usa prompt_toolkit para leer
# input en un hilo), el CLI se congela: no recibe más teclas y las tareas
# parecen detenerse. Aquí instalamos un policy que re-arma el self-pipe ante
# ese error para que el lector de entrada nunca muera por un cambio de red.
# =============================================================================
if sys.platform == "win32":
    try:
        import asyncio as _asyncio

        _REPAIR_COUNTS = {}
        _REPAIR_GUARD = {"active": False}

        def _install_loop_safety(loop):
            def _safety_handler(loop_, context):
                exc = context.get("exception")
                winerr = getattr(exc, "winerror", None)
                if isinstance(exc, OSError) and winerr in (1236, 995, 996) and not _REPAIR_GUARD["active"]:
                    # Re-arm del self-pipe (limitado para no girar en bucle).
                    n = _REPAIR_COUNTS.get(id(loop_), 0)
                    if n < 3:
                        _REPAIR_COUNTS[id(loop_)] = n + 1
                        _REPAIR_GUARD["active"] = True
                        try:
                            if hasattr(loop_, "_loop_self_reading"):
                                loop_._loop_self_reading(None)
                            elif hasattr(loop_, "_write_to_self"):
                                loop_._write_to_self()
                        except Exception:
                            pass
                        finally:
                            _REPAIR_GUARD["active"] = False
                        return
                try:
                    loop_.default_exception_handler(context)
                except Exception:
                    pass

            try:
                loop.set_exception_handler(_safety_handler)
            except Exception:
                pass

        class _SafeWindowsProactorPolicy(_asyncio.WindowsProactorEventLoopPolicy):
            def new_event_loop(self):
                loop = super().new_event_loop()
                _install_loop_safety(loop)
                return loop

        _asyncio.set_event_loop_policy(_SafeWindowsProactorPolicy())
    except Exception:
        pass

# =============================================================================
# 1. IMPORTS
# =============================================================================
import json
import time
import threading
import queue
import argparse
import logging
import signal
import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

try:
    from . import runtime
except ImportError:
    import runtime

USER_DATA_DIR = Path(runtime.get_user_data_dir())
DEBUG_LOG = USER_DATA_DIR / "cli_debug.log"
DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(DEBUG_LOG, encoding="utf-8")],
    force=True
)
for _name in ["agent_core", "chromadb", "chromadb.telemetry", "urllib3", "httpcore", "httpx", "requests"]:
    logging.getLogger(_name).setLevel(logging.ERROR)

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.box import ROUNDED
from rich.text import Text
from rich.align import Align
from rich.live import Live

from .agent_core import Orchestrator, AgentEvent, EventBus
from .daemon_agent import get_daemon_instance
from .checkpoint_manager import CheckpointManager
from .memory_skills import get_skill_manager
from .agency_catalog import create_default_catalog

try:
    from .autocompletion import IntelligentCompleter, SessionHistoryManager
except ImportError:
    IntelligentCompleter = None

try:
    from .ipc import IPCBridge, SharedState
except ImportError:
    IPCBridge = None
    SharedState = None

try:
    from .metrics import MetricsCollector, PerformanceTimer
except ImportError:
    MetricsCollector = None

# =============================================================================
# 2. CONSTANTES
# =============================================================================
VERSION = "3.0.0"
SESSIONS_DIR = USER_DATA_DIR / "cli_sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# 3. IMPORTS Y INICIALIZACIÓN DE AUTOCOMPLETADO
# =============================================================================
from .autocompletion import IntelligentCompleter, SessionHistoryManager, FuzzyHistorySearcher

class SlashCommandCompleter(Completer):
    """DEPRECATED: Usar IntelligentCompleter en su lugar."""
    COMMANDS = {
        "/help": "Muestra esta ayuda",
        "/status": "Muestra el estado del agente y daemon",
        "/exit": "Sale de la CLI",
        "/pause": "Pausa el daemon",
        "/resume": "Reanuda el daemon",
        "/stop": "Detiene el daemon",
        "/checkpoints": "Lista checkpoints disponibles",
        "/rollback": "Restaura un checkpoint (/rollback <id>)",
        "/skills": "Lista skills disponibles",
        "/model": "Muestra o cambia el modelo",
        "/clear": "Limpia la pantalla",
        "/reasoning": "Activa/desactiva razonamiento (/reasoning on|off)",
        "/history": "Historial de sesión o búsqueda",
        "/tree": "Árbol del workspace",
        "/diff": "Compara dos archivos (/diff a b)",
        "/stats": "Métricas de sesión",
        "/compact": "Alterna modo visual compacto",
        "/verbose": "Alterna modo visual verbose",
        "/metrics": "Métricas de ejecución detalladas",
        "/export": "Exporta datos (/export session|metrics|skills)",
        "/goal": "Inicia el daemon con un objetivo (/goal <texto>)",
        "/good": "La última tarea fue útil",
        "/bad": "La última tarea no fue útil",
        "/procedure": "Gestiona procedimientos (/procedure list|save|run|delete|show)",
        "/agents": "Lista o selecciona agentes agency (/agents list|select|clear)",
    }
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            for cmd in self.COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text))

# =============================================================================
# 4. RENDERER DE EVENTOS
# =============================================================================
class EventRenderer:
    @staticmethod
    def render(event: AgentEvent) -> Optional[Any]:
        t = event.type
        p = event.payload

        if t == "text":
            text = str(p or "").strip()
            if not text:
                return None
            return Text(text, style="white")
        if t == "reasoning":
            return Text(p, style="bright_black")
        if t == "tool_call":
            name = p.get("name", "tool") or "?"
            args = p.get("arguments", {})
            desc = p.get("description", "")
            try:
                args_str = json.dumps(args, ensure_ascii=False, indent=None) if args else "{}"
            except Exception:
                args_str = str(args)
            if len(args_str) > 120:
                args_str = args_str[:117] + "..."
            txt = Text()
            txt.append("🔧 ", style="bold cyan")
            txt.append(f"{name}", style="bold yellow")
            txt.append(f" {args_str}", style="dim")
            if desc:
                txt.append(f"\n  ↳ {desc}", style="italic dim")
            return Panel(txt, title="Herramienta", border_style="cyan", padding=(0, 1))
        if t == "tool_result":
            result = p.get("result", "")
            raw = str(result)
            is_error = (
                raw.lower().startswith("error")
                or "❌" in raw
                or (isinstance(result, dict) and result.get("status") in ("error", "denied", "disabled"))
            )
            # Detectar binario/volcado de bytes: no imprimirlo entero.
            sample = raw[:512]
            printable_ratio = sum(1 for c in sample if c.isprintable() or c in "\n\r\t ") / max(1, len(sample))
            # Los repr de bytes (b'PK\x03\x04...') son ASCII imprimible: se
            # detectan por la densidad de escapes \xNN.
            is_binary = "\x00" in sample or sample.count("\\x") > 8 or printable_ratio < 0.6
            if is_binary:
                preview = f"⚠️ resultado binario/no imprimible ({len(raw)} bytes) — usa una herramienta adecuada para procesarlo"
            elif len(raw) > 800:
                preview = raw[:800] + f"\n… (truncado: {len(raw)} caracteres)"
            else:
                preview = raw
            color = "red" if is_error else "green"
            txt = Text()
            txt.append("📋 ", style=f"bold {color}")
            txt.append("Resultado", style="bold")
            if is_error:
                txt.append(" (error)", style="bold red")
            txt.append(":\n", style="bold")
            txt.append(preview, style="dim")
            return Panel(txt, title="Resultado", border_style=color, padding=(0, 1))
        if t == "plan":
            return EventRenderer._format_plan(p)
        if t == "step_start":
            idx = p.get("idx", 0)
            total = p.get("total", 0)
            subtask = p.get("subtask", "")
            agent = p.get("agent", "")
            txt = Text()
            txt.append(f"\n─── Paso {idx}/{total} · {agent} ───\n", style="bold magenta")
            txt.append(f"📝 {subtask}\n", style="white")
            return txt
        if t == "step_end":
            idx = p.get("idx", 0)
            total = p.get("total", 0)
            return Text(f"✅ Paso {idx}/{total} completado\n", style="dim green")
        if t == "error":
            return Text(f"\n❌ Error: {p}\n", style="bold red")
        if t == "done":
            content = str(p.get("content", "")).strip() if isinstance(p, dict) else str(p).strip()
            if content:
                return Panel(content[:1200], title="✅ Resultado final", border_style="green", padding=(0, 1))
            return None
        if t == "router":
            return EventRenderer._format_router(p)
        if t == "usage":
            return None
        return None

    @staticmethod
    def _format_plan(plan: Dict) -> Panel:
        steps = plan.get("steps", [])
        total = len(steps)
        agents = sorted({str(s.get("agent", "General")) for s in steps})
        title = f"📋 Plan de Ejecución — {total} pasos · {' · '.join(agents)}" if agents else "📋 Plan de Ejecución"
        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED, expand=True)
        table.add_column("#", style="dim", width=4)
        table.add_column("Tarea", style="white", ratio=2)
        table.add_column("Agente", style="green", ratio=1)
        table.add_column("Deps", style="yellow", ratio=1)
        for step in steps:
            deps = ", ".join(step.get("depends_on", [])) or "-"
            table.add_row(step.get("id", "?"), step.get("subtask", ""), step.get("agent", "General"), deps)
        return Panel(table, title=title, border_style="cyan", padding=(0, 1))

    @staticmethod
    def _format_router(payload: Dict) -> Optional[Any]:
        status = payload.get("status", "")
        if status == "analyzing":
            return Text(f"\n🔀 Analizando tarea…\n", style="italic dim")
        elif status == "decided":
            mode = payload.get("mode", "?")
            conf = payload.get("confidence", 0)
            reason = payload.get("reasoning", "")
            color = {
                "PLAN": "magenta", "GENERAL": "green",
                "CODE": "blue", "RESEARCH": "yellow", "ECONOMY": "yellow", "CHAT": "green"
            }.get(mode, "cyan")
            txt = Text()
            txt.append("Modo: ", style="dim")
            txt.append(f"{mode}", style=f"bold {color}")
            txt.append(f"   confianza {conf:.0%}", style="dim")
            if reason:
                txt.append(f"\n{reason}", style="italic dim")
            return Panel(txt, title="🔀 Router", border_style=color, padding=(0, 1))
        return None

# =============================================================================
# 5. CLI PRINCIPAL
# =============================================================================
class AgentCLI:
    def __init__(self, workspace_path: Optional[str] = None, headless: bool = False):
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
        self.headless = headless
        self.orchestrator = Orchestrator(workspace_path=str(self.workspace_path))
        self.agency_catalog = create_default_catalog()
        self.selected_agency_agents = []
        self.checkpoint_manager = CheckpointManager(str(self.workspace_path))
        self.skill_manager = get_skill_manager(str(self.workspace_path))
        self.daemon = get_daemon_instance()
        self._daemon_event_bus = EventBus()
        self._daemon_event_bus.on(self._queue_daemon_event)

        self.history: List[Dict[str, Any]] = []
        self.session_id = None
        self.session_file = None
        self.session_start = datetime.now()
        self.total_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._task_input_tokens = 0
        self._task_output_tokens = 0

        self.current_model = self._detect_model()
        self.reasoning_enabled = True
        self.is_generating = False
        self.is_running = True
        self._reasoning_buffer = ""
        self._live_reasoning: Optional[Live] = None
        self._live_kind: Optional[str] = None  # "progress" | "reasoning"
        self._at_line_start = True
        self._progress_state: Optional[Dict] = None
        self._task_incomplete_reason: Optional[str] = None
        # Razonamiento del ROUTER: no se streamea en vivo (solo el panel final).
        self._router_phase = False
        # Texto retenido que parece tool call (execute_tool(...)) hasta que
        # llegue el evento tool_call que la consume (supresión por estado).
        self._tool_suppress = ""
        # Últimos caracteres sin imprimir: permiten detectar el inicio de una
        # tool call aunque el stream la corte en trozos.
        self._stream_pending = ""

        # Flags de control (señales y comandos en caliente)
        self._cancel_requested = False
        self._exit_requested = False
        self._force_quit = False
        self._last_sigint_time: Optional[float] = None
        self._last_idle_interrupt: Optional[float] = None
        self._last_input_beat = time.time()

        # Métricas y preferencias
        self.metrics_collector = MetricsCollector(str(self.workspace_path)) if MetricsCollector else None
        self._task_start_time: Optional[float] = None
        self._tool_timing: Optional[Dict[str, Any]] = None
        self._last_task_text = ""

        # IPC con el Manager (fallo silencioso si no hay Manager)
        self.ipc_bridge = None
        self._shared_state = None
        self._ipc_stop = threading.Event()
        self._ipc_thread: Optional[threading.Thread] = None
        self._last_shared_model = None

        # Modo CLI por defecto (restaurado desde preferencias)
        self.cli_mode = "NORMAL"
        self._load_prefs()

        self.console = Console(
            file=sys.stdout,
            force_terminal=True,
            color_system="auto",
            legacy_windows=False
        )
        
        # Sesión de prompt re-creable (el hilo de entrada puede reconstruirla
        # si el event loop subyacente muere, p.ej. WinError 1236 por cambio de red).
        # Lazy: headless / tests no necesitan consola de Windows.
        self.prompt_session = None
        
        # Session history manager para tracking avanzado
        self.session_history = SessionHistoryManager(SESSIONS_DIR) if IntelligentCompleter else None
        
        self._event_queue: List[AgentEvent] = []
        self._queue_lock = threading.Lock()

        # Cola para input asincrono (permite comandos / durante generacion)
        self._input_queue: queue.Queue = queue.Queue()
        self._input_thread: Optional[threading.Thread] = None

        # Señales del terminal: Ctrl+C / Ctrl+D deben cancelar o cerrar de forma consistente.
        self._install_signal_handlers()

    def run(self, message: Optional[str] = None):
        if self.headless:
            self._run_headless(message)
            return
        self._init_ipc()
        self._print_banner()
        self._load_session()
        self._start_input_thread()
        self._interactive_loop()
        self._stop_ipc()
        self._save_prefs()

    def _create_prompt_session(self):
        """Crea (o recrea) la sesión de prompt de prompt_toolkit."""
        history_file = SESSIONS_DIR / "cli_history.txt"
        intelligent_completer = None
        if IntelligentCompleter:
            intelligent_completer = IntelligentCompleter(
                workspace_path=str(self.workspace_path),
                history_file=history_file
            )
        return PromptSession(
            history=FileHistory(str(history_file)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=intelligent_completer,
            multiline=False,
        )

    # ========================================================================
    # PREFERENCIAS PERSISTENTES
    # ========================================================================
    PREFS_FILE = USER_DATA_DIR / "cli_prefs.json"

    def _load_prefs(self):
        """Restaura preferencias de la CLI entre sesiones."""
        try:
            if self.PREFS_FILE.exists():
                data = json.loads(self.PREFS_FILE.read_text(encoding="utf-8"))
                self.cli_mode = data.get("cli_mode", self.cli_mode)
                self.reasoning_enabled = bool(data.get("reasoning", self.reasoning_enabled))
                saved_model = data.get("model")
                if saved_model:
                    self.current_model = saved_model
        except Exception:
            pass

    def _save_prefs(self):
        """Guarda preferencias de la CLI entre sesiones."""
        try:
            self.PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.PREFS_FILE.write_text(json.dumps({
                "cli_mode": self.cli_mode,
                "reasoning": self.reasoning_enabled,
                "model": self.current_model,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ========================================================================
    # IPC CON EL MANAGER (fallo silencioso si no hay Manager)
    # ========================================================================
    def _init_ipc(self):
        try:
            if SharedState is not None:
                self._shared_state = SharedState(
                    state_file=Path(runtime.get_user_data_dir()) / "shared_state.json"
                )
                self._last_shared_model = self._shared_state.get("current_model")
            if IPCBridge is not None:
                self.ipc_bridge = IPCBridge(role="cli")
        except Exception as e:
            logger.debug(f"IPC CLI no disponible: {e}")
            self.ipc_bridge = None
        # Poller de SharedState: detecta cambios de modelo / estado del servidor
        if self._shared_state is not None and not self.headless:
            self._ipc_thread = threading.Thread(target=self._ipc_poll_loop, daemon=True, name="cli-ipc")
            self._ipc_thread.start()

    def _stop_ipc(self):
        self._ipc_stop.set()
        if self._ipc_thread is not None:
            self._ipc_thread.join(timeout=1.5)

    def _ipc_poll_loop(self):
        while not self._ipc_stop.is_set():
            try:
                model = self._shared_state.get("current_model")
                if model and model != self._last_shared_model and not self.is_generating:
                    self._last_shared_model = model
                    self.current_model = model
                    self.console.print(f"[dim]🔌 Modelo actualizado desde el Manager: {model}[/dim]")
            except Exception:
                pass
            self._ipc_stop.wait(timeout=2.0)

    def _send_ipc(self, msg_type: str, data: Optional[Dict] = None):
        """Envía un mensaje IPC al Manager (best-effort)."""
        if self.ipc_bridge is None:
            return
        try:
            from .ipc import IPCMessageType
            mtype = IPCMessageType(msg_type)
            if self.ipc_bridge.client is not None:
                from .ipc import IPCMessage
                self.ipc_bridge.client.send(IPCMessage(mtype, data or {}))
        except Exception:
            pass

    def _start_input_thread(self):
        """Hilo dedicado a leer input del usuario sin bloquear el render loop.

        Es resiliente: si el event loop interno de prompt_toolkit muere por un
        cambio de red (WinError 1236) o por cualquier otra excepción, el hilo
        reconstruye la sesión y sigue leyendo en lugar de congelar la CLI.
        """
        self._last_input_beat = time.time()
        if self.prompt_session is None:
            self.prompt_session = self._create_prompt_session()

        def _reader():
            while self.is_running:
                # Mientras se genera, el prompt NO se dibuja: la generación es
                # el ÚNICO escritor del terminal (Opción 1). Sin prompt activo,
                # rich.Live y el streaming token a token no pueden corromperse.
                if self.is_generating:
                    time.sleep(0.05)
                    continue
                self._last_input_beat = time.time()
                try:
                    user_input = self.prompt_session.prompt(
                        HTML("<ansigreen><b>➜</b></ansigreen> "),
                        bottom_toolbar=self._toolbar_html,
                        refresh_interval=0.5
                    )
                    self._input_queue.put(("input", user_input))
                    # Pequeña pausa: deja que el hilo principal marque
                    # is_generating antes de volver a mostrar el prompt, para
                    # que el prompt nunca compita con la generación.
                    time.sleep(0.15)
                except KeyboardInterrupt:
                    self._input_queue.put(("interrupt", None))
                except EOFError:
                    self._input_queue.put(("eof", None))
                    break
                except Exception as e:
                    logger.error(f"Input thread error: {e}")
                    self._input_queue.put(("input_error", f"Entrada reiniciada ({e})"))
                    try:
                        self.prompt_session = self._create_prompt_session()
                    except Exception:
                        pass
                    time.sleep(0.4)

        self._input_thread = threading.Thread(target=_reader, daemon=True, name="cli-input")
        self._input_thread.start()

    def _install_signal_handlers(self):
        """Intercepta Ctrl+C en terminal para cancelar la tarea activa."""
        try:
            if hasattr(signal, "SIGINT"):
                self._sigint_handler = signal.getsignal(signal.SIGINT)
                signal.signal(signal.SIGINT, self._handle_terminal_signal)
            if hasattr(signal, "SIGTERM"):
                self._sigterm_handler = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, self._handle_terminal_signal)
        except (AttributeError, ValueError, OSError):
            pass

    def _handle_terminal_signal(self, signum, frame):
        """Ctrl+C: primera pulsación cancela, doble pulsación fuerza salida.

        No cancela directamente dentro del handler (imprimir desde un signal
        handler puede corromper la salida): marca un flag que los bucles
        principales procesan en cuanto pueden.
        """
        now = time.time()
        if self._last_sigint_time is not None and (now - self._last_sigint_time) < 2.0:
            self._force_quit = True
            self._exit_requested = True
            self.is_running = False
            try:
                self.orchestrator.stop()
            except Exception:
                pass
            return
        self._last_sigint_time = now
        if self.is_generating:
            self._cancel_requested = True
        else:
            # No imprimir desde el signal handler (corrompe la salida): se
            # enruta por la cola y lo imprime el bucle principal.
            try:
                self._input_queue.put(("notify", "Ctrl+C: cancela la tarea activa. Ctrl+D o /exit para salir."))
            except Exception:
                pass

    def _cancel_current_generation(self, reason: str = "Generación cancelada por usuario"):
        """Cancela el trabajo activo sin dejar la CLI en estado inconsistente."""
        try:
            self.orchestrator.stop()
        except Exception:
            pass
        self.is_generating = False
        self._stop_live()
        self._reasoning_buffer = ""
        self.console.print(f"\n[bold red]{reason}[/bold red]")

    def _interactive_loop(self):
        # IMPORTANTE: la consola escribe DIRECTO al stdout original (la que se
        # creó en __init__), NO al proxy de patch_stdout. Si se recreara con
        # file=sys.stdout dentro del patch, prompt_toolkit desactiva el VT
        # processing y las secuencias ANSI de rich salen crudas (?[2m...).
        # Con el stdout directo y el VT activado en el arranque, los colores
        # y cuadros se renderizan bien y el stream es fluido.
        with patch_stdout():
            while self.is_running:
                if self._exit_requested or self._force_quit:
                    break
                # Ctrl+C marcado por el signal handler mientras generábamos
                if not self.is_generating and self._cancel_requested:
                    self._cancel_requested = False
                    self.console.print("\n[dim]Ctrl+C: cancela la tarea activa. Ctrl+D o /exit para salir.[/dim]")
                # Watchdog del hilo de entrada: solo actúa si el hilo MURIÓ.
                # (El heartbeat no es fiable: mientras el lector espera en prompt()
                # está sano pero bloqueado, y eso disparaba falsas alarmas.)
                if self._input_thread is not None and not self._input_thread.is_alive():
                    self.console.print("[yellow]⚠️ Lector de entrada reiniciado…[/yellow]")
                    self._start_input_thread()

                try:
                    evt_type, data = self._input_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if evt_type == "eof":
                    if self.is_generating:
                        self._cancel_current_generation("🛑 Cierre solicitado con Ctrl+D")
                    self.is_running = False
                    self._exit_requested = True
                    break
                if evt_type == "interrupt":
                    if self.is_generating:
                        self._cancel_current_generation()
                    else:
                        now = time.time()
                        if self._last_idle_interrupt is not None and (now - self._last_idle_interrupt) < 1.5:
                            # Doble Ctrl+C en reposo = salir (coherente con el banner)
                            self._exit_requested = True
                            self.is_running = False
                            self._force_quit = True
                            break
                        self._last_idle_interrupt = now
                        self.console.print("\n[dim]Ctrl+C: cancela la tarea activa. Pulsa Ctrl+C otra vez o /exit para salir.[/dim]")
                    continue
                if evt_type == "notify":
                    self.console.print(f"\n[dim]{data}[/dim]")
                    continue
                if evt_type == "daemon_event":
                    rendered = EventRenderer.render(data)
                    if rendered is not None:
                        self.console.print(rendered)
                    continue
                if evt_type == "input_error":
                    self.console.print(f"[dim]⚠️ {data}[/dim]")
                    continue
                if evt_type == "input":
                    user_input = data.strip()
                    if not user_input:
                        continue
                    if self.is_generating:
                        if user_input.startswith("/"):
                            self._handle_hot_command(user_input)
                        else:
                            self.console.print("[yellow]Espera a que termine la generación actual antes de enviar otra tarea.[/yellow]")
                        continue
                    self._process_input(user_input)
        self._save_session()
        self.is_running = False
        self.console.print("\n[bold green]¡Hasta luego! 👋[/bold green]")

    def _process_input(self, user_input: str):
        if user_input.startswith("/"):
            self._handle_command(user_input)
        else:
            self._send_message(user_input)

    # ========================================================================
    # COMANDOS
    # ========================================================================
    def _handle_command(self, command: str):
        parts = command.split()
        cmd = parts[0].lower()
        args = parts[1:]
        handlers = {
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/exit": self._cmd_exit,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/stop": self._cmd_stop,
            "/checkpoints": self._cmd_checkpoints,
            "/rollback": lambda: self._cmd_rollback(args),
            "/skills": self._cmd_skills,
            "/clear": self._cmd_clear,
            "/model": lambda: self._cmd_model(args),
            "/reasoning": lambda: self._cmd_reasoning(args),
            "/history": lambda: self._cmd_history(args),
            "/tree": lambda: self._cmd_tree(args),
            "/diff": lambda: self._cmd_diff(args),
            "/stats": lambda: self._cmd_stats(args),
            "/compact": self._cmd_compact,
            "/verbose": self._cmd_verbose,
            "/metrics": self._cmd_metrics,
            "/export": lambda: self._cmd_export(args),
            "/goal": lambda: self._cmd_goal(args),
            "/good": self._cmd_good,
            "/bad": self._cmd_bad,
            "/procedure": lambda: self._cmd_procedure(args),
            "/agents": lambda: self._cmd_agents(args),
        }
        h = handlers.get(cmd)
        if h:
            try:
                h()
            except EOFError:
                self.is_running = False
        else:
            self.console.print(f"[red]Comando desconocido: {cmd}[/red]")

    def _handle_hot_command(self, command: str):
        """Comandos ejecutables en caliente mientras el modelo genera."""
        parts = command.split()
        cmd = parts[0].lower()
        if cmd == "/stop":
            self._cancel_current_generation("🛑 Generación detenida")
        elif cmd == "/pause":
            self._cmd_pause()
        elif cmd == "/reasoning":
            args = parts[1:] if len(parts) > 1 else []
            self._cmd_reasoning(args)
        elif cmd == "/status":
            self._cmd_status()
        elif cmd == "/compact":
            self._cmd_compact()
        elif cmd == "/verbose":
            self._cmd_verbose()
        elif cmd == "/exit":
            try:
                self.orchestrator.stop()
            except Exception:
                pass
            self.is_running = False
            self._exit_requested = True
        else:
            self.console.print(f"[yellow]Comando '{cmd}' no disponible durante generacion. Usa /stop para cancelar.[/yellow]")

    def _handle_input_during_generation(self) -> Optional[str]:
        """Drena la cola de entrada mientras el modelo genera.

        Devuelve una acción: 'cancel' (detener generación), 'exit' (salir)
        o None (continuar generando). Esto hace que /stop, Ctrl+C y Ctrl+D
        funcionen durante cualquier modo (plan, directo, general, etc.).
        """
        action = None
        while action is None:
            try:
                evt_type, data = self._input_queue.get_nowait()
            except queue.Empty:
                break
            if evt_type == "interrupt":
                action = "cancel"
                break
            if evt_type == "eof":
                self._exit_requested = True
                action = "exit"
                break
            if evt_type == "input_error":
                self.console.print(f"[dim]⚠️ {data}[/dim]")
                continue
            if evt_type == "input":
                txt = (data or "").strip()
                if not txt:
                    continue
                if txt.startswith("/"):
                    low = txt.lower()
                    if low == "/stop":
                        action = "cancel"
                        break
                    if low == "/exit":
                        self._exit_requested = True
                        action = "exit"
                        break
                    self._handle_hot_command(txt)
                else:
                    self.console.print("[yellow]Espera a que termine la generación actual antes de enviar otra tarea.[/yellow]")
        return action

    def _cmd_help(self):
        table = Table(title="Comandos Disponibles", box=ROUNDED)
        table.add_column("Comando", style="cyan")
        table.add_column("Descripción", style="white")
        for c, d in SlashCommandCompleter.COMMANDS.items():
            table.add_row(c, d)
        self.console.print(table)

    def _cmd_status(self):
        agents = self.orchestrator.list_agents()
        text = Text()
        text.append(f"🤖 Agentes activos: {len(agents)}\n", style="bold")
        for a in agents:
            text.append(f"   • {a}\n", style="white")
        if self.daemon and getattr(self.daemon, "is_running", False):
            ds = self.daemon.get_status()
            text.append(f"\n🔄 Daemon: {'🟢 En ejecución' if ds.get('is_running') else '🔴 Detenido'}\n", style="bold")
            text.append(f"   Ciclos: {ds.get('current_cycle', 0)}\n")
            text.append(f"   Completadas: {ds.get('completed_tasks', 0)}\n")
            text.append(f"   Fallidas: {ds.get('failed_tasks', 0)}\n")
        else:
            text.append("\n⏸️ Daemon: No ejecutándose\n", style="dim")
        text.append(f"\n📊 Sesión: {self.session_id or 'Nueva'}\n", style="bold")
        text.append(f"   Tokens: {self.total_tokens:,}\n")
        text.append(f"   Tiempo: {self._get_session_time()}\n")
        text.append(f"   Razonamiento: {'✅ Activado' if self.reasoning_enabled else '❌ Desactivado'}\n")
        text.append(f"   Modo visual: {self.cli_mode}\n", style="bold")
        self.console.print(Panel(text, title="📊 Estado", border_style="cyan"))

    def _cmd_agents(self, args):
        subcommand = args[0].lower() if args else "list"
        if subcommand == "list":
            query = " ".join(args[1:])
            agents = self.agency_catalog.search(query)
            table = Table(title=f"Agentes disponibles ({len(agents)})", box=ROUNDED)
            table.add_column("Nombre", style="cyan")
            table.add_column("Categoría", style="dim")
            table.add_column("Descripción", style="white", max_width=70)
            for agent in agents[:50]:
                table.add_row(agent["name"], agent["category"], agent["description"])
            self.console.print(table)
            if len(agents) > 50:
                self.console.print("[dim]Usa /agents list <búsqueda> para filtrar.[/dim]")
            return
        if subcommand == "clear":
            self.selected_agency_agents = []
            self.console.print("[green]Selección de agentes limpiada.[/green]")
            return
        if subcommand == "select":
            names = " ".join(args[1:]).split(",")
            selected = []
            for name in names:
                agent = self.agency_catalog.get(name)
                if agent and agent["name"] not in selected:
                    selected.append(agent["name"])
            if not selected:
                self.console.print("[red]No se encontraron agentes válidos.[/red]")
                return
            self.selected_agency_agents = selected[:3]
            self.console.print(f"[green]Agentes seleccionados: {', '.join(self.selected_agency_agents)}[/green]")
            return
        self.console.print("[yellow]Uso: /agents list [búsqueda] | /agents select <nombre,nombre> | /agents clear[/yellow]")

    def _cmd_exit(self):
        self.orchestrator.stop()
        self.is_generating = False
        self.is_running = False
        raise EOFError

    def _cmd_pause(self):
        if self.daemon and getattr(self.daemon, "is_running", False):
            self.daemon.pause()
            self.console.print("[yellow]⏸️ Daemon pausado[/yellow]")
        else:
            self.console.print("[red]No hay daemon en ejecución[/red]")

    def _cmd_resume(self):
        if self.daemon and getattr(self.daemon, "is_paused", False):
            self.daemon.resume()
            self.console.print("[green]▶️ Daemon reanudado[/green]")
        else:
            self.console.print("[red]No hay daemon pausado[/red]")

    def _cmd_stop(self):
        self.orchestrator.stop()
        if self.daemon and getattr(self.daemon, "is_running", False):
            self.daemon.stop()
            self.console.print("[red]🛑 Daemon detenido[/red]")
        else:
            self.console.print("[red]🛑 Generación detenida[/red]")

    def _cmd_checkpoints(self):
        cps = self.checkpoint_manager.list_checkpoints()
        if not cps:
            self.console.print("[dim]No hay checkpoints[/dim]")
            return
        table = Table(title="📸 Checkpoints", box=ROUNDED)
        table.add_column("ID", style="cyan")
        table.add_column("Fecha", style="white")
        table.add_column("Descripción", style="white")
        table.add_column("Archivos", style="blue")
        for cp in cps:
            table.add_row(str(cp.get("id", "?")), str(cp.get("timestamp", ""))[:19], str(cp.get("description", "-"))[:40], str(cp.get("files_count", 0)))
        self.console.print(table)

    def _cmd_rollback(self, args):
        if not args:
            self.console.print("[red]Uso: /rollback <id>[/red]")
            return
        r = self.checkpoint_manager.rollback(args[0], "full")
        if r.get("success"):
            self.console.print(f"[green]✅ Rollback desde {args[0]} completado[/green]")
        else:
            self.console.print(f"[red]❌ Rollback fallido: {r.get('errors', ['desconocido'])}[/red]")

    def _cmd_skills(self):
        skills = self.skill_manager.list_skills(include_auto=True)
        if not skills:
            self.console.print("[dim]No hay skills[/dim]")
            return
        table = Table(title="🧠 Skills", box=ROUNDED)
        table.add_column("Nombre", style="cyan")
        table.add_column("Descripción", style="white")
        table.add_column("Confianza", style="blue")
        for sk in skills:
            auto = "[auto]" if sk.get("auto_generated") else "[manual]"
            table.add_row(sk.get("name", "?"), str(sk.get("description", "-"))[:50], f"{sk.get('confidence', 0.5):.2f} {auto}")
        self.console.print(table)

    def _cmd_clear(self):
        os.system("cls" if os.name == "nt" else "clear")
        self._print_banner()

    def _cmd_model(self, args):
        if args:
            self.current_model = args[0]
            self._save_prefs()
            self.console.print(f"[green]Modelo: {self.current_model}[/green]")
        else:
            self.console.print(f"[dim]Modelo actual: {self.current_model}[/dim]")

    def _cmd_reasoning(self, args):
        if args and args[0].lower() == "on":
            self.reasoning_enabled = True
            self.orchestrator.reasoning = True
            # Reiniciar historial de agentes para que tomen el nuevo system prompt
            for agent in self.orchestrator.agents.values():
                agent.messages = []
                agent.reasoning = True
            self._save_prefs()
            self.console.print("[green]🧠 Razonamiento activado[/green]")
        elif args and args[0].lower() == "off":
            self.reasoning_enabled = False
            self.orchestrator.reasoning = False
            for agent in self.orchestrator.agents.values():
                agent.messages = []
                agent.reasoning = False
            self._save_prefs()
            self.console.print("[yellow]🧠 Razonamiento desactivado[/yellow]")
        else:
            st = "activado" if self.reasoning_enabled else "desactivado"
            self.console.print(f"[dim]🧠 Razonamiento: {st}[/dim]")

    def _cmd_history(self, args):
        """Buscar en historial de sesión."""
        query = " ".join(args) if args else ""
        if not query:
            results = self.session_history.load_recent_sessions(limit=10)
            self.console.print(f"[dim]Últimas {len(results)} sesiones:[/dim]")
            for path in results:
                self.console.print(f"  📄 {path.name}")
        else:
            results = self.session_history.search_history(query, limit=20)
            if not results:
                self.console.print(f"[dim]No se encontraron resultados para '{query}'[/dim]")
                return
            table = Table(title=f"Búsqueda: {query}", box=ROUNDED)
            table.add_column("Timestamp", style="dim")
            table.add_column("Input", style="cyan")
            table.add_column("Output", style="green")
            for r in results[:10]:
                ts = r.get("timestamp", "")[:19]
                inp = r.get("input", "")[:40]
                out = r.get("output", "")[:40]
                table.add_row(ts, inp, out)
            self.console.print(table)

    def _cmd_tree(self, args):
        """Mostrar árbol de directorios."""
        path = Path(args[0]) if args else self.workspace_path
        if not path.exists():
            self.console.print(f"[red]Ruta no existe: {path}[/red]")
            return
        
        def _tree(p: Path, prefix: str = "", max_depth: int = 3, current_depth: int = 0) -> str:
            if current_depth >= max_depth:
                return ""
            
            items = []
            try:
                for item in sorted(p.iterdir()):
                    if item.name.startswith((".", "__pycache__", ".venv")):
                        continue
                    items.append(item)
            except PermissionError:
                return ""
            
            lines = []
            for i, item in enumerate(items):
                is_last = i == len(items) - 1
                current_prefix = "└── " if is_last else "├── "
                lines.append(prefix + current_prefix + item.name)
                
                if item.is_dir():
                    next_prefix = prefix + ("    " if is_last else "│   ")
                    lines.append(_tree(item, next_prefix, max_depth, current_depth + 1))
            
            return "\n".join(filter(None, lines))
        
        tree_output = _tree(path)
        self.console.print(f"[bold]{path}[/bold]\n{tree_output}")

    def _cmd_diff(self, args):
        """Comparar dos archivos."""
        if len(args) < 2:
            self.console.print("[red]Uso: /diff archivo1 archivo2[/red]")
            return
        
        file1, file2 = Path(args[0]), Path(args[1])
        if not file1.exists() or not file2.exists():
            self.console.print(f"[red]Uno o ambos archivos no existen[/red]")
            return
        
        try:
            with open(file1, 'r', encoding='utf-8') as f:
                lines1 = f.readlines()
            with open(file2, 'r', encoding='utf-8') as f:
                lines2 = f.readlines()
            
            from difflib import unified_diff
            diff_lines = list(unified_diff(lines1, lines2, fromfile=str(file1), tofile=str(file2), lineterm=''))
            # Diff con colores: + verde, - rojo, @ cyan, resto dim
            txt = Text()
            for dl in diff_lines:
                if dl.startswith("+") and not dl.startswith("+++"):
                    txt.append(dl + "\n", style="green")
                elif dl.startswith("-") and not dl.startswith("---"):
                    txt.append(dl + "\n", style="red")
                elif dl.startswith("@"):
                    txt.append(dl + "\n", style="cyan")
                else:
                    txt.append(dl + "\n", style="dim")
            self.console.print(txt)
        except Exception as e:
            self.console.print(f"[red]Error comparando archivos: {e}[/red]")

    def _cmd_stats(self, args):
        """Mostrar estadísticas de sesión."""
        table = Table(title="📊 Estadísticas de Sesión", box=ROUNDED)
        table.add_column("Métrica", style="cyan")
        table.add_column("Valor", style="white")
        
        table.add_row("Sesión ID", self.session_id or "Nueva")
        table.add_row("Tiempo", self._get_session_time())
        table.add_row("Tokens totales", f"{self.total_tokens:,}")
        table.add_row("Tokens entrada", f"{self.input_tokens:,}")
        table.add_row("Tokens salida", f"{self.output_tokens:,}")
        table.add_row("Mensajes", str(len(self.history)))
        
        self.console.print(table)

    def _cmd_compact(self):
        """Alterna modo compacto (solo input/output/errores)."""
        self.cli_mode = "COMPACT" if self.cli_mode != "COMPACT" else "NORMAL"
        self._save_prefs()
        self.console.print(f"[green]✓ Modo visual: {self.cli_mode}[/green]")

    def _cmd_verbose(self):
        """Alterna modo verbose (todo incluido)."""
        self.cli_mode = "VERBOSE" if self.cli_mode != "VERBOSE" else "NORMAL"
        self._save_prefs()
        self.console.print(f"[green]✓ Modo visual: {self.cli_mode}[/green]")

    def _cmd_metrics(self):
        """Muestra métricas de ejecución detalladas."""
        if self.metrics_collector is None:
            self.console.print("[yellow]Métricas no disponibles[/yellow]")
            return
        try:
            summary = self.metrics_collector.get_summary()
            recent = self.metrics_collector.get_recent_events(limit=15)
            table = Table(title="📈 Métricas de Ejecución", box=ROUNDED)
            table.add_column("Métrica", style="cyan")
            table.add_column("Valor", style="white")
            table.add_row("Tareas", str(summary.get("total_tasks", 0)))
            table.add_row("Tool calls", str(summary.get("total_tool_calls", 0)))
            table.add_row("Iteraciones", str(summary.get("total_iterations", 0)))
            table.add_row("Tokens totales", str(summary.get("total_tokens", 0)))
            table.add_row("Tiempo total", f"{summary.get('total_time_ms', 0):.0f} ms")
            table.add_row("Tiempo medio/tarea", f"{summary.get('average_task_time_ms', 0):.0f} ms")
            table.add_row("Tasa de éxito", f"{summary.get('success_rate_percent', 0):.1f}%")
            table.add_row("Éxitos/Fallos", f"{summary.get('success_count', 0)}/{summary.get('failure_count', 0)}")
            self.console.print(table)
            if recent:
                lines = [""]
                for ev in recent[-8:]:
                    lines.append(f"  [{ev.level}] {ev.component}/{ev.event} · {ev.timestamp:.0f}s")
                self.console.print(Text("\n".join(lines), style="dim"))
        except Exception as e:
            self.console.print(f"[red]Error leyendo métricas: {e}[/red]")

    def _cmd_export(self, args):
        """Exporta datos: /export session|metrics|skills"""
        what = args[0].lower() if args else "session"
        export_dir = USER_DATA_DIR / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            if what == "session":
                data = {
                    "session_id": self.session_id,
                    "created_at": self.session_start.isoformat(),
                    "history": self.history,
                    "total_tokens": self.total_tokens,
                }
                path = export_dir / f"session_{ts}.json"
            elif what == "metrics":
                events = []
                if self.metrics_collector is not None:
                    for ev in self.metrics_collector.get_recent_events(limit=100000):
                        events.append(ev.to_dict())
                data = {"summary": self.metrics_collector.get_summary(), "events": events} if self.metrics_collector else {"events": []}
                path = export_dir / f"metrics_{ts}.json"
            elif what == "skills":
                skills = self.skill_manager.list_skills(include_auto=True) if self.skill_manager else []
                data = {"skills": skills}
                path = export_dir / f"skills_{ts}.json"
            else:
                self.console.print(f"[red]Uso: /export session|metrics|skills[/red]")
                return
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self.console.print(f"[green]✓ Exportado a {path}[/green]")
        except Exception as e:
            self.console.print(f"[red]Error exportando {what}: {e}[/red]")

    def _cmd_goal(self, args):
        """Inicia el daemon con un objetivo: /goal <texto>"""
        if not args:
            self.console.print("[red]Uso: /goal <objetivo>[/red]")
            return
        try:
            from .daemon_agent import start_daemon, get_daemon_instance
            objective = " ".join(args)
            msg = start_daemon(
                objective,
                str(self.workspace_path),
                event_bus=self._daemon_event_bus,
            )
            self.daemon = get_daemon_instance()
            self.console.print(f"[green]{msg}[/green]")
        except Exception as e:
            self.console.print(f"[red]Error iniciando daemon: {e}[/red]")

    def _cmd_good(self):
        self._record_feedback(1)

    def _cmd_bad(self):
        self._record_feedback(-1)

    def _record_feedback(self, rating: int):
        """Registra feedback de la última tarea (/good = 1, /bad = -1)."""
        try:
            fb_file = USER_DATA_DIR / "feedback.json"
            records = []
            if fb_file.exists():
                try:
                    records = json.loads(fb_file.read_text(encoding="utf-8"))
                except Exception:
                    records = []
            records.append({
                "rating": rating,
                "task": self._last_task_text,
                "timestamp": datetime.now().isoformat(),
            })
            fb_file.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            label = "👍 Útil" if rating > 0 else "👎 No útil"
            self.console.print(f"[green]{label} — feedback registrado[/green]")
        except Exception as e:
            self.console.print(f"[red]Error registrando feedback: {e}[/red]")

    # ========================================================================
    # COMANDO /procedure
    # ========================================================================
    def _cmd_procedure(self, args):
        """Gestiona procedimientos: /procedure list|save|run|delete|show"""
        if not args:
            self.console.print("[yellow]Uso: /procedure list|save|run|delete|show[/yellow]")
            return
        subcmd = args[0].lower()
        try:
            from .procedures import ProcedureStore, ProcedureEngine, Procedure
        except ImportError:
            from tools.procedures import ProcedureStore, ProcedureEngine, Procedure

        store = ProcedureStore(str(self.workspace_path))

        if subcmd == "list":
            procs = store.list_all()
            if not procs:
                self.console.print("[dim]No hay procedimientos guardados[/dim]")
                return
            table = Table(title="📋 Procedimientos", box=ROUNDED)
            table.add_column("Nombre", style="cyan")
            table.add_column("Pasos", style="white")
            table.add_column("Descripción", style="white")
            table.add_column("Ejecuciones", style="blue")
            table.add_column("Auto", style="dim")
            for p in procs:
                auto = "🤖" if p.auto_generated else "✋"
                table.add_row(p.name, str(len(p.steps)), p.description[:40], str(p.execution_count), auto)
            self.console.print(table)

        elif subcmd == "save":
            if len(args) < 2:
                self.console.print("[yellow]Uso: /procedure save <nombre>[/yellow]")
                return
            name = args[1]
            # Crear un procedure de ejemplo con los últimos tool calls
            if not self.history:
                self.console.print("[yellow]No hay historial para guardar como procedimiento[/yellow]")
                return
            # Extraer tool calls del historial reciente
            recent_tool_calls = []
            for entry in self.history[-10:]:
                if entry.get("tool_calls"):
                    recent_tool_calls.extend(entry["tool_calls"])
            if not recent_tool_calls:
                self.console.print("[yellow]No se encontraron tool calls recientes para guardar[/yellow]")
                return
            proc = Procedure(
                name=name,
                params=[],
                steps=[{"action": "tool_call", "tool": tc.get("tool", ""), "args": tc.get("arguments", {}), "description": tc.get("description", "")} for tc in recent_tool_calls],
                description=f"Procedimiento guardado manualmente con {len(recent_tool_calls)} pasos",
            )
            store.save(proc)
            self.console.print(f"[green]✅ Procedimiento '{name}' guardado con {len(recent_tool_calls)} pasos[/green]")

        elif subcmd == "run":
            if len(args) < 2:
                self.console.print("[yellow]Uso: /procedure run <nombre> [param=value ...][/yellow]")
                return
            proc_name = args[1]
            proc = store.get(proc_name)
            if not proc:
                self.console.print(f"[red]Procedimiento '{proc_name}' no encontrado[/red]")
                return
            # Parsear variables: param=value
            variables = {}
            for arg in args[2:]:
                if "=" in arg:
                    k, v = arg.split("=", 1)
                    variables[k] = v
            engine = ProcedureEngine()
            self.console.print(f"[cyan]🔄 Ejecutando procedimiento '{proc_name}' ({len(proc.steps)} pasos)...[/cyan]")
            results = engine.execute(proc, variables)
            # Actualizar contadores
            proc.execution_count += 1
            proc.last_run = time.time()
            store.save(proc)
            # Mostrar resultados
            for r in results:
                status = r.get("status", "unknown")
                icon = "✅" if status == "success" else "❌" if status == "error" else "⏭️"
                self.console.print(f"  {icon} {r.get('step', '?')}: {status}")
                if r.get("result"):
                    self.console.print(f"     {r['result'][:100]}")

        elif subcmd == "delete":
            if len(args) < 2:
                self.console.print("[yellow]Uso: /procedure delete <nombre>[/yellow]")
                return
            if store.delete(args[1]):
                self.console.print(f"[green]✅ Procedimiento '{args[1]}' eliminado[/green]")
            else:
                self.console.print(f"[red]Procedimiento '{args[1]}' no encontrado[/red]")

        elif subcmd == "show":
            if len(args) < 2:
                self.console.print("[yellow]Uso: /procedure show <nombre>[/yellow]")
                return
            proc = store.get(args[1])
            if not proc:
                self.console.print(f"[red]Procedimiento '{args[1]}' no encontrado[/red]")
                return
            text = Text()
            text.append(f"Nombre: {proc.name}\n", style="bold cyan")
            text.append(f"Descripción: {proc.description}\n", style="white")
            text.append(f"Parámetros: {', '.join(proc.params) if proc.params else 'ninguno'}\n", style="dim")
            text.append(f"Pasos: {len(proc.steps)}\n", style="dim")
            text.append(f"Ejecuciones: {proc.execution_count}\n", style="dim")
            text.append(f"Auto-generado: {'Sí' if proc.auto_generated else 'No'}\n", style="dim")
            text.append("\nPasos:\n", style="bold")
            for i, step in enumerate(proc.steps, 1):
                tool = step.get("tool", "?")
                desc = step.get("description", "")
                args_str = json.dumps(step.get("args", {}), ensure_ascii=False)
                if len(args_str) > 80:
                    args_str = args_str[:77] + "..."
                text.append(f"  {i}. {tool} — {desc}\n", style="white")
                text.append(f"     args: {args_str}\n", style="dim")
            self.console.print(Panel(text, title=f"📋 Procedimiento: {proc.name}", border_style="cyan"))

        else:
            self.console.print(f"[red]Subcomando desconocido: {subcmd}[/red]")

    # ========================================================================
    # ENVÍO DE MENSAJES
    # ========================================================================
    def _send_message(self, message: str):
        if self.is_generating:
            self.console.print("[yellow]Espera a que termine la generacion actual...[/yellow]")
            return
        self.is_generating = True
        self.orchestrator.reset_cancel()
        self._reasoning_buffer = ""
        self._stop_live()
        self._router_phase = False
        self._tool_suppress = ""
        self._stream_pending = ""
        self._at_line_start = True
        self._progress_state = None
        self._task_incomplete_reason = None
        with self._queue_lock:
            self._event_queue.clear()
        self.history.append({"role": "user", "content": message, "time": datetime.now().isoformat()})
        self._last_task_text = message
        self._task_start_time = time.time()
        self._task_input_tokens = 0
        self._task_output_tokens = 0
        self._tool_timing = None
        if self.metrics_collector is not None:
            try:
                self.metrics_collector.record_task_start(message)
            except Exception:
                pass
        bus = EventBus()
        bus.on(self._on_event)

        result_container = {"text": ""}
        def _task():
            try:
                if self.selected_agency_agents:
                    resp = self.orchestrator.run_task_stream(
                        message,
                        bus=bus,
                        reasoning=self.reasoning_enabled,
                        selected_agents=self.selected_agency_agents,
                    )
                else:
                    resp = self.orchestrator.run_task_stream(
                        message,
                        bus=bus,
                        reasoning=self.reasoning_enabled
                    )
                result_container["text"] = resp
            except Exception as e:
                bus.emit(AgentEvent("error", str(e)))
            finally:
                bus.emit(AgentEvent("done", {}))

        thread = threading.Thread(target=_task, daemon=True, name="agent-task")
        thread.start()
        try:
            self._render_loop(bus, thread)
        finally:
            # Remove listener to avoid leak and ensure orchestrator state reset
            try:
                bus.off(self._on_event)
            except Exception:
                pass
            self._reasoning_buffer = ""
            self._stop_live()
            # Esperar a que el hilo de tarea termine (cancelación limpia:
            # evita ejecutar herramientas desde contenido parcial truncado)
            try:
                thread.join(timeout=5)
            except Exception:
                pass
            self.is_generating = False
            # Métricas de fin de tarea + notificación IPC al Manager
            if self.metrics_collector is not None:
                try:
                    duration_ms = (time.time() - (self._task_start_time or time.time())) * 1000
                    self.metrics_collector.record_task_end(
                        message, duration_ms, success=not self.orchestrator._cancelled.is_set(),
                        result=(result_container.get("text") or "")[:200],
                    )
                except Exception:
                    pass
            self._send_ipc("task_completed", {"task_id": self.session_id or "", "task": message[:200]})
        final = result_container["text"]
        if final:
            self.history.append({"role": "assistant", "content": final, "time": datetime.now().isoformat()})
        self._save_session()

    def _on_event(self, event: AgentEvent):
        with self._queue_lock:
            self._event_queue.append(event)

    def _queue_daemon_event(self, event: AgentEvent):
        """Entrega eventos del daemon al bucle interactivo de la CLI."""
        if self.is_running and not self.headless:
            self._input_queue.put(("daemon_event", event))


    @staticmethod
    def _looks_like_tool_call_start(s: str) -> bool:
        """¿Algún sufijo de s es prefijo de execute_tool( o tool_call(? — detecta
        una tool call partida por cortes de chunk del stream."""
        for marker in ("execute_tool(", "tool_call("):
            for i in range(1, min(len(marker), len(s)) + 1):
                if s.endswith(marker[:i]):
                    return True
        return False

    def _stop_live(self):
        """Detiene el Live activo. Con transient=False, el estado final queda
        impreso como contenido estático (la caja de razonamiento o la última
        línea de progreso permanecen en el scrollback)."""
        if self._live_reasoning is not None:
            try:
                self._live_reasoning.stop()
            except Exception:
                pass
            self._live_reasoning = None
            self._live_kind = None

    def _show_live(self, kind: str, renderable):
        """Inicia o actualiza una región en vivo. Durante la generación hay un
        ÚNICO escritor (el prompt está oculto), así que rich.Live nunca pelea
        con prompt_toolkit y no puede corromper la salida."""
        if self._live_reasoning is not None and self._live_kind == kind:
            try:
                self._live_reasoning.update(renderable)
                return
            except Exception:
                # El Live activo falló (refresh thread muerto, consola rara):
                # recrearlo en vez de quedarnos con un panel congelado que
                # hace que el razonamiento deje de verse en el stream.
                try:
                    self._live_reasoning.stop()
                except Exception:
                    pass
                self._live_reasoning = None
                self._live_kind = None
        self._stop_live()
        self._live_reasoning = Live(
            renderable,
            console=self.console,
            refresh_per_second=8,
            transient=False,
        )
        self._live_kind = kind
        try:
            self._live_reasoning.start()
        except Exception:
            self._live_reasoning = None
            self._live_kind = None

    def _progress_renderable(self, p: dict) -> Text:
        percent = p.get("percent")
        elapsed = float(p.get("elapsed") or 0.0)
        prompt_tokens = int(p.get("prompt_tokens") or 0)
        n_past = int(p.get("n_past") or 0)
        if percent is not None:
            width = 20
            filled = int(max(0.0, min(1.0, float(percent))) * width)
            bar = "█" * filled + "░" * (width - filled)
            toks = f" · {n_past}/{prompt_tokens} tokens" if prompt_tokens else ""
            return Text(f"⌛ Procesando prompt {bar} {percent * 100:.0f}% · {elapsed:.1f}s{toks}", style="cyan")
        return Text(f"⏳ Esperando al modelo… {elapsed:.1f}s", style="cyan")

    def _render_loop(self, bus: EventBus, thread: threading.Thread):
        displayed_text = ""
        natural_end = False
        router_phase = False

        def ensure_line_start():
            """Evita que un panel se imprima a mitad de una línea de texto streamed."""
            if not self._at_line_start:
                self.console.print()
                self._at_line_start = True

        def stop_with(reason: str):
            self._cancel_current_generation(reason)

        try:
            while thread.is_alive() or self._event_queue:
                if self._force_quit or self._exit_requested:
                    break
                if self._cancel_requested:
                    self._cancel_requested = False
                    stop_with("🛑 Cancelado por Ctrl+C")
                    break
                if self.orchestrator._cancelled.is_set():
                    break

                # Comandos en caliente durante la generación (/stop, Ctrl+C, Ctrl+D)
                action = self._handle_input_during_generation()
                if action == "exit":
                    # Detener también el hilo de tarea antes de salir
                    try:
                        self.orchestrator.stop()
                    except Exception:
                        pass
                    break
                if action == "cancel":
                    stop_with("🛑 Generación detenida")
                    break

                with self._queue_lock:
                    events = self._event_queue[:]
                    self._event_queue.clear()

                for evt in events:
                    if self._force_quit or self._exit_requested:
                        break
                    if self._cancel_requested or self.orchestrator._cancelled.is_set():
                        break

                    # Métricas en vivo (tool calls, errores)
                    if self.metrics_collector is not None:
                        try:
                            if evt.type == "tool_call":
                                self._tool_timing = {"name": evt.payload.get("name", "tool"), "start": time.time()}
                            elif evt.type == "tool_result":
                                if self._tool_timing is not None:
                                    dur_ms = (time.time() - self._tool_timing["start"]) * 1000
                                    raw = str(evt.payload.get("result", "")).lower()
                                    ok = not (raw.startswith("error") or "❌" in raw)
                                    self.metrics_collector.record_tool_call(self._tool_timing["name"], dur_ms, ok)
                                    self._tool_timing = None
                            elif evt.type == "error":
                                self.metrics_collector.record_error("agent", str(evt.payload))
                        except Exception:
                            pass

                    # Modo compacto: solo texto y errores
                    if self.cli_mode == "COMPACT" and evt.type in (
                        "reasoning", "tool_call", "tool_result",
                        "step_start", "step_end", "router", "progress"
                    ):
                        continue
                    if evt.type == "reasoning" and not self.reasoning_enabled:
                        continue

                    if evt.type == "progress":
                        p = evt.payload if isinstance(evt.payload, dict) else {}
                        self._progress_state = dict(p)
                        self._progress_state["ts"] = time.time()
                        # Progreso en UNA línea viva (en sitio, porcentual): Live
                        # de una sola línea, sin líneas nuevas repetidas. Solo si
                        # no hay una caja de razonamiento activa (el poller para
                        # en el primer token).
                        if self._live_kind != "reasoning":
                            self._show_live("progress", self._progress_renderable(p))
                        continue

                    if evt.type == "router":
                        status = evt.payload.get("status", "") if isinstance(evt.payload, dict) else ""
                        if status == "analyzing":
                            router_phase = True
                        elif status == "decided":
                            router_phase = False
                            self._progress_state = None
                        self._stop_live()
                        ensure_line_start()
                        r = EventRenderer.render(evt)
                        if r is not None:
                            self.console.print(r)
                        continue

                    if evt.type == "reasoning":
                        self._progress_state = None
                        self._reasoning_buffer += str(evt.payload or "")
                        # Tope para no redibujar cajas gigantes (rendimiento)
                        if len(self._reasoning_buffer) > 3000:
                            self._reasoning_buffer = "…" + self._reasoning_buffer[-3000:]
                        panel = Panel(
                            self._reasoning_buffer.strip(),
                            title="🧠 Razonamiento",
                            border_style="bright_black",
                            style="grey50",
                            padding=(0, 1),
                        )
                        self._show_live("reasoning", panel)
                        continue

                    # Cualquier otro evento cierra el bloque en vivo actual: la
                    # caja/progreso queda impresa como contenido estático.
                    self._stop_live()
                    self._at_line_start = True

                    if evt.type == "text":
                        payload = str(evt.payload or "")
                        if not payload:
                            continue
                        # Marcadores internos "[Modo X] Ejecutando..." → estilo dim
                        if re.match(r"^\[Modo [A-Za-z ]+\] ", payload):
                            ensure_line_start()
                            self.console.print(Text(payload, style="italic dim"), end="")
                            self._at_line_start = False
                            continue
                        # --- Supresión de tool calls por estado ---
                        # Desde el primer execute_tool(/tool_call( se retiene
                        # todo hasta que el evento tool_call la consuma (el panel
                        # Herramienta ya muestra la llamada). Funciona aunque el
                        # stream corte la llamada en trozos (ex|ecute_tool|...).
                        if self._tool_suppress:
                            self._tool_suppress = (self._tool_suppress + payload)[-6000:]
                            continue
                        pending = self._stream_pending + payload
                        m = re.search(r"execute_tool\(|tool_call\(", pending)
                        if m is not None:
                            prefix = pending[:m.start()]
                            if prefix:
                                self.console.print(Text(prefix, style="white"), end="")
                                self._at_line_start = False
                                displayed_text = (displayed_text + prefix)[-8000:]
                            self._tool_suppress = pending[m.start():]
                            self._stream_pending = ""
                            continue
                        if pending and not self._looks_like_tool_call_start(pending):
                            # Texto normal: stream token a token (el prompt está
                            # oculto durante la generación → sin restricciones).
                            self.console.print(Text(pending, style="white"), end="")
                            self._at_line_start = False
                            displayed_text = (displayed_text + pending)[-8000:]
                            self._stream_pending = ""
                        else:
                            # Podría ser el inicio de una tool call partida por
                            # chunks: retener solo el posible prefijo.
                            self._stream_pending = pending[-14:]
                        continue

                    if evt.type == "tool_call":
                        self._progress_state = None
                        # El texto crudo retenido (execute_tool(...)) se descarta:
                        # el panel Herramienta ya muestra la llamada.
                        self._tool_suppress = ""

                    if evt.type == "incomplete":
                        reason = ""
                        if isinstance(evt.payload, dict):
                            reason = str(evt.payload.get("reason", "") or "")
                        else:
                            reason = str(evt.payload or "")
                        self._task_incomplete_reason = reason or "pasos fallidos sin autocorrección exitosa"
                        continue

                    if evt.type == "done":
                        content = ""
                        if isinstance(evt.payload, dict):
                            content = str(evt.payload.get("content", "") or "").strip()
                        else:
                            content = str(evt.payload or "").strip()
                        if not content:
                            continue
                        shown = displayed_text.strip()
                        if shown and (content == shown or shown.endswith(content) or (len(content) > 40 and content in shown)):
                            # El contenido ya se transmitió en vivo → sin duplicar
                            continue
                        r = EventRenderer.render(evt)
                        if r is not None:
                            self.console.print(r)
                        continue

                    r = EventRenderer.render(evt)
                    if r is not None:
                        if isinstance(r, Text):
                            self.console.print(r, end="")
                        else:
                            self.console.print(r)

                    if evt.type == "usage" and isinstance(evt.payload, dict):
                        self.total_tokens += evt.payload.get("total_tokens", 0)
                        self.input_tokens += evt.payload.get("prompt_tokens", 0)
                        self.output_tokens += evt.payload.get("completion_tokens", 0)
                        self._task_input_tokens += evt.payload.get("prompt_tokens", 0)
                        self._task_output_tokens += evt.payload.get("completion_tokens", 0)

                time.sleep(0.01)
            else:
                # El hilo de tarea terminó y la cola se drenó sin interrupción
                natural_end = True
        except KeyboardInterrupt:
            stop_with("🛑 Cancelado por Ctrl+C")
        finally:
            self._stop_live()
            self._reasoning_buffer = ""
            # Texto pendiente retenido por posible inicio de tool call: se
            # imprime al final (nunca se pierde texto normal).
            if self._stream_pending and not self._cancel_requested and not self._exit_requested:
                try:
                    self.console.print(Text(self._stream_pending, style="white"), end="")
                    displayed_text = (displayed_text + self._stream_pending)[-8000:]
                    self._stream_pending = ""
                except Exception:
                    pass
            # Texto retenido como tool call que NUNCA se ejecutó (no llegó el
            # evento tool_call): no se pierde, se muestra como texto normal.
            if self._tool_suppress and not self._cancel_requested and not self._exit_requested:
                try:
                    self.console.print(Text(self._tool_suppress, style="white"), end="")
                    self._tool_suppress = ""
                except Exception:
                    pass
            ensure_line_start()
            self._progress_state = None
        if natural_end and not self._exit_requested:
            ensure_line_start()
            extra = ""
            if self._task_input_tokens or self._task_output_tokens:
                extra = f"  ·  📥 {self._task_input_tokens:,} in   📤 {self._task_output_tokens:,} out"
            if self._task_incomplete_reason:
                self.console.print(Panel(
                    f"⚠️ Tarea incompleta: {self._task_incomplete_reason}{extra}",
                    border_style="yellow",
                    padding=(0, 1),
                ))
            else:
                self.console.print(Panel(f"✅ Tarea completada{extra}", border_style="green", padding=(0, 1)))
        self.console.print()






    def _run_headless(self, message: Optional[str] = None):
        if not message:
            self.console.print("[red]Error: proporciona un mensaje[/red]")
            sys.exit(1)
        self._reasoning_buffer = ""
        self._stop_live()
        self._router_phase = False
        self._tool_suppress = ""
        self._stream_pending = ""
        self._at_line_start = True
        self._progress_state = None
        self._task_incomplete_reason = None
        self._task_input_tokens = 0
        self._task_output_tokens = 0
        self.console.print(f"[dim]Ejecutando: {message}[/dim]\n")
        bus = EventBus()
        bus.on(self._on_event)
        result_container = {"text": ""}
        def _task():
            try:
                result_container["text"] = self.orchestrator.run_task_stream(
                    message, bus=bus, reasoning=self.reasoning_enabled
                )
            except Exception as e:
                bus.emit(AgentEvent("error", str(e)))
            finally:
                bus.emit(AgentEvent("done", {}))
        thread = threading.Thread(target=_task, daemon=True)
        thread.start()
        try:
            self._render_loop(bus, thread)
        finally:
            try:
                bus.off(self._on_event)
            except Exception:
                pass

    # ========================================================================
    # BARRA DE ESTADO
    # ========================================================================
    def _toolbar_html(self) -> HTML:
        """Toolbar dinámica del prompt: muestra el progreso del prompt
        processing en UNA sola línea (en sitio, la renderiza prompt_toolkit)
        y, si no hay progreso activo, el estado de la sesión."""
        p = getattr(self, "_progress_state", None)
        if p and isinstance(p, dict) and (time.time() - p.get("ts", 0)) < 3.0:
            percent = p.get("percent")
            elapsed = float(p.get("elapsed") or 0.0)
            prompt_tokens = int(p.get("prompt_tokens") or 0)
            n_past = int(p.get("n_past") or 0)
            if percent is not None:
                width = 20
                filled = int(max(0.0, min(1.0, float(percent))) * width)
                bar = "█" * filled + "░" * (width - filled)
                toks = f" · {n_past}/{prompt_tokens} tokens" if prompt_tokens else ""
                return HTML(f"<b>⌛ Procesando prompt</b> {bar} {percent * 100:.0f}% · {elapsed:.1f}s{toks}")
            return HTML(f"<b>⏳ Esperando al modelo…</b> {elapsed:.1f}s")
        return self._get_status_bar_html()

    def _get_status_bar_html(self) -> HTML:
        elapsed = self._get_session_time()
        tokens = f"{self.total_tokens:,}" if self.total_tokens > 0 else "0"
        reasoning = "🧠 ON" if self.reasoning_enabled else "🧠 OFF"
        daemon = "🔄" if self.daemon and getattr(self.daemon, "is_running", False) else "⏸️"
        gen = "⚡" if self.is_generating else "○"
        hint = "  |  <b>🛑 /stop para cancelar</b>" if self.is_generating else ""
        return HTML(
            f"<b>Modelo:</b> {self.current_model} | "
            f"<b>Tokens:</b> {tokens} | "
            f"<b>Tiempo:</b> {elapsed} | "
            f"<b>Razonamiento:</b> {reasoning} | "
            f"<b>Modo:</b> {self.cli_mode} | "
            f"{daemon} {gen}{hint}"
        )

    # ========================================================================
    # SESIONES
    # ========================================================================
    def _load_session(self):
        self.session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.session_file = SESSIONS_DIR / f"{self.session_id}.json"
        self._save_session()

    def _save_session(self):
        if not self.session_file:
            return
        try:
            data = {
                "session_id": self.session_id,
                "created_at": self.session_start.isoformat(),
                "history": self.history,
                "total_tokens": self.total_tokens,
                "reasoning_enabled": self.reasoning_enabled
            }
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Save session error: {e}")

    def _get_session_time(self) -> str:
        d = datetime.now() - self.session_start
        h, rem = divmod(int(d.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _detect_model(self) -> str:
        try:
            sp = USER_DATA_DIR / "status.json"
            if sp.exists():
                with open(sp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("model") or data.get("current_model") or "llama"
        except Exception:
            pass
        return "llama"

    # ========================================================================
    # UI: BANNER
    # ========================================================================
    def _print_banner(self):
        banner = f"""
[bold cyan]🐧 AGENT CLI v{VERSION}[/bold cyan]

[dim]Asistente autónomo para desarrollo y automatización.[/dim]
[dim]• Escribe una tarea para iniciar la generación[/dim]
[dim]• Usa /help para ver comandos[/dim]
[dim]• Ctrl+C cancela la tarea actual (2× fuerza salida) · Ctrl+D sale del CLI[/dim]
[dim]• /stop durante la generación cancela · /compact y /verbose cambian la vista[/dim]

[bold green]Workspace:[/bold green] {self.workspace_path}
[bold green]Modelo:[/bold green] {self.current_model}
[bold green]Razonamiento:[/bold green] {'✅ Activado' if self.reasoning_enabled else '❌ Desactivado'}
        """
        self.console.print(Panel(banner, title="🚀 Bienvenido", border_style="cyan", padding=(1, 2)))

# =============================================================================
# 6. ENTRY POINT
# =============================================================================
def build_arg_parser():
    """Parser CLI del agente.

    Expuesto aparte para que los tests puedan comprobar el contrato de banderas
    que usa el Manager (`--run-cli --workspace ...`) sin arrancar el CLI.
    """
    parser = argparse.ArgumentParser(description="Agent CLI v3")
    parser.add_argument("--workspace", "-w", help="Ruta al workspace")
    parser.add_argument("--run", "-r", nargs="*", help="Modo headless")
    parser.add_argument("--no-progress", action="store_true",
                        help="Desactiva el poller de /slots (útil si el stream se corta aleatoriamente)")
    return parser


def main():
    args = build_arg_parser().parse_args()

    # --- NUEVO: precarga de Laya en el proceso del CLI ---
    # Cada proceso tiene su propio singleton; el del manager no sirve aquí.
    try:
        from .laya_onnx_router import preload_laya_if_enabled
        info = preload_laya_if_enabled()
        if info["loaded"]:
            msg = (f"🧠 Laya cargado en {info['seconds']:.2f}s"
                   if info["seconds"] else "🧠 Laya ya estaba cargado")
            print(f"[dim]{msg}[/dim]", file=sys.stderr)
    except Exception:
        pass  # el CLI no debe morir por Laya

    try:
        if args.run is not None:
            cli = AgentCLI(workspace_path=args.workspace, headless=True)
            cli.run(message=" ".join(args.run))
            return
        cli = AgentCLI(workspace_path=args.workspace, headless=False)
        if getattr(args, 'no_progress', False):
            cli.orchestrator.llm_client.disable_progress_poller = True
            cli.console.print("[dim]Poller de progreso desactivado (--no-progress)[/dim]")
        cli.run()
    except KeyboardInterrupt:
        print("\n[dim]Interrupción del terminal. Cerrando CLI...[/dim]")
        sys.exit(0)
    except EOFError:
        sys.exit(0)

if __name__ == "__main__":
    main()