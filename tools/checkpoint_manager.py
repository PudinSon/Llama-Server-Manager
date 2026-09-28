# tools/checkpoint_manager.py
# Módulo para la gestión de checkpoints y persistencia avanzada.
# Permite guardar, listar, cargar y restaurar el estado completo del proyecto.

import os
import json
import shutil
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import hashlib

logger = logging.getLogger("checkpoint_manager")

# ============================================================================
# CONSTANTES Y RUTAS
# ============================================================================

CHECKPOINTS_DIR_NAME = ".checkpoints"
#: Directorios que NUNCA deben copiarse dentro de un checkpoint.
CHECKPOINT_EXCLUDE_DIRS = {
    ".checkpoints", ".git", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".eggs", "dist", "build", ".next", ".nuxt", ".svelte-kit",
    "EBWebView", "webui_profiles",       # WebView2 (por si acaso)
    ".daemon", ".metrics", ".skills", ".freebuff", ".chromadb",
    ".procedures", ".idea", ".vscode",
}

CHECKPOINT_EXCLUDE_FILE_PREFIXES = ("cache_", "data_")
CHECKPOINT_EXCLUDE_SUFFIXES = (
    ".lock", ".tmp", ".part", ".log", ".pyc", ".pyo",
    ".db-shm", ".db-wal",
)
INDEX_FILE = "INDEX.json"
STATE_FILE = "state.json"
METADATA_FILE = "metadata.json"
FILES_DIR = "files"
def _json_safe(obj):
    """Convierte tipos no serializables por json a equivalentes seguros.

    Los estados de checkpoint los construyen varios agentes (AGENCY, PLAN,
    daemon...). Cualquiera de ellos puede colar un `set` (p. ej. un conjunto de
    ids), un `Path`, un `datetime` o un objeto con `to_dict`. Antes, un solo
    valor así tumbaba el checkpoint entero con «Object of type set is not JSON
    serializable» y el paso se quedaba sin respaldo. Aquí se degrada a un tipo
    serializable; nunca se pierde el checkpoint por un detalle de formato.
    """
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)          # orden estable para diffs limpios
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)
# ============================================================================
# CLASE CHECKPOINTMANAGER
# ============================================================================

