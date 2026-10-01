# `tools/tool_guard.py` — Barrera anti-bucle

Unifica las **5 guardias inline** que estaban repartidas por `agent_core`
(rachas de solo lectura, reintentos de la misma llamada fallida, trabajo
duplicado entre pasos…) en una sola clase testeable.

## Piezas

| Pieza | Qué hace |
|---|---|
| `GuardDecision` | Resultado: `allowed`, `blocked`, `kind`, `reason`. |
| `ALLOW` | Decisión por defecto. |
| `PhaseLimits` | Umbrales por fase (`readonly_streak`, reintentos…). |
| `PHASE_LIMITS` | Tabla de límites; `TOOLLESS_PHASES = ("chat", "synthesis")`. |
| `limits_for(phase)` | Límites de una fase (con respaldo). |
| `result_failed(result)` | ¿El texto de la herramienta es un fallo? |
| `signature(name, args)` | Firma estable de una llamada. |
| `normalize_dir_args(args)` | Normaliza `path`/`depth` para comparar. |
| `ToolCallGuard` | El guardia. |

## `ToolCallGuard`

| Método | Cuándo |
|---|---|
| `check(name, args)` | **Antes** de ejecutar: repetición y bloqueos. |
| `record(name, args, result, ok)` | **Después**: progreso, rachas y circuit-breaker. |
| `claim(name, args, step_id)` | Trabajo duplicado entre pasos del mismo plan. |

`check` puede cerrar el paso en **éxito** (`kind == "repeat"`: repetir una
llamada que ya trajo datos es la señal de que el trabajo está hecho) o marcar
el paso como `incomplete` (explorar en bucle, bombardear una herramienta).

En las fases de `MUST_PRODUCE_PHASES` (hoy solo `construction`) la repetición
se parte en dos según haya o no progreso:

* repetir una llamada **productiva** → `repeat`, cierre en éxito;
* repetir una llamada de **solo lectura** sin haber producido nada →
  `repeat_corrective`: la llamada no se ejecuta y el agente avisa al modelo
  ("deja de explorar y produce el entregable") en vez de cerrar el paso; tras
  `repeat_correctives` avisos pasa a `no_progress` y el paso se corta.

Sin esta distinción, un modelo que listaba el directorio dos veces cerraba el
paso como éxito sin haber creado nada, y el verificador del plan lo fallaba
igualmente: el paso moría y el siguiente repetía el mismo baile.

`claim` solo deduplica el trabajo **productivo**: volver a mirar el workspace
(`list_directory`, `read_file`…) no es trabajo duplicado. Antes lo era, y eso
convertía la exploración normal de cada paso en un fallo garantizado (el paso 2
moría por repetir el `list_directory` del paso 1 antes de intentar nada).

El módulo no expone CLI; se ejercita desde `tests/test_tool_guard.py`.

## Bug corregido

```python
# mal: `or` se queda con el primer valor truthy y saltaba bloqueos válidos
return streak or retry or ALLOW

# bien: se comparan contra None explícitamente
if streak is not None:
    return streak
if retry is not None:
    return retry
return ALLOW
```

Los umbrales dependen de la fase: un paso de `synthesis` (redactar) tolera
menos exploración que uno de `construction`.

## CLI

```bash
python -m tools.tool_guard --report
```
