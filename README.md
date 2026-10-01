# Llama Server Manager

Gestor de escritorio para **llama.cpp** con interfaz **customtkinter (Tk)**: lanza
y controla `llama-server`, configura modelos GGUF (incluida multimodalidad con
`mmproj`), y añade un asistente agéntico completo con voz (TTS/STT), memoria,
RAG, automatizaciones 24/7 y agentes especializados por área.

El modo agente es **determinista**: el LLM solo genera *contenido*; clasificar la
petición, elegir agentes, construir el plan, impedir bucles, verificar artefactos
y persistir estado es código Python testeable. Ver
[`docs/agent/README.md`](docs/agent/README.md).

## Estructura del repositorio

```
Llama Server Manager/
├── Llama_Server_Manager.py   # Aplicación principal (GUI + orquestación + cierre limpio)
├── compilador.txt            # Comando PyInstaller para compilar el .exe
├── requirements.txt          # Dependencias Python con versiones fijadas
├── tools/                    # Módulos del backend del asistente (Python)
├── docs/                     # Documentación de la arquitectura agéntica y de automatización
├── app_data/                 # Datos de aplicación (NO versionada: solo estructura + READMEs)
└── llama-server-ui/          # WebUI nativa de llama.cpp (ggml), forkeada y adaptada
```

## Componentes

### `Llama_Server_Manager.py`

