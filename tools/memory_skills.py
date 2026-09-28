# tools/memory_skills.py
# Módulo para la gestión de skills (memoria procedimental)
# Skills: unidades de conocimiento reutilizables que el agente puede cargar y ejecutar.

import os
import json
import re
import shutil
import threading
import time
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

import yaml
import logging

logger = logging.getLogger("memory_skills")

# ---------------------------------------------------------------------------
# Dependencias opcionales: numpy (para el índice vectorial) y requests
# (para hablar con el servidor Nomic). Si faltan, se degrada a búsqueda
# léxica sin romper la app.
# ---------------------------------------------------------------------------
try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None
    _NUMPY_AVAILABLE = False

try:
    import requests
except ImportError:
    requests = None

# ---------------------------------------------------------------------------
# Configuración del índice vectorial
# ---------------------------------------------------------------------------
_SKILL_EMB_ENDPOINT = os.environ.get(
    "SKILLS_EMB_ENDPOINT", "http://localhost:8082/v1/embeddings"
)
_SKILL_EMB_TIMEOUT = float(os.environ.get("SKILLS_EMB_TIMEOUT", "30"))
_SKILL_EMB_BATCH = int(os.environ.get("SKILLS_EMB_BATCH", "50"))
_SKILL_EMB_PREFIX_DOC = "search_document: "
_SKILL_EMB_PREFIX_QUERY = "search_query: "

# Cuántos caracteres del SKILL.md se inyectan en el prompt. 2000 da cabida al
# DEFAULT_BASE_SKILL completo (~1000) y a skills reales sin cortar pasos.
_SKILL_MAX_CONTENT_CHARS = int(os.environ.get("SKILLS_MAX_CONTENT_CHARS", "2000"))

# Umbral de inyección (score combinado) y pesos. Score = sim_weight * similitud
# + conf_weight * confidence. Con 0.7/0.3 y sim 0.5 + conf 0.5 da 0.5, que
# pasa el umbral.
_SKILL_MIN_SCORE = float(os.environ.get("SKILLS_MIN_SCORE", "0.35"))
_SKILL_SIM_WEIGHT = float(os.environ.get("SKILLS_SIM_WEIGHT", "0.7"))
_SKILL_CONF_WEIGHT = float(os.environ.get("SKILLS_CONF_WEIGHT", "0.3"))

# Feedback loop: ajuste de confidence al terminar cada tarea que usó el skill.
_SKILL_CONF_DELTA = 0.05
_SKILL_CONF_MIN = 0.1
_SKILL_CONF_MAX = 1.0

# ============================================================================
# CONSTANTES Y RUTAS
# ============================================================================

SKILLS_DIR_NAME = ".skills"
CENTRAL_SKILLS_DIR_NAME = "skills"
AUTO_GENERATED_DIR = "auto-generated"
INDEX_FILE = "INDEX.md"
LEARNINGS_FILE = "LEARNINGS.md"
ACTIVE_FILE = "active.json"

DEFAULT_BASE_SKILL = """\
---
name: "base"
description: "Operaciones básicas del agente: explorar el workspace, leer y escribir archivos, y consultar su estado. Skill de ejemplo y por defecto."
version: "1.0"
created: "2026-01-01T00:00:00Z"
updated: "2026-01-01T00:00:00Z"
author: "sistema"
tags: [base, ejemplo, utilidades, workspace]
confidence: 0.9
auto_generated: false
---

# Skill base

Skill que el sistema crea por defecto. Sirve como **plantilla de ejemplo** del
formato de una skill y como conjunto mínimo de operaciones reutilizables:
navegar por el workspace, leer/crear archivos y comprobar el estado del agente.

## Pasos

### Paso 1: Explorar el workspace
- **Herramienta**: `list_directory`
- **Argumentos**: `{"path": "."}`

### Paso 2: Leer un archivo
- **Herramienta**: `read_file`
- **Argumentos**: `{"path": "ruta/relativa/al/archivo"}`

### Paso 3: Escribir o crear un archivo
- **Herramienta**: `write_file`
- **Argumentos**: `{"path": "ruta/relativa", "content": "contenido del archivo"}`

## Consejos

- Usa siempre rutas relativas al workspace para que las operaciones respeten
  `safe_path` y no escapen del directorio de trabajo.
- Verifica la ruta con `list_directory` antes de leerla o escribirla.
- Para procesos largos usa `run_command_async` y consulta con `job_output`.
"""

