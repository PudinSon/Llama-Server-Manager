"""Dispatcher de tipo **cron**: tareas periódicas según un horario.

Es el tercer módulo enchufable sobre la misma cola. Un cron no detecta nada del
exterior: mira el reloj y, cuando toca, encola una tarea con su prompt. Sirve para
los resúmenes matutinos, los informes semanales o las limpiezas automáticas.

Registro
--------
La configuración vive en `user_data/automation_crons.json`. Cada entrada define:

===================  ==========================================================
`name`               identificador (slug: `a-z0-9_-`, 1-64)
`enabled`            si se dispara (por defecto `true`)
`schedule`           expresión cron de 5 campos: `min hora dom mes dow`
`every_seconds`      alternativa a `schedule` (mínimo 5 s)
`source`             etiqueta de la cola (por defecto `cron:<name>`)
`wake_prompt`        plantilla con `{date}`, `{time}`, `{weekday}`, `{name}`
`agent_template`     instrucciones fijas antepuestas al prompt
`requires_approval`  fuerza el gate humano para esta fuente
`priority`           prioridad en la cola (-1000..1000)
`dedupe`             evita repetir el mismo «slot» tras un reinicio (`true`)
===================  ==========================================================

`schedule` admite `*`, `*/n`, `a`, `a-b`, `a-b/n`, listas con comas, nombres de
mes y de día (`mon..sun`, `jan..dec`) y los macros `@hourly`, `@daily`,
`@weekly`, `@monthly`, `@yearly`. El día de la semana usa 0/7 = domingo.

Fiabilidad
----------
La deduplicación por «slot» (`source_ref = <name>:<minuto>`) hace que un reinicio
del servicio dentro del mismo minuto no vuelva a disparar la tarea, sin necesidad
de estado en memoria.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence

from .dispatcher_registry import BaseRegistry, is_kind_paused, render_template
from .event_queue import EventQueue

logger = logging.getLogger("cron_dispatcher")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [CRON] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

CONFIG_FILENAME = "automation_crons.json"
DEFAULT_TICK_SECONDS = 20.0
MIN_EVERY_SECONDS = 5
PARTIAL_DAY_LIMIT = 366 * 4

#: Índice = día de la semana cron (0 = domingo), igual que en la expresión.
WEEKDAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
#: Índice + 1 = mes (enero es 1).
MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

__all__ = [
    "CONFIG_FILENAME",
    "CronDispatcher",
    "CronSchedule",
    "CronScheduleError",
    "get_cron_dispatcher",
    "start_cron_dispatcher",
    "stop_cron_dispatcher",
    "reset_cron_dispatcher",
]


class CronScheduleError(ValueError):
    """Expresión de horario inválida."""


# ============================================================================
# Expresiones cron
# ============================================================================
def _parse_field(
    text: str,
    low: int,
    high: int,
    names: Optional[Sequence[str]] = None,
    sunday_alias: bool = False,
) -> FrozenSet[int]:
    """Convierte un campo cron (`*/5`, `1-3`, `1,4`, `mon-fri`) en un conjunto."""
    text = str(text or "").strip().lower()
    if not text:
        raise CronScheduleError("campo vacío")

    def _value(token: str) -> int:
        token = token.strip().lower()
        if names:
            if token in names:
                return names.index(token) + (low if not sunday_alias else 0)
            if sunday_alias and token.startswith("sun"):
                return 0
        try:
            number = int(token)
        except ValueError as exc:
            raise CronScheduleError(f"valor no numérico: {token!r}") from exc
        if sunday_alias and number == 7:
            number = 0
        if not low <= number <= high:
            raise CronScheduleError(f"valor fuera de rango ({low}-{high}): {number}")
        return number

    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronScheduleError("lista con un elemento vacío")
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            try:
                step = int(raw_step)
            except ValueError as exc:
                raise CronScheduleError(f"paso inválido: {raw_step!r}") from exc
            if step <= 0:
                raise CronScheduleError("el paso debe ser positivo")
            part = part.strip()
        if part in {"*", ""}:
            start, end = low, high
        elif "-" in part[1:]:
            raw_start, _, raw_end = part.partition("-")
            start, end = _value(raw_start), _value(raw_end)
        else:
            start = end = _value(part)
        if start > end:
            raise CronScheduleError(f"rango invertido: {part}")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronScheduleError(f"campo sin valores: {text!r}")
    return frozenset(values)


@dataclass(frozen=True)
class CronSchedule:
    """Una expresión cron de 5 campos, ya interpretada."""

    minute: FrozenSet[int]
    hour: FrozenSet[int]
    day: FrozenSet[int]
    month: FrozenSet[int]
    weekday: FrozenSet[int]
    expression: str = ""

    @classmethod
    def parse(cls, expression: str) -> "CronSchedule":
        raw = str(expression or "").strip()
        if not raw:
            raise CronScheduleError("expresión vacía")
        normalized = _MACROS.get(raw.lower(), raw)
        fields = normalized.split()
        if len(fields) != 5:
            raise CronScheduleError(
                f"se esperan 5 campos (min hora dom mes dow), hay {len(fields)}"
            )
        minute, hour, day, month, weekday = fields
        return cls(
            minute=_parse_field(minute, 0, 59),
            hour=_parse_field(hour, 0, 23),
            day=_parse_field(day, 1, 31),
            month=_parse_field(month, 1, 12, names=MONTH_NAMES),
            weekday=_parse_field(weekday, 0, 6, names=WEEKDAY_NAMES, sunday_alias=True),
            expression=raw,
        )

    def _day_matches(self, moment: datetime) -> bool:
        """Día correcto, con la semántica clásica de dom/dow (OR si ambos limitan)."""
        day_restricted = len(self.day) < 31
        weekday_restricted = len(self.weekday) < 7
        day_ok = moment.day in self.day
        weekday_ok = ((moment.weekday() + 1) % 7) in self.weekday
        if day_restricted and weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def matches(self, moment: datetime) -> bool:
        return (
            moment.minute in self.minute
            and moment.hour in self.hour
            and moment.month in self.month
            and self._day_matches(moment)
        )

    def next_after(self, moment: datetime) -> Optional[datetime]:
        """Primer instante que cumple la expresión, a partir de `moment`."""
        current = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = current.date()
        midnight = datetime.min.time()
        for _ in range(PARTIAL_DAY_LIMIT):
            base = datetime.combine(day, midnight)
            if day.month in self.month and self._day_matches(base):
                for hour in sorted(self.hour):
                    for minute in sorted(self.minute):
                        candidate = base.replace(hour=hour, minute=minute)
                        if candidate >= current:
                            return candidate
            day += timedelta(days=1)
        return None


# ============================================================================
# Registro + bucle
# ============================================================================
class CronDispatcher(BaseRegistry):
    """Registro de horarios y bucle que los dispara."""

    config_filename = CONFIG_FILENAME
    source_prefix = "cron"
    label = "Cron"
    collection_key = "crons"
    default_fields = {
        "enabled": True,
        "schedule": "",
        "every_seconds": 0,
        "source": "",
        "wake_prompt": "",
        "agent_template": "",
        "requires_approval": None,
        "priority": 0,
        "dedupe": True,
    }

    def __init__(
        self,
        queue: Optional[EventQueue] = None,
        config_path: Optional[Path] = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        super().__init__(queue=queue, config_path=config_path)
        self.tick_seconds = max(1.0, float(tick_seconds))
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._runs_total = 0

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "CronDispatcher":
        if self.is_running:
            return self
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="cron-dispatcher", daemon=True)
        self._thread.start()
        logger.info(f"⏰ Cron iniciado ({self.tick_seconds}s por vuelta)")
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("⏰ Cron detenido")

    def _loop(self) -> None:
        while not self._stop_event.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - nunca debe matar el hilo
                logger.error(f"Error en el ciclo del cron: {exc}", exc_info=True)

    # ------------------------------------------------------------------
    # Disparo
    # ------------------------------------------------------------------
    def _validate_entry(self, data: Dict[str, Any]) -> None:
        schedule = str(data.get("schedule") or "").strip()
        try:
            every = int(data.get("every_seconds") or 0)
        except (TypeError, ValueError):
            raise ValueError("every_seconds debe ser un número de segundos")
        if not schedule and every <= 0:
            raise ValueError("define 'schedule' (p. ej. '0 8 * * *') o 'every_seconds'")
        if schedule:
            CronSchedule.parse(schedule)  # lanza CronScheduleError (ValueError)
        if every and every < MIN_EVERY_SECONDS:
            raise ValueError(f"every_seconds debe ser >= {MIN_EVERY_SECONDS}")

    def next_run_for(self, entry: Dict[str, Any], now: Optional[datetime] = None) -> Optional[str]:
        now = now or datetime.now()
        try:
            every = int(entry.get("every_seconds") or 0)
        except (TypeError, ValueError):
            every = 0
        if every > 0:
            last = entry.get("last_run")
            base = datetime.fromtimestamp(float(last)) if last else now
            nxt = base + timedelta(seconds=every)
            while nxt < now:
                nxt = now
            return nxt.replace(microsecond=0).isoformat(sep=" ")
        schedule = str(entry.get("schedule") or "").strip()
        if not schedule:
            return None
        try:
            nxt = CronSchedule.parse(schedule).next_after(now)
        except CronScheduleError:
            return None
        return nxt.isoformat(sep=" ", timespec="minutes") if nxt else None

    def _slot_ref(self, entry: Dict[str, Any], now: datetime) -> Optional[str]:
        """Identificador del «slot» actual, para no repetirlo tras un reinicio."""
        if not entry.get("dedupe", True):
            return None
        try:
            every = int(entry.get("every_seconds") or 0)
        except (TypeError, ValueError):
            every = 0
        if every > 0:
            return f"{entry['name']}:slot:{int(now.timestamp()) // every}"
        return f"{entry['name']}:{now.strftime('%Y%m%dT%H%M')}"

    def _is_due(self, entry: Dict[str, Any], now: datetime) -> bool:
        try:
            every = int(entry.get("every_seconds") or 0)
        except (TypeError, ValueError):
            every = 0
        if every > 0:
            last = entry.get("last_run")
            if not last:
                return True
            return (now - datetime.fromtimestamp(float(last))).total_seconds() >= every
        schedule = str(entry.get("schedule") or "").strip()
        if not schedule:
            return False
        try:
            parsed = CronSchedule.parse(schedule)
        except CronScheduleError:
            return False
        if not parsed.matches(now):
            return False
        last = entry.get("last_run")
        if last:
            last_dt = datetime.fromtimestamp(float(last))
            if last_dt.strftime("%Y%m%dT%H%M") == now.strftime("%Y%m%dT%H%M"):
                return False
        return True

    def _prompt(self, entry: Dict[str, Any], now: datetime) -> str:
        template = str(entry.get("wake_prompt") or "")
        extra = {
            "name": entry["name"],
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "weekday": WEEKDAY_NAMES[(now.weekday() + 1) % 7],
            "schedule": str(entry.get("schedule") or f"cada {entry.get('every_seconds')}s"),
        }
        if not template:
            return f"Ejecuta la rutina programada «{entry['name']}» ({extra['date']} {extra['time']})."
        return render_template(template, extra, extra)

    def tick(self, now: Optional[datetime] = None) -> List[int]:
        """Una vuelta del reloj: encola las entradas que tocan. Devuelve los ids.

        Si el dispatcher está en pausa global, la vuelta automática no hace nada.
        `run_now()` (disparo manual) ignora la pausa a propósito.
        """
        if is_kind_paused(self.source_prefix):
            return []
        now = now or datetime.now()
        with self._lock:
            entries = [dict(entry) for entry in self._entries.values()]
        enqueued: List[int] = []
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            if not self._is_due(entry, now):
                continue
            prompt = self._prompt(entry, now)
            payload = {
                "cron": entry["name"],
                "schedule": entry.get("schedule") or "",
                "every_seconds": entry.get("every_seconds") or 0,
                "fired_at": now.isoformat(sep=" ", timespec="seconds"),
            }
            try:
                task_id = self._enqueue(
                    entry,
                    payload=payload,
                    wake_prompt=prompt,
                    source_ref=self._slot_ref(entry, now),
                )
            except ValueError as exc:
                self._reject()
                logger.warning(f"⏰ No se pudo encolar «{entry['name']}»: {exc}")
                continue
            self._runs_total += 1
            enqueued.append(task_id)
            self.mark_run(entry["name"], task_id=task_id, status="enqueued", at=now.timestamp())
            logger.info(f"⏰ «{entry['name']}» → tarea {task_id}")
        return enqueued

    def run_now(self, name: str, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Dispara una entrada a mano (útil para probar sin esperar al horario)."""
        entry = self.get(name, include_secrets=True)
        if entry is None:
            raise KeyError(f"cron '{name}' no encontrado")
        now = now or datetime.now()
        task_id = self._enqueue(
            entry,
            payload={
                "cron": entry["name"],
                "manual": True,
                "fired_at": now.isoformat(sep=" ", timespec="seconds"),
            },
            wake_prompt=self._prompt(entry, now),
            source_ref=None,  # un disparo manual nunca debe deduplicarse
        )
        self._runs_total += 1
        self.mark_run(entry["name"], task_id=task_id, status="manual", at=now.timestamp())
        return {"success": True, "cron": entry["name"], "task_id": task_id}

    # ------------------------------------------------------------------
    # Introspección
    # ------------------------------------------------------------------
    def status(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = datetime.now()
        with self._lock:
            entries = [dict(entry) for entry in self._entries.values()]
        for entry in entries:
            entry["next_run"] = self.next_run_for(entry, now)
        payload = super().status(extra)
        payload["crons"] = [
            self._public(entry) for entry in sorted(entries, key=lambda item: item["name"])
        ]
        payload["running"] = self.is_running
        payload["tick_seconds"] = self.tick_seconds
        payload["runs_total"] = self._runs_total
        return payload


# ============================================================================
# Instancia global (una por proceso)
# ============================================================================
_dispatcher: Optional[CronDispatcher] = None
_lock = threading.Lock()


def get_cron_dispatcher(
    queue: Optional[EventQueue] = None,
    config_path: Optional[Path] = None,
) -> CronDispatcher:
    global _dispatcher
    with _lock:
        if _dispatcher is None:
            _dispatcher = CronDispatcher(queue=queue, config_path=config_path)
        return _dispatcher


def start_cron_dispatcher() -> Optional[CronDispatcher]:
    try:
        return get_cron_dispatcher().start()
    except Exception as exc:  # pragma: no cover
        logger.error(f"No se pudo iniciar el cron: {exc}", exc_info=True)
        return None


def stop_cron_dispatcher() -> None:
    global _dispatcher
    with _lock:
        dispatcher = _dispatcher
    if dispatcher is not None:
        dispatcher.stop()


def reset_cron_dispatcher() -> None:
    """Olvida la instancia global (tests)."""
    global _dispatcher
    with _lock:
        _dispatcher = None
