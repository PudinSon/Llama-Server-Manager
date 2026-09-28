"""Limitador de peticiones en memoria (endurecimiento, Bloque 8).

Ventana deslizante por clave (normalmente IP del cliente). No usa dependencias
externas ni persistencia: si el proceso se reinicia, los contadores se reinician,
que es exactamente lo que queremos para proteger el API de automatización de
ráfagas (webhooks, reintentos en bucle, clientes mal comportados).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

DEFAULT_LIMIT = 120
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_MAX_KEYS = 2000


class RateLimiter:
    """Ventana deslizante simple y thread-safe."""

    def __init__(self, limit: int = DEFAULT_LIMIT, window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self.limit = max(1, int(limit))
        self.window = max(0.001, float(window_seconds))
        self.max_keys = max(1, int(max_keys))
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> Tuple[bool, float]:
        """¿Permitida? Devuelve `(permitida, segundos_para_reintentar)`."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = self._hits[key] = deque()
            cutoff = now - self.window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = self.window - (now - bucket[0])
                return False, max(0.0, retry_after)
            bucket.append(now)
            if len(self._hits) > self.max_keys:
                self._prune_locked(now)
            return True, 0.0

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window
        for key in [k for k, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]:
            self._hits.pop(key, None)

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def get_rate_limiter(limit: int = DEFAULT_LIMIT,
                     window_seconds: float = DEFAULT_WINDOW_SECONDS) -> RateLimiter:
    """Devuelve (creando si hace falta) el limitador global del proceso."""
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = RateLimiter(limit=limit, window_seconds=window_seconds)
        return _limiter


def reset_rate_limiter() -> None:
    """Olvida el limitador global (tests)."""
    global _limiter
    with _limiter_lock:
        _limiter = None
