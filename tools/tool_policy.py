"""Whitelist de herramientas por agente. Determinista, sin LLM.

Por qué existe
--------------
Hasta ahora cualquier agente podía ejecutar CUALQUIER herramienta interna: un
redactor de marketing tenía a su disposición `kill_process`, `screenshot`,
`start_daemon_tool` o `setup_telegram`. La única barrera era el prompt ("no
hagas cosas raras"), y las barreras de prompt no son barreras.

Aquí está la barrera real: una lista de herramientas permitidas que se calcula
ANTES de ejecutar nada, a partir de dos cosas que ya existen y ya son
deterministas:

* los **dominios** del agente (`agency_domains` / `task_router.DOMAINS`:
  research, coding, creative, business, math, ops);
* la **fase** del paso (`task_router`/`agent_core.classify_subtask_mode`:
  construction, synthesis, default...).

Cómo se lee
-----------
Los perfiles PARTEN la superficie de herramientas — cada herramienta del
registro pertenece al menos a uno — y el permiso de un agente es la UNIÓN de:
`core` + los perfiles de sus dominios (+ `research` si el dominio lo pide). Los
perfiles `system` y `state` (procesos, GUI, Telegram, daemon, checkpoints,
skills) NO se conceden nunca por dominio: solo el agente general, en modo chat,
recibe `ALL` (el usuario está pidiendo "hazme esto", no una tarea de agencia).

Consecuencias deliberadas:

* Un agente de agencia no puede matar procesos, hacer capturas, imprimir,
  mandar Telegram ni arrancar el daemon.
* Un paso de `synthesis` (redactar) pierde las herramientas de ejecución
  (`exec_shell_command`, `run_command_async`, `run_python_code`, jobs): un paso
  que "sintetiza" y a la vez arranca procesos es el clásico paso que divaga.
* Sin dominios (o en modo `chat`) la policy es `ALL`: no se rompe el uso de un
  solo agente, que es donde el usuario espera manos libres.

La comprobación la hace el cliente de herramientas (`mcp_client.execute_tool`)
y el descubrimiento (`query_tools` no propone lo que no se puede usar). Aquí
solo se decide, sin efectos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

try:
    from .tool_taxonomy import BASE_TOOLS, KNOWN_TOOLS
except ImportError:  # ejecución suelta, sin paquete
    from tool_taxonomy import BASE_TOOLS, KNOWN_TOOLS  # type: ignore


# =============================================================================
# 1. Perfiles: la superficie de herramientas, partida por tipo de trabajo
# =============================================================================

#: Descubrimiento: SIN estas dos el agente no puede ni saber qué existe. Van en
#: `core` para que ningún agente de agencia se quede sin el mecanismo de
#: descubrimiento (y para que `query_tools` siga apareciendo en su resultado).
#: `view`, `search`, `tree`, `edit` y `bash` son los atajos de `TOOL_IMPL`.
META_TOOLS: FrozenSet[str] = frozenset({
    "query_tools",
    "execute_tool",
    "view",
    "search",
    "tree",
    "edit",
    "bash",
})

#: Trabajo real: leer, escribir, explorar, ejecutar, gestionar jobs.
CORE_TOOLS: FrozenSet[str] = frozenset({
    "read_file",
    "write_file",
    "edit_file",
    "apply_diff",
    "file_glob_search",
    "grep_search",
    "list_directory",
    "create_directory",
    "delete_file",
    "delete_directory",
    "move_file",
    "compress_files",
    "extract_archive",
    "exec_shell_command",
    "get_datetime",
    "run_python_code",
    "create_venv",
    "run_command_async",
    "job_status",
    "job_output",
    "job_kill",
    "job_list",
    "workspace_download_link",
}) | META_TOOLS

#: Herramientas de ejecución: las que un paso de síntesis no necesita.
EXECUTION_TOOLS: FrozenSet[str] = frozenset({
    "exec_shell_command",
    "run_command_async",
    "run_python_code",
    "create_venv",
    "job_status",
    "job_output",
    "job_kill",
    "job_list",
})

#: Buscar y guardar información: RAG, memoria, descargas, documentos.
RESEARCH_TOOLS: FrozenSet[str] = frozenset({
    "rag_search",
    "list_libraries",
    "set_active_library",
    "retrieve_memories",
    "store_memory",
    "download_file",
    "convert_document",
    "classify_text",
    "add_task",
    "list_tasks",
    "complete_task",
})

#: Calidad, pruebas, documentación, git y SQL.
CODING_TOOLS: FrozenSet[str] = frozenset({
    "sqlite_query",
    "sqlite_execute",
    "get_db_schema",
    "git_status",
    "git_diff",
    "git_log",
    "git_commit",
    "lint_code",
    "check_security",
    "type_check",
    "measure_complexity",
    "run_tests",
    "generate_unit_tests",
    "generate_docstrings",
    "generate_readme",
    "generate_api_docs",
    "format_code",
    "sort_imports",
    "remove_unused_imports",
    "clean_code",
    "add_license",
    "check_dependencies",
    "review_code",
    "summarize_changes",
    "chat_completion",
})

#: Entregables y comunicación interna del trabajo: informes, apertura de URLs.
BUSINESS_TOOLS: FrozenSet[str] = frozenset({
    "open_url",
    "daily_report",
})

#: Introspección y control del propio agente (panel de la UI). Privilegiadas:
#: un agente de agencia no se pone en pausa ni se cambia el objetivo solo.
AGENT_CONTROL_TOOLS: FrozenSet[str] = frozenset({
    "agent_status",
    "agent_memory",
    "agent_skills",
    "agent_plan",
    "agent_goal",
    "agent_pause",
    "agent_resume",
})

#: Cálculo y datos.
MATH_TOOLS: FrozenSet[str] = frozenset({
    "sqlite_query",
    "sqlite_execute",
    "get_db_schema",
    "convert_document",
    "classify_text",
})

#: Diseño y contenido: documentación, conversión, publicación.
CREATIVE_TOOLS: FrozenSet[str] = frozenset({
    "generate_readme",
    "generate_api_docs",
    "generate_docstrings",
    "convert_document",
    "open_url",
    "daily_report",
})

#: Sistemas: procesos, unidades, vigilancia de cambios, avisos.
OPS_TOOLS: FrozenSet[str] = frozenset({
    "list_processes",
    "list_drives",
    "check_system_tool",
    "install_system_tool",
    "start_watchdog",
    "stop_watchdog",
    "get_watchdog_events",
    "notify",
})

#: Peligroso o personal: NUNCA por dominio. Solo el agente general en chat.
SYSTEM_TOOLS: FrozenSet[str] = frozenset({
    "kill_process",
    "screenshot",
    "take_photo",
    "send_telegram",
    "setup_telegram",
    "set_reminder",
    "print_file",
    "open_file_with_default_app",
    "start_daemon_tool",
    "stop_daemon_tool",
    "daemon_status_tool",
}) | AGENT_CONTROL_TOOLS

#: Estado persistente: checkpoints y skills.
STATE_TOOLS: FrozenSet[str] = frozenset({
    "create_checkpoint",
    "list_checkpoints",
    "rollback_checkpoint",
    "delete_checkpoint",
    "get_checkpoint_info",
    "list_skills",
    "get_skill",
    "create_skill",
    "promote_skill",
    "delete_skill",
})

#: Perfil -> herramientas. Un nombre puede estar en varios (la unión manda).
PROFILES: Dict[str, FrozenSet[str]] = {
    "core": CORE_TOOLS,
    "research": RESEARCH_TOOLS,
    "coding": CODING_TOOLS,
    "creative": CREATIVE_TOOLS,
    "business": BUSINESS_TOOLS,
    "math": MATH_TOOLS,
    "ops": OPS_TOOLS,
    "system": SYSTEM_TOOLS,
    "state": STATE_TOOLS,
}

#: Perfiles que un agente de agencia NO recibe nunca por dominio.
PRIVILEGED_PROFILES: Tuple[str, ...] = ("system", "state")

#: Todos los perfiles: la policy permisiva del agente general / modo chat.
ALL_PROFILES: Tuple[str, ...] = tuple(PROFILES)

#: Dominio (vocabulario de `task_router.DOMAINS`) -> perfiles que concede.
DOMAIN_PROFILES: Dict[str, Tuple[str, ...]] = {
    "research": ("core", "research"),
    "coding": ("core", "coding"),
    "creative": ("core", "creative", "research"),
    "business": ("core", "business", "research"),
    "math": ("core", "math", "coding"),
    "ops": ("core", "ops", "coding"),
}

#: Dominio desconocido (o agente sin dominio resuelto): lo mínimo útil.
DEFAULT_PROFILES: Tuple[str, ...] = ("core", "research")

#: Fases que no ejecutan trabajo (`tool_guard.TOOLLESS_PHASES`): sin shell.
NO_EXECUTION_PHASES: Tuple[str, ...] = ("synthesis",)

#: Modo del agente que recibe permisos totales (no hay tarea de agencia).
OPEN_MODE = "chat"

#: Marca del mensaje de denegación. Quien necesite saber si un resultado de
#: `execute_tool` fue una denegación (la UI, el orquestador) usa `is_denial`
#: en vez de buscar substrings por su cuenta.
DENIAL_PREFIX = "[HERRAMIENTA NO PERMITIDA]"


def is_denial(result: object) -> bool:
    """True si el texto devuelto por el cliente es una denegación de la policy."""
    return isinstance(result, str) and result.startswith(DENIAL_PREFIX)


# =============================================================================
# 2. La policy
# =============================================================================

@dataclass(frozen=True)
class ToolPolicy:
    """Herramientas permitidas para UN agente en UN paso.

    `allowed` es la unión de los perfiles. `open_policy` marca la policy
    permisiva (agente general / chat), donde no tiene sentido denegar nada y la
    UI no debería pintar avisos.
    """

    agent: str = "agent"
    profiles: Tuple[str, ...] = field(default_factory=lambda: DEFAULT_PROFILES)
    allowed: FrozenSet[str] = frozenset()
    mode: Optional[str] = None
    open_policy: bool = False

    # -- consultas ---------------------------------------------------------

    def allows(self, tool_name: Optional[str]) -> bool:
        """¿Puede este agente ejecutar esa herramienta?"""
        if not tool_name:
            return False
        if self.open_policy:
            return True
        return tool_name in self.allowed

    def denial(self, tool_name: str) -> str:
        """Mensaje determinista que se le devuelve al modelo (y se emite).

        Corto a propósito: el modelo lo lee en cada denegación y no debe gastar
        contexto; la lista completa se descubre con `query_tools`.
        """
        perfiles = ", ".join(self.profiles) or "sin perfil"
        base = ", ".join(BASE_TOOLS[:4])
        return (
            f"{DENIAL_PREFIX} '{tool_name}' no está habilitada para "
            f"el agente '{self.agent}' (perfil: {perfiles}). No la vuelvas a "
            f"intentar: usa otra herramienta ({base}, ...) o resuelve la tarea "
            f"con lo que ya tienes."
        )

    def filter(self, names: Iterable[str]) -> List[str]:
        """Subconjunto permitido de una lista de nombres (orden estable)."""
        if self.open_policy:
            return list(names)
        return [n for n in names if n in self.allowed]

    def to_dict(self) -> Dict[str, object]:
        return {
            "agent": self.agent,
            "profiles": list(self.profiles),
            "mode": self.mode,
            "open": self.open_policy,
            "tools": len(self.allowed),
        }


def profiles_tools(profiles: Iterable[str]) -> FrozenSet[str]:
    """Unión de las herramientas de varios perfiles (ignora los desconocidos)."""
    tools: set = set()
    for name in profiles:
        tools.update(PROFILES.get(name, frozenset()))
    return frozenset(tools)


#: Policy permisiva compartida (para el agente general y el modo chat).
OPEN_POLICY = ToolPolicy(
    agent="general",
    profiles=ALL_PROFILES,
    allowed=frozenset(),  # `open_policy` manda; el conjunto no se consulta
    open_policy=True,
)


def policy_for(
    agent: str = "agent",
    domains: Optional[Sequence[str]] = None,
    mode: Optional[str] = None,
) -> ToolPolicy:
    """Policy determinista de un agente.

    Reglas, en orden:

    1. Modo `chat` (o sin dominios) → `ALL`: un solo agente conversando no es
       una tarea de agencia y no se le recorta nada.
    2. Con dominios → `core` + los perfiles de cada dominio; los perfiles
       privilegiados (`system`, `state`) no entran nunca, ni aunque un dominio
       futuro los declarara.
    3. Fase de redacción (`synthesis`) → fuera las herramientas de ejecución:
       redactar no es arrancar procesos.
    """
    clean_mode = (mode or "").strip().lower() or None
    clean_domains = tuple(d for d in (domains or ()) if d)

    if not clean_domains or clean_mode == OPEN_MODE:
        # Sin tarea de agencia (un solo agente conversando) no hay nada que
        # acotar: la whitelist es la superficie entera.
        return ToolPolicy(
            agent=agent,
            profiles=ALL_PROFILES,
            allowed=frozenset(),
            mode=clean_mode,
            open_policy=True,
        )

    profiles: Tuple[str, ...] = DEFAULT_PROFILES
    for domain in clean_domains:
        extra = DOMAIN_PROFILES.get(domain)
        if extra:
            profiles = tuple(dict.fromkeys(profiles + extra))
    profiles = tuple(p for p in profiles if p not in PRIVILEGED_PROFILES)

    allowed = profiles_tools(profiles)
    if clean_mode in NO_EXECUTION_PHASES:
        allowed = allowed - EXECUTION_TOOLS

    return ToolPolicy(
        agent=agent,
        profiles=profiles,
        allowed=allowed,
        mode=clean_mode,
    )


# =============================================================================
# 3. Auditoría
# =============================================================================

def unassigned(known: Optional[Iterable[str]] = None) -> List[str]:
    """Herramientas de una superficie que no caen en ningún perfil.

    Con `known` (normalmente el registro real de `dev_agent`) se comprueba que
    la partición cubre TODA la superficie ejecutable: una herramienta nueva sin
    perfil sería invisible para todos los agentes de agencia, que es un fallo
    silencioso y difícil de diagnosticar.
    """
    superficie = frozenset(known) if known is not None else KNOWN_TOOLS
    return sorted(superficie - profiles_tools(ALL_PROFILES))


def summary() -> Dict[str, object]:
    """Vista compacta para el CLI y para depurar permisos."""
    return {
        "profiles": {name: len(tools) for name, tools in PROFILES.items()},
        "privileged": list(PRIVILEGED_PROFILES),
        "domains": {d: list(p) for d, p in DOMAIN_PROFILES.items()},
        "no_execution_phases": list(NO_EXECUTION_PHASES),
        "unassigned_by_taxonomy": unassigned(),
    }


def _cli(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover
    """`python -m tools.tool_policy [--agent A --domains coding,ops --mode chat]`."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="tool_policy", description="Whitelist de herramientas por agente."
    )
    parser.add_argument("--report", action="store_true", help="Perfiles y cobertura.")
    parser.add_argument("--agent", default="agent", help="Nombre del agente.")
    parser.add_argument("--domains", default="", help="Dominios separados por comas.")
    parser.add_argument("--mode", default=None, help="Fase del paso (chat, synthesis...).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.report:
        print(json.dumps(summary(), ensure_ascii=False, indent=2))
        return 0

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    policy = policy_for(agent=args.agent, domains=domains, mode=args.mode)
    allowed = sorted(policy.allowed) if not policy.open_policy else sorted(KNOWN_TOOLS)
    print(json.dumps({**policy.to_dict(), "allowed": allowed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