def get_workspace_skills_dir(workspace_path: str) -> Path:
    """Devuelve la ruta al directorio de skills del workspace."""
    return Path(workspace_path) / SKILLS_DIR_NAME

def get_global_skills_dir() -> Path:
    """Devuelve la ruta central de skills compartidos en app_data."""
    try:
        from . import runtime
        return Path(runtime.get_data_dir()) / CENTRAL_SKILLS_DIR_NAME
    except:
        return Path.cwd() / CENTRAL_SKILLS_DIR_NAME

# ============================================================================
# MOTOR DE BÚSQUEDA VECTORIAL (numpy + Nomic HTTP)
#
# Filosofía:
#   * El modelo Nomic vive en :8082 (ya arrancado por el Manager). No
#     cargamos ningún ONNX en proceso.
#   * La matriz de embeddings de los skills se cachea por FIRMA (paths +
#     mtimes). Cambiar un SKILL.md invalida la caché y se reconstruye en el
#     siguiente `search_skills`.
#   * La búsqueda en sí es un producto punto (np.dot) L2-normalizado, ~µs
#     para N<1000. Es lo que reemplaza al HNSW de Chroma.
# ============================================================================

_skill_emb_cache: Dict[str, Tuple[Tuple[str, ...], "np.ndarray", Dict[str, int]]] = {}
_skill_emb_lock = threading.Lock()


def _skills_signature(workspace_skills_dir: Path, global_skills_dir: Path) -> str:
    """Firma barata: hash de paths + mtime_ns de todos los SKILL.md.

    Cambia si se crea, edita, promociona o borra un skill en cualquier scope.
    """
    digest = hashlib.sha1()
    for base in (workspace_skills_dir, global_skills_dir):
        if not base.exists():
            continue
        try:
            for md in sorted(base.rglob("SKILL.md")):
                try:
                    digest.update(str(md).encode("utf-8"))
                    digest.update(b":")
                    digest.update(str(md.stat().st_mtime_ns).encode("ascii"))
                    digest.update(b"|")
                except OSError:
                    continue
        except OSError:
            continue
    return digest.hexdigest()


def _embed_skill_texts(texts: List[str], kind: str = "document") -> Optional[List[List[float]]]:
    """Embeddings vía Nomic (:8082). Devuelve None si el servicio no responde.

    `kind` selecciona el prefijo correcto (`search_document:` para skills,
    `search_query:` para la consulta). Nomic fue entrenado con esos prefijos.
    """
    if not texts:
        return []
    if requests is None:
        return None
    prefix = _SKILL_EMB_PREFIX_DOC if kind == "document" else _SKILL_EMB_PREFIX_QUERY
    out: List[List[float]] = []
    for i in range(0, len(texts), _SKILL_EMB_BATCH):
        chunk = [prefix + str(t) for t in texts[i:i + _SKILL_EMB_BATCH]]
        try:
            resp = requests.post(
                _SKILL_EMB_ENDPOINT,
                json={"input": chunk},
                timeout=_SKILL_EMB_TIMEOUT,
            )
        except Exception as exc:
            logger.debug("Skills embeddings: fallo HTTP (%s)", exc)
            return None
        if resp.status_code != 200:
            logger.debug("Skills embeddings: HTTP %s", resp.status_code)
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        vectors = [item.get("embedding") for item in data.get("data", [])]
        if len(vectors) != len(chunk) or any(v is None for v in vectors):
            return None
        out.extend(vectors)
    return out


def _build_skill_matrix(
    skills: List[Dict[str, Any]],
    signature: str,
) -> Optional[Tuple[Tuple[str, ...], "np.ndarray", Dict[str, int]]]:
    """Construye (names, matrix_l2, by_name) y lo cachea. None si Nomic falla."""
    if not _NUMPY_AVAILABLE or not skills:
        return None

    names: List[str] = []
    texts: List[str] = []
    for skill in skills:
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        names.append(name)
        texts.append(" | ".join(filter(None, [
            name,
            str(skill.get("description") or ""),
            " ".join(skill.get("tags") or []),
        ])))

    vectors = _embed_skill_texts(texts, kind="document")
    if not vectors:
        return None

    matrix = np.asarray(vectors, dtype="float32")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms
    state = (tuple(names), matrix, {n: i for i, n in enumerate(names)})

    with _skill_emb_lock:
        _skill_emb_cache.clear()   # solo guardamos la última firma
        _skill_emb_cache[signature] = state
    logger.debug("Skills: matriz cacheada (firma=%s, N=%d)", signature[:8], len(names))
    return state


