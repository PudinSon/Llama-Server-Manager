"""
tools/goals.py

Gestor de Metas y OKRs (Tarea 3.2 - Fase 3).
Coordina con sensores y daemon para ejecutar tareas basadas en objetivos.
"""

import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum

logger = logging.getLogger("goals")


class GoalStatus(Enum):
    """Estados de una meta."""
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class GoalPriority(Enum):
    """Prioridades de metas."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class Goal:
    """Representa una meta o objetivo."""
    goal_id: str
    description: str
    frequency: str  # "once", "daily", "weekly", "monthly"
    priority: GoalPriority
    status: GoalStatus = GoalStatus.NOT_STARTED
    progress: float = 0.0  # 0.0 a 1.0
    check_prompt: Optional[str] = None
    linked_sensor_id: Optional[str] = None
    due_date: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    last_checked: float = 0.0
    completion_date: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a dict para serialización."""
        data = asdict(self)
        data["priority"] = self.priority.value
        data["status"] = self.status.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Goal":
        """Crea Goal desde dict."""
        data = data.copy()
        data["priority"] = GoalPriority(data["priority"])
        data["status"] = GoalStatus(data["status"])
        return cls(**data)


@dataclass
class OKR:
    """Objetivo y Resultados Clave (OKR)."""
    okr_id: str
    objective: str  # "O" - objetivo cualitativo
    key_results: List[Dict[str, Any]]  # "KRs" - resultados medibles
    owner: str  # Usuario o agente responsable
    period: str  # "quarterly", "yearly", "ongoing"
    priority: GoalPriority = GoalPriority.MEDIUM
    start_date: float = field(default_factory=time.time)
    end_date: Optional[float] = None
    status: GoalStatus = GoalStatus.NOT_STARTED
    progress: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a dict para serialización."""
        data = asdict(self)
        data["priority"] = self.priority.value
        data["status"] = self.status.value
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OKR":
        """Crea OKR desde dict."""
        data = data.copy()
        data["priority"] = GoalPriority(data["priority"])
        data["status"] = GoalStatus(data["status"])
        return cls(**data)


class GoalManager:
    """Gestor centralizado de metas y OKRs."""
    
    def __init__(self, workspace_path: str):
        self.workspace_path = Path(workspace_path)
        self.goals: Dict[str, Goal] = {}
        self.okrs: Dict[str, OKR] = {}
        self.goal_queue: List[Goal] = []  # Cola prioritizada
        self.state_file = self.workspace_path / "user_data" / "goals.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_state()
    
    def add_goal(self, goal_id: str, description: str, 
                 frequency: str = "weekly", 
                 priority: GoalPriority = GoalPriority.MEDIUM,
                 check_prompt: Optional[str] = None) -> Goal:
        """Añade una meta al sistema."""
        goal = Goal(
            goal_id=goal_id,
            description=description,
            frequency=frequency,
            priority=priority,
            check_prompt=check_prompt
        )
        self.goals[goal_id] = goal
        logger.info(f"Meta añadida: {goal_id} - {description}")
        self._save_state()
        return goal
    
    def add_okr(self, okr_id: str, objective: str, 
                key_results: List[Dict[str, Any]],
                owner: str = "daemon",
                period: str = "quarterly",
                priority: GoalPriority = GoalPriority.HIGH) -> OKR:
        """Añade un OKR al sistema."""
        okr = OKR(
            okr_id=okr_id,
            objective=objective,
            key_results=key_results,
            owner=owner,
            period=period,
            priority=priority
        )
        self.okrs[okr_id] = okr
        logger.info(f"OKR añadido: {okr_id} - {objective}")
        self._save_state()
        return okr
    
    def update_goal_status(self, goal_id: str, status: GoalStatus, progress: float = None):
        """Actualiza el estado de una meta."""
        if goal_id not in self.goals:
            logger.error(f"Meta no encontrada: {goal_id}")
            return
        
        goal = self.goals[goal_id]
        goal.status = status
        if progress is not None:
            goal.progress = max(0.0, min(1.0, progress))
        goal.last_checked = time.time()
        
        if status == GoalStatus.COMPLETED:
            goal.completion_date = time.time()
        
        logger.info(f"Meta actualizada: {goal_id} -> {status.value} ({goal.progress*100:.0f}%)")
        self._save_state()
    
    def update_okr_progress(self, okr_id: str, kr_index: int, progress: float):
        """Actualiza el progreso de un KR específico."""
        if okr_id not in self.okrs:
            logger.error(f"OKR no encontrado: {okr_id}")
            return
        
        okr = self.okrs[okr_id]
        if 0 <= kr_index < len(okr.key_results):
            okr.key_results[kr_index]["progress"] = max(0.0, min(1.0, progress))
            
            # Calcular progreso general como promedio
            avg_progress = sum(kr.get("progress", 0) for kr in okr.key_results) / len(okr.key_results)
            okr.progress = avg_progress
            
            logger.info(f"OKR actualizado: {okr_id} KR[{kr_index}] -> {progress*100:.0f}%")
            self._save_state()
    
    def get_priority_ordered_goals(self) -> List[Goal]:
        """Retorna metas ordenadas por prioridad."""
        priority_order = {
            GoalPriority.URGENT: 0,
            GoalPriority.HIGH: 1,
            GoalPriority.MEDIUM: 2,
            GoalPriority.LOW: 3
        }
        return sorted(self.goals.values(), 
                     key=lambda g: (priority_order.get(g.priority, 999), g.created_at))
    
    def get_pending_goals(self) -> List[Goal]:
        """Retorna metas que necesitan atención."""
        pending = [
            g for g in self.goals.values()
            if g.status in [GoalStatus.NOT_STARTED, GoalStatus.IN_PROGRESS, GoalStatus.BLOCKED]
        ]
        return sorted(pending, 
                     key=lambda g: (
                         g.priority.value != GoalPriority.URGENT,
                         g.priority.value != GoalPriority.HIGH,
                         g.created_at
                     ))
    
    def get_all_goals(self) -> List[Dict[str, Any]]:
        """Retorna todas las metas como diccionarios serializables."""
        return [
            {
                "id": goal.goal_id,
                "title": goal.goal_id,
                "description": goal.description,
                "status": goal.status.value,
                "priority": goal.priority.value,
                "progress": goal.progress,
                "frequency": goal.frequency,
                "linked_sensor_id": goal.linked_sensor_id,
                "last_checked": goal.last_checked,
            }
            for goal in self.goals.values()
        ]

    def get_goal_summary(self, goal_id: Optional[str] = None) -> Dict[str, Any]:
        """Obtiene un resumen del estado de metas o de una meta concreta."""
        goals = self.goals.values() if goal_id is None else [self.goals[goal_id]] if goal_id in self.goals else []

        if goal_id is not None and not goals:
            return {"goal_id": goal_id, "status": "not_found", "progress": 0.0, "average_progress": 0.0, "summary": "Meta no encontrada"}

        if goal_id is None:
            total = len(self.goals)
            status_counts = {}
            for status in GoalStatus:
                status_counts[status.value] = len([g for g in self.goals.values() if g.status == status])

            avg_progress = (
                sum(g.progress for g in self.goals.values()) / total
                if total > 0 else 0
            )

            priority_counts = {}
            for priority in GoalPriority:
                priority_counts[priority.value] = len([g for g in self.goals.values() if g.priority == priority])

            return {
                "total_goals": total,
                "status": status_counts,
                "average_progress": avg_progress,
                "priorities": priority_counts,
                "pending_count": len(self.get_pending_goals()),
                "summary": self._generate_text_summary()
            }

        goal = goals[0]
        return {
            "goal_id": goal.goal_id,
            "title": goal.goal_id,
            "description": goal.description,
            "status": goal.status.value,
            "progress": goal.progress,
            "average_progress": goal.progress,
            "summary": f"Meta {goal.goal_id}: {goal.status.value} ({goal.progress * 100:.0f}%)",
            "priority": goal.priority.value,
        }
    
    def _generate_text_summary(self) -> str:
        """Genera un resumen textual del estado."""
        pending = self.get_pending_goals()
        if not pending:
            return "Todas las metas completadas ✓"
        
        green = len([g for g in pending if g.progress > 0.8])
        yellow = len([g for g in pending if 0.3 < g.progress <= 0.8])
        red = len([g for g in pending if g.progress <= 0.3])
        
        summary = f"{green} en verde 🟢"
        if yellow > 0:
            summary += f", {yellow} en amarillo 🟡"
        if red > 0:
            summary += f", {red} en rojo 🔴"
        
        return summary
    
    def link_goal_to_sensor(self, goal_id: str, sensor_id: str):
        """Vincula una meta a un sensor específico."""
        if goal_id not in self.goals:
            logger.error(f"Meta no encontrada: {goal_id}")
            return
        
        self.goals[goal_id].linked_sensor_id = sensor_id
        logger.info(f"Meta {goal_id} vinculada al sensor {sensor_id}")
        self._save_state()
    
    def _load_state(self):
        """Carga el estado desde archivo."""
        if not self.state_file.exists():
            return
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            for goal_data in data.get("goals", []):
                goal = Goal.from_dict(goal_data)
                self.goals[goal.goal_id] = goal
            
            for okr_data in data.get("okrs", []):
                okr = OKR.from_dict(okr_data)
                self.okrs[okr.okr_id] = okr
            
            logger.info(f"Estado cargado: {len(self.goals)} metas, {len(self.okrs)} OKRs")
        except Exception as e:
            logger.error(f"Error cargando estado: {e}")
    
    def _save_state(self):
        """Guarda el estado en archivo."""
        try:
            data = {
                "goals": [g.to_dict() for g in self.goals.values()],
                "okrs": [o.to_dict() for o in self.okrs.values()],
                "saved_at": time.time()
            }
            
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error guardando estado: {e}")


class GoalEvaluator:
    """Evalúa el progreso de metas usando LLM (futuro) o heurísticas."""
    
    def __init__(self, goal_manager: GoalManager, llm_client=None):
        self.goal_manager = goal_manager
        self.llm_client = llm_client
    
    def evaluate_goal(self, goal_id: str) -> Dict[str, Any]:
        """Evalúa una meta y retorna recomendaciones."""
        if goal_id not in self.goal_manager.goals:
            return {"error": "Goal not found"}
        
        goal = self.goal_manager.goals[goal_id]
        
        evaluation = {
            "goal_id": goal_id,
            "current_status": goal.status.value,
            "current_progress": goal.progress,
            "recommendation": "",
            "next_action": ""
        }
        
        # Heurística simple: si sin progreso hace mucho, marcar como bloqueado
        if goal.status == GoalStatus.IN_PROGRESS:
            time_since_check = time.time() - goal.last_checked
            if time_since_check > 86400 * 3:  # 3 días
                evaluation["recommendation"] = "Meta sin progreso reciente, marcar como bloqueada"
                evaluation["next_action"] = "revisar_obstaculos"
        
        # Si el LLM está disponible, usarlo para evaluación más sofisticada
        if self.llm_client and goal.check_prompt:
            # Aquí iría integración con LLM
            pass
        
        return evaluation
