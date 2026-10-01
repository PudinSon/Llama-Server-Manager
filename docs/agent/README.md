# Modo agente determinista

Refactorización del modo agente: **el LLM solo genera CONTENIDO**. Todo lo demás
—clasificar la petición, elegir agentes, construir el plan, impedir bucles,
verificar artefactos y persistir estado— es código Python determinista y
testeable, sin llamadas al modelo.

Este directorio documenta los cuatro módulos que sostienen esa decisión:

| Módulo | Responsabilidad | Documento |
|---|---|---|
| `tools/task_router.py` | ¿CHAT o AGENCY? ¿Qué naturaleza y qué dominios? | [task_router.md](task_router.md) |
| `tools/pipeline_templates.py` | El plan: fases, pasos, dependencias y recuperación | [pipeline_templates.md](pipeline_templates.md) |
| `tools/agency_domains.py` | Qué agentes encajan con los dominios detectados | [agency_domains.md](agency_domains.md) |
| `tools/tool_guard.py` | Barrera anti-bucle y anti-exploración por fase | [tool_guard.md](tool_guard.md) |

El modo AGENCY se apoya además en `tools/tool_policy.py` (whitelist por agente)
y `tools/tool_taxonomy.py` (taxonomía única de herramientas).

## El recorrido de una petición

```
petición
   │
   ▼
task_router.decide()        →  modo (CHAT/AGENCY), naturaleza, dominios
   │
   ▼
agency_domains.select()     →  agentes candidatos (filtrado por dominio)
   │
   ▼
pipeline_templates.build_plan()  →  plan[fases, pasos, dependencias]
   │
   ▼
por cada paso: Agent.run_stream() con tool_guard + tool_policy
   │
   ▼
verificación de artefacto → recuperación determinista (recovery_step) si falta
```

Ninguno de esos pasos consulta al LLM para decidir *qué hacer*; el LLM solo
redacta el `subtask` de cada paso y produce el contenido final.

## Piezas que esta refactorización unificó

- **Un único cliente de herramientas** (`tools/mcp_client.py`): el core, el CLI
  y los servicios pasan por el mismo punto; incluye el respaldo en proceso
  cuando el servidor MCP no responde.
- **Un único reductor en la UI nativa**: el transporte SSE vive en
  `src/lib/utils/agency-stream.ts` y el estado se aplica en
  `agentStore.applyChatAgencyEvent`; el store de chat es el único consumidor.
- **Una única taxonomía de herramientas** (`tools/tool_taxonomy.py`),
  consumida por `agent_core`, `dev_agent` y `tool_policy`.
- **Una whitelist por agente** (`tools/tool_policy.py`), aplicada antes de
  ejecutar cualquier herramienta.

## Ejecutar la auditoría

Los módulos con CLI son `agency_domains`, `tool_taxonomy` y `tool_policy`:

```bash
python -m tools.agency_domains --report
python -m tools.agency_domains --agent "Developer Tooling Engineer"
python -m tools.tool_taxonomy --report
python -m tools.tool_taxonomy --audit
python -m tools.tool_policy --report
python -m tools.tool_policy --agent developer --domains coding,ops --mode synthesis
```

`task_router`, `pipeline_templates` y `tool_guard` se ejercitan desde los tests
(`tests/test_task_nature.py`, `tests/test_pipeline_templates.py`,
`tests/test_tool_guard.py`).
