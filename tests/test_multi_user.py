"""
Multi-user personalization tests.

The headline requirement (12): a SINGLE shared sensor reading must be able to
produce DIFFERENT warnings for different users' profiles. These tests prove
that directly, plus alert dedup (7), presence gating (4), hysteresis
stability, and the shared-buzzer policy (8).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import database, evaluation, risk_engine  # noqa: E402
from backend.risk_engine import Thresholds, classify, plan_alert_actions  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test_multiuser.db")
    database.init_db()
    # The default seed creates two users; give this test a clean slate.
    _wipe_users()
    yield


def _wipe_users():
    database.init_db()
    from backend import config
    import sqlite3
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()


def _make_user(name, low, mod, high, hyst=5.0, present=True):
    uid = database.create_user(name)
    database.set_user_thresholds(uid, low, mod, high, hyst)
    database.set_presence(uid, present)
    return uid


# ---------------------------------------------------------------------------
# Requirement 12 — one reading, different warnings per user
# ---------------------------------------------------------------------------
def test_same_reading_different_warnings_per_user():
    """A reading of 110 must be HIGH for a sensitive user (high=90),
    MODERATE for a standard user (high=150, moderate=100), and LOW-ish /
    below-band for a relaxed user (low=120)."""
    _make_user("Sensitive", low=60, mod=80, high=90)     # 110 -> HIGH
    _make_user("Standard",  low=85, mod=100, high=150)   # 110 -> MODERATE
    _make_user("Relaxed",   low=120, mod=140, high=200)  # 110 -> LOW/CLEAR

    value = 110.0
    payload = {"device_id": "dev1", "filtered_adc": value,
               "raw_adc": value, "simulation": 0}
    result = evaluation.evaluate_reading(payload)

    levels = {r.user_name: r.level for r in result.risks}
    assert levels["Sensitive"] == "HIGH"
    assert levels["Standard"] == "MODERATE"
    assert levels["Relaxed"] in ("LOW", "CLEAR")
    # Prove they are not all the same (the core of requirement 12).
    assert len(set(levels.values())) >= 2


def test_evaluate_is_pure_per_user_classification():
    """classify() itself gives a distinct band per threshold set at 110."""
    assert classify(110.0, Thresholds(60, 80, 90)) == "HIGH"
    assert classify(110.0, Thresholds(85, 100, 150)) == "MODERATE"
    assert classify(110.0, Thresholds(120, 140, 200)) in ("LOW", "CLEAR")


def test_absent_user_is_not_evaluated():
    _make_user("Here", 85, 100, 150, present=True)
    _make_user("Gone", 60, 80, 90, present=False)
    result = evaluation.evaluate_reading(
        {"device_id": "d", "filtered_adc": 200.0, "simulation": 0})
    names = {r.user_name for r in result.risks}
    assert "Here" in names and "Gone" not in names


# ---------------------------------------------------------------------------
# Requirement 7 — alert dedup for ongoing events
# ---------------------------------------------------------------------------
def test_alert_opens_once_and_touches_while_ongoing():
    uid = _make_user("U", 85, 100, 150)
    payload = {"device_id": "d", "filtered_adc": 160.0, "simulation": 0}

    r1 = evaluation.evaluate_reading(payload)  # enters HIGH -> open
    open_after_1 = database.get_open_alerts(uid)
    high_alerts = [a for a in open_after_1 if a["level"] == "HIGH"]
    assert len(high_alerts) == 1
    aid = high_alerts[0]["id"]

    r2 = evaluation.evaluate_reading(payload)    # still HIGH -> touch
    open_after_2 = database.get_open_alerts(uid)
    assert len([a for a in open_after_2 if a["level"] == "HIGH"]) == 1
    assert database.get_open_alerts(uid)[0]["id"] == aid  # same row, no dup

    # Total HIGH alert rows for this user should still be exactly one.
    hist = [a for a in database.get_alert_history(uid)
            if a["level"] == "HIGH"]
    assert len(hist) == 1


def test_alert_closes_when_level_drops():
    uid = _make_user("U", 85, 100, 150)
    evaluation.evaluate_reading(
        {"device_id": "d", "filtered_adc": 160.0, "simulation": 0})
    assert any(a["level"] == "HIGH" for a in database.get_open_alerts(uid))
    # Hysteresis: must drop well below to exit HIGH (high - 2*hyst = 140)
    evaluation.evaluate_reading(
        {"device_id": "d", "filtered_adc": 100.0, "simulation": 0})
    open_now = database.get_open_alerts(uid)
    assert not any(a["level"] == "HIGH" for a in open_now)


# ---------------------------------------------------------------------------
# Requirement 3 — hysteresis stability (no flapping on a stable reading)
# ---------------------------------------------------------------------------
def test_hysteresis_prevents_flapping_at_boundary():
    thr = Thresholds(low=85, moderate=100, high=150, hysteresis=5.0)
    # Sitting just at the MODERATE entry boundary should not oscillate
    # between LOW and MODERATE reading to reading.
    lvl = "LOW"
    for _ in range(10):
        lvl = classify(100.0, thr, prev_level=lvl)
    assert lvl == "MODERATE"  # entered, and stays
    lvl = "MODERATE"
    lvl = classify(100.0, thr, prev_level=lvl)
    assert lvl == "MODERATE"  # does not immediately flap back to LOW


def test_hysteresis_exit_needs_full_band():
    thr = Thresholds(low=85, moderate=100, high=150, hysteresis=5.0)
    # To exit MODERATE, value must drop below moderate - 2*hyst = 90.
    assert classify(95.0, thr, prev_level="MODERATE") == "MODERATE"
    assert classify(89.0, thr, prev_level="MODERATE") == "LOW"


# ---------------------------------------------------------------------------
# Requirement 8 — shared buzzer policy
# ---------------------------------------------------------------------------
def test_shared_buzzer_on_when_any_present_user_high():
    _make_user("Low user", 85, 100, 150)     # 110 -> MODERATE
    _make_user("High user", 60, 80, 90)      # 110 -> HIGH
    result = evaluation.evaluate_reading(
        {"device_id": "d", "filtered_adc": 110.0, "simulation": 0})
    assert result.shared_buzzer_on is True


def test_shared_buzzer_off_when_no_user_high():
    _make_user("Only moderate", 85, 100, 150)
    result = evaluation.evaluate_reading(
        {"device_id": "d", "filtered_adc": 110.0, "simulation": 0})
    assert result.shared_buzzer_on is False


def test_buzzer_command_structure_keeps_local_fallback_flag():
    cmd = evaluation.build_buzzer_command(
        True, reasons=["A HIGH"], device_id="dev1")
    assert cmd["command"] == "on"
    assert cmd["local_fallback_unchanged"] is True
    assert cmd["shared_policy"] is True


# ---------------------------------------------------------------------------
# Requirement 10 — real vs simulated provenance
# ---------------------------------------------------------------------------
def test_real_and_simulated_provenance_logged():
    uid = _make_user("U", 85, 100, 150)
    evaluation.evaluate_reading(
        {"device_id": "d", "filtered_adc": 100.0, "simulation": 1})
    evaluation.evaluate_reading(
        {"device_id": "d", "filtered_adc": 100.0, "simulation": 0})
    cfg = database.get_sensor_config()
    sources = {c["data_source"] for c in cfg}
    assert "simulated" in sources and "real" in sources


# ---------------------------------------------------------------------------
# Requirement 9 — symptom linked to a sensor window
# ---------------------------------------------------------------------------
def test_symptom_log_stores_sensor_window_stats():
    uid = _make_user("U", 85, 100, 150)
    # Some telemetry to give the window data.
    for v in (90, 100, 110):
        database.insert_telemetry(
            {"device_id": "d", "filtered_adc": v, "simulation": 0})
    lid = database.add_symptom_log(
        uid, 3, "mild cough",
        window_stats=database.sensor_window_stats(15),
        window_start="2026-01-01T00:00:00+00:00",
        window_end="2026-01-01T00:15:00+00:00")
    logs = database.get_symptom_logs(uid)
    assert logs[0]["id"] == lid
    import json
    stats = json.loads(logs[0]["window_stats"])
    assert stats["count"] == 3 and stats["mean"] is not None
