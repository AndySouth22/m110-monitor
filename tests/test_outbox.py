from datetime import datetime, timezone

from decoder import Measurement, aggregate_measurements
from outbox import Outbox


def aggregate(value: float):
    sample = Measurement(1, round(value * 10), value, 0, "измерение успешно", 100)
    return aggregate_measurements([sample, sample, sample], 1.0)


def test_outbox_tracks_targets_independently_and_deletes_after_both(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    measured_utc = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
    measured_local = datetime(2026, 9, 5, 13, 0)
    item_id = outbox.enqueue(
        [aggregate(30.6)] * 8,
        measured_utc,
        measured_local,
        mysql_enabled=True,
        postgres_enabled=True,
    )

    assert outbox.pending_count("mysql") == 1
    assert outbox.pending_count("postgres") == 1
    assert outbox.pending("postgres", 10)[0]["measurements"][7]["value"] == 30.6

    outbox.mark_sent(item_id, "mysql")
    assert outbox.pending_count("mysql") == 0
    assert outbox.pending_count("postgres") == 1

    outbox.mark_sent(item_id, "postgres")
    assert outbox.pending_count() == 0
    outbox.close()


def test_disabled_target_is_marked_delivered(tmp_path):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    item_id = outbox.enqueue(
        [aggregate(1.0)] * 8,
        datetime.now(timezone.utc),
        datetime.now(),
        mysql_enabled=False,
        postgres_enabled=True,
    )
    assert outbox.pending_count("mysql") == 0
    outbox.mark_sent(item_id, "postgres")
    assert outbox.pending_count() == 0
    outbox.close()
