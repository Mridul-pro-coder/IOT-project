"""
SQLite persistence layer for AirSense.

The database is opened in WAL journal mode so that the MQTT writer and the
Streamlit reader can coexist without locking each other out. Each public
function opens a short-lived connection so the code is safe to call from
multiple threads (the MQTT callback and the Streamlit reruns).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    ts_received TEXT NOT NULL,          -- ISO-8601 UTC, received at the backend
    ts_device   TEXT,                   -- device-side timestamp if provided
    raw_adc     REAL,
    filtered_adc REAL,
    exposure    TEXT,                   -- LOW / MODERATE / HIGH
    buzzer      INTEGER,
    wifi_rssi   INTEGER,
    mqtt_connected INTEGER,
    simulation  INTEGER,                -- 1 = simulated reading, 0 = real
    sensor_fault INTEGER,
    uptime_s    REAL
);
CREATE INDEX IF NOT EXISTS idx_tele_device_time ON telemetry(device_id, ts_received);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    level      TEXT NOT NULL,           -- LOW / MODERATE / HIGH
    message    TEXT,
    source     TEXT,                    -- 'device' | 'backend'
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts);

CREATE TABLE IF NOT EXISTS device_status (
    device_id  TEXT PRIMARY KEY,
    state      TEXT,                    -- online / offline
    last_seen  TEXT NOT NULL,
    mqtt_connected INTEGER,
    wifi_rssi  INTEGER,
    simulation INTEGER
);

CREATE TABLE IF NOT EXISTS symptoms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    profile     TEXT,
    text        TEXT,
    severity    INTEGER
);

CREATE TABLE IF NOT EXISTS profiles (
    name             TEXT PRIMARY KEY,
    moderate_threshold REAL,
    high_threshold     REAL,
    hysteresis         REAL,
    ewma_alpha         REAL,
    notes              TEXT,
    is_device_default  INTEGER DEFAULT 0
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Apply schema migrations (which also enable WAL), then seed.

    Idempotent — safe to call at every start of the service and the app.
    Migrations never drop or rewrite existing data (see migrations.py).
    """
    from .migrations import run_migrations
    run_migrations()
    _seed_profiles()
    # Multi-user defaults: one seed user with a Standard-equivalent
    # threshold set so the team-risk panel has data on first run.
    _seed_default_users()


def _seed_profiles() -> None:
    """Seed three rule-based sensitivity profiles on first run.

    These are MANUALLY CONFIGURED thresholds, not learned models. The
    numbers are in the same uncalibrated ADC units as the sensor and must
    be tuned to each user's baseline.
    """
    with _connect() as conn:
        existing = {row[0] for row in
                    conn.execute("SELECT name FROM profiles").fetchall()}
        defaults = [
            # more sensitive -> lower thresholds, alarms fire earlier
            ("Sensitive", 80.0, 120.0, 5.0, 0.30,
             "Fires earlier on small increases. For users who report symptoms "
             "at low exposure."),
            # standard -> mirrors the device defaults
            ("Standard", config.DEFAULT_MODERATE_THRESHOLD,
             config.DEFAULT_HIGH_THRESHOLD, config.DEFAULT_HYSTERESIS,
             config.DEFAULT_EWMA_ALPHA, "Baseline sensitivity profile."),
            # relaxed -> higher thresholds
            ("Relaxed", 130.0, 190.0, 5.0, 0.30,
             "Fires later. For users tolerant of higher background levels."),
        ]
        for name, mod, high, hyp, alpha, notes in defaults:
            if name not in existing:
                conn.execute(
                    "INSERT INTO profiles (name, moderate_threshold, high_threshold,"
                    " hysteresis, ewma_alpha, notes, is_device_default)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (name, mod, high, hyp, alpha, notes,
                     1 if name == "Standard" else 0))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def insert_telemetry(payload: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO telemetry (device_id, ts_received, ts_device, raw_adc,"
            " filtered_adc, exposure, buzzer, wifi_rssi, mqtt_connected, simulation,"
            " sensor_fault, uptime_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(payload.get("device_id", "unknown")),
                utcnow_iso(),
                _device_ts(payload.get("ts_device")),
                _num(payload.get("raw_adc")),
                _num(payload.get("filtered_adc")),
                str(payload.get("exposure", "UNKNOWN")),
                _bool(payload.get("buzzer")),
                _int(payload.get("wifi_rssi")),
                _bool(payload.get("mqtt_connected")),
                _bool(payload.get("simulation")),
                _bool(payload.get("sensor_fault")),
                _num(payload.get("uptime_s")),
            ))


