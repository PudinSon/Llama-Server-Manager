"""
tools/context_manager.py

ContextManager: gestión simple de ventana de contexto, historial y resumen.

Provee métodos mínimos:
- `add_observation(text)` — añade observación al historial
- `get_context()` — devuelve historial truncado a token_limit aproximado
- `set_state(dict)` — establece estado compartido (world_state)
"""
from typing import List, Dict, Any
import time

class ContextManager:
    def __init__(self, token_limit: int = 2000):
        self.token_limit = token_limit
        self.history: List[Dict[str, Any]] = []  # lista de {'role': 'tool'|'agent'|'user', 'text': str, 'ts': float}
        self.world_state: Dict[str, Any] = {}

    def set_state(self, state: Dict[str, Any]):
        self.world_state = state or {}

    def add_observation(self, text: str, role: str = "tool"):
        self.history.append({"role": role, "text": text, "ts": time.time()})
        self._ensure_limits()

    def _approx_tokens(self, text: str) -> int:
        # Aproximación simple: 1 token ~ 0.75 palabras -> usar palabras como proxy
        return max(1, int(len(text.split()) * 1.3))

    def _ensure_limits(self):
        # Trunca el historial si excede token_limit (por aproximación)
        total = 0
        kept = []
        for item in reversed(self.history):
            total += self._approx_tokens(item.get("text", ""))
            if total > self.token_limit:
                break
            kept.append(item)
        # kept está en orden inverso
        self.history = list(reversed(kept))

    def summarize(self) -> str:
        # Resumen simple: concatenar primeros/últimos items (scaffold)
        if not self.history:
            return ""
        parts = []
        # tomar hasta 3 acciones recientes
        for item in self.history[-3:]:
            parts.append(f"[{item['role']}] {item['text']}")
        return " \n".join(parts)

    def get_context(self) -> Dict[str, Any]:
        return {"world_state": self.world_state, "history": self.history, "summary": self.summarize()}
