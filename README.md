# Llama Server Manager

Gestor de escritorio para **llama.cpp** con interfaz wxPython: lanza y controla `llama-server`, configura modelos GGUF (incluida multimodalidad con `mmproj`), y añade un asistente agéntico completo con voz (TTS/STT), memoria, RAG, automatizaciones y agentes especializados por área.

## Estructura del repositorio

```
Llama Server Manager/
├── Llama_Server_Manager.py   # Aplicación principal (GUI wxPython + orquestación)
├── requirements.txt          # Dependencias Python con versiones fijadas
├── compilador.txt            # Comando PyInstaller para compilar el .exe
├── tools/                    # Módulos del backend del asistente (Python)
├── app_data/                 # Datos de aplicación (NO versionada: solo estructura + READMEs)
└── llama-server-ui/          # WebUI nativa de llama.cpp (ggml), forkeada y adaptada
```

## Componentes

### `Llama_Server_Manager.py`
Aplicación principal. Proporciona la GUI para administrar instancias de `llama-server`, seleccionar modelos en `app_data/models`, ajustar parámetros de inferencia y gestionar los servicios auxiliares del asistente.

### `tools/`
Backend modular del asistente, importado por la app principal y reutilizable por los servidores web incluidos:

| Módulo | Función |
|---|---|
| `agent_core.py`, `dev_agent.py` | Núcleo del agente y agente de desarrollo |
| `agency_catalog.py` | Catálogo de agentes de `app_data/agency-agents/` |
| `memory_manager.py`, `episodic_memory.py`, `memory_skills.py` | Memoria conversacional y episódica (ChromaDB) |
| `rag_web_server.py` | RAG y búsqueda semántica (embeddings + NLTK) |
| `piper_service.py` | Voz: TTS con Piper y STT con faster-whisper |
| `mcp_server.py`, `mcp_client.py` | Servidor y cliente MCP (Model Context Protocol) |
| `config_server.py`, `web_manager_server.py`, `workspace_server.py` | Servidores web de configuración y workspace |
| `automation_dispatcher.py`, `cron_dispatcher.py`, `filewatch_dispatcher.py`, `webhook_dispatcher.py` | Automatizaciones (cron, vigilancia de archivos, webhooks) |
| `scheduled_chats.py`, `scheduler_service.py` | Chats programados y planificación |
| `telegram_service.py`, `imap_listener.py`, `imap_sender.py`, `notifications.py` | Telegram, correo IMAP y notificaciones |
| `process_manager.py`, `job_manager.py` | Gestión de procesos de llama.cpp y trabajos |
| `laya_downloader.py`, `laya_onnx_router.py` | Router semántico Laya (ONNX) |
| `skill_learner.py`, `meta_learner.py`, `procedures.py`, `goals.py` | Aprendizaje de skills, metas y procedimientos |
| `runtime.py`, `logger.py`, `env_detector.py`, `ipc.py` | Infraestructura común (rutas, logs, entorno) |

### `app_data/`
Almacén de datos de aplicación: binarios de llama.cpp (CPU y CUDA), modelos GGUF, proyectores multimodales, modelo de embeddings, datos de NLTK, voces TTS y modelos STT, runtime WebView2, FFmpeg portable y el modelo router Laya.

**No se versiona su contenido** (binarios y modelos, demasiado pesados). El repositorio conserva únicamente la estructura de carpetas, con un `README.md` dentro de cada una que explica qué debe contener. Al instalar o ejecutar la aplicación, las carpetas se rellenan con los recursos reales (descargados por los módulos de `tools/` o colocados manualmente).

### `llama-server-ui/`
WebUI nativa de llama.cpp (proyecto de ggml), **forkeada y adaptada**: aplicación SvelteKit + TypeScript con soporte PWA.

Características del stack:
- SvelteKit + Vite + TypeScript
- PWA (vite-plugin-pwa, con iconos y splash screens)
- Componentes shadcn-svelte + TailwindCSS
- Tests con Vitest y Playwright

Incluye su propio `README.md` con la documentación original de la UI y su `.gitignore`.

**Desarrollo:**
```bash
cd llama-server-ui
npm install
npm run dev      # servidor de desarrollo
npm run build    # build de producción
```

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
  --collect-all "fastapi" --hidden-import "chromadb" --hidden-import "flask" --hidden-import "fastmcp" \
  --hidden-import "piper" --hidden-import "faster_whisper" Llama_Server_Manager.py
```

## Notas

- `app_data/llama-cpp/` y `app_data/llama-cuda/` deben contener los binarios de llama.cpp (`llama-server.exe` + DLLs `ggml-*`); el manager detecta y usa la variante disponible (CPU o CUDA).
- Los modelos GGUF no se incluyen: colócalos en `app_data/models/` (y los proyectores `mmproj-*.gguf` en `app_data/mmproj/`).
- La configuración local del agente (`app_data/mcp.json`, personalidades) se genera en la instalación y no se publica.