def record_event(device_id: str, level: str, message: str,
                 source: str = "device", detail: Optional[dict] = None) -> None:
    import json
    with _connect() as conn:
        conn.execute(
            "INSERT INTO events (device_id, ts, level, message, source, detail)"
            " VALUES (?,?,?,?,?,?)",
            (device_id, utcnow_iso(), level, message, source,
             json.dumps(detail or {})))


def upsert_device_status(state: dict) -> None:
    """Called from the status / lastwill topics. Tracks device up/down."""
    with _connect() as conn:
        dev = str(state.get("device_id", "unknown"))
        conn.execute(
            "INSERT INTO device_status (device_id, state, last_seen, mqtt_connected,"
            " wifi_rssi, simulation) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(device_id) DO UPDATE SET state=excluded.state,"
            " last_seen=excluded.last_seen, mqtt_connected=excluded.mqtt_connected,"
            " wifi_rssi=excluded.wifi_rssi, simulation=excluded.simulation",
            (dev, state.get("state", "unknown"), utcnow_iso(),
             _bool(state.get("mqtt_connected")),
             _int(state.get("wifi_rssi")),
             _bool(state.get("simulation"))))


def add_symptom(profile: str, text: str, severity: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO symptoms (ts, profile, text, severity) VALUES (?,?,?,?)",
            (utcnow_iso(), profile, text, int(severity)))


def set_profile(name: str, moderate: float, high: float, hysteresis: float,
                ewma_alpha: float, notes: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO profiles (name, moderate_threshold, high_threshold,"
            " hysteresis, ewma_alpha, notes) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET moderate_threshold=excluded."
            "moderate_threshold, high_threshold=excluded.high_threshold,"
            " hysteresis=excluded.hysteresis, ewma_alpha=excluded.ewma_alpha,"
            " notes=excluded.notes",
            (name, moderate, high, hysteresis, ewma_alpha, notes))


# ---------------------------------------------------------------------------
# Reads (used by the dashboard / analytics)
# ---------------------------------------------------------------------------
def get_recent_telemetry(device_id: Optional[str] = None,
                         limit: int = 500) -> list[dict]:
    with _connect() as conn:
        sql = "SELECT * FROM telemetry"
        args: list = []
        if device_id:
            sql += " WHERE device_id = ?"
            args.append(device_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in reversed(rows)]  # oldest first


def get_device_status(device_id: Optional[str] = None) -> list[dict]:
    with _connect() as conn:
        sql = "SELECT * FROM device_status"
        args: list = []
        if device_id:
            sql += " WHERE device_id = ?"
            args.append(device_id)
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_events(limit: int = 200, level: Optional[str] = None) -> list[dict]:
    with _connect() as conn:
        sql = "SELECT * FROM events"
        args: list = []
        if level:
            sql += " WHERE level = ?"
            args.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_symptoms(profile: Optional[str] = None, limit: int = 200) -> list[dict]:
    with _connect() as conn:
        sql = "SELECT * FROM symptoms"
        args: list = []
        if profile:
            sql += " WHERE profile = ?"
            args.append(profile)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_profiles() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in
                conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()]


# ===========================================================================
# Multi-user personalized risk management
# ===========================================================================
def _seed_default_users() -> None:
    """On first run, create two seed users so the team-risk panel is useful
    immediately. Uses the same uncalibrated-ADC thresholds as profiles."""
    with _connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return
        seeds = [
            # (name, low, moderate, high, hysteresis, lead_s, room, present)
            ("Aarav (sensitive)",  70.0,  90.0, 130.0, 5.0, 30.0, "Lab", 1),
            ("Standard user",      85.0, 100.0, 150.0, 5.0, 30.0, "Lab", 1),
        ]
        for name, low, mod, high, hyp, lead, room, present in seeds:
            cur = conn.execute(
                "INSERT INTO users (name, notes, created_at) VALUES (?,?,?)",
                (name, "Seeded default user.", utcnow_iso()))
            uid = cur.lastrowid
            conn.execute(
                "INSERT INTO user_thresholds (user_id, low_threshold,"
                " moderate_threshold, high_threshold, hysteresis,"
                " warning_lead_s) VALUES (?,?,?,?,?,?)",
                (uid, low, mod, high, hyp, lead))
            conn.execute(
                "INSERT INTO user_presence (user_id, present, room, changed_at)"
                " VALUES (?,?,?,?)",
                (uid, present, room, utcnow_iso()))


