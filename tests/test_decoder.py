import struct
from datetime import datetime

import pytest

from decoder import Measurement, aggregate_measurements, decode_all_inputs, decode_registers
from main import aggregate_cycle, format_measurements, read_all_inputs
from mysql_database import LOG_INSERT_SQL, STATUS_UPSERT_SQL, MySQLWriter, database_status_code, status_row


def reading(value: float, status: int = 0) -> Measurement:
    return Measurement(2, round(value * 100), value, status, "измерение успешно", 740)


def test_decoder_abcd_and_signed_int16():
    data = struct.pack(">HHHHf", 2, 0xFF9C, 0, 125, 0.0)
    result = decode_registers(data)
    assert result.int16 == -100
    assert result.scaled == -1


def test_int16_regression_uses_scaled_value_not_float32():
    data = struct.pack(">HHHHf", 1, 306, 0, 740, 0.0)
    result = decode_registers(data)
    assert result.int16 == 306
    assert result.scaled == 30.6


@pytest.mark.parametrize("status, text", [
    (0x0000, "измерение успешно"),
    (0xF007, "датчик отключён"),
    (0xF00D, "обрыв датчика"),
])
def test_status_decoding(status, text):
    data = struct.pack(">HHHHf", 1, 2351, status, 740, 0.0)
    result = decode_registers(data)
    assert result.status == status
    assert result.status_text == text


def test_decode_all_eight_inputs_with_signed_values_and_statuses():
    registers = []
    for input_number in range(1, 9):
        status = 0 if input_number != 2 else 0xF007
        registers.extend((2, 0x8000 + input_number, status, 700 + input_number, 0x41B8, 0x0000))

    measurements = decode_all_inputs(registers)

    assert len(measurements) == 8
    assert measurements[0].int16 == -32767
    assert measurements[0].scaled == -327.67
    assert measurements[1].status_text == "датчик отключён"
    assert measurements[7].cycle_ms == 708


def test_all_inputs_rejects_incomplete_response():
    with pytest.raises(ValueError, match="48"):
        decode_all_inputs([0] * 47)


def test_read_all_inputs_uses_one_48_register_request():
    class Response:
        registers = [0] * 48

        @staticmethod
        def isError():
            return False

    class FakeClient:
        def __init__(self):
            self.calls = []

        def read_holding_registers(self, **kwargs):
            self.calls.append(kwargs)
            return Response()

    client = FakeClient()
    measurements = read_all_inputs(client, 16)

    assert len(measurements) == 8
    assert client.calls == [{"address": 0, "count": 48, "device_id": 16}]


def test_format_outputs_eight_lines_and_masks_failed_measurement():
    output = format_measurements([aggregate_measurements([reading(23.51, 0xF007)] * 3, 1.0)] * 8)
    lines = output.splitlines()

    assert len(lines) == 9
    assert "Вход 1: значение=—, отсчёты=[—, —, —]" in lines[1]
    assert "статус=0xF007 (датчик отключён)" in lines[1]
    assert lines[8].startswith("Вход 8:")


def test_three_close_values_use_median():
    result = aggregate_measurements([reading(24.10), reading(24.20), reading(24.15)], 1.0)
    assert result.value == 24.15
    assert result.spread == pytest.approx(0.1)
    assert result.reliable and not result.outlier


def test_three_values_with_outlier_keep_median_and_warn():
    result = aggregate_measurements([reading(30.6), reading(95.2), reading(30.7)], 1.0)
    assert result.value == 30.7
    assert result.outlier and result.reliable


def test_stable_new_value_is_accepted():
    result = aggregate_measurements([reading(30.10), reading(30.20), reading(30.15)], 0.5)
    assert result.value == 30.15
    assert result.reliable


def test_two_valid_close_values_are_averaged():
    result = aggregate_measurements([reading(24.0), reading(24.4, 0xF007), reading(24.2)], 0.5)
    assert result.value == pytest.approx(24.1)
    assert result.reliable


def test_two_valid_distant_values_are_unreliable():
    result = aggregate_measurements([reading(20.0), reading(24.0), reading(24.0, 0xF007)], 1.0)
    assert result.value is None and not result.reliable


def test_one_valid_value_is_unreliable():
    result = aggregate_measurements([reading(20.0), reading(20.0, 0xF007), None], 1.0)
    assert result.value is None and not result.reliable


def test_three_error_statuses_choose_most_frequent_status():
    result = aggregate_measurements([reading(0, 0xF007), reading(0, 0xF007), reading(0, 0xF00D)], 1.0)
    assert result.status == 0xF007
    assert result.status_text == "датчик отключён"


def test_three_different_error_statuses_show_all_statuses():
    result = aggregate_measurements([reading(0, 0xF007), reading(0, 0xF00D), reading(0, 0xF00E)], 1.0)
    assert result.status_text == "0xF007, 0xF00D, 0xF00E"


def test_failed_whole_read_is_excluded_per_input():
    results = aggregate_cycle([[reading(10.0)] * 8, None, [reading(10.2)] * 8], 1.0)
    assert all(result.reliable for result in results)


def test_mysql_upsert_preserves_sensor_description_and_logs_only_valid_values():
    class Cursor:
        def __init__(self):
            self.executions = []

        def executemany(self, sql, rows):
            self.executions.append((sql, rows))

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()
            self.commits = 0

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            self.commits += 1

        def rollback(self):
            raise AssertionError("rollback не должен вызываться")

    connection = Connection()
    writer = MySQLWriter(connection)
    valid = aggregate_measurements([reading(30.6)] * 3, 1.0)
    invalid = aggregate_measurements([reading(0, 0xF007)] * 3, 1.0)
    measured = datetime(2026, 9, 4, 15, 30, 20)
    assert writer.save_cycle([valid, invalid] + [valid] * 6, measured) == (8, 7)

    status_sql, status_rows = connection.cursor_instance.executions[0]
    log_sql, log_rows = connection.cursor_instance.executions[1]
    assert "INSERT INTO sensor_status (sensor_id, sensor_value, sensor_status, measured)" in " ".join(status_sql.split())
    assert "INSERT IGNORE INTO sensors_log (sensor_id, measured, value)" in " ".join(log_sql.split())
    assert "log_value" not in log_sql
    assert "sensor_status_text" not in status_sql
    assert status_rows[0] == (1, 30.6, 0, measured)
    assert status_rows[1] == (2, None, 7, measured)
    assert log_rows == [(1, measured, 306), (3, measured, 306), (4, measured, 306), (5, measured, 306), (6, measured, 306), (7, measured, 306), (8, measured, 306)]
    assert "sensor_status_text" not in log_sql
    assert connection.commits == 1


def test_unknown_status_uses_compact_database_code():
    assert database_status_code(0x1234) == 127