class CheckpointManager:
    """
    Gestor de checkpoints para el agente autónomo.
    Permite guardar, listar, cargar y restaurar el estado completo del proyecto.
    """
    def __init__(self, workspace_path: str):
        self.workspace_path = Path(workspace_path).resolve()
        self.checkpoints_dir = self.workspace_path / CHECKPOINTS_DIR_NAME
        self.index_file = self.checkpoints_dir / INDEX_FILE
        self._ensure_directories()
        self._load_index()

    def _ensure_directories(self):
        """Crea las estructuras de directorios necesarias."""
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_file.exists():
            self._save_index({"checkpoints": [], "latest": None})

    def _load_index(self) -> Dict:
        """Carga el índice de checkpoints."""
        if self.index_file.exists():
            try:
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error cargando índice: {e}")
        return {"checkpoints": [], "latest": None}

    def _save_index(self, index: Dict):
        """Guarda el índice de checkpoints."""
        with open(self.index_file, 'w', encoding='utf-8') as f:
            json.dump(index, f, indent=2, ensure_ascii=False)

    def _generate_checkpoint_id(self) -> str:
        """Genera un ID único para el checkpoint basado en timestamp."""
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        # Añadir un hash corto para evitar colisiones
        import random
        suffix = ''.join(random.choices('abcdef0123456789', k=6))
        return f"checkpoint_{timestamp}_{suffix}"

    def _should_skip(self, item: Path, workspace_root: Path) -> bool:
        """True si `item` no debe copiarse al checkpoint."""
        if item.name in CHECKPOINT_EXCLUDE_DIRS:
            return True
        try:
            rel_parts = item.relative_to(workspace_root).parts
        except ValueError:
            rel_parts = item.parts
        if any(part in CHECKPOINT_EXCLUDE_DIRS for part in rel_parts):
            return True
        if item.is_file():
            lower = item.name.casefold()
            if lower.startswith(CHECKPOINT_EXCLUDE_FILE_PREFIXES):
                return True
            if lower.endswith(CHECKPOINT_EXCLUDE_SUFFIXES):
                return True
        return False


    def _copy_files(self, src_dir: Path, dst_dir: Path):
        """Copia recursiva saltando exclusiones y SIN abortar por fichero suelto."""
        dst_dir.mkdir(parents=True, exist_ok=True)
        workspace_root = self.workspace_path
        for item in src_dir.iterdir():
            if self._should_skip(item, workspace_root):
                continue
            try:
                if item.is_dir():
                    self._copy_files(item, dst_dir / item.name)
                else:
                    shutil.copy2(item, dst_dir / item.name)
            except (PermissionError, OSError) as err:
                # Un fichero bloqueado (WebView2, .db-shm, antivirus...) no debe
                # tumbar el checkpoint entero ni dejar el workspace a medias.
                logger.debug("Checkpoint: omitido %s (%s)", item, err)
                continue

    def _restore_files(self, src_dir: Path, dst_dir: Path):
        """Restaura archivos desde src_dir a dst_dir (sobrescribe)."""
        for item in src_dir.iterdir():
            if item.is_dir():
                self._restore_files(item, dst_dir / item.name)
            else:
                # Crear directorio padre si no existe
                (dst_dir / item.name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dst_dir / item.name)

    def _copy_selected_files(self, src_dir: Path, dst_dir: Path, files: List[str]):
        """Copia SOLO las rutas indicadas (relativas al workspace).

        Un checkpoint quirúrgico no debe copiar el workspace entero en cada paso
        de un proyecto de decenas de pasos: solo respalda el artefacto que el
        paso declaró (`artifact`). Las rutas fuera del workspace o inexistentes
        se ignoran en silencio.
        """
        for rel in files:
            if not rel:
                continue
            src = (src_dir / str(rel)).resolve()
            try:
                src.relative_to(src_dir)
            except ValueError:
                continue  # fuera del workspace: no se respalda
            if not src.is_file():
                continue
            if self._should_skip(src, src_dir):
                continue
            try:
                dst = dst_dir / str(rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except (PermissionError, OSError) as err:
                # Fichero bloqueado (antivirus, otro proceso, .db-shm abierto):
                # omitir no debe tumbar el checkpoint ni dejar el workspace a
                # medias. Misma tolerancia que en _copy_files.
                logger.debug("Checkpoint selectivo: omitido %s (%s)", src, err)
                continue

    def _calculate_dir_hash(self, dir_path: Path) -> str:
        """Hash por streaming (antes read_bytes() cargaba cada fichero ENTERO
        en RAM — con la caché WebView2 eso eran cientos de MB de pico)."""
        hasher = hashlib.sha256()
        workspace_root = self.workspace_path
        for file_path in sorted(dir_path.rglob('*')):
            if not file_path.is_file():
                continue
            if self._should_skip(file_path, workspace_root):
                continue
            try:
                with open(file_path, 'rb') as fh:
                    for block in iter(lambda: fh.read(1024 * 1024), b''):
                        hasher.update(block)
            except (PermissionError, OSError):
                continue
        return hasher.hexdigest()

    def create_checkpoint(
        self,
        description: str = "",
        state: Dict = None,
        files: Optional[List[str]] = None,
    ) -> str:
        """
        Crea un nuevo checkpoint.
        - description: Descripción del checkpoint (opcional).
        - state: Estado del agente a guardar (opcional).
        - files:
            * None            → respalda el workspace COMPLETO.
            * lista NO vacía  → respaldo QUIRÚRGICO de esos archivos.
            * lista VACÍA []  → solo state.json/metadata.json, sin archivos.
              Es el caso de un paso "synthesis" (el artefacto es texto).
        Retorna el ID del checkpoint creado.
        """
        checkpoint_id = self._generate_checkpoint_id()
        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        files_dir = checkpoint_dir / FILES_DIR
  
        # Crear SIEMPRE la carpeta del checkpoint antes de escribir nada.
        # Sin esta línea:
        #   * `files=[]` (síntesis) caía al `else` y copiaba el workspace entero
        #     (lentísimo → SSE sin eventos → "conexión perdida"),
        #   * un `artifact` aún inexistente en `_copy_selected_files` no creaba
        #     ni `files_dir` ni `checkpoint_dir`, y state.json fallaba con
        #     `[Errno 2] No such file or directory`.
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        files_dir.mkdir(parents=True, exist_ok=True)
    
        logger.info(f"📸 Creando checkpoint: {checkpoint_id}")
        if description:
            logger.info(f"   Descripción: {description}")
  
        # 1. Copiar archivos: workspace completo, SOLO los artefactos pedidos,
        #    o NINGUNO si el caller pasa lista vacía (síntesis).
        if files is None:
            scope = "workspace"
            self._copy_files(self.workspace_path, files_dir)
        elif files:
            scope = "artifacts"
            self._copy_selected_files(self.workspace_path, files_dir, files)
        else:
            # Lista vacía explícita: solo state.json/metadata.json.
            scope = "state_only"
    
        # 2. Guardar estado del agente
        if state is None:
            state = {}
        state_file = checkpoint_dir / STATE_FILE
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=_json_safe)
    
        # 3. Guardar metadatos
        metadata = {
            "id": checkpoint_id,
            "timestamp": datetime.now().isoformat(),
            "description": description,
            "scope": scope,
            "files": [str(f) for f in files] if files else None,
            "file_hash": self._calculate_dir_hash(files_dir),
            "files_count": sum(1 for _ in files_dir.rglob("*") if _.is_file()),
            "state_keys": list(state.keys()) if state else [],
        }
        metadata_file = checkpoint_dir / METADATA_FILE
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False, default=_json_safe)
   
        # 4. Actualizar índice
        index = self._load_index()
        index["checkpoints"].append({
            "id": checkpoint_id,
            "timestamp": metadata["timestamp"],
            "description": description,
            "files_count": metadata["files_count"],
        })
        index["latest"] = checkpoint_id
        self._save_index(index)
  
        logger.info(f"✅ Checkpoint {checkpoint_id} creado exitosamente.")
        return checkpoint_id

    def list_checkpoints(self, limit: int = 20) -> List[Dict]:
        """Lista los checkpoints disponibles, ordenados por fecha (más reciente primero)."""
        index = self._load_index()
        checkpoints = index.get("checkpoints", [])
        # Ordenar por timestamp descendente
        checkpoints.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return checkpoints[:limit]

    def get_checkpoint_info(self, checkpoint_id: str) -> Optional[Dict]:
        """Obtiene información detallada de un checkpoint específico."""
        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        metadata_file = checkpoint_dir / METADATA_FILE
        if not metadata_file.exists():
            return None
        with open(metadata_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def load_checkpoint_state(self, checkpoint_id: str) -> Optional[Dict]:
        """Carga el estado del agente desde un checkpoint."""
        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        state_file = checkpoint_dir / STATE_FILE
        if not state_file.exists():
            return None
        with open(state_file, 'r', encoding='utf-8') as f:
            return json.load(f)

    def rollback(self, checkpoint_id: str, rollback_strategy: str = "full") -> Dict:
        """
        Restaura el estado desde un checkpoint.
        - rollback_strategy: "full" (archivos + estado), "files" (solo archivos), "state" (solo estado).
        Retorna un diccionario con el resultado de la operación.
        """
        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        if not checkpoint_dir.exists():
            return {"success": False, "error": f"Checkpoint '{checkpoint_id}' no encontrado."}

        files_dir = checkpoint_dir / FILES_DIR
        state_file = checkpoint_dir / STATE_FILE

        result = {"success": True, "restored": [], "errors": []}

        # 1. Restaurar archivos
        if rollback_strategy in ["full", "files"]:
            if files_dir.exists():
                try:
                    self._restore_files(files_dir, self.workspace_path)
                    result["restored"].append("files")
                    logger.info(f"📂 Archivos restaurados desde checkpoint {checkpoint_id}")
                except Exception as e:
                    result["errors"].append(f"Error restaurando archivos: {e}")
                    result["success"] = False
            else:
                result["errors"].append("Directorio de archivos no encontrado en checkpoint.")

        # 2. Restaurar estado del agente
        if rollback_strategy in ["full", "state"]:
            if state_file.exists():
                try:
                    with open(state_file, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    result["state"] = state
                    result["restored"].append("state")
                    logger.info(f"🧠 Estado del agente restaurado desde checkpoint {checkpoint_id}")
                except Exception as e:
                    result["errors"].append(f"Error restaurando estado: {e}")
                    result["success"] = False
            else:
                result["errors"].append("Archivo de estado no encontrado en checkpoint.")

        return result

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """Elimina un checkpoint específico."""
        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        if not checkpoint_dir.exists():
            return False

        shutil.rmtree(checkpoint_dir)
        logger.info(f"🗑️ Checkpoint {checkpoint_id} eliminado.")

        # Actualizar índice
        index = self._load_index()
        index["checkpoints"] = [c for c in index["checkpoints"] if c["id"] != checkpoint_id]
        if index.get("latest") == checkpoint_id:
            # Buscar el más reciente
            if index["checkpoints"]:
                index["latest"] = max(index["checkpoints"], key=lambda x: x["timestamp"])["id"]
            else:
                index["latest"] = None
        self._save_index(index)
        return True

    def get_latest_checkpoint(self) -> Optional[str]:
        """Devuelve el ID del checkpoint más reciente."""
        index = self._load_index()
        return index.get("latest")

    def get_file_diff(self, checkpoint_id: str) -> List[Dict]:
        """
        Calcula las diferencias entre el estado actual y un checkpoint.
        Útil para preview de rollback.
        """
        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        files_dir = checkpoint_dir / FILES_DIR
        if not files_dir.exists():
            return []

        diff = []
        for file_path in files_dir.rglob('*'):
            if file_path.is_file():
                rel_path = file_path.relative_to(files_dir)
                current_path = self.workspace_path / rel_path
                if current_path.exists():
                    # Comparar contenido
                    try:
                        with open(file_path, 'rb') as f1, open(current_path, 'rb') as f2:
                            if f1.read() != f2.read():
                                diff.append({
                                    "file": str(rel_path),
                                    "status": "modified"
                                })
                    except:
                        diff.append({
                            "file": str(rel_path),
                            "status": "error_reading"
                        })
                else:
                    diff.append({
                        "file": str(rel_path),
                        "status": "missing_in_current"
                    })

        # Buscar archivos en el workspace que no están en el checkpoint
        for file_path in self.workspace_path.rglob('*'):
            if file_path.is_file():
                rel_path = file_path.relative_to(self.workspace_path)
                checkpoint_file = files_dir / rel_path
                if not checkpoint_file.exists():
                    # Ignorar directorio de checkpoints
                    if CHECKPOINTS_DIR_NAME in str(rel_path):
                        continue
                    diff.append({
                        "file": str(rel_path),
                        "status": "new_in_current"
                    })

        return diff