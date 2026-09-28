# tools/workspace_server.py
"""
Servidor HTTP del workspace (puerto 19000).

Da a la WebUI (y al modelo) acceso al workspace real: listar, previsualizar,
descargar, subir, crear carpetas y borrar (a la papelera cuando se puede).

Diseñado para vivir DETRÁS de nginx:
  * ProxyFix → respeta X-Forwarded-Proto/Host/Prefix.
  * `public_url()` devuelve URLs absolutas válidas también desde fuera.
  * CORS abierto (igual que el resto de servicios del proyecto).
  * Todos los endpoints llevan `@requires_auth` (hoy no-op; ver tools/auth.py).
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import shutil
import sys
import unicodedata
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_file, Response

try:
    from . import auth as auth_mod
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from tools import auth as auth_mod
        from tools import runtime
    except ImportError:
        import auth as auth_mod  # type: ignore
        import runtime  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("workspace_server")

WORKSPACE_PORT = 19000
PREVIEW_MAX_BYTES = 200_000
UPLOAD_MAX_BYTES = 512 * 1024 * 1024
IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
                ".chromadb", ".venvs", ".mypy_cache", ".pytest_cache", ".idea"}

#: Extensiones que el navegador pinta por sí mismo desde /api/workspace/raw
#: (el preview devuelve esta marca de tipo en lugar de un blob binario).
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
NATIVE_VIEWER_EXTS = {".pdf"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MAX_BYTES
auth_mod.apply_proxy_fix(app)


# ----------------------------------------------------------------------------
# Utilidades de rutas
# ----------------------------------------------------------------------------
def workspace_root() -> Path:
    """Raíz del workspace actual (misma resolución que dev_agent.get_workspace).

    Orden: user_data/config.json (defaults.workspace_path / workspace) y, si no
    hay nada, RuntimeConfig.get_workspace_abs(); último recurso: cwd.
    """
    try:
        cfg_path = Path(runtime.get_user_data_dir()) / "config.json"
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
        mode = defaults.get("workspace_mode") or data.get("workspace_mode") or "default"
        if mode == "global":
            return Path(os.path.abspath(os.sep))
        raw = defaults.get("workspace_path") or data.get("workspace")
        if raw:
            candidate = Path(str(raw)).expanduser()
            if not candidate.is_absolute():
                candidate = Path(runtime.get_base_dir()) / candidate
            return candidate.resolve()
    except Exception as e:
        logger.debug("Sin config.json utilizable para el workspace: %s", e)

    try:
        config_cls = getattr(runtime, "RuntimeConfig", None)
        if config_cls is not None:
            return Path(str(config_cls.get_workspace_abs())).resolve()
    except Exception as e:
        logger.warning("No se pudo resolver el workspace desde RuntimeConfig: %s", e)
    return Path(os.getcwd()).resolve()


def safe_path(relative: str = ".", must_exist: bool = False) -> Path:
    """
    Resuelve `relative` DENTRO del workspace.

    Lanza ValueError si el resultado se sale del workspace (path traversal).
    """
    root = workspace_root()
    raw = (relative or ".").strip().strip('"')
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"Ruta fuera del workspace: {relative}")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"No existe: {relative}")
    return resolved


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(workspace_root())).replace("\\", "/")
    except Exception:
        return str(path)


def _safe_filename(name: str) -> str:
    """Nombre de archivo seguro para Content-Disposition y para escribir en disco."""
    name = os.path.basename((name or "").strip()) or "archivo"
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if c.isprintable() and c not in '\\/:*?"<>|')
    name = name.strip(". ") or "archivo"
    return name[:180]


def _is_binary(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return True
    try:
        data[:8192].decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _entry_info(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": rel(path),
        "type": "dir" if path.is_dir() else "file",
        "size": stat.st_size if path.is_file() else None,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "ext": path.suffix.lower(),
    }


def _walk(base: Path, recursive: bool, depth: int) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    stack = [(base, 0)]
    while stack:
        current, level = stack.pop(0)
        try:
            children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except (PermissionError, OSError):
            continue
        for child in children:
            if child.name in IGNORED_DIRS or child.name.startswith("."):
                if child.name not in (".skills",):
                    continue
            try:
                entries.append(_entry_info(child))
            except OSError:
                continue
            if recursive and child.is_dir() and level < depth:
                stack.append((child, level + 1))
    return entries


# ----------------------------------------------------------------------------
# Respuestas / errores
# ----------------------------------------------------------------------------
@app.after_request
def _add_cors(response: Response) -> Response:
    for key, value in auth_mod.cors_headers().items():
        response.headers[key] = value
    return response


@app.errorhandler(Exception)
def _handle_error(error):
    code = getattr(error, "code", 500) or 500
    if isinstance(error, ValueError):
        code = 400
    elif isinstance(error, FileNotFoundError):
        code = 404
    elif isinstance(error, PermissionError):
        code = 403
    logger.warning("Error %s: %s", code, error)
    return jsonify({"success": False, "error": str(error)}), code


@app.route("/api/workspace/ping", methods=["GET"])
@auth_mod.requires_auth
def ping():
    return jsonify({"success": True, "service": "workspace", "port": WORKSPACE_PORT,
                    "workspace": str(workspace_root())})


# ----------------------------------------------------------------------------
# Listado y metadatos
# ----------------------------------------------------------------------------
@app.route("/api/workspace/info", methods=["GET"])
@auth_mod.requires_auth
def info():
    root = workspace_root()
    return jsonify({
        "success": True,
        "workspace": str(root),
        "exists": root.exists(),
        "writable": os.access(root, os.W_OK) if root.exists() else False,
    })


@app.route("/api/workspace/list", methods=["GET", "OPTIONS"])
@auth_mod.requires_auth
def list_dir():
    if request.method == "OPTIONS":
        return ("", 204)
    path = request.args.get("path", ".")
    recursive = request.args.get("recursive", "0") in ("1", "true", "True")
    try:
        depth = int(request.args.get("depth", 3 if recursive else 0))
    except Exception:
        depth = 3
    base = safe_path(path, must_exist=True)
    if base.is_file():
        return jsonify({"success": True, "path": rel(base), "type": "file",
                        "entries": [_entry_info(base)]})
    entries = _walk(base, recursive=recursive, depth=depth)
    return jsonify({
        "success": True,
        "workspace": str(workspace_root()),
        "path": rel(base),
        "recursive": recursive,
        "total": len(entries),
        "entries": entries,
    })


def _docx_to_html(path: Path) -> Optional[str]:
    """Convierte un .docx a HTML simple (párrafos + tablas) con python-docx.

    Devuelve None si python-docx no está disponible o falla la conversión.
    """
    try:
        from docx import Document


    except Exception:
        return None
    try:
        doc = Document(str(path))
    except Exception as e:
        logger.warning("No se pudo leer el docx %s: %s", path, e)
        return None
    parts: List[str] = []
    for para in doc.paragraphs:
        text = " ".join(run.text for run in para.runs) or para.text
        if text.strip():
            parts.append(f"<p>{escape(text)}</p>")
    for table in doc.tables:
        rows = []
        for row in table.rows:
            cells = "".join(f"<td>{escape(c.text)}</td>" for c in row.cells)
            rows.append(f"<tr>{cells}</tr>")
        if rows:
            parts.append(f"<table border='1' cellpadding='4' cellspacing='0'>{''.join(rows)}</table>")
    return "\n".join(parts) if parts else None


@app.route("/api/workspace/preview", methods=["GET", "OPTIONS"])
@auth_mod.requires_auth
def preview():
    if request.method == "OPTIONS":
        return ("", 204)
    target = safe_path(request.args.get("path", ""), must_exist=True)
    if target.is_dir():
        return jsonify({"success": False, "error": "'path' apunta a un directorio"}, 400)
    size = target.stat().st_size
    ext = target.suffix.lower()
    if ext == ".docx":
        html = _docx_to_html(target)
        if html is not None:
            return jsonify({
                "success": True, "path": rel(target), "kind": "html",
                "size": size, "html": html,
            })
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # PDF e imágenes: la UI los muestra con el visor nativo apuntando a /raw.
    # Devolver "binary" los dejaba en "Vista previa no disponible".
    if ext in NATIVE_VIEWER_EXTS:
        return jsonify({
            "success": True, "path": rel(target), "kind": "pdf",
            "size": size, "mime": mime,
        })
    if ext in IMAGE_EXTS:
        return jsonify({
            "success": True, "path": rel(target), "kind": "image",
            "size": size, "mime": mime,
        })
    data = target.read_bytes()[:PREVIEW_MAX_BYTES]
    kind = "binary" if _is_binary(data) else "text"
    payload: Dict[str, Any] = {
        "success": True,
        "path": rel(target),
        "kind": kind,
        "size": size,
        "truncated": size > PREVIEW_MAX_BYTES,
        "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
    }
    if kind == "text":
        payload["content"] = data.decode("utf-8", errors="replace")
    return jsonify(payload)


# ----------------------------------------------------------------------------
# Descarga / visualización
# ----------------------------------------------------------------------------
def _send(target: Path, as_attachment: bool) -> Response:
    response = send_file(
        str(target),
        as_attachment=as_attachment,
        download_name=_safe_filename(target.name),
        conditional=True,
    )
    if not as_attachment:
        # El visor nativo de PDF de Chrome/Edge se implementa como un
        # contexto de navegación anidado. Cuando la página anfitriona
        # (WebUI) tiene COEP: require-corp, Chrome exige que la respuesta
        # del PDF declare COEP también. Sin este header:
        #   net::ERR_BLOCKED_BY_RESPONSE
        #   «Cross-Origin-Embedder-Policy not set on the response»
        # CORP (que ya se envía vía auth.cors_headers()) autoriza el embed,
        # pero no satisface esta comprobación adicional de Chrome.
        if target.suffix.lower() in NATIVE_VIEWER_EXTS:
            response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    return response

@app.route("/api/workspace/download", methods=["GET", "OPTIONS"])
@auth_mod.requires_auth
def download():
    if request.method == "OPTIONS":
        return ("", 204)
    target = safe_path(request.args.get("path", ""), must_exist=True)
    if target.is_dir():
        # Descarga de carpeta: se empaqueta al vuelo en un temporal y se envía.
        import tempfile
        base_name = _safe_filename(target.name)
        tmp_dir = tempfile.mkdtemp(prefix="ws_zip_")
        try:
            archive = shutil.make_archive(str(Path(tmp_dir) / base_name), "zip", target)
            return send_file(archive, as_attachment=True,
                             download_name=f"{base_name}.zip", mimetype="application/zip")
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
    return _send(target, as_attachment=True)


@app.route("/api/workspace/raw", methods=["GET", "OPTIONS"])
@auth_mod.requires_auth
def raw():
    if request.method == "OPTIONS":
        return ("", 204)
    target = safe_path(request.args.get("path", ""), must_exist=True)
    if target.is_dir():
        return jsonify({"success": False, "error": "'path' apunta a un directorio"}, 400)
    return _send(target, as_attachment=False)


@app.route("/api/workspace/link", methods=["GET"])
@auth_mod.requires_auth
def link():
    """Devuelve una URL absoluta de descarga (útil detrás de nginx)."""
    target = safe_path(request.args.get("path", ""), must_exist=True)
    path = rel(target)
    return jsonify({
        "success": True,
        "path": path,
        "download_url": auth_mod.public_url(f"/api/workspace/download?path={path}",
                                            direct_port=WORKSPACE_PORT),
        "raw_url": auth_mod.public_url(f"/api/workspace/raw?path={path}",
                                       direct_port=WORKSPACE_PORT),
    })


# ----------------------------------------------------------------------------
# Escritura: subir, crear carpetas, borrar, renombrar
# ----------------------------------------------------------------------------
@app.route("/api/workspace/upload", methods=["POST", "OPTIONS"])
@auth_mod.requires_auth
def upload():
    if request.method == "OPTIONS":
        return ("", 204)
    dest_arg = request.form.get("path") or request.args.get("path") or "."
    dest_dir = safe_path(dest_arg, must_exist=True)
    if not dest_dir.is_dir():
        return jsonify({"success": False, "error": "El destino no es un directorio"}, 400)

    files = request.files.getlist("file") or request.files.getlist("files")
    if not files:
        return jsonify({"success": False, "error": "No se recibió ningún archivo"}, 400)

    saved = []
    for storage in files:
        if not storage or not storage.filename:
            continue
        name = _safe_filename(storage.filename)
        target = dest_dir / name
        # Evitar sobrescribir: sufijo _1, _2...
        if target.exists():
            stem, suffix = target.stem, target.suffix
            index = 1
            while target.exists():
                target = dest_dir / f"{stem}_{index}{suffix}"
                index += 1
        storage.save(str(target))
        saved.append(_entry_info(target))
    return jsonify({"success": True, "uploaded": saved, "count": len(saved)})


@app.route("/api/workspace/mkdir", methods=["POST", "OPTIONS"])
@auth_mod.requires_auth
def make_dir():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    name = _safe_filename(str(data.get("name") or data.get("path") or "").strip())
    if not name:
        return jsonify({"success": False, "error": "'name' es obligatorio"}, 400)
    parent = safe_path(data.get("path") or ".", must_exist=True)
    target = (parent / name) if parent.is_dir() else parent.parent / name
    target.mkdir(parents=True, exist_ok=True)
    return jsonify({"success": True, "path": rel(target)})


@app.route("/api/workspace/delete", methods=["DELETE", "POST", "OPTIONS"])
@auth_mod.requires_auth
def delete():
    if request.method == "OPTIONS":
        return ("", 204)
    payload = request.get_json(silent=True) or {}
    path = request.args.get("path") or payload.get("path") or ""
    target = safe_path(path, must_exist=True)
    if target == workspace_root():
        return jsonify({"success": False, "error": "No se puede borrar la raíz del workspace"}, 400)
    moved_to_trash = False
    try:
        from send2trash import send2trash  # type: ignore
        send2trash(str(target))
        moved_to_trash = True
    except Exception:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    return jsonify({"success": True, "path": rel(target), "trash": moved_to_trash})


@app.route("/api/workspace/rename", methods=["POST", "OPTIONS"])
@auth_mod.requires_auth
def rename():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    target = safe_path(data.get("path") or "", must_exist=True)
    new_name = _safe_filename(str(data.get("new_name") or "").strip())
    if not new_name:
        return jsonify({"success": False, "error": "'new_name' es obligatorio"}, 400)
    destination = target.parent / new_name
    if destination.exists():
        return jsonify({"success": False, "error": f"Ya existe '{new_name}'"}, 409)
    target.rename(destination)
    return jsonify({"success": True, "path": rel(destination)})


# ----------------------------------------------------------------------------
# Arranque
# ----------------------------------------------------------------------------
def start_workspace_server(host: str = "0.0.0.0", port: int = WORKSPACE_PORT,
                           debug: bool = False) -> None:
    logger.info("📁 Workspace server en http://%s:%s (workspace=%s)", host, port, workspace_root())
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)


def build_arg_parser():
    """Parser CLI del servidor de workspace.

    Expuesto aparte para que los tests puedan comprobar el contrato de banderas
    que usa el Manager sin arrancar el servidor.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="workspace_server",
                                     description="Servidor HTTP del workspace")
    parser.add_argument("--host", default=os.environ.get("WORKSPACE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORKSPACE_PORT", WORKSPACE_PORT)))
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    start_workspace_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
