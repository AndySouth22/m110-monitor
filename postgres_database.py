"""PostgreSQL/TimescaleDB delivery for MV110 measurements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCALAR_INSERT_SQL = """
INSERT INTO telemetry.scalar_measurements
    (measured_at, metric_id, value, quality, raw_status, collected_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (metric_id, measured_at) DO NOTHING
"""

STATUS_UPSERT_SQL = """
INSERT INTO telemetry.device_status
    (device_id, measured_at, online, quality, status_code, status_text, status_values)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (device_id) DO UPDATE SET
    measured_at = EXCLUDED.measured_at,
    online = EXCLUDED.online,
    quality = EXCLUDED.quality,
    status_code = EXCLUDED.status_code,
    status_text = EXCLUDED.status_text,
    status_values = EXCLUDED.status_values,
    updated_at = now()
WHERE telemetry.device_status.measured_at IS NULL
   OR EXCLUDED.measured_at >= telemetry.device_status.measured_at
"""


def quality_for(item: dict[str, Any]) -> int:
    if item.get("reliable") and item.get("value") is not None:
        return 0
    if item.get("status") is not None:
        return 3
    if not any(value is not None for value in item.get("raw_values", [])):
        return 2
    return 4


@dataclass
class PostgreSQLWriter:
    connection: Any
    source_system: str
    metric_code: str
    mapping: dict[int, tuple[int, int]] = field(default_factory=dict)

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass

    def is_connected(self) -> bool:
        return not bool(getattr(self.connection, "closed", True))

    def load_mapping(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.source_device_key, d.id, m.id
                FROM telemetry.devices d
                JOIN telemetry.metrics m ON m.device_id = d.id
                WHERE d.source_system = %s
                  AND m.code = %s
                  AND d.enabled = true
                  AND m.enabled = true
                """,
                (self.source_system, self.metric_code),
            )
            mapping: dict[int, tuple[int, int]] = {}
            for source_key, device_id, metric_id in cursor.fetchall():
                try:
                    sensor_id = int(source_key)
                except (TypeError, ValueError):
                    continue
                if 1 <= sensor_id <= 8:
                    mapping[sensor_id] = (int(device_id), int(metric_id))
        missing = sorted(set(range(1, 9)) - set(mapping))
        if missing:
            raise RuntimeError(
                "В PostgreSQL не найдены активные metrics для датчиков "
                f"{missing}; source_system={self.source_system!r}, metric_code={self.metric_code!r}"
            )
        self.mapping = mapping

    def save_payload(self, measurements: list[dict[str, Any]], measured_at) -> tuple[int, int]:
        if not self.mapping:
            self.load_mapping()
        from psycopg.types.json import Jsonb

        scalar_rows = []
        status_rows = []
        for item in measurements:
            sensor_id = int(item["sensor_id"])
            device_id, metric_id = self.mapping[sensor_id]
            quality = quality_for(item)
            raw_status = item.get("status")
            value = item.get("value") if quality == 0 else None
            scalar_rows.append((measured_at, metric_id, value, quality, raw_status))
            status_values = {
                "samples": item.get("samples", []),
                "raw_values": item.get("raw_values", []),
                "decimal_points": item.get("decimal_points", []),
                "cycle_times": item.get("cycle_times", []),
                "spread": item.get("spread"),
                "outlier": bool(item.get("outlier")),
            }
            status_rows.append(
                (
                    device_id,
                    measured_at,
                    quality != 2,
                    quality,
                    raw_status,
                    item.get("status_text"),
                    Jsonb(status_values),
                )
            )
        try:
            with self.connection.cursor() as cursor:
                cursor.executemany(SCALAR_INSERT_SQL, scalar_rows)
                cursor.executemany(STATUS_UPSERT_SQL, status_rows)
            self.connection.commit()
            return len(scalar_rows), len(status_rows)
        except Exception:
            self.connection.rollback()
            raise
