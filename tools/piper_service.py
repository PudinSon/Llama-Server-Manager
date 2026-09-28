"""
Piper TTS Service with WebSocket-based Call Mode and Energy VAD.

Sustituto directo de `tools/kokoro_service.py`: mismo contrato de API que
consum\u00eda la UI, pero usando piper-tts (m\u00e1s ligero que Kokoro, ideal para CPU).

Endpoints:
  GET  /health           - Health check (lazy)
  GET  /voices           - Lista de voces disponibles (archivos .onnx)
  POST /synthesize       - Sintetiza texto a audio (base64 WAV)
  WS   /ws/call          - Canal de llamada bidireccional con VAD
                          (pipeline STT -> LLM -> TTS)

Audio esperado del cliente (micr\u00f3fono): PCM 16kHz mono, 16-bit.
"""

from __future__ import annotations

from piper.config import DEFAULT_LENGTH_SCALE

try:
    from pathlib import Path
    import sys
    _THIS_DIR = Path(__file__).resolve().parent
    # tools/ está en <project_root>/tools; necesitamos <project_root> en sys.path
    _PROJECT_ROOT = _THIS_DIR.parent
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
except Exception:
    pass

import os
import io
import json
import base64
import asyncio
import logging
import tempfile
import threading
import time
import wave
from typing import Optional

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import soundfile as sf

# ---------------------------------------------------------------------------
# Piper TTS setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("piper_service")

