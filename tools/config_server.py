"""
Config Server - Servidor HTTP para gestionar configuración desde la WebUI
Puerto: 5001 (configurable)
Endpoints:
  GET  /api/config - Obtener configuración actual (workspace, global_mode)
  POST /api/config - Actualizar configuración
  GET  /api/select_workspace - Abrir diálogo y devolver ruta seleccionada
"""

import json
import logging
import threading
import socket
import os
from pathlib import Path
from typing import Dict, Optional, Callable, Any
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

logger = logging.getLogger("config_server")

# ============================================================================
# HANDLER HTTP PERSONALIZADO
# ============================================================================

class ConfigHTTPHandler(BaseHTTPRequestHandler):
    """Handler HTTP para la API de configuración."""
    
    # Referencia al servidor que contiene los callbacks
    config_server = None
    
    def do_GET(self):
        """Maneja peticiones GET."""
        if not self.config_server:
            self.send_error(500)
            return
        
        path = self.path.split('?')[0]
        
        if path == "/api/config":
            self._handle_get_config()
        elif path == "/api/select_workspace":
            self._handle_select_workspace()
        elif path == "/api/telegram":
            self._handle_get_telegram()
        else:
            self.send_error(404)
    
    def do_POST(self):
        """Maneja peticiones POST."""
        if not self.config_server:
            self.send_error(500)
            return
        
        path = self.path
        
        if path == "/api/config":
            self._handle_post_config()
        elif path == "/api/resolve_workspace":
            self._handle_post_resolve_workspace()
        elif path == "/api/telegram":
            self._handle_post_telegram()
        elif path == "/api/telegram/pairing":
            self._handle_post_telegram_pairing()
        else:
            self.send_error(404)
    
    def do_DELETE(self):
        """Maneja peticiones DELETE."""
        if not self.config_server:
            self.send_error(500)
            return
        
        path = self.path
        
        if path == "/api/telegram":
            self._handle_delete_telegram()
        else:
            self.send_error(404)
    
    def do_OPTIONS(self):
        """Maneja preflight de CORS."""
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()
    
    def _handle_get_config(self):
        """GET /api/config - Devuelve la configuración actual."""
        try:
            config = self.config_server.get_config()
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            
            self.wfile.write(json.dumps(config).encode('utf-8'))
        except Exception as e:
            logger.error(f"Error en GET /api/config: {e}")
            self.send_error(500, str(e))
    
    def _handle_post_config(self):
        """POST /api/config - Actualiza la configuración."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            result = self.config_server.update_config(data)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            
            self.wfile.write(json.dumps(result).encode('utf-8'))
        except Exception as e:
            logger.error(f"Error en POST /api/config: {e}")
            self.send_error(500, str(e))
    
    def _handle_select_workspace(self):
        """GET /api/select_workspace - Abre diálogo de selección de carpeta."""
        try:
            if not self.config_server.select_workspace_callback:
                self.send_error(501, "Selección de workspace no implementada")
                return
            
            # Ejecutar callback en el hilo principal (si es necesario)
            path = self.config_server.select_workspace_callback()
            
            if path:
                response = {"path": path}
                status = 200
            else:
                response = {"error": "Selección cancelada"}
                status = 200  # No es error, es cancelación
            
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            
            self.wfile.write(json.dumps(response).encode('utf-8'))
        except Exception as e:
            logger.error(f"Error en GET /api/select_workspace: {e}")
            self.send_error(500, str(e))
    
    def _handle_post_resolve_workspace(self):
        """
        POST /api/resolve_workspace - Resuelve el nombre de una carpeta a una
        ruta absoluta, buscándola en el workspace actual (padre) y en
        ubicaciones comunes del usuario.
        Body JSON: { folder_name: str, base_path?: str }
        """
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            data = json.loads(body) if body else {}

            folder_name = (data.get('folder_name') or '').strip()
            base_path = (data.get('base_path') or '').strip()

            if not folder_name:
                self._send_json(400, {"error": "folder_name requerido"})
                return

            resolved = ConfigHTTPHandler._resolve_folder_path(folder_name, base_path)
            if not resolved:
                self._send_json(404, {
                    "error": f"No se encontró la carpeta '{folder_name}' en las ubicaciones conocidas."
                })
                return

            self._send_json(200, {"path": resolved})
        except Exception as e:
            logger.error(f"Error en POST /api/resolve_workspace: {e}")
            self._send_json(500, {"error": str(e)})
    
    @staticmethod
    def _resolve_folder_path(folder_name: str, base_path: str) -> Optional[str]:
        """
        Busca una carpeta por nombre en ubicaciones candidatas y devuelve
        la primera coincidencia que exista en disco.

        Orden de búsqueda:
          1. base_path/<folder_name>     (workspace actual)
          2. base_path/../<folder_name>  (padre del workspace)
          3. ~/Documents/<folder_name>
          4. ~/Desktop/<folder_name>
          5. ~/<folder_name>             (home)
          6. CWD/<folder_name>
          7. Cualquier unidad montada en Windows (C:\, D:\, ...)
        """
        folder_name = folder_name.replace('/', '').replace('\\', '').strip()
        if not folder_name:
            return None

        candidates: list[Path] = []

        if base_path:
            bp = Path(base_path)
            candidates.append(bp / folder_name)
            if bp.parent != bp:
                candidates.append(bp.parent / folder_name)

        home = Path.home()
        candidates.extend([
            home / "Documents" / folder_name,
            home / "Desktop" / folder_name,
            home / folder_name,
        ])

        try:
            candidates.append(Path.cwd() / folder_name)
        except Exception:
            pass

        # En Windows, buscar también en las raíces de unidades disponibles.
        if os.name == 'nt':
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                root = Path(f"{letter}:/")
                if root.exists():
                    candidates.append(root / folder_name)

        seen: set[str] = set()
        for cand in candidates:
            try:
                key = str(cand).lower()
                if key in seen:
                    continue
                seen.add(key)
                if cand.is_dir():
                    return str(cand.resolve())
            except (OSError, ValueError):
                continue

        return None
    
    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode('utf-8'))
    
    # ---------------------------------------------------------------------------
    # ENDPOINTS DE TELEGRAM (/api/telegram)
    # ---------------------------------------------------------------------------
    
    def _handle_get_telegram(self):
        """GET /api/telegram - Devuelve la configuración de Telegram."""
        try:
            config = self.config_server.get_telegram_config()
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            
            self.wfile.write(json.dumps(config).encode('utf-8'))
        except Exception as e:
            logger.error(f"Error en GET /api/telegram: {e}")
            self.send_error(500, str(e))
    
    def _handle_post_telegram(self):
        """POST /api/telegram - Actualiza la configuración de Telegram."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}
            
            result = self.config_server.update_telegram_config(data)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            
            self.wfile.write(json.dumps(result).encode('utf-8'))
        except Exception as e:
            logger.error(f"Error en POST /api/telegram: {e}")
            self.send_error(500, str(e))
    
    def _handle_post_telegram_pairing(self):
        """POST /api/telegram/pairing - Genera un código de emparejamiento."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}
            
            result = self.config_server.request_pairing(data)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            
            self.wfile.write(json.dumps(result).encode('utf-8'))
        except Exception as e:
            logger.error(f"Error en POST /api/telegram/pairing: {e}")
            self.send_error(500, str(e))
    
    def _handle_delete_telegram(self):
        """DELETE /api/telegram - Desactiva el bot de Telegram."""
        try:
            result = self.config_server.update_telegram_config({"enabled": False})
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_cors_headers()
            self.end_headers()
            
            self.wfile.write(json.dumps(result).encode('utf-8'))
        except Exception as e:
            logger.error(f"Error en DELETE /api/telegram: {e}")
            self.send_error(500, str(e))
    
    def _set_cors_headers(self):
        """Añade headers CORS."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
    
    def log_message(self, format, *args):
        """Sobrescribe log_message para usar nuestro logger."""
        logger.debug(format % args)

