# app_data

Almacén de datos de aplicación de **Llama Server Manager**. Aquí viven los recursos pesados y generados que no se versionan en este repositorio: binarios de llama.cpp, modelos GGUF, datos de NLTK, voces TTS, runtimes embebidos y configuración local.

Cada subcarpeta incluye un `README.md` que explica qué debe contener.

| Carpeta | Contenido |
|---|---|
| `agency-agents/` | Catálogo de agentes especializados (prompts en Markdown) por área |
| `dspark/` | Datos del componente DSpark |
| `edgewv2sdk/` | Runtime WebView2 (Edge) para vistas web embebidas |
| `embed/` | Modelo de embeddings GGUF para RAG/búsqueda semántica |
| `ffmpeg-local/` | Binarios portables de FFmpeg |
| `laya/` | Modelo ONNX del router semántico Laya (+ tokenizador) |
| `llama-cpp/` | Binarios de llama.cpp (build CPU) |
| `llama-cuda/` | Binarios de llama.cpp (build CUDA) |
| `mmproj/` | Proyectores multimodales GGUF (visión) |
| `models/` | Modelos principales del chat en formato GGUF |
| `nltk_data/` | Datos descargados de NLTK |
| `office/` | CLI de Office para procesar documentos |
| `piper/` | Voces TTS (Piper) y modelos STT (faster-whisper) |
| `skills/` | Skills del agente (una carpeta con `SKILL.md` por skill) |

La configuración local del agente (p. ej. `mcp.json`) y las configuraciones de personalidad también se guardan aquí en la instalación real, pero no se versionan.