def create_user(name: str, notes: str = "") -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (name, notes, created_at) VALUES (?,?,?)",
            (name.strip(), notes, utcnow_iso()))
        uid = int(cur.lastrowid)
        # Give every new user a sensible starting threshold set.
        conn.execute(
            "INSERT INTO user_thresholds (user_id, low_threshold,"
            " moderate_threshold, high_threshold, hysteresis, warning_lead_s)"
            " VALUES (?,?,?,?,?,?)",
            (uid, config.DEFAULT_MODERATE_THRESHOLD - 15.0,
             config.DEFAULT_MODERATE_THRESHOLD,
             config.DEFAULT_HIGH_THRESHOLD,
             config.DEFAULT_HYSTERESIS, 30.0))
        conn.execute(
            "INSERT INTO user_presence (user_id, present, changed_at)"
            " VALUES (?,?,?)", (uid, 0, utcnow_iso()))
    return uid


def delete_user(user_id: int) -> None:
    # FK cascade handles thresholds / presence / risk / alerts / symptoms.
    with _connect() as conn:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def get_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT u.id, u.name, u.notes, u.created_at, t.low_threshold,"
            " t.moderate_threshold, t.high_threshold, t.hysteresis,"
            " t.warning_lead_s, t.calibration_status, p.present, p.room, p.changed_at AS presence_ts,"
            " c.level, c.last_value, c.data_source, c.updated_at AS risk_ts"
            " FROM users u"
            " LEFT JOIN user_thresholds t ON t.user_id = u.id"
            " LEFT JOIN user_presence p ON p.user_id = u.id"
            " LEFT JOIN user_current_risk c ON c.user_id = u.id"
            " ORDER BY u.name").fetchall()
    return [dict(r) for r in rows]


def get_user(user_id: int) -> Optional[dict]:
    rows = [u for u in get_users() if u["id"] == user_id]
    return rows[0] if rows else None


def set_user_thresholds(user_id: int, low: float, moderate: float,
                        high: float, hysteresis: float = 5.0,
                        warning_lead_s: float = 30.0,
                        calibration_status: str = "uncalibrated") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_thresholds (user_id, low_threshold,"
            " moderate_threshold, high_threshold, hysteresis, warning_lead_s,"
            " calibration_status) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET low_threshold=excluded."
            "low_threshold, moderate_threshold=excluded.moderate_threshold,"
            " high_threshold=excluded.high_threshold, hysteresis=excluded."
            "hysteresis, warning_lead_s=excluded.warning_lead_s,"
            " calibration_status=excluded.calibration_status",
            (user_id, low, moderate, high, hysteresis, warning_lead_s,
             calibration_status))


def set_presence(user_id: int, present: bool, room: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_presence (user_id, present, room, changed_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET present=excluded.present,"
            " room=COALESCE(excluded.room, user_presence.room),"
            " changed_at=excluded.changed_at",
            (user_id, 1 if present else 0, room, utcnow_iso()))


def present_users() -> list[dict]:
    """All users currently present, with their thresholds joined on."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT u.id, u.name, t.low_threshold, t.moderate_threshold,"
            " t.high_threshold, t.hysteresis, t.warning_lead_s, p.room"
            " FROM users u"
            " JOIN user_presence p ON p.user_id = u.id AND p.present = 1"
            " LEFT JOIN user_thresholds t ON t.user_id = u.id"
            " ORDER BY u.name").fetchall()
    return [dict(r) for r in rows]


def set_user_risk(user_id: int, level: str, last_value: float,
                  data_source: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_current_risk (user_id, level, last_value,"
            " data_source, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET level=excluded.level,"
            " last_value=excluded.last_value, data_source=excluded."
            "data_source, updated_at=excluded.updated_at",
            (user_id, level, last_value, data_source, utcnow_iso()))


def get_user_risk(user_id: Optional[int] = None) -> list[dict]:
    with _connect() as conn:
        sql = "SELECT * FROM user_current_risk"
        args: list = []
        if user_id is not None:
            sql += " WHERE user_id = ?"
            args.append(user_id)
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_open_alerts(user_id: Optional[int] = None) -> list[dict]:
    with _connect() as conn:
        sql = ("SELECT a.*, u.name FROM user_alerts a"
               " JOIN users u ON u.id = a.user_id WHERE a.closed_at IS NULL")
        args: list = []
        if user_id is not None:
            sql += " AND a.user_id = ?"
            args.append(user_id)
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def open_alert(user_id: int, level: str, value: float, data_source: str,
               note: str = "") -> int:
    """Start a new ongoing alert episode for (user, level). Returns its id.

    The caller (risk engine) must first confirm no open alert exists for the
    same user+level — this function is a pure insert, dedup happens above.
    """
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO user_alerts (user_id, level, value, data_source,"
            " started_at, last_seen, note) VALUES (?,?,?,?,?,?,?)",
            (user_id, level, value, data_source, utcnow_iso(),
             utcnow_iso(), note))
        return int(cur.lastrowid)


def touch_alert(alert_id: int, value: float) -> None:
    """Extend an open alert with the latest value WITHOUT a new row
    (requirement 7: no repeated notification for the same ongoing event)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE user_alerts SET last_seen=?, value=? WHERE id=? AND closed_at IS NULL",
            (utcnow_iso(), value, alert_id))


