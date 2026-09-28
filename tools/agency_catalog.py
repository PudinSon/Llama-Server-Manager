"""Catalog and parse the local agency-agents Markdown collection."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FIELD = re.compile(r"^(name|display_name_es|aliases|description|color|emoji|vibe):\s*(.*?)\s*$")

_SPANISH_TOKENS = {
    "architect": "Arquitecto",
    "backend": "backend",
    "frontend": "frontend",
    "developer": "Desarrollador",
    "engineer": "Ingeniero",
    "designer": "Diseñador",
    "research": "Investigación",
    "researcher": "Investigador",
    "analyst": "Analista",
    "data": "datos",
    "database": "bases de datos",
    "security": "seguridad",
    "tester": "Pruebas",
    "test": "Pruebas",
    "writer": "Redactor",
    "documentation": "Documentación",
    "devops": "DevOps",
    "marketing": "Marketing",
    "manager": "Gestor",
    "planner": "Planificador",
    "product": "producto",
    "api": "API",
    "anthropologist": "antropólogo",
    "architect": "arquitecto",
    "biologist": "biólogo",
    "chemist": "químico",
    "coach": "asesor",
    "consultant": "consultor",
    "economist": "economista",
    "historian": "historiador",
    "lawyer": "abogado",
    "linguist": "lingüista",
    "physicist": "físico",
    "psychologist": "psicólogo",
    "scientist": "científico",
    "strategist": "estratega",
    "teacher": "profesor",
    "technical": "técnico",
    "social": "social",
    "media": "medios",
    "community": "comunidad",
    "content": "contenido",
    "creative": "creativo",
    "lead": "líder",
    "expert": "experto",
}


def _spanish_display_name(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    translated = [_SPANISH_TOKENS.get(word.casefold(), word) for word in words]
    value = " ".join(translated).strip()
    return value if any(word.casefold() in _SPANISH_TOKENS for word in words) else f"Especialista en {value}"


class AgencyCatalog:
    """Parse agency agent front matter and cache it until files change."""

    def __init__(self, root: Path, cache_path: Optional[Path] = None) -> None:
        self.root = root
        self.cache_path = cache_path
        self._agents: List[Dict[str, Any]] = []
        self._signature: Optional[str] = None
        self._lock = threading.Lock()

    def _files_signature(self, files: List[Path]) -> str:
        return "|".join(f"{path}:{path.stat().st_mtime_ns}" for path in files)

    def _parse_file(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        match = _FRONT_MATTER.match(content)
        if not match:
            return None
        metadata: Dict[str, str] = {}
        for line in match.group(1).splitlines():
            field = _FIELD.match(line)
            if field:
                metadata[field.group(1)] = field.group(2).strip().strip('"')
        if not metadata.get("name"):
            return None
        aliases = [alias.strip() for alias in metadata.get("aliases", "").split(",") if alias.strip()]
        display_name_es = metadata.get("display_name_es") or _spanish_display_name(metadata["name"])
        return {
            "aliases": aliases,
            "category": path.parent.name,
            "description": metadata.get("description", ""),
            "display_name_es": display_name_es,
            "emoji": metadata.get("emoji", ""),
            "color": metadata.get("color", ""),
            "name": metadata["name"],
            "path": path.relative_to(self.root).as_posix(),
            "vibe": metadata.get("vibe", "")
        }

    def load(self) -> List[Dict[str, Any]]:
        with self._lock:
            files = sorted(self.root.rglob("*.md")) if self.root.exists() else []
            signature = self._files_signature(files)
            if signature == self._signature:
                return list(self._agents)
            agents = [agent for path in files if (agent := self._parse_file(path))]
            agents.sort(key=lambda agent: (agent["category"], agent["name"]))
            self._agents = agents
            self._signature = signature
            if self.cache_path:
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self.cache_path.write_text(json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError:
                    pass
            return list(agents)

    def search(self, query: str = "") -> List[Dict[str, Any]]:
        agents = self.load()
        query = query.strip().casefold()
        if not query:
            return agents
        return [
            agent for agent in agents
            if query in agent["name"].casefold()
            or query in agent.get("display_name_es", "").casefold()
            or any(query in alias.casefold() for alias in agent.get("aliases", []))
            or query in agent["description"].casefold()
            or query in agent["category"].casefold()
        ]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        target = name.strip().casefold()
        return next((agent for agent in self.load() if agent["name"].casefold() == target), None)

    def instructions(self, name: str) -> Optional[str]:
        agent = self.get(name)
        if not agent:
            return None
        try:
            content = (self.root / agent["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        match = _FRONT_MATTER.match(content)
        return content[match.end():].strip() if match else content.strip()

    # ------------------------------------------------------------------
    # CRUD para el gestor de agentes de la WebUI
    # ------------------------------------------------------------------
    def full(self, name: str) -> Optional[Dict[str, Any]]:
        """Agente + markdown completo (para el preview del gestor)."""
        agent = self.get(name)
        if not agent:
            return None
        try:
            content = (self.root / agent["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            content = ""
        return {**agent, "content": content, "instructions": self.instructions(name) or ""}

    @staticmethod
    def _slug(name: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip().lower()).strip("-.")
        return slug[:80]

    def create(self, name: str, description: str = "", category: str = "custom",
               body: str = "", display_name_es: str = "", emoji: str = "",
               color: str = "", vibe: str = "", aliases: Optional[List[str]] = None,
               overwrite: bool = False) -> Dict[str, Any]:
        """Crea un agente `.md` con frontmatter válido dentro de app_data."""
        slug = self._slug(name)
        if not slug:
            raise ValueError("Nombre de agente inválido (usa letras, números, guiones)")
        if self.get(slug) and not overwrite:
            raise FileExistsError(f"El agente '{slug}' ya existe")

        safe_category = self._slug(category) or "custom"
        target = self.root / safe_category / f"{slug}.md"
        target.parent.mkdir(parents=True, exist_ok=True)

        front = ["---", f"name: {slug}"]
        if display_name_es:
            front.append(f"display_name_es: {display_name_es}")
        if description:
            front.append(f"description: {description}")
        if aliases:
            front.append("aliases: " + ", ".join(str(a).strip() for a in aliases if str(a).strip()))
        if emoji:
            front.append(f"emoji: {emoji}")
        if color:
            front.append(f"color: {color}")
        if vibe:
            front.append(f"vibe: {vibe}")
        front.append("---")

        instructions = (body or "").strip() or f"Eres {display_name_es or slug}. Describe tu experiencia aquí."
        target.write_text("\n".join(front) + "\n\n" + instructions + "\n", encoding="utf-8")
        self._signature = None  # fuerza recarga del catálogo
        return self.get(slug) or {}

    def save_raw(self, name: str, raw: str) -> Dict[str, Any]:
        """Crea o sobreescribe el `.md` de un agente con contenido markdown crudo.

        Usado por el gestor de agentes (editar el texto completo e importar .md).
        El nombre del agente se toma del frontmatter si existe; si no, del slug.
        """
        raw = (raw or "").replace("\\r\\n", "\\n")
        existing = self.get(name)
        if existing:
            target = self.root / existing["path"]
        else:
            # Si el frontmatter trae un `name`, respétalo como slug.
            slug = self._slug(name)
            m = re.match(r"^---\\s*\\n(.*?)\\n---", raw, re.DOTALL)
            if m:
                nm = re.search(r"^name:\\s*(.+)$", m.group(1), re.MULTILINE)
                if nm:
                    slug = self._slug(nm.group(1).strip()) or slug
            if not slug:
                raise ValueError("Nombre de agente inválido (usa letras, números, guiones)")
            target = self.root / "custom" / f"{slug}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw, encoding="utf-8")
        self._signature = None  # fuerza recarga del catálogo
        key = existing["name"] if existing else target.stem
        return self.get(key) or {}

    def delete(self, name: str) -> bool:
        """Elimina un agente del catálogo (a la papelera si se puede)."""
        agent = self.get(name)
        if not agent:
            return False
        target = self.root / agent["path"]
        try:
            try:
                from send2trash import send2trash  # type: ignore
                send2trash(str(target))
            except Exception:
                target.unlink()
        except OSError:
            return False
        self._signature = None
        return True


def create_default_catalog() -> AgencyCatalog:
    from . import runtime

    data_dir = Path(runtime.get_data_dir())
    root = data_dir / "agency-agents-main"
    if not root.exists():
        root = data_dir / "agency-agents"
    cache = Path(runtime.get_user_data_dir()) / "agency_agents_catalog.json"
    return AgencyCatalog(root, cache)
