# tools/ipc.py
# Comunicación Inter-Proceso entre Manager y CLI v4

import os
import sys
import json
import socket
import threading
import queue
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class IPCMessageType(Enum):
    """Tipos de mensajes IPC."""
    # Manager -> CLI
    MODEL_CHANGED = "model_changed"
    SERVER_RESTARTED = "server_restarted"
    SERVER_STOPPED = "server_stopped"
    STATUS_UPDATE = "status_update"
    
    # CLI -> Manager
    TASK_COMPLETED = "task_completed"
    ERROR_CRITICAL = "error_critical"
    STOP_REQUEST = "stop_request"
    
    # Bidireccional
    PING = "ping"
    PONG = "pong"


class IPCMessage:
    """Estructura de mensaje IPC."""
    
    def __init__(self, msg_type: IPCMessageType, data: Optional[Dict[str, Any]] = None):
        self.type = msg_type
        self.data = data or {}
        self.timestamp = datetime.now().isoformat()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte mensaje a diccionario."""
        return {
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp
        }
    
    def to_json(self) -> str:
        """Convierte mensaje a JSON."""
        return json.dumps(self.to_dict(), ensure_ascii=False)
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'IPCMessage':
        """Crea mensaje desde diccionario."""
        msg_type = IPCMessageType(data.get("type", "ping"))
        return IPCMessage(msg_type, data.get("data", {}))
    
    @staticmethod
    def from_json(json_str: str) -> 'IPCMessage':
        """Crea mensaje desde JSON."""
        data = json.loads(json_str)
        return IPCMessage.from_dict(data)


class IPCServer:
    """Servidor IPC para recibir mensajes."""
    
    def __init__(self, socket_path: Optional[str] = None):
        self.socket_path = socket_path or self._get_default_socket_path()
        self.socket = None
        self.is_running = False
        self.message_queue: queue.Queue = queue.Queue()
        self.callbacks: Dict[IPCMessageType, list] = {}
        self._thread = None
    
    def _get_default_socket_path(self) -> str:
        """Obtiene la ruta de socket por defecto según el SO."""
        if sys.platform == "win32":
            # Named Pipe en Windows
            return r"\\.\pipe\LlamaAgentIPC"
        else:
            # Unix socket en Linux/Mac
            return f"/tmp/llama_agent_ipc_{os.getuid()}.sock"
    
    def start(self):
        """Inicia el servidor IPC."""
        self.is_running = True
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        logger.info(f"IPC Server iniciado en {self.socket_path}")
    
    def stop(self):
        """Detiene el servidor IPC."""
        self.is_running = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        logger.info("IPC Server detenido")
    
    def _run_server(self):
        """Loop principal del servidor."""
        try:
            if sys.platform == "win32":
                self._run_named_pipe_server()
            else:
                self._run_unix_socket_server()
        except Exception as e:
            logger.error(f"Error en IPC Server: {e}")
    
    def _run_unix_socket_server(self):
        """Implementación para Unix socket."""
        import socket
        
        # Limpiar socket anterior si existe
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(self.socket_path)
        self.socket.listen(1)
        
        while self.is_running:
            try:
                conn, _ = self.socket.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True
                ).start()
            except OSError:
                break
    
    def _run_named_pipe_server(self):
        """Implementación para Named Pipe (Windows)."""
        import win32pipe
        import win32file
        import pywintypes
        
        while self.is_running:
            try:
                handle = win32pipe.CreateNamedPipe(
                    self.socket_path,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    65536,
                    65536,
                    0,
                    None
                )
                
                win32pipe.ConnectNamedPipe(handle, None)
                threading.Thread(
                    target=self._handle_pipe,
                    args=(handle,),
                    daemon=True
                ).start()
            except pywintypes.error:
                break
    
    def _handle_client(self, conn):
        """Maneja cliente Unix socket."""
        try:
            while self.is_running:
                data = conn.recv(4096).decode('utf-8')
                if not data:
                    break
                self._process_message(data)
        except Exception as e:
            logger.error(f"Error manejando cliente: {e}")
        finally:
            conn.close()
    
    def _handle_pipe(self, handle):
        """Maneja cliente Named Pipe."""
        import win32file
        try:
            while self.is_running:
                _, data = win32file.ReadFile(handle, 4096)
                if data:
                    self._process_message(data.decode('utf-8'))
        except Exception as e:
            logger.error(f"Error manejando pipe: {e}")
    
    def _process_message(self, json_str: str):
        """Procesa mensaje recibido."""
        try:
            msg = IPCMessage.from_json(json_str)
            self.message_queue.put(msg)
            
            # Ejecutar callbacks registrados
            if msg.type in self.callbacks:
                for callback in self.callbacks[msg.type]:
                    try:
                        callback(msg)
                    except Exception as e:
                        logger.error(f"Error en callback: {e}")
        except json.JSONDecodeError:
            logger.error(f"Mensaje JSON inválido: {json_str}")
    
    def register_callback(self, msg_type: IPCMessageType, callback: Callable):
        """Registra callback para tipo de mensaje."""
        if msg_type not in self.callbacks:
            self.callbacks[msg_type] = []
        self.callbacks[msg_type].append(callback)
    
    def get_message(self, timeout: Optional[float] = None) -> Optional[IPCMessage]:
        """Obtiene mensaje de la cola."""
        try:
            return self.message_queue.get(timeout=timeout)
        except queue.Empty:
            return None


class IPCClient:
    """Cliente IPC para enviar mensajes."""
    
    def __init__(self, socket_path: Optional[str] = None):
        self.socket_path = socket_path or self._get_default_socket_path()
        self.socket = None
    
    def _get_default_socket_path(self) -> str:
        """Obtiene la ruta de socket por defecto según el SO."""
        if sys.platform == "win32":
            return r"\\.\pipe\LlamaAgentIPC"
        else:
            return f"/tmp/llama_agent_ipc_{os.getuid()}.sock"
    
    def connect(self) -> bool:
        """Conecta al servidor IPC."""
        try:
            if sys.platform == "win32":
                import win32file
                self.socket = win32file.CreateFile(
                    self.socket_path,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None
                )
                return True
            else:
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.socket.connect(self.socket_path)
                return True
        except Exception as e:
            logger.error(f"Error conectando a IPC: {e}")
            return False
    
    def send(self, msg: IPCMessage) -> bool:
        """Envía mensaje al servidor."""
        if not self.socket:
            if not self.connect():
                return False
        
        try:
            json_data = msg.to_json()
            if sys.platform == "win32":
                import win32file
                win32file.WriteFile(self.socket, json_data.encode('utf-8'))
            else:
                self.socket.sendall(json_data.encode('utf-8'))
            return True
        except Exception as e:
            logger.error(f"Error enviando mensaje: {e}")
            return False
    
    def close(self):
        """Cierra conexión."""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass


class SharedState:
    """Estado compartido entre Manager y CLI."""
    
    def __init__(self, state_file: Optional[Path] = None):
        self.state_file = state_file or Path("app_data/shared_state.json")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict[str, Any] = self._load_state()
        self._lock = threading.Lock()
    
    def _load_state(self) -> Dict[str, Any]:
        """Carga estado compartido desde archivo."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error cargando estado: {e}")
        
        return {
            "current_model": "default",
            "server_status": "stopped",
            "server_pid": None,
            "last_update": datetime.now().isoformat(),
            "cli_pid": None,
            "cli_status": "idle"
        }
    
    def update(self, key: str, value: Any):
        """Actualiza clave en estado."""
        with self._lock:
            self.state[key] = value
            self.state["last_update"] = datetime.now().isoformat()
            self._save_state()
    
    def get(self, key: str, default: Any = None) -> Any:
        """Obtiene valor de estado."""
        with self._lock:
            return self.state.get(key, default)
    
    def _save_state(self):
        """Guarda estado a archivo."""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error guardando estado: {e}")


