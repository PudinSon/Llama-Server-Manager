"""Sub-catálogo: qué agentes pertenecen a qué dominio de trabajo.

El catálogo tiene 273 agentes repartidos en 17 áreas (`engineering`, `marketing`,
`specialized`, `gis`, …). Cargarlo entero para una tarea de código mete en el
shortlist agentes de ventas, salud o hospitality: el supervisor elige peor
cuando la lista es ruido, y el plan sale inflado con especialistas que no vienen
al caso.

Este módulo es la ÚNICA pieza que traduce "área de la colección" a "dominio de
trabajo" (`task_router.DOMAINS`), para que `_resolve_agency_selection` pueda
preguntar por el subconjunto relevante y solo después construir el shortlist.

Nada se mueve del disco y ningún `.md` se edita: esto es una VISTA sobre el
catálogo. El mapeo es determinista y vive en código (versionado) porque los
agentes no están versionados en el repo nuevo.

Reglas (en orden):

1. Override explícito por agente (`domains.json`, ver abajo) — manda siempre.
2. Dominio previo del ÁREA: primera carpeta conocida subiendo por la ruta del
   `.md` (los agentes de `game-development/unity/…` son de `game-development`).
3. Refinamiento por palabras del propio agente (slug + `vibe` + `description`):
   `engineering-devops-automator` es `coding` por área y `ops` por palabra.
4. Área desconocida (p. ej. un agente creado por el usuario en `custom/`) se
   considera universal: NUNCA se esconde por no estar en la tabla.

Dos salvaguardas contra esconder de más:

* `UNIVERSAL_AGENTS` (orquestadores, generalistas, revisores) entra siempre.
* Si el filtro deja menos de `MIN_POOL` candidatos, se devuelve el catálogo
  completo: un filtro que esconde casi todo es peor que no filtrar.

Override opcional (`<root>/domains.json`, junto al catálogo cargado):

    {
      "categories": {"specialized": ["business"]},
      "agents":     {"mi-agente": ["coding", "ops"]},
      "universal":  ["mi-generalista"]
    }

`categories` y `agents` SUSTITUYEN el valor por defecto; `universal` añade.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from . import task_router

logger = logging.getLogger("agency_domains")

#: Dominios válidos, en orden canónico. Mismo contrato que `task_router.DOMAINS`.
ALL_DOMAINS: Tuple[str, ...] = tuple(task_router.DOMAINS)

#: Dominio previo por ÁREA (primera carpeta de la ruta). Un área homogénea
#: declara uno; las mezcladas declaran dos y el refinamiento por palabras
#: decide cada agente. Las áreas que no aparecen aquí son universales (nunca se
#: esconden), así que añadir una carpeta nueva al catálogo no rompe nada.
CATEGORY_DOMAINS: Dict[str, Tuple[str, ...]] = {
    "academic": ("research",),
    "design": ("creative",),
    "engineering": ("coding",),
    "finance": ("business", "math"),
    "game-development": ("coding", "creative"),
    "gis": ("research", "coding"),
    "healthcare": ("research", "business"),
    "marketing": ("business", "creative"),
    "paid-media": ("business", "creative"),
    "product": ("business",),
    "project-management": ("business", "ops"),
    "sales": ("business",),
    "security": ("ops",),
    "spatial-computing": ("coding", "creative"),
    "specialized": ("business", "ops"),
    "support": ("ops", "business"),
    "testing": ("coding",),
    # Estas dos colecciones son artefactos de proceso (playbooks, escenarios,
    # plantillas) que sirven a cualquier tarea: universales por diseño.
    "strategy": ALL_DOMAINS,
    "examples": ALL_DOMAINS,
}

#: Áreas que se cargan SIEMPRE, sea cual sea el dominio de la tarea.
UNIVERSAL_CATEGORIES: Tuple[str, ...] = ("strategy", "examples")

#: Agentes (por slug de archivo) que se cargan SIEMPRE. Son el pegamento del
#: pipeline: orquestadores, generalistas y revisores que cualquier tarea puede
#: necesitar. Verificado por test contra el catálogo real para que un typo no
#: convierta esto en una promesa vacía.
UNIVERSAL_AGENTS: FrozenSet[str] = frozenset({
    "codigo",
    "agents-orchestrator",
    "automation-governance-architect",
    "business-strategist",
    "change-management-consultant",
    "engineering-code-reviewer",
    "engineering-minimal-change-engineer",
    "engineering-rapid-prototyper",
    "engineering-senior-developer",
    "engineering-software-architect",
    "engineering-technical-writer",
    "project-manager-senior",
    "project-management-project-shepherd",
    "support-executive-summary-generator",
    "testing-evidence-collector",
    "testing-reality-checker",
    "testing-workflow-optimizer",
})

#: Refinamiento por PALABRAS del propio agente. Suma dominios al previo del
#: área; nunca quita. Se aplica sobre el slug + `name` + `vibe` (los tres campos
#: curados) y NO sobre `description`, que es una plantilla
#: (`ES: Agente local de <Área> para <Nombre>. EN: …`) más un párrafo largo.
#:
#: Semántica de cada marcador:
#:
#: * `"token"`  → coincide con esa palabra EXACTA (comparación por tokens: el
#:   texto se normaliza a palabras, así que `unity` ya no coincide dentro de
#:   `community` ni `api` dentro de `capital`).
#: * `"token*"` → coincide con cualquier palabra que EMPIECE por `token`
#:   (`payment*` → payments, `finance*` → financial). El comodín es explícito a
#:   propósito: `product` debe ser exacta porque `production` no es producto.
#: * `"dos palabras"` → frase, se busca sobre el texto normalizado
#:   (`site reliability`, `smart contract`).
KEYWORD_DOMAINS: Dict[str, Tuple[str, ...]] = {
    "coding": (
        "code", "codebase", "coder", "developer",
        "backend", "frontend", "full stack", "api", "sdk", "cli", "sql",
        "database", "docker", "kubernetes", "script", "scripter", "compiler",
        "refactor", "tooling", "repository", "commit", "branch", "git",
        "mcp", "lsp", "wasm", "webassembly", "embedded", "firmware",
        "mobile", "ios", "android", "unity", "unreal", "godot", "roblox",
        "blender", "shader", "blockchain", "solidity", "smart contract",
        "data pipeline", "debug", "bug", "qa", "tester", "webgpu", "metal",
    ),
    "ops": (
        "devops", "sre", "secops", "infra*", "cloud*", "kubernetes", "k8s",
        "docker", "container*", "network*", "incident*", "responder",
        "reliability", "observability", "monitor*", "uptime", "deploy*",
        "release", "maintenance", "maintainer", "firewall", "backup*",
        "secret*", "credential*", "threat", "detection", "penetration",
        "compliance", "finops", "fleet", "iot", "systemd", "nginx",
        "server*", "daemon", "security", "seguridad", "site reliability",
        "on call", "migrat*",
    ),
    "research": (
        "research*", "analyst", "analysis", "analista", "historian",
        "anthropolog*", "geograph*", "psycholog*", "statistic*", "scientist",
        "evidence", "intelligence", "clinical",
        "narratolog*", "geopolit*", "academic", "investiga*", "bibliograph*",
        "survey", "geospatial",
    ),
    "creative": (
        "design", "designer", "artist", "audio", "sound",
        "music", "brand", "visual*", "ui", "ux", "storytell*", "whimsy",
        "level", "avatar*", "cartograph*", "illustrat*", "animat*", "video",
        "carousel", "creative", "image", "prompt", "persona", "map", "game",
        "3d", "motion", "graphics", "logo", "poster", "theme",
    ),
    "business": (
        "marketing", "sales", "salesforce", "ventas", "revenue", "finance*",
        "account*", "payment*", "billing", "pricing", "strateg*", "business",
        "customer", "client*", "product", "manager*", "hr", "recruit*",
        "grant*", "legal", "tax", "payable", "bookkeep*",
        "fpa", "invest*", "ecommerce", "commerce", "publisher*", "growth",
        "hospitality", "guest", "cfo", "controller", "esg",
        "sustainability", "consultant", "presales", "proposal", "stakeholder",
        "vendor", "supply", "procurement", "retail", "brand", "reporter",
        "tracker", "analytics", "kpi", "roi", "crm", "monetiz*", "budget*",
        "forecast*", "invoice*", "revenue ops", "engag*",
    ),
    "math": (
        "math*", "statistic*", "quant*", "actuar*", "numeric*", "algebra*",
        "econometric*", "arithmetic", "geometry", "probability",
    ),
}

#: Mínimo de candidatos que debe dejar el filtro. Por debajo, se devuelve el
#: catálogo completo: es preferible un shortlist con ruido a uno donde el
#: especialista correcto no está.
MIN_POOL = 15


# ---------------------------------------------------------------------------
# Mapa (tablas + override) 
# ---------------------------------------------------------------------------

class DomainMap:
    """Tablas efectivas de dominio: las de arriba con el override aplicado."""

    __slots__ = ("categories", "agents", "universal")

    def __init__(
        self,
        categories: Mapping[str, Sequence[str]],
        agents: Mapping[str, Sequence[str]],
        universal: Sequence[str],
    ) -> None:
        self.categories: Dict[str, Tuple[str, ...]] = {
            str(key): _clean_domains(value) for key, value in categories.items()
        }
        self.agents: Dict[str, Tuple[str, ...]] = {
            str(key): _clean_domains(value) for key, value in agents.items()
        }
        self.universal: FrozenSet[str] = frozenset(str(name) for name in universal)

    def domains_for(self, agent: Mapping[str, Any]) -> Tuple[str, ...]:
        """Dominios de un agente del catálogo, en el orden de `ALL_DOMAINS`."""
        stem = _stem(agent)
        explicit = self.agents.get(stem)
        if explicit is not None:
            return explicit
        category = self.category_of(agent)
        if category not in self.categories:
            # Área desconocida: agente universal. Nunca se esconde.
            return ALL_DOMAINS
        found = set(self.categories[category])
        tokens, text = agent_words(agent)
        for domain, markers in KEYWORD_DOMAINS.items():
            if _matches(tokens, text, markers):
                found.add(domain)
        return tuple(domain for domain in ALL_DOMAINS if domain in found)

    def is_universal(self, agent: Mapping[str, Any]) -> bool:
        """¿Se carga este agente sea cual sea el dominio pedido?"""
        if _stem(agent) in self.universal:
            return True
        category = self.category_of(agent)
        return category not in self.categories

    def category_of(self, agent: Mapping[str, Any]) -> str:
        """Área conocida más profunda de la ruta del agente.

        `game-development/unity/unity-architect.md` → `game-development` (el
        catálogo guarda `category="unity"`, que no es un área de la tabla).
        Devuelve el nombre de área desconocida si ninguna de la ruta está en la
        tabla, y `""` si el agente no trae ruta.
        """
        candidates = _category_candidates(agent)
        for candidate in candidates:
            if candidate in self.categories:
                return candidate
        return candidates[0] if candidates else ""


def _clean_domains(values: Sequence[str]) -> Tuple[str, ...]:
    """Normaliza una lista de dominios: válidos, sin duplicados, orden canónico."""
    wanted = {str(value).strip().casefold() for value in values or ()}
    return tuple(domain for domain in ALL_DOMAINS if domain in wanted)


def agent_words(agent: Mapping[str, Any]) -> Tuple[set, str]:
    """Palabras del agente (slug + `name` + `vibe`) y su texto normalizado.

    `description` se deja fuera a propósito: en esta colección es una plantilla
    más un párrafo largo, y su vocabulario ensucia el mapeo ("adapts software
    interactions" marcaría a un diseñador de producto como `coding`).

    Es la MISMA normalización que usan los marcadores de dominio y la asignación
    de fases de `pipeline_templates`: dos tokenizadores distintos volverían a
    crear dos verdades sobre el mismo agente.
    """
    stem = _stem(agent)
    raw = " ".join((
        stem.replace("-", " ").replace("_", " "),
        str(agent.get("name") or ""),
        str(agent.get("vibe") or ""),
    ))
    text = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
    return set(text.split()), text


def task_words(text: str) -> set:
    """Palabras de una petición, con la MISMA normalización que `agent_words`.

    Existe para que el shortlist léxico de la selección de agentes compare
    TOKENS y no substrings. La versión anterior hacía `if kw in haystack`, así
    que `app` coincidía dentro de `application` y `crea` dentro de cualquier
    cosa, y arrastraba agentes irrelevantes al shortlist (un ingeniero de
    add-ons de Blender colaba en una tarea de calculadora).
    """
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()
    return set(normalized.split())


def _matches(tokens: set, text: str, markers: Sequence[str]) -> bool:
    """¿Alguna palabra del agente coincide con algún marcador? (ver `KEYWORD_DOMAINS`)."""
    for marker in markers:
        if marker.endswith("*"):
            stem = marker[:-1]
            if any(token.startswith(stem) for token in tokens):
                return True
        elif " " in marker:
            if marker in text:
                return True
        elif marker in tokens:
            return True
    return False


def _stem(agent: Mapping[str, Any]) -> str:
    """Slug del agente: nombre del archivo sin `.md` (estable ante renombrados)."""
    path = str(agent.get("path") or "")
    if path:
        return Path(path).stem
    return str(agent.get("name") or "")


def _category_candidates(agent: Mapping[str, Any]) -> List[str]:
    """Áreas posibles del agente, de la más profunda a la más superficial."""
    parts = [part for part in str(agent.get("path") or "").split("/")[:-1] if part]
    ordered = list(reversed(parts))
    category = agent.get("category")
    if category and str(category) not in ordered:
        ordered.append(str(category))
    return ordered


# ---------------------------------------------------------------------------
# Override opcional (junto al catálogo cargado)
# ---------------------------------------------------------------------------

_OVERRIDE_FILE = "domains.json"
_override_cache: Dict[str, Any] = {"key": None, "data": {}}


def override_path(root: Optional[Path]) -> Optional[Path]:
    """Ruta del override (`<root>/domains.json`) o None si no hay raíz."""
    return Path(root) / _OVERRIDE_FILE if root else None


def _load_override(root: Optional[Path]) -> Dict[str, Any]:
    """Lee el override con caché por mtime. Nunca lanza; {} si no hay archivo."""
    path = override_path(root)
    if path is None:
        return {}
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    key = f"{path}|{stamp}"
    if _override_cache["key"] == key:
        return _override_cache["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        logger.warning("Override de dominios ilegible (%s): %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Override de dominios ignorado (no es un objeto JSON): %s", path)
        return {}
    _override_cache["key"] = key
    _override_cache["data"] = data
    return data


def reset_cache() -> None:
    """Descarta la caché del override (tests / recarga de configuración)."""
    _override_cache["key"] = None
    _override_cache["data"] = {}


def domain_map(root: Optional[Path] = None) -> DomainMap:
    """Tablas efectivas: las de este módulo con el override de `root` encima."""
    override = _load_override(root)
    categories: Dict[str, Sequence[str]] = dict(CATEGORY_DOMAINS)
    agents: Dict[str, Sequence[str]] = {}
    universal = set(UNIVERSAL_AGENTS)

    raw_categories = override.get("categories")
    if isinstance(raw_categories, dict):
        for key, value in raw_categories.items():
            if isinstance(value, (list, tuple)):
                categories[str(key)] = value
            else:
                logger.warning("Override de categoría ignorado (%s): no es lista", key)
    raw_agents = override.get("agents")
    if isinstance(raw_agents, dict):
        for key, value in raw_agents.items():
            if isinstance(value, (list, tuple)):
                agents[str(key)] = value
            else:
                logger.warning("Override de agente ignorado (%s): no es lista", key)
    raw_universal = override.get("universal")
    if isinstance(raw_universal, (list, tuple)):
        universal |= {str(name) for name in raw_universal}

    return DomainMap(categories, agents, universal)


# ---------------------------------------------------------------------------
# Filtrado
# ---------------------------------------------------------------------------

def select(
    agents: Sequence[Mapping[str, Any]],
    domains: Optional[Sequence[str]],
    root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Subconjunto de `agents` relevante para `domains` (+ universales).

    Sin dominios pedidos devuelve el catálogo tal cual: el filtro es una
    optimización del shortlist, nunca un requisito. Si el subconjunto queda por
    debajo de `MIN_POOL` también devuelve todo (ver el docstring del módulo).
    """
    listed = [dict(agent) for agent in agents]
    wanted = _clean_domains(list(domains or ()))
    if not wanted or not listed:
        return listed
    mapping = domain_map(root)
    keep = [
        agent for agent in listed
        if mapping.is_universal(agent)
        or any(domain in mapping.domains_for(agent) for domain in wanted)
    ]
    if len(keep) < MIN_POOL:
        logger.debug(
            "Filtro de dominios %s descartado: dejaba %d agentes (< %d)",
            list(wanted), len(keep), MIN_POOL,
        )
        return listed
    return keep


def summary(
    agents: Sequence[Mapping[str, Any]],
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Diagnóstico del mapeo: cuántos agentes quedan por dominio pedido.

    No se usa en producción; existe para poder mirar el mapa sin abrir 273
    archivos a mano (`python -m tools.agency_domains --report`).
    """
    mapping = domain_map(root)
    listed = list(agents)
    per_domain: Dict[str, int] = {}
    for domain in ALL_DOMAINS:
        per_domain[domain] = len(select(listed, [domain], root))
    unmapped = sorted({
        mapping.category_of(agent) for agent in listed
        if not mapping.is_universal(agent)
        and mapping.category_of(agent) not in mapping.categories
    })
    return {
        "total": len(listed),
        "universal": sum(1 for agent in listed if mapping.is_universal(agent)),
        "per_domain": per_domain,
        "unmapped_categories": unmapped,
    }


def _cli() -> int:  # pragma: no cover - utilidad de línea de comandos
    import argparse

    from .agency_catalog import create_default_catalog

    parser = argparse.ArgumentParser(description="Informe del mapa área→dominio")
    parser.add_argument("--report", action="store_true", help="imprime el informe")
    parser.add_argument("--agent", help="dominios derivados de un agente")
    args = parser.parse_args()

    catalog = create_default_catalog()
    if args.agent:
        agent = catalog.get(args.agent)
        if not agent:
            print(f"Agente no encontrado: {args.agent}")
            return 1
        mapping = domain_map(catalog.root)
        print(f"{agent['name']}: area={mapping.category_of(agent)!r} "
              f"dominios={list(mapping.domains_for(agent))} "
              f"universal={mapping.is_universal(agent)}")
        return 0

    info = summary(catalog.load(), catalog.root)
    print(f"Agentes en el catálogo: {info['total']} (universales: {info['universal']})")
    for domain, count in info["per_domain"].items():
        print(f"  {domain:9} -> {count}")
    if info["unmapped_categories"]:
        print("Áreas sin mapear (universales):", ", ".join(info["unmapped_categories"]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
