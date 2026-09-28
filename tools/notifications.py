"""
tools/notifications.py

Gestor de Notificaciones (Tarea 3.3 - Fase 3).
Maneja múltiples canales: desktop, telegram, log, CLI.
"""

import os
import time
import logging
import threading
from typing import Dict, List, Optional, Callable
from enum import Enum
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("notifications")


class NotificationLevel(Enum):
    """Niveles de notificación."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


class NotificationChannel(Enum):
    """Canales de entrega."""
    DESKTOP = "desktop"
    TELEGRAM = "telegram"
    LOG = "log"
    CLI = "cli"


@dataclass
class Notification:
    """Representa una notificación."""
    title: str
    message: str
    level: NotificationLevel
    timestamp: float
    channels: List[NotificationChannel]
    event_type: str = "generic"  # "task_completed", "error", "goal_reached", "sensor_triggered"
    metadata: Dict = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.timestamp == 0:
            self.timestamp = time.time()


class NotificationManager:
    """Gestor centralizado de notificaciones."""
    
    def __init__(self, workspace_path: str, throttle_interval: int = 60):
        """
        Args:
            workspace_path: Ruta del workspace
            throttle_interval: Segundos mínimos entre notificaciones del mismo tipo
        """
        self.workspace_path = Path(workspace_path)
        self.throttle_interval = throttle_interval
        self._last_notification: Dict[str, float] = {}
        self._notification_queue: List[Notification] = []
        self._lock = threading.Lock()
        self._callbacks: Dict[str, List[Callable]] = {}
        
        # Configuración de canales
        self.enabled_channels: Dict[NotificationChannel, bool] = {
            NotificationChannel.LOG: True,
            NotificationChannel.CLI: True,
            NotificationChannel.DESKTOP: False,
            NotificationChannel.TELEGRAM: False
        }
        
        self.telegram_config: Optional[Dict] = None
        self._load_config()
    
    def _load_config(self):
        """Carga configuración de notificaciones."""
        config_file = self.workspace_path / "user_data" / "notification_config.json"
        if config_file.exists():
            try:
                import json
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                
                for channel_name in config.get("enabled_channels", []):
                    try:
                        channel = NotificationChannel[channel_name.upper()]
                        self.enabled_channels[channel] = True
                    except KeyError:
                        logger.warning(f"Canal desconocido: {channel_name}")
                
                self.telegram_config = config.get("telegram", {})
            except Exception as e:
                logger.warning(f"Error cargando config: {e}")
    
    def notify(self, title: str, message: str, 
               level: NotificationLevel = NotificationLevel.INFO,
               event_type: str = "generic",
               channels: Optional[List[NotificationChannel]] = None) -> bool:
        """
        Envía una notificación.
        
        Returns:
            bool: True si se envió, False si fue throttled
        """
        if channels is None:
            channels = [ch for ch, enabled in self.enabled_channels.items() if enabled]
        
        # Throttling
        throttle_key = f"{event_type}:{title}"
        last_time = self._last_notification.get(throttle_key, 0)
        if time.time() - last_time < self.throttle_interval:
            logger.debug(f"Notificación throttled: {throttle_key}")
            return False
        
        self._last_notification[throttle_key] = time.time()
        
        notification = Notification(
            title=title,
            message=message,
            level=level,
            timestamp=time.time(),
            channels=channels,
            event_type=event_type
        )
        
        with self._lock:
            self._notification_queue.append(notification)
        
        # Enviar inmediatamente
        self._send_notification(notification)
        
        # Trigger callbacks
        self._trigger_callbacks(event_type, notification)
        
        return True
    
    def notify_task_completed(self, task_name: str, duration: float, result: str = ""):
        """Notifica que una tarea larga se completó."""
        message = f"Tarea '{task_name}' completada en {duration:.1f}s"
        if result:
            message += f": {result}"
        
        self.notify(
            title="✓ Tarea Completada",
            message=message,
            level=NotificationLevel.SUCCESS,
            event_type="task_completed",
            channels=[NotificationChannel.CLI, NotificationChannel.LOG]
        )
    
    def notify_error(self, component: str, error: str):
        """Notifica un error crítico."""
        self.notify(
            title="⚠ Error Crítico",
            message=f"[{component}] {error}",
            level=NotificationLevel.ERROR,
            event_type="error",
            channels=[NotificationChannel.LOG, NotificationChannel.CLI]
        )
    
    def notify_goal_reached(self, goal_name: str, progress: float = 1.0) -> bool:
        """Notifica que una meta fue alcanzada."""
        return self.notify(
            title="🎯 Meta Alcanzada",
            message=f"'{goal_name}' completada al {progress*100:.0f}%",
            level=NotificationLevel.SUCCESS,
            event_type="goal_reached",
            channels=[NotificationChannel.CLI, NotificationChannel.DESKTOP]
        )
    
    def notify_sensor_triggered(self, sensor_name: str, event_type: str, data: Optional[Dict] = None) -> bool:
        """Notifica que un sensor fue activado."""
        message = f"{sensor_name}: {event_type}"
        if data:
            details = data.get("modified") or data.get("task_id") or data
            if isinstance(details, (list, dict)):
                message = f"{message} | {details}"
            else:
                message = f"{message} | {details}"
        return self.notify(
            title="📡 Sensor Activado",
            message=message,
            level=NotificationLevel.INFO,
            event_type="sensor_triggered",
            channels=[NotificationChannel.LOG, NotificationChannel.CLI]
        )
    
    def notify_goal_failed(self, goal_name: str, reason: str = "") -> bool:
        """Notifica que una meta no se pudo alcanzar."""
        message = f"'{goal_name}' no se pudo completar"
        if reason:
            message += f": {reason}"
        
        return self.notify(
            title="❌ Meta Fallida",
            message=message,
            level=NotificationLevel.WARNING,
            event_type="goal_failed",
            channels=[NotificationChannel.CLI, NotificationChannel.LOG]
        )
    
    def _send_notification(self, notification: Notification):
        """Envía la notificación a los canales habilitados."""
        for channel in notification.channels:
            if not self.enabled_channels.get(channel, False):
                continue
            
            try:
                if channel == NotificationChannel.LOG:
                    self._send_to_log(notification)
                elif channel == NotificationChannel.CLI:
                    self._send_to_cli(notification)
                elif channel == NotificationChannel.DESKTOP:
                    self._send_to_desktop(notification)
                elif channel == NotificationChannel.TELEGRAM:
                    self._send_to_telegram(notification)
            except Exception as e:
                logger.error(f"Error enviando a {channel.value}: {e}")
    
    def _send_to_log(self, notification: Notification):
        """Envía a log."""
        log_level = {
            NotificationLevel.INFO: logging.INFO,
            NotificationLevel.WARNING: logging.WARNING,
            NotificationLevel.ERROR: logging.ERROR,
            NotificationLevel.SUCCESS: logging.INFO
        }
        
        level = log_level.get(notification.level, logging.INFO)
        logger.log(level, f"{notification.title}: {notification.message}")
    
    def _send_to_cli(self, notification: Notification):
        """Envía a CLI (stdout)."""
        icons = {
            NotificationLevel.INFO: "ℹ",
            NotificationLevel.WARNING: "⚠",
            NotificationLevel.ERROR: "✗",
            NotificationLevel.SUCCESS: "✓"
        }
        icon = icons.get(notification.level, "•")
        timestamp = datetime.fromtimestamp(notification.timestamp).strftime("%H:%M:%S")
        print(f"[{timestamp}] {icon} {notification.title}: {notification.message}")
    
    def _send_to_desktop(self, notification: Notification):
        """Envía notificación desktop (Windows/Linux/Mac)."""
        try:
            import sys
            if sys.platform == "win32":
                # Windows: usar pywin32 o notify-send via WSL
                self._send_to_desktop_windows(notification)
            elif sys.platform == "darwin":
                # macOS: usar osascript
                self._send_to_desktop_macos(notification)
            else:
                # Linux: usar notify-send
                self._send_to_desktop_linux(notification)
        except Exception as e:
            logger.warning(f"Desktop notification failed: {e}")
    
    def _send_to_desktop_windows(self, notification: Notification):
        """Notificación Windows.

        Se prefieren `winotify`/`notifypy` (notificaciones nativas modernas,
        compatibles con Windows 10/11) y se deja `win10toast` como último
        recurso para entornos antiguos.

        El toast se lanza en un hilo aparte: `winotify`/`notifypy` pueden
        bloquearse en sesiones no interactivas (servicios, tests), y notificar
        nunca debe congelar a quien llama.
        """
        threading.Thread(
            target=self._send_windows_toast,
            args=(notification,),
            name="desktop-notification",
            daemon=True,
        ).start()

    def _send_windows_toast(self, notification: Notification):
        if self._windows_toast("winotify", notification):
            return
        if self._windows_toast("notifypy", notification):
            return
        try:
            from win10toast import ToastNotifier
            toaster = ToastNotifier()
            toaster.show_toast(
                notification.title,
                notification.message,
                duration=10,
                threaded=True
            )
        except ImportError:
            logger.debug("win10toast no instalado")

    def _windows_toast(self, backend: str, notification: Notification) -> bool:
        """Intenta enviar el toast con un backend concreto. True si lo logró."""
        try:
            if backend == "winotify":
                from winotify import Notification as WinNotification

                toast = WinNotification(
                    app_id="Llama Web UI",
                    title=notification.title,
                    msg=notification.message,
                    duration="short",
                )
                toast.show()
            else:
                from notifypy import Notify as NotifyPy

                toast = NotifyPy()
                toast.title = notification.title
                toast.message = notification.message
                toast.send()
            return True
        except ImportError:
            logger.debug(f"{backend} no instalado")
            return False
        except Exception as exc:  # un backend roto no debe impedir el siguiente
            logger.debug(f"{backend} falló: {exc}")
            return False
    
    def _send_to_desktop_macos(self, notification: Notification):
        """Notificación macOS."""
        import os
        os.system(f'osascript -e \'display notification "{notification.message}" with title "{notification.title}\'')
    
    def _send_to_desktop_linux(self, notification: Notification):
        """Notificación Linux."""
        import os
        os.system(f'notify-send "{notification.title}" "{notification.message}"')
    
    def _send_to_telegram(self, notification: Notification):
        """Envía a Telegram."""
        if not self.telegram_config or not self.telegram_config.get("token"):
            logger.warning("Telegram no configurado")
            return
        
        try:
            import requests
            token = self.telegram_config["token"]
            chat_id = self.telegram_config.get("chat_id")
            
            if not chat_id:
                logger.warning("Chat ID de Telegram no configurado")
                return
            
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            message = f"*{notification.title}*\n{notification.message}"
            
            requests.post(url, json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }, timeout=5)
        except Exception as e:
            logger.error(f"Error enviando Telegram: {e}")
    
    def register_callback(self, event_type: str, callback: Callable[[Notification], None]):
        """Registra un callback para un tipo de evento."""
        if event_type not in self._callbacks:
            self._callbacks[event_type] = []
        self._callbacks[event_type].append(callback)
    
    def _trigger_callbacks(self, event_type: str, notification: Notification):
        """Ejecuta callbacks registrados."""
        callbacks = self._callbacks.get(event_type, [])
        for callback in callbacks:
            try:
                callback(notification)
            except Exception as e:
                logger.error(f"Error en callback: {e}")
    
    def get_notification_history(self, limit: int = 50) -> List[Notification]:
        """Retorna historial de notificaciones."""
        with self._lock:
            return self._notification_queue[-limit:]
    
    def enable_channel(self, channel: NotificationChannel):
        """Habilita un canal."""
        self.enabled_channels[channel] = True
    
    def disable_channel(self, channel: NotificationChannel):
        """Deshabilita un canal."""
        self.enabled_channels[channel] = False
