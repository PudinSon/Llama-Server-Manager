import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class MetaLearner:
    """Guarda feedback /good y /bad y genera un resumen útil para ajustar confianza."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(Path.cwd() / "user_data" / "meta_learner.db")
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
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sentiment TEXT,
                    task TEXT,
                    context TEXT,
                    timestamp REAL
                )
                """
            )
            conn.commit()

    def record_feedback(self, sentiment: str, task: str, context: str = "") -> int:
        sentiment = sentiment.lower()
        if sentiment not in {"good", "bad"}:
            raise ValueError("sentiment debe ser 'good' o 'bad'")

        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO feedback (sentiment, task, context, timestamp) VALUES (?, ?, ?, ?)",
                (sentiment, task, context, time.time()),
            )
            conn.commit()
        return int(cursor.lastrowid)

    def summarize(self) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sentiment, task, COUNT(*) AS count FROM feedback GROUP BY sentiment, task ORDER BY task, sentiment"
            ).fetchall()

        totals = {"good": 0, "bad": 0}
        by_task: Dict[str, Dict[str, int]] = {}
        for row in rows:
            totals[row["sentiment"]] += int(row["count"])
            task_entry = by_task.setdefault(row["task"], {"good": 0, "bad": 0})
            task_entry[row["sentiment"]] = int(row["count"])

        return {"totals": totals, "by_task": by_task}

    def suggest_adjustment(self, task: str) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sentiment, COUNT(*) AS count FROM feedback WHERE task = ? GROUP BY sentiment",
                (task,),
            ).fetchall()

        if not rows:
            return {"task": task, "confidence": 0.5, "recommendation": "sin_feedback"}

        good = 0
        bad = 0
        for row in rows:
            if row["sentiment"] == "good":
                good = int(row["count"])
            if row["sentiment"] == "bad":
                bad = int(row["count"])

        total = good + bad
        confidence = (good / total) if total else 0.5
        return {
            "task": task,
            "confidence": round(confidence, 3),
            "recommendation": "increase_confidence" if confidence >= 0.5 else "decrease_confidence",
        }