# Carpeta donde viven los modelos .onnx + .json de voz.
# Por defecto apunta a app_data/piper/voices (donde están los archivos .onnx).
def _resolve_app_data_dir() -> Path:
    """Devuelve app_data correcto en dev y en binario congelado."""
    # 1) runtime.get_data_dir() es la fuente de verdad
    for modname in ("tools.runtime", "runtime"):
        try:
            mod = __import__(modname, fromlist=["get_data_dir"])
            return Path(mod.get_data_dir())
        except Exception:
            continue
    # 2) Variable de entorno explícita (útil en despliegues raros)
    env_dir = os.environ.get("LLAMA_MANAGER_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    # 3) Fallback duro
    return Path(__file__).resolve().parent.parent / "app_data"


_APP_DATA_DIR = _resolve_app_data_dir()
_DEFAULT_VOICES_DIR = _APP_DATA_DIR / "piper" / "voices"
PIPER_MODEL_DIR = Path(os.environ.get("PIPER_MODEL_DIR", str(_DEFAULT_VOICES_DIR)))

# Modelo por defecto si no se indica ninguna voz (id = nombre del archivo .onnx).
DEFAULT_VOICE = os.environ.get("PIPER_MODEL", "es_MX-claude-14947-epoch-high")
SAMPLE_RATE = 22050

# Parámetros de síntesis probados (estables, reducen variación tonal):
#   noise_scale=0.667, noise_w_scale=0.8 → voz más natural y estable
DEFAULT_NOISE_SCALE = 0.667
DEFAULT_NOISE_W_SCALE = 0.8
DEFAULT_VOLUME = 1.0

# Parámetros de streaming (aprendidos de piperstream.py):
#   - Fragmentos de ~150 caracteres para más contexto
#   - Crossfade de 20ms entre chunks para eliminar saltos tonales
#   - FIFO con numeración secuencial para garantizar orden estricto
CHAR_THRESHOLD = 150          # Fragmentos más largos para mejor contexto
TIMEOUT_SECONDS = 3          # Timeout para iniciar TTS si no hay suficientes caracteres
CROSSFADE_MS = 20            # Milisegundos de solapamiento entre chunks
CROSSFADE_SAMPLES = int(SAMPLE_RATE * CROSSFADE_MS / 1000)  # ~441 samples a 22050 Hz

# ---------------------------------------------------------------------------
# Whisper STT (local, con lazy loading)
# ---------------------------------------------------------------------------
# Modelo faster-whisper para transcripción local. Ruta por defecto:
#   app_data/piper/whisper/models/faster-whisper-base
_WHISPER_MODEL_DIR = _APP_DATA_DIR / "piper" / "whisper" / "models" / "faster-whisper-base"
WHISPER_MODEL_PATH = Path(os.environ.get("WHISPER_MODEL_PATH", str(_WHISPER_MODEL_DIR)))
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model():
    """Lazy-initialize (y cachea) el modelo Whisper local para STT."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper no está instalado.\nEjecuta: pip install faster-whisper"
            ) from e
        if not WHISPER_MODEL_PATH.exists():
            raise FileNotFoundError(
                f"Modelo Whisper no encontrado: {WHISPER_MODEL_PATH}. "
                "Coloca el modelo en app_data/piper/whisper/models/faster-whisper-base/"
            )
        logger.info("Cargando modelo Whisper %s (device=%s, compute_type=%s)",
                     WHISPER_MODEL_PATH, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
        _whisper_model = WhisperModel(
            str(WHISPER_MODEL_PATH),
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
        return _whisper_model


def _available_voices() -> list[str]:
    """Lista las voces Piper disponibles como ids (stem de cada .onnx)."""
    if not PIPER_MODEL_DIR.is_dir():
        return []
    voices = sorted(
        p.stem for p in PIPER_MODEL_DIR.glob("*.onnx")
        if (p.with_suffix(".onnx.json")).exists()
    )
    if not voices:
        voices = sorted(p.stem for p in PIPER_MODEL_DIR.glob("*.onnx") if p.stem)
    return voices


_piper_cache: dict[str, object] = {}

# PiperVoice NO es thread-safe: varias síntesis concurrentes (auto-speak dispara
# varios /synthesize/stream, cada uno en un hilo del pool vía run_in_executor)
# sobre la MISMA instancia Piper causan race/deadlock: la petición se cuelga con
# uso de CPU pero nunca completa y el servicio "cae". Este lock global serializa
# TODAS las llamadas a Piper (una a la vez), como hace piperstream.py de facto.
_SYNTH_LOCK = threading.Lock()


def _get_piper(voice: Optional[str] = None):
    """Lazy-initialize (y cachea) la voz Piper indicada (o la por defecto)."""
    global _piper_cache
    voice_id = (voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE

    cached = _piper_cache.get(voice_id)
    if cached is not None:
        return cached

    try:
        from piper import PiperVoice
    except ImportError as e:
        raise RuntimeError(
            "\u274c piper-tts no est\u00e1 instalado.\nEjecuta: pip install piper-tts"
        ) from e

    model_path = Path(voice_id)
    if not model_path.is_absolute():
        model_path = PIPER_MODEL_DIR / f"{voice_id}.onnx"
    json_path = Path(str(model_path) + ".json")

    if not model_path.exists():
        raise FileNotFoundError(
            f"Modelo Piper no encontrado: {model_path}. "
            f"Coloca el modelo en {PIPER_MODEL_DIR}/"
        )
    if not json_path.exists():
        raise FileNotFoundError(
            f"Config del modelo Piper no encontrada: {json_path}. "
            "Necesitas el archivo .json junto al modelo .onnx."
        )

    logger.info("Cargando voz Piper %s (%s)", voice_id, model_path)
    _piper_cache[voice_id] = PiperVoice.load(model_path, config_path=json_path)
    return _piper_cache[voice_id]


def _build_synthesis_config(
    speed: float = 1.0,
    noise_scale: Optional[float] = None,
    noise_w_scale: Optional[float] = None,
    length_scale: Optional[float] = None,
    volume: float = 1.0,
):
    """Construye un SynthesisConfig de Piper.

    - speed -> length_scale (inverso): velocidad 2.0 => length_scale 0.5 (m\u00e1s r\u00e1pido).
    - Los par\u00e1metros de ruido se usan si se pasan; si no, piper usa el default de la voz.
    """
    from piper import SynthesisConfig

    if length_scale is None:
        length_scale = (1.0 / speed) if (speed and speed > 0) else DEFAULT_LENGTH_SCALE
    else:
        length_scale = float(length_scale)

    return SynthesisConfig(
        length_scale=float(length_scale),
        noise_scale=float(noise_scale) if noise_scale is not None else DEFAULT_NOISE_SCALE,
        noise_w_scale=float(noise_w_scale) if noise_w_scale is not None else DEFAULT_NOISE_W_SCALE,
        volume=float(volume) if volume else 1.0,
    )


def _synthesize_to_wav_bytes(
    text: str,
    voice: Optional[str] = None,
    speed: float = 1.0,
    noise_scale: Optional[float] = None,
    noise_w_scale: Optional[float] = None,
    length_scale: Optional[float] = None,
    volume: float = 1.0,
) -> tuple[bytes, int]:
    """Sintetiza texto a bytes WAV (PCM 16-bit mono) + sample rate."""
    # Serializar el acceso a Piper: no es thread-safe. Si otro hilo está
    # sintetizando, este espera -> nunca corrompe / cuelga el modelo.
    with _SYNTH_LOCK:
        piper = _get_piper(voice)
        config = _build_synthesis_config(
            speed=speed,
            noise_scale=noise_scale,
            noise_w_scale=noise_w_scale,
            length_scale=length_scale,
            volume=volume,
        )
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            piper.synthesize_wav(text, wav, config)
        buffer.seek(0)
        return buffer.read(), int(getattr(piper.config, "sample_rate", 22050))

# ---------------------------------------------------------------------------
# Streaming FIFO + crossfade (inspirado en piperstream.py, adaptado a FastAPI/asyncio)
# ---------------------------------------------------------------------------
# Cola FIFO para chunks de audio (secuencia garantizada)
_audio_fifo: asyncio.Queue = asyncio.Queue()
_stop_stream_event = asyncio.Event()


def _crossfade_chunks(
    chunks_int16: list[np.ndarray],
    crossfade_samples: int = CROSSFADE_SAMPLES,
) -> np.ndarray:
    """Une chunks Int16 aplicando crossfade de N muestras entre ellos.

    Basado en piperstream.py: crossfade de 20ms (441 muestras a 22050 Hz)
    para eliminar saltos tonales entre chunks de síntesis.
    """
    if not chunks_int16:
        return np.array([], dtype=np.int16)

    result = chunks_int16[0].copy()
    for chunk in chunks_int16[1:]:
        fade_len = min(crossfade_samples, len(result), len(chunk))
        if fade_len > 0:
            # Ventanas de fading: final del resultado anterior se desvanece
            fade_out = np.linspace(1, 0, fade_len, dtype=np.float64)
            fade_in = np.linspace(0, 1, fade_len, dtype=np.float64)
            # Solapamiento: resultado[-fade_len:] * fade_out + chunk[:fade_len] * fade_in
            overlap = (
                (result[-fade_len:].astype(np.float64) * fade_out)
                + (chunk[:fade_len].astype(np.float64) * fade_in)
            ).astype(np.int16)
            result = np.concatenate([result[:-fade_len], overlap, chunk[fade_len:]])
        else:
            result = np.concatenate([result, chunk])

    return result


def _int16_to_base64_wav(
    audio_int16: np.ndarray,
    sample_rate: int = SAMPLE_RATE,
) -> tuple[str, int]:
    """Convierte un array Int16 a base64 WAV (un solo chunk o secuencia unificada)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(audio_int16.tobytes())
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("ascii"), sample_rate


def _synthesize_streaming_chunks(
    text: str,
    voice: str | None = None,
    speed: float = 1.0,
    noise_scale: float | None = None,
    noise_w_scale: float | None = None,
    length_scale: float | None = None,
    volume: float = DEFAULT_VOLUME,
) -> tuple[list[np.ndarray], int]:
    """Sintetiza texto y devuelve una lista de chunks Int16 + sample rate.

    Usa piper.synthesize() (generator) para obtener chunks individuales,
    aplicando los parámetros probados de piperstream.py:
    - noise_scale=0.667, noise_w_scale=0.8 (estables)
    - length_scale = 1/speed (inverso de velocidad)
    - volume=1.0
    """
    piper = _get_piper(voice)
    config = _build_synthesis_config(
        speed=speed,
        noise_scale=noise_scale if noise_scale is not None else DEFAULT_NOISE_SCALE,
        noise_w_scale=noise_w_scale if noise_w_scale is not None else DEFAULT_NOISE_W_SCALE,
        length_scale=length_scale,
        volume=volume,
    )

    chunks: list[np.ndarray] = []
    sample_rate = SAMPLE_RATE

    # OJO: _synthesize_streaming_chunks NO se usa en /synthesize/stream (que va por
    # _synthesize_to_wav_bytes, ya serializado). Este generador se documenta pero
    # igualmente serializa por si otro camino lo usa.
    try:
        # Piper no es thread-safe: serializar la síntesis completa de este chunk.
        with _SYNTH_LOCK:
            for chunk in piper.synthesize(text, config):
                if hasattr(chunk, "audio_int16_bytes") and chunk.audio_int16_bytes is not None:
                    chunk_bytes = chunk.audio_int16_bytes
                elif hasattr(chunk, "_audio_int16_bytes") and chunk._audio_int16_bytes is not None:
                    chunk_bytes = chunk._audio_int16_bytes
                else:
                    # float → int16
                    float_array = chunk.audio_float_array
                    int16_array = (np.asarray(float_array) * 32767).astype(np.int16)
                    chunk_bytes = int16_array.tobytes()

                sr = getattr(chunk, "sample_rate", SAMPLE_RATE)
                sample_rate = sr
                audio_int16 = np.frombuffer(chunk_bytes, dtype=np.int16).copy()
                chunks.append(audio_int16)
    except Exception:
        pass

    return chunks, sample_rate


def _synthesize_bulk(
    text: str,
    voice: str | None = None,
    speed: float = 1.0,
    noise_scale: float | None = None,
    noise_w_scale: float | None = None,
    length_scale: float | None = None,
    volume: float = DEFAULT_VOLUME,
) -> tuple[str, int]:
    """Modo manual/bocina: síntesis completa SIN streaming, una vez termina el modelo.

    Usa _synthesize_to_wav_bytes existente para compatibilidad directa.
    """
    wav_bytes, sr = _synthesize_to_wav_bytes(
        text=text,
        voice=voice,
        speed=speed,
        noise_scale=noise_scale,
        noise_w_scale=noise_w_scale,
        length_scale=length_scale,
        volume=volume,
    )
    return base64.b64encode(wav_bytes).decode("ascii"), sr


# SSE response helper
def event_stream_response(
    generate: callable,
) -> StreamingResponse:
    """Helper para responder con Server-Sent Events (SSE)."""

    async def _event_generator():
        try:
            async for event in generate():
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # IMPORTANTE: Connection: close (NO keep-alive). Si el servidor deja la
            # conexión viva tras terminar el SSE, y el navegador dejó de leer (el
            # front hace break al llegar {"done":true} sin cancelar el reader), esa
            # conexión se cuenta como ACTIVA contra el pool del navegador (~6 por
            # host). Al llenarse el pool, /health y los siguientes /synthesize/stream
            # quedan bloqueados => "Piper no disponible" falso y botón enganchado
            # hasta F5 (que cierra todas las conexiones). Con Connection: close el
            # servidor cierra el socket al terminar el cuerpo y el navegador lo purga.
            "Connection": "close",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Streaming synthesis (FIFO + crossfade)
# ---------------------------------------------------------------------------


def _split_text(text, threshold=CHAR_THRESHOLD):
    """Divide texto en fragmentos respetando puntuación y longitud mínima."""
    text = text.strip()
    if not text:
        return []

    import re
    sentences = re.split(r'(?<=[.!?\n])\s+', text)
    chunks = []
    current = ""

    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(current) + len(sent) + 1 <= threshold:
            current = f"{current} {sent}".strip() if current else sent
        else:
            if current:
                chunks.append(current)
            if len(sent) > threshold:
                parts = re.split(r'(?<=,)\s+', sent)
                for part in parts:
                    part = part.strip()
                    if part:
                        chunks.append(part)
            else:
                current = sent

    if current:
        chunks.append(current)
    return chunks

async def _synthesize_streaming(
    text,
    voice=None,
    speed=1.0,
    noise_scale=None,
    noise_w_scale=None,
    length_scale=None,
    volume=1.0,
):
    """Generador asincrono que emite chunks de audio PCM con crossfade.

    Yields dicts: {"audio": base64_chunk, "sample_rate": N, "seq": int}
    """
    piper = _get_piper(voice)
    config = _build_synthesis_config(
        speed=speed,
        noise_scale=noise_scale,
        noise_w_scale=noise_w_scale,
        length_scale=length_scale,
        volume=volume,
    )
    sample_rate = int(getattr(piper.config, "sample_rate", 22050))
    crossfade_samples = int(sample_rate * CROSSFADE_MS / 1000)

    chunks = _split_text(text)
    prev_tail = None
    seq = 0

    for chunk_text in chunks:
        if not chunk_text.strip():
            continue
        # Síntesis del fragmento (sincrónica, ejecutar en thread para no bloquear)
        loop = asyncio.get_event_loop()
        wav_bytes, _sr = await loop.run_in_executor(
            None,
            _synthesize_to_wav_bytes,
            chunk_text, voice, speed, noise_scale, noise_w_scale, length_scale, volume,
        )
        # Extraer PCM del WAV (saltar header de 44 bytes)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            pcm_data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).copy()

        if len(pcm_data) == 0:
            continue

        # Crossfade inline (piperstream.py): solapar final del chunk anterior
        # con el inicio del actual para eliminar saltos tonales.
        if prev_tail is not None and len(prev_tail) > 0:
            fade_len = min(crossfade_samples, len(prev_tail), len(pcm_data))
            if fade_len > 0:
                fade_out = np.linspace(1, 0, fade_len, dtype=np.float64)
                fade_in = np.linspace(0, 1, fade_len, dtype=np.float64)
                overlap = (
                    (prev_tail[-fade_len:].astype(np.float64) * fade_out)
                    + (pcm_data[:fade_len].astype(np.float64) * fade_in)
                ).astype(np.int16)
                pcm_data = np.concatenate([overlap, pcm_data[fade_len:]])

        # Guardar cola para el siguiente crossfade
        prev_tail = pcm_data[-crossfade_samples:] if len(pcm_data) > crossfade_samples else pcm_data

        # Emitir chunk
        yield {
            "audio": base64.b64encode(pcm_data.tobytes()).decode("ascii"),
            "sample_rate": sample_rate,
            "seq": seq,
        }
        seq += 1


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Piper TTS Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check."""

    try:
        voice_id = DEFAULT_VOICE
        model_path = Path(voice_id)
        if not model_path.is_absolute():
            model_path = PIPER_MODEL_DIR / f"{voice_id}.onnx"
        available = _available_voices()
        return {
            "status": "ok" if model_path.exists() else "degraded",
            "voices_available": len(available) > 0,
            "voice": voice_id,
            "voices": available,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/voices")
async def list_voices():
    """Lista de voces disponibles (compatibilidad con la UI)."""
    try:
        return {"voices": _available_voices()}
    except Exception as e:
        logger.error("Failed to list voices: %s", e)
        return {"voices": [], "error": str(e)}


@app.get("/v1/voices")
async def list_voices_v1():
    return await list_voices()


@app.get("/v1/audio/voices")
async def list_audio_voices_v1():
    return await list_voices()


class SynthesizeRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    speed: float = 1.0
    noise_scale: Optional[float] = None
    noise_w_scale: Optional[float] = None
    length_scale: Optional[float] = None
    volume: float = 1.0


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest):
    """Sintetiza texto a audio y lo devuelve como WAV en base64."""
    try:
        wav_bytes, sr = _synthesize_to_wav_bytes(
            text=req.text,
            voice=req.voice,
            speed=req.speed,
            noise_scale=req.noise_scale,
            noise_w_scale=req.noise_w_scale,
            length_scale=req.length_scale,
            volume=req.volume,
        )
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        return {"audio": b64, "sample_rate": sr}
    except Exception as e:
        logger.error("Synthesis failed: %s", e)
        return {"error": str(e)}


@app.post("/synthesize/stream")
async def synthesize_stream(req: SynthesizeRequest):
    """Sintetiza texto a audio en streaming (SSE) con chunks y crossfade.

    Emite eventos: {"audio": base64_pcm_chunk, "sample_rate": N, "seq": int}
    y al final: {"done": True}
    """
    async def _generate():
        try:
            async for chunk in _synthesize_streaming(
                text=req.text,
                voice=req.voice,
                speed=req.speed,
                noise_scale=req.noise_scale,
                noise_w_scale=req.noise_w_scale,
                length_scale=req.length_scale,
                volume=req.volume,
            ):
                yield chunk
            yield {"done": True}
        except Exception as e:
            logger.error("Streaming synthesis failed: %s", e)
            yield {"type": "error", "message": str(e)}

    return event_stream_response(_generate)


# ---------------------------------------------------------------------------
# Energy-based VAD
# ---------------------------------------------------------------------------

class EnergyVAD:
    """Detector de actividad de voz por energ\u00eda (PCM 16kHz mono, 16-bit).

    Trabaja en TIEMPO (ms), no en frames: el cliente puede mandar frames de
    cualquier tama\u00f1o (128 muestras con AudioWorklet, 480 con ScriptProcessor,
    960 te\u00f3ricos...). Antes, con frames de 128 muestras (8ms) el hangover de
    "10 frames" eran 80ms reales y se cerraban utterances a mitad de frase.
    Adem\u00e1s guarda ~PRE_ROLL_MS de audio previo al speech_start, para no
    perder el inicio de la frase mientras se acumula min_speech_ms.
    """

    PRE_ROLL_MS = 400

    def __init__(
        self,
        threshold: float = 0.02,
        hangover_ms: int = 300,
        min_speech_ms: int = 120,
        sample_rate: int = 16000,
    ):
        self.threshold = threshold
        self.sample_rate = sample_rate
        self.hangover_ms = max(60, min(2000, int(hangover_ms)))
        self.min_speech_ms = max(30, min(2000, int(min_speech_ms)))

        self._speech_ms = 0.0
        self._hangover_ms = 0.0
        self._is_speaking = False
        self._pre_roll: list[bytes] = []
        self._pre_roll_samples = 0
        self._utterance: list[bytes] = []

    def reset(self):
        self._speech_ms = 0.0
        self._hangover_ms = 0.0
        self._is_speaking = False
        self._pre_roll = []
        self._pre_roll_samples = 0
        self._utterance = []

    def update_config(self, threshold=None, hangover_frames=None, min_speech_frames=None):
        if threshold is not None:
            self.threshold = threshold
        if hangover_frames is not None:
            try:
                hv = int(hangover_frames)
            except (TypeError, ValueError):
                hv = self.hangover_ms
            # Compat: el frontend manda hangover en ms (p.ej. 300). Valores
            # <= 50 se interpretan como frames de ~30ms (comportamiento viejo).
            if hv <= 50:
                hv = hv * 30
            self.hangover_ms = max(60, min(2000, hv))
        if min_speech_frames is not None:
            try:
                ms = int(min_speech_frames)
            except (TypeError, ValueError):
                ms = self.min_speech_ms
            # Compat: frames de ~30ms (valores chicos) o ms directos.
            if ms < 100:
                ms = ms * 30
            self.min_speech_ms = max(30, min(2000, ms))
        logger.info(
            "VAD config: threshold=%.4f hangover_ms=%d min_speech_ms=%d",
            self.threshold, self.hangover_ms, self.min_speech_ms,
        )

    def process_frame(self, pcm_bytes: bytes) -> dict:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) == 0:
            return self._result(0.0, False, False, False)

        frame_ms = 1000.0 * len(samples) / self.sample_rate
        rms = float(np.sqrt(np.mean(samples ** 2)))
        score = min(1.0, rms / 0.15)

        speech_frame = rms > self.threshold
        speech_start = False
        speech_end = False

        if speech_frame:
            self._hangover_ms = 0.0
            self._speech_ms += frame_ms
            if not self._is_speaking and self._speech_ms >= self.min_speech_ms:
                self._is_speaking = True
                speech_start = True
                # El utterance arranca con el pre-roll (~400ms previos)
                self._utterance = list(self._pre_roll)
                self._pre_roll = []
                self._pre_roll_samples = 0
            if self._is_speaking:
                self._utterance.append(pcm_bytes)
        else:
            if self._is_speaking:
                self._utterance.append(pcm_bytes)
                self._hangover_ms += frame_ms
                if self._hangover_ms >= self.hangover_ms:
                    self._is_speaking = False
                    self._speech_ms = 0.0
                    self._hangover_ms = 0.0
                    speech_end = True
            else:
                self._speech_ms = 0.0
                # Acumular pre-roll durante el silencio
                self._pre_roll.append(pcm_bytes)
                self._pre_roll_samples += len(samples)
                cap = int(self.PRE_ROLL_MS * self.sample_rate / 1000)
                while self._pre_roll and self._pre_roll_samples > cap:
                    dropped = self._pre_roll.pop(0)
                    self._pre_roll_samples -= len(dropped) // 2  # int16 = 2 bytes

        return self._result(score, self._is_speaking, speech_start, speech_end)

    def take_utterance(self) -> bytes:
        """Devuelve el PCM acumulado del utterance (incluye pre-roll) y lo limpia."""
        pcm = b"".join(getattr(self, "_utterance", []))
        self._utterance = []
        return pcm

    def _result(self, score, is_speaking, speech_start, speech_end):
        return {
            "score": round(score, 4),
            "speech_detected": is_speaking,
            "speech_start": speech_start,
            "speech_end": speech_end,
        }
# ---------------------------------------------------------------------------
# WebSocket Call Mode
# ---------------------------------------------------------------------------

@app.websocket("/ws/call")
async def ws_call(websocket: WebSocket):
    """
    Canal de llamada bidireccional.

    Cliente env\u00eda:
      - Frames binarios: PCM bruto 16kHz mono 16-bit (cada frame ~30ms = 960 bytes)
      - Frames de texto: mensajes JSON de configuraci\u00f3n

    Servidor env\u00eda:
      - {"type": "vad_score", "score": 0.95}
      - {"type": "speech_start"}
      - {"type": "speech_end", "audio": "<base64 WAV>"}
      - {"type": "transcript", "role": "...", "text": "..."}
      - {"type": "tts_playing"} / {"type": "tts_stopped"}
      - {"type": "tts_audio", "audio": "<base64 WAV>", "sample_rate": N}
      - {"type": "error", "message": "..."}
    """
    await websocket.accept()
    logger.info("WebSocket call connected")

    vad = EnergyVAD()
    is_tts_playing = False
    send_lock = asyncio.Lock()
    # Estado para no saturar el WS con vad_score (~125 msg/s con frames de 8ms)
    _last_vad_send = [0.0]
    _last_vad_score = [0.0]
    call_session: dict = {
        "history": [],
        "voice": DEFAULT_VOICE,
        "speed": 1.0,
        "lang": "es",
        "stt_language": "es",
        "stt_enabled": True,
        "llm_enabled": True,
    }

    async def send_event(data: dict):
        async with send_lock:
            try:
                await websocket.send_text(json.dumps(data))
            except Exception:
                pass

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.receive":
                if "text" in message and message["text"]:
                    try:
                        config = json.loads(message["text"])
                        msg_type = config.get("type", "")

                        if msg_type == "config":
                            vad.update_config(
                                threshold=config.get("threshold"),
                                hangover_frames=config.get("hangover"),
                                min_speech_frames=config.get("min_speech_duration_frames"),
                            )
                            if config.get("voice"):
                                call_session["voice"] = config["voice"]
                            if config.get("speed") is not None:
                                call_session["speed"] = float(config["speed"])
                            if config.get("lang"):
                                call_session["lang"] = str(config["lang"])
                            _stt_lang = config.get("stt_language") or config.get("sttLanguage")
                            if _stt_lang:
                                call_session["stt_language"] = str(_stt_lang)
                            if config.get("stt_enabled") is not None:
                                call_session["stt_enabled"] = bool(config["stt_enabled"])
                            if config.get("llm_enabled") is not None:
                                call_session["llm_enabled"] = bool(config["llm_enabled"])
                            await send_event({"type": "config_ack", "ok": True})

                        elif msg_type == "tts_playing":
                            is_tts_playing = True
                            vad.reset()
                            speech_buffer.clear()

                        elif msg_type == "tts_stopped":
                            is_tts_playing = False

                        elif msg_type == "synthesize":
                            await _handle_ws_synthesize(websocket, config, send_event)

                        elif msg_type == "stop":
                            break

                    except json.JSONDecodeError:
                        pass

                if "bytes" in message and message["bytes"]:
                    if is_tts_playing:
                        continue  # Supresi\u00f3n de eco

                    pcm_data = message["bytes"]
                    result = vad.process_frame(pcm_data)

                    # Enviar el score solo si cambió lo suficiente (máx ~10/s)
                    _now = time.monotonic()
                    if (
                        _now - _last_vad_send[0] >= 0.10
                        or abs(result["score"] - _last_vad_score[0]) > 0.1
                    ):
                        _last_vad_send[0] = _now
                        _last_vad_score[0] = result["score"]
                        await send_event({"type": "vad_score", "score": result["score"]})

                    if result["speech_start"]:
                        await send_event({"type": "speech_start"})
                    elif result["speech_end"]:
                        pcm_full = vad.take_utterance()
                        # Ignorar "utterances" de ruido < ~100ms de audio real
                        if len(pcm_full) >= 3200:
                            wav_bytes = _wav_bytes_from_pcm(pcm_full)
                            await send_event({
                                "type": "speech_end",
                                "audio": base64.b64encode(wav_bytes).decode("ascii"),
                            })
                            asyncio.create_task(_call_pipeline(send_event, wav_bytes, call_session))
                        else:
                            logger.info("Utterance descartado por corto: %d bytes", len(pcm_full))

    except WebSocketDisconnect:
        logger.info("WebSocket call disconnected")
    except Exception as e:
        logger.error("WebSocket call error: %s", e)
        try:
            await send_event({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        logger.info("WebSocket call session ended")
# ---------------------------------------------------------------------------
# STT + LLM helpers (llama-server en 8081)
# ---------------------------------------------------------------------------

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8081")
CALL_SYSTEM_PROMPT = (
    "Eres un asistente de voz. Responde de forma breve, clara y conversacional "
    "(m\u00e1ximo 2-3 oraciones), en el mismo idioma del usuario."
)


_HALLUCINATION_PHRASES = (
    "gracias por ver este video",
    "gracias por ver el video",
    "suscribete",
    "suscríbete",
    "subtitulos",
    "subtítulos",
    "amara.org",
    "transcrito por",
    "www.",
    "http",
)


def _looks_like_hallucination(text: str) -> bool:
    """Detecta textos tipicos inventados por Whisper en silencios/ruido."""
    t = (text or "").strip()
    if not t:
        return True
    letters = sum(1 for c in t if c.isalpha())
    if letters < 2:
        return True
    low = t.lower()
    return any(p in low for p in _HALLUCINATION_PHRASES)


async def _transcribe_wav(wav_bytes: bytes, language=None) -> str:
    """Transcribe el WAV usando el modelo Whisper local (faster-whisper)."""
    try:
        model = _get_whisper_model()
    except Exception as e:
        logger.error("Whisper model not available: %s", e)
        raise

    # faster-whisper es síncrono: ejecutar en thread para no bloquear el loop
    lang = (language or "es").strip().lower() or "es"
    _alias = {"chi": "zh", "chinese": "zh", "mandarin": "zh",
              "espanol": "es", "spanish": "es",
              "english": "en", "french": "fr"}
    lang = _alias.get(lang, lang)
    _prompts = {
        "es": "El siguiente audio esta en espanol.",
        "en": "The following audio is in English.",
        "fr": "Le fichier audio suivant est en francais.",
    }
    _prompt = _prompts.get(lang, "The following audio is in " + lang + ".")

    def _transcribe_sync():
        # Escribir WAV a archivo temporal (faster-whisper lee rutas)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(wav_bytes)
        try:
            segments, info = model.transcribe(
                tmp_path,
                language=lang,  # Idioma forzado desde la UI: sin autodeteccion
                task="transcribe",  # Nunca traducir: transcribir tal cual
                beam_size=5,
                best_of=5,
                temperature=[0.0, 0.2, 0.4],
                compression_ratio_threshold=2.4,
                hallucination_silence_threshold=1.0,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 500,
                    "threshold": 0.5,
                    "min_speech_duration_ms": 250,
                },
                no_speech_threshold=0.9,  # Mas alto: requiere mas confianza para decir "no hay voz"
                log_prob_threshold=-2.0,  # Más bajo: acepta transcripciones con menos confianza
                condition_on_previous_text=False,  # Evita que el contexto previo cause alucinaciones
                initial_prompt=_prompt,  # Prompt inicial para guiar
            )
            kept: list[str] = []
            for seg in segments:
                avg_lp = getattr(seg, "avg_logprob", None)
                no_sp = getattr(seg, "no_speech_prob", None)
                if avg_lp is not None and avg_lp < -2.0:
                    continue
                if no_sp is not None and no_sp > 0.9:
                    continue
                txt = (seg.text or "").strip()
                if not txt:
                    continue
                import re as _re
                words = _re.findall(r"[\w]+", txt.lower())
                if words and len(set(words)) == 1 and len(words) >= 3:
                    continue
                kept.append(txt)
            text = " ".join(kept).strip()
            logger.info("STT: '%s' (lang=%.2f, dur=%.1fs)", text[:80], info.language_probability, info.duration)
            if _looks_like_hallucination(text):
                logger.warning("STT descartado como alucinacion: '%s'", text[:80])
                return ""
            return text
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return await asyncio.to_thread(_transcribe_sync)


async def _llm_chat(user_text: str, history: list) -> str:
    """Env\u00eda el texto al chat completions del llama-server y devuelve la respuesta."""
    import httpx
    url = f"{LLAMA_SERVER_URL}/v1/chat/completions"
    messages = [{"role": "system", "content": CALL_SYSTEM_PROMPT}]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, json={
            "messages": messages,
            "temperature": 0.6,
            "max_tokens": 512,
            "stream": False,
        })
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


def _wav_bytes_from_pcm(pcm_full: bytes) -> bytes:
    samples = np.frombuffer(pcm_full, dtype=np.int16).astype(np.float32) / 32768.0
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        sf.write(tmp.name, samples, 16000)
        tmp.seek(0)
        wav_bytes = tmp.read()
    os.unlink(tmp.name)
    return wav_bytes


async def _handle_ws_synthesize(websocket: WebSocket, config: dict, send_event):
    """Sintetiza un texto bajo demanda dentro de una sesion de llamada.
    Usa streaming FIFO con crossfade para reproduccion en tiempo real.
    """
    try:
        text = config.get("text", "")
        voice = config.get("voice")
        speed = config.get("speed", 1.0)
        noise_scale = config.get("noise_scale")
        noise_w_scale = config.get("noise_w_scale")
        length_scale = config.get("length_scale")
        volume = config.get("volume", 1.0)

        if not text:
            await send_event({"type": "error", "message": "Empty text for synthesis"})
            return

        await send_event({"type": "tts_playing"})
        async for chunk in _synthesize_streaming(
            text=text,
            voice=voice if isinstance(voice, str) else None,
            speed=float(speed),
            noise_scale=noise_scale,
            noise_w_scale=noise_w_scale,
            length_scale=length_scale,
            volume=float(volume),
        ):
            await send_event({
                "type": "tts_chunk",
                "audio": chunk["audio"],
                "sample_rate": chunk["sample_rate"],
                "seq": chunk["seq"],
            })
        await send_event({"type": "tts_stopped"})

    except Exception as e:
        logger.error("Synthesize failed: %s", e)
        await send_event({"type": "error", "message": f"Synthesis error: {e}"})


async def _call_pipeline(send_event, wav_bytes: bytes, session: dict):
    """STT -> LLM -> TTS para un turno de voz del modo llamada."""
    try:
        # 0. Normalizar volumen: muchos micrófonos llegan a picos ~1000/32768
        # (-30 dBFS) y Whisper devuelve vacío/alucinaciones con audio tan bajo.
        # OJO: wav_bytes es un WAV COMPLETO (header RIFF + datos). Leerlo con
        # np.frombuffer incluye el header como si fueran muestras: los bytes
        # "RIFF..." producen un pico falso ~0.55 y el umbral peak<0.25 nunca se
        # cumple => el gain era código muerto. Decodificar con soundfile.
        try:
            samples, _sr_norm = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            if samples.ndim > 1:
                samples = samples.mean(axis=1)
            samples = np.asarray(samples, dtype=np.float32).flatten()
            peak = float(np.max(np.abs(samples))) if samples.size else 0.0
            if 0.0 < peak < 0.25:
                gain = min(10.0, 0.85 / peak)  # normalizar a ~-1.4 dBFS
                samples = np.clip(samples * gain, -1.0, 1.0)
                logger.info("STT gain aplicado: peak=%.4f -> x%.2f", peak, gain)
                wav_bytes = _wav_bytes_from_pcm(
                    (samples * 32767.0).astype(np.int16).tobytes()
                )
        except Exception as e:
            logger.warning("No se pudo normalizar el audio: %s", e)

        # 1. STT
        try:
            user_text = await _transcribe_wav(wav_bytes, session.get("stt_language", "es"))
        except Exception as e:
            logger.error("STT failed: %s", e)
            await send_event({"type": "error", "message": f"STT no disponible: {e}"})
            return
        if not user_text:
            # Avisar a la UI: sin esto queda en 'escuchando' sin feedback
            await send_event({"type": "transcript", "role": "user", "text": ""})
            await send_event({
                "type": "error",
                "message": "No se entendió el audio. Acércate al micrófono y habla más fuerte.",
            })
            return
        await send_event({"type": "transcript", "role": "user", "text": user_text})

        # 2. LLM (si est\u00e1 habilitado)
        if session.get("llm_enabled", True):
            try:
                reply = await _llm_chat(user_text, session.get("history", []))
            except Exception as e:
                logger.error("LLM failed: %s", e)
                await send_event({"type": "error", "message": f"LLM no disponible: {e}"})
                return
            if not reply:
                return
            history = session.setdefault("history", [])
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": reply})
            await send_event({"type": "transcript", "role": "assistant", "text": reply})

            # 3. TTS de la respuesta (streaming FIFO + crossfade)
            try:
                await send_event({"type": "tts_playing"})
                async for chunk in _synthesize_streaming(
                    text=reply,
                    voice=session.get("voice"),
                    speed=session.get("speed", 1.0),
                    noise_scale=session.get("noise_scale"),
                    noise_w_scale=session.get("noise_w_scale"),
                    length_scale=session.get("length_scale"),
                    volume=session.get("volume", 1.0),
                ):
                    await send_event({
                        "type": "tts_chunk",
                        "audio": chunk["audio"],
                        "sample_rate": chunk["sample_rate"],
                        "seq": chunk["seq"],
                    })
                await send_event({"type": "tts_stopped"})
            except Exception as e:
                logger.error("TTS streaming failed: %s", e)
                await send_event({"type": "error", "message": f"TTS streaming error: {e}"})
    except Exception as e:
        logger.error("Call pipeline error: %s", e)
        try:
            await send_event({"type": "error", "message": str(e)})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PIPER_PORT", 8880))
    logger.info("Starting Piper TTS service on port %d", port)
    # proxy_headers: respeta X-Forwarded-Proto/Host cuando nginx está delante
    # (necesario para que las URLs de audio/WS usen el dominio público y https).
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )