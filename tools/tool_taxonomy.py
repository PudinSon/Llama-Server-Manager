"""Taxonomía ÚNICA de las herramientas internas del agente.

Por qué existe
--------------
La misma taxonomía estaba escrita en DOS módulos y de tres formas distintas:

* `agent_core.PRODUCTIVE_TOOLS` / `agent_core.READONLY_TOOLS` — qué cuenta como
  trabajo (validación de pasos de "construction", rachas de solo lectura);
* `dev_agent.TOOL_DESCRIPTIONS` — el texto que se le enseña al modelo y con el
  que se rankean herramientas por embeddings;
* `dev_agent.TOOL_CATEGORIES` / `dev_agent.CATEGORY_KEYWORDS` — el refuerzo por
  intención que usa `get_relevant_tools`.

Nada garantizaba que dijeran lo mismo, y de hecho no lo decían: el diccionario
`TOOL_CATEGORIES` declaraba la categoría `system` DOS veces, así que la segunda
entrada (`check_system_tool`, `install_system_tool`) machacaba a la primera y
`exec_shell_command`, `get_datetime`, `list_processes`, `kill_process` y
`list_drives` se quedaron sin categoría. El refuerzo por intención "sistema /
shell / comando" acababa apuntando a las dos herramientas equivocadas. Un test
lo habría cazado; la taxonomía no era testeable porque no era un módulo.

Aquí vive la única copia. Este archivo NO importa nada del proyecto (ni
`dev_agent` ni `agent_core`), así que lo pueden consumir los tres sin ciclos:

* `dev_agent`  — `query_tools`, el ranking por embeddings y el prompt;
* `agent_core` — la barrera de progreso (qué es "trabajo productivo");
* `tool_guard` — recibe los conjuntos como parámetros (por defecto, estos).

Regla de mantenimiento
----------------------
Al añadir una herramienta se toca ESTE archivo (nombre, tipo, categoría,
descripción) y `dev_agent.TOOL_IMPL`/`TOOL_SCHEMAS` (implementación y esquema).
`audit()` comprueba la coherencia y `tests/test_tool_taxonomy.py` la ejecuta
contra las herramientas realmente implementadas, así que una herramienta
descrita y no implementada (o al revés) no puede pasar desapercibida.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple


# =============================================================================
# 1. ¿La herramienta PRODUCE trabajo o solo mira?
# =============================================================================
# La distinción la usa `_run_agency_mode` para saber si un paso de
# "construction" terminó sin producir nada, y `ToolCallGuard` para cortar
# rachas de exploración. Se declara por nombre, sin importar la implementación.

#: Herramientas que modifican el workspace o ejecutan trabajo real.
PRODUCTIVE_TOOLS: FrozenSet[str] = frozenset({
    "write_file",
    "edit_file",
    "apply_diff",
    "create_directory",
    "delete_file",
    "delete_directory",
    "move_file",
    "compress_files",
    "extract_archive",
    "run_python_code",
    "exec_shell_command",
    "run_command_async",
    "sqlite_execute",
    "git_commit",
})

#: Herramientas que escriben en el workspace recibiendo la ruta en el
#: argumento `path`: tras llamarlas hay que reindexar esa ruta en el índice del
#: workspace (`Agent.run_stream`), o el siguiente paso trabajará con un índice
#: obsoleto. Es un subconjunto de PRODUCTIVE_TOOLS a propósito.
FILE_MUTATING_TOOLS: FrozenSet[str] = frozenset({
    "write_file",
    "edit_file",
    "delete_file",
})

#: Herramientas de solo lectura / exploración: NO cuentan como progreso.
READONLY_TOOLS: FrozenSet[str] = frozenset({
    "list_directory",
    "read_file",
    "file_glob_search",
    "grep_search",
    "tree",
    "view",
    "search",
    "get_datetime",
    "list_processes",
    "list_drives",
    "list_libraries",
    "list_tasks",
    "list_skills",
    "list_checkpoints",
    "job_status",
    "job_output",
    "job_list",
    "git_status",
    "git_diff",
    "git_log",
    "get_db_schema",
    "query_tools",
})


# =============================================================================
# 2. Descripción por herramienta (texto para el modelo y para los embeddings)
# =============================================================================

TOOL_DESCRIPTIONS: Dict[str, str] = {
    "read_file": "Lee archivos de texto plano (.txt, .py, .json, .csv, .md, .html, etc.). Si el contenido es JSON, lo formatea automaticamente para mayor legibilidad. No soporta archivos Office (.docx, .xlsx, .pptx). Ejemplo: read_file('datos.json')",
    "write_file": "Escribe archivos de texto plano (.txt, .py, .json, .csv, .md, .html, etc.). Parametro mode: 'w' (escribir, por defecto) o 'a' (append). No soporta archivos Office. Ejemplo: write_file('salida.txt', 'contenido')",
    "edit_file": "Edita archivos de texto plano sin perder formato. Operaciones: replace, append, insert_after, insert_before, delete. Si no se especifica operation, detecta automaticamente (old_text+new_text=replace, solo new_text=append). No soporta archivos Office. Ejemplo: edit_file('config.txt', old_text='valor_viejo', new_text='valor_nuevo')",
    "apply_diff": "Aplica un patch unificado (diff) a un archivo. Útil para aplicar cambios generados por otros sistemas. Sinónimos: patch, aplicar cambios.",
    "file_glob_search": "Busca archivos usando patrones glob (*, ?). Útil para encontrar archivos por nombre o extensión. Sinónimos: buscar archivos, find, ls **/*.",
    "grep_search": "Busca texto dentro de archivos usando expresiones regulares. Útil para encontrar código, palabras clave o configuraciones. Sinónimos: buscar texto, grep, findstr.",
    "exec_shell_command": "Ejecuta un comando en la shell del sistema. Poderoso pero restringido a comandos seguros. Sinónimos: ejecutar comando, run, sh, cmd.",
    "get_datetime": "Obtiene la fecha y hora actual del sistema. Útil para timestamps, logs, o saber qué hora es. Sinónimos: fecha, hora, timestamp, now.",
    "create_directory": "Crea un nuevo directorio o carpeta. Útil para organizar archivos o preparar espacio. Sinónimos: mkdir, crear carpeta, new folder.",
    "list_directory": "Lista un directorio y, opcionalmente, sus subdirectorios. Uso: execute_tool(tool_name='list_directory', arguments={'path': '.', 'depth': 2}). depth=0 muestra sólo el nivel indicado; depth=1 incluye hijos; depth=2 incluye nietos.",
    "delete_file": "Mueve un archivo o directorio a la papelera de reciclaje (seguro). Sinónimos: borrar, eliminar, trash, rm (seguro).",
    "move_file": "Mueve o renombra un archivo o directorio. Útil para reorganizar. Sinónimos: mv, renombrar, mover.",
    "compress_files": "Comprime archivos en un zip. Útil para empaquetar, reducir tamaño. Sinónimos: zip, comprimir, empaquetar.",
    "extract_archive": "Extrae archivos de un zip o rar. Útil para descomprimir. Sinónimos: unzip, descomprimir, extraer.",
    "create_venv": "Crea un entorno virtual de Python aislado. Útil para gestionar dependencias. Sinónimos: venv, virtualenv, entorno virtual.",
    "run_python_code": "Ejecuta código Python en un entorno virtual o sistema (síncrono, ~30s). Útil para automatizar, calcular, procesar datos. Sinónimos: python, ejecutar script.",
    "run_command_async": "Lanza un comando o proceso LARGO en segundo plano y devuelve un job_id al instante, sin bloquear la conversación ni abrir consola. Úsalo para pip install, builds, servidores, descargas y scripts largos. Ejemplo: run_command_async('pip install pandas')",
    "job_status": "Estado de un job lanzado con run_command_async: running/exited/error/killed, exit_code y tiempo transcurrido. Ejemplo: job_status('a1b2c3d4')",
    "job_output": "Lee las últimas líneas de la salida de un job. tail=0 devuelve todo. Ejemplo: job_output('a1b2c3d4', tail=200)",
    "job_kill": "Detiene un job en ejecución matando su árbol de procesos. Ejemplo: job_kill('a1b2c3d4')",
    "job_list": "Lista los jobs de la sesión (running/exited/error) con su comando y tiempos.",
    "workspace_download_link": "Devuelve una URL absoluta (válida también en remoto o tras nginx) para descargar o ver un archivo del workspace. Ejemplo: workspace_download_link('informe.pdf')",
    "sqlite_query": "Ejecuta consultas SQL (SELECT) en una base SQLite. Útil para extraer datos. Sinónimos: SQL, consulta, SELECT.",
    "sqlite_execute": "Ejecuta comandos SQL (INSERT, UPDATE, DELETE) en SQLite. Sinónimos: modificar SQL, INSERT, UPDATE.",
    "get_db_schema": "Obtiene el esquema de una base SQLite (tablas y columnas). Útil para conocer la estructura. Sinónimos: schema, estructura, tablas.",
    "list_processes": "Lista los procesos del sistema en ejecución. Sinónimos: ps, tasklist, procesos.",
    "kill_process": "Termina un proceso por su PID. Sinónimos: matar proceso, taskkill.",
    "screenshot": "Captura la pantalla completa y guarda una imagen. Sinónimos: print screen, captura de pantalla.",
    "take_photo": "Toma una foto con la cámara web. Sinónimos: foto, selfie, cámara.",
    "notify": "Envía una notificación del sistema al usuario. Sinónimos: alerta, popup, notificar.",
    "open_url": "Abre una URL en el navegador predeterminado. Sinónimos: navegar, abrir web, visitar.",
    "add_task": "Añade una tarea a la lista TODO. Sinónimos: tarea, pendiente, añadir tarea.",
    "list_tasks": "Lista las tareas pendientes o completadas. Sinónimos: todo, pendientes.",
    "complete_task": "Marca una tarea como completada. Sinónimos: terminar, finalizar.",
    "set_reminder": "Programa un recordatorio para después de N segundos. Sinónimos: recordar, alarma, timer.",
    "print_file": "Envía un archivo a la impresora (Windows o CUPS). Sinónimos: imprimir, imprimir archivo.",
    "open_file_with_default_app": "Abre un archivo con su aplicación predeterminada (ej. .pdf, .docx). Sinónimos: abrir, launch.",
    "rag_search": "Busca información en documentos indexados (RAG) usando búsqueda semántica híbrida. Útil para investigar, recuperar información de documentos. Sinónimos: buscar documento, investigar, buscar en documentos, RAG.",
    "list_libraries": "Lista los documentos indexados en la biblioteca RAG activa. Sinónimos: bibliotecas, documentos indexados.",
    "set_active_library": "Cambia la biblioteca activa de RAG. Sinónimos: cambiar biblioteca, activar.",
    "store_memory": "Guarda un fragmento en la memoria del agente para recordarlo después. Sinónimos: recordar, guardar contexto.",
    "retrieve_memories": "Recupera memorias relevantes del agente. Sinónimos: recordar, memoria.",
    "send_telegram": "Envía un mensaje por Telegram usando el bot configurado. Sinónimos: telegram, mensaje.",
    "setup_telegram": "Configura las credenciales del bot de Telegram. Sinónimos: configurar bot, token.",
    "download_file": "Descarga un archivo desde una URL. Sinónimos: descargar, wget, curl.",
    "git_status": "Muestra el estado del repositorio git (archivos modificados). Sinónimos: git status.",
    "git_commit": "Realiza un commit en git con los cambios añadidos. Sinónimos: git commit.",
    "git_diff": "Muestra las diferencias entre cambios en git. Sinónimos: git diff.",
    "git_log": "Muestra el historial de commits en git. Sinónimos: git log.",
    "start_watchdog": "Inicia el monitor de cambios en archivos (watchdog). Sinónimos: monitorear, vigilar cambios.",
    "stop_watchdog": "Detiene el monitor de cambios. Sinónimos: detener monitor.",
    "get_watchdog_events": "Obtiene los eventos registrados por el monitor. Sinónimos: eventos, cambios.",
    "convert_document": "Convierte documentos entre formatos (usando pandoc o fallback). Sinónimos: convertir, transformar, pandoc.",
    "list_drives": "Lista las unidades de disco disponibles (Windows/Linux). Sinónimos: unidades, discos.",
    "classify_text": "Clasifica un texto en categorías usando el LLM. Sinónimos: clasificar, categorizar.",
    "delete_directory": "Elimina un directorio y todo su contenido de forma recursiva. Sinónimos: borrar directorio, eliminar carpeta, rmdir, rm -rf.",
    "lint_code": "Analiza el código Python en busca de errores de estilo y problemas comunes usando flake8.",
    "check_security": "Busca vulnerabilidades de seguridad en el código Python usando bandit.",
    "type_check": "Verifica la consistencia de tipos estáticos usando pyright.",
    "measure_complexity": "Mide la complejidad ciclomática del código usando radon.",
    "run_tests": "Ejecuta pruebas unitarias con pytest y muestra los resultados.",
    "generate_unit_tests": "Genera pruebas unitarias para una función o archivo usando IA.",
    "generate_docstrings": "Añade docstrings a funciones y métodos usando IA.",
    "generate_readme": "Genera un archivo README.md para el proyecto usando IA.",
    "generate_api_docs": "Genera documentación API con pdoc o sphinx.",
    "format_code": "Formatea el código Python usando black.",
    "sort_imports": "Ordena y agrupa los imports usando isort.",
    "remove_unused_imports": "Elimina imports no utilizados usando autoflake.",
    "clean_code": "Limpia el código: elimina imports no usados, ordena y formatea.",
    "add_license": "Añade un encabezado de licencia a los archivos .py.",
    "check_dependencies": "Revisa vulnerabilidades en las dependencias del proyecto.",
    "check_system_tool": "Verifica si una herramienta del sistema está instalada.",
    "install_system_tool": "Proporciona instrucciones para instalar una herramienta del sistema.",
    "review_code": "Genera un informe completo de calidad del código.",
    "summarize_changes": "Resume los cambios desde el último checkpoint.",
    "chat_completion": "Envía mensajes al LLM y devuelve la respuesta (sin herramientas).",
    "list_skills": "Lista los skills disponibles (memoria procedimental).",
    "get_skill": "Obtiene el contenido completo de un skill por su nombre.",
    "create_skill": "Crea un nuevo skill con pasos definidos.",
    "promote_skill": "Promueve un skill auto-generado a skill permanente.",
    "delete_skill": "Elimina un skill del workspace.",
    "create_checkpoint": "Crea un checkpoint del estado actual del proyecto.",
    "list_checkpoints": "Lista los checkpoints disponibles.",
    "rollback_checkpoint": "Restaura el proyecto y/o estado del agente desde un checkpoint.",
    "delete_checkpoint": "Elimina un checkpoint específico.",
    "get_checkpoint_info": "Obtiene información detallada de un checkpoint.",
    "start_daemon_tool": "Inicia el agente en modo daemon para trabajar de forma autónoma en segundo plano.",
    "stop_daemon_tool": "Detiene el daemon en ejecución.",
    "daemon_status_tool": "Muestra el estado actual del daemon.",
}

#: Herramientas base que SIEMPRE entran en el contexto del modelo. Son las
#: mismas que anuncia `BASE_TOOLS_CHEATSHEET`: el tope de `top_k` dejaba fuera
#: `write_file`/`edit_file`, y sin ellas el modelo caía en `query_tools` para
#: intentar escribir un archivo.
BASE_TOOLS: Tuple[str, ...] = (
    "list_directory",
    "read_file",
    "write_file",
    "edit_file",
    "get_datetime",
    "exec_shell_command",
)

#: Chuletas que se añaden al prompt de `execute_tool` (atajos siempre
#: disponibles, para que el modelo no gaste una llamada en `query_tools`).
BASE_TOOLS_CHEATSHEET = (
    "Tools base vía execute_tool(tool_name, arguments): "
    "read_file(path), write_file(path,content), edit_file(path,old_text,new_text), "
    "file_glob_search(pattern,path='.'), grep_search(pattern,path='.'), "
    "exec_shell_command(command), get_datetime(), list_directory(path,depth=0), "
    "run_python_code(code,venv=None), run_command_async(command), job_output(job_id), "
    "job_status(job_id), job_kill(job_id). Úsalas directamente; no necesitas query_tools para estas. "
    "Si dudas, usa query_tools."
)


# =============================================================================
# 3. Categorías (refuerzo por intención en `get_relevant_tools`)
# =============================================================================
# Cada herramienta puede estar en UNA sola categoría: si una clave se declarara
# dos veces, la segunda machacaría a la primera sin avisar (era justo el bug de
# `system`). `audit()` avisa de nombres desconocidos y de categorías sin
# herramientas.

TOOL_CATEGORIES: Dict[str, List[str]] = {
    "filesystem": ["list_directory", "create_directory", "delete_file", "delete_directory", "move_file", "read_file", "write_file", "edit_file", "apply_diff", "file_glob_search", "grep_search"],
    "system": ["exec_shell_command", "get_datetime", "list_processes", "kill_process", "list_drives", "check_system_tool", "install_system_tool"],
    "db": ["sqlite_query", "sqlite_execute", "get_db_schema"],
    "rag": ["rag_search", "list_libraries", "set_active_library"],
    "code": ["run_python_code", "create_venv"],
    "jobs": ["run_command_async", "job_status", "job_output", "job_kill", "job_list"],
    "git": ["git_status", "git_commit", "git_diff", "git_log"],
    "media": ["screenshot", "take_photo", "notify"],
    "telegram": ["send_telegram", "setup_telegram"],
    "todo": ["add_task", "list_tasks", "complete_task"],
    "network": ["open_url", "download_file"],
    "compression": ["compress_files", "extract_archive"],
    "printing": ["print_file"],
    "watchdog": ["start_watchdog", "stop_watchdog", "get_watchdog_events"],
    "conversion": ["convert_document", "classify_text"],
    "code_quality": ["lint_code", "check_security", "type_check", "measure_complexity"],
    "testing": ["run_tests", "generate_unit_tests"],
    "documentation": ["generate_docstrings", "generate_readme", "generate_api_docs"],
    "refactoring": ["format_code", "sort_imports", "remove_unused_imports", "clean_code"],
    "licensing": ["add_license", "check_dependencies"],
    "review": ["review_code", "summarize_changes"],
    "skills": ["list_skills", "get_skill", "create_skill", "promote_skill", "delete_skill"],
    "checkpoints": ["create_checkpoint", "list_checkpoints", "rollback_checkpoint", "delete_checkpoint", "get_checkpoint_info"],
    "daemon": ["start_daemon_tool", "stop_daemon_tool", "daemon_status_tool"],
}

CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "filesystem": ["directorio", "carpeta", "archivo", "file", "listar", "crear", "borrar", "mover", "editar", "buscar archivo", "renombrar", "eliminar", "borrar"],
    "system": ["sistema", "shell", "comando", "proceso", "fecha", "hora", "disco", "unidad", "ejecutar", "correr", "herramienta sistema", "instalar", "verificar"],
    "db": ["base de datos", "sqlite", "sql", "tabla", "consulta", "schema", "esquema"],
    "rag": ["rag", "documento", "biblioteca", "indexado", "buscar documento", "listar biblioteca"],
    "code": ["python", "código", "ejecutar código", "entorno virtual", "venv"],
    "jobs": ["job", "segundo plano", "background", "async", "pip install", "instalar dependencia", "proceso largo", "servidor", "build", "compilar", "descarga"],
    "git": ["git", "commit", "pull", "push", "estado", "diff", "historial"],
    "media": ["captura", "pantalla", "screenshot", "foto", "cámara", "notificación"],
    "telegram": ["telegram", "bot", "mensaje", "chat"],
    "todo": ["tarea", "todo", "pendiente", "completar", "lista"],
    "network": ["url", "descargar", "navegador", "web"],
    "compression": ["comprimir", "zip", "descomprimir", "extraer"],
    "printing": ["imprimir"],
    "watchdog": ["monitor", "cambios", "vigilar"],
    "conversion": ["convertir", "formato", "clasificar"],
    "code_quality": ["calidad código", "lint", "estilo", "seguridad", "tipos", "complejidad"],
    "testing": ["pruebas", "tests", "pytest", "unit tests"],
    "documentation": ["documentación", "docstrings", "README", "API docs"],
    "refactoring": ["refactorizar", "formatear", "limpiar código", "ordenar imports"],
    "licensing": ["licencia", "dependencias", "vulnerabilidades"],
    "review": ["revisión", "informe", "resumen cambios"],
    "skills": ["skill", "memoria", "aprender", "lección", "procedimiento"],
    "checkpoints": ["checkpoint", "rollback", "restaurar", "guardar estado", "persistencia", "snapshot"],
    "daemon": ["daemon", "fondo", "autónomo", "continuo", "24/7", "servicio"],
}


# =============================================================================
# 4. Herramientas cuyos parámetros llegan como string JSON
# =============================================================================

TOOLS_JSON_PARAMS: Dict[str, List[str]] = {
    "create_skill": ["steps_json"],
    "create_checkpoint": ["state_json"],
}


# =============================================================================
# 5. Derivados y auditoría
# =============================================================================

#: Tipos posibles de una herramienta.
PRODUCTIVE = "productive"
READONLY = "readonly"
OTHER = "other"

#: Categoría -> herramientas (copia ordenada de `TOOL_CATEGORIES`).
CATEGORY_TOOLS: Dict[str, Tuple[str, ...]] = {
    category: tuple(names) for category, names in TOOL_CATEGORIES.items()
}

#: Herramienta -> categorías que la contienen, en orden de declaración.
_TOOL_CATEGORY_INDEX: Dict[str, Tuple[str, ...]] = {}
for _category, _names in TOOL_CATEGORIES.items():
    for _name in _names:
        _TOOL_CATEGORY_INDEX[_name] = _TOOL_CATEGORY_INDEX.get(_name, ()) + (_category,)

#: Todas las herramientas que la taxonomía conoce por nombre (descritas,
#: productivas o de solo lectura). No incluye los atajos de `dev_agent.TOOL_IMPL`
#: (`view`, `edit`, `search`, `bash`, `tree`), que no se describen al modelo.
KNOWN_TOOLS: FrozenSet[str] = (
    frozenset(TOOL_DESCRIPTIONS) | PRODUCTIVE_TOOLS | READONLY_TOOLS
)

#: Herramientas que aparecen en alguna categoría.
CATEGORIZED_TOOLS: FrozenSet[str] = frozenset(_TOOL_CATEGORY_INDEX)


def kind_of(name: Optional[str]) -> str:
    """`productive` | `readonly` | `other` para una herramienta.

    Una herramienta que no esté declarada en ninguna de las dos listas es
    `other`: no produce trabajo, pero tampoco se cuenta como exploración. Es
    intencional no adivinar por el nombre.
    """
    if name in PRODUCTIVE_TOOLS:
        return PRODUCTIVE
    if name in READONLY_TOOLS:
        return READONLY
    return OTHER


def is_productive(name: Optional[str]) -> bool:
    """True si la herramienta modifica el workspace o ejecuta trabajo real."""
    return name in PRODUCTIVE_TOOLS


def is_readonly(name: Optional[str]) -> bool:
    """True si la herramienta solo explora (no cuenta como progreso)."""
    return name in READONLY_TOOLS


def category_of(name: str) -> Optional[str]:
    """Primera categoría declarada para la herramienta (o `None`)."""
    categories = _TOOL_CATEGORY_INDEX.get(name) or ()
    return categories[0] if categories else None


def categories_of(name: str) -> Tuple[str, ...]:
    """Todas las categorías que contienen la herramienta."""
    return _TOOL_CATEGORY_INDEX.get(name, ())


def tools_in(category: str) -> Tuple[str, ...]:
    """Herramientas declaradas en una categoría."""
    return CATEGORY_TOOLS.get(category, ())


def audit(implemented: Optional[Iterable[str]] = None) -> Dict[str, List[str]]:
    """Inconsistencias de la taxonomía. `{}` == todo coherente.

    Sin argumentos solo se auditan las listas declaradas aquí:

    * `overlap`            — herramientas a la vez productivas y de solo lectura.
    * `unknown_in_category`— nombres en categorías que no existen en la taxonomía.
    * `undescribed`        — herramientas categorizadas sin descripción.
    * `category_without_keywords` / `keywords_without_category` — desalineadas.

    Con `implemented` (los nombres reales de `dev_agent`) se añaden:

    * `described_not_implemented` — descrita pero no existe.
    * `unclassified`              — implementada sin tipo (habitual en atajos).
    """
    problems: Dict[str, List[str]] = {}

    def _add(key: str, values: Iterable[str]) -> None:
        ordered = sorted({v for v in values if v})
        if ordered:
            problems[key] = ordered

    _add("overlap", PRODUCTIVE_TOOLS & READONLY_TOOLS)
    _add("unknown_in_category", CATEGORIZED_TOOLS - KNOWN_TOOLS)
    _add("undescribed", CATEGORIZED_TOOLS - frozenset(TOOL_DESCRIPTIONS))
    _add("category_without_keywords", (c for c in TOOL_CATEGORIES if c not in CATEGORY_KEYWORDS))
    _add("keywords_without_category", (c for c in CATEGORY_KEYWORDS if c not in TOOL_CATEGORIES))

    if implemented is not None:
        available = frozenset(implemented)
        _add("described_not_implemented", frozenset(TOOL_DESCRIPTIONS) - available)
        _add("unclassified", (n for n in available if kind_of(n) == OTHER))

    return problems


def _implemented_tool_names() -> FrozenSet[str]:
    """Nombres de herramientas realmente implementadas en `dev_agent`.

    Se importa en diferido: este módulo es la base común y no puede depender de
    `dev_agent` (que a su vez importa `agent_core`).
    """
    names: set = set()
    try:
        try:
            from tools import dev_agent  # type: ignore
        except ImportError:
            import dev_agent  # type: ignore
        names.update(getattr(dev_agent, "TOOL_IMPL", {}).keys())
        getter = getattr(dev_agent, "get_all_tool_functions", None)
        if callable(getter):
            names.update(getter().keys())
    except Exception as exc:  # pragma: no cover - entorno sin dev_agent
        print(f"[tool_taxonomy] no se pudo importar dev_agent: {exc}")
    return frozenset(names)


def _summary() -> Dict[str, object]:
    """Vista compacta: categorías, tipos y cobertura."""
    kinds: Dict[str, List[str]] = {PRODUCTIVE: [], READONLY: [], OTHER: []}
    for name in sorted(KNOWN_TOOLS):
        kinds[kind_of(name)].append(name)
    return {
        "tools": len(KNOWN_TOOLS),
        "described": len(TOOL_DESCRIPTIONS),
        "categories": len(TOOL_CATEGORIES),
        "kinds": {kind: len(names) for kind, names in kinds.items()},
        "uncategorized": sorted(KNOWN_TOOLS - CATEGORIZED_TOOLS),
    }


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m tools.tool_taxonomy [--report|--audit|--tool NAME]`."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="tool_taxonomy", description="Taxonomía de herramientas del agente."
    )
    parser.add_argument("--report", action="store_true", help="Resumen de la taxonomía.")
    parser.add_argument("--audit", action="store_true", help="Inconsistencias (y contra dev_agent).")
    parser.add_argument("--tool", metavar="NAME", help="Detalle de una herramienta.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.tool:
        name = args.tool
        print(json.dumps({
            "name": name,
            "kind": kind_of(name),
            "categories": list(categories_of(name)),
            "description": TOOL_DESCRIPTIONS.get(name),
            "in_taxonomy": name in KNOWN_TOOLS,
        }, ensure_ascii=False, indent=2))
        return 0

    if args.audit:
        problems = audit(implemented=_implemented_tool_names())
        print(json.dumps(problems or {"ok": []}, ensure_ascii=False, indent=2))
        return 1 if problems else 0

    print(json.dumps(_summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
