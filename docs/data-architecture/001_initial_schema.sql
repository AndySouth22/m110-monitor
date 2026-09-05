\set ON_ERROR_STOP on

BEGIN;

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;

CREATE SCHEMA IF NOT EXISTS telemetry AUTHORIZATION telemetry_admin;
CREATE SCHEMA IF NOT EXISTS production AUTHORIZATION telemetry_admin;

SET ROLE telemetry_admin;

CREATE TABLE IF NOT EXISTS telemetry.quality_codes (
    id          smallint PRIMARY KEY,
    code        text NOT NULL UNIQUE,
    description text NOT NULL
);

INSERT INTO telemetry.quality_codes (id, code, description) VALUES
    (0, 'good',         'Достоверное измерение'),
    (1, 'stale',        'Значение устарело'),
    (2, 'offline',      'Нет связи с устройством'),
    (3, 'device_error', 'Устройство сообщило об ошибке'),
    (4, 'parse_error',  'Ошибка разбора ответа'),
    (5, 'manual',       'Значение введено или исправлено вручную')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS telemetry.devices (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code              text NOT NULL UNIQUE,
    source_system     text NOT NULL,
    source_device_key text NOT NULL,
    name              text NOT NULL,
    device_type       text NOT NULL,
    enabled           boolean NOT NULL DEFAULT true,
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_device_key)
);

CREATE TABLE IF NOT EXISTS telemetry.metrics (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id   bigint NOT NULL REFERENCES telemetry.devices(id),
    code        text NOT NULL,
    name        text NOT NULL,
    unit        text,
    value_kind  text NOT NULL DEFAULT 'gauge'
                CHECK (value_kind IN ('gauge', 'counter', 'state')),
    scale       double precision NOT NULL DEFAULT 1,
    value_offset double precision NOT NULL DEFAULT 0,
    enabled     boolean NOT NULL DEFAULT true,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (device_id, code)
);

CREATE TABLE IF NOT EXISTS telemetry.scalar_measurements (
    measured_at timestamptz NOT NULL,
    metric_id   bigint NOT NULL REFERENCES telemetry.metrics(id),
    value       double precision,
    quality     smallint NOT NULL DEFAULT 0
                REFERENCES telemetry.quality_codes(id),
    raw_status  integer,
    collected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (metric_id, measured_at)
);

SELECT create_hypertable(
    'telemetry.scalar_measurements',
    by_range('measured_at', INTERVAL '7 days'),
    if_not_exists => true
);

CREATE INDEX IF NOT EXISTS scalar_measurements_time_idx
    ON telemetry.scalar_measurements (measured_at DESC);

CREATE TABLE IF NOT EXISTS telemetry.gas_measurements (
    measured_at       timestamptz NOT NULL,
    device_id         bigint NOT NULL REFERENCES telemetry.devices(id),
    amount_standard   numeric(20,3),
    amount_working    numeric(20,3),
    flow_standard     double precision,
    flow_working      double precision,
    pressure          double precision,
    gas_temperature   double precision,
    correction_factor double precision,
    quality           smallint NOT NULL DEFAULT 0
                      REFERENCES telemetry.quality_codes(id),
    collected_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, measured_at)
);

SELECT create_hypertable(
    'telemetry.gas_measurements',
    by_range('measured_at', INTERVAL '7 days'),
    if_not_exists => true
);

CREATE INDEX IF NOT EXISTS gas_measurements_time_idx
    ON telemetry.gas_measurements (measured_at DESC);

CREATE TABLE IF NOT EXISTS telemetry.water_measurements (
    measured_at   timestamptz NOT NULL,
    device_id     bigint NOT NULL REFERENCES telemetry.devices(id),
    flow_rate     numeric(20,3),
    total_volume  numeric(20,3),
    work_time     numeric(20,3),
    signal_level_1 smallint,
    signal_level_2 smallint,
    quality       smallint NOT NULL DEFAULT 0
                  REFERENCES telemetry.quality_codes(id),
    collected_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, measured_at)
);

