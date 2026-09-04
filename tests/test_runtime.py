from pathlib import Path

import pytest

from main import ConfigurationError, load_config, preliminary_args, setup_logging


def write_config(path: Path, **overrides: str) -> Path:
    values = {
        "MODBUS_HOST": "10.0.0.1",
        "MODBUS_PORT": "4005",
        "MODBUS_DEVICE_ID": "16",
        "MODBUS_TIMEOUT": "1.0",
        "READ_COUNT": "3",
        "READ_PAUSE": "0.15",
        "POLL_PERIOD": "1.0",
        "MAX_SPREAD": "1.0",
        "MYSQL_ENABLED": "false",
        "LOG_LEVEL": "INFO",
        "LOG_FILE": "logs/monitor.log",
        "LOG_MAX_BYTES": "10485760",
        "LOG_BACKUP_COUNT": "10",
    }
    values.update(overrides)
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    return path


def test_config_path_is_required():
    path, check_config, check_connections = preliminary_args([])
    assert path is None
    assert not check_config
    assert not check_connections


def test_explicit_config_is_loaded_without_automatic_dotenv_search(tmp_path, monkeypatch):
    monkeypatch.setenv("MODBUS_HOST", "should-not-be-used")
    config_path = write_config(tmp_path / "custom.env")
    config = load_config(config_path)
    assert config.modbus_host == "10.0.0.1"
    assert config.config_path == config_path.resolve()


def test_missing_config_file_has_clear_error(tmp_path):
    with pytest.raises(ConfigurationError, match="Конфигурационный файл не найден"):
        load_config(tmp_path / "missing.env")


@pytest.mark.parametrize("name, value", [("MODBUS_PORT", "bad"), ("MYSQL_ENABLED", "maybe"), ("LOG_MAX_BYTES", "-1")])
def test_invalid_config_names_are_reported(tmp_path, name, value):
    with pytest.raises(ConfigurationError, match=name):
        load_config(write_config(tmp_path / "bad.env", **{name: value}))


def test_enabled_mysql_requires_database_and_user(tmp_path):
    with pytest.raises(ConfigurationError, match="MYSQL_DATABASE"):
        load_config(write_config(tmp_path / "bad.env", MYSQL_ENABLED="true"))


def test_relative_log_path_is_relative_to_config_directory(tmp_path):
    config = load_config(write_config(tmp_path / "custom.env"))
    assert config.log_file == tmp_path / "logs" / "monitor.log"
    logger = setup_logging(config)
    assert config.log_file.parent.is_dir()
    assert len(logger.handlers) == 2
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()


def test_password_is_not_in_connection_error(monkeypatch, tmp_path, caplog):
    import main

    config = load_config(write_config(tmp_path / "custom.env", MYSQL_ENABLED="true", MYSQL_DATABASE="db", MYSQL_USER="user"))
    args = type("Args", (), {"database": True, "db_name": "db", "db_user": "user", "db_host": "localhost", "db_port": 3306, "db_password": "secret"})()
    monkeypatch.setattr("mysql.connector.connect", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("password=secret")))
    logger = setup_logging(config)
    writer = main.create_mysql_writer(args, config, logger)
    assert writer is None
    assert "secret" not in caplog.text
    logger.handlers.clear()


def test_sigterm_handler_sets_stop_event(monkeypatch, tmp_path):
    import signal
    import threading
    import main

    config = load_config(write_config(tmp_path / "custom.env"))
    logger = setup_logging(config)
    event = threading.Event()
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler))
    main.install_signal_handlers(event, logger)
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert event.is_set()
    logger.handlers.clear()


def test_no_sqlite_dependency_or_database_path():
    assert not Path("database.sqlite").exists()
    assert not Path("/var/lib/m110-monitor").exists()
