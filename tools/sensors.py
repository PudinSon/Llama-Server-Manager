"""
tools/sensors.py

Sensores para el Daemon Inteligente (Fase 3).
Los sensores monitorean el workspace y generan triggers para tareas automáticas.

Sensores Implementados:
- FileSystemSensor: Watchdog de directorios
- TimeSensor: Scheduling cron-like
- URLSensor: Polling de endpoints
- LLMSensor: Generación proactiva de tareas
"""

import os
import time
import threading
import logging
import requests
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger("sensors")


@dataclass
class SensorEvent:
    """Evento generado por un sensor."""
    sensor_name: str
    event_type: str  # "file_changed", "time_trigger", "url_changed", "llm_task"
    timestamp: float
    data: Dict[str, Any]
    action: str  # Acción recomendada para el daemon
    priority: str = "medium"  # low, medium, high, urgent


class BaseSensor(ABC):
    """Clase base abstracta para sensores."""
    
    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.callbacks: List[Callable[[SensorEvent], None]] = []
    
    @abstractmethod
    def check(self) -> Optional[SensorEvent]:
        """Verifica el sensor y retorna un evento si hay cambios."""
        pass
    
    def register_callback(self, callback: Callable[[SensorEvent], None]):
        """Registra un callback para eventos."""
        self.callbacks.append(callback)
    
    def emit_event(self, event: SensorEvent):
        """Emite un evento a todos los callbacks registrados."""
        for callback in self.callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Error en callback de {self.name}: {e}")
    
    def start_polling(self, interval: int = 60):
        """Inicia polling en segundo plano."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning(f"{self.name} ya está en polling")
            return
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._polling_loop, args=(interval,), daemon=True)
        self._thread.start()
        logger.info(f"{self.name} iniciado (polling cada {interval}s)")
    
    def stop_polling(self):
        """Detiene el polling."""
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=5)
            logger.info(f"{self.name} detenido")
    
    def _polling_loop(self, interval: int):
        """Loop de polling."""
        while not self._stop_event.is_set():
            try:
                if self.enabled:
                    event = self.check()
                    if event:
                        self.emit_event(event)
            except Exception as e:
                logger.error(f"Error en polling de {self.name}: {e}")
            
            # Sleep con interrupción
            self._stop_event.wait(timeout=interval)


class FileSystemSensor(BaseSensor):
    """
    Monitorea cambios en el filesystem.
    Detecta: creación, modificación, eliminación de archivos.
    """
    
    def __init__(self, watch_path: str, extensions: Optional[List[str]] = None):
        super().__init__("FileSystemSensor")
        self.watch_path = Path(watch_path)
        self.extensions = extensions or [".py", ".txt", ".json", ".md"]
        self._file_hashes: Dict[str, int] = {}
        self._scan_initial()
    
    def _get_file_hash(self, path: Path) -> int:
        """Retorna un hash simple de la modificación del archivo."""
        try:
            return int(path.stat().st_mtime * 1000)
        except (FileNotFoundError, OSError):
            return 0
    
    def _scan_initial(self):
        """Escanea el directorio inicial."""
        if not self.watch_path.exists():
            return
        
        for file_path in self.watch_path.rglob("*"):
            if file_path.is_file() and self._matches_extensions(file_path):
                self._file_hashes[str(file_path)] = self._get_file_hash(file_path)
    
    def _matches_extensions(self, path: Path) -> bool:
        """Verifica si el archivo coincide con las extensiones monitoreadas."""
        return any(str(path).endswith(ext) for ext in self.extensions)
    
    def check(self) -> Optional[SensorEvent]:
        """Detecta cambios en el filesystem."""
        if not self.watch_path.exists():
            return None
        
        changes = {"created": [], "modified": [], "deleted": []}
        current_files = {}
        
        # Scan actual
        for file_path in self.watch_path.rglob("*"):
            if file_path.is_file() and self._matches_extensions(file_path):
                path_str = str(file_path)
                current_hash = self._get_file_hash(file_path)
                current_files[path_str] = current_hash
                
                if path_str not in self._file_hashes:
                    changes["created"].append(path_str)
                elif self._file_hashes[path_str] != current_hash:
                    changes["modified"].append(path_str)
        
        # Detectar eliminados
        for path_str in self._file_hashes:
            if path_str not in current_files:
                changes["deleted"].append(path_str)
        
        # Actualizar hash
        self._file_hashes = current_files
        
        # Si hay cambios, generar evento
        if any(changes.values()):
            action = "reindex_workspace"
            if changes["modified"]:
                # Si requirements.txt cambió, ejecutar pip install
                if any("requirements.txt" in p for p in changes["modified"]):
                    action = "update_dependencies"
            
            return SensorEvent(
                sensor_name=self.name,
                event_type="file_changed",
                timestamp=time.time(),
                data=changes,
                action=action,
                priority="high" if "requirements" in str(changes.get("modified", [])) else "medium"
            )
        
        return None


class TimeSensor(BaseSensor):
    """
    Scheduling cron-like.
    Dispara tareas en horarios específicos.
    """
    
    def __init__(self):
        super().__init__("TimeSensor")
        self.tasks: List[Dict[str, Any]] = []
        self._last_triggered: Dict[str, float] = {}
    
    def add_task(self, task_id: str, description: str, 
                 hour: int = None, day_of_week: int = None, 
                 interval_seconds: int = None, priority: str = "medium"):
        """
        Añade una tarea programada.
        
        Args:
            task_id: Identificador único
            description: Descripción de la tarea
            hour: Hora del día (0-23), None para cualquier hora
            day_of_week: Día de la semana (0=Lunes, 6=Domingo), None para cualquier día
            interval_seconds: Intervalo en segundos (alternativa a hour/day_of_week)
            priority: Prioridad de la tarea
        """
        self.tasks.append({
            "id": task_id,
            "description": description,
            "hour": hour,
            "day_of_week": day_of_week,
            "interval_seconds": interval_seconds,
            "priority": priority
        })
        logger.info(f"Tarea programada: {task_id} - {description}")
    
    def check(self) -> Optional[SensorEvent]:
        """Verifica si alguna tarea debe ejecutarse."""
        now = datetime.now()
        
        for task in self.tasks:
            task_id = task["id"]
            should_trigger = False
            
            # Verificar interval_seconds
            if task["interval_seconds"]:
                last_trigger = self._last_triggered.get(task_id, 0)
                if time.time() - last_trigger >= task["interval_seconds"]:
                    should_trigger = True
            
            # Verificar hora específica
            elif task["hour"] is not None:
                if now.hour == task["hour"]:
                    last_trigger = self._last_triggered.get(task_id, 0)
                    if last_trigger == 0:
                        should_trigger = True
                    else:
                        # Convertir timestamp a datetime y comparar hora
                        last_trigger_dt = datetime.fromtimestamp(last_trigger)
                        if last_trigger_dt.hour != now.hour or last_trigger_dt.day != now.day:
                            should_trigger = True
            
            # Verificar día de la semana
            if task["day_of_week"] is not None and not should_trigger:
                if now.weekday() == task["day_of_week"]:
                    last_trigger = self._last_triggered.get(task_id, 0)
                    if last_trigger == 0:
                        should_trigger = True
                    else:
                        last_trigger_dt = datetime.fromtimestamp(last_trigger)
                        if last_trigger_dt.day != now.day:
                            should_trigger = True
            
            if should_trigger:
                self._last_triggered[task_id] = time.time()
                return SensorEvent(
                    sensor_name=self.name,
                    event_type="time_trigger",
                    timestamp=time.time(),
                    data={"task_id": task_id, "task": task},
                    action=f"execute_task:{task_id}",
                    priority=task["priority"]
                )
        
        return None


class URLSensor(BaseSensor):
    """
    Polling de URLs para detectar cambios.
    Útil para monitorear repositorios, endpoints, etc.
    """
    
    def __init__(self, timeout: int = 5):
        super().__init__("URLSensor")
        self.endpoints: Dict[str, Dict[str, Any]] = {}
        self.timeout = timeout
        self._last_response: Dict[str, str] = {}
    
    def add_endpoint(self, endpoint_id: str, url: str, description: str,
                     check_field: Optional[str] = None, priority: str = "medium"):
        """
        Añade un endpoint para monitorear.
        
        Args:
            endpoint_id: ID único
            url: URL a monitorear
            description: Descripción
            check_field: Campo JSON a monitorear (None = toda la respuesta)
            priority: Prioridad del evento
        """
        self.endpoints[endpoint_id] = {
            "url": url,
            "description": description,
            "check_field": check_field,
            "priority": priority
        }
        logger.info(f"Endpoint monitorizado: {endpoint_id} - {url}")
    
    def check(self) -> Optional[SensorEvent]:
        """Verifica cambios en los endpoints."""
        for endpoint_id, config in self.endpoints.items():
            try:
                response = requests.get(config["url"], timeout=self.timeout)
                response_text = response.text[:1000]  # Limitar tamaño
                
                if config["check_field"]:
                    try:
                        data = response.json()
                        response_text = str(data.get(config["check_field"], ""))
                    except:
                        pass
                
                last_response = self._last_response.get(endpoint_id, "")
                
                if response_text != last_response:
                    self._last_response[endpoint_id] = response_text
                    
                    return SensorEvent(
                        sensor_name=self.name,
                        event_type="url_changed",
                        timestamp=time.time(),
                        data={
                            "endpoint_id": endpoint_id,
                            "url": config["url"],
                            "status_code": response.status_code,
                            "response": response_text
                        },
                        action=f"review_endpoint:{endpoint_id}",
                        priority=config["priority"]
                    )
            
            except requests.RequestException as e:
                logger.warning(f"Error monitoreando {endpoint_id}: {e}")
        
        return None


class LLMSensor(BaseSensor):
    """
    Genera tareas proactivas basadas en metas del sistema.
    Coordina con el LLM para crear sub-objetivos dinámicos.
    """
    
    def __init__(self, llm_client=None):
        super().__init__("LLMSensor")
        self.llm_client = llm_client
        self.goals: List[Dict[str, Any]] = []
        self._last_evaluation: float = 0
        self.evaluation_interval = 300  # 5 minutos
    
    def add_goal(self, goal_id: str, description: str, frequency: str = "weekly",
                 priority: str = "medium", check_prompt: Optional[str] = None):
        """
        Añade una meta para ser evaluada periódicamente.
        
        Args:
            goal_id: ID único
            description: Descripción de la meta
            frequency: "daily", "weekly", "monthly", "continuous"
            priority: Prioridad
            check_prompt: Prompt personalizado para evaluación
        """
        self.goals.append({
            "id": goal_id,
            "description": description,
            "frequency": frequency,
            "priority": priority,
            "check_prompt": check_prompt
        })
        logger.info(f"Meta agregada: {goal_id} - {description}")
    
    def check(self) -> Optional[SensorEvent]:
        """Evalúa metas y genera tareas proactivas."""
        if not self.goals:
            return None
        
        # Solo evaluar periódicamente
        now = time.time()
        if now - self._last_evaluation < self.evaluation_interval:
            return None
        
        self._last_evaluation = now
        
        # Si hay LLM client, usar para evaluar metas
        if self.llm_client:
            try:
                goal_descriptions = "\n".join([f"- {g['description']}" for g in self.goals])
                prompt = f"""Evalúa estas metas y sugiere una tarea proactiva:
{goal_descriptions}

