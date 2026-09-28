# tools/procedures.py
# ProcedureEngine: ejecuta procedimientos parametrizados paso a paso.
# Persiste procedures en JSON. Se integra con execute_tool del MCP.
#
# Uso desde CLI:
#   /procedure list           — listar procedures guardados
#   /procedure save <name>    — guardar el último patrón detectado
#   /procedure run <name> p1=v1 p2=v2  — ejecutar un procedure
#   /procedure delete <name>  — eliminar un procedure
#   /procedure show <name>    — ver detalles de un procedure

from __future__ import annotations

import json
import re
import time
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("procedures")

# ---------------------------------------------------------------------------
# Template rendering: {{variable_name}}
# ---------------------------------------------------------------------------
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\-\.]+)\s*\}\}")


@dataclass
class Procedure:
    """Un procedimiento guardado: lista de pasos con variables."""
    name: str
    params: List[str]
    steps: List[Dict[str, Any]] = field(default_factory=list)
    description: str = ""
    created_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    auto_generated: bool = False
    execution_count: int = 0
    last_run: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "params": list(self.params),
            "steps": list(self.steps),
            "description": self.description,
            "created_at": self.created_at,
            "tags": list(self.tags),
            "auto_generated": self.auto_generated,
            "execution_count": self.execution_count,
            "last_run": self.last_run,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Procedure":
        return cls(
            name=data.get("name", "unnamed"),
            params=data.get("params", []),
            steps=data.get("steps", []),
            description=data.get("description", ""),
            created_at=data.get("created_at", time.time()),
            tags=data.get("tags", []),
            auto_generated=data.get("auto_generated", False),
            execution_count=data.get("execution_count", 0),
            last_run=data.get("last_run"),
        )

    def render(self, variables: Optional[Dict[str, Any]] = None) -> "Procedure":
        """Devuelve una copia con {{var}} reemplazadas por values."""
        variables = variables or {}
        rendered_steps: List[Dict[str, Any]] = []
        for step in self.steps:
            rendered = json.loads(json.dumps(step))
            rendered["args"] = self._render_value(step.get("args", {}), variables)
            rendered["description"] = self._render_str(step.get("description", ""), variables)
            if "tool" in rendered:
                rendered["tool"] = self._render_str(rendered["tool"], variables)
            rendered_steps.append(rendered)
        return Procedure(
            name=self.name,
            params=list(self.params),
            steps=rendered_steps,
            description=self._render_str(self.description, variables),
            created_at=self.created_at,
            tags=list(self.tags),
        )

    def _render_str(self, value: Any, variables: Dict[str, Any]) -> Any:
        if not isinstance(value, str):
            return value
        return _TEMPLATE_RE.sub(
            lambda m: str(variables.get(m.group(1), m.group(0))), value
        )

    def _render_value(self, value: Any, variables: Dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {k: self._render_value(v, variables) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render_value(v, variables) for v in value]
        if isinstance(value, str):
            return self._render_str(value, variables)
        return value


# ---------------------------------------------------------------------------
# ProcedureEngine: ejecuta un procedure paso a paso
# ---------------------------------------------------------------------------
class ProcedureEngine:
    """Ejecuta un procedure paso a paso usando execute_tool del MCP.

    Cada paso tiene:
      - action: "tool_call" (único soportado por ahora)
      - tool: nombre de la herramienta MCP
      - args: argumentos (pueden tener {{variables}})
      - description: descripción legible del paso
    """

    def __init__(self, execute_tool_fn: Optional[Callable] = None):
        """
        execute_tool_fn: callable(name, args) -> str  (por defecto usa execute_tool de agent_core)
        """
        self._execute_tool = execute_tool_fn

    def _get_execute_tool(self) -> Callable:
        if self._execute_tool is not None:
            return self._execute_tool
        # Lazy import para evitar ciclo circular
        try:
            from tools.agent_core import execute_tool
            return execute_tool
        except ImportError:
            from agent_core import execute_tool
            return execute_tool

    def execute(
        self,
        procedure: Procedure,
        variables: Optional[Dict[str, Any]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> List[Dict[str, Any]]:
        """Ejecuta un procedure completo.

        Retorna lista de resultados, uno por paso.
        Cada resultado: {step, status, tool?, result?, reason?}
        """
        variables = variables or {}
        rendered = procedure.render(variables)
        execute_tool = self._get_execute_tool()
        results: List[Dict[str, Any]] = []

        for i, step in enumerate(rendered.steps):
            # Verificar si se solicitó cancelación
            if stop_event is not None and stop_event.is_set():
                results.append({
                    "step": step.get("description", f"Paso {i+1}"),
                    "status": "cancelled",
                    "reason": "cancelación solicitada",
                })
                break

            action = step.get("action", "tool_call")
            if action != "tool_call":
                results.append({
                    "step": step.get("description", f"Paso {i+1}"),
                    "status": "skipped",
                    "reason": f"acción no soportada: {action}",
                })
                continue

            tool_name = step.get("tool")
            args = step.get("args", {})
            desc = step.get("description", f"Paso {i+1}: {tool_name}")

            if not tool_name:
                results.append({
                    "step": desc,
                    "status": "error",
                    "reason": "nombre de herramienta vacío",
                })
                continue

            try:
                result = execute_tool(tool_name, args)
                result_str = str(result) if result else ""
                is_error = (
                    result_str.lower().startswith("error")
                    or "❌" in result_str
                    or "no encontrada" in result_str.lower()
                )
                results.append({
                    "step": desc,
                    "status": "error" if is_error else "success",
                    "tool": tool_name,
                    "result": result_str[:500],
                })
            except Exception as e:
                results.append({
                    "step": desc,
                    "status": "error",
                    "tool": tool_name,
                    "reason": str(e),
                })

        return results


# ---------------------------------------------------------------------------
# ProcedureStore: persistencia en JSON
# ---------------------------------------------------------------------------
class ProcedureStore:
    """Almacena y recupera procedures desde un archivo JSON."""

    def __init__(self, workspace_path: str):
        self._path = Path(workspace_path) / ".procedures.json"
        self._lock = threading.Lock()
        self._procedures: Dict[str, Procedure] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data:
                    proc = Procedure.from_dict(entry)
                    self._procedures[proc.name] = proc
                logger.info(f"Cargados {len(self._procedures)} procedures.")
            except Exception as e:
                logger.error(f"Error cargando procedures: {e}")

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = [p.to_dict() for p in self._procedures.values()]
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error guardando procedures: {e}")

    def save(self, procedure: Procedure):
        with self._lock:
            self._procedures[procedure.name] = procedure
            self._save()

    def get(self, name: str) -> Optional[Procedure]:
        return self._procedures.get(name)

    def list_all(self) -> List[Procedure]:
        return list(self._procedures.values())

    def delete(self, name: str) -> bool:
        with self._lock:
            if name in self._procedures:
                del self._procedures[name]
                self._save()
                return True
            return False

    def search(self, query: str) -> List[Procedure]:
        query_lower = query.lower()
        return [
            p for p in self._procedures.values()
            if query_lower in p.name.lower()
            or query_lower in p.description.lower()
            or any(query_lower in t.lower() for t in p.tags)
        ]


# ---------------------------------------------------------------------------
# Funciones auxiliares para crear procedures desde tool calls
# ---------------------------------------------------------------------------
def procedure_from_tool_calls(
    tool_calls: List[Dict[str, Any]],
    name: str = "",
    description: str = "",
    params: Optional[List[str]] = None,
) -> Procedure:
    """Crea un Procedure a partir de una secuencia de tool calls ejecutados.

    Cada tool_call: {"tool": "name", "arguments": {...}} o {"name": "name", "arguments": {...}}
    """
    steps = []
    detected_params = set()
    all_params = params or []

    for tc in tool_calls:
        tool_name = tc.get("tool") or tc.get("name", "")
        args = tc.get("arguments", {})
        # Detectar variables: si un valor coincide con algún param conocido
        rendered_args = {}
        for k, v in args.items():
            if isinstance(v, str) and v in all_params:
                rendered_args[k] = "{{" + v + "}}"
            else:
                rendered_args[k] = v

        steps.append({
            "action": "tool_call",
            "tool": tool_name,
            "args": rendered_args,
            "description": f"Ejecutar {tool_name}",
        })

    proc_name = name or f"procedure_{int(time.time())}"
    return Procedure(
        name=proc_name,
        params=all_params,
        steps=steps,
        description=description or f"Procedimiento auto-generado con {len(steps)} pasos",
        auto_generated=not bool(name),
    )


def detect_repeating_pattern(
    tool_calls_history: List[Dict[str, Any]],
    min_occurrences: int = 3,
) -> Optional[List[Dict[str, Any]]]:
    """Detecta si hay un patrón de tool calls que se repite.

    Retorna los steps del patrón si se detecta, None si no.
    """
    if len(tool_calls_history) < min_occurrences:
        return None

    # Buscar secuencias repetidas de longitud 2-5
    for seq_len in range(2, min(6, len(tool_calls_history) // min_occurrences + 1)):
        for start in range(len(tool_calls_history) - seq_len + 1):
            pattern = tool_calls_history[start:start + seq_len]
            pattern_sig = json.dumps(
                [(tc.get("tool") or tc.get("name", "")) for tc in pattern],
                sort_keys=True,
            )
            occurrences = 0
            for i in range(len(tool_calls_history) - seq_len + 1):
                candidate = tool_calls_history[i:i + seq_len]
                candidate_sig = json.dumps(
                    [(tc.get("tool") or tc.get("name", "")) for tc in candidate],
                    sort_keys=True,
                )
                if candidate_sig == pattern_sig:
                    occurrences += 1
            if occurrences >= min_occurrences:
                return pattern

    return None