SELECT create_hypertable(
    'telemetry.water_measurements',
    by_range('measured_at', INTERVAL '7 days'),
    if_not_exists => true
);

CREATE INDEX IF NOT EXISTS water_measurements_time_idx
    ON telemetry.water_measurements (measured_at DESC);

CREATE TABLE IF NOT EXISTS telemetry.weight_measurements (
    measured_at   timestamptz NOT NULL,
    device_id     bigint NOT NULL REFERENCES telemetry.devices(id),
    counter_global numeric(20,3),
    counter_shift  numeric(20,3),
    productivity   numeric(20,3),
    quality        smallint NOT NULL DEFAULT 0
                   REFERENCES telemetry.quality_codes(id),
    collected_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, measured_at)
);

SELECT create_hypertable(
    'telemetry.weight_measurements',
    by_range('measured_at', INTERVAL '7 days'),
    if_not_exists => true
);

CREATE INDEX IF NOT EXISTS weight_measurements_time_idx
    ON telemetry.weight_measurements (measured_at DESC);

CREATE TABLE IF NOT EXISTS telemetry.device_status (
    device_id    bigint PRIMARY KEY REFERENCES telemetry.devices(id),
    measured_at  timestamptz,
    online       boolean NOT NULL DEFAULT false,
    quality      smallint NOT NULL DEFAULT 2
                 REFERENCES telemetry.quality_codes(id),
    status_code  integer,
    status_text  text,
    status_values jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS production.packaging_events (
    occurred_at   timestamptz NOT NULL,
    device_id     bigint NOT NULL REFERENCES telemetry.devices(id),
    operator_code smallint,
    product_code  smallint,
    package_count integer,
    weight_raw    bigint,
    error_code    integer,
    raw_message   text,
    device_time   timestamptz,
    quality       smallint NOT NULL DEFAULT 0
                  REFERENCES telemetry.quality_codes(id),
    collected_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, occurred_at)
);

SELECT create_hypertable(
    'production.packaging_events',
    by_range('occurred_at', INTERVAL '30 days'),
    if_not_exists => true
);

CREATE INDEX IF NOT EXISTS packaging_events_time_idx
    ON production.packaging_events (occurred_at DESC);

CREATE TABLE IF NOT EXISTS production.packaging_status (
    device_id      bigint PRIMARY KEY REFERENCES telemetry.devices(id),
    measured_at    timestamptz,
    current_state  smallint,
    mechanisms     integer,
    suspended      boolean,
    current_error  integer,
    end_of_file    boolean,
    weight_raw     bigint,
    global_counter bigint,
    global_weight_raw bigint,
    status_text    text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

RESET ROLE;

GRANT CONNECT ON DATABASE telemetry TO collector_writer;
GRANT USAGE ON SCHEMA telemetry, production TO collector_writer;
GRANT SELECT ON telemetry.devices, telemetry.metrics, telemetry.quality_codes
    TO collector_writer;
GRANT SELECT, INSERT ON
    telemetry.scalar_measurements,
    telemetry.gas_measurements,
    telemetry.water_measurements,
    telemetry.weight_measurements,
    production.packaging_events
    TO collector_writer;
GRANT SELECT, INSERT, UPDATE ON
    telemetry.device_status,
    production.packaging_status
    TO collector_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA telemetry, production
    TO collector_writer;

ALTER DEFAULT PRIVILEGES FOR ROLE telemetry_admin IN SCHEMA telemetry
    GRANT SELECT, INSERT ON TABLES TO collector_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE telemetry_admin IN SCHEMA telemetry
    GRANT USAGE, SELECT ON SEQUENCES TO collector_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE telemetry_admin IN SCHEMA production
    GRANT SELECT, INSERT ON TABLES TO collector_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE telemetry_admin IN SCHEMA production
    GRANT USAGE, SELECT ON SEQUENCES TO collector_writer;

COMMIT;

