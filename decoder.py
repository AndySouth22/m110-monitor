"""ОВЕН МВ110-8А register decoder."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from math import isfinite
from statistics import median


STATUS_TEXT = {
    0x0000: "измерение успешно", 0xF000: "значение недостоверно", 0xF006: "данные ещё не готовы",
    0xF007: "датчик отключён", 0xF008: "слишком высокая температура свободных концов",
    0xF009: "слишком низкая температура свободных концов", 0xF00A: "измеренное значение слишком велико",
    0xF00B: "измеренное значение слишком мало", 0xF00C: "короткое замыкание датчика",
    0xF00D: "обрыв датчика", 0xF00E: "отсутствует связь с АЦП",
    0xF00F: "некорректный калибровочный коэффициент",
}
INPUT_COUNT = 8
REGISTERS_PER_INPUT = 6


@dataclass(frozen=True)
class Measurement:
    decimal_point: int
    int16: int
    scaled: float
    status: int
    status_text: str
    cycle_ms: int


@dataclass(frozen=True)
class AggregatedMeasurement:
    value: float | None
    samples: tuple[float | None, ...]
    raw_values: tuple[int | None, ...]
    decimal_points: tuple[int | None, ...]
    cycle_times: tuple[int | None, ...]
    status: int | None
    status_text: str
    spread: float | None
    outlier: bool
    reliable: bool


def aggregate_measurements(
    readings: list[Measurement | None], max_spread: float,
) -> AggregatedMeasurement:
    if len(readings) == 0:
        raise ValueError("Нужен хотя бы один результат чтения")
    samples = tuple(
        reading.scaled if reading is not None and reading.status == 0 and isfinite(reading.scaled) else None
        for reading in readings
    )
    raw_values = tuple(reading.int16 if reading is not None else None for reading in readings)
    decimal_points = tuple(reading.decimal_point if reading is not None else None for reading in readings)
    cycle_times = tuple(reading.cycle_ms if reading is not None else None for reading in readings)
    valid = [value for value in samples if value is not None]
    spread = max(valid) - min(valid) if valid else None
    if len(valid) == 3:
        return AggregatedMeasurement(median(valid), samples, raw_values, decimal_points, cycle_times, 0, STATUS_TEXT[0], spread, spread > max_spread, True)
    if len(valid) == 2:
        if spread is not None and spread <= max_spread:
            return AggregatedMeasurement(sum(valid) / 2, samples, raw_values, decimal_points, cycle_times, 0, STATUS_TEXT[0], spread, False, True)
        return AggregatedMeasurement(None, samples, raw_values, decimal_points, cycle_times, None, "недостоверное измерение", spread, False, False)
    if len(valid) == 1:
        return AggregatedMeasurement(None, samples, raw_values, decimal_points, cycle_times, None, "недостоверное измерение", 0.0, False, False)

    statuses = [reading.status for reading in readings if reading is not None and reading.status != 0]
    if statuses:
        status = max(set(statuses), key=statuses.count)
        status_text = STATUS_TEXT.get(status, "неизвестный статус")
        if len(set(statuses)) == len(statuses):
            status_text = ", ".join(f"0x{value:04X}" for value in statuses)
        return AggregatedMeasurement(None, samples, raw_values, decimal_points, cycle_times, status, status_text, None, False, False)
    return AggregatedMeasurement(None, samples, raw_values, decimal_points, cycle_times, None, "нет корректных результатов", None, False, False)


def decode_all_inputs(registers: list[int]) -> list[Measurement]:
    """Decode one 48-register response into the eight input measurements."""
    expected = INPUT_COUNT * REGISTERS_PER_INPUT
    if len(registers) != expected:
        raise ValueError(f"Неполный ответ: ожидалось {expected} регистров, получено {len(registers)}")
    return [
        decode_registers(
            struct.pack(">6H", *(value & 0xFFFF for value in registers[offset:offset + REGISTERS_PER_INPUT])),
        )
        for offset in range(0, expected, REGISTERS_PER_INPUT)
    ]


def decode_registers(data: bytes) -> Measurement:
    if len(data) != 12:
        raise ValueError(f"Ожидалось 12 байт регистров, получено {len(data)}")
    registers = struct.unpack(">6H", data)
    decimal_point, raw_value, status, cycle_ms = registers[:4]
    raw_signed = raw_value
    if raw_signed >= 0x8000:
        raw_signed -= 0x10000
    scaled = raw_signed / (10 ** decimal_point)
    return Measurement(decimal_point, raw_signed, scaled, status, STATUS_TEXT.get(status, "неизвестный статус"), cycle_ms)