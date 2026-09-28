try:
    from pathlib import Path
    import sys
    _THIS_DIR = Path(__file__).resolve().parent
    _PROJECT_ROOT = _THIS_DIR.parent
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
except Exception:
    pass

import os
import sys
import json
import logging
import threading
import time

logger = logging.getLogger(__name__)
from flask import Flask, render_template_string, request, jsonify, send_file, make_response
import chromadb
from chromadb.utils import embedding_functions
import requests
import csv
import shutil
import tempfile

# Importar runtime y configuraciones
try:
    import tools.runtime as runtime
except ImportError:
    runtime = None

# ========== IMPORTAR LIBRERÍAS DE EXTRACCIÓN ==========
try:
    from PyPDF2 import PdfReader
except ImportError:
    PdfReader = None
try:
    from docx import Document
except ImportError:
    Document = None
try:
    from pptx import Presentation
except ImportError:
    Presentation = None
try:
    from openpyxl import load_workbook
except ImportError:
    load_workbook = None
try:
    from striprtf.striprtf import rtf_to_text
except ImportError:
    rtf_to_text = None
try:
    from odf.opendocument import load as odf_load
    from odf.text import P
    from odf.table import Table, TableRow, TableCell
except ImportError:
    odf_load = None
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
try:
    import yaml
except ImportError:
    yaml = None

# Configuración de rutas
def get_base_dir():
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path.cwd()
    return base
try:
    import tools.runtime as runtime
except ImportError:
    runtime = None
if runtime is not None:
    try:
        USER_DATA_DIR = Path(runtime.get_user_data_dir())
    except Exception:
        USER_DATA_DIR = get_base_dir() / "user_data"
else:
    USER_DATA_DIR = get_base_dir() / "user_data"

USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_DB_DIR = USER_DATA_DIR / "chroma_db"
ACTIVE_LIB_FILE = USER_DATA_DIR / "active_library.json"

# Redirigir NLTK
NLTK_DATA_DIR = Path(runtime.get_data_dir()) / "nltk_data" if runtime else Path(get_base_dir()) / "app_data" / "nltk_data"
NLTK_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["NLTK_DATA"] = str(NLTK_DATA_DIR)

def get_active_library():
    if ACTIVE_LIB_FILE.exists():
        try:
            with open(ACTIVE_LIB_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("active_collection")
        except:
            return None
    return None

def set_active_library(name):
    with open(ACTIVE_LIB_FILE, "w", encoding="utf-8") as f:
        json.dump({"active_collection": name}, f)

# Embedding function personalizada
class NomicLlamaEmbeddingFunction(embedding_functions.EmbeddingFunction):
    def __init__(self, server_url: str = "http://localhost:8082", batch_size: int = 10, timeout: int = 120):
        self.server_url = server_url
        self.batch_size = batch_size
        self.timeout = timeout

    def __call__(self, input):
        prefixed = [f"search_document: {text}" for text in input]
        all_embeddings = []
        for i in range(0, len(prefixed), self.batch_size):
            batch = prefixed[i:i+self.batch_size]
            try:
                response = requests.post(
                    f"{self.server_url}/v1/embeddings",
                    json={"input": batch},
                    timeout=self.timeout
                )
                if response.status_code != 200:
                    raise Exception(f"Error HTTP {response.status_code}: {response.text}")
                data = response.json()
                all_embeddings.extend([item["embedding"] for item in data["data"]])
            except Exception as e:
                raise Exception(f"Fallo en lote {i//self.batch_size + 1}: {str(e)}")
        return all_embeddings

# Inicializar ChromaDB
_chroma_client = None
_embed_fn = None

def get_chroma_client():
    global _chroma_client, _embed_fn
    if _chroma_client is None:
        # Dense embedding function (la que ya tienes)
        _embed_fn = NomicLlamaEmbeddingFunction(server_url="http://localhost:8082")
        # Sparse embedding function (BM25)
        try:
            # ChromaDB 0.5.0+ incluye BM25SparseEmbeddingFunction
            from chromadb.utils.embedding_functions import BM25SparseEmbeddingFunction
            _sparse_fn = BM25SparseEmbeddingFunction()
        except ImportError:
            # Fallback: usar una implementación simple basada en TF-IDF
            # (opcional, pero aquí asumimos que existe)
            _sparse_fn = None
            print("⚠️ BM25SparseEmbeddingFunction no disponible. La búsqueda híbrida no funcionará.")

        try:
            CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
        except Exception:
            try:
                _chroma_client = chromadb.PersistentClient(path="./chroma_db")
            except Exception:
                _chroma_client = None

        # Guardar la función sparse en la variable global para usarla al crear colecciones
        _chroma_client._sparse_fn = _sparse_fn

    return _chroma_client

# Crear app Flask
app = Flask(__name__)

# Detrás de nginx: confiar en X-Forwarded-* para URLs y host correctos.
try:
    try:
        from .auth import apply_proxy_fix
    except ImportError:
        from tools.auth import apply_proxy_fix
    apply_proxy_fix(app)
except Exception as _proxy_err:  # pragma: no cover
    logger.warning(f"ProxyFix no aplicado: {_proxy_err}")

# ============================================================
# CORS (permitir que la WebUI en localhost:8081 llame esta API)
# ============================================================
@app.after_request
def add_cors_headers(response):
    """Permite llamadas desde la WebUI local (CORS para navegador)."""
    origin = request.headers.get("Origin", "")
    if origin in {"http://localhost:8081", "http://127.0.0.1:8081", "http://localhost:5173"}:
        response.headers["Access-Control-Allow-Origin"] = origin
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Vary"] = "Origin"
    return response

@app.route("/api/docs/delete", methods=["POST", "OPTIONS"])
def delete_doc():
    """Borra todos los chunks de un documento (fuente) de la biblioteca activa."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.json or {}
    file_name = data.get("file_name") or data.get("fuente")
    if not file_name:
        return jsonify({'success': False, 'error': 'file_name requerido'})
    active = get_active_library()
    if not active:
        return jsonify({'success':False,'error':'No hay biblioteca activa'})
    client = get_chroma_client()
    if client is None:
        return jsonify({'success':False,'error':'ChromaDB no disponible'})
    try:
        col = client.get_collection(name=active, embedding_function=_embed_fn)
        col.delete(where={"fuente": file_name})
        return jsonify({'success': True, 'file_name': file_name})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# ============================================================
# FUNCIÓN DE EXTRACCIÓN DE TEXTO (copiada de rag_mcp.py)
# ============================================================
def extract_text_from_file(file_path, filename):
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext == ".pdf":
            if not PdfReader:
                return None, "PyPDF2 no instalado"
            reader = PdfReader(file_path)
            text = "".join(page.extract_text() for page in reader.pages)
            return text, None
        elif ext == ".docx":
            if not Document:
                return None, "python-docx no instalado"
            doc = Document(file_path)
            text = "\n".join(p.text for p in doc.paragraphs)
            return text, None
        elif ext == ".pptx":
            if not Presentation:
                return None, "python-pptx no instalado"
            prs = Presentation(file_path)
            text = ""
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text += shape.text + "\n"
            return text, None
        elif ext in (".xlsx", ".xls"):
            if not load_workbook:
                return None, "openpyxl no instalado"
            wb = load_workbook(file_path, read_only=True, data_only=True)
            text = ""
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    for cell in row:
                        if cell:
                            text += str(cell) + " "
                    text += "\n"
            return text, None
        elif ext == ".rtf":
            if not rtf_to_text:
                return None, "striprtf no instalado"
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return rtf_to_text(f.read()), None
        elif ext in (".odt", ".odp", ".ods"):
            if not odf_load:
                return None, "odfpy no instalado"
            odf_doc = odf_load(file_path)
            text = ""
            for para in odf_doc.getElementsByType(P):
                text += str(para) + "\n"
            for table in odf_doc.getElementsByType(Table):
                for row in table.getElementsByType(TableRow):
                    for cell in row.getElementsByType(TableCell):
                        text += str(cell) + " "
                    text += "\n"
            return text, None
        elif ext in (".html", ".htm"):
            if not BeautifulSoup:
                return None, "beautifulsoup4 no instalado"
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
            return soup.get_text(), None
        elif ext == ".xml":
            if not BeautifulSoup:
                return None, "beautifulsoup4 no instalado"
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                soup = BeautifulSoup(f.read(), "xml")
            return soup.get_text(), None
        elif ext == ".md":
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read(), None
        elif ext == ".txt":
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read(), None
        elif ext == ".csv":
            text = ""
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for row in csv.reader(f):
                    text += " ".join(row) + "\n"
            return text, None
        elif ext == ".tsv":
            text = ""
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for row in csv.reader(f, delimiter="\t"):
                    text += " ".join(row) + "\n"
            return text, None
        elif ext == ".json":
            with open(file_path, "r", encoding="utf-8") as f:
                return json.dumps(json.load(f), indent=2), None
        elif ext in (".yaml", ".yml"):
            if not yaml:
                return None, "pyyaml no instalado"
            with open(file_path, "r", encoding="utf-8") as f:
                return yaml.dump(yaml.safe_load(f), allow_unicode=True), None
        else:
            return None, f"Formato no soportado: {ext}"
    except Exception as e:
        return None, f"Error al leer: {str(e)}"

# Plantilla HTML para RAG Web - CON BARRA DE ESTADO, LISTA DE DOCS Y BOTÓN X
RAG_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📚 Gestor RAG</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0d1117; 
            color: #c9d1d9;
            padding: 20px;
            height: 100vh;
            overflow-y: auto;
        }
        .container { max-width: 900px; margin: 0 auto; }
        
        /* Barra superior con botón de retorno */
        .header-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            background: #161b22;
            border-radius: 12px 12px 0 0;
            border-bottom: 1px solid #30363d;
            margin-bottom: 20px;
        }
        .header-bar h1 { 
            font-size: 20px; 
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 0;
        }
        .btn-back {
            background: #21262d;
            color: #c9d1d9;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 8px 16px;
            font-size: 14px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .btn-back:hover {
            background: #30363d;
            border-color: #58a6ff;
        }
        
        .card {
            background: #161b22;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
            border: 1px solid #30363d;
        }
        .card h2 { font-size: 16px; margin-bottom: 12px; color: #58a6ff; }
        .flex { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .flex-1 { flex: 1; min-width: 200px; }
        .flex-2 { flex: 2; min-width: 300px; }
        input, select, button {
            background: #0d1117;
            color: #c9d1d9;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 10px 14px;
            font-size: 14px;
            outline: none;
        }
        input:focus, select:focus { border-color: #58a6ff; }
        input[type="file"] {
            padding: 8px;
            background: #161b22;
            border: 1px dashed #30363d;
        }
        input[type="file"]:hover {
            border-color: #58a6ff;
        }
        button {
            background: #238636;
            color: white;
            border: none;
            cursor: pointer;
            font-weight: 600;
            transition: background 0.2s;
            width: auto;
            padding: 10px 20px;
            white-space: nowrap;
        }
        button:hover { background: #2ea043; }
        button.danger { background: #da3633; }
        button.danger:hover { background: #f85149; }
        button.secondary { background: #21262d; }
        button.secondary:hover { background: #30363d; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .list-item {
            padding: 8px 12px;
            background: #0d1117;
            border-radius: 6px;
            margin-bottom: 4px;
            border-left: 3px solid #58a6ff;
            font-size: 13px;
        }
        .log-area {
            background: #0d1117;
            border-radius: 8px;
            padding: 12px;
            max-height: 200px;
            overflow-y: auto;
            font-family: 'Consolas', monospace;
            font-size: 12px;
            border: 1px solid #30363d;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .log-area .info { color: #58a6ff; }
        .log-area .success { color: #3fb950; }
        .log-area .warning { color: #d29922; }
        .log-area .error { color: #f85149; }
        .badge {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 12px;
            font-size: 12px;
            background: #21262d;
        }
        .badge.active { background: #238636; color: white; }
        .badge.warning { background: #d29922; color: white; }
        .status-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0;
            font-size: 13px;
            color: #8b949e;
            border-bottom: 1px solid #30363d;
            margin-bottom: 16px;
            flex-wrap: wrap;
            gap: 8px;
        }
        .file-list { 
            max-height: 150px; 
            overflow-y: auto; 
            margin-top: 8px;
        }
        .file-list .file-item {
            padding: 4px 8px;
            font-size: 12px;
            color: #8b949e;
            border-bottom: 1px solid #21262d;
        }
        .file-list .file-item .icon { margin-right: 6px; }
        .stats {
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
            font-size: 13px;
            color: #8b949e;
        }
        .stats span { 
            display: flex;
            align-items: center;
            gap: 4px;
        }
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #0d1117; }
        ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
        .progress-bar {
            width: 100%;
            height: 4px;
            background: #21262d;
            border-radius: 2px;
            margin-top: 8px;
            overflow: hidden;
        }
        .progress-bar .fill {
            height: 100%;
            background: #58a6ff;
            width: 0%;
            transition: width 0.3s;
        }
        .hidden { display: none; }
        .text-muted { color: #8b949e; font-size: 13px; }
        .text-center { text-align: center; }
        .mt-8 { margin-top: 8px; }
        .mb-8 { margin-bottom: 8px; }
        .gap-4 { gap: 4px; }
        .flex-wrap { flex-wrap: wrap; }
        /* Botón X más visible */
        .btn-close-rag {
            background: #21262d !important;
            color: #f85149 !important;
            border: 1px solid #f85149 !important;
            border-radius: 6px !important;
            font-size: 20px !important;
            font-weight: bold !important;
            padding: 2px 12px !important;
            cursor: pointer !important;
            transition: all 0.2s !important;
        }
        .btn-close-rag:hover {
            background: #f85149 !important;
            color: #ffffff !important;
        }
        /* Lista de documentos indexados */
        .doc-list {
            max-height: 200px;
            overflow-y: auto;
            background: #0d1117;
            border-radius: 6px;
            padding: 4px;
            border: 1px solid #30363d;
        }
        .doc-list .doc-item {
            padding: 6px 10px;
            cursor: pointer;
            border-radius: 4px;
            border-left: 3px solid transparent;
            transition: all 0.2s;
            font-size: 13px;
        }
        .doc-list .doc-item:hover {
            background: #161b22;
            border-left-color: #58a6ff;
        }
        .doc-list .doc-item.selected {
            background: #1c2333;
            border-left-color: #3fb950;
        }
        .doc-preview {
            background: #0d1117;
            border-radius: 6px;
            padding: 10px;
            max-height: 250px;
            overflow-y: auto;
            border: 1px solid #30363d;
            font-family: 'Consolas', monospace;
            font-size: 12px;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .doc-preview .chunk-sep {
            color: #8b949e;
            border-top: 1px dashed #30363d;
            margin: 6px 0;
            padding-top: 6px;
        }
        .doc-preview .chunk-header {
            color: #58a6ff;
            font-weight: bold;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Barra superior con botón de cierre -->
        <div class="header-bar" style="display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; background: #161b22; border-radius: 12px 12px 0 0; border-bottom: 1px solid #30363d; margin-bottom: 20px;">
            <h1 style="font-size: 20px; display: flex; align-items: center; gap: 10px; margin: 0;">📚 Gestor de Bibliotecas RAG</h1>
            <button id="rag-close-btn" onclick="closeSplit()" title="Cerrar RAG" class="btn-close-rag">
                ✕
            </button>
        </div>

        <!-- BARRA DE ESTADO -->
        <div class="status-bar">
            <span>🧠 Embeddings: <span id="embed-status" style="color:#d29922;">Verificando...</span></span>
            <span>📚 Biblioteca: <span id="active-library-name">Ninguna</span></span>
            <span>📄 Documentos: <span id="doc-count">0</span></span>
            <span>🧩 Fragmentos: <span id="chunk-count">0</span></span>
            <span>🔄 <span id="lib-count">0</span> bibliotecas</span>
        </div>

        <!-- Biblioteca -->
        <div class="card">
            <h2>📂 Biblioteca Activa</h2>
            <div class="flex">
                <div class="flex-1">
                    <select id="library-select" onchange="changeLibrary()">
                        <option value="">Seleccionar biblioteca...</option>
                    </select>
                </div>
                <button onclick="newLibrary()">➕ Nueva</button>
                <button class="danger" onclick="deleteLibrary()">🗑️ Eliminar</button>
                <button class="secondary" onclick="refreshLibraryList()">🔄 Actualizar</button>
            </div>
            <div style="margin-top: 8px;">
                <span class="badge" id="active-badge">Ninguna</span>
            </div>
        </div>

        <!-- Indexación -->
        <div class="card">
            <h2>📄 Indexar Documentos</h2>
            <div class="flex flex-wrap" style="gap: 10px;">
                <div class="flex-2">
                    <input type="file" id="file-input" multiple accept=".pdf,.docx,.pptx,.xlsx,.rtf,.odt,.odp,.ods,.html,.xml,.md,.txt,.csv,.tsv,.json,.yaml,.yml" 
                           onchange="updateFileList()" style="width: 100%;">
                </div>
                <button class="secondary" onclick="document.getElementById('file-input').click()">📂 Seleccionar archivos</button>
                <button onclick="selectFolder()">📁 Seleccionar carpeta</button>
                <button onclick="indexFiles()" id="index-btn" disabled>🚀 Indexar</button>
                <button class="secondary" onclick="refreshDocs()">🔄 Actualizar</button>
            </div>
            <div style="margin-top: 8px;">
                <span class="text-muted">Formatos soportados: PDF, DOCX, PPTX, XLSX, RTF, ODT, ODP, ODS, HTML, XML, MD, TXT, CSV, TSV, JSON, YAML</span>
            </div>
            <div id="file-list" class="file-list"></div>
            <div class="progress-bar hidden" id="progress-container">
                <div class="fill" id="progress-fill"></div>
            </div>
        </div>

        <!-- Documentos Indexados (nuevo) -->
        <div class="card">
            <h2>📋 Documentos Indexados</h2>
            <div style="display: flex; gap: 16px; flex-wrap: wrap;">
                <div style="flex: 1; min-width: 200px;">
                    <div id="doc-list-container" class="doc-list">
                        <div class="text-muted" style="padding: 8px;">No hay documentos</div>
                    </div>
                </div>
                <div style="flex: 2; min-width: 300px;">
                    <div id="doc-preview-container" class="doc-preview">
                        <span class="text-muted">Selecciona un documento para ver su contenido</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Búsqueda -->
        <div class="card">
            <h2>🔍 Buscar en Biblioteca</h2>
            <div class="flex">
                <div class="flex-1">
                    <input type="text" id="search-input" placeholder="Escribe tu consulta..." onkeydown="if(event.key==='Enter') search()">
                </div>
                <button onclick="search()">🔎 Buscar</button>
                <span class="text-muted" style="padding: 10px;">Top <input type="number" id="search-limit" value="10" min="1" max="50" style="width:50px;padding:4px;text-align:center;"> resultados</span>
            </div>
            <div id="search-results" style="margin-top: 12px;"></div>
        </div>

        <!-- Logs -->
        <div class="card">
            <h2>📋 Logs</h2>
            <div id="log-area" class="log-area"></div>
        </div>
    </div>

    <script>
        let selectedFiles = [];
        let currentDocs = []; // para almacenar los documentos cargados

        // ============================================================
        // LOGS Y FUNCIONES RAG
        // ============================================================
        function log(message, type = 'info') {
            const area = document.getElementById('log-area');
            const entry = document.createElement('div');
            entry.className = type;
            entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
            area.appendChild(entry);
            area.scrollTop = area.scrollHeight;
        }

        function updateFileList() {
            const input = document.getElementById('file-input');
            const files = Array.from(input.files || []);
            selectedFiles = files.map(f => f.name);
            const list = document.getElementById('file-list');
            if (selectedFiles.length === 0) {
                list.innerHTML = '<div class="text-muted">No hay archivos seleccionados</div>';
                document.getElementById('index-btn').disabled = true;
                return;
            }
            const groups = {};
            files.forEach(f => {
                const ext = f.name.split('.').pop().toUpperCase();
                if (!groups[ext]) groups[ext] = [];
                groups[ext].push(f.name);
            });
            let html = '';
            for (const [ext, names] of Object.entries(groups)) {
                html += `<div class="file-item"><span class="icon">📄</span> [${ext}] ${names.length} archivo${names.length > 1 ? 's' : ''}</div>`;
            }
            html += `<div class="file-item text-muted">Total: ${files.length} archivos (${(files.reduce((s, f) => s + f.size, 0) / 1024 / 1024).toFixed(2)} MB)</div>`;
            list.innerHTML = html;
            document.getElementById('index-btn').disabled = false;
            log(`📂 ${files.length} archivos seleccionados (${Object.keys(groups).length} formatos)`, 'info');
        }

        function selectFolder() {
            const input = document.getElementById('file-input');
            input.setAttribute('webkitdirectory', '');
            input.removeAttribute('multiple');
            input.click();
            input.removeAttribute('webkitdirectory');
            input.setAttribute('multiple', 'multiple');
        }

        function refreshLibraryList() {
            fetch('/api/libraries')
                .then(r => r.json())
                .then(data => {
                    const select = document.getElementById('library-select');
                    select.innerHTML = '<option value="">Seleccionar biblioteca...</option>';
                    data.libraries.forEach(lib => {
                        const opt = document.createElement('option');
                        opt.value = lib;
                        opt.textContent = lib;
                        select.appendChild(opt);
                    });
                    document.getElementById('lib-count').textContent = data.libraries.length;
                    
                    fetch('/api/active')
                        .then(r => r.json())
                        .then(d => {
                            document.getElementById('active-library-name').textContent = d.active || 'Ninguna';
                            if (d.active) {
                                select.value = d.active;
                                document.getElementById('active-badge').textContent = `📚 ${d.active}`;
                                document.getElementById('active-badge').className = 'badge active';
                                refreshDocCount();
                                loadDocsList();
                            } else {
                                document.getElementById('active-badge').textContent = 'Ninguna';
                                document.getElementById('active-badge').className = 'badge';
                                document.getElementById('doc-count').textContent = '0';
                                document.getElementById('chunk-count').textContent = '0';
                                document.getElementById('doc-list-container').innerHTML = '<div class="text-muted" style="padding:8px;">No hay documentos</div>';
                                document.getElementById('doc-preview-container').innerHTML = '<span class="text-muted">Selecciona un documento para ver su contenido</span>';
                            }
                        });
                });
        }

        function changeLibrary() {
            const name = document.getElementById('library-select').value;
            if (!name) return;
            fetch('/api/active', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('active-badge').textContent = `📚 ${name}`;
                    document.getElementById('active-badge').className = 'badge active';
                    document.getElementById('active-library-name').textContent = name;
                    log(`✅ Biblioteca activa: ${name}`, 'success');
                    refreshDocCount();
                    loadDocsList();
                }
            });
        }

        function newLibrary() {
            const name = prompt('Nombre de la nueva biblioteca:');
            if (!name) return;
            fetch('/api/libraries', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    log(`✅ Biblioteca "${name}" creada`, 'success');
                    refreshLibraryList();
                } else {
                    log(`❌ Error: ${data.error}`, 'error');
                }
            });
        }

        function deleteLibrary() {
            const name = document.getElementById('library-select').value;
            if (!name) return alert('Selecciona una biblioteca');
            if (!confirm(`¿Eliminar "${name}" y todos sus documentos?`)) return;
            fetch('/api/libraries', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    log(`🗑️ Biblioteca "${name}" eliminada`, 'warning');
                    refreshLibraryList();
                }
            });
        }

        function refreshDocCount() {
            fetch('/api/docs/count')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('doc-count').textContent = data.count || 0;
                    document.getElementById('chunk-count').textContent = data.chunks || 0;
                });
        }

        // ============================================================
        // LISTA DE DOCUMENTOS INDEXADOS (nuevo)
        // ============================================================
        function loadDocsList() {
            fetch('/api/docs/list')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        log(`❌ Error cargando documentos: ${data.error}`, 'error');
                        return;
                    }
                    currentDocs = data.documents || [];
                    const container = document.getElementById('doc-list-container');
                    if (currentDocs.length === 0) {
                        container.innerHTML = '<div class="text-muted" style="padding:8px;">No hay documentos</div>';
                        document.getElementById('doc-preview-container').innerHTML = '<span class="text-muted">Selecciona un documento para ver su contenido</span>';
                        return;
                    }
                    let html = '';
                    currentDocs.forEach((doc, idx) => {
                        const label = `${doc.fuente} (${doc.chunks.length} fragmentos)`;
                        html += `<div class="doc-item" data-index="${idx}" onclick="selectDocument(${idx})">${label}</div>`;
                    });
                    container.innerHTML = html;
                    // Seleccionar el primero por defecto
                    if (currentDocs.length > 0) {
                        selectDocument(0);
                    }
                })
                .catch(err => {
                    log(`❌ Error: ${err}`, 'error');
                });
        }

        function selectDocument(index) {
            const doc = currentDocs[index];
            if (!doc) return;
            // Resaltar elemento seleccionado
            const items = document.querySelectorAll('.doc-item');
            items.forEach((el, i) => {
                el.classList.toggle('selected', i === index);
            });
            // Mostrar vista previa
            const preview = document.getElementById('doc-preview-container');
            let html = `<div class="chunk-header">📄 ${doc.fuente}</div><br>`;
            doc.chunks.forEach((chunk, i) => {
                html += `<div class="chunk-sep">Fragmento ${i+1}</div>`;
                html += `<div>${chunk.texto}</div>`;
            });
            preview.innerHTML = html;
        }

        function showProgress(show) {
            const container = document.getElementById('progress-container');
            if (show) {
                container.classList.remove('hidden');
            } else {
                container.classList.add('hidden');
            }
        }

        function updateProgress(value) {
            document.getElementById('progress-fill').style.width = value + '%';
        }

        function indexFiles() {
            const name = document.getElementById('library-select').value;
            if (!name) return alert('Selecciona una biblioteca');
            
            const input = document.getElementById('file-input');
            const files = Array.from(input.files || []);
            if (files.length === 0) return alert('Selecciona archivos o una carpeta');

            log(`🚀 Indexando ${files.length} archivos en "${name}"...`, 'info');
            showProgress(true);
            updateProgress(10);
            document.getElementById('index-btn').disabled = true;
            
            const formData = new FormData();
            files.forEach(f => formData.append('files', f));
            formData.append('library', name);

            fetch('/api/index', {
                method: 'POST',
                body: formData
            })
            .then(r => r.json())
            .then(data => {
                updateProgress(100);
                setTimeout(() => showProgress(false), 500);
                document.getElementById('index-btn').disabled = false;
                if (data.success) {
                    log(`✅ ${data.indexed} archivos indexados (${data.chunks} fragmentos)`, 'success');
                    refreshDocCount();
                    loadDocsList();  // actualizar lista
                    document.getElementById('file-list').innerHTML = '<div class="text-muted">Indexación completada ✅</div>';
                    document.getElementById('file-input').value = '';
                    selectedFiles = [];
                } else {
                    log(`❌ Error: ${data.error}`, 'error');
                }
            })
            .catch(err => {
                showProgress(false);
                document.getElementById('index-btn').disabled = false;
                log(`❌ Error: ${err}`, 'error');
            });
        }

        function search() {
            const query = document.getElementById('search-input').value;
            if (!query) return;
            const limit = parseInt(document.getElementById('search-limit').value) || 10;
            log(`🔍 Buscando: "${query}" (${limit} resultados)`, 'info');
            
            const resultsDiv = document.getElementById('search-results');
            resultsDiv.innerHTML = '<div style="color:#8b949e;">Buscando...</div>';

            fetch('/api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query, n_results: limit })
            })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    resultsDiv.innerHTML = `<div style="color:#f85149;">❌ ${data.error}</div>`;
                    log(`❌ Error: ${data.error}`, 'error');
                    return;
                }
                if (data.results && data.results.length > 0) {
                    resultsDiv.innerHTML = data.results.map((r, i) => `
                        <div class="list-item" style="border-left-color: ${i === 0 ? '#3fb950' : '#58a6ff'};">
                            <div style="display:flex;justify-content:space-between;align-items:center;">
                                <strong>📄 ${r.fuente}</strong>
                                <span style="font-size:11px;color:#8b949e;">#${r.chunk || '?'}</span>
                            </div>
                            <div style="font-size:12px;color:#8b949e;margin-top:4px;">${r.text.substring(0, 400)}${r.text.length > 400 ? '...' : ''}</div>
                            ${r.score ? `<div style="font-size:11px;color:#8b949e;margin-top:4px;">🎯 Score: ${r.score.toFixed(4)}</div>` : ''}
                        </div>
                    `).join('');
                    log(`✅ ${data.results.length} resultados encontrados`, 'success');
                } else {
                    resultsDiv.innerHTML = '<div style="color:#8b949e;">Sin resultados</div>';
                    log('Sin resultados', 'warning');
                }
            });
        }

        function refreshDocs() {
            refreshDocCount();
            loadDocsList();
            log('🔄 Documentos actualizados', 'info');
        }

        function checkEmbedStatus() {
            fetch('/api/embed-status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('embed-status').textContent = data.status;
                    document.getElementById('embed-status').style.color = data.available ? '#3fb950' : '#f85149';
                });
        }

        // Inicializar
        refreshLibraryList();
        checkEmbedStatus();
        setInterval(checkEmbedStatus, 10000);
        setInterval(refreshDocCount, 5000);
        log('🚀 Gestor RAG listo', 'success');
        log('💡 Haz clic en "✕" para cerrar RAG', 'info');
        
        function closeSplit() {
            console.log('[RAG] ✕ Cerrando split...');
            window.location.href = 'http://localhost:8081/rag_toggle';
        }
    </script>
</body>
</html>
'''

# ===================== RUTAS DE LA API =====================

@app.route('/')
def index():
    return render_template_string(RAG_HTML)

@app.route('/api/libraries', methods=['POST'])
def create_library():
    data = request.json
    name = data.get('name')
    if not name:
        return jsonify({'success': False, 'error': 'Nombre requerido'})
    client = get_chroma_client()
    if client is None:
        return jsonify({'success': False, 'error': 'ChromaDB no disponible'})
    try:
        # Obtener la función sparse (si está disponible)
        sparse_fn = getattr(client, '_sparse_fn', None)
        if sparse_fn is not None:
            # Crear colección con soporte híbrido
            collection = client.create_collection(
                name=name,
                embedding_function=_embed_fn,
                sparse_embedding_function=sparse_fn
            )
        else:
            # Crear colección solo con dense
            collection = client.create_collection(name=name, embedding_function=_embed_fn)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/libraries', methods=['DELETE'])
def delete_library():
    data = request.json
    name = data.get('name')
    if not name:
        return jsonify({'success': False, 'error': 'Nombre requerido'})
    client = get_chroma_client()
    if client is None:
        return jsonify({'success': False, 'error': 'ChromaDB no disponible'})
    try:
        client.delete_collection(name)
        if get_active_library() == name:
            set_active_library("")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    
@app.route('/api/libraries', methods=['GET'])
def get_libraries():
    """Devuelve la lista de bibliotecas (colecciones) existentes en ChromaDB."""
    client = get_chroma_client()
    if client is None:
        return jsonify({'libraries': [], 'error': 'ChromaDB no disponible'})
    try:
        collections = [col.name for col in client.list_collections()]
        return jsonify({'libraries': collections})
    except Exception as e:
        return jsonify({'libraries': [], 'error': str(e)})

@app.route('/api/active', methods=['GET'])
def get_active():
    active = get_active_library()
    return jsonify({'active': active})

@app.route('/api/active', methods=['POST'])
def set_active():
    data = request.json
    name = data.get('name')
    if not name:
        return jsonify({'success': False, 'error': 'Nombre requerido'})
    try:
        set_active_library(name)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/docs/count')
def get_doc_count():
    active = get_active_library()
    if not active:
        return jsonify({'count': 0, 'chunks': 0})
    client = get_chroma_client()
    if client is None:
        return jsonify({'count': 0, 'chunks': 0})
    try:
        col = client.get_collection(name=active)
        count = col.count()
        return jsonify({'count': count, 'chunks': count})
    except:
        return jsonify({'count': 0, 'chunks': 0})

# NUEVO ENDPOINT: lista de documentos agrupados por fuente
@app.route('/api/docs/list')
def list_docs():
    active = get_active_library()
    if not active:
        return jsonify({'documents': [], 'error': 'No hay biblioteca activa'})
    client = get_chroma_client()
    if client is None:
        return jsonify({'documents': [], 'error': 'ChromaDB no disponible'})
    try:
        col = client.get_collection(name=active, embedding_function=_embed_fn)
        total = col.count()
        if total == 0:
            return jsonify({'documents': []})
        limit = min(total, 5000)
        res = col.get(limit=limit)
        ids = res.get('ids', [])
        documents = res.get('documents', [])
        metadatas = res.get('metadatas', [])
        
        # Agrupar por fuente
        groups = {}
        for i, doc_id in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) and metadatas[i] is not None else {}
            if not isinstance(meta, dict):
                meta = {}
            fuente = meta.get('fuente') or meta.get('source') or str(doc_id)
            if fuente not in groups:
                groups[fuente] = []
            groups[fuente].append({
                'chunk': meta.get('chunk', i),
                'texto': documents[i] if i < len(documents) else ''
            })
        # Convertir a lista ordenada
        result = [{'fuente': k, 'chunks': v} for k, v in groups.items()]
        result.sort(key=lambda x: x['fuente'])
        return jsonify({'documents': result})
    except Exception as e:
        return jsonify({'documents': [], 'error': str(e)})

@app.route('/api/index', methods=['POST'])
def index_files():
    library = request.form.get('library')
    if not library:
        return jsonify({'success': False, 'error': 'Biblioteca requerida'})
    
    files = request.files.getlist('files')
    if not files:
        return jsonify({'success': False, 'error': 'No se seleccionaron archivos'})

    client = get_chroma_client()
    if client is None:
        return jsonify({'success': False, 'error': 'ChromaDB no disponible'})
    
    try:
        col = client.get_collection(name=library, embedding_function=_embed_fn)
    except:
        return jsonify({'success': False, 'error': f'Biblioteca "{library}" no existe'})

    chunk_size = 512
    overlap = 50
    total_chunks = 0
    indexed = 0

    for file in files:
        # --- INICIO DE EXTRACCIÓN DE TEXTO CON ARCHIVO TEMPORAL ---
        temp_path = None
        content = None
        try:
            # Guardar el archivo subido en un temporal para procesarlo
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
                file.save(tmp.name)
                temp_path = tmp.name
            
            text, error = extract_text_from_file(temp_path, file.filename)
            if error:
                # Si falla la extracción, se registra y se salta el archivo
                print(f"⚠️ {file.filename} - {error}")
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
                continue
            if not text or len(text.strip()) < 10:
                print(f"⚠️ {file.filename} - texto vacío o muy corto")
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
                continue
            content = text
        except Exception as e:
            print(f"❌ Error procesando {file.filename}: {e}")
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            continue
        finally:
            # Eliminar el temporal siempre, incluso si hubo error
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except:
                    pass
        # --- FIN DE EXTRACCIÓN ---

        if not content or len(content.strip()) < 10:
            continue

        chunks = []
        start = 0
        while start < len(content):
            end = start + chunk_size
            chunk = content[start:end]
            if chunk.strip():
                chunks.append(chunk.strip())
            start += (chunk_size - overlap)

        if not chunks:
            continue

        base_name = file.filename
        ext = os.path.splitext(base_name)[1]
        ids = [f"{base_name}_chunk_{j}" for j in range(len(chunks))]
        metadatas = [{"fuente": base_name, "chunk": j, "formato": ext} for j in range(len(chunks))]

        try:
            col.add(ids=ids, documents=chunks, metadatas=metadatas)
            indexed += 1
            total_chunks += len(chunks)
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    return jsonify({'success': True, 'indexed': indexed, 'chunks': total_chunks})

@app.route('/api/search', methods=['POST'])
def search():
    data = request.json
    query = data.get('query', '')
    n_results = data.get('n_results', 10)

    if not query:
        return jsonify({'error': 'Consulta vacía'})

    active = get_active_library()
    if not active:
        return jsonify({'error': 'No hay biblioteca activa'})

    client = get_chroma_client()
    if client is None:
        return jsonify({'error': 'ChromaDB no disponible'})

    try:
        col = client.get_collection(name=active, embedding_function=_embed_fn)
    except:
        return jsonify({'error': f'Biblioteca "{active}" no existe'})

    # --- 1. Obtener embeddings (dense y sparse) de la consulta ---
    try:
        # Dense
        resp_dense = requests.post(
            "http://localhost:8082/v1/embeddings",
            json={"input": [f"search_query: {query}"]},
            timeout=30
        )
        if resp_dense.status_code != 200:
            return jsonify({'error': f'Error en embeddings densos: {resp_dense.text}'})
        q_emb_dense = resp_dense.json()["data"][0]["embedding"]

        # Sparse (si está disponible)
        sparse_fn = getattr(client, '_sparse_fn', None)
        q_emb_sparse = None
        if sparse_fn is not None:
            # La función sparse espera una lista de textos
            q_emb_sparse = sparse_fn([query])[0]  # Devuelve un dict con índices y valores
    except Exception as e:
        return jsonify({'error': f'Error al generar embeddings: {e}'})

    # --- 2. Realizar consultas ---
    results_dense = col.query(query_embeddings=[q_emb_dense], n_results=n_results*2)
    docs_dense = results_dense['documents'][0] if results_dense['documents'] else []
    metas_dense = results_dense['metadatas'][0] if results_dense['metadatas'] else []
    ids_dense = results_dense['ids'][0] if results_dense['ids'] else []

    results_sparse = {'documents': [], 'metadatas': [], 'ids': []}
    if q_emb_sparse is not None:
        try:
            # Consulta sparse
            results_sparse = col.query(
                query_embeddings=[q_emb_sparse],
                n_results=n_results*2,
                # Nota: para sparse, se usa el mismo parámetro query_embeddings
                # pero internamente ChromaDB lo maneja como sparse
            )
        except Exception as e:
            # Si falla, ignoramos y usamos solo dense
            pass

    docs_sparse = results_sparse['documents'][0] if results_sparse['documents'] else []
    metas_sparse = results_sparse['metadatas'][0] if results_sparse['metadatas'] else []
    ids_sparse = results_sparse['ids'][0] if results_sparse['ids'] else []

    # --- 3. Fusionar con Reciprocal Rank Fusion (RRF) ---
    # Crear un diccionario de puntuaciones
    scores = {}
    # Asignar puntuaciones a dense
    for rank, doc_id in enumerate(ids_dense):
        if doc_id not in scores:
            scores[doc_id] = {
                'document': docs_dense[rank] if rank < len(docs_dense) else '',
                'metadata': metas_dense[rank] if rank < len(metas_dense) else {},
                'score': 0
            }
        # RRF: 1 / (k + rank)
        scores[doc_id]['score'] += 1.0 / (60 + rank + 1)

    # Asignar puntuaciones a sparse
    for rank, doc_id in enumerate(ids_sparse):
        if doc_id not in scores:
            scores[doc_id] = {
                'document': docs_sparse[rank] if rank < len(docs_sparse) else '',
                'metadata': metas_sparse[rank] if rank < len(metas_sparse) else {},
                'score': 0
            }
        scores[doc_id]['score'] += 1.0 / (60 + rank + 1)

    # Ordenar por puntuación
    sorted_items = sorted(scores.items(), key=lambda x: x[1]['score'], reverse=True)[:n_results]

    output = []
    for doc_id, data in sorted_items:
        output.append({
            'text': data['document'],
            'fuente': data['metadata'].get('fuente', 'desconocido'),
            'chunk': data['metadata'].get('chunk', ''),
            'score': data['score']
        })

    return jsonify({'results': output})

@app.route('/api/embed-status')
def embed_status():
    try:
        resp = requests.get("http://localhost:8082/health", timeout=2)
        if resp.status_code == 200:
            return jsonify({'status': '✅ Conectado', 'available': True})
    except:
        pass
    try:
        resp = requests.get("http://localhost:8082/v1/models", timeout=2)
        if resp.status_code == 200:
            return jsonify({'status': '✅ Conectado', 'available': True})
    except:
        pass
    return jsonify({'status': '❌ No disponible', 'available': False})

_RAG_PORT = 25000

def get_rag_port():
    """Puerto único del servidor RAG web (consistente con el Manager y el visor)."""
    return _RAG_PORT

def run_server():
    port = get_rag_port()
    logger.info(f"🚀 Servidor RAG web iniciado en http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def main():
    port = 25000
    logger.info(f"📚 Servidor RAG web iniciado en http://127.0.0.1:{port}")
    print(f"📚 Servidor RAG web en http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

if __name__ == '__main__':
    main()