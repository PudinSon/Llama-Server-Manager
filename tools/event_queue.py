"""Cola persistente de eventos para la automatización 24/7 (Bloque 7).

Los eventos (correos, webhooks, ficheros vigilados, tareas manuales...) se
guardan en SQLite en vez de mantenerse en memoria. Así una tarea sobrevive a un
reinicio del proceso, a un corte de luz o a un cierre accidental de la UI.

El fichero vive en `user_data/automation.db` y se abre en modo WAL para que
varios hilos del MISMO proceso (dispatcher + worker pool) y, eventualmente,
varios procesos puedan leer y escribir sin bloquearse entre sí.

Puntos clave:

* `claim_next()` es **atómico**: usa `BEGIN IMMEDIATE` y una única sentencia
  `UPDATE` sobre la fila elegida, de modo que dos workers nunca ejecutan la
  misma tarea. Si no hay tareas pendientes devuelve `None`.
* `heartbeat()` marca que un worker sigue vivo. Un worker que muere deja el
  latido congelado y `requeue_stale()` devuelve esa tarea a `pending`.
* Los estados `done`/`failed`/`cancelled` son terminales.

Este módulo no depende de nada del resto del proyecto salvo `runtime` para
localizar `user_data/`, por lo que se puede importar y testear de forma aislada.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

logger = logging.getLogger("event_queue")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [QUEUE] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

DB_FILENAME = "automation.db"
DEFAULT_STALE_MINUTES = 15
DEFAULT_APPROVAL_TTL_MINUTES = 24 * 60
DEFAULT_PRUNE_DAYS = 7
# Límites de entrada: evitan que un evento mal formado o gigante tumbe la cola.
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_WAKE_PROMPT_CHARS = 8000
PRIORITY_MIN, PRIORITY_MAX = -1000, 1000
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Estados posibles de una tarea.
PENDING = "pending"
RUNNING = "running"
AWAITING_APPROVAL = "awaiting_approval"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({DONE, FAILED, CANCELLED})
ACTIVE_STATUSES = frozenset({PENDING, RUNNING, AWAITING_APPROVAL})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT    NOT NULL DEFAULT 'manual',
    source_ref        TEXT,
    payload           TEXT,
    wake_prompt       TEXT,
    agent_template    TEXT,
    priority          INTEGER NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   REAL,
    status            TEXT    NOT NULL DEFAULT 'pending',
    worker_id         TEXT,
    heartbeat         REAL,
    created_at        REAL    NOT NULL,
    started_at        REAL,
    finished_at       REAL,
    result            TEXT,
    error             TEXT,
    approval_token    TEXT,
    notified_os       INTEGER NOT NULL DEFAULT 0,
    notified_telegram INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_automation_claim
    ON automation_tasks (status, priority DESC, created_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_automation_source_ref
    ON automation_tasks (source, source_ref);
CREATE INDEX IF NOT EXISTS idx_automation_approval
    ON automation_tasks (approval_token);
CREATE INDEX IF NOT EXISTS idx_automation_next_attempt
    ON automation_tasks (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS automation_audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL    NOT NULL,
    action  TEXT    NOT NULL,
    task_id INTEGER,
    actor   TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_audit_at
    ON automation_audit (at DESC);
"""

# Columnas añadidas después de la primera versión del esquema. `CREATE TABLE IF
# NOT EXISTS` no las añade a una base ya creada, así que `_init_schema()` las
# agrega a mano cuando falta la migración.
_MIGRATION_COLUMNS = (
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("next_attempt_at", "REAL"),
)


def default_db_path() -> Path:
    """Ruta por defecto de la base de datos de la cola."""
    return Path(runtime.get_user_data_dir()) / DB_FILENAME


