# tools/autocompletion.py
# Autocompletado inteligente para Agent CLI v4

import os
import re
import json
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from datetime import datetime
from difflib import SequenceMatcher
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document


class PathCompleter(Completer):
    """Autocompletado de rutas de archivo."""
    
    def __init__(self, workspace_path: Optional[str] = None):
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
    
    def get_completions(self, document: Document, complete_event):
        """Genera completados de paths."""
        text = document.text_before_cursor
        
        # Detectar si estamos escribiendo una ruta
        # Buscar la última palabra que parece ser una ruta
        match = re.search(r'(?:^|[\s\(="\'])([^\s\(="\']*/)?([^\s\(="\']*)', text)
        if not match:
            return
        
        path_prefix = match.group(1) or ""
        current_word = match.group(2) or ""
        
        # Construir la ruta base
        if path_prefix.startswith("/"):
            base_path = Path(path_prefix)
        else:
            base_path = self.workspace_path / path_prefix
        
        try:
            base_path = base_path.resolve()
            if not base_path.exists() or not base_path.is_dir():
                base_path = base_path.parent
            
            # Listar archivos que coinciden
            for item in sorted(base_path.iterdir()):
                # Filtrar .venv, __pycache__, .git
                if item.name.startswith((".", "__pycache__")):
                    continue
                
                if item.name.startswith(current_word):
                    if item.is_dir():
                        completion_text = item.name + "/"
                        display_text = f"📁 {item.name}/"
                    else:
                        completion_text = item.name
                        display_text = f"📄 {item.name}"
                    
                    start_pos = len(current_word)
                    yield Completion(completion_text, start_position=-start_pos, display=display_text)
        except (PermissionError, OSError):
            pass


class ToolCompleter(Completer):
    """Autocompletado de nombres de herramientas con descripciones."""
    
    TOOLS = {
        "bash": "Ejecutar comando shell con timeout",
        "view": "Ver archivo con números de línea",
        "edit": "Reemplazar texto exacto en archivo",
        "search": "Buscar texto en el proyecto",
        "tree": "Mostrar estructura de directorios",
        "create": "Crear nuevo archivo",
        "delete": "Eliminar archivo o directorio",
        "execute": "Ejecutar código o script",
        "query": "Hacer consulta a memoria semántica",
    }
    
    def get_completions(self, document: Document, complete_event):
        """Genera completados de herramientas."""
        text = document.text_before_cursor
        
        # Detectar si estamos ejecutando una herramienta
        # Patrón: "tool_name(...)" o "tool_name "
        match = re.search(r'(?:^|\s)([\w_]*)$', text)
        if not match:
            return
        
        current_word = match.group(1) or ""
        
        for tool_name, description in self.TOOLS.items():
            if tool_name.startswith(current_word):
                start_pos = len(current_word)
                display_text = f"🔧 {tool_name}: {description}"
                yield Completion(tool_name, start_position=-start_pos, display=display_text)


class FuzzyHistorySearcher:
    """Búsqueda fuzzy en historial de comandos."""
    
    def __init__(self, history_file: Optional[Path] = None):
        self.history_file = history_file
        self.history_cache: List[str] = []
        self.last_load_time = 0
    
    def _load_history(self):
        """Carga historial desde archivo."""
        if not self.history_file or not self.history_file.exists():
            return
        
        try:
            mtime = self.history_file.stat().st_mtime
            if mtime > self.last_load_time:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.history_cache = [line.strip() for line in f if line.strip()]
                self.last_load_time = mtime
        except (OSError, IOError):
            pass
    
    def search(self, query: str, limit: int = 10) -> List[Tuple[str, float]]:
        """Busca en historial con fuzzy matching."""
        self._load_history()
        
        if not query or not self.history_cache:
            return []
        
        results: List[Tuple[str, float]] = []
        
        for item in self.history_cache:
            # Calcular similitud fuzzy
            ratio = SequenceMatcher(None, query.lower(), item.lower()).ratio()
            if ratio > 0.3:  # Umbral mínimo de similitud
                results.append((item, ratio))
        
        # Ordenar por similitud (descendente)
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]
    
    def get_suggestions(self, query: str) -> List[str]:
        """Devuelve sugerencias para autocomplete."""
        return [item for item, _ in self.search(query)]


class IntelligentCompleter(Completer):
    """Completer inteligente que combina paths, herramientas e historial."""
    
    SLASH_COMMANDS = {
        "/help": "Mostrar ayuda",
        "/status": "Estado del agente",
        "/exit": "Salir",
        "/pause": "Pausar daemon",
        "/resume": "Reanudar daemon",
        "/stop": "Detener daemon",
        "/checkpoints": "Listar checkpoints",
        "/rollback": "Restaurar checkpoint",
        "/skills": "Listar skills",
        "/model": "Ver/cambiar modelo",
        "/clear": "Limpiar pantalla",
        "/reasoning": "Activar/desactivar razonamiento",
        "/compact": "Activar modo compacto",
        "/verbose": "Activar modo verbose",
        "/tree": "Mostrar árbol de directorios",
        "/diff": "Comparar archivos",
        "/history": "Ver historial",
        "/stats": "Mostrar estadísticas",
    }
    
    def __init__(self, workspace_path: Optional[str] = None, 
                 history_file: Optional[Path] = None):
        self.path_completer = PathCompleter(workspace_path)
        self.tool_completer = ToolCompleter()
        self.history_searcher = FuzzyHistorySearcher(history_file)
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
    
    def get_completions(self, document: Document, complete_event):
        """Completer principal que delega según contexto."""
        text = document.text_before_cursor
        
        if not text:
            return
        
        # 1. Comandos slash (/)
        if text.startswith("/"):
            yield from self._complete_slash_commands(text)
        
        # 2. Argumentos de herramientas (detectar contexto)
        elif self._looks_like_tool_call(text):
            yield from self.path_completer.get_completions(document, complete_event)
        
        # 3. Nombres de herramientas en inicio
        elif text.strip() and not any(c in text for c in "()[]{}"):
            yield from self.tool_completer.get_completions(document, complete_event)
    
    def _complete_slash_commands(self, text: str):
        """Completa comandos slash."""
        for cmd, description in self.SLASH_COMMANDS.items():
            if cmd.startswith(text):
                display_text = f"{cmd}: {description}"
                start_pos = len(text)
                yield Completion(cmd, start_position=-start_pos, display=display_text)
    
    def _looks_like_tool_call(self, text: str) -> bool:
        """Detecta si se está ejecutando una herramienta."""
        return any(tool in text.lower() for tool in [
            "bash", "view", "edit", "search", "tree", "create", "delete", "execute", "query"
        ])


class SessionHistoryManager:
    """Gestor de historial de sesión con persistencia."""
    
    def __init__(self, sessions_dir: Optional[Path] = None):
        self.sessions_dir = sessions_dir or Path.cwd() / "user_data" / "cli_sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.current_session: List[Dict[str, str]] = []
        self.session_id = self._generate_session_id()
    
    def _generate_session_id(self) -> str:
        """Genera ID único para sesión."""
        return datetime.now().strftime("%Y%m%d_%H%M%S")
    
    def add_entry(self, user_input: str, agent_output: Optional[str] = None, 
                  metadata: Optional[Dict] = None):
        """Añade entrada al historial de sesión."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "input": user_input,
            "output": agent_output or "",
            "metadata": metadata or {}
        }
        self.current_session.append(entry)
    
    def save_session(self):
        """Guarda sesión a archivo JSON."""
        if not self.current_session:
            return
        
        session_file = self.sessions_dir / f"session_{self.session_id}.json"
        try:
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump(self.current_session, f, indent=2, ensure_ascii=False)
        except (OSError, IOError) as e:
            print(f"Error guardando sesión: {e}")
    
    def load_recent_sessions(self, limit: int = 10) -> List[Path]:
        """Carga sesiones recientes."""
        sessions = sorted(
            self.sessions_dir.glob("session_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        return sessions[:limit]
    
    def search_history(self, query: str, limit: int = 20) -> List[Dict]:
        """Busca en todo el historial."""
        results = []
        
        for session_file in self.sessions_dir.glob("session_*.json"):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session_data = json.load(f)
                    for entry in session_data:
                        if query.lower() in entry.get("input", "").lower():
                            entry["source_file"] = session_file.name
                            results.append(entry)
            except (json.JSONDecodeError, IOError):
                continue
        
        # Ordenar por timestamp descendente
        results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return results[:limit]


# Test unitario básico
if __name__ == "__main__":
    from pathlib import Path
    
    print("Testing autocompletion modules...")
    
    # Test PathCompleter
    pc = PathCompleter()
    print("✓ PathCompleter inicializado")
    
    # Test ToolCompleter
    tc = ToolCompleter()
    print("✓ ToolCompleter inicializado")
    
    # Test FuzzyHistorySearcher
    fhs = FuzzyHistorySearcher()
    results = fhs.search("help")
    print(f"✓ FuzzyHistorySearcher: {len(results)} resultados")
    
    # Test IntelligentCompleter
    ic = IntelligentCompleter()
    print("✓ IntelligentCompleter inicializado")
    
    # Test SessionHistoryManager
    shm = SessionHistoryManager()
    shm.add_entry("test command", "test output")
    print("✓ SessionHistoryManager funciona")
    
    print("\n✅ Todos los módulos de autocompletado listos")
