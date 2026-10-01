"""Plantillas de pipeline: el PLAN lo escribe el código, no el LLM.

Antes, cada tarea AGENCY pagaba una llamada LLM completa —con un prompt de ~60
líneas— para que un "supervisor" inventara un plan JSON que después había que
reparar a mano (coalescer meta-pasos, recortar al tope, validar agentes y
renumerar dependencias). Y aun así el plan salía inflado o con pasos que no
producían nada.

Planificar es una decisión de PROCESO, y el proceso no depende del contenido de
la tarea: una app se construye en capas, un informe se reparte en facetas, un
despliegue se cambia y se verifica. Eso es una plantilla, no una generación. Lo
único que el LLM decide aquí es el CONTENIDO de cada paso, que es justo lo que
sí sabe hacer.

Contrato (`build_plan`):

    {"steps": [{"id", "agent", "subtask", "depends_on", "artifact", "phase"}],
     "supervisor": "Agency Pipeline", "task": task,
     "nature": nature, "pipeline": "<nombre>", "source": "template"}

Reglas de diseño:

* **Determinista**: mismo (tarea, agentes, naturaleza, dominios) → mismo plan.
  Ni red, ni modelo, ni azar. Se puede testear y depurar.
* **Compacto**: en síntesis, un paso por agente (el supervisor final integra los
  informes, así que un paso de "integración" sería trabajo duplicado). En
  construcción, las fases por capa; la variante completa solo cuando la tarea
  pide descomposición.
* **Sin meta-pasos**: no hay pasos de "revisión final" ni de "compilación del
  informe"; eso era lo que el post-procesado tenía que deshacer.
* **Agentes asignados por evidencia**: palabras de la fase contra slug/nombre/
  `vibe` del agente (mismo normalizador que `agency_domains`) más su área, con
  desempate por orden de entrada. El especialista de pruebas hace los tests, no
  el historiador.
* **`artifact` vacío a propósito**: una plantilla no puede saber si el proyecto
  usará `src/app.py` o `app/main.py`, y un artefacto inventado convierte un paso
  correcto en un fallo. La verificación de construcción se apoya en que el paso
  ejecute una herramienta productiva (ver `tool_guard`/`_run_agency_mode`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import agency_domains

logger = logging.getLogger("pipeline_templates")

#: Naturalezas válidas. Espejo de `task_router.NATURES`.
NATURES: Sequence[str] = ("synthesis", "construction", "mixed")

#: Tope duro de pasos de cualquier plantilla. Si un pipeline crece por encima,
#: el plan deja de ser legible (y de caber en el contexto de los agentes).
MAX_STEPS = 12

#: Tope de facetas (pasos paralelos, uno por agente). Cada faceta es una llamada
#: al modelo: un texto único no gana nada repartiéndose entre cinco agentes, se
#: diluye. En tareas sin descomposición el tope baja a `MAX_FACETS_COMPACT`.
MAX_FACETS = 3
MAX_FACETS_COMPACT = 2


@dataclass(frozen=True)
class Phase:
    """Una fase del pipeline: qué se hace y a quién se le pide."""

    key: str
    instruction: str
    #: Palabras que identifican al agente adecuado (prefijo sobre los tokens).
    keywords: Tuple[str, ...] = ()
    #: Áreas preferidas (ver `agency_domains.CATEGORY_DOMAINS`).
    areas: Tuple[str, ...] = ()
    #: Claves de fases anteriores que deben terminar antes.
    depends: Tuple[str, ...] = ()
    #: Una fase por agente seleccionado (facetas en paralelo).
    per_agent: bool = False
    title: str = ""

    def with_depends(self, depends: Tuple[str, ...]) -> "Phase":
        """Copia de la fase con otras dependencias (variantes compactas)."""
        return Phase(
            key=self.key,
            instruction=self.instruction,
            keywords=self.keywords,
            areas=self.areas,
            depends=depends,
            per_agent=self.per_agent,
            title=self.title,
        )


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------
#
# Los textos son instrucciones de TRABAJO (qué entregar), no de proceso. Todo lo
# que el agente necesita saber de su papel va en "Tu enfoque:", que se rellena
# con su propia `vibe` del catálogo: el ángulo especializado sin gastar una
# llamada al modelo en que un supervisor lo explique.

_FACET = Phase(
    key="facet",
    per_agent=True,
    title="Faceta",
    instruction=(
        "Cubre desde tu especialidad la parte que te corresponde y devuelve el "
        "resultado final de esa faceta, no un plan ni un esquema.\n\n"
        "Tu enfoque: {focus}"
    ),
)

_SYNTHESIS_PIPELINE: Tuple[Phase, ...] = (_FACET,)

_MIXED_PIPELINE: Tuple[Phase, ...] = (
    Phase(
        key="research",
        per_agent=True,
        title="Investigación",
        instruction=(
            "Reúne el material verificable que la tarea necesita (datos, fuentes, "
            "requisitos y referencias) y déjalo listo para usarlo.\n\n"
            "Tu enfoque: {focus}"
        ),
    ),
    Phase(
        key="build",
        title="Construcción",
        depends=("research",),
        keywords=("build", "creat", "implement", "develop", "design", "engineer", "frontend", "generat"),
        areas=("engineering", "design", "game-development", "spatial-computing"),
        instruction=(
            "Con el material anterior, construye el entregable completo y deja "
            "el resultado usable."
        ),
    ),
    Phase(
        key="verify",
        title="Verificación",
        depends=("build",),
        keywords=("test", "verif", "qa", "check", "reality", "audit", "review", "inspect"),
        areas=("testing",),
        instruction=(
            "Comprueba el entregable contra el objetivo, corrige lo que falle y "
            "deja constancia de qué verificaste."
        ),
    ),
)

#: Construcción de software: capas, cada una con su punto de fallo.
_SETUP = Phase(
    key="setup",
    title="Estructura",
    keywords=("setup", "scaffold", "structur", "tooling", "architect", "senior", "rapid", "devops", "initialize"),
    areas=("engineering",),
    instruction=(
        "Deja lista la base de trabajo. Revisa la estructura existente y ajusta "
        "lo necesario, o crea directorios, archivos base y dependencias si hace falta."
    ),
)

_DATA = Phase(
    key="data",
    title="Datos",
    depends=("setup",),
    keywords=("data", "database", "schema", "model", "migrat", "storage", "persist"),
    areas=("engineering",),
    instruction=(
        "Define el modelo de datos y su esquema, migración o estado persistente."
    ),
)

_CORE = Phase(
    key="core",
    title="Lógica",
    depends=("data",),
    keywords=("backend", "core", "develop", "senior", "systems", "python", "solidity", "optimiz"),
    areas=("engineering",),
    instruction=(
        "Implementa la lógica principal, sus módulos, funciones y reglas en el workspace."
    ),
)

_INTERFACE = Phase(
    key="interface",
    title="Interfaz",
    depends=("core",),
    keywords=("frontend", "api", "ui", "ux", "designer", "interfac", "mobile", "uswds", "cli"),
    areas=("engineering", "design"),
    instruction=(
        "Construye la interfaz que expone la funcionalidad (API, CLI o UI) y "
        "conéctala con la lógica."
    ),
)

_TESTS = Phase(
    key="tests",
    title="Pruebas",
    depends=("interface",),
    keywords=("test", "qa", "bench", "accessib", "verif"),
    areas=("testing",),
    instruction=(
        "Escribe y ejecuta pruebas que demuestren que el resultado funciona, y "
        "corrige lo que falle."
    ),
)

_INTEGRATION = Phase(
    key="integration",
    title="Integración",
    depends=("tests",),
    keywords=("integration", "reality", "architect", "review", "senior", "evidence"),
    areas=("testing",),
    instruction=(
        "Comprueba el flujo de extremo a extremo y corrige los fallos de integración."
    ),
)

_DELIVERY = Phase(
    key="delivery",
    title="Entrega",
    depends=("integration",),
    keywords=("documentation", "technical writer", "devops", "deploy", "release", "security", "perform"),
    areas=("engineering",),
    instruction=(
        "Deja el entregable listo para usarse: cómo se ejecuta o despliega, "
        "configuración necesaria y documentación mínima."
    ),
)

_CONSTRUCTION_CODING_FULL: Tuple[Phase, ...] = (
    _SETUP, _DATA, _CORE, _INTERFACE, _TESTS, _INTEGRATION, _DELIVERY,
)

#: Construcción pequeña: estructura, lógica y pruebas. Las dependencias se
#: recalculan sobre las fases que sobreviven (sin esto, "tests" esperaría a una
#: fase de interfaz que ya no existe y perdería el resultado que debe verificar).
_CONSTRUCTION_CODING_COMPACT: Tuple[Phase, ...] = (
    _SETUP,
    _CORE.with_depends(("setup",)),
    _TESTS.with_depends(("core",)),
)

_STRUCTURE = Phase(
    key="structure",
    title="Estructura",
    keywords=("ux", "architect", "content", "writer", "curator", "strateg"),
    areas=("design",),
    instruction=(
        "Define la estructura y el contenido del entregable antes de producirlo."
    ),
)

_PRODUCE = Phase(
    key="build",
    title="Producción",
    depends=("structure",),
    keywords=("designer", "frontend", "develop", "visual", "creative", "writer", "curator", "storytell"),
    areas=("design", "engineering"),
    instruction=(
        "Produce el entregable completo y déjalo listo para usarse."
    ),
)

_REVIEW = Phase(
    key="review",
    title="Revisión",
    depends=("build",),
    keywords=("review", "audit", "accessib", "guardian", "inclusive", "finish", "verif"),
    areas=("design", "testing"),
    instruction=(
        "Revisa el entregable por accesibilidad, coherencia y cumplimiento del "
        "objetivo; corrige lo que falle."
    ),
)

#: Entregable visual (web, piezas gráficas, documentos largos).
_CREATIVE_FULL: Tuple[Phase, ...] = (_STRUCTURE, _PRODUCE, _REVIEW)
_CREATIVE_COMPACT: Tuple[Phase, ...] = (_STRUCTURE, _PRODUCE.with_depends(()))

_INSPECT = Phase(
    key="inspect",
    title="Inspección",
    keywords=("audit", "security", "reliab", "monitor", "architect", "responder", "compliance"),
    areas=("security", "support"),
    instruction=(
        "Revisa la configuración, los servicios y los riesgos; resume los cambios necesarios."
    ),
)

_CHANGE = Phase(
    key="change",
    title="Cambio",
    depends=("inspect",),
    keywords=("devops", "sre", "engineer", "automat", "infra", "cloud", "deploy", "network"),
    areas=("engineering",),
    instruction=(
        "Aplica los cambios necesarios en configuración, scripts o servicios."
    ),
)

_VERIFY_OPS = Phase(
    key="verify",
    title="Verificación",
    depends=("change",),
    keywords=("test", "sre", "reliab", "audit", "reality", "responder", "evidence"),
    areas=("testing", "security"),
    instruction=(
        "Verifica que el cambio funciona y no ha causado regresiones; deja "
        "evidencia comprobable."
    ),
)

_DOCUMENT_OPS = Phase(
    key="document",
    title="Documentación",
    depends=("verify",),
    keywords=("documentation", "writer", "compliance", "legal", "strateg"),
    instruction=(
        "Documenta qué cambió, cómo revertirlo y qué permisos o credenciales "
        "se vieron afectados."
    ),
)

#: Operación (configuración, infraestructura, scripts de sistema).
_OPS_FULL: Tuple[Phase, ...] = (_INSPECT, _CHANGE, _VERIFY_OPS, _DOCUMENT_OPS)
_OPS_COMPACT: Tuple[Phase, ...] = (
    _INSPECT,
    _CHANGE.with_depends(()),
    _VERIFY_OPS.with_depends(("change",)),
)


#: Marcadores de persistencia/estado: la fase de datos solo aplica si la tarea
#: guarda algo. Una calculadora de un archivo no tiene "modelo de datos y su
#: migración", y pedírselo a un agente especialista es la clase de subtarea
#: absurda que hace que el paso divague y muera sin producir nada.
DATA_MARKERS = (
    "base de datos", "database", "sql", "sqlite", "schema", "migra",
    "persiste", "persistencia", "modelo de datos", "almacen", "storage", "tabla",
)

#: Marcadores de entrega/despliegue: la fase de entrega solo aplica si hay algo
#: que desplegar, empaquetar o documentar.
DELIVERY_MARKERS = (
    "deploy", "desplieg", "despliega", "docker", "contenedor", "documenta",
    "readme", "release", "publica", "instala", "produccion", "producción",
)


def _has_marker(text: str, markers: Sequence[str]) -> bool:
    """¿Aparece alguno de los marcadores en la tarea? (case-insensitive)."""
    low = str(text or "").casefold()
    return any(marker in low for marker in markers)


def _linear(phases: Sequence[Phase]) -> Tuple[Phase, ...]:
    """Encadena las fases: cada una depende de la anterior que sobrevive.

    Necesario al recortar fases: si se cae `data`, `core` no puede seguir
    dependiendo de una fase que ya no existe (perdería el material anterior).
    """
    out: List[Phase] = []
    for index, phase in enumerate(phases):
        depends = () if index == 0 else (phases[index - 1].key,)
        out.append(phase.with_depends(depends))
    return tuple(out)


def _coding_pipeline(task: str, decompose: bool) -> Tuple[Phase, ...]:
    """Pipeline de código dimensionado a lo que la tarea pide de verdad.

    La plantilla completa (setup → data → core → interface → tests →
    integration → delivery) solo tiene sentido cuando la tarea tiene estado y
    entrega. Sin marcadores se cae `data` y/o `delivery`, y las fases que
    quedan se reencadenan.
    """
    if not decompose:
        return _CONSTRUCTION_CODING_COMPACT
    text = str(task or "").strip()
    if not text:
        # Llamada sin contexto (tests, CLI): se mantiene la plantilla completa.
        return _CONSTRUCTION_CODING_FULL
    phases: List[Phase] = [_SETUP]
    if _has_marker(text, DATA_MARKERS):
        phases.append(_DATA)
    phases.extend((_CORE, _INTERFACE, _TESTS, _INTEGRATION))
    if _has_marker(text, DELIVERY_MARKERS):
        phases.append(_DELIVERY)
    return _linear(phases)


def pipeline_for(
    nature: str,
    domains: Optional[Sequence[str]] = None,
    decompose: bool = True,
    task: str = "",
) -> Tuple[Phase, ...]:
    """Pipeline para esa naturaleza (y variante de dominio).

    Orden de decisión:

    1. **Software primero**: si el dominio incluye `coding`, el pipeline es de
       capas aunque también haya dominio de despliegue.
    2. **Dominio sobre léxico**: una tarea sólo visual, sólo de operaciones o
       visual-con-material usa su pipeline aunque la naturaleza léxica diga
       "síntesis". "Un paso por agente" repartiría un cambio de configuración
       entre varios especialistas en vez de inspeccionar, cambiar y verificar.
    3. **Naturaleza**: construcción genérica por capas, mixta (reúne y construye)
       o síntesis (un paso por agente).

    Una naturaleza desconocida cae en síntesis: es el pipeline más barato y el
    que menos puede romper (sin dependencias entre pasos).
    """
    domain_set = {str(domain).casefold() for domain in (domains or ())}
    material = domain_set & {"research", "business", "math"}
    if "coding" in domain_set:
        # Software manda sobre el resto: un proyecto con despliegue sigue siendo
        # un proyecto de software.
        pipeline = _coding_pipeline(task, decompose)
    elif "creative" in domain_set and material:
        # Construir algo con material de otro dominio: primero se reúne, luego
        # se produce (una web con investigación detrás, un informe ilustrado).
        pipeline = _MIXED_PIPELINE
    elif "creative" in domain_set:
        pipeline = _CREATIVE_FULL if decompose else _CREATIVE_COMPACT
    elif "ops" in domain_set and not material:
        # Cambiar configuración o infraestructura: inspeccionar, cambiar y
        # verificar. Vale aunque el léxico diga "síntesis".
        pipeline = _OPS_FULL if decompose else _OPS_COMPACT
    elif nature == "construction":
        pipeline = _coding_pipeline(task, decompose)
    elif nature == "mixed":
        pipeline = _MIXED_PIPELINE
    else:
        pipeline = _SYNTHESIS_PIPELINE
    return pipeline


def pipeline_name(
    nature: str,
    domains: Optional[Sequence[str]] = None,
    decompose: bool = True,
    task: str = "",
) -> str:
    """Nombre estable del pipeline elegido (viaja en el plan y en los logs)."""
    pipeline = pipeline_for(nature, domains, decompose, task)
    return f"{nature}:{pipeline[0].key}:{'full' if decompose else 'compact'}"


# ---------------------------------------------------------------------------
# Asignación determinista de agentes
# ---------------------------------------------------------------------------

def _matches(tokens: set, text: str, keywords: Sequence[str]) -> int:
    """Cuántas palabras de la fase aparecen en el agente (prefijo sobre tokens)."""
    hits = 0
    for keyword in keywords:
        if " " in keyword:
            if keyword in text:
                hits += 1
        elif any(token.startswith(keyword) for token in tokens):
            hits += 1
    return hits


def score_agent(agent: Mapping[str, Any], phase: Phase, area: str = "") -> int:
    """Idoneidad determinista del agente para la fase (0 = sin evidencia)."""
    tokens, text = agency_domains.agent_words(agent)
    score = _matches(tokens, text, phase.keywords)
    if area and area in phase.areas:
        score += 2
    return score


def _focus_of(agent: Mapping[str, Any]) -> str:
    """Ángulo especializado del agente, sacado de su propio catálogo."""
    vibe = str(agent.get("vibe") or "").strip()
    if vibe:
        return vibe[:200]
    description = str(agent.get("description") or "").strip()
    return description[:200] or "tu especialidad"


def _pick_agent(
    phase: Phase,
    agents: Sequence[Mapping[str, Any]],
    areas: Sequence[str],
    fallback_index: int,
) -> int:
    """Índice del agente más adecuado para la fase (desempate: orden de entrada).

    Precedencia:

    1. Un agente del ÁREA de la fase (ya lleva +2 en `score_agent`).
    2. La mejor coincidencia de palabras clave.
    3. Un generalista universal (solo si no hay evidencia de especialidad).
    4. Reparto round-robin.

    El área y la especialidad tienen prioridad sobre lo universal para no
    concentrar un plan completo en el primer agente generalista.
    """
    best_area_index: Optional[int] = None
    best_area_score = 0
    best_index: Optional[int] = None
    best_score = 0
    universal_index: Optional[int] = None
    for index, agent in enumerate(agents):
        area = areas[index] if index < len(areas) else ""
        score = score_agent(agent, phase, area)
        if area and area in phase.areas and score > best_area_score:
            best_area_score = score
            best_area_index = index
        if score > best_score:
            best_score = score
            best_index = index
        if universal_index is None and _is_universal(agent):
            universal_index = index
    if best_area_index is not None:
        return best_area_index
    if best_index is not None:
        return best_index
    if universal_index is not None:
        return universal_index
    # Sin universales ni coincidencias: se reparte el trabajo entre los agentes
    # en vez de cargar todas las fases sobre el primero.
    return fallback_index % len(agents)


def _is_universal(agent: Mapping[str, Any]) -> bool:
    """¿Es un agente que cualquier tarea puede necesitar (`agency_domains`)?"""
    try:
        return agency_domains.domain_map().is_universal(agent)
    except Exception:  # pragma: no cover - el mapa no debería fallar aquí
        return False


def _area_of(agent: Mapping[str, Any]) -> str:
    """Área del catálogo del agente ("" si no se puede determinar)."""
    try:
        return agency_domains.domain_map().category_of(agent)
    except Exception:  # pragma: no cover - el mapa no debería fallar aquí
        return ""


# ---------------------------------------------------------------------------
# Construcción del plan
# ---------------------------------------------------------------------------

def build_plan(
    task: str,
    agents: Sequence[Mapping[str, Any]],
    nature: str = "synthesis",
    domains: Optional[Sequence[str]] = None,
    decompose: Optional[bool] = None,
) -> Dict[str, Any]:
    """Plan determinista por fases. Nunca lanza y nunca inventa agentes.

    `agents` puede ser la selección ya filtrada por dominio o el catálogo
    entero: la asignación por fase elige al más adecuado de la lista.
    """
    if nature not in NATURES:
        logger.warning("Naturaleza desconocida %r; se usa 'synthesis'", nature)
        nature = "synthesis"

    names = [
        str(agent.get("name") or f"Agente {index + 1}")
        for index, agent in enumerate(agents)
    ]
    if not names:
        # Sin agentes no hay plan posible: un plan vacío (y su error) es más
        # honesto que pasos con nombres inventados que luego no resuelven.
        logger.warning("build_plan sin agentes: no hay plan que construir")
        return {
            "steps": [],
            "supervisor": "Agency Pipeline",
            "task": task,
            "nature": nature,
            "pipeline": "none",
            "source": "template",
            "error": "no hay agentes seleccionados",
        }

    from . import task_router  # diferido: solo para el default de descomposición

    if decompose is None:
        # Mismo criterio que `task_router.decide` (una sola verdad sobre "plan
        # completo o compacto"). Si el llamador ya tiene la decisión, la pasa.
        decompose = task_router.requires_decomposition(task, nature=nature, domains=domains)

    text = str(task or "").strip()
    pipeline = pipeline_for(nature, domains, bool(decompose), text)
    areas = [_area_of(agent) for agent in agents]

    steps: List[Dict[str, Any]] = []
    ids_by_phase: Dict[str, List[str]] = {}
    next_id = 1
    fallback = 0
    facet_limit = MAX_FACETS if decompose else MAX_FACETS_COMPACT

    def _append(agent_index: int, phase: Phase) -> None:
        nonlocal next_id, fallback
        instruction = phase.instruction.format(focus=_focus_of(agents[agent_index]))
        dependencies = _deps(phase, ids_by_phase)
        agent_name = names[agent_index]
        previous = steps[-1] if steps else None
        external_dependencies = (
            set(dependencies) - {previous["id"]}
            if previous is not None
            else set(dependencies)
        )
        can_merge = (
            previous is not None
            and previous["agent"] == agent_name
            and external_dependencies <= set(previous["depends_on"])
        )

        if can_merge:
            step_id = previous["id"]
            previous["subtask"] = f'{previous["subtask"]}\n\n{instruction}'
            phases = previous.setdefault("phases", [previous["phase"]])
            phases.append(phase.key)
            previous["phase"] = "+".join(phases)
            previous["depends_on"] = list(dict.fromkeys(
                previous["depends_on"] + [dep for dep in dependencies if dep != step_id]
            ))
        else:
            step_id = str(next_id)
            steps.append({
                "id": step_id,
                "agent": agent_name,
                "subtask": instruction,
                "depends_on": dependencies,
                "artifact": None,
                "phase": phase.key,
            })
            next_id += 1

        ids_by_phase.setdefault(phase.key, []).append(step_id)
        if not phase.per_agent:
            fallback += 1

    for phase in pipeline:
        if phase.per_agent:
            for index in range(min(len(names), facet_limit)):
                _append(index, phase)
                if len(steps) >= MAX_STEPS:
                    break
        else:
            _append(_pick_agent(phase, agents, areas, fallback), phase)
        if len(steps) >= MAX_STEPS:
            logger.warning("Pipeline %s recortado al tope de %d pasos", phase.key, MAX_STEPS)
            break

    return {
        "steps": steps,
        "supervisor": "Agency Pipeline",
        "task": task,
        "nature": nature,
        "pipeline": pipeline_name(nature, domains, bool(decompose), text),
        "source": "template",
    }


def _deps(phase: Phase, ids_by_phase: Mapping[str, Sequence[str]]) -> List[str]:
    """IDs de los pasos de las fases de las que depende esta fase."""
    resolved: List[str] = []
    for key in phase.depends:
        for step_id in ids_by_phase.get(key, ()):
            if step_id not in resolved:
                resolved.append(step_id)
    return resolved


# ---------------------------------------------------------------------------
# Recuperación determinista de fallos
# ---------------------------------------------------------------------------

#: Intentos correctivos por paso (además del intento original).
MAX_RECOVERY_ATTEMPTS = 1

#: Marcador del paso correctivo: lo usa el bucle para no reintentar en cadena y
#: los tests para comprobar que el reintento se ejecutó de verdad.
RECOVERY_MARKER = "[CORRECCIÓN]"


def recovery_step(
    failed: Mapping[str, Any],
    error: str,
    attempt: int = 1,
) -> Dict[str, Any]:
    """Un único paso correctivo para un fallo, sin LLM.

    Antes esto era otra llamada al supervisor (que solía devolver 3-6 pasos y
    reescribir el proyecto entero). Aquí es una decisión de proceso: el mismo
    agente repite el mismo objetivo, con el error delante y con la instrucción
    de corregir la causa en vez de reintentar a ciegas.

    El paso correctivo NO depende del fallido (su resultado no existe: es lo que
    se quiere sustituir) y conserva el `artifact`, para que la verificación
    vuelva a exigirlo.
    """
    step_id = str(failed.get("id") or "?")
    subtask = str(failed.get("subtask") or "").strip()
    reason = " ".join(str(error or "").split())[:400] or "error desconocido"
    note = (
        f"{RECOVERY_MARKER} El intento anterior del paso {step_id} falló: {reason}\n"
        "Corrige la CAUSA del fallo (no repitas la misma llamada) y deja el paso "
        "completo y verificable antes de terminar."
    )
    return {
        "id": f"{step_id}R{attempt}",
        "subtask": f"{subtask}\n\n{note}" if subtask else note,
        "agent_name": failed.get("agent_name") or failed.get("agent") or "",
        "depends_on": [],
        "artifact": failed.get("artifact"),
        "phase": "recovery",
    }


def is_recovery(step: Mapping[str, Any]) -> bool:
    """¿Es un paso correctivo? (no se reintenta en cadena)."""
    return (
        str(step.get("phase") or "") == "recovery"
        or RECOVERY_MARKER in str(step.get("subtask") or "")
    )
