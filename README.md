# Монитор ОВЕН МВ110-8А

Полная схема PostgreSQL/TimescaleDB и описание общей архитектуры находятся в
[`docs/data-architecture`](docs/data-architecture/README.md).

Консольное приложение на Python 3.11+ для постоянного мониторинга всех восьми входов ОВЕН МВ110-8А по **Modbus RTU over TCP**. Используется `PyModbus` с `ModbusTcpClient` и `FramerType.RTU`: RTU-кадры с CRC передаются через TCP без MBAP-заголовка.

## Запуск

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
python main.py --config .env
```

Конфигурация загружается только из явно указанного файла через `--config`; автоматический поиск `.env` не выполняется. Скопируйте [.env.example](.env.example) в `.env` и заполните реквизиты. По умолчанию handlers имеют уровень `WARNING`, поэтому штатные циклы, показания и SQL не выводятся. Для подробного журнала используйте `python main.py --config .env --debug`.

В обычном режиме в консоль и файл попадают только `WARNING`, `ERROR` и `CRITICAL`. В режиме `--debug` выводятся показания, три отсчёта, raw/DP/статусы/разброс, операции MySQL и диагностический журнал PyModbus. Повторяющиеся ошибки и выбросы подавляются на `LOG_REPEAT_INTERVAL` секунд; состояние активных входов задаётся через `ACTIVE_SENSORS`.

После каждого опроса один нормализованный пакет помещается в локальный SQLite outbox. Из него данные независимо доставляются в старый MySQL и PostgreSQL/TimescaleDB. Сбой одной базы не мешает второй, устройство повторно не опрашивается, а недоставленные пакеты переживают перезапуск службы.

При `MYSQL_ENABLED=true` приложение сохраняет совместимые данные в старый MySQL с `latin1` и `latin1_swedish_ci`. Аргументы `--db-*` и `--no-database` могут переопределить конфигурацию. Приложение обновляет только значение, компактный код статуса и время в `sensor_status`; пользовательское описание датчика не затрагивается. В `sensors_log` записываются только корректные итоговые значения в десятых долях.

При `POSTGRES_ENABLED=true` приложение записывает все восемь каналов в `telemetry.scalar_measurements` и обновляет `telemetry.device_status`. Качество: `good` для достоверного значения, `device_error` для статуса прибора, `offline` при отсутствии ответа и `parse_error` для неразобранного результата. Время PostgreSQL записывается в UTC. Внутренние ID не задаются в конфиге: они находятся через `POSTGRES_SOURCE_SYSTEM`, `source_device_key` датчика и `POSTGRES_METRIC_CODE`.

Перед первым запуском заполните справочники после применения общей схемы:

```bash
psql -h 10.80.22.29 -U telemetry_admin -d telemetry \
  -f deploy/register_postgres.sql
```

Outbox в Linux хранится в `/var/lib/m110-monitor/delivery-outbox.sqlite3`. Не удаляйте его при обычном обновлении или переустановке.

## Тесты

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Проверка без подключений:

```powershell
python main.py --config .env --check-config
```

Проверка Modbus, MySQL и PostgreSQL без записи:

```powershell
python main.py --config .env --check-connections
```

## Linux/systemd

```bash
sudo ./deploy/install.sh
sudo nano /etc/daemons/m110-monitor.env
sudo systemctl start m110-monitor
sudo systemctl status m110-monitor
sudo journalctl -u m110-monitor -f
sudo tail -f /var/log/m110-monitor/m110-monitor.log
```

Для удаления с сохранением конфигурации, логов и outbox: `sudo ./deploy/uninstall.sh`. Полное удаление, включая недоставленные данные: `sudo ./deploy/uninstall.sh --purge`.

По умолчанию: IP `192.168.0.10`, Slave/Device ID `16`, TCP-порт `502`, тайм-аут `1` секунда, период `1` секунда, адрес `0`, функция `03`, количество `48` регистров. Каждый цикл выполняет 3 чтения с паузой `0.15` секунды. Допустимый разброс `MAX_SPREAD` по умолчанию равен `1.0`, повтор сообщений ограничен `LOG_REPEAT_INTERVAL=300` секундами. `ACTIVE_SENSORS` задаёт номера входов, для которых логируются переходы состояний. Основное значение, медиана и разброс вычисляются только из знакового Int16 и DP; Float32 не используется.

При ошибке подключения, тайм-ауте, Modbus exception, неполном ответе или разрыве соединения приложение выводит сообщение, закрывает клиент и повторяет подключение через 3 секунды. Завершение выполняется через `Ctrl+C` с закрытием TCP-клиента.

Пример вывода:

```text
2026-09-04 16:10:25
Вход 1: значение=23.51, отсчёты=[23.50, 23.51, 23.51], статус=0x0000 (измерение успешно), raw=[2350, 2351, 2351], DP=[2, 2, 2], цикл=[740, 740, 741] мс, разброс=0.010, предупреждение=нет
```
