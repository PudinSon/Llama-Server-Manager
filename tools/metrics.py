"""
tools/metrics.py

Métricas y Logging Estructurado (Tarea 4.4 - Fase 4).
Registra eventos en formato JSON para análisis y auditoría.
"""

import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
from dataclasses import dataclass, asdict
from enum import Enum

logger = logging.getLogger("metrics")


class EventLevel(Enum):
    """Niveles de evento para logging."""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class MetricEvent:
    """Evento métrico estructurado."""
    timestamp: float
    level: str
    component: str
    event: str
    duration_ms: Optional[float] = None
    tokens_input: Optional[int] = None
    tokens_output: Optional[int] = None
    success: Optional[bool] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a dict para serialización."""
        data = asdict(self)
        data["timestamp_iso"] = datetime.fromtimestamp(self.timestamp).isoformat()
        return data


class MetricsCollector:
    """Colecciona y almacena métricas de ejecución."""
    
    def __init__(self, workspace_path: str):
        self.workspace_path = Path(workspace_path)
        self.log_dir = self.workspace_path / ".metrics"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "metrics.jsonl"
        self._setup_logging()
        
        # Métricas en memoria
        self.events: List[MetricEvent] = []
        self.stats = {
            "total_tasks": 0,
            "total_tool_calls": 0,
            "total_tokens": 0,
            "total_time_ms": 0,
            "success_count": 0,
            "failure_count": 0,
            "tool_call_times": {},
            "agent_iterations": 0
        }
    
    def _setup_logging(self):
        """Configura logging a archivo JSON."""
        # Handler adicional para JSON
        self.json_handler = logging.FileHandler(
            self.log_file, mode='a', encoding='utf-8'
        )
        self._file_handle = None  # Para control manual si es necesario
    
    def close(self):
        """Cierra handlers abiertos."""
        if self.json_handler:
            self.json_handler.close()
    
    def record_event(self, component: str, event: str, 
                    level: EventLevel = EventLevel.INFO,
                    duration_ms: Optional[float] = None,
                    tokens_input: Optional[int] = None,
                    tokens_output: Optional[int] = None,
                    success: Optional[bool] = None,
                    error: Optional[str] = None,
                    metadata: Optional[Dict[str, Any]] = None) -> MetricEvent:
        """Registra un evento métrico."""
        metric = MetricEvent(
            timestamp=time.time(),
            level=level.value,
            component=component,
            event=event,
            duration_ms=duration_ms,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            success=success,
            error=error,
            metadata=metadata or {}
        )
        
        self.events.append(metric)
        
        # Guardar a archivo JSON Lines (con cierre automático)
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                json.dump(metric.to_dict(), f, ensure_ascii=False)
                f.write('\n')
                f.flush()
        except Exception as e:
            logger.error(f"Error escribiendo métricas: {e}")
        
        # Update stats
        self._update_stats(metric)
        
        return metric
    
    def _update_stats(self, metric: MetricEvent):
        """Actualiza estadísticas agregadas."""
        if metric.component == "agent":
            if metric.event == "task_started":
                self.stats["total_tasks"] += 1
            elif metric.event == "iteration":
                self.stats["agent_iterations"] += 1
        
        elif metric.component == "tool":
            if metric.event == "call_completed":
                self.stats["total_tool_calls"] += 1
                if metric.success and metric.duration_ms:
                    # Agrupar por NOMBRE de herramienta (no por componente)
                    tool_name = (metric.metadata or {}).get("tool_name", "tool")
                    self.stats["tool_call_times"].setdefault(tool_name, []).append(metric.duration_ms)
        
        if metric.duration_ms:
            self.stats["total_time_ms"] += metric.duration_ms
        
        if metric.tokens_input:
            self.stats["total_tokens"] += metric.tokens_input
        if metric.tokens_output:
            self.stats["total_tokens"] += metric.tokens_output
        
        if metric.success is True:
            self.stats["success_count"] += 1
        elif metric.success is False:
            self.stats["failure_count"] += 1
    
    def record_task_start(self, task_name: str, metadata: Optional[Dict] = None):
        """Registra el inicio de una tarea."""
        return self.record_event(
            component="agent",
            event="task_started",
            level=EventLevel.INFO,
            metadata={"task_name": task_name, **(metadata or {})}
        )
    
    def record_task_end(self, task_name: str, duration_ms: float, 
                       success: bool, result: str = ""):
        """Registra el fin de una tarea."""
        return self.record_event(
            component="agent",
            event="task_completed",
            level=EventLevel.INFO,
            duration_ms=duration_ms,
            success=success,
            metadata={"task_name": task_name, "result": result}
        )
    
    def record_tool_call(self, tool_name: str, duration_ms: float,
                        success: bool, tokens_in: int = 0, tokens_out: int = 0,
                        error: Optional[str] = None):
        """Registra una llamada a herramienta."""
        return self.record_event(
            component="tool",
            event="call_completed",
            level=EventLevel.INFO if success else EventLevel.WARNING,
            duration_ms=duration_ms,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            success=success,
            error=error,
            metadata={"tool_name": tool_name}
        )
    
    def record_llm_call(self, duration_ms: float, tokens_in: int, tokens_out: int,
                       temperature: float = 0.7, model: str = ""):
        """Registra una llamada al LLM."""
        return self.record_event(
            component="llm",
            event="inference",
            level=EventLevel.DEBUG,
            duration_ms=duration_ms,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            metadata={"temperature": temperature, "model": model}
        )
    
    def record_error(self, component: str, error: str, metadata: Optional[Dict] = None):
        """Registra un error."""
        return self.record_event(
            component=component,
            event="error",
            level=EventLevel.ERROR,
            success=False,
            error=error,
            metadata=metadata or {}
        )
    
    def get_summary(self) -> Dict[str, Any]:
        """Retorna un resumen de métricas."""
        avg_task_time = 0
        if self.stats["total_tasks"] > 0:
            avg_task_time = self.stats["total_time_ms"] / self.stats["total_tasks"]
        
        success_rate = 0
        total_ops = self.stats["success_count"] + self.stats["failure_count"]
        if total_ops > 0:
            success_rate = (self.stats["success_count"] / total_ops) * 100
        
        return {
            "total_tasks": self.stats["total_tasks"],
            "total_tool_calls": self.stats["total_tool_calls"],
            "total_iterations": self.stats["agent_iterations"],
            "total_time_ms": self.stats["total_time_ms"],
            "total_tokens": self.stats["total_tokens"],
            "average_task_time_ms": avg_task_time,
            "success_rate_percent": success_rate,
            "success_count": self.stats["success_count"],
            "failure_count": self.stats["failure_count"]
        }
    
    def get_recent_events(self, limit: int = 50) -> List[MetricEvent]:
        """Retorna eventos recientes."""
        return self.events[-limit:]
    
    def export_metrics(self, filepath: Optional[Path] = None) -> Dict[str, Any]:
        """Exporta métricas a archivo."""
        if filepath is None:
            filepath = self.log_dir / f"metrics_export_{int(time.time())}.json"
        
        export_data = {
            "exported_at": datetime.now().isoformat(),
            "summary": self.get_summary(),
            "recent_events": [e.to_dict() for e in self.get_recent_events(limit=100)]
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Métricas exportadas a {filepath}")
        return export_data


class PerformanceTimer:
    """Context manager para medir tiempo de ejecución."""
    
    def __init__(self, collector: MetricsCollector, component: str, event: str):
        self.collector = collector
        self.component = component
        self.event = event
        self.start_time = 0
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self.start_time) * 1000
        success = exc_type is None
        error = str(exc_val) if exc_val else None
        
        self.collector.record_event(
            component=self.component,
            event=self.event,
            duration_ms=duration_ms,
            success=success,
            error=error
        )
