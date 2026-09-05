"""Persistent delivery outbox shared by the MySQL and PostgreSQL sinks."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from decoder import AggregatedMeasurement


class Outbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                measured_at_utc TEXT NOT NULL,
                mysql_measured_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                mysql_sent INTEGER NOT NULL CHECK (mysql_sent IN (0, 1)),
                postgres_sent INTEGER NOT NULL CHECK (postgres_sent IN (0, 1)),
                created_at_utc TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS delivery_outbox_pending_idx "
            "ON delivery_outbox (mysql_sent, postgres_sent, id)"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def enqueue(
        self,
        measurements: list[AggregatedMeasurement],
        measured_at_utc: datetime,
        mysql_measured_at: datetime,
        *,
        mysql_enabled: bool,
        postgres_enabled: bool,
    ) -> int:
        payload = json.dumps(
            [dict(sensor_id=index, **asdict(item)) for index, item in enumerate(measurements, 1)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cursor = self.connection.execute(
            """
            INSERT INTO delivery_outbox
                (measured_at_utc, mysql_measured_at, payload,
                 mysql_sent, postgres_sent, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                measured_at_utc.isoformat(),
                mysql_measured_at.isoformat(),
                payload,
                int(not mysql_enabled),
                int(not postgres_enabled),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def pending(self, target: str, limit: int) -> list[dict[str, Any]]:
        if target not in {"mysql", "postgres"}:
            raise ValueError(f"Неизвестная цель outbox: {target}")
        rows = self.connection.execute(
            f"SELECT id, measured_at_utc, mysql_measured_at, payload "
            f"FROM delivery_outbox WHERE {target}_sent = 0 ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "measured_at_utc": datetime.fromisoformat(row["measured_at_utc"]),
                "mysql_measured_at": datetime.fromisoformat(row["mysql_measured_at"]),
                "measurements": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def mark_sent(self, item_id: int, target: str) -> None:
        if target not in {"mysql", "postgres"}:
            raise ValueError(f"Неизвестная цель outbox: {target}")
        with self.connection:
            self.connection.execute(
                f"UPDATE delivery_outbox SET {target}_sent = 1 WHERE id = ?",
                (item_id,),
            )
            self.connection.execute(
                "DELETE FROM delivery_outbox WHERE id = ? AND mysql_sent = 1 AND postgres_sent = 1",
                (item_id,),
            )

    def pending_count(self, target: str | None = None) -> int:
        if target is None:
            query, params = "SELECT count(*) FROM delivery_outbox", ()
        elif target in {"mysql", "postgres"}:
            query, params = f"SELECT count(*) FROM delivery_outbox WHERE {target}_sent = 0", ()
        else:
            raise ValueError(f"Неизвестная цель outbox: {target}")
        return int(self.connection.execute(query, params).fetchone()[0])