# ============================================================================
# SERVIDOR DE CONFIGURACIÓN
# ============================================================================

class ConfigServer:
    """Servidor HTTP para gestionar configuración."""
    
    def __init__(
        self,
        port: int = 5001,
        get_config_callback: Optional[Callable[[], Dict]] = None,
        update_config_callback: Optional[Callable[[Dict], Dict]] = None,
        select_workspace_callback: Optional[Callable[[], Optional[str]]] = None,
        get_telegram_config_callback: Optional[Callable[[], Dict]] = None,
        update_telegram_config_callback: Optional[Callable[[Dict], Dict]] = None,
        request_pairing_callback: Optional[Callable[[Dict], Dict]] = None
    ):
        self.port = port
        self.get_config_callback = get_config_callback
        self.update_config_callback = update_config_callback
        self.select_workspace_callback = select_workspace_callback
        self.get_telegram_config_callback = get_telegram_config_callback
        self.update_telegram_config_callback = update_telegram_config_callback
        self.request_pairing_callback = request_pairing_callback
        
        self.server = None
        self._thread = None
        self.is_running = False
    
    def start(self) -> bool:
        """Inicia el servidor HTTP."""
        try:
            # Verificar que el puerto esté disponible
            if not self._is_port_available(self.port):
                logger.error(f"Puerto {self.port} no disponible.")
                return False
            
            # Crear servidor
            self.server = HTTPServer(("0.0.0.0", self.port), ConfigHTTPHandler)
            ConfigHTTPHandler.config_server = self
            
            # Iniciar en hilo separado
            self._thread = threading.Thread(target=self._run_server, daemon=True)
            self._thread.start()
            
            self.is_running = True
            logger.info(f"✅ Servidor de configuración iniciado en puerto {self.port}")
            return True
        
        except Exception as e:
            logger.error(f"❌ Error iniciando servidor de configuración: {e}")
            return False
    
    def stop(self):
        """Detiene el servidor HTTP."""
        if self.server:
            self.is_running = False
            self.server.shutdown()
            self.server.server_close()
            if self._thread:
                self._thread.join(timeout=2)
            logger.info("🛑 Servidor de configuración detenido")
    
    def _run_server(self):
        """Bucle del servidor HTTP."""
        if self.server:
            try:
                self.server.serve_forever()
            except Exception as e:
                logger.error(f"Error en servidor HTTP: {e}")
    
    def get_config(self) -> Dict[str, Any]:
        """Obtiene la configuración actual."""
        if self.get_config_callback:
            return self.get_config_callback()
        return {}
    
    def update_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Actualiza la configuración."""
        if self.update_config_callback:
            return self.update_config_callback(data)
        return {"error": "Callback no configurado"}
    
    def resolve_workspace_folder(self, folder_name: str, base_path: str) -> Optional[str]:
        """Resuelve una carpeta por nombre a una ruta absoluta en disco."""
        return ConfigHTTPHandler._resolve_folder_path(folder_name, base_path)
    
    def get_telegram_config(self) -> Dict[str, Any]:
        """Obtiene la configuración de Telegram."""
        if self.get_telegram_config_callback:
            return self.get_telegram_config_callback()
        return {"enabled": False, "status": "stopped"}
    
    def update_telegram_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Actualiza la configuración de Telegram."""
        if self.update_telegram_config_callback:
            return self.update_telegram_config_callback(data)
        return {"error": "Callback no configurado"}
    
    def request_pairing(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Genera un código de emparejamiento para un usuario."""
        if self.request_pairing_callback:
            return self.request_pairing_callback(data)
        return {"error": "Callback no configurado"}
    
    @staticmethod
    def _is_port_available(port: int) -> bool:
        """Verifica si un puerto está disponible."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("0.0.0.0", port))
            sock.close()
            return True
        except OSError:
            return False

# ============================================================================
# INTERFAZ GLOBAL
# ============================================================================

_config_server: Optional[ConfigServer] = None

def start_config_server(
    port: int = 5001,
    get_config_callback: Optional[Callable[[], Dict]] = None,
    update_config_callback: Optional[Callable[[Dict], Dict]] = None,
    select_workspace_callback: Optional[Callable[[], Optional[str]]] = None,
    get_telegram_config_callback: Optional[Callable[[], Dict]] = None,
    update_telegram_config_callback: Optional[Callable[[Dict], Dict]] = None,
    request_pairing_callback: Optional[Callable[[Dict], Dict]] = None
) -> bool:
    """Inicia el servidor de configuración global."""
    global _config_server
    
    if _config_server and _config_server.is_running:
        logger.warning("Servidor de configuración ya está en ejecución.")
        return False
    
    _config_server = ConfigServer(
        port=port,
        get_config_callback=get_config_callback,
        update_config_callback=update_config_callback,
        select_workspace_callback=select_workspace_callback,
        get_telegram_config_callback=get_telegram_config_callback,
        update_telegram_config_callback=update_telegram_config_callback,
        request_pairing_callback=request_pairing_callback
    )
    
    return _config_server.start()

def stop_config_server():
    """Detiene el servidor de configuración global."""
    global _config_server
    
    if _config_server:
        _config_server.stop()
        _config_server = None

def get_config_server() -> Optional[ConfigServer]:
    """Obtiene la instancia del servidor de configuración."""
    return _config_server

def is_config_server_running() -> bool:
    """Verifica si el servidor está en ejecución."""
    return _config_server is not None and _config_server.is_running
