# `tools/pipeline_templates.py` — El plan como plantilla

Construye el plan de la agencia **sin LLM**. Sustituye los ~470 líneas de
`agent_core` que parseaban, coalescían y validaban planes generados por el
modelo (`_parse_plan`, `_coalesce_plan_steps`, `_plan_budget`, …).

## Plantillas

Cada `Phase` es un `frozen dataclass` con su sub-tarea, agente preferido y
dependencias. Las familias:

| Naturaleza | Pipeline llena | Pipeline compacta |
|---|---|---|
| `synthesis` | `_SYNTHESIS_PIPELINE` (facetas) | — |
| `mixed` | `_MIXED_PIPELINE` | — |
| `construction` (código) | `_CONSTRUCTION_CODING_FULL` | `_CONSTRUCTION_CODING_COMPACT` |
| `construction` (estructura) | estructura → producción → revisión | versión reducida |
| `ops` | inspección → cambio → verificación → documentación | `_OPS_COMPACT` |

Límites: `MAX_STEPS = 12`, `MAX_FACETS = 3` (`MAX_FACETS_COMPACT = 2`).

## Dimensionado por la tarea

La plantilla de código completa **no se aplica siempre**. `_coding_pipeline`
recorta las fases que no aplican según la tarea:

* `data` solo entra si hay `DATA_MARKERS` (base de datos, schema, migración,
  persistencia…);
* `delivery` solo entra si hay `DELIVERY_MARKERS` (deploy, docker, readme,
  release…).

Una calculadora de un archivo no recibe "define el modelo de datos y su
migración": es la subtarea absurda que hacía divagar al agente. Al recortar
fases, `_linear` reencadena las que quedan (cada una depende de la anterior
superviviente).

La asignación de agente usa esta precedencia: área de la fase → generalista
universal (`agency_domains.UNIVERSAL_AGENTS`) → mejor coincidencia de palabras
clave → reparto round-robin. El generalista va antes que la coincidencia floja
para que un especialista ajeno no acabe en una fase que no le toca (un
`Godot Gameplay Scripter` "integrando" una calculadora porque su `vibe`
contenía `app`).

## Fusión de fases del mismo agente

Cuando dos fases contiguas acaban asignadas al **mismo agente** y no
introducen dependencias externas nuevas, `build_plan` las funde en un solo
paso: concatena sus subtareas y registra todas las fases en una lista
`phases`. Así un plan de un único agente no repite siete pasos del mismo
especialista; el orden y la cobertura de fases se conservan.

- `step["phase"]`: fase única, o unión con `+` si hubo fusión
  (p. ej. `"setup+data+core"`).
- `step["phases"]`: **solo aparece** en pasos fusionados; lista las fases en
  orden.

Los consumidores que necesiten la secuencia de fases deben aplanar
`step.get("phases", [step["phase"]])`.

## API

| Función | Para qué |
|---|---|
| `pipeline_for(nature, domains, decompose)` | Elige la plantilla. |
| `pipeline_name(...)` | Nombre legible (`construction:setup:full`, `synthesis:inspect:compact`, …). |
| `score_agent(agent, phase, area)` | Puntúa agentes para una fase. |
| `build_plan(task, selected, nature, domains, decompose)` | **Entrada principal.** |
| `recovery_step(step, ...)` | Paso de corrección determinista. |
| `is_recovery(step)` | ¿Es un paso de recuperación? |

## Contrato de `build_plan`

```python
{
  "steps": [{
      "id": str,            # "1", "1R1", ...
      "agent": str,
      "subtask": str,       # lo único que puede venir del LLM
      "depends_on": [str],
      "artifact": None,     # SIEMPRE None; el artefacto real lo fija la ejecución
      "phase": str,         # setup | core | interface | tests | ... (o unión con +)
      "phases": [str],      # opcional: fases fusionadas en este paso, en orden
  }],
  "supervisor": "Agency Pipeline",
  "nature": str,
  "pipeline": str,
  "source": "template",
  "artifact": None,
}
```

`artifact` viaja como `None` a propósito: el plan no puede inventar rutas que
luego no existan. El artefacto real se descubre al cerrar el paso.

## Recuperación

`recovery_step` crea un paso id `"{id}R{n}"` con `RECOVERY_MARKER =
"[CORRECCIÓN]"`. `is_recovery` lo reconoce y `MAX_RECOVERY_ATTEMPTS = 1` limita
los reintentos para que un paso roto no se reintente en bucle infinito.

## Ejemplos verificados

| Petición | Pipeline | Pasos |
|---|---|---|
| `Crea una app FastAPI con base de datos, docker y tests` | `construction:setup:full` | 7, con dependencias |
| `Crea un entorno python con una calculadora GUI` | `construction:setup:full` | 5 (sin `data` ni `delivery`) |
| `Refactoriza este módulo` | compacto | 3 |
| `Diseña un sitio web` | `construction:structure:full` | — |
| `Configura nginx` | `synthesis:inspect:compact` | — |
| `Investiga X y crea un sitio web` | `construction:research:full` | 5 |
