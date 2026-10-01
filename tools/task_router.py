"""Decisión única sobre el tipo de trabajo. Determinista y sin LLM.

Antes convivían CUATRO clasificadores respondiendo la misma pregunta con
resultados distintos:

* `Orchestrator._is_conversational`   -> CHAT | trabajo (léxico).
* `ToolRouter.route`                  -> CHAT | AGENCY, pagando **una llamada
  LLM completa por tarea** con un prompt de ~40 líneas.
* `LayaONNXRouter.route`              -> CHAT | AGENCY + naturaleza (ONNX, ms).
* `Orchestrator._looks_multi_step`    -> simple | multi-paso (léxico).

...y `detect_task_nature` estaba triplicado. Además, `Orchestrator._route_task`
—el único punto que consultaba a Laya— **no se llamaba desde ningún sitio**, así
que el modelo ONNX se cargaba pero nunca clasificaba nada: el modo se decidía
solo con el `_is_conversational` léxico.

Aquí queda UN punto de entrada (`decide`) con un contrato estable, y las
heurísticas léxicas que lo respaldan. Nunca lanza, nunca llama a un LLM, y si
Laya no está disponible o no es concluyente cae en las heurísticas locales (que
no necesitan red ni modelo).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("task_router")

#: Modos de ejecución. Contrato público; espejo en `laya_onnx_router.MODES`
#: (hay un test que verifica que no divergen).
MODES: Sequence[str] = ("CHAT", "AGENCY")

#: Naturalezas del trabajo. Espejo en `laya_onnx_router.NATURES`.
NATURES: Sequence[str] = ("synthesis", "construction", "mixed")

#: Dominios de trabajo. Los resuelve, por este orden: la cabeza de dominios de
#: Laya si el modelo la declara, y si no la tabla léxica de este módulo. El
#: contrato (`domains` + `domains_source`) es el mismo en los dos casos, así que
#: `agency_domains` filtra el sub-catálogo igual sin saber quién decidió.
DOMAINS: Sequence[str] = ("coding", "research", "math", "creative", "business", "ops")


# ---------------------------------------------------------------------------
# Conversación vs trabajo
# ---------------------------------------------------------------------------

#: Saludos que, SOLOS, no son una tarea.
GREETINGS = (
    "hola", "buenos días", "buenas tardes", "buenas noches",
    "hey", "hi", "hello", "saludos", "qué tal", "que tal",
)

#: Verbos/expresiones que convierten el mensaje en una tarea real. Se comprueban
#: por substring sobre el texto en minúsculas.
ACTION_VERBS = (
    "listar", "crear", "borrar", "eliminar", "ejecutar", "buscar", "leer", "escribir",
    "modificar", "mover", "copiar", "comprimir", "descomprimir", "instalar", "configurar",
    "analizar", "generar", "crea", "borra", "lista", "muestra", "abre", "cierra",
    "create", "delete", "run", "execute", "search", "find", "read", "write", "edit",
    "copy", "install", "configure", "analyze", "generate", "show", "list", "open", "close",
    "haz", "hacer", "quiero", "necesito", "dame", "proporciona", "obtén", "descarga",
    # Conjugaciones en 3ª persona de presente.
    "escribe", "guarda", "calcula", "analiza", "genera", "modifica", "mueve", "copia",
    "comprime", "descomprime", "instala", "configura", "ejecuta", "busca", "lee",
    "elimina", "obtiene", "proporciona", "necesita", "quieres", "haces",
    # Referencias a contenido/archivos: tampoco son conversación casual.
    "contiene", "contenido", "dime", "explica", "describe", "resume", "resúmeme",
    "replanificación", "re-planificación", "replanificacion", "re-planificacion",
)

#: Verbos de TRABAJO: aunque no sean un comando, implican una tarea real de
#: ingeniería o producción de contenido. Es la lista que faltaba: sin ella
#: "Refactoriza este módulo Python" no contenía ningún verbo conocido y se
#: respondía como charla en vez de activar el pipeline de código.
WORK_VERBS = (
    # Ingeniería
    "refactoriz", "refactor", "implementa", "implementar", "implement",
    "programa", "programar", "codifica", "codificar", "depura", "depurar",
    "debug", "arregla", "arreglar", "corrige", "corregir", "fix", "soluciona",
    "optimiza", "optimizar", "migra", "migrar", "porta", "portar",
    "despliega", "desplegar", "deploy", "integra", "integrar", "compila",
    "compilar", "instancia", "renderiza", "parsea", "valida", "versiona",
    "documenta", "documentar", "revisa", "revisar", "audita", "auditar",
    "testea", "testear", "prueba", "medir", "mide", "benchmark",
    # Producción / análisis de contenido
    "investiga", "investigar", "estudia", "estudiar", "analiza", "analizar",
    "sintetiza", "sintetizar", "redacta", "redactar", "traduce", "traducir",
    "resume", "resumir", "explica", "explicar", "describe", "describir",
    "compara", "comparar", "evalúa", "evaluar", "critica", "recomienda",
    "diseña", "disena", "diseñar", "modela", "modelar", "planifica", "planificar",
    "organiza", "organizar", "esquematiza", "estructura", "estructurar",
    "probable", "busca", "encontrar", "obtén", "saca", "extrae", "extraer",
    # Señales de artefacto/defecto (una tarea técnica sin verbo en imperativo)
    "bug", "traceback", "excepción", "excepcion", "stack trace", "no funciona",
    "falla", "error de", "deadline", "requisitos", "criterios de aceptación",
)

#: A partir de esta longitud de mensaje se asume tarea (un párrafo largo no es
#: un saludo).
CONVERSATIONAL_MAX_CHARS = 150


def is_conversational(task: str) -> bool:
    """¿Es un mensaje conversacional sin trabajo real detrás?

    Un saludo inicial NO convierte en conversacional un mensaje que además pide
    una tarea real ("hola lista el directorio" es una tarea): primero se quita
    el saludo y solo se devuelve True si no queda ninguna instrucción.

    Conservador a propósito —ante la duda, trabajo—: las señales objetivas
    (`agency_signals`) y los verbos de trabajo tienen prioridad absoluta sobre
    la lista corta de verbos de acción.
    """
    text = (task or "").lower().strip()
    remaining = text
    for greeting in sorted(GREETINGS, key=len, reverse=True):
        if remaining.startswith(greeting):
            remaining = remaining[len(greeting):].strip(" .,;:!?¡¿()")
            break
    if not remaining:
        return True  # solo un saludo / cortesía

    # Las señales objetivas mandan: 3+ verbos, verificación, re-planificación…
    if agency_signals(remaining)["force_agency"]:
        return False
    if any(verb in remaining for verb in WORK_VERBS):
        return False
    if any(verb in remaining for verb in ACTION_VERBS):
        return False
    return len(remaining) < CONVERSATIONAL_MAX_CHARS


# ---------------------------------------------------------------------------
# Naturaleza del trabajo (síntesis vs construcción)
# ---------------------------------------------------------------------------
#
# No es lo mismo un informe (síntesis: el valor está en el texto final y
# fragmentarlo en meta-pasos no aporta nada) que una app (construcción: cada paso
# produce un artefacto persistente y verificable que puede fallar por separado).
#
# NOTA HISTÓRICA: en `agent_core` había DOS tuplas llamadas `_SYNTHESIS_MARKERS`
# (una para la naturaleza y otra para clasificar subtareas). Python se queda con
# la última definición, así que la de la naturaleza **nunca se usó**: el
# shadowing la descartaba en silencio. Aquí la de la naturaleza vive con su
# nombre propio y ya no colisiona.

#: Señales léxicas de construcción: artefactos que persisten y evolucionan.
NATURE_CONSTRUCTION_MARKERS = (
    "app", "aplicación", "aplicacion", "sistema", "plataforma",
    "api", "endpoint", "backend", "frontend", "interfaz",
    "base de datos", "esquema", "schema", "migración", "migracion",
    "script", "programa", "herramienta", "cli", "bot",
    "docker", "kubernetes", "deploy", "despliegue",
    "instalar", "configurar", "montar", "construir",
    "refactoriz", "refactor", "migrar", "portar", "implementa", "codifica",
    "código", "codigo", "función", "funcion", "clase", "módulo", "modulo",
    "tests", "pytest", "sql", "repositorio", "commit",
    "durante días", "durante dias", "multi-sesión", "multi-sesion",
    # Artefactos de contenido que persisten y evolucionan (web, documentos).
    "sitio web", "página web", "pagina web", "html", "css", "interactivo",
)

#: Señales léxicas de síntesis: el output es un texto único.
NATURE_SYNTHESIS_MARKERS = (
    "resumen", "resume", "resúmeme", "resumeme", "informe", "reporte",
    "analiza", "análisis", "analisis", "explica", "describe",
    "compara", "evalúa", "evalua", "crítica", "critica",
)


# ---------------------------------------------------------------------------
# Dominios de trabajo
# ---------------------------------------------------------------------------
#
# La cabeza de dominio de Laya es el objetivo; mientras el modelo ONNX no esté
# reentrenado, esta tabla es el respaldo determinista y produce EL MISMO
# contrato (`domains`), así que el sub-catálogo puede empezar a filtrar ya y el
# día que Laya dé dominios solo cambia el origen, no el consumidor.

DOMAIN_MARKERS = {
    "coding": (
        "refactoriz", "refactor", "código", "codigo", "python", "javascript",
        "typescript", "función", "funcion", "clase", "api", "endpoint", "bug",
        "compil", "script", "módulo", "modulo", "sql", "docker", "tests",
        "pytest", "traceback", "sintaxis", "variable", "repositorio", "commit",
        "backend", "frontend", "framework", "librería", "libreria",
    ),
    "research": (
        "investiga", "historia", "histórico", "historico", "imperio", "roma",
        "roma", "siglo", "antigua", "guerra", "caída", "caida", "civiliza",
        "análisis", "analisis", "analiza", "fuentes", "citas", "citación",
        "bibliografía", "bibliografia", "estudio", "ensayo", "documenta",
        "evidencia", "contexto histórico", "tesis",
    ),
    "math": (
        "calcula", "cálculo", "calculo", "ecuación", "ecuacion", "derivada",
        "integral", "estadística", "estadistica", "probabilidad", "matriz",
        "álgebra", "algebra", "aritmética", "aritmetica", "vector", "teorema",
    ),
    "creative": (
        "sitio web", "página web", "pagina web", "html", "css", "web",
        "diseño", "diseno", "logo", "cuento", "poema", "guion", "guión",
        "narrativa", "estilo", "branding", "interfaz", "interactivo",
        "ilustración", "ilustracion", "animación", "animacion",
    ),
    "business": (
        "negocio", "mercado", "marketing", "ventas", "cliente", "precio",
        "presupuesto", "estrategia", "roi", "ingresos", "campaña", "campana",
        "producto", "inversión", "inversion", "facturación", "facturacion",
        "competencia", "proveedor",
    ),
    "ops": (
        "despliega", "desplegar", "despliegue", "deploy", "servidor",
        "infraestructura", "kubernetes", "monitor", "backup", "respaldo",
        "seguridad", "permisos", "logs", "systemd", "puerto", "nginx",
        "certificado", "firewall", "cron", "daemon", "vigilancia", "uptime",
    ),
}

#: Capas o entregables distintos dentro de UNA misma construcción. Dos ya
#: justifican un pipeline por fases: "una app con base de datos, docker y tests"
#: no es un script de un archivo, aunque el léxico no vea una cadena de verbos.
CONSTRUCTION_SCOPE_MARKERS = (
    "api", "endpoint", "frontend", "backend", "base de datos", "database",
    "docker", "kubernetes", "despliegue", "deploy", "autentic", "test",
    "migración", "migracion", "interfaz", "panel", "dashboard", "crud",
    "modelo de datos", "esquema", "schema", "cola", "cache", "usuarios",
    "login", "webhook", "cli", "html", "css", "sitio web", "página web",
    "pagina web", "landing", "logo", "marca", "persistencia",
)

#: Mínimo de capas para considerar que la construcción necesita descomposición.
CONSTRUCTION_SCOPE_MIN = 2

#: Máximo de dominios devueltos. Más de tres implica pipelines que cargan medio
#: catálogo y vuelven a la sobrecarga que este diseño quiere evitar.
MAX_DOMAINS = 3


def construction_scope_hits(task: str) -> int:
    """Cuántas capas/entregables distintos menciona la tarea (sin LLM)."""
    text = (task or "").casefold()
    return sum(1 for marker in CONSTRUCTION_SCOPE_MARKERS if marker in text)


def requires_decomposition(
    task: str,
    nature: Optional[str] = None,
    domains: Optional[Sequence[str]] = None,
) -> bool:
    """¿Hay que descomponer la tarea en fases?

    Único punto de decisión (lo consumen `decide` y `pipeline_templates`, para
    que no existan dos criterios distintos de "plan completo o compacto"):

    * varios dominios → hay que coordinar especialidades distintas;
    * una tarea encadenada (`looks_multi_step`) → varias acciones;
    * una CONSTRUCCIÓN con varias capas mencionadas → varias fases aunque el
      léxico no vea una cadena de verbos ("una app con base de datos, docker y
      tests" no es un script de un archivo).
    """
    if nature is None:
        nature = detect_nature(task)
    if domains is None:
        domains = detect_domains(task)
    return (
        len(list(domains)) > 1
        or looks_multi_step(task)
        or (
            nature == "construction"
            and construction_scope_hits(task) >= CONSTRUCTION_SCOPE_MIN
        )
    )


def detect_domains(task: str) -> List[str]:
    """Dominios probables de la tarea, sin LLM (respaldo determinista de Laya).

    Devuelve como máximo `MAX_DOMAINS`, ordenados por número de coincidencias y,
    a igualdad, según el orden declarado en `DOMAINS` para que el resultado sea
    estable entre ejecuciones.
    """
    text = (task or "").casefold()
    hits = {
        domain: sum(1 for marker in DOMAIN_MARKERS.get(domain, ()) if marker in text)
        for domain in DOMAINS
    }
    detected = [d for d in DOMAINS if hits[d] > 0]
    if len(detected) <= MAX_DOMAINS:
        return detected
    ranked = sorted(detected, key=lambda d: (-hits[d], DOMAINS.index(d)))
    keep = set(ranked[:MAX_DOMAINS])
    return [d for d in DOMAINS if d in keep]


def detect_nature(task: str) -> str:
    """Clasifica la tarea en 'synthesis' | 'construction' | 'mixed' sin LLM.

    El default es 'synthesis' porque las reglas de síntesis —plan compacto y
    coalescencia— son las conservadoras: un plan de 3 pasos siempre se puede
    ejecutar, fragmentar de más sí rompe el presupuesto de contexto.
    """
    text = (task or "").casefold()
    construction_hits = sum(1 for marker in NATURE_CONSTRUCTION_MARKERS if marker in text)
    synthesis_hits = sum(1 for marker in NATURE_SYNTHESIS_MARKERS if marker in text)

    if construction_hits >= 2 and synthesis_hits == 0:
        return "construction"
    if synthesis_hits >= 2 and construction_hits == 0:
        return "synthesis"
    if construction_hits > 0 and synthesis_hits > 0:
        return "mixed"
    return "synthesis"


# ---------------------------------------------------------------------------
# Señales de tarea multi-paso
# ---------------------------------------------------------------------------
#
# Se usan para decidir si una tarea autónoma (scheduler/daemon) merece AGENCY o
# basta una respuesta directa. Son heurísticas locales: no gastan una llamada al
# modelo.

#: Señales FUERTES: por sí solas implican trabajo de varios pasos (un informe
#: hay que extraerlo, analizarlo y redactarlo).
MULTI_STEP_MARKERS = (
    "informe", "reporte", "roadmap", "pipeline", "migrar", "migración",
    "migracion", "automatiza", "paso a paso", "por pasos", "resumen ejecutivo",
)

#: Señales de SECUENCIA: por sí solas no bastan ("primero" puede ser retórico),
#: pero dos ya indican una cadena de subtareas.
MULTI_STEP_CHAIN_MARKERS = (
    " luego", " despues", " después", " primero", " segundo", " tercero",
    " finalmente", " cada dia", " cada día", " y luego", " y despues",
    " y después", " investiga y", " busca y", " analiza y", " compara",
)


def looks_multi_step(task: str) -> bool:
    """¿La tarea pide varias acciones encadenadas?

    Deliberadamente conservador: ante la duda, respuesta directa. Una tarea
    programada simple ("suma 2+2") no debe pagar el coste de un plan AGENCY.
    """
    import re as _re

    text = (task or "").casefold()
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > 400:
        return True
    if any(marker in stripped for marker in MULTI_STEP_MARKERS):
        return True
    chain_hits = sum(1 for marker in MULTI_STEP_CHAIN_MARKERS if marker in stripped)
    if chain_hits >= 2:
        return True
    # Listas numeradas o con viñetas: varias subtareas explícitas.
    if _re.search(r"(?:^|\n)\s*(?:\d+[).:]|[-*•])\s+\S", stripped):
        return True
    # "haz A y B" — conjunción con un texto ya largo.
    return " y " in stripped and len(stripped) > 90


# ---------------------------------------------------------------------------
# Señales objetivas que fuerzan AGENCY
# ---------------------------------------------------------------------------

VERIFICATION_MARKERS = ("verifica", "comprueba", "confirma", "valida", "asegura")
SEQUENCE_MARKERS = (" luego", " después", " despues", " primero", " segundo", " finalmente")
DELIVERABLE_MARKERS = ("informe", "reporte", "roadmap", "resumen ejecutivo")
REPLAN_MARKERS = (
    "replanificación", "re-planificación", "replanificacion", "re-planificacion",
)

#: Regla dura: con 3+ verbos de acción distintos una tarea es AGENCY aunque el
#: clasificador diga CHAT.
FORCE_AGENCY_MIN_VERBS = 3


def agency_signals(task: str) -> Dict[str, Any]:
    """Señales deterministas de que la tarea es trabajo real (no conversación)."""
    text = (task or "").lower()
    verb_count = sum(1 for verb in ACTION_VERBS if verb in text)
    verification = any(marker in text for marker in VERIFICATION_MARKERS)
    sequence = any(marker in text for marker in SEQUENCE_MARKERS)
    deliverable = any(marker in text for marker in DELIVERABLE_MARKERS)
    replanning = any(marker in text for marker in REPLAN_MARKERS)
    force_agency = (
        verb_count >= FORCE_AGENCY_MIN_VERBS
        or verification
        or replanning
        or (sequence and verb_count >= 2)
        or (deliverable and verb_count >= 2)
    )
    return {
        "verb_count": verb_count,
        "verification": verification,
        "sequence": sequence,
        "deliverable": deliverable,
        "replanning": replanning,
        "force_agency": force_agency,
    }


# ---------------------------------------------------------------------------
# Decisión única
# ---------------------------------------------------------------------------

def _laya_router():
    """Router Laya si está instalado. Devuelve None si no se puede importar."""
    try:
        from .laya_onnx_router import get_laya_router

        return get_laya_router(lazy=True)
    except Exception as exc:  # pragma: no cover - depende del entorno
        logger.debug("Laya no disponible: %s", exc)
        return None


def decide(
    task: str,
    reasoning: bool = True,
    conversational: Optional[bool] = None,
    laya: Any = None,
) -> Dict[str, Any]:
    """Decide modo, naturaleza y dominios de una tarea.

    Orden de decisión:

    1. Si el mensaje es conversacional (léxico), es CHAT y no se paga el coste
       de cargar/consultar Laya.
    2. Si Laya está disponible y concluyente, manda su clasificación (incluidos
       los dominios, cuando el modelo declara esa cabeza).
    3. Si no, modo AGENCY con la naturaleza y los dominios léxicos.
    4. Las señales objetivas (3+ verbos, verificación, secuencia + verbos)
       fuerzan AGENCY aunque el paso 1 o 2 dijeran CHAT.

    Los dominios son la UNIÓN de los que dé Laya y los léxicos: un dominio de
    más carga más agentes, y equivocarse cargando de más es mucho más barato que
    esconder al especialista que hacía falta.

    El diccionario devuelto es el contrato que consume el orquestador y que
    viaja al evento `router`:

        mode, nature, domains, domains_source, requires_decomposition,
        confidence, source, reasoning, signals
    """
    text = task or ""
    # Las señales se calculan ANTES del atajo conversacional: si hay señales
    # objetivas de trabajo, el atajo no puede ganar. Antes se calculaban después
    # y el `force_agency` documentado en el paso 4 nunca llegaba a aplicarse.
    signals = agency_signals(text)

    if conversational is None:
        conversational = is_conversational(text)

    if conversational and not signals["force_agency"]:
        return {
            "mode": "CHAT",
            "nature": "synthesis",
            "domains": [],
            "domains_source": "lexical",
            "requires_decomposition": False,
            "confidence": 0.95,
            "source": "lexical",
            "reasoning": "Mensaje conversacional detectado por heuristica",
            "signals": signals,
        }

    mode: Optional[str] = None
    nature: Optional[str] = None
    laya_domains: List[str] = []
    confidence = 0.0
    source = "lexical"

    if laya is None:
        laya = _laya_router()
    if laya is not None:
        try:
            if laya.available():
                decision = laya.route(text, reasoning=reasoning, bus=None)
                if decision.get("router") == "laya":
                    mode = str(decision.get("mode") or "").upper() or None
                    nature = str(decision.get("nature") or "").lower() or None
                    confidence = float(decision.get("confidence") or 0.0)
                    source = "laya"
                    laya_domains = [
                        str(domain).strip().casefold()
                        for domain in (decision.get("domains") or [])
                        if str(domain).strip().casefold() in DOMAINS
                    ]
        except Exception as exc:
            logger.debug("Laya no concluyente (%s); se usan las heuristicas", exc)

    if mode not in MODES:
        # Llegar hasta aquí ya significa que el léxico no lo consideró CHAT.
        mode = "AGENCY"
    if signals["force_agency"]:
        mode = "AGENCY"
        confidence = max(confidence, 0.6)
    if nature not in NATURES:
        nature = detect_nature(text)

    lexical_domains = detect_domains(text)
    domains = list(dict.fromkeys(laya_domains + lexical_domains))[:MAX_DOMAINS]
    if not laya_domains:
        domains_source = "lexical"
    elif lexical_domains:
        domains_source = "laya+lexical"
    else:
        domains_source = "laya"
    decompose = requires_decomposition(text, nature=nature, domains=domains)

    return {
        "mode": mode,
        "nature": nature,
        "domains": domains,
        "domains_source": domains_source,
        "requires_decomposition": decompose,
        "confidence": max(0.0, min(1.0, confidence)),
        "source": source,
        "reasoning": f"clasificado por {source}",
        "signals": signals,
    }