Aplicación principal. Proporciona la GUI para administrar instancias de
`llama-server`, seleccionar modelos en `app_data/models`, ajustar parámetros de
inferencia y gestionar los servicios auxiliares del asistente. Incluye el panel
web de administración (puerto 12000) y la secuencia de cierre que apaga todos
los servicios al salir (ver [Arranque y cierre](#arranque-y-cierre)).

### `tools/`

Backend modular del asistente, importado por la app principal y reutilizable por
los servidores web incluidos:

| Módulo | Función |
|---|---|
| `agent_core.py`, `dev_agent.py` | Núcleo del agente y agente de desarrollo (MCP) |
| `task_router.py` | Decide **CHAT/AGENCY**, la naturaleza y los dominios de la petición (determinista) |
| `pipeline_templates.py` | Construye el plan: fases, pasos, dependencias y recuperación |
| `agency_domains.py` | Mapea dominios detectados → agentes candidatos |
| `tool_guard.py` | Barrera anti-bucle y anti-exploración por fase |
| `tool_taxonomy.py`, `tool_policy.py` | Taxonomía única de herramientas y whitelist por agente |
| `agency_catalog.py` | Catálogo de agentes de `app_data/agency-agents/` |
| `agent_cli.py`, `daemon_agent.py` | CLI de control y agente demonio |
| `episodic_memory.py`, `memory_skills.py` | Memoria episódica y skills (ChromaDB) |
| `skill_learner.py`, `procedures.py`, `goals.py` | Aprendizaje de skills, procedimientos y metas |
| `rag_web_server.py` | RAG y búsqueda semántica (embeddings + NLTK) |
| `piper_service.py` | Voz: TTS con Piper y STT con faster-whisper |
| `mcp_server.py`, `mcp_client.py` | Servidor y cliente MCP (Model Context Protocol) |
| `config_server.py`, `web_manager_server.py`, `workspace_server.py`, `web_viewer.py` | Servidores web de configuración/administración/workspace y visor embebido |
| `automation_service.py`, `automation_process.py` | Automatización 24/7: servicio de cola + dispatchers (puerto 15001) y su supervisor ([`docs/automation_24_7.md`](docs/automation_24_7.md)) |
| `automation_dispatcher.py`, `cron_dispatcher.py`, `filewatch_dispatcher.py`, `webhook_dispatcher.py` | Fuentes de eventos (cron, vigilancia de archivos, webhooks) |
| `dispatcher_registry.py`, `dispatcher_control.py` | Registro y control de dispatchers |
| `event_queue.py`, `checkpoint_manager.py`, `parallel_executor.py` | Cola de eventos, checkpoints y ejecución paralela |
| `scheduler_service.py`, `scheduled_chats.py` | Planificación y chats programados |
| `telegram_service.py`, `imap_listener.py`, `imap_sender.py`, `gmail_listener.py`, `google_mail.py`, `notifications.py` | Telegram, correo (IMAP/Gmail) y notificaciones |
| `process_manager.py`, `job_manager.py`, `metrics.py`, `sensors.py` | Gestión de procesos de llama.cpp, trabajos, métricas y sensores |
| `laya_downloader.py`, `laya_onnx_router.py` | Router semántico Laya (ONNX) |
| `llama_params.py` | Parseo compartido de parámetros (mmproj, DSpark, MoE) entre manager y panel web |
| `runtime.py`, `logger.py`, `env_detector.py`, `ipc.py`, `auth.py`, `rate_limit.py`, `autocompletion.py`, `web_helper.py` | Infraestructura común (rutas, logs, entorno, IPC, auth) |

### `docs/`

- [`docs/agent/README.md`](docs/agent/README.md): arquitectura del modo agente
  determinista y sus cuatro módulos (`task_router`, `pipeline_templates`,
  `agency_domains`, `tool_guard`).
- [`docs/automation_24_7.md`](docs/automation_24_7.md): ciclo de vida, watchdog y
  API del servicio de automatización 24/7.

### `app_data/`

Almacén de datos de aplicación: binarios de llama.cpp (CPU y CUDA), modelos GGUF,
proyectores multimodales, modelo de embeddings, datos de NLTK, voces TTS y
modelos STT, runtime WebView2, FFmpeg portable, el modelo router Laya y el
catálogo de agentes (`app_data/agency-agents/`).

**No se versiona su contenido** (binarios y modelos, demasiado pesados). El
repositorio conserva únicamente la estructura de carpetas, con un `README.md`
dentro de cada una que explica qué debe contener. Al instalar o ejecutar la
aplicación, las carpetas se rellenan con los recursos reales (descargados por
los módulos de `tools/` o colocados manualmente).

### `llama-server-ui/`

WebUI nativa de llama.cpp (proyecto de ggml), **forkeada y adaptada**: aplicación
SvelteKit + TypeScript con soporte PWA.

Características del stack:
- SvelteKit + Vite + TypeScript
- PWA (vite-plugin-pwa, con iconos y splash screens)
- Componentes shadcn-svelte + TailwindCSS
- Tests con Vitest y Playwright

Incluye su propio `README.md` con la documentación de la UI (actualizada con las
adiciones del fork: panel de agente, panel de automatización y medidor de
contexto) y su `.gitignore`.

**Desarrollo:**
```bash
cd llama-server-ui
npm install
npm run dev      # servidor de desarrollo
npm run build    # build de producción
```

## Arranque y cierre

Al arrancar, el manager lanza sus servicios en orden y espera activamente a que
sus puertos respondan: config server (5001), Telegram, embeddings Nomic (8082),
`dev_agent` MCP (15000), workspace server (19000), automatización 24/7 (15001),
OfficeCLI, RAG (25000) y Piper (8880).

Al cerrarse —con el **botón de cerrar ventana**, con **Salir** en el menú de la
bandeja, con **CTRL_C/CTRL_CLOSE** o vía `atexit`— todos esos caminos convergen
en un único cierre idempotente:

1. detiene los procesos hijos en orden y con timeouts acotados,
2. libera los puertos usados (incluido el 15001 de la automatización),
3. para la bandeja y la GUI, y
4. garantiza la salida del **ejecutable principal** (`os._exit`) para no dejar
   ni procesos huérfanos ni el `.exe` colgado.

Un watchdog de cierre (60 s) asegura que, si alguna parada se atasca, el exe sale
igualmente. El servicio de automatización añade su propio watchdog
`--parent-pid`: si el manager muere de golpe, el servicio se cierra en ≤5 s
(parando antes sus backends). Detalles en
[`docs/automation_24_7.md`](docs/automation_24_7.md).

## Instalación (app Python)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
python Llama_Server_Manager.py
```

## Compilación del ejecutable

`compilador.txt` contiene el comando PyInstaller usado para generar el ejecutable:

```bash
pyinstaller --noconsole --onefile --icon=app.ico --add-data "app.ico;." --add-data "tools;tools" \
  --collect-all "wx" --collect-all "piper" --collect-all "fastmcp" --collect-all "chromadb" \
  --collect-all "fastapi" --hidden-import "chromadb" --hidden-import "flask" \
  --hidden-import "fastmcp"  --hidden-import "piper" --hidden-import "faster_whisper" \
  Llama_Server_Manager.py
```

El ejecutable resultante es multi-uso: abre la GUI por defecto y expone
subcomandos (`--service`, `--web-only`, `--run-mcp-server`, `--run-piper-server`,
`--run-rag-server`, `--run-workspace-server`, `--run-automation-service`,
`--run-web-viewer`, `--run-python-code`, `--run-python-script`, `--run-cli`) que
reutilizan los mismos módulos de `tools/`.

## Notas

- `app_data/llama-cpp/` y `app_data/llama-cuda/` deben contener los binarios de llama.cpp (`llama-server.exe` + DLLs `ggml-*`); el manager detecta y usa la variante disponible (CPU o CUDA).
- Los modelos GGUF no se incluyen: colócalos en `app_data/models/` (y los proyectores `mmproj-*.gguf` en `app_data/mmproj/`).
- La configuración local del agente (`app_data/mcp.json`, personalidades) se genera en la instalación y no se publica.
- El catálogo de agentes vive en `app_data/agency-agents/` (los `.md` de agentes no se versionan).
