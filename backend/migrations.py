"""
Schema migrations for AirSense.

Existing databases are upgraded in place: each migration is applied exactly
once (tracked in `schema_migrations`), never re-run, and only ever ADDS
tables/columns — it never drops or rewrites existing data. Existing
telemetry / events / device_status / symptoms / profiles rows are preserved.
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from . import config

# ---------------------------------------------------------------------------
# Migration 1 — the original base schema (kept verbatim from the first release
# so databases created before multi-user still validate as "v1 applied").
# ---------------------------------------------------------------------------
_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    ts_received TEXT NOT NULL,
    ts_device   TEXT,
    raw_adc     REAL,
    filtered_adc REAL,
    exposure    TEXT,
    buzzer      INTEGER,
    wifi_rssi   INTEGER,
    mqtt_connected INTEGER,
    simulation  INTEGER,
    sensor_fault INTEGER,
    uptime_s    REAL
);
CREATE INDEX IF NOT EXISTS idx_tele_device_time ON telemetry(device_id, ts_received);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    level      TEXT NOT NULL,
    message    TEXT,
    source     TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts);

CREATE TABLE IF NOT EXISTS device_status (
    device_id  TEXT PRIMARY KEY,
    state      TEXT,
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

# ---------------------------------------------------------------------------
# Migration 2 — multi-user personalized risk management.
# ---------------------------------------------------------------------------
_MIGRATION_2 = """
-- A person whose risk is tracked. `name` is the identity used across the UI.
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    notes       TEXT,
    created_at  TEXT NOT NULL
);

-- One threshold set per user. Levels are computed as:
--   HIGH     : value >= high_threshold
--   MODERATE : value >= moderate_threshold
--   LOW      : value >= low_threshold        (mild "elevated" band)
--   CLEAR    : value <  low_threshold
-- Hysteresis debounces band entry/exit. All values are in the sensor's
-- uncalibrated ADC units.
CREATE TABLE IF NOT EXISTS user_thresholds (
    user_id           INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    low_threshold     REAL NOT NULL,
    moderate_threshold REAL NOT NULL,
    high_threshold    REAL NOT NULL,
    hysteresis        REAL NOT NULL DEFAULT 5.0,
    warning_lead_s    REAL NOT NULL DEFAULT 30.0,
    calibration_status TEXT NOT NULL DEFAULT 'uncalibrated',
    notes             TEXT
);

-- Presence: is this user in the monitored room right now?
-- Only present users are evaluated against incoming readings.
CREATE TABLE IF NOT EXISTS user_presence (
    user_id   INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    present   INTEGER NOT NULL DEFAULT 0,
    room      TEXT,
    changed_at TEXT NOT NULL
);

-- Live per-user risk state (one row per user, overwritten each evaluation).
CREATE TABLE IF NOT EXISTS user_current_risk (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    level      TEXT NOT NULL,        -- CLEAR / LOW / MODERATE / HIGH
    last_value REAL,
    data_source TEXT,                -- 'real' | 'simulated'
    updated_at TEXT NOT NULL
);

-- Append-only personalized alerts. One row per *ongoing* (level, user)
-- episode: opened when the user ENTERS a MODERATE/HIGH band, closed (with
-- closed_at) when they drop below it. The same continuing event is NOT
-- re-notified — only its last_seen moves.
CREATE TABLE IF NOT EXISTS user_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    level       TEXT NOT NULL,       -- MODERATE / HIGH
    value       REAL,
    data_source TEXT,
    started_at  TEXT NOT NULL,
    closed_at   TEXT,
    last_seen   TEXT NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_alerts_open ON user_alerts(user_id, closed_at);

-- Timestamped symptom/discomfort logs, linked to the sensor window they
-- occurred against.
CREATE TABLE IF NOT EXISTS symptom_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts            TEXT NOT NULL,
    severity      INTEGER NOT NULL,
    text          TEXT,
    window_start  TEXT,             -- sensor window the log is linked to
    window_end    TEXT,
    window_stats  TEXT,             -- JSON: {count, min, mean, max, source}
    note          TEXT
);

-- Sensor provenance / configuration history: records whether readings are
-- real or simulated and the sensor's calibration status, so real and
-- simulated datasets stay separable in every historical query.
CREATE TABLE IF NOT EXISTS sensor_config (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    device_id       TEXT,
    data_source     TEXT,            -- 'real' | 'simulated'
    sensor_model    TEXT,
    calibration_status TEXT,         -- 'uncalibrated' | 'calibrated' | ...
    raw_units       TEXT,
    notes           TEXT
);

-- Reserved for the future per-user calibration layer (ML). Populated only
-- once a real model + evaluation exists; created now so the schema is stable.
CREATE TABLE IF NOT EXISTS user_calibration (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    model_id   TEXT NOT NULL,
    parameters TEXT,                -- JSON blob, model-defined
    trained_at TEXT,
    evaluated  INTEGER NOT NULL DEFAULT 0
);
"""

# Ordered migrations: (version, name, sql)
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "base_schema", _MIGRATION_1),
    (2, "multi_user", _MIGRATION_2),
]


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def run_migrations() -> list[str]:
    """Apply any pending migrations. Returns the names of newly applied ones."""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "applied_at TEXT NOT NULL)")
    from datetime import datetime, timezone
    already = applied_versions(conn)
    applied_now: list[str] = []
    for version, name, sql in MIGRATIONS:
        if version in already:
            continue
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations (version, name, applied_at)"
                     " VALUES (?,?,?)",
                     (version, name, datetime.now(timezone.utc).isoformat()))
        applied_now.append(name)
        print(f"[migrations] applied v{version} ({name})")
    conn.commit()
    conn.close()
    return applied_now
