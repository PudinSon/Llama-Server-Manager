"""
tools/laya_onnx_router.py

Router de tareas basado en el modelo Laya (ONNX) que sustituye al `ToolRouter`
de Gemma cuando está disponible. Gemma cuesta una llamada LLM completa por
tarea (segundos); Laya es un clasificador de 650 MB (~100× más rápido).

Diseño defensivo a propósito: este módulo NO puede romper el sistema.
- `onnxruntime` y `tokenizers` se importan dentro de `load()`, no al importar
  el módulo (dev_agent arranca aunque falten).
- Si el modelo no está, si falla la carga o si la inferencia da algo raro,
  `route()` devuelve el diccionario de fallback y el orquestador usa Gemma.
- `route()` nunca lanza excepción.

Calibración pendiente: el formato exacto de prompt/scoring de Laya debe
ajustarse contra el modelo real (ver `laya-mlx/common.py` del repo de
referencia). El contrato de este módulo es estable; los detalles de I/O están
aislados en `build_prompt()` y `_classify()`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:  # ejecución como paquete
    from . import runtime
    from . import laya_downloader
except ImportError:  # pragma: no cover - ejecución suelta
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from tools import runtime  # type: ignore
        from tools import laya_downloader  # type: ignore
    except ImportError:
        import runtime  # type: ignore
        import laya_downloader  # type: ignore

logger = logging.getLogger("laya_onnx_router")


#: Modos válidos. Espejo de `agent_core.ToolRouter.MODES` (se mantienen
#: duplicados a propósito para no importar `agent_core` —y con él ChromaDB—
#: solo por una tupla; hay un test que verifica que no divergen).
MODES: Sequence[str] = ("CHAT", "AGENCY")

#: Naturalezas válidas. Espejo de `agent_core.TASK_NATURES`.
NATURES: Sequence[str] = ("synthesis", "construction", "mixed")

#: Marcadores del formato Laya (estado/pregunta), configurables por
#: `rl_agent_config.json` (`prompt_markers` o `marker_start`/`marker_end`).
DEFAULT_MARKER_START = "<|marker|>"
DEFAULT_MARKER_END = "<|/marker|>"

MODE_QUESTION = "¿Qué modo de ejecución requiere esta tarea?"
NATURE_QUESTION = "¿Qué naturaleza tiene el trabajo: texto único o artefactos persistentes?"

_FALLBACK = {"mode": "AGENCY", "nature": "synthesis", "confidence": 0.0, "reasoning": "fallback"}


# ---------------------------------------------------------------------------
# Ajustes (leídos del config.json compartido, sin importar dev_agent)
# ---------------------------------------------------------------------------

def _config_path() -> Path:
    return Path(runtime.get_user_data_dir()) / "config.json"


def _load_defaults() -> Dict[str, Any]:
    """Lee `defaults` de config.json. Nunca lanza: devuelve {} si no puede."""
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    merged = DEFAULT_SETTINGS.copy()
    if isinstance(data.get("defaults"), dict):
        merged.update(data["defaults"])
    else:
        merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    return merged


#: Espejo de los defaults de `dev_agent.DEFAULT_SETTINGS` para las 2 claves que
#: nos importan (evita importar dev_agent, que arrastra FastMCP).
DEFAULT_SETTINGS: Dict[str, Any] = {"laya_enabled": True, "laya_preload": False}


def laya_enabled() -> bool:
    """¿El usuario tiene activado el router Laya?"""
    value = _load_defaults().get("laya_enabled", True)
    return bool(value)


def laya_preload() -> bool:
    """¿Debe cargarse al arranque en vez de en la primera ruta?"""
    value = _load_defaults().get("laya_preload", False)
    return bool(value)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class LayaONNXRouter:
    """Clasificador ONNX de modo/naturaleza con carga perezosa."""

    def __init__(self, model_dir: Optional[Path] = None, lazy: bool = True):
        self.model_dir = Path(model_dir) if model_dir else laya_downloader.laya_dir()
        self.lazy = lazy
        self._session = None
        self._tokenizer = None
        self._marker_start = DEFAULT_MARKER_START
        self._marker_end = DEFAULT_MARKER_END
        self._choices_config: Dict[str, Any] = {}
        self._load_seconds: Optional[float] = None
        self._load_error: Optional[str] = None
        self._lock = threading.Lock()

    # -- estado ------------------------------------------------------------
    def is_loaded(self) -> bool:
        return self._session is not None

    def load_seconds(self) -> Optional[float]:
        return self._load_seconds

    def last_error(self) -> Optional[str]:
        return self._load_error

    def available(self) -> bool:
        """¿Se puede (y se debe) usar Laya ahora mismo?"""
        return laya_enabled() and laya_downloader.is_installed(self.model_dir)

    def status(self) -> Dict[str, Any]:
        info = laya_downloader.status(self.model_dir)
        info.update({
            "enabled": laya_enabled(),
            "preload": laya_preload(),
            "loaded": self.is_loaded(),
            "load_seconds": self._load_seconds,
            "error": self._load_error,
        })
        return info

    # -- carga / descarga --------------------------------------------------
    def _read_marker_config(self) -> None:
        """Lee marcadores/opciones de `rl_agent_config.json` si existe."""
        try:
            raw = (self.model_dir / "rl_agent_config.json").read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return
            self._choices_config = data
            self._marker_start = str(data.get("marker_start") or data.get("markers", {}).get("start") or DEFAULT_MARKER_START)
            self._marker_end = str(data.get("marker_end") or data.get("markers", {}).get("end") or DEFAULT_MARKER_END)
        except Exception as exc:
            logger.debug("Sin rl_agent_config.json utilizable: %s", exc)

    def load(self) -> bool:
        """Carga el modelo. Idempotente y a prueba de fallos (devuelve bool)."""
        with self._lock:
            if self.is_loaded():
                return True

            started = time.perf_counter()
            try:
                import onnxruntime  # type: ignore
                from tokenizers import Tokenizer  # type: ignore
            except Exception as exc:
                self._load_error = f"dependencias ausentes: {exc}"
                logger.warning("Laya no cargable: %s", self._load_error)
                return False

            model_path = self.model_dir / "model.onnx"
            try:
                self._session = onnxruntime.InferenceSession(
                    str(model_path), providers=["CPUExecutionProvider"]
                )
                self._tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer" / "tokenizer.json"))
                self._read_marker_config()
            except Exception as exc:
                self._session = None
                self._tokenizer = None
                self._load_error = str(exc)
                logger.error("Fallo cargando Laya: %s", exc)
                return False

            self._load_seconds = time.perf_counter() - started
            self._load_error = None
            logger.info("Laya cargado en %.2fs", self._load_seconds)
            return True

    def ensure_loaded(self) -> bool:
        """Carga si procede (respeta `laya_enabled`)."""
        if not laya_enabled():
            return False
        if self.is_loaded():
            return True
        if not laya_downloader.is_installed(self.model_dir):
            return False
        return self.load()

    def unload(self) -> bool:
        with self._lock:
            self._session = None
            self._tokenizer = None
            self._load_seconds = None
            return True

    # -- inferencia --------------------------------------------------------
    def build_prompt(self, task: str, question: str, state: str = "") -> str:
        """Prompt con marcadores Laya. AISLADO: punto de calibración."""
        parts = []
        if state:
            parts.append(f"{self._marker_start}{state}{self._marker_end}")
        parts.append(f"{self._marker_start}{task}{self._marker_end}")
        parts.append(f"{self._marker_start}{question}{self._marker_end}")
        return "\n".join(parts)

    def _encode(self, text: str) -> Dict[str, Any]:
        return self._tokenizer.encode(text).ids

    def _labels_for(self, choices_key: str) -> List[str]:
        """Orden de etiquetas de la cabeza clasificadora.

        Si `rl_agent_config.json` declara `labels` (o `choices`) para ese
        clasificador, ESE es el orden autoritativo del modelo. Si no lo declara
        se usa el orden canónico de `MODES`/`NATURES` y se registra en el log:
        mapear el argmax por posición sin conocer el orden real de las etiquetas
        es una suposición, y las suposiciones silenciosas son las que luego
        cuestan una tarde de depuración.
        """
        canonical = {"mode": list(MODES), "nature": list(NATURES)}[choices_key]
        declared = None
        try:
            config = self._choices_config or {}
            for key in ("labels", "choices"):
                block = config.get(key)
                if isinstance(block, dict) and isinstance(block.get(choices_key), list):
                    declared = block[choices_key]
                    break
        except Exception:
            declared = None
        if declared:
            return [str(item) for item in declared]
        logger.debug(
            "Laya: sin orden de etiquetas declarado para '%s'; se asume el canónico",
            choices_key,
        )
        return canonical

    def _classify(self, prompt: str, choices: Sequence[str], choices_key: str) -> Optional[Dict[str, Any]]:
        """Elige una opción entre `choices`.

        1. Si el modelo expone una cabeza de clasificación (salida con última
           dimensión == número de opciones), se usa su argmax.
        2. Si no, scoring tipo teacher-forcing sobre `prompt + opción`.
        Devuelve {"choice", "probability"} o None si no se pudo decidir.
        """
        try:
            import numpy as np  # type: ignore
        except Exception:
            return None

        try:
            input_names = {i.name for i in self._session.get_inputs()}
            outputs = self._run(np, prompt)
            if not outputs:
                return None

            # Caso 1: cabeza clasificadora. El índice se resuelve con el ORDEN
            # declarado por el modelo, no con el orden de `choices` (que solo es
            # un conjunto de opciones válidas).
            labels = self._labels_for(choices_key)
            if len(labels) == len(choices):
                for out in outputs:
                    arr = np.asarray(out)
                    if arr.ndim == 2 and arr.shape[-1] == len(choices):
                        probs = _softmax(arr[0].astype("float64"))
                        best = int(probs.argmax())
                        return {"choice": labels[best], "probability": float(probs[best])}

            # Caso 2: teacher-forcing (necesita el vocabulario del tokenizer).
            vocab = getattr(self._tokenizer, "get_vocab_size", lambda: None)()
            scored: List[float] = []
            for choice in choices:
                ids = self._encode(prompt + " " + choice)
                logits = self._run(np, None, ids)
                if not logits:
                    return None
                arr = np.asarray(logits[0])
                if arr.ndim != 3 or vocab is None or arr.shape[-1] != vocab:
                    return None
                # Log-prob media de los tokens de la opción.
                offset = max(0, len(ids) - len(choice))
                total = 0.0
                for pos in range(1, len(ids)):
                    token = ids[pos]
                    log_probs = np.log(_softmax(arr[0, pos - 1].astype("float64")) + 1e-12)
                    total += float(log_probs[token])
                scored.append(total / max(1, len(ids) - offset))

            if not scored:
                return None
            probs = _softmax(np.asarray(scored))
            best = int(probs.argmax())
            return {"choice": choices[best], "probability": float(probs[best])}
        except Exception as exc:
            logger.debug("Laya classify falló (%s): %s", choices_key, exc)
            return None

    def _run(self, np, prompt: str, ids: Optional[List[int]] = None):
        """Ejecuta el ONNX con los nombres de entrada reales del modelo."""
        inputs = {}
        input_meta = {i.name: i for i in self._session.get_inputs()}

        if ids is None:
            ids = self._encode(prompt)
        arr = np.asarray([ids], dtype="int64")
        if "input_ids" in input_meta:
            inputs["input_ids"] = arr
        else:
            # Un único tensor de entrada: se le pasa tal cual.
            inputs[list(input_meta)[0]] = arr
        if "attention_mask" in input_meta:
            inputs["attention_mask"] = np.ones_like(arr)

        return self._session.run(None, inputs)

    # -- API pública -------------------------------------------------------
    def route(self, task: str, reasoning: bool = True, bus: Any = None) -> Dict[str, Any]:
        """Clasifica modo + naturaleza. Nunca lanza; si no puede, hay fallback."""
        if not self.ensure_loaded():
            return dict(_FALLBACK, router="fallback", tools_desc="")

        started = time.perf_counter()
        try:
            prompt = self.build_prompt(task, MODE_QUESTION)
            mode_result = self._classify(prompt, list(MODES), "mode")
            if mode_result is None:
                return dict(_FALLBACK, router="fallback", tools_desc="")

            nature_result = self._classify(
                self.build_prompt(task, NATURE_QUESTION), list(NATURES), "nature"
            )
            nature = nature_result["choice"] if nature_result else "synthesis"
            elapsed_ms = (time.perf_counter() - started) * 1000

            decision = {
                "mode": str(mode_result["choice"]).upper(),
                "nature": nature,
                "confidence": float(mode_result["probability"]),
                "reasoning": f"Laya multilingual ({elapsed_ms:.0f} ms)",
                "tools_desc": "",
                "router": "laya",
            }
            _audit(task, mode_result, nature_result, fallback=False)
            if bus is not None:
                try:
                    bus.emit(_event("router", {"status": "decided", "router": "laya", **decision}))
                except Exception:
                    pass
            return decision
        except Exception as exc:
            logger.warning("Laya route falló: %s", exc)
            _audit(task, None, None, fallback=True)
            return dict(_FALLBACK, router="fallback", tools_desc="")

    def route_nature(self, task: str) -> str:
        """Solo la naturaleza (`synthesis` | `construction` | `mixed`)."""
        if not self.ensure_loaded():
            return "synthesis"
        result = self._classify(
            self.build_prompt(task, NATURE_QUESTION), list(NATURES), "nature"
        )
        return result["choice"] if result else "synthesis"


def _softmax(values):
    """Softmax estable en una dimensión."""
    import numpy as np  # type: ignore

    vector = np.asarray(values, dtype="float64")
    vector = vector - vector.max()
    exponentials = np.exp(vector)
    return exponentials / exponentials.sum()


def _event(event_type: str, payload: Dict[str, Any]):
    """Crea un AgentEvent sin importar agent_core al cargar el módulo."""
    try:
        from .agent_core import AgentEvent

        return AgentEvent(event_type, payload)
    except Exception:
        return None


def _audit(task: str, mode_result, nature_result, fallback: bool) -> None:
    """Traza de auditoría de la decisión (Acción 8.6).

    Sin esto, depurar una clasificación errónea es imposible: no queda registro
    de qué opción eligió el modelo ni con qué probabilidad.
    """
    logger.info(
        "laya_audit %s",
        json.dumps({
            "task_snippet": (task or "")[:100],
            "mode": (mode_result or {}).get("choice"),
            "mode_prob": round((mode_result or {}).get("probability", 0.0), 4),
            "nature": (nature_result or {}).get("choice"),
            "fallback": fallback,
        }, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_router: Optional[LayaONNXRouter] = None
_router_lock = threading.Lock()


def get_laya_router(lazy: bool = True) -> LayaONNXRouter:
    """Instancia única del router.

    Con `lazy=False` (o `laya_preload=True` en config) intenta cargar el ONNX
    en la primera llamada. El manager/dev_agent lo usan para precargar al
    arrancar y no pagar 1-3 s en la primera tarea.
    """
    global _router
    with _router_lock:
        if _router is None:
            _router = LayaONNXRouter(lazy=lazy)
            # Antes solo miraba `not lazy and laya_preload()`. Ahora cualquiera
            # de las dos condiciones activa el preload, y si falla no rompe.
            if (not lazy or laya_preload()) and laya_enabled():
                _router.ensure_loaded()
        return _router


def preload_laya_if_enabled() -> Dict[str, Any]:
    """Precarga Laya SOLO si `laya_enabled=True` Y `laya_preload=True`.

    Antes esta función respetaba `laya_enabled` pero llamaba a `router.load()`
    de forma incondicional, ignorando `laya_preload`. Resultado: dev_agent
    arrancaba con ~650 MB de ONNX cargados aunque el usuario hubiera pedido
    carga perezosa. Ahora ambos flags son OBLIGATORIOS.
    """
    info: Dict[str, Any] = {
        "enabled": laya_enabled(),
        "preload": laya_preload(),
        "installed": False,
        "loaded": False,
        "seconds": None,
        "error": None,
    }
    # Diagnóstico: sin esto, un `laya_enabled()` que devuelva True por un
    # config.json ausente/parcial es indepurable desde fuera.
    logger.info(
        "🧠 Laya: enabled=%s preload=%s config=%s",
        info["enabled"], info["preload"], _config_path(),
    )
    try:
        if not info["enabled"]:
            logger.info("🧠 Laya: desactivado por el usuario; no se precarga")
            return info
        if not info["preload"]:
            logger.info("🧠 Laya: precarga diferida (laya_preload=False)")
            return info
        router = get_laya_router(lazy=True)   # singleton, sin forzar carga
        info["installed"] = laya_downloader.is_installed(router.model_dir)
        if not info["installed"]:
            logger.info("🧠 Laya: enabled+preload pero el modelo no está instalado")
            return info
        if router.is_loaded():
            info["loaded"] = True
            info["seconds"] = router.load_seconds()
            return info
        info["loaded"] = router.load()
        info["seconds"] = router.load_seconds()
        info["error"] = router.last_error()
    except Exception as exc:
        info["error"] = str(exc)
    return info


def reset_laya_router() -> None:
    """Descarta el singleton (tests / recarga de configuración)."""
    global _router
    with _router_lock:
        if _router is not None:
            _router.unload()
        _router = None
