"""Logging filters for repeated infrastructure and sensor events."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


CONNECTED = "CONNECTED"
DISCONNECTED = "DISCONNECTED"
RECOVERING = "RECOVERING"


@dataclass
class RepeatSuppressor:
    interval: float = 300.0

    def __post_init__(self) -> None:
        self._last: dict[str, float] = {}
        self._counts: dict[str, int] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        self._counts[key] = self._counts.get(key, 0) + 1
        previous = self._last.get(key)
        if previous is None or current - previous >= self.interval:
            self._last[key] = current
            return True
        return False

    def count(self, key: str) -> int:
        return self._counts.get(key, 0)

    def reset(self, key: str) -> None:
        self._last.pop(key, None)
        self._counts.pop(key, None)


@dataclass
class ConnectionState:
    state: str = DISCONNECTED
    failures: int = 0

    def connected(self) -> bool:
        recovered = self.state != CONNECTED
        self.state = CONNECTED
        self.failures = 0
        return recovered

    def failed(self) -> bool:
        changed = self.state == CONNECTED
        self.state = RECOVERING if self.failures else DISCONNECTED
        self.failures += 1
        return changed


class SensorStateTracker:
    def __init__(self, active_sensors: set[int], logger, debug: bool = False) -> None:
        self.active_sensors = active_sensors
        self.logger = logger
        self.debug = debug
        self.states: dict[int, int] = {}

    def update(self, sensor_id: int, status: int | None, status_text: str) -> None:
        if sensor_id not in self.active_sensors:
            return
        if status is None:
            return
        previous = self.states.get(sensor_id)
        self.states[sensor_id] = status
        if self.debug:
            self.logger.debug("Датчик %s: текущее состояние 0x%04X (%s)", sensor_id, status, status_text)
        elif previous is not None and previous == status:
            return
        elif previous == 0 and status != 0:
            self.logger.warning("Датчик %s: состояние изменилось 0x%04X -> 0x%04X (%s)", sensor_id, previous, status, status_text)
        elif previous is not None and previous != 0 and status == 0:
            self.logger.info("Датчик %s: состояние восстановлено 0x%04X -> 0x0000", sensor_id, previous)
        elif previous is not None and previous != status:
            self.logger.warning("Датчик %s: состояние изменилось 0x%04X -> 0x%04X (%s)", sensor_id, previous, status, status_text)
        elif status != 0:
            self.logger.warning("Датчик %s: ошибочное состояние 0x%04X (%s)", sensor_id, status, status_text)
