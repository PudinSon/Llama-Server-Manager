# `tools/agency_domains.py` — Agentes por dominio

Decide qué agentes del catálogo encajan con los dominios detectados por
`task_router`.

## El problema de partida

`app_data/agency-agents/` tiene **295 `.md` en 27 carpetas**, pero solo **273
se cargan** en el catálogo (los 22 de `strategy/` y `examples/` no tienen
frontmatter). El catálogo guarda `category` como la **carpeta inmediata**
(`unity`), no el área real (`game-development`), así que no basta para asignar
dominios.

El mapeo por palabras no puede usar `description`: es una plantilla
(`ES: Agente local de <Área> para <Nombre>…`) y produciría ruido. Usa
**slug + name + vibe**, tokenizados.

## Vocabulario

| Constante | Qué es |
|---|---|
| `ALL_DOMAINS` | Los dominios de `task_router.DOMAINS`. |
| `CATEGORY_DOMAINS` | Carpeta del catálogo → dominios (17 áreas). |
| `UNIVERSAL_CATEGORIES` | `("strategy", "examples")`: aplican a todo. |
| `UNIVERSAL_AGENTS` | 17 slugs que encajan en cualquier dominio. |
| `KEYWORD_DOMAINS` | Palabras del nombre/vibe → dominio. |
| `MIN_POOL = 15` | Mínimo de agentes antes de dejar de recortar. |

## `DomainMap` y `select`

```
DomainMap.domains_for(agent)   →  dominios declarados/derivados del agente
DomainMap.is_universal(agent)  →  ¿es universal?
DomainMap.category_of(agent)   →  carpeta del catálogo

select(agents, domains, limit) →  agentes que encajan, con respaldo
```

`select` nunca devuelve una lista vacía: si el filtro deja menos de `MIN_POOL`,
relaja hasta el catálogo completo. Un dominio desconocido no rompe la selección.

## Override `domains.json`

En la raíz del proyecto se puede colocar un `domains.json` para reemplazar el
mapa por defecto sin tocar el código:

```json
{
  "categories": { "unity": ["coding", "creative"] },
  "universal_agents": ["game-designer"],
  "keywords": { "shader": ["creative"] }
}
```

`override_path(root)` / `_load_override(root)` lo leen y `reset_cache()` limpia
la caché (útil en tests). El mapa por defecto **es código versionado** porque
los 295 `.md` de agentes no lo están.

## Lección aprendida

La primera versión usaba *substring* y producía ruido
(`unity` ⊂ *community*, `api` ⊂ *capital*). Con **tokens** y prefijos
(`"token"` exacto, `"token*"` prefijo, `"frase"` literal) el filtro de `coding`
pasó de 165 a 130 agentes.

## CLI

```bash
python -m tools.agency_domains --report
python -m tools.agency_domains --agent "Developer Tooling Engineer"
```

`--report` imprime el reparto de agentes por dominio; `--agent` el área y los
dominios derivados de un agente concreto.
