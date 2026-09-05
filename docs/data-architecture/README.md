# Система сбора технологических данных

## Назначение

Новая система постепенно заменяет существующие демоны и хранилища MySQL 5.1. На переходном этапе новые Python-сборщики читают оборудование один раз и независимо доставляют результат в две базы:

1. существующий MySQL `10.80.22.21` для совместимости со старым интерфейсом;
2. PostgreSQL 17 + TimescaleDB `10.80.22.29` для новой системы.

Старая история в PostgreSQL не переносится. Накопление начинается с момента запуска новых сборщиков.

## Компоненты

```text
Оборудование
    |
    v
Python-сборщик на 10.80.22.45
    |
    +--> локальный SQLite outbox
    |        |                 |
    |        v                 v
    |     старый MySQL      PostgreSQL/TimescaleDB
    |     10.80.22.21       10.80.22.29
    |
    +--> обновление текущего состояния
```

Сборщик не должен повторно опрашивать устройство отдельно для каждой базы. Один полученный пакет сохраняется в локальный outbox и получает два независимых признака доставки: `mysql_sent` и `postgres_sent`.

Ошибка одной базы не должна останавливать доставку во вторую. Повторные попытки выполняются из outbox с задержкой и ограничением частоты.

## База PostgreSQL

Используется одна физическая база `telemetry` и две логические схемы:

- `telemetry` — оборудование, датчики и технологические измерения;
- `production` — события упаковочных установок.

Служебная база `postgres` не используется приложениями.

## Источники данных

| Старый источник | Новая таблица | Назначение |
|---|---|---|
| `PlantData.sensors_log` | `telemetry.scalar_measurements` | Температуры барабанов |
| `boilers.log` | `telemetry.scalar_measurements` | Температуры, давление и состояния котлов |
| `vacuum.log` | `telemetry.scalar_measurements` | Вакуум |
| `GasCounters.counter_log` | `telemetry.gas_measurements` | Объёмы и расходы газа |
| `WaterCounter.irka_log` | `telemetry.water_measurements` | Вода, расход и наработка |
| `WeightBridge.weight_log` | `telemetry.weight_measurements` | Весовые счётчики и производительность |
| `packaging.log` | `production.packaging_events` | События упаковочных машин |

Старые таблицы `status` заменяются таблицами `telemetry.device_status` и `production.packaging_status`. В них хранится только последнее известное состояние каждого устройства.

## Справочники

`telemetry.devices` содержит физические устройства и логические узлы. Код устройства постоянный и не зависит от внутреннего числового ID PostgreSQL.

Примеры кодов:

```text
plantdata.sensor-1
boilers.room-1
vacuum.room-1
gas.counter-1
water.counter-2
weight.scales-4
packaging.machine-1
```

`telemetry.metrics` описывает отдельные скалярные каналы: название, единицу измерения, тип значения, коэффициент и смещение.

Формула преобразования сырого значения:

```text
value = raw_value * scale + offset
```

Единицы и коэффициенты заполняются после проверки протоколов существующих демонов. До этого нельзя предполагать, что хранимое число уже выражено в градусах, барах или килограммах.

## Время

Все новые временные поля имеют тип `timestamptz` и записываются в UTC.

Сборщик должен различать:

- `measured_at` — время измерения или формирования пакета устройством;
- `collected_at` — время получения записи сервером сбора;
- `device_time` — дополнительное исходное время устройства, если оно присутствует.

Если устройство передаёт локальное время без часового пояса, зона задаётся явно в конфигурации сборщика. Записи с неправдоподобным будущим временем не удаляются молча: им назначается плохое качество и создаётся диагностическое событие.

## Качество данных

Коды `telemetry.quality_codes`:

| Код | Значение | Смысл |
|---:|---|---|
| 0 | `good` | Достоверное измерение |
| 1 | `stale` | Значение устарело |
| 2 | `offline` | Нет связи |
| 3 | `device_error` | Ошибка устройства |
| 4 | `parse_error` | Ответ не удалось разобрать |
| 5 | `manual` | Ручное или исправленное значение |

Отсутствие связи нельзя записывать как нулевое показание. Нулевой расход и отсутствие данных — разные состояния.

## Защита от дублей

Исторические таблицы имеют ключ `device/metric + measured_at`. Повторная доставка выполняется идемпотентно:

```sql
INSERT INTO telemetry.scalar_measurements
    (measured_at, metric_id, value, quality, raw_status)
VALUES
    ($1, $2, $3, $4, $5)
ON CONFLICT (metric_id, measured_at) DO NOTHING;
```

Текущее состояние обновляется через upsert:

```sql
INSERT INTO telemetry.device_status
    (device_id, measured_at, online, quality, status_code, status_text, status_values)
VALUES
    ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (device_id) DO UPDATE SET
    measured_at = EXCLUDED.measured_at,
    online = EXCLUDED.online,
    quality = EXCLUDED.quality,
    status_code = EXCLUDED.status_code,
    status_text = EXCLUDED.status_text,
    status_values = EXCLUDED.status_values,
    updated_at = now()
WHERE telemetry.device_status.measured_at IS NULL
   OR EXCLUDED.measured_at >= telemetry.device_status.measured_at;
```

Условие по времени не позволяет запоздавшей записи заменить более свежее состояние.

## Пользователи и права

- `telemetry_admin` владеет базой, создаёт таблицы и выполняет миграции;
- `collector_writer` читает справочники, добавляет историю и обновляет текущее состояние;
- встроенный `postgres` используется только для обслуживания;
- будущий веб-пользователь должен получить права только на чтение и необходимые операции настройки.

Сетевой доступ к PostgreSQL разрешается только с сервера сборщиков `10.80.22.45` и административного компьютера `10.80.22.10`.

## TimescaleDB

Исторические таблицы преобразуются в hypertable с временным разбиением:

- семь дней для телеметрии;
- тридцать дней для событий упаковки.

Политики хранения, columnstore и непрерывные агрегаты пока не включаются. Их следует настроить после появления реальных данных и измерения скорости роста.

## Рекомендуемый порядок перехода

1. Создать схему командой из `001_initial_schema.sql`.
2. Заполнить `devices` и `metrics` реальными устройствами и единицами.
3. Переписать один некритичный сборщик.
4. Включить локальный SQLite outbox.
5. Запустить двойную запись в MySQL и PostgreSQL.
6. Несколько недель сравнивать полноту и значения.
7. Подключить FastAPI и новый веб-интерфейс.
8. После стабилизации поочерёдно отключать запись в старые базы.

## Проверка схемы

```sql
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('timescaledb', 'timescaledb_toolkit');

SELECT hypertable_schema, hypertable_name
FROM timescaledb_information.hypertables
ORDER BY hypertable_schema, hypertable_name;
```

Ожидаемые hypertable:

```text
production.packaging_events
telemetry.gas_measurements
telemetry.scalar_measurements
telemetry.water_measurements
telemetry.weight_measurements
```

