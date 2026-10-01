"""Guardia de bucles y de progreso del runtime. Determinista, sin LLM.

Por qué existe
--------------
Antes había SEIS guardias repartidos, cada uno con sus propios contadores
locales dentro de un método enorme:

* en `Agent.run_stream`: firma exacta `tool+args` repetida, `list_directory`
  consecutiva, misma herramienta N veces seguidas, racha de acciones de solo
  lectura sin producir, y reintentos con los mismos argumentos;
* en `Orchestrator._run_agency_mode`: una firma ya usada por un paso ANTERIOR.

Todos eran correctos por separado y frágiles en conjunto: umbrales literales
dentro del bucle, imposibles de leer, de probar o de ajustar por fase. Aquí
queda UNA clase con umbrales declarados, y el `Agent` solo pregunta.

La lección de fondo del brief: al modelo no se le pide que "no repita
herramientas". Se le impide. Este módulo es esa barrera, y vive fuera del LLM.

Contrato actual
---------------
Revisar un archivo varias veces, volver a listar después de escribir o repetir
una búsqueda con otro argumento son acciones normales. Solo se interviene tras
una racha larga de la misma herramienta con los mismos argumentos, y primero se
añade una instrucción correctiva al contexto del mismo agente.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

try:
    from .tool_taxonomy import PRODUCTIVE_TOOLS, READONLY_TOOLS
except ImportError:  # ejecución suelta, sin paquete
    from tool_taxonomy import PRODUCTIVE_TOOLS, READONLY_TOOLS  # type: ignore

logger = logging.getLogger("tool_guard")


# ---------------------------------------------------------------------------
# Resultado de una comprobación
# ---------------------------------------------------------------------------

@dataclass
class GuardDecision:
    """Veredicto del guardia.

    `allow=False` significa que la llamada NO se ejecuta. `kind` distingue por
    qué se cortó (el orquestador y la UI reaccionan distinto a cada caso):

    * `repeat_corrective` — la misma llamada exacta se repitió consecutivamente;
                            el agente debe usar el resultado o cambiar de acción.
    * `no_progress` — el agente mantuvo el loop tras las correcciones.
    """

    allow: bool
    kind: str = ""
    reason: str = ""

    def __bool__(self) -> bool:  # permite `if guard.check(...):`
        return self.allow

    @property
    def blocked(self) -> bool:
        return not self.allow


ALLOW = GuardDecision(True)


def result_failed(result: Any) -> bool:
    """¿El resultado de una herramienta es un fallo?

    Única definición de "esto no salió bien" del sistema. Estaba inline en
    `run_stream` y duplicada en otros sitios; si cambia aquí, cambia en todos.
    """
    text = str(result)
    lower = text.lower()
    return (
        lower.startswith("error")
        or "❌" in text
        or "no encontrada" in lower
        or "denegad" in lower
        or "denied" in lower
        or "disabled" in lower
        or "no registrada" in lower
    )


def signature(name: str, args: Any) -> str:
    """Firma estable de una llamada: misma herramienta + mismos argumentos."""
    try:
        rendered = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(args)
    return f"{name}:{rendered}"


def normalize_dir_args(args: Any) -> Dict[str, Any]:
    """Normaliza los args de `list_directory`.

    `list_directory('.'), list_directory('') y list_directory()` son la MISMA
    llamada efectiva: sin normalizar, el contador se reseteaba solo con variar
    el argumento por defecto.
    """
    data = args if isinstance(args, dict) else {}
    return {
        "path": data.get("path", ".") or ".",
        "depth": data.get("depth", 0),
    }


# ---------------------------------------------------------------------------
# Umbrales
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhaseLimits:
    """Compatibilidad de límites; solo las llamadas idénticas activan corrección."""

    #: Umbral heredado, informativo. No bloquea listados.
    dir_list: int = 2
    #: Umbral heredado, informativo. No bloquea argumentos distintos.
    same_tool: int = 4
    #: Umbral heredado, informativo. Las tareas de solo lectura son válidas.
    readonly_streak: int = 6
    #: llamadas idénticas consecutivas antes de pedir una corrección.
    identical_retries: int = 5
    #: avisos correctivos tolerados antes de cerrar un loop exacto.
    repeat_correctives: int = 2


#: Las mismas reglas aplican a coding, investigación y análisis: solo una
#: secuencia larga de la misma llamada exacta requiere una corrección.
PHASE_LIMITS: Dict[str, PhaseLimits] = {
    phase: PhaseLimits(dir_list=999, same_tool=999, readonly_streak=999)
    for phase in ("construction", "synthesis", "research", "default")
}

#: Fases que no ejecutan herramientas: el guardia no puede cortar nada.
TOOLLESS_PHASES = ("chat", "synthesis")

def limits_for(phase: Optional[str]) -> PhaseLimits:
    """Límites de la fase, con `default` como respaldo."""
    return PHASE_LIMITS.get((phase or "default").strip().lower(), PHASE_LIMITS["default"])


# ---------------------------------------------------------------------------
# El guardia
# ---------------------------------------------------------------------------

class ToolCallGuard:
    """Contadores de progreso de UN agente, con límites por fase.

    Uso:

        guard = ToolCallGuard(phase=step_mode)   # productivas/readonly: taxonomía
        decision = guard.check(tool_name, args)
        if decision.blocked:
            ...  # no se ejecuta; `decision.kind` dice por qué
        result = execute_tool(tool_name, args)
        decision = guard.record(tool_name, args, result)
        if decision.blocked:
            ...  # se corta el paso

    `check` es pre-ejecución (bucles de repetición) y `record` post-ejecución
    (progreso y reintentos). Están separadas porque solo la primera puede
    impedir un efecto secundario.

    `productive`/`readonly` por defecto salen de `tool_taxonomy` (la copia
    única). Se pueden pasar explícitamente cuando el llamante quiere su propia
    clasificación (los tests lo hacen).
    """

    def __init__(
        self,
        phase: Optional[str] = None,
        productive: Optional[Iterable[str]] = None,
        readonly: Optional[Iterable[str]] = None,
        limits: Optional[PhaseLimits] = None,
        cross_step: Optional[Dict[str, str]] = None,
    ):
        self.phase = (phase or "default").strip().lower()
        self.limits = limits or limits_for(self.phase)
        self._productive = frozenset(PRODUCTIVE_TOOLS if productive is None else productive)
        self._readonly = frozenset(READONLY_TOOLS if readonly is None else readonly)
        #: firma -> resultado de ESTE agente (paridad con el antiguo
        #: `executed_tools`, que también se usaba para listar lo ya hecho).
        self.results: Dict[str, str] = {}
        #: firma -> id del paso que la ejecutó. Compartido entre los pasos de un
        #: mismo plan para detectar trabajo duplicado ENTRE pasos.
        self.cross_step: Dict[str, str] = {} if cross_step is None else cross_step
        self._last_dir_args: Optional[Dict[str, Any]] = None
        self._dir_count = 0
        self._last_tool: Optional[str] = None
        self._same_tool_count = 0
        self._readonly_streak = 0
        self._last_signature: Optional[str] = None
        self._consecutive_identical = 0
        self._repeat_correctives = 0
        self.tool_history: List[str] = []

    # -- estado útil para el llamador -------------------------------------

    @property
    def signatures(self) -> List[str]:
        """Firmas ya ejecutadas (para el resumen final del prompt)."""
        return list(self.results.keys())

    @property
    def readonly_streak(self) -> int:
        return self._readonly_streak

    # -- pre-ejecución -----------------------------------------------------

    def check(self, name: str, args: Any = None) -> GuardDecision:
        """Corrige una secuencia extrema de la misma llamada exacta."""
        normalized_args = normalize_dir_args(args) if name == "list_directory" else args
        sig = signature(name, normalized_args)
        if sig == self._last_signature:
            self._consecutive_identical += 1
        else:
            self._last_signature = sig
            self._consecutive_identical = 1
            self._repeat_correctives = 0

        if self._consecutive_identical < self.limits.identical_retries:
            return ALLOW

        self._repeat_correctives += 1
        if self._repeat_correctives > self.limits.repeat_correctives:
            return GuardDecision(
                False,
                "no_progress",
                f"La llamada idéntica a '{name}' continuó después de varias "
                "correcciones consecutivas.",
            )
        return GuardDecision(
            False,
            "repeat_corrective",
            f"La llamada '{name}' con esos mismos argumentos ya se repitió "
            "consecutivamente. Usa el resultado previo o cambia de acción.",
        )

    # -- post-ejecución ----------------------------------------------------

    def record(self, name: str, args: Any, result: Any, ok: Optional[bool] = None) -> GuardDecision:
        """Añade el resultado al contexto local sin juzgar el tipo de tarea."""
        normalized_args = normalize_dir_args(args) if name == "list_directory" else args
        sig = signature(name, normalized_args)
        if name in self._productive and (ok is None or ok):
            self._readonly_streak = 0
        else:
            self._readonly_streak += 1
        self.results[sig] = str(result)
        self.tool_history.append(name)
        return ALLOW

    # -- trabajo duplicado entre pasos del mismo plan ----------------------

    def claim(self, name: str, args: Any, step_id: str) -> Optional[str]:
        """Compatibilidad: registra una llamada productiva en el plan.

        Devuelve None la primera vez (y registra el paso actual); si otro paso
        del mismo plan ya ejecutó exactamente esta llamada, devuelve su id. Es la
        guarda cross-step del modo AGENCY, ahora en el mismo sitio que el resto.

        Solo el trabajo PRODUCTIVO se deduplica: volver a mirar el workspace
        (`list_directory`, `read_file`…) no es trabajo duplicado, y marcarlo
        como tal convertía la exploración normal de cada paso en un fallo
        garantizado (el paso 2 moría por repetir el `list_directory` del paso 1
        antes de haber intentado nada).
        """
        if name not in self._productive:
            return None
        sig = signature(name, args)
        previous_step = self.cross_step.get(sig)
        if previous_step and previous_step != step_id:
            return previous_step
        self.cross_step[sig] = step_id
        return None
