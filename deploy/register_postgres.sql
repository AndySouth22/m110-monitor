\set ON_ERROR_STOP on

BEGIN;

INSERT INTO telemetry.devices
    (code, source_system, source_device_key, name, device_type)
SELECT
    'plantdata.sensor-' || sensor_id,
    'PlantData',
    sensor_id::text,
    'Датчик ' || sensor_id,
    'temperature_sensor'
FROM generate_series(1, 8) AS sensor_id
ON CONFLICT (source_system, source_device_key) DO NOTHING;

INSERT INTO telemetry.metrics
    (device_id, code, name, unit, value_kind, scale, value_offset)
SELECT
    d.id,
    'temperature',
    'Температура',
    '°C',
    'gauge',
    1,
    0
FROM telemetry.devices AS d
WHERE d.source_system = 'PlantData'
  AND d.source_device_key IN ('1','2','3','4','5','6','7','8')
ON CONFLICT (device_id, code) DO NOTHING;

COMMIT;