def close_alert(alert_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE user_alerts SET closed_at=? WHERE id=?",
                     (utcnow_iso(), alert_id))


def get_alert_history(user_id: Optional[int] = None,
                      limit: int = 100) -> list[dict]:
    with _connect() as conn:
        sql = ("SELECT a.*, u.name FROM user_alerts a"
               " JOIN users u ON u.id = a.user_id")
        args: list = []
        if user_id is not None:
            sql += " WHERE a.user_id = ?"
            args.append(user_id)
        sql += " ORDER BY a.id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def add_symptom_log(user_id: int, severity: int, text: str,
                    window_stats: Optional[dict] = None,
                    window_start: Optional[str] = None,
                    window_end: Optional[str] = None,
                    note: str = "") -> int:
    import json as _json
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO symptom_logs (user_id, ts, severity, text,"
            " window_start, window_end, window_stats, note) VALUES"
            " (?,?,?,?,?,?,?,?)",
            (user_id, utcnow_iso(), int(severity), text, window_start,
             window_end, _json.dumps(window_stats or {}), note))
        return int(cur.lastrowid)


def get_symptom_logs(user_id: Optional[int] = None,
                     limit: int = 100) -> list[dict]:
    with _connect() as conn:
        sql = ("SELECT s.*, u.name FROM symptom_logs s"
               " JOIN users u ON u.id = s.user_id")
        args: list = []
        if user_id is not None:
            sql += " WHERE s.user_id = ?"
            args.append(user_id)
        sql += " ORDER BY s.id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def record_sensor_config(data_source: str, device_id: str = "",
                         sensor_model: str = "",
                         calibration_status: str = "uncalibrated",
                         raw_units: str = "adc_counts",
                         notes: str = "") -> None:
    """Snapshot of sensor provenance so real vs simulated datasets stay
    separable in every historical query (requirement 10)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sensor_config (ts, device_id, data_source, sensor_model,"
            " calibration_status, raw_units, notes) VALUES (?,?,?,?,?,?,?)",
            (utcnow_iso(), device_id, data_source, sensor_model,
             calibration_status, raw_units, notes))


def get_sensor_config(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sensor_config ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sensor window helper (used to link symptoms to historical readings)
# ---------------------------------------------------------------------------
def sensor_window_stats(minutes_before: float,
                       data_source: Optional[str] = None) -> dict:
    """Aggregate the last N minutes of telemetry, optionally filtered by
    real vs simulated source. Returns count/min/mean/max/source so a
    symptom log can carry the sensor context it occurred against."""
    import statistics
    from datetime import timedelta
    with _connect() as conn:
        sql = ("SELECT raw_adc, filtered_adc, simulation FROM telemetry"
               " WHERE ts_received >= ?")
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(minutes=minutes_before)).isoformat()
        args: list = [cutoff]
        if data_source is not None:
            sql += " AND simulation = ?"
            args.append(1 if data_source == "simulated" else 0)
        rows = conn.execute(sql, args).fetchall()
    if not rows:
        return {"count": 0, "min": None, "mean": None, "max": None,
                "source": data_source}
    values = [float(r["filtered_adc"] if r["filtered_adc"] is not None
                    else (r["raw_adc"] or 0)) for r in rows]
    source = "mixed"
    sims = {bool(r["simulation"]) for r in rows}
    if sims == {0}:
        source = "real"
    elif sims == {1}:
        source = "simulated"
    return {"count": len(values), "min": min(values),
            "mean": round(statistics.fmean(values), 2),
            "max": max(values), "source": source}


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _bool(v) -> Optional[int]:
    if v is None:
        return None
    return 1 if v else 0


def _device_ts(v) -> Optional[str]:
    """ts_device is device millis; convert to a comparable ISO string or keep raw."""
    if v is None:
        return None
    try:
        ms = int(v)
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return str(v)
