"""Long-running OVEN MV110-8A monitor for Windows and systemd."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic

from dotenv import dotenv_values
from pymodbus.client import ModbusTcpClient
from pymodbus.framer import FramerType

from mysql_database import MySQLWriter
from decoder import AggregatedMeasurement, Measurement, aggregate_measurements, decode_all_inputs
from logging_utils import ConnectionState, RepeatSuppressor, SensorStateTracker
from outbox import Outbox
from postgres_database import PostgreSQLWriter

ADDRESS = 0
COUNT = 48
RECONNECT_DELAYS = (1, 2, 4, 8, 15, 30)


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    modbus_host: str
    modbus_port: int
    modbus_device_id: int
    modbus_timeout: float
    read_count: int
    read_pause: float
    poll_period: float
    max_spread: float
    mysql_enabled: bool
    mysql_host: str
    mysql_port: int
    mysql_database: str
    mysql_user: str
    mysql_password: str
    mysql_charset: str
    mysql_collation: str
    mysql_connect_timeout: int
    postgres_enabled: bool
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_user: str
    postgres_password: str
    postgres_connect_timeout: int
    postgres_sslmode: str
    postgres_source_system: str
    postgres_metric_code: str
    outbox_path: Path
    outbox_batch_size: int
    log_level: str
    log_file: Path
    log_max_bytes: int
    log_backup_count: int
    log_repeat_interval: float
    active_sensors: set[int]


def _required(values: dict[str, str | None], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"Некорректный параметр {name}: значение обязательно")
    return value.strip()


def _string(values: dict[str, str | None], name: str, default: str) -> str:
    return (values.get(name) or default).strip()


def _bool(values: dict[str, str | None], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise ConfigurationError(f"Некорректный параметр {name}: ожидается true или false")
    return normalized == "true"


def _int(values: dict[str, str | None], name: str, default: int, minimum: int = 0) -> int:
    raw = values.get(name, str(default))
    try:
        value = int(raw or "")
    except ValueError as exc:
        raise ConfigurationError(f"Некорректный параметр {name}: ожидается целое число") from exc
    if value < minimum:
        raise ConfigurationError(f"Некорректный параметр {name}: значение должно быть не меньше {minimum}")
    return value


def _float(values: dict[str, str | None], name: str, default: float, minimum: float = 0.0) -> float:
    raw = values.get(name, str(default))
    try:
        value = float(raw or "")
    except ValueError as exc:
        raise ConfigurationError(f"Некорректный параметр {name}: ожидается число") from exc
    if value < minimum:
        raise ConfigurationError(f"Некорректный параметр {name}: значение должно быть не меньше {minimum}")
    return value


def _active_sensors(values: dict[str, str | None]) -> set[int]:
    raw = _string(values, "ACTIVE_SENSORS", "1,2,3,4,5,6,7,8")
    try:
        sensors = {int(item.strip()) for item in raw.split(",") if item.strip()}
    except ValueError as exc:
        raise ConfigurationError("Некорректный параметр ACTIVE_SENSORS: ожидается список номеров") from exc
    if not sensors or not sensors.issubset(set(range(1, 9))):
        raise ConfigurationError("Некорректный параметр ACTIVE_SENSORS: номера должны быть от 1 до 8")
    return sensors


def load_config(config_path: Path) -> AppConfig:
    absolute_path = config_path.expanduser().resolve()
    if not absolute_path.is_file():
        raise ConfigurationError(f"Конфигурационный файл не найден: {absolute_path}")
    values = {key: value for key, value in dotenv_values(absolute_path).items()}
    modbus_port = _int(values, "MODBUS_PORT", 502, 1)
    device_id = _int(values, "MODBUS_DEVICE_ID", 16, 1)
    if device_id > 247 or modbus_port > 65535:
        raise ConfigurationError("Некорректный параметр MODBUS_PORT или MODBUS_DEVICE_ID: значение вне диапазона")
    log_file = Path(_string(values, "LOG_FILE", "logs/m110-monitor.log"))
    if not log_file.is_absolute():
        log_file = absolute_path.parent / log_file
    mysql_enabled = _bool(values, "MYSQL_ENABLED", True)
    mysql_database = _string(values, "MYSQL_DATABASE", "")
    mysql_user = _string(values, "MYSQL_USER", "")
    if mysql_enabled and (not mysql_database or not mysql_user):
        raise ConfigurationError("Некорректные параметры MYSQL_DATABASE и MYSQL_USER: значения обязательны при MYSQL_ENABLED=true")
    mysql_port = _int(values, "MYSQL_PORT", 3306, 1)
    if mysql_port > 65535:
        raise ConfigurationError("Некорректный параметр MYSQL_PORT: значение вне диапазона")
    mysql_charset = _string(values, "MYSQL_CHARSET", "latin1")
    mysql_collation = _string(values, "MYSQL_COLLATION", "latin1_swedish_ci")
    if mysql_charset.lower() != "latin1":
        raise ConfigurationError("Некорректный параметр MYSQL_CHARSET: требуется latin1")
    if mysql_collation.lower() != "latin1_swedish_ci":
        raise ConfigurationError("Некорректный параметр MYSQL_COLLATION: требуется latin1_swedish_ci")
    postgres_enabled = _bool(values, "POSTGRES_ENABLED", False)
    postgres_database = _string(values, "POSTGRES_DATABASE", "telemetry")
    postgres_user = _string(values, "POSTGRES_USER", "")
    if postgres_enabled and (not postgres_database or not postgres_user):
        raise ConfigurationError("Некорректные параметры POSTGRES_DATABASE и POSTGRES_USER: значения обязательны при POSTGRES_ENABLED=true")
    postgres_port = _int(values, "POSTGRES_PORT", 5432, 1)
    if postgres_port > 65535:
        raise ConfigurationError("Некорректный параметр POSTGRES_PORT: значение вне диапазона")
    outbox_path = Path(_string(values, "OUTBOX_PATH", "data/delivery-outbox.sqlite3"))
    if not outbox_path.is_absolute():
        outbox_path = absolute_path.parent / outbox_path
    return AppConfig(
        config_path=absolute_path,
        modbus_host=_required(values, "MODBUS_HOST"), modbus_port=modbus_port,
        modbus_device_id=device_id, modbus_timeout=_float(values, "MODBUS_TIMEOUT", 1.0, 0.01),
        read_count=_int(values, "READ_COUNT", 3, 1), read_pause=_float(values, "READ_PAUSE", 0.15),
        poll_period=_float(values, "POLL_PERIOD", 1.0), max_spread=_float(values, "MAX_SPREAD", 1.0),
        mysql_enabled=mysql_enabled, mysql_host=_string(values, "MYSQL_HOST", "127.0.0.1"),
        mysql_port=mysql_port, mysql_database=mysql_database,
        mysql_user=mysql_user, mysql_password=_string(values, "MYSQL_PASSWORD", ""),
        mysql_charset=mysql_charset, mysql_collation=mysql_collation,
        mysql_connect_timeout=_int(values, "MYSQL_CONNECT_TIMEOUT", 3, 1), log_level=_string(values, "LOG_LEVEL", "INFO").upper(),
        postgres_enabled=postgres_enabled,
        postgres_host=_string(values, "POSTGRES_HOST", "127.0.0.1"),
        postgres_port=postgres_port, postgres_database=postgres_database,
        postgres_user=postgres_user, postgres_password=_string(values, "POSTGRES_PASSWORD", ""),
        postgres_connect_timeout=_int(values, "POSTGRES_CONNECT_TIMEOUT", 3, 1),
        postgres_sslmode=_string(values, "POSTGRES_SSLMODE", "prefer"),
        postgres_source_system=_string(values, "POSTGRES_SOURCE_SYSTEM", "PlantData"),
        postgres_metric_code=_string(values, "POSTGRES_METRIC_CODE", "temperature"),
        outbox_path=outbox_path,
        outbox_batch_size=_int(values, "OUTBOX_BATCH_SIZE", 100, 1),
        log_file=log_file, log_max_bytes=_int(values, "LOG_MAX_BYTES", 10485760, 1),
        log_backup_count=_int(values, "LOG_BACKUP_COUNT", 10, 0),
        log_repeat_interval=_float(values, "LOG_REPEAT_INTERVAL", 300.0, 0.1),
        active_sensors=_active_sensors(values),
    )


def preliminary_args(argv: list[str]) -> tuple[Path | None, bool, bool]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--check-connections", action="store_true")
    known, _ = parser.parse_known_args(argv)
    if not known.config:
        print("Не указан конфигурационный файл.\nПример: python main.py --config /etc/daemons/m110-monitor.env", file=sys.stderr)
        return None, known.check_config, known.check_connections
    return Path(known.config), known.check_config, known.check_connections


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, AppConfig]:
    arguments = list(sys.argv[1:] if argv is None else argv)
    config_path, check_config, check_connections = preliminary_args(arguments)
    if config_path is None:
        raise SystemExit(2)
    config = load_config(config_path)
    parser = argparse.ArgumentParser(description="Монитор ОВЕН МВ110-8А")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check-config", action="store_true", default=check_config)
    parser.add_argument("--check-connections", action="store_true", default=check_connections)
    parser.add_argument("--host", dest="modbus_host", default=config.modbus_host)
    parser.add_argument("--port", dest="modbus_port", type=int, default=config.modbus_port)
    parser.add_argument("--device-id", dest="modbus_device_id", type=int, default=config.modbus_device_id)
    parser.add_argument("--timeout", dest="modbus_timeout", type=float, default=config.modbus_timeout)
    parser.add_argument("--read-count", type=int, default=config.read_count)
    parser.add_argument("--read-pause", type=float, default=config.read_pause)
    parser.add_argument("--period", dest="poll_period", type=float, default=config.poll_period)
    parser.add_argument("--max-spread", type=float, default=config.max_spread)
    parser.add_argument("--debug", action="store_true")
    database_group = parser.add_mutually_exclusive_group()
    database_group.add_argument("--database", dest="database", action="store_true")
    database_group.add_argument("--no-database", dest="database", action="store_false")
    parser.set_defaults(database=None)
    parser.add_argument("--db-host", default=config.mysql_host)
    parser.add_argument("--db-port", type=int, default=config.mysql_port)
    parser.add_argument("--db-name", default=config.mysql_database)
    parser.add_argument("--db-user", default=config.mysql_user)
    parser.add_argument("--db-password", default=config.mysql_password)
    parser.add_argument("--log-level", default=config.log_level)
    args = parser.parse_args(arguments)
    return args, config


def setup_logging(config: AppConfig, level_override: str | None = None) -> logging.Logger:
    level_name = (level_override or config.log_level).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise ConfigurationError(f"Некорректный параметр LOG_LEVEL: {level_name}")
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("m110_monitor")
    root.setLevel(level)
    root.handlers.clear()
    stream = logging.StreamHandler()
    handler_level = logging.DEBUG if level_name == "DEBUG" else logging.WARNING
    stream.setLevel(handler_level)
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(config.log_file, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count, encoding="utf-8")
    file_handler.setLevel(handler_level)
    file_handler.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(file_handler)
    root.propagate = False
    pymodbus_logger = logging.getLogger("pymodbus")
    pymodbus_logger.handlers.clear()
    pymodbus_logger.propagate = False
    if level_name == "DEBUG":
        pymodbus_logger.setLevel(logging.DEBUG)
        pymodbus_logger.addHandler(stream)
        pymodbus_logger.addHandler(file_handler)
    else:
        pymodbus_logger.setLevel(logging.CRITICAL + 1)
    return root


def install_signal_handlers(stop_event, logger: logging.Logger) -> None:
    def stop_handler(signum, _frame):
        logger.info("Получен сигнал остановки: %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)


def log_repeated(logger: logging.Logger, suppressor: RepeatSuppressor, key: str, message: str, *args, debug: bool = False) -> None:
    if debug or suppressor.allow(key):
        count = suppressor.count(key)
        if count > 1:
            message = f"{message}: {count} неудачных операций за последние {int(suppressor.interval)} секунд"
            logger.warning(message)
        else:
            logger.warning(message, *args)


def redact_secret(message: str, secret: str) -> str:
    return message.replace(secret, "***") if secret else message


def measurements_from_registers(registers: list[int]) -> list[Measurement]:
    return decode_all_inputs(registers)


def read_all_inputs(client: ModbusTcpClient, device_id: int) -> list[Measurement]:
    response = client.read_holding_registers(address=ADDRESS, count=COUNT, device_id=device_id)
    if response.isError():
        raise RuntimeError(f"Ответ Modbus с ошибкой: {response}")
    return measurements_from_registers(response.registers)


def aggregate_cycle(readings: list[list[Measurement] | None], max_spread: float) -> list[AggregatedMeasurement]:
    return [aggregate_measurements([response[index] if response is not None else None for response in readings], max_spread) for index in range(8)]


def format_measurements(measurements: list[AggregatedMeasurement], measured: datetime | None = None) -> str:
    lines = [(measured or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")]
    for input_number, item in enumerate(measurements, 1):
        value = f"{item.value:.1f}" if item.reliable and item.value is not None else "—"
        samples = "[" + ", ".join(f"{sample:.1f}" if sample is not None else "—" for sample in item.samples) + "]"
        status = "0x0000 (измерение успешно)" if item.reliable else f"0x{item.status:04X} ({item.status_text})" if item.status is not None else f"— ({item.status_text})"
        if not any(sample is not None for sample in item.samples):
            lines.append(f"Вход {input_number}: значение={value}, отсчёты={samples}, статус={status}")
            continue
        raw = "[" + ", ".join(str(v) if v is not None else "—" for v in item.raw_values) + "]"
        points = "[" + ", ".join(str(v) if v is not None else "—" for v in item.decimal_points) + "]"
        cycles = "[" + ", ".join(str(v) if v is not None else "—" for v in item.cycle_times) + "]"
        spread = f"{item.spread:.1f}" if item.spread is not None else "—"
        warning = ", предупреждение=обнаружен выброс" if item.outlier else ""
        lines.append(f"Вход {input_number}: значение={value}, отсчёты={samples}, raw={raw}, DP={points}, цикл={cycles} мс, статус={status}, разброс={spread}{warning}")
    return "\n".join(lines)


def create_mysql_writer(args: argparse.Namespace, config: AppConfig, logger: logging.Logger) -> MySQLWriter | None:
    database_enabled = config.mysql_enabled if args.database is None else args.database
    if not database_enabled:
        logger.getChild("mysql").info("MySQL: сохранение отключено (MYSQL_ENABLED=false)")
        return None
    if not args.db_name or not args.db_user:
        raise ConfigurationError("MySQL: обязательны MYSQL_DATABASE и MYSQL_USER")
    import mysql.connector
    try:
        connection = mysql.connector.connect(host=args.db_host, port=args.db_port, database=args.db_name, user=args.db_user, password=args.db_password or "", charset=config.mysql_charset, collation=config.mysql_collation, connection_timeout=config.mysql_connect_timeout)
        logger.getChild("mysql").info("MySQL: подключено, сервер=%s:%s, база=%s", args.db_host, args.db_port, args.db_name)
        return MySQLWriter(connection)
    except Exception as exc:
        logger.getChild("mysql").debug(
            "Не удалось подключиться к MySQL: %s", redact_secret(str(exc), args.db_password or "")
        )
        return None


def create_postgres_writer(config: AppConfig, logger: logging.Logger) -> PostgreSQLWriter | None:
    if not config.postgres_enabled:
        return None
    try:
        import psycopg

        connection = psycopg.connect(
            host=config.postgres_host,
            port=config.postgres_port,
            dbname=config.postgres_database,
            user=config.postgres_user,
            password=config.postgres_password,
            connect_timeout=config.postgres_connect_timeout,
            sslmode=config.postgres_sslmode,
        )
        writer = PostgreSQLWriter(
            connection,
            source_system=config.postgres_source_system,
            metric_code=config.postgres_metric_code,
        )
        writer.load_mapping()
        logger.getChild("postgres").info(
            "PostgreSQL: подключено, сервер=%s:%s, база=%s",
            config.postgres_host, config.postgres_port, config.postgres_database,
        )
        return writer
    except Exception as exc:
        logger.getChild("postgres").debug(
            "Не удалось подключиться к PostgreSQL: %s",
            redact_secret(str(exc), config.postgres_password),
        )
        return None


def deliver_pending(outbox: Outbox, target: str, writer, limit: int) -> int:
    delivered = 0
    for item in outbox.pending(target, limit):
        measured = item["mysql_measured_at"] if target == "mysql" else item["measured_at_utc"]
        writer.save_payload(item["measurements"], measured)
        outbox.mark_sent(item["id"], target)
        delivered += 1
    return delivered


def check_connections(args: argparse.Namespace, config: AppConfig, logger: logging.Logger) -> int:
    client = ModbusTcpClient(host=args.modbus_host, port=args.modbus_port, framer=FramerType.RTU, timeout=args.modbus_timeout)
    writer = None
    postgres_writer = None
    try:
        if not client.connect():
            raise ConnectionError("Modbus-подключение не установлено")
        logger.getChild("modbus").info("Соединение Modbus установлено: %s:%s", args.modbus_host, args.modbus_port)
        measurements = read_all_inputs(client, args.modbus_device_id)
        print(f"Modbus: чтение успешно, получено {len(measurements)} входов")
        writer = create_mysql_writer(args, config, logger)
        database_enabled = config.mysql_enabled if args.database is None else args.database
        if database_enabled and writer is None:
            logger.getChild("mysql").error("Ошибка проверки подключения MySQL")
            return 1
        print("MySQL: подключение успешно" if writer is not None else "MySQL: проверка пропущена")
        postgres_writer = create_postgres_writer(config, logger)
        if config.postgres_enabled and postgres_writer is None:
            logger.getChild("postgres").error("Ошибка проверки подключения PostgreSQL или справочников devices/metrics")
            return 1
        print("PostgreSQL: подключение успешно" if postgres_writer is not None else "PostgreSQL: проверка пропущена")
        return 0
    except Exception as exc:
        logger.exception("Ошибка проверки соединений: %s", exc)
        return 1
    finally:
        client.close()
        if writer is not None:
            writer.close()
        if postgres_writer is not None:
            postgres_writer.close()


def monitor(args: argparse.Namespace, config: AppConfig, logger: logging.Logger, stop_event) -> None:
    client: ModbusTcpClient | None = None
    writer = None
    postgres_writer = None
    outbox = Outbox(config.outbox_path)
    modbus_failure = 0
    mysql_failure = 0
    postgres_failure = 0
    next_mysql_attempt = 0.0
    next_postgres_attempt = 0.0
    next_modbus_attempt = 0.0
    modbus_state = ConnectionState()
    mysql_state = ConnectionState()
    postgres_state = ConnectionState()
    error_suppressor = RepeatSuppressor(config.log_repeat_interval)
    sensor_tracker = SensorStateTracker(config.active_sensors, logger.getChild("sensor"), args.debug)
    try:
        while not stop_event.is_set():
            if client is None:
                client = ModbusTcpClient(host=args.modbus_host, port=args.modbus_port, framer=FramerType.RTU, timeout=args.modbus_timeout)
            readings: list[list[Measurement] | None] = []
            for attempt in range(max(1, args.read_count)):
                if stop_event.is_set():
                    break
                try:
                    remaining = next_modbus_attempt - monotonic()
                    if remaining > 0:
                        stop_event.wait(remaining)
                    if stop_event.is_set():
                        break
                    if not client.connected:
                        if not client.connect():
                            raise ConnectionError("Modbus-подключение не установлено")
                        logger.getChild("modbus").info("Соединение установлено: %s:%s", args.modbus_host, args.modbus_port)
                        if modbus_state.connected():
                            logger.getChild("modbus").info("Соединение Modbus восстановлено")
                        modbus_failure = 0
                        next_modbus_attempt = 0.0
                    readings.append(read_all_inputs(client, args.modbus_device_id))
                except Exception as exc:
                    readings.append(None)
                    modbus_state.failed()
                    error_key = f"modbus:{type(exc).__name__}:{exc}"
                    log_repeated(logger.getChild("modbus"), error_suppressor, error_key, f"Ошибка Modbus (чтение {attempt + 1}/{args.read_count}): {exc}", debug=args.debug)
                    client.close()
                    modbus_failure += 1
                    delay = RECONNECT_DELAYS[min(modbus_failure - 1, len(RECONNECT_DELAYS) - 1)]
                    next_modbus_attempt = monotonic() + delay
                    log_repeated(logger.getChild("modbus"), error_suppressor, "modbus:reconnect", f"Повторное подключение Modbus через {delay} с", debug=args.debug)
                    if attempt + 1 < args.read_count:
                        stop_event.wait(delay)
                else:
                    if attempt + 1 >= args.read_count:
                        continue
                    stop_event.wait(max(0.0, args.read_pause))
            if stop_event.is_set():
                break
            mysql_measured = datetime.now().replace(microsecond=0)
            measured_utc = datetime.now(timezone.utc).replace(microsecond=0)
            measurements = aggregate_cycle(readings, args.max_spread)
            if args.debug:
                logger.debug("Результат цикла: %s", format_measurements(measurements, mysql_measured).replace("\n", " | "))
            for index, item in enumerate(measurements, 1):
                sensor_tracker.update(index, item.status, item.status_text)
                if item.outlier:
                    outlier_key = f"outlier:{index}"
                    log_repeated(logger, error_suppressor, outlier_key, f"Вход {index}: отброшен выброс при сохранении медианы", debug=args.debug)
            now = monotonic()
            database_enabled = config.mysql_enabled if args.database is None else args.database
            outbox.enqueue(
                measurements,
                measured_utc,
                mysql_measured,
                mysql_enabled=database_enabled,
                postgres_enabled=config.postgres_enabled,
            )
            if database_enabled:
                if writer is None and now >= next_mysql_attempt:
                    writer = create_mysql_writer(args, config, logger)
                    if writer is None:
                        mysql_failure += 1
                        delay = RECONNECT_DELAYS[min(mysql_failure - 1, len(RECONNECT_DELAYS) - 1)]
                        next_mysql_attempt = now + delay
                        log_repeated(logger.getChild("mysql"), error_suppressor, "mysql:unavailable", f"Повторное подключение MySQL через {delay} с", debug=args.debug)
                    else:
                        mysql_failure = 0
                        if mysql_state.connected():
                            logger.info("Соединение MySQL восстановлено")
                if writer is not None:
                    try:
                        if not writer.is_connected():
                            writer.close()
                            writer = None
                            raise ConnectionError("MySQL-соединение недоступно")
                        delivered = deliver_pending(outbox, "mysql", writer, config.outbox_batch_size)
                        if args.debug:
                            logger.debug("MySQL: доставлено циклов из outbox: %s", delivered)
                    except Exception as exc:
                        failed_writer = writer
                        mysql_state.failed()
                        error_key = f"mysql:{type(exc).__name__}:{exc}"
                        if args.debug or error_suppressor.allow(error_key):
                            logger.warning("Ошибка MySQL: %s", redact_secret(str(exc), args.db_password or ""))
                        try:
                            if failed_writer is not None:
                                failed_writer.connection.rollback()
                        except Exception:
                            pass
                        if failed_writer is not None:
                            failed_writer.close()
                        writer = None
                        mysql_failure += 1
                        next_mysql_attempt = now + RECONNECT_DELAYS[min(mysql_failure - 1, len(RECONNECT_DELAYS) - 1)]
            if config.postgres_enabled:
                if postgres_writer is None and now >= next_postgres_attempt:
                    postgres_writer = create_postgres_writer(config, logger)
                    if postgres_writer is None:
                        postgres_failure += 1
                        delay = RECONNECT_DELAYS[min(postgres_failure - 1, len(RECONNECT_DELAYS) - 1)]
                        next_postgres_attempt = now + delay
                        log_repeated(
                            logger.getChild("postgres"), error_suppressor, "postgres:unavailable",
                            f"Повторное подключение PostgreSQL через {delay} с", debug=args.debug,
                        )
                    else:
                        postgres_failure = 0
                        if postgres_state.connected():
                            logger.getChild("postgres").info("Соединение PostgreSQL восстановлено")
                if postgres_writer is not None:
                    try:
                        if not postgres_writer.is_connected():
                            postgres_writer.close()
                            postgres_writer = None
                            raise ConnectionError("PostgreSQL-соединение недоступно")
                        delivered = deliver_pending(
                            outbox, "postgres", postgres_writer, config.outbox_batch_size,
                        )
                        if args.debug:
                            logger.debug("PostgreSQL: доставлено циклов из outbox: %s", delivered)
                    except Exception as exc:
                        failed_writer = postgres_writer
                        postgres_state.failed()
                        error_key = f"postgres:{type(exc).__name__}:{exc}"
                        if args.debug or error_suppressor.allow(error_key):
                            safe_message = redact_secret(str(exc), config.postgres_password)
                            logger.getChild("postgres").warning("Ошибка PostgreSQL: %s", safe_message)
                        if failed_writer is not None:
                            failed_writer.close()
                        postgres_writer = None
                        postgres_failure += 1
                        next_postgres_attempt = now + RECONNECT_DELAYS[
                            min(postgres_failure - 1, len(RECONNECT_DELAYS) - 1)
                        ]
            stop_event.wait(max(0.0, args.poll_period))
    finally:
        if client is not None:
            client.close()
            logger.getChild("modbus").info("Соединение Modbus закрыто")
        if writer is not None:
            writer.close()
            logger.getChild("mysql").info("Соединение MySQL закрыто")
        if postgres_writer is not None:
            postgres_writer.close()
            logger.getChild("postgres").info("Соединение PostgreSQL закрыто")
        outbox.close()


def main(argv: list[str] | None = None) -> int:
    config_path, check_config, check_connections_flag = preliminary_args(list(sys.argv[1:] if argv is None else argv))
    if config_path is None:
        return 2
    try:
        args, config = parse_args(argv)
        logger = setup_logging(config, "DEBUG" if args.debug else args.log_level)
        logger.info("Запуск программы, конфигурация: %s", config.config_path)
        if check_config:
            print("Конфигурация корректна")
            return 0
        stop_event = __import__("threading").Event()
        install_signal_handlers(stop_event, logger)
        if check_connections_flag:
            return check_connections(args, config, logger)
        monitor(args, config, logger, stop_event)
        logger.info("Штатное завершение")
        return 0
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.getLogger("m110_monitor").exception("Неожиданное исключение")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
