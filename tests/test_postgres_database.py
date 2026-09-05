from postgres_database import quality_for


def test_quality_mapping():
    assert quality_for({"reliable": True, "value": 30.6}) == 0
    assert quality_for({"reliable": False, "value": None, "status": 0xF007}) == 3
    assert quality_for({"reliable": False, "value": None, "status": None, "raw_values": [None] * 3}) == 2
    assert quality_for({"reliable": False, "value": None, "status": None, "raw_values": [1, None, None]}) == 4
