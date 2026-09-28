"""
tools/laya_downloader.py

Descarga y verificación del router Laya (ONNX) que puede sustituir al
`ToolRouter` basado en Gemma.

IMPORTANTE — no se descarga nada automáticamente. Este módulo es pasivo: solo
toca el disco cuando se invoca de forma explícita, ya sea por CLI

    python -m tools.laya_downloader            # descarga (≈650 MB)
    python -m tools.laya_downloader --check    # solo informa del estado

o por el endpoint `POST /api/laya/download` cuando el usuario lo pide desde el
panel. El sistema completo funciona SIN el modelo: el orquestador cae al
`ToolRouter` de Gemma, así que la descarga es siempre una decisión del usuario.

Estructura esperada en disco (`app_data/laya/`):

    model.onnx                 (~647 MB)
    onnx_config.json
    rl_agent_config.json
    tokenizer/tokenizer.json
    tokenizer/tokenizer_config.json
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

try:  # ejecución como paquete
    from . import runtime
except ImportError:  # ejecución suelta (python tools/laya_downloader.py)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from tools import runtime
    except ImportError:  # pragma: no cover
        import runtime  # type: ignore

import logging

logger = logging.getLogger("laya_downloader")


#: Repositorio HuggingFace con el ONNX publicado.
MODEL_REPO = "mizchi/laya-multilingual-onnx"

#: Ficheros imprescindibles. Si falta cualquiera, el router no se intenta cargar.
REQUIRED_FILES: Tuple[str, ...] = (
    "model.onnx",
    "onnx_config.json",
    "rl_agent_config.json",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
)

#: Umbral de cordura para `model.onnx`: un fichero a medias (descarga cortada,
#: LFS apuntando al puntero de texto) pasaría un `exists()` ingenuo y luego
#: reventaría al cargar. El modelo real ronda los 647 MB.
MIN_MODEL_BYTES = 100 * 1024 * 1024

_HF_URL_TEMPLATE = "https://huggingface.co/{repo}/resolve/main/{path}"


def laya_dir() -> Path:
    """Directorio del modelo Laya (`app_data/laya/`)."""
    return Path(runtime.get_data_dir()) / "laya"


def missing_files(model_dir: Optional[Path] = None) -> List[str]:
    """Ficheros requeridos que faltan o están incompletos."""
    root = Path(model_dir) if model_dir else laya_dir()
    missing: List[str] = []
    for rel in REQUIRED_FILES:
        target = root / rel
        if not target.is_file() or target.stat().st_size == 0:
            missing.append(rel)
    return missing


def is_installed(model_dir: Optional[Path] = None) -> bool:
    """¿Está el modelo completo y con un tamaño plausible?

    Verifica existencia + tamaño mínimo de `model.onnx`. No valida el contenido
    del ONNX (eso lo hace `onnxruntime` al cargar; el router tiene fallback).
    """
    root = Path(model_dir) if model_dir else laya_dir()
    if missing_files(root):
        return False
    try:
        if (root / "model.onnx").stat().st_size < MIN_MODEL_BYTES:
            return False
    except OSError:
        return False
    return True


# Alias con nombre más explícito para los consumidores externos.
integrity_ok = is_installed


def status(model_dir: Optional[Path] = None) -> dict:
    """Estado del modelo para exponerlo por API/CLI (nunca descarga)."""
    root = Path(model_dir) if model_dir else laya_dir()
    missing = missing_files(root)
    size = 0
    model_path = root / "model.onnx"
    try:
        if model_path.is_file():
            size = model_path.stat().st_size
    except OSError:
        size = 0
    return {
        "installed": is_installed(root),
        "dir": str(root),
        "repo": MODEL_REPO,
        "model_bytes": size,
        "missing": missing,
    }


def _download_with_hf_hub(root: Path, progress: Optional[Callable[[str], None]] = None) -> bool:
    """Vía preferente: `huggingface_hub` (reanuda, verifica y cachea)."""
    try:
        from huggingface_hub import hf_hub_download  # type: ignore
    except Exception:
        return False

    for rel in REQUIRED_FILES:
        if progress:
            progress(f"descargando {rel}")
        hf_hub_download(repo_id=MODEL_REPO, filename=rel, local_dir=str(root))
    return True


def _download_with_requests(root: Path, progress: Optional[Callable[[str], None]] = None) -> bool:
    """Fallback sin dependencias de HF: `requests` en streaming."""
    try:
        import requests  # type: ignore
    except Exception:
        return False

    for rel in REQUIRED_FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        url = _HF_URL_TEMPLATE.format(repo=MODEL_REPO, path=rel)
        if progress:
            progress(f"descargando {rel}")
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            tmp = target.with_suffix(target.suffix + ".part")
            with open(tmp, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
            os.replace(tmp, target)
    return True


def download(
    model_dir: Optional[Path] = None,
    force: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> bool:
    """Descarga el modelo. NUNCA se llama solo: requiere acción explícita.

    Devuelve True si al terminar el modelo queda instalado. Con `force=False`
    y el modelo ya presente, no hace nada.
    """
    root = Path(model_dir) if model_dir else laya_dir()
    if not force and is_installed(root):
        if progress:
            progress("ya instalado; nada que hacer")
        return True

    root.mkdir(parents=True, exist_ok=True)
    ok = False
    try:
        ok = _download_with_hf_hub(root, progress)
        if not ok:
            ok = _download_with_requests(root, progress)
    except Exception as exc:  # red caída, repo privado, 404...
        logger.error("Descarga Laya fallida: %s", exc)
        if progress:
            progress(f"error: {exc}")
        return False

    if not ok:
        logger.error("No hay `huggingface_hub` ni `requests` disponibles para descargar Laya.")
        if progress:
            progress("error: sin cliente de descarga")
        return False

    return is_installed(root)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.laya_downloader",
        description="Descarga/verifica el router Laya ONNX (≈650 MB). Sin argumentos descarga.",
    )
    parser.add_argument("--check", action="store_true", help="solo informa del estado, no descarga")
    parser.add_argument("--download", action="store_true", help="descarga (equivalente a no pasar nada)")
    parser.add_argument("--force", action="store_true", help="descarga aunque ya exista")
    args = parser.parse_args(argv)

    info = status()

    if args.check:
        print(f"Laya: {'INSTALADO' if info['installed'] else 'NO INSTALADO'}")
        print(f"  dir:     {info['dir']}")
        print(f"  repo:    {info['repo']}")
        print(f"  pesos:   {info['model_bytes'] / (1024 * 1024):.1f} MB")
        if info["missing"]:
            print(f"  faltan:  {', '.join(info['missing'])}")
        return 0

    if not download(force=args.force, progress=lambda msg: print(f"  {msg}")):
        print("No se pudo completar la descarga de Laya.", file=sys.stderr)
        return 1
    print("Laya listo.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