class IPCBridge:
    """Puente de comunicación bidireccional Manager ↔ CLI."""
    
    def __init__(self, role: str = "cli", socket_path: Optional[str] = None):
        """
        Inicializa el puente IPC.
        
        Args:
            role: "cli" para CLI o "manager" para Manager
            socket_path: Ruta de socket personalizada
        """
        self.role = role
        self.shared_state = SharedState()
        
        if role == "manager":
            self.server = IPCServer(socket_path)
            self.client = None
        else:
            self.server = None
            self.client = IPCClient(socket_path)
    
    def start(self):
        """Inicia el puente."""
        if self.role == "manager" and self.server:
            self.server.start()
            # Registrar callbacks para mensajes del CLI
            self.server.register_callback(IPCMessageType.TASK_COMPLETED, self._on_task_completed)
            self.server.register_callback(IPCMessageType.ERROR_CRITICAL, self._on_error_critical)
    
    def send_model_changed(self, new_model: str):
        """Notifica cambio de modelo al CLI."""
        msg = IPCMessage(
            IPCMessageType.MODEL_CHANGED,
            {"new_model": new_model}
        )
        if self.client:
            self.client.send(msg)
        self.shared_state.update("current_model", new_model)
    
    def send_server_restarted(self):
        """Notifica reinicio de servidor."""
        msg = IPCMessage(
            IPCMessageType.SERVER_RESTARTED,
            {"server_pid": os.getpid()}
        )
        if self.client:
            self.client.send(msg)
        self.shared_state.update("server_status", "running")
    
    def send_task_completed(self, task_id: str, result: Any):
        """Notifica tarea completada al Manager."""
        msg = IPCMessage(
            IPCMessageType.TASK_COMPLETED,
            {"task_id": task_id, "result": result}
        )
        if self.client:
            self.client.send(msg)
    
    def send_critical_error(self, error_msg: str):
        """Notifica error crítico al Manager."""
        msg = IPCMessage(
            IPCMessageType.ERROR_CRITICAL,
            {"error": error_msg}
        )
        if self.client:
            self.client.send(msg)
    
    def _on_task_completed(self, msg: IPCMessage):
        """Callback cuando se completa tarea desde CLI."""
        logger.info(f"Tarea completada desde CLI: {msg.data.get('task_id')}")
    
    def _on_error_critical(self, msg: IPCMessage):
        """Callback para error crítico desde CLI."""
        logger.error(f"Error crítico desde CLI: {msg.data.get('error')}")
    
    def stop(self):
        """Detiene el puente."""
        if self.server:
            self.server.stop()
        if self.client:
            self.client.close()


# Test básico
if __name__ == "__main__":
    print("Testing IPC module...")
    
    # Test IPCMessage
    msg = IPCMessage(IPCMessageType.MODEL_CHANGED, {"new_model": "test"})
    print(f"✓ IPCMessage: {msg.to_json()}")
    
    # Test SharedState
    state = SharedState()
    state.update("test_key", "test_value")
    assert state.get("test_key") == "test_value"
    print("✓ SharedState funciona")
    
    # Test IPCBridge
    bridge = IPCBridge("cli")
    print("✓ IPCBridge inicializado")
    
    print("\n✅ Módulo IPC listo")
