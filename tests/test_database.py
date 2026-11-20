"""Round-trip tests for the SQLite layer, on a throwaway database file."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Point the backend at an isolated DB so tests never touch real data."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    yield
    # close WAL sidecars cleanly
    import sqlite3
    for f in ("test.db-wal", "test.db-shm"):
        p = tmp_path / f
        p.unlink(missing_ok=True)


def test_schema_and_profile_seeding():
    profs = database.get_profiles()
    names = {p["name"] for p in profs}
    assert {"Sensitive", "Standard", "Relaxed"} <= names
    std = next(p for p in profs if p["name"] == "Standard")
    assert std["is_device_default"] == 1


def test_telemetry_round_trip():
    database.insert_telemetry({
        "device_id": "dev1", "ts_device": 12345, "raw_adc": 77,
        "filtered_adc": 77.3, "exposure": "LOW", "buzzer": 0,
        "wifi_rssi": -55, "mqtt_connected": 1, "simulation": 1,
        "sensor_fault": 0, "uptime_s": 12})
    rows = database.get_recent_telemetry(device_id="dev1")
    assert len(rows) == 1
    r = rows[0]
    assert r["raw_adc"] == 77.0
    assert r["exposure"] == "LOW"
    assert r["simulation"] == 1
    # ts_device (millis) is converted to ISO-8601
    assert r["ts_device"].startswith("1970-01-01T00:00")


def test_telemetry_malformed_fields_are_tolerated():
    database.insert_telemetry({"device_id": "dev1", "raw_adc": None,
                               "exposure": None})
    rows = database.get_recent_telemetry(device_id="dev1")
    assert rows[0]["raw_adc"] is None


def test_device_status_upsert_keeps_latest():
    database.upsert_device_status({"device_id": "dev1", "state": "online"})
    database.upsert_device_status({"device_id": "dev1", "state": "offline",
                                   "wifi_rssi": -70})
    statuses = database.get_device_status("dev1")
    assert len(statuses) == 1
    assert statuses[0]["state"] == "offline"
    assert statuses[0]["wifi_rssi"] == -70


def test_events_and_symptoms_round_trip():
    database.record_event("dev1", "HIGH", "level entered HIGH",
                          source="device", detail={"filtered_adc": 210})
    database.add_symptom("Standard", "mild cough", 2)
    evs = database.get_events(level="HIGH")
    assert any(e["message"] == "level entered HIGH" for e in evs)
    syms = database.get_symptoms(profile="Standard")
    assert syms and syms[0]["text"] == "mild cough"
    assert syms[0]["severity"] == 2


def test_profile_update_upsert():
    database.set_profile("Custom", 90.0, 140.0, 4.0, 0.4, "tuned by hand")
    profs = {p["name"]: p for p in database.get_profiles()}
    assert profs["Custom"]["high_threshold"] == 140.0
    # update existing
    database.set_profile("Custom", 95.0, 145.0, 4.0, 0.4, "tuned again")
    profs = {p["name"]: p for p in database.get_profiles()}
    assert profs["Custom"]["moderate_threshold"] == 95.0