def _adjust_skill_confidence(path: Path, delta: float) -> Optional[float]:
    """Suma `delta` al campo `confidence` del frontmatter del SKILL.md.

    Devuelve la nueva confidence o None si el archivo no es parseable. Respeta
    los límites [_SKILL_CONF_MIN, _SKILL_CONF_MAX].
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except Exception:
        return None
    try:
        old = float(fm.get("confidence", 0.5))
    except (TypeError, ValueError):
        old = 0.5
    new = max(_SKILL_CONF_MIN, min(_SKILL_CONF_MAX, old + float(delta)))
    if abs(new - old) < 1e-6:
        return old
    fm["confidence"] = round(new, 3)
    fm["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = parts[2]
    new_content = "---\n" + yaml.dump(fm, default_flow_style=False, allow_unicode=True) + "---" + body
    try:
        path.write_text(new_content, encoding="utf-8")
    except OSError:
        return None
    return new

# ============================================================================
# CLASE SKILLMANAGER
# ============================================================================

class SkillManager:
    """
    Gestor de skills (memoria procedimental).
    Administra skills locales (workspace) y globales (app_data/skills).
    Búsqueda semántica vía numpy + Nomic HTTP (con fallback léxico).
    """
    def __init__(self, workspace_path: Optional[str] = None, enable_semantic: Optional[bool] = None):
        self.workspace_path = workspace_path or os.getcwd()
        self.workspace_skills_dir = get_workspace_skills_dir(self.workspace_path)
        self.global_skills_dir = get_global_skills_dir()
        self._active_skills: set = self._load_active_skills()
        self._ensure_directories()
        self._ensure_default_skill()
        self._load_index()

        # Flag histórico conservado por compat: ahora el motor vectorial se
        # auto-gestiona (si Nomic no responde, cae a búsqueda léxica).
        if enable_semantic is None:
            env = os.environ.get("SKILLS_SEMANTIC", "1").strip().lower()
            enable_semantic = env not in {"0", "false", "no", "off"}
        self.semantic_enabled = bool(enable_semantic) and _NUMPY_AVAILABLE and requests is not None


    def _load_active_skills(self) -> set:
        """Carga los skills activos de forma persistente."""
        try:
            active_path = get_global_skills_dir() / ACTIVE_FILE
            data = json.loads(active_path.read_text(encoding="utf-8")) if active_path.exists() else []
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()

    def _save_active_skills(self) -> None:
        try:
            active_path = self.global_skills_dir / ACTIVE_FILE
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text(
                json.dumps(sorted(self._active_skills), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as error:
            logger.warning(f"No se pudo persistir skills activos: {error}")

    def _ensure_default_skill(self):
        """Crea la skill base por defecto (global) si aún no existe.

        Sirve de plantilla y garantiza que el área de skills tenga siempre un
        ejemplo usable nada más arrancar.
        """
        base_name = "base"
        skill_dir = self.global_skills_dir / base_name
        skill_path = skill_dir / "SKILL.md"
        if skill_path.exists():
            return
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(DEFAULT_BASE_SKILL, encoding="utf-8")
            logger.info("Skill base por defecto creada en %s", skill_path)
        except Exception as error:
            logger.warning(f"No se pudo crear la skill base por defecto: {error}")

    def _ensure_directories(self):
        """Crea las estructuras de directorios necesarias."""
        for dir_path in [self.workspace_skills_dir, self.global_skills_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)
            (dir_path / AUTO_GENERATED_DIR).mkdir(parents=True, exist_ok=True)
            # Crear INDEX.md si no existe
            index_path = dir_path / INDEX_FILE
            if not index_path.exists():
                index_path.write_text("# Skills Index\n\n", encoding='utf-8')

    def _load_index(self):
        """Carga el índice de skills (global, workspace y auto-generados) en memoria."""
        self.index = {"workspace": [], "global": [], "auto_generated": []}
        self._scan_skills(self.workspace_skills_dir, "workspace")
        self._scan_skills(self.global_skills_dir, "global")
        # Ojo: `_scan_skills(workspace)` ya recoge los auto-generados en su
        # sub-bucle, así que NO hay que volver a escanear ese directorio (los
        # duplicaría en el listado).

    def _scan_skills(self, base_dir: Path, scope: str):
        """Escanea un directorio base y actualiza el índice."""
        if not base_dir.exists():
            return
        # Skills en la raíz
        for skill_dir in base_dir.iterdir():
            if skill_dir.is_dir() and skill_dir.name != AUTO_GENERATED_DIR:
                skill_path = skill_dir / "SKILL.md"
                if skill_path.exists():
                    skill_data = self._parse_skill_file(skill_path)
                    if skill_data:
                        self.index[scope].append(skill_data)
        # Skills auto-generados
        auto_dir = base_dir / AUTO_GENERATED_DIR
        if auto_dir.exists():
            for skill_dir in auto_dir.iterdir():
                if skill_dir.is_dir():
                    skill_path = skill_dir / "SKILL.md"
                    if skill_path.exists():
                        skill_data = self._parse_skill_file(skill_path)
                        if skill_data:
                            skill_data["auto_generated"] = True
                            self.index["auto_generated"].append(skill_data)

    def _parse_skill_file(self, skill_path: Path) -> Optional[Dict]:
        """Parsea un archivo SKILL.md y devuelve un diccionario con los metadatos."""
        try:
            content = skill_path.read_text(encoding='utf-8')
            # Extraer frontmatter YAML (si existe)
            frontmatter = {}
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    try:
                        frontmatter = yaml.safe_load(parts[1])
                    except:
                        pass
                    content = parts[2]
            # Extraer nombre del directorio
            skill_name = skill_path.parent.name
            # Usar nombre del frontmatter si existe
            name = frontmatter.get('name', skill_name)
            description = frontmatter.get('description', '')
            tags = frontmatter.get('tags', [])
            version = frontmatter.get('version', '1.0')
            confidence = frontmatter.get('confidence', 0.5)
            if skill_path.parent.parent == self.workspace_skills_dir:
                scope = "workspace"
            elif skill_path.parent.parent == self.workspace_skills_dir / AUTO_GENERATED_DIR:
                scope = "auto_generated"
            else:
                scope = "global"
            return {
                "name": name,
                "path": str(skill_path),
                "dir": str(skill_path.parent),
                "description": description,
                "tags": tags,
                "version": version,
                "confidence": confidence,
                "frontmatter": frontmatter,
                "content": content,
                "auto_generated": scope == "auto_generated",
                "scope": scope
            }
        except Exception as e:
            logger.error(f"Error parseando skill {skill_path}: {e}")
            return None

    def list_skills(self, scope: Optional[str] = None, include_auto: bool = False) -> List[Dict]:
        """Lista todos los skills disponibles."""
        skills = []
        if scope is None or scope == "workspace":
            skills.extend(self.index["workspace"])
        if scope is None or scope == "global":
            skills.extend(self.index["global"])
        if include_auto:
            skills.extend(self.index["auto_generated"])
        return skills

    # ------------------------------------------------------------------
    # Búsqueda: semántica (numpy + Nomic) con fallback léxico.
    # ------------------------------------------------------------------
    def search_skills(
        self,
        query: str,
        top_k: int = 5,
        only_active: bool = True,
    ) -> List[Dict]:
        """Devuelve los skills más relevantes para `query`.

        `only_active=True` (default): filtra por el set de skills activos
        (`active.json`). Antes el parámetro se llamaba `include_auto` y su
        semántica era confusa (en Chroma filtraba por `active`, en el fallback
        no filtraba nada). Ahora es explícito.
        """
        query = (query or "").strip()
        if not query:
            return []

        # Gate determinista: si no hay ningún candidato, ni siquiera
        # intentamos embeddings (evita abrir HTTP y descartar luego).
        candidates = self._search_candidate_names(only_active)
        if not candidates:
            return []

        hits = self._search_semantic(query, top_k, only_active)
        if hits:
            return hits

        return self._search_simple(query, top_k, only_active)

    def _search_candidate_names(self, only_active: bool) -> List[str]:
        """Lista de nombres de skill a considerar según `only_active`."""
        if only_active:
            return sorted(self._active_skills)
        names: List[str] = []
        for scope in ("workspace", "global", "auto_generated"):
            for skill in self.index.get(scope, []):
                name = str(skill.get("name") or "").strip()
                if name:
                    names.append(name)
        return names

    def _search_semantic(
        self,
        query: str,
        top_k: int,
        only_active: bool,
    ) -> List[Dict]:
        """Top-k por similitud coseno + confidence. [] si no hay motor."""
        if not self.semantic_enabled:
            return []

        signature = _skills_signature(self.workspace_skills_dir, self.global_skills_dir)
        with _skill_emb_lock:
            state = _skill_emb_cache.get(signature)
        if state is None:
            state = _build_skill_matrix(self.list_skills(include_auto=True), signature)
        if state is None:
            return []
        names, matrix, _by_name = state

        q_vecs = _embed_skill_texts([query], kind="query")
        if not q_vecs:
            return []
        try:
            q = np.asarray(q_vecs[0], dtype="float32")
        except Exception:
            return []
        qn = float(np.linalg.norm(q))
        if qn <= 0:
            return []
        q = q / qn

        sims = matrix @ q  # (N,)  — el "vector search" real

        scored: List[Tuple[float, float, float, Dict]] = []
        for idx, name in enumerate(names):
            if only_active and name not in self._active_skills:
                continue
            skill = self.get_skill(name)
            if not skill:
                continue
            try:
                conf = float(skill.get("confidence", 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            sim = float(sims[idx])
            score = _SKILL_SIM_WEIGHT * sim + _SKILL_CONF_WEIGHT * conf
            scored.append((score, sim, conf, skill))

        if not scored:
            return []
        scored.sort(key=lambda item: (-item[0], item[3].get("name", "")))

        out: List[Dict] = []
        for score, sim, _conf, skill in scored[:top_k]:
            if score < _SKILL_MIN_SCORE:
                continue
            entry = dict(skill)
            entry["_score"] = round(score, 4)
            entry["_similarity"] = round(sim, 4)
            out.append(entry)
        return out

    def _search_simple(
        self,
        query: str,
        top_k: int,
        only_active: bool,
    ) -> List[Dict]:
        """Fallback léxico. Ahora RESPETA `only_active` (antes no lo hacía)."""
        ql = query.lower()
        scored: List[Tuple[int, Dict]] = []
        for skill in self.list_skills(include_auto=True):
            name = str(skill.get("name") or "")
            if only_active and name not in self._active_skills:
                continue
            score = 0
            if ql in name.lower():
                score += 10
            if ql in str(skill.get("description") or "").lower():
                score += 5
            for tag in skill.get("tags", []) or []:
                if ql in str(tag).lower():
                    score += 3
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda item: (-item[0], item[1].get("name", "")))
        return [s for _, s in scored[:top_k]]

    def get_skill(self, skill_name: str) -> Optional[Dict]:
        """Obtiene un skill por su nombre (búsqueda en todos los scopes)."""
        # Buscar en workspace, luego global, luego auto_generated
        for scope in ["workspace", "global", "auto_generated"]:
            for skill in self.index.get(scope, []):
                if skill["name"] == skill_name:
                    return skill
        return None

    def update_skill(self, skill_name: str, content: str,
                     scope: Optional[str] = None) -> bool:
        """Reescribe el SKILL.md de un skill existente (edición desde la UI).

        `content` es el archivo completo (frontmatter YAML incluido). El nombre
        del directorio no cambia: si el frontmatter declara otro `name`, ese pasa
        a ser el nombre visible del skill.
        """
        if not content or not content.strip():
            logger.warning("Contenido vacío al actualizar el skill '%s'.", skill_name)
            return False
        skill = self.get_skill(skill_name)
        if not skill:
            logger.warning(f"Skill '{skill_name}' no encontrado para actualizar.")
            return False
        if scope and skill.get("scope") and skill["scope"] != scope:
            logger.warning("El skill '%s' no pertenece al scope '%s'.", skill_name, scope)
            return False
        target = Path(skill["path"])
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.error(f"No se pudo escribir el skill {target}: {e}")
            return False

        # Refrescar solo la entrada editada: reescanear el directorio duplicaría
        # entradas porque `_scan_skills` añade en lugar de reemplazar.
        bucket = "auto_generated" if skill.get("auto_generated") else skill.get("scope", "workspace")
        entries = self.index.get(bucket, [])
        for position, existing in enumerate(entries):
            if existing.get("path") == skill["path"]:
                parsed = self._parse_skill_file(target)
                if parsed is not None:
                    entries[position] = parsed
                break
        logger.info("✏️ Skill '%s' actualizado (%s)", skill_name, target)
        return True

    def create_skill(self, name: str, description: str, steps: List[Dict],
                     tags: List[str] = None, version: str = "1.0",
                     auto_generated: bool = False) -> str:
        """
        Crea un nuevo skill en el workspace.
        steps: lista de diccionarios con {"tool": "nombre", "arguments": {...}, "description": "..."}
        """
        # Sanitizar nombre
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name).lower()
        if auto_generated:
            base_dir = self.workspace_skills_dir / AUTO_GENERATED_DIR
        else:
            base_dir = self.workspace_skills_dir
        skill_dir = base_dir / safe_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"

        # Construir contenido
        frontmatter = {
            "name": name,
            "description": description,
            "version": version,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "author": "agent",
            "tags": tags or [],
            "confidence": 0.3 if auto_generated else 0.7,
            "auto_generated": auto_generated
        }
        yaml_content = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)

        # Pasos en Markdown
        steps_md = "\n".join([
            f"### Paso {i+1}: {step.get('description', 'Sin descripción')}\n"
            f"- **Herramienta**: `{step.get('tool', '')}`\n"
            f"- **Argumentos**: `{json.dumps(step.get('arguments', {}), ensure_ascii=False)}`\n"
            for i, step in enumerate(steps)
        ])

        content = f"""---
{yaml_content}
---