def _maybe_json(value: Any) -> Optional[str]:
    """Serializa `payload` sin reventar si trae algo no serializable."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _decode_json(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class EventQueue:
    """Cola persistente sobre SQLite (WAL) con reserva atómica de tareas."""

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Conexión / esquema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                existing = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(automation_tasks)")
                }
                for column, definition in _MIGRATION_COLUMNS:
                    if column not in existing:
                        conn.execute(
                            f"ALTER TABLE automation_tasks ADD COLUMN {column} {definition}"
                        )
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Escritura / lectura
    # ------------------------------------------------------------------
    def enqueue(
        self,
        source: str = "manual",
        payload: Any = None,
        wake_prompt: Optional[str] = None,
        agent_template: Optional[str] = None,
        priority: int = 0,
        source_ref: Optional[str] = None,
        status: str = PENDING,
        dedupe: bool = True,
    ) -> int:
        """Encola un evento y devuelve su id.

        Si `dedupe` y ya existe una tarea activa con el mismo
        `(source, source_ref)`, devuelve el id existente sin crear otra. Esto
        evita reencolar el mismo correo/fichero en cada sondeo.

        Endurecimiento: valida `source`, acota `priority` y rechaza payloads o
        prompts desmedidos (`ValueError`) para que un evento mal formado no
        contamine la cola.
        """
        source = str(source or "").strip() or "manual"
        if not _SOURCE_RE.match(source):
            raise ValueError(f"source inválido: {source!r}")
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 0
        priority = max(PRIORITY_MIN, min(PRIORITY_MAX, priority))
        payload_json = _maybe_json(payload)
        if payload_json is not None and len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload demasiado grande (> {MAX_PAYLOAD_BYTES} bytes)")
        if wake_prompt is not None:
            wake_prompt = str(wake_prompt)
            if len(wake_prompt) > MAX_WAKE_PROMPT_CHARS:
                raise ValueError(f"wake_prompt demasiado largo (> {MAX_WAKE_PROMPT_CHARS} chars)")

        if source_ref and dedupe:
            existing = self.find_by_source_ref(source, source_ref)
            if existing is not None:
                return int(existing["id"])

        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO automation_tasks
                        (source, source_ref, payload, wake_prompt, agent_template,
                         priority, status, created_at, heartbeat)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        source_ref,
                        payload_json,
                        wake_prompt,
                        agent_template,
                        priority,
                        str(status),
                        now,
                        now if status == RUNNING else None,
                    ),
                )
                task_id = int(cursor.lastrowid)
                # Auditoría en la MISMA conexión: `record_audit` tomaría el lock
                # otra vez y aquí ya lo tenemos.
                conn.execute(
                    "INSERT INTO automation_audit (at, action, task_id, actor, detail)\n"
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, "enqueue", task_id, source, None),
                )
                return task_id
            finally:
                conn.close()

    def claim_next(
        self,
        worker_id: str,
        sources: Optional[Iterable[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Reserva la siguiente tarea pendiente (atómico).

        Prioridad: `priority` descendente, luego `created_at` ascendente (FIFO
        dentro de la misma prioridad). Devuelve la tarea ya marcada como
        `running`, o `None` si no hay nada pendiente.
        """
        sources = list(sources) if sources else None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    params: List[Any] = [PENDING]
                    sql = "SELECT id FROM automation_tasks WHERE status = ?"
                    if sources:
                        placeholders = ",".join("?" for _ in sources)
                        sql += f" AND source IN ({placeholders})"
                        params.extend(sources)
                    # Reintentos con backoff: una tarea reprogramada no se
                    # reclama hasta que venza `next_attempt_at`.
                    sql += " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                    params.append(time.time())
                    sql += " ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1"
                    row = conn.execute(sql, params).fetchone()
                    if row is None:
                        conn.execute("COMMIT")
                        return None

                    task_id = int(row["id"])
                    now = time.time()
                    conn.execute(
                        """
                        UPDATE automation_tasks
                           SET status = ?, worker_id = ?, started_at = ?, heartbeat = ?,
                               next_attempt_at = NULL
                         WHERE id = ?
                        """,
                        (RUNNING, str(worker_id), now, now, task_id),
                    )
                    claimed = conn.execute(
                        "SELECT * FROM automation_tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        return self._row_to_task(claimed)

    def heartbeat(self, task_id: int, worker_id: Optional[str] = None) -> bool:
        """Renueva el latido de una tarea en ejecución."""
        sql = "UPDATE automation_tasks SET heartbeat = ? WHERE id = ? AND status = ?"
        params: List[Any] = [time.time(), int(task_id), RUNNING]
        if worker_id is not None:
            sql += " AND worker_id = ?"
            params.append(str(worker_id))
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(sql, params)
                return cursor.rowcount > 0
            finally:
                conn.close()

    def mark_awaiting_approval(self, task_id: int, approval_token: str) -> bool:
        """Pausa la tarea a la espera de aprobación humana."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE automation_tasks
                       SET status = ?, approval_token = ?, heartbeat = ?
                     WHERE id = ?
                    """,
                    (AWAITING_APPROVAL, str(approval_token), time.time(), int(task_id)),
                )
                return cursor.rowcount > 0
            finally:
                conn.close()

    def mark_approved(self, task_id: int) -> bool:
        """Libera una tarea en espera de aprobación para que se ejecute.

        El `approval_token` se conserva: la presencia de un token indica que la
        tarea ya fue aprobada, así el gate no vuelve a bloquearla.
        """
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE automation_tasks
                       SET status = ?, worker_id = NULL, started_at = NULL, heartbeat = NULL
                     WHERE id = ? AND status = ?
                    """,
                    (PENDING, int(task_id), AWAITING_APPROVAL),
                )
                return cursor.rowcount > 0
            finally:
                conn.close()

    def mark_rejected(self, task_id: int, reason: Any = None) -> bool:
        """Cancela una tarea en espera de aprobación (rechazada por el humano)."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE automation_tasks
                       SET status = ?, finished_at = ?, error = COALESCE(?, error)
                     WHERE id = ? AND status = ?
                    """,
                    (CANCELLED, time.time(), str(reason) if reason is not None else None,
                     int(task_id), AWAITING_APPROVAL),
                )
                return cursor.rowcount > 0
            finally:
                conn.close()

    def mark_done(self, task_id: int, result: Any = None) -> bool:
        return self._mark_terminal(task_id, DONE, result=result)

    def mark_failed(self, task_id: int, error: Any = None, result: Any = None) -> bool:
        return self._mark_terminal(task_id, FAILED, result=result, error=error)

    def mark_cancelled(self, task_id: int, reason: Any = None) -> bool:
        return self._mark_terminal(task_id, CANCELLED, error=reason)

    def _mark_terminal(
        self,
        task_id: int,
        status: str,
        result: Any = None,
        error: Any = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Estado no terminal: {status}")
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE automation_tasks
                       SET status = ?, finished_at = ?, heartbeat = ?,
                           result = COALESCE(?, result),
                           error = COALESCE(?, error)
                     WHERE id = ?
                    """,
                    (
                        status,
                        time.time(),
                        time.time(),
                        _maybe_json(result) if result is not None else None,
                        str(error) if error is not None else None,
                        int(task_id),
                    ),
                )
                return cursor.rowcount > 0
            finally:
                conn.close()

    def requeue_stale(self, timeout_minutes: float = DEFAULT_STALE_MINUTES) -> int:
        """Devuelve a `pending` las tareas cuyo worker murió.

        Solo toca tareas `running` con latido más viejo que `timeout_minutes`
        (o sin latido). Devuelve cuántas se requeuearon.
        """
        cutoff = time.time() - max(0.0, float(timeout_minutes)) * 60.0
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE automation_tasks
                       SET status = ?, worker_id = NULL, started_at = NULL, heartbeat = NULL,
                           error = COALESCE(error, ?)
                     WHERE status = ? AND (heartbeat IS NULL OR heartbeat < ?)
                    """,
                    (PENDING, "requeued: worker sin latido", RUNNING, cutoff),
                )
                count = cursor.rowcount
            finally:
                conn.close()
        if count:
            logger.warning(f"♻️  {count} tarea(s) requeueadas por latido vencido")
        return count

    def requeue_for_retry(
        self,
        task_id: int,
        delay_seconds: float = 0.0,
        error: Optional[str] = None,
    ) -> bool:
        """Reprograma una tarea fallida para reintentarla tras un backoff.

        Incrementa `attempts`, fija `next_attempt_at` (para que `claim_next()`
        la ignore hasta que venza) y limpia el worker/latido del intento
        anterior. Conserva `result` y `error` del intento previo como contexto.
        Devuelve `True` si la fila se actualizó.
        """
        now = time.time()
        delay = max(0.0, float(delay_seconds or 0.0))
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE automation_tasks
                       SET status = ?, worker_id = NULL, started_at = NULL, heartbeat = NULL,
                           finished_at = NULL, attempts = attempts + 1,
                           next_attempt_at = ?, error = COALESCE(?, error)
                     WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        PENDING,
                        now + delay,
                        str(error) if error is not None else None,
                        int(task_id),
                        FAILED,
                        RUNNING,
                    ),
                )
                return cursor.rowcount > 0
            finally:
                conn.close()

    def retry_delay_seconds(
        self,
        task_id: int,
        base_seconds: float = 30.0,
        max_seconds: float = 3600.0,
    ) -> float:
        """Backoff exponencial según los intentos ya consumidos por la tarea."""
        task = self.get_task(int(task_id)) or {}
        try:
            attempts = max(0, int(task.get("attempts") or 0))
        except (TypeError, ValueError):
            attempts = 0
        base = max(1.0, float(base_seconds))
        cap = max(base, float(max_seconds))
        return min(cap, base * (2 ** attempts))

    def expire_approvals(self, timeout_minutes: float = DEFAULT_APPROVAL_TTL_MINUTES) -> int:
        """Cancela aprobaciones que llevan demasiado tiempo pendientes.

        Un token de aprobación no puede quedar válido para siempre: si nadie
        decide en `timeout_minutes`, la tarea se cancela (endurecimiento).
        """
        cutoff = time.time() - max(0.0, float(timeout_minutes)) * 60.0
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE automation_tasks
                       SET status = ?, finished_at = ?, heartbeat = ?,
                           error = COALESCE(error, ?)
                     WHERE status = ? AND created_at < ?
                    """,
                    (CANCELLED, time.time(), time.time(),
                     "aprobación caducada sin respuesta", AWAITING_APPROVAL, cutoff),
                )
                count = cursor.rowcount
            finally:
                conn.close()
        if count:
            logger.info(f"⌛ {count} aprobación(es) caducada(s) sin respuesta")
        return count

    def set_notified(
        self,
        task_id: int,
        os_notified: Optional[bool] = None,
        telegram_notified: Optional[bool] = None,
    ) -> bool:
        """Marca qué canales de notificación ya avisaron de esta tarea."""
        fields: List[str] = []
        params: List[Any] = []
        if os_notified is not None:
            fields.append("notified_os = ?")
            params.append(1 if os_notified else 0)
        if telegram_notified is not None:
            fields.append("notified_telegram = ?")
            params.append(1 if telegram_notified else 0)
        if not fields:
            return False
        params.append(int(task_id))
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    f"UPDATE automation_tasks SET {', '.join(fields)} WHERE id = ?", params
                )
                return cursor.rowcount > 0
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------------
    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM automation_tasks WHERE id = ?", (int(task_id),)
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_task(row)

    def find_by_source_ref(self, source: str, source_ref: str) -> Optional[Dict[str, Any]]:
        """Busca una tarea activa (no terminal) con ese origen.

        Se usa para deduplicar: un correo ya procesado no debe reencolarse.
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT * FROM automation_tasks
                     WHERE source = ? AND source_ref = ?
                       AND status IN (?, ?, ?)
                     ORDER BY id DESC LIMIT 1
                    """,
                    (str(source), str(source_ref), PENDING, RUNNING, AWAITING_APPROVAL),
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_task(row)

    def exists_by_source_ref(self, source: str, source_ref: str) -> bool:
        return self.find_by_source_ref(source, source_ref) is not None

    def find_by_approval_token(self, token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM automation_tasks WHERE approval_token = ? ORDER BY id DESC LIMIT 1",
                    (str(token),),
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_task(row)

    def list_tasks(
        self,
        status: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if source:
            clauses.append("source = ?")
            params.append(str(source))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, int(limit)), max(0, int(offset))])
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"""
                    SELECT * FROM automation_tasks
                    {where}
                    ORDER BY created_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                ).fetchall()
            finally:
                conn.close()
        return [self._row_to_task(row) for row in rows]

    def count_by_status(self) -> Dict[str, int]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM automation_tasks GROUP BY status"
                ).fetchall()
            finally:
                conn.close()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def stats(self) -> Dict[str, Any]:
        """Resumen para la UI de automatización."""
        counts = self.count_by_status()
        return {
            "db_path": str(self.db_path),
            "pending": counts.get(PENDING, 0),
            "running": counts.get(RUNNING, 0),
            "awaiting_approval": counts.get(AWAITING_APPROVAL, 0),
            "done": counts.get(DONE, 0),
            "failed": counts.get(FAILED, 0),
            "cancelled": counts.get(CANCELLED, 0),
            "by_status": counts,
        }

    def backlog_counts(self, now: Optional[float] = None) -> Dict[str, int]:
        """Tareas esperando al ejecutor, desglosadas por motivo.

        `ready` son las que un worker reclamaría ahora mismo; `waiting_backoff`
        las reprogramadas que aún están dentro de su ventana de backoff; y
        `awaiting_approval` las que esperan al humano. Es lo que se pinta como
        «represadas» cuando el ejecutor está pausado.
        """
        current = float(now if now is not None else time.time())
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT
                        SUM(CASE WHEN status = ?
                                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                                 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status = ?
                                  AND next_attempt_at IS NOT NULL
                                  AND next_attempt_at > ?
                                 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status = ? THEN 1 ELSE 0 END)
                      FROM automation_tasks
                    """,
                    (PENDING, current, PENDING, current, AWAITING_APPROVAL),
                ).fetchone()
            finally:
                conn.close()
        ready, waiting, approval = (int(value or 0) for value in row)
        return {
            "ready": ready,
            "waiting_backoff": waiting,
            "awaiting_approval": approval,
            "total": ready + waiting + approval,
        }

    def prune(self, keep_terminal_days: float = 7.0) -> int:
        """Borra tareas terminales antiguas. Devuelve cuántas eliminó."""
        cutoff = time.time() - max(0.0, float(keep_terminal_days)) * 86400.0
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    f"""
                    DELETE FROM automation_tasks
                     WHERE status IN ({placeholders})
                       AND COALESCE(finished_at, created_at) < ?
                    """,
                    [*sorted(TERMINAL_STATUSES), cutoff],
                )
                return cursor.rowcount
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Auditoría
    # ------------------------------------------------------------------
    def record_audit(
        self,
        action: str,
        task_id: Optional[int] = None,
        actor: Optional[str] = None,
        detail: Any = None,
    ) -> int:
        """Registra una acción sensible (encolar, aprobar, rechazar…)."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "INSERT INTO automation_audit (at, action, task_id, actor, detail)\n"
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        time.time(),
                        str(action),
                        int(task_id) if task_id is not None else None,
                        actor,
                        _maybe_json(detail),
                    ),
                )
                return int(cursor.lastrowid)
            finally:
                conn.close()

    def list_audit(
        self,
        limit: int = 100,
        task_id: Optional[int] = None,
        actor_prefix: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Últimas acciones registradas.

        Filtra por tarea y/o por prefijo de `actor` (que es la fuente: `imap`,
        `cron:diario`, `filewatch:facturas`, `webhook:github`…), que es lo que
        usa el panel de disparadores para mostrar el historial de una fuente.
        """
        clauses, params = [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(int(task_id))
        if actor_prefix:
            clauses.append("actor LIKE ?")
            params.append(f"{str(actor_prefix)}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT * FROM automation_audit {where} ORDER BY at DESC, id DESC LIMIT ?",
                    params,
                ).fetchall()
            finally:
                conn.close()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Serialización
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_task(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        task = dict(row)
        try:
            task["attempts"] = int(task.get("attempts") or 0)
        except (TypeError, ValueError):
            task["attempts"] = 0
        task["payload"] = _decode_json(task.get("payload"))
        task["result"] = _decode_json(task.get("result"))
        task["notified_os"] = bool(task.get("notified_os"))
        task["notified_telegram"] = bool(task.get("notified_telegram"))
        return task


# ============================================================================
# Instancia global (una por proceso)
# ============================================================================

_queue: Optional[EventQueue] = None
_queue_lock = threading.Lock()


def get_event_queue(db_path: Optional[str | Path] = None) -> EventQueue:
    """Devuelve (creando si hace falta) la cola global del proceso."""
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = EventQueue(db_path)
        return _queue


def reset_event_queue() -> None:
    """Olvida la instancia global (útil para tests y para cambiar de ruta)."""
    global _queue
    with _queue_lock:
        _queue = None
