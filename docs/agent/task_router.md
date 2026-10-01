# `tools/task_router.py` — Clasificación determinista

Decide, **sin LLM**, qué hacer con una petición. Reemplazó al antiguo
`ToolRouter` (que le preguntaba al modelo) y a un detector léxico ad-hoc.

## Vocabulario

```python
MODES   = ("CHAT", "AGENCY")
NATURES = ("synthesis", "construction", "mixed")
DOMAINS = ("coding", "research", "math", "creative", "business", "ops")
```

## Funciones

| Función | Para qué |
|---|---|
| `is_conversational(task)` | Saludos, preguntas cortas, small talk (`CONVERSATIONAL_MAX_CHARS`). |
| `detect_nature(task)` | `synthesis` (redactar/investigar) vs `construction` (crear/modificar) vs `mixed`. |
| `detect_domains(task)` | Hasta `MAX_DOMAINS = 3` dominios, por `DOMAIN_MARKERS`. |
| `looks_multi_step(task)` | Cadenas de verbos, secuenciadores y entregables. |
| `agency_signals(task)` | Señales crudas (verbos, secuencia, entregables) para depurar. |
| `construction_scope_hits(task)` | Marcadores de alcance (`CONSTRUCTION_SCOPE_MARKERS`). |
| `requires_decomposition(task)` | Si hace falta partir la tarea (`CONSTRUCTION_SCOPE_MIN = 2`, `FORCE_AGENCY_MIN_VERBS = 3`). |
| `decide(task)` | **Entrada principal.** Devuelve modo, naturaleza, dominios y `domains_source`. |

## `decide()` y la unión Laya + léxico

`decide` combina dos fuentes y las reconcilia con `domains_source`:

1. **Laya** (`laya_onnx_router.route_domains()`) — clasificador ONNX local. Si
   el modelo no declara la cabeza de dominios, devuelve `None`.
2. **Léxico** (`detect_domains`) — siempre disponible.

Cuando Laya no aporta (o su I/O ONNX no está calibrado) el resultado cae al
léxico **por diseño**: el router nunca depende de que el clasificador esté
presente. La salida de `decide` es el contrato que consumen
`agent_core._run_agency_mode`, `agency_domains.select` y
`pipeline_templates.pipeline_for`.

## Bug corregido

`"Refactoriza este módulo Python"` clasificaba como **CHAT** porque el verbo
`refactoriza` no estaba en `ACTION_VERBS`. Ahora ese caso entra en AGENCY con
dominio `coding`.