# {name}

## Descripción
{description}

## Pasos
{steps_md}

## Ejemplo de Uso
(El agente cargará este skill automáticamente cuando sea relevante.)

## Notas
- Skill {'auto-generado' if auto_generated else 'creado manualmente'}.
- Confianza: {frontmatter['confidence']}
"""
        skill_path.write_text(content, encoding='utf-8')
        logger.info(f"Skill '{name}' creado en {skill_path}")
        # Actualizar índice
        self._scan_skills(self.workspace_skills_dir, "workspace")
        return str(skill_path)

    def promote_skill(self, skill_name: str) -> bool:
        """Promueve un skill auto-generado a skill permanente."""
        # Buscar en auto_generated
        for skill in self.index["auto_generated"]:
            if skill["name"] == skill_name:
                src_dir = Path(skill["dir"])
                dest_dir = self.workspace_skills_dir / skill_name
                # Mover el directorio
                if src_dir.exists() and not dest_dir.exists():
                    shutil.move(str(src_dir), str(dest_dir))
                    logger.info(f"Skill '{skill_name}' promovido a permanente.")
                    self._scan_skills(self.workspace_skills_dir, "workspace")
                    self._scan_skills(self.workspace_skills_dir, "auto_generated")
                    return True
                else:
                    logger.warning(f"No se pudo promover '{skill_name}': destino ya existe o origen no encontrado.")
                    return False
        logger.warning(f"Skill '{skill_name}' no encontrado en auto-generated.")
        return False

    def delete_skill(self, skill_name: str, scope: str = "workspace") -> bool:
        """Elimina un skill (solo workspace, no globales por seguridad)."""
        if scope not in ["workspace", "auto_generated"]:
            logger.warning("Solo se pueden eliminar skills del workspace o auto-generated.")
            return False
        base_dir = self.workspace_skills_dir if scope == "workspace" else self.workspace_skills_dir / AUTO_GENERATED_DIR
        target_dir = base_dir / skill_name
        if target_dir.exists():
            shutil.rmtree(target_dir)
            logger.info(f"Skill '{skill_name}' eliminado.")
            # `_scan_skills` añade al índice (no lo reemplaza), así que hay que
            # recargarlo entero o los skills sobrevivientes quedan duplicados.
            self._load_index()
            
            # Desactivar del conjunto activo
            self._active_skills.discard(skill_name)
            self._save_active_skills()
            
            return True
        return False
    
    # ============================================================================
    # GESTIÓN DE SKILLS ACTIVOS (activar/desactivar)
    # ============================================================================
    
    def activate_skill(self, skill_name: str) -> bool:
        """Activa un skill para que sea inyectado en el contexto del agente."""
        skill = self.get_skill(skill_name)
        if not skill:
            logger.warning(f"Skill '{skill_name}' no encontrado")
            return False
        self._active_skills.add(skill_name)
        self._save_active_skills()
        logger.info(f"✅ Skill '{skill_name}' activado")
        return True

    def deactivate_skill(self, skill_name: str) -> bool:
        """Desactiva un skill."""
        if skill_name not in self._active_skills:
            return False
        self._active_skills.discard(skill_name)
        self._save_active_skills()
        logger.info(f"🔇 Skill '{skill_name}' desactivado")
        return True
    
    def get_active_skills(self) -> List[Dict]:
        """Devuelve lista de skills activos."""
        return [self.get_skill(name) for name in self._active_skills if self.get_skill(name)]
    
    def get_relevant_skills_for_task(self, task_description: str, limit: int = 3) -> List[Dict]:
        """
        Obtiene skills relevantes para una tarea específica.
        Primero devuelve skills activos relevantes, luego otros relevantes.
        """
        # Buscar skills relevantes
        relevant = self.search_skills(task_description, top_k=limit*2)
        
        # Priorizar skills activos
        active = self.get_active_skills()
        result = []
        
        # Agregar primero los skills activos que sean relevantes
        for skill in active:
            if skill in relevant and len(result) < limit:
                result.append(skill)
        
        # Agregar otros skills relevantes hasta completar el límite
        for skill in relevant:
            if skill not in result and len(result) < limit:
                result.append(skill)
        
        return result[:limit]
    
    # ------------------------------------------------------------------
    # Feedback loop: ajusta la confianza del skill según el resultado real.
    # ------------------------------------------------------------------
    def record_outcome(self, skill_name: str, success: bool) -> bool:
        """Sube/baja la confidence del skill tras una tarea que lo inyectó.

        Sube +0.05 si `success`, baja -0.05 si no. Persiste en el frontmatter
        del SKILL.md. Idempotente y tolerante a errores (I/O, YAML roto, etc.).
        """
        skill = self.get_skill(skill_name)
        if not skill:
            return False
        path = Path(skill["path"])
        if not path.exists():
            return False
        delta = _SKILL_CONF_DELTA if success else -_SKILL_CONF_DELTA
        new_conf = _adjust_skill_confidence(path, delta)
        if new_conf is None:
            return False
        # Refrescar en memoria para que la próxima búsqueda vea la nueva conf.
        skill["confidence"] = new_conf
        logger.debug(
            "Skill '%s': confidence %s a %.3f",
            skill_name, "subida" if success else "bajada", new_conf,
        )
        return True

    def inject_skills_into_context(self, task_description: str, max_skills: int = 3) -> str:
        """
        Genera una inyección de skills para el contexto del agente.
        Devuelve un string formateado con los skills más relevantes.
        """
        relevant_skills = self.get_relevant_skills_for_task(task_description, limit=max_skills)
        
        if not relevant_skills:
            return ""
        
        context = "## 📚 Skills Disponibles\n\n"
        for skill in relevant_skills:
            context += f"### {skill['name']}\n"
            context += f"{skill['description']}\n"
            if skill.get('tags'):
                context += f"*Tags: {', '.join(skill['tags'])}*\n"
            context += "\n"
        
        return context


# ============================================================================
# Singleton por workspace
#
# Antes cada `Agent.__init__` creaba su propio SkillManager → N clientes
# Chroma, N cargas de modelo ONNX, N reindexados. Ahora hay uno por workspace
# y todos los Agent lo reutilizan.
# ============================================================================

_skill_managers: Dict[str, SkillManager] = {}
_skill_managers_lock = threading.Lock()


def get_skill_manager(workspace_path: Optional[str] = None) -> SkillManager:
    """Devuelve (creando si hace falta) el SkillManager del workspace."""
    key = str(Path(workspace_path or os.getcwd()).resolve())
    with _skill_managers_lock:
        manager = _skill_managers.get(key)
        if manager is None:
            manager = SkillManager(key)
            _skill_managers[key] = manager
        return manager


def reset_skill_managers() -> None:
    """Olvida todos los singletons (tests / cambio de config)."""
    global _skill_managers
    with _skill_managers_lock:
        _skill_managers = {}