Responde en JSON: {{"task_id": "...", "description": "...", "priority": "..."}}"""
                
                # Aquí iría una llamada al LLM (pseudocódigo)
                # response = self.llm_client.chat([{"role": "user", "content": prompt}])
                # return parse_llm_response(response)
                pass
            except Exception as e:
                logger.error(f"Error evaluando metas: {e}")
        
        # Fallback: generar tarea genérica
        if self.goals:
            primary_goal = self.goals[0]
            return SensorEvent(
                sensor_name=self.name,
                event_type="llm_task",
                timestamp=time.time(),
                data={
                    "goal_id": primary_goal["id"],
                    "description": primary_goal["description"],
                    "generated_task": f"Evaluar progreso de: {primary_goal['description']}"
                },
                action=f"evaluate_goal:{primary_goal['id']}",
                priority=primary_goal["priority"]
            )
        
        return None


class SensorManager:
    """Gestor centralizado de sensores."""
    
    def __init__(self):
        self.sensors: Dict[str, BaseSensor] = {}
        self.event_queue: List[SensorEvent] = []
        self._lock = threading.Lock()
    
    def register_sensor(self, sensor: BaseSensor) -> BaseSensor:
        """Registra un sensor."""
        self.sensors[sensor.name] = sensor
        sensor.register_callback(self._on_sensor_event)
        logger.info(f"Sensor registrado: {sensor.name}")
        return sensor
    
    def _on_sensor_event(self, event: SensorEvent):
        """Callback interno para eventos de sensores."""
        with self._lock:
            self.event_queue.append(event)
        logger.info(f"Evento capturado: {event.event_type} ({event.sensor_name})")
    
    def get_pending_events(self) -> List[SensorEvent]:
        """Obtiene y limpia la cola de eventos."""
        with self._lock:
            events = self.event_queue[:]
            self.event_queue.clear()
        return events
    
    def start_all_polling(self, interval: int = 60):
        """Inicia polling en todos los sensores."""
        for sensor in self.sensors.values():
            sensor.start_polling(interval)
    
    def stop_all_polling(self):
        """Detiene polling en todos los sensores."""
        for sensor in self.sensors.values():
            sensor.stop_polling()
