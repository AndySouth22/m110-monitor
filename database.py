"""Optional MySQL persistence for filtered sensor measurements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from decoder import AggregatedMeasurement


DATABASE_STATUS_CODES = {
    0x0000: 0,
    0xF000: 1,
    0xF006: 6,
    0xF007: 7,
    0xF008: 8,
    0xF009: 9,
    0xF00A: 10,
    0xF00B: 11,
    0xF00C: 12,
    0xF00D: 13,
    0xF00E: 14,
    0xF00F: 15,
}

STATUS_UPSERT_SQL = """\
INSERT INTO sensor_status
    (sensor_id, sensor_value, sensor_status, measured)
VALUES
    (%s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    sensor_value = VALUES(sensor_value),
    sensor_status = VALUES(sensor_status),
    measured = VALUES(measured)
"""

LOG_INSERT_SQL = """\
INSERT IGNORE INTO sensors_log (sensor_id, measured, value)
VALUES (%s, %s, %s)
"""


def database_status_code(status: int | None) -> int:
    return DATABASE_STATUS_CODES.get(status, 127)


def status_row(sensor_id: int, measurement: AggregatedMeasurement, measured: datetime) -> tuple[Any, ...]:
    if not 1 <= sensor_id <= 8:
        raise ValueError("sensor_id должен быть от 1 до 8")
    if measurement.reliable and measurement.value is not None:
        return sensor_id, round(measurement.value, 1), 0, measured
    return sensor_id, None, database_status_code(measurement.status), measured


def log_row(sensor_id: int, measurement: AggregatedMeasurement, measured: datetime) -> tuple[Any, ...] | None:
    if not 1 <= sensor_id <= 8:
        raise ValueError("sensor_id должен быть от 1 до 8")
    if not measurement.reliable or measurement.value is None:
        return None
    return sensor_id, measured, round(measurement.value * 10)


@dataclass
class MySQLWriter:
    connection: Any

    def is_connected(self) -> bool:
        try:
            return bool(self.connection.is_connected())
        except Exception:
            return False

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass

    def save_cycle(self, measurements: list[AggregatedMeasurement], measured: datetime) -> tuple[int, int]:
        if len(measurements) != 8:
            raise ValueError(f"Ожидалось 8 измерений, получено {len(measurements)}")
        status_rows = [status_row(sensor_id, measurement, measured) for sensor_id, measurement in enumerate(measurements, 1)]
        log_rows = [row for sensor_id, measurement in enumerate(measurements, 1) if (row := log_row(sensor_id, measurement, measured)) is not None]
        cursor = self.connection.cursor()
        try:
            cursor.executemany(STATUS_UPSERT_SQL, status_rows)
            if log_rows:
                cursor.executemany(LOG_INSERT_SQL, log_rows)
            self.connection.commit()
            return len(status_rows), len(log_rows)
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()