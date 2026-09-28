import json
import sqlite3
import uuid
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from . import runtime
except ImportError:  # pragma: no cover
    import runtime


def _default_db_path() -> str:
    try:
        base_dir = Path(runtime.get_user_data_dir())
    except Exception:
        base_dir = Path.cwd()
    base_dir.mkdir(parents=True, exist_ok=True)
    return str(base_dir / "episodic_memory.db")


class EpisodicMemory:
    """Memoria episódica persistente para tareas, resultados y herramientas."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _default_db_path()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS episodic_memory (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    timestamp REAL,
                    task TEXT,
                    result TEXT,
                    outcome TEXT,
                    tags TEXT,
                    tools_used TEXT,
                    files_touched TEXT
                )
                """
            )
            conn.commit()

    def record(
        self,
        session_id: str,
        task: str,
        result: str,
        outcome: str = "success",
        tags: Optional[List[str]] = None,
        tools_used: Optional[List[str]] = None,
        files_touched: Optional[List[str]] = None,
    ) -> str:
        item_id = str(uuid.uuid4())
        payload = {
            "id": item_id,
            "session_id": session_id,
            "timestamp": time.time(),
            "task": task,
            "result": result,
            "outcome": outcome,
            "tags": json.dumps(tags or [], ensure_ascii=False),
            "tools_used": json.dumps(tools_used or [], ensure_ascii=False),
            "files_touched": json.dumps(files_touched or [], ensure_ascii=False),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO episodic_memory (
                    id, session_id, timestamp, task, result, outcome, tags, tools_used, files_touched
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["session_id"],
                    payload["timestamp"],
                    payload["task"],
                    payload["result"],
                    payload["outcome"],
                    payload["tags"],
                    payload["tools_used"],
                    payload["files_touched"],
                ),
            )
            conn.commit()
        return item_id

    def fetch_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, timestamp, task, result, outcome, tags, tools_used, files_touched
                FROM episodic_memory
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        items: List[Dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "timestamp": row["timestamp"],
                    "task": row["task"],
                    "result": row["result"],
                    "outcome": row["outcome"],
                    "tags": json.loads(row["tags"] or "[]"),
                    "tools_used": json.loads(row["tools_used"] or "[]"),
                    "files_touched": json.loads(row["files_touched"] or "[]"),
                }
            )
        return items


class UserProfile:
    """Almacena preferencias del usuario en SQLite."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(Path(_default_db_path()).with_name("user_profile.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profile (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                )
                """
            )
            conn.commit()

    def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_profile (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, payload, time.time()),
            )
            conn.commit()

    def get(self, key: str) -> Optional[Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM user_profile WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        value = row["value"]
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def delete(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM user_profile WHERE key = ?", (key,))
            conn.commit()
