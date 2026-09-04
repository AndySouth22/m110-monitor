import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from logging_utils import ConnectionState, RepeatSuppressor, SensorStateTracker
from main import load_config, setup_logging


def test_default_handlers_are_warning_and_debug_handlers_are_debug(tmp_path):
    values = """MODBUS_HOST=127.0.0.1
MODBUS_PORT=502
MYSQL_ENABLED=false
LOG_FILE=monitor.log
LOG_LEVEL=WARNING
"""
    path = tmp_path / "monitor.env"
    path.write_text(values, encoding="utf-8")
    config = load_config(path)
    logger = setup_logging(config)
    assert [handler.level for handler in logger.handlers] == [logging.WARNING, logging.WARNING]
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()

    debug_logger = setup_logging(config, "DEBUG")
    assert [handler.level for handler in debug_logger.handlers] == [logging.DEBUG, logging.DEBUG]
    for handler in debug_logger.handlers:
        handler.close()
    debug_logger.handlers.clear()


def test_warning_and_error_reach_log_but_debug_reading_does_not_by_default(tmp_path):
    path = tmp_path / "monitor.env"
    path.write_text("MODBUS_HOST=127.0.0.1\nMYSQL_ENABLED=false\nLOG_FILE=monitor.log\nLOG_LEVEL=WARNING\n", encoding="utf-8")
    config = load_config(path)
    logger = setup_logging(config)
    logger.debug("показание=30.6")
    logger.warning("предупреждение")
    logger.error("ошибка")
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    logger.handlers.clear()
    contents = config.log_file.read_text(encoding="utf-8")
    assert "показание=30.6" not in contents
    assert "предупреждение" in contents
    assert "ошибка" in contents


def test_debug_attaches_pymodbus_and_rotates_file(tmp_path):
    path = tmp_path / "monitor.env"
    path.write_text("MODBUS_HOST=127.0.0.1\nMYSQL_ENABLED=false\nLOG_FILE=monitor.log\nLOG_MAX_BYTES=20\nLOG_BACKUP_COUNT=1\n", encoding="utf-8")
    config = load_config(path)
    logger = setup_logging(config, "DEBUG")
    pymodbus_logger = logging.getLogger("pymodbus")
    assert pymodbus_logger.level == logging.DEBUG
    assert any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers)
    logger.debug("012345678901234567890123456789")
    logger.debug("second record triggers rotation")
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    logger.handlers.clear()
    assert config.log_file.exists()
    assert Path(str(config.log_file) + ".1").exists()


def test_repeated_messages_are_suppressed_and_counted():
    suppressor = RepeatSuppressor(interval=300)
    assert suppressor.allow("timeout", now=0)
    assert not suppressor.allow("timeout", now=1)
    assert suppressor.count("timeout") == 2
    assert suppressor.allow("timeout", now=300)


def test_connection_state_transitions_reset_failures():
    state = ConnectionState()
    assert state.connected()
    assert state.failed()
    assert state.failures == 1
    assert state.connected()
    assert state.failures == 0


def test_active_sensor_state_changes_are_logged_once_and_inactive_is_silent(caplog):
    logger = logging.getLogger("test.sensor")
    logger.handlers.clear()
    logger.propagate = True
    tracker = SensorStateTracker({8}, logger)
    with caplog.at_level(logging.INFO):
        tracker.update(1, 0xF007, "датчик отключён")
        tracker.update(8, 0, "измерение успешно")
        tracker.update(8, 0xF00D, "обрыв датчика")
        tracker.update(8, 0xF00D, "обрыв датчика")
        tracker.update(8, 0, "измерение успешно")
    messages = [record.getMessage() for record in caplog.records]
    assert not any("Датчик 1" in message for message in messages)
    assert sum("Датчик 8" in message for message in messages) == 2
    assert any("состояние изменилось" in message for message in messages)
    assert any("состояние восстановлено" in message for message in messages)
