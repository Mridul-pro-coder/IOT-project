"""
AirSense backend configuration.

All credentials / hostnames come from the environment so that nothing
sensitive is committed. Copy `.env.example` to `.env` and fill it in.
`python-dotenv` loads it automatically for convenience, but real deployment
can set the same variables in the shell / Windows env instead.
"""
from __future__ import annotations

import os
from pathlib import Path

# Optional: load .env from the repo root if present (no-op if absent).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover - dotenv is a soft dep for config
    pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("AIRSENSE_DATA_DIR", PROJECT_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("AIRSENSE_DB", DATA_DIR / "airsense.db"))

# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")        # empty = no auth
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "airsense/group9/")
TOPIC_TELEMETRY = TOPIC_PREFIX + "telemetry"
TOPIC_STATUS = TOPIC_PREFIX + "status"
TOPIC_ALERTS = TOPIC_PREFIX + "alerts"
TOPIC_CONFIG = TOPIC_PREFIX + "config"
TOPIC_LWT = TOPIC_PREFIX + "lastwill"

MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "airsense-backend")

# Device is considered OFFLINE if we haven't seen telemetry/status for this
# many seconds. This is the heartbeat TTL the dashboard uses.
DEVICE_OFFLINE_TTL_SECONDS = int(os.getenv("DEVICE_OFFLINE_TTL", "30"))

# ---------------------------------------------------------------------------
# Defaults for exposure thresholds (uncalibrated ADC units). These mirror the
# firmware defaults and are used as the "device standard" profile seed.
# ---------------------------------------------------------------------------
DEFAULT_MODERATE_THRESHOLD = float(os.getenv("DEFAULT_MODERATE", "100.0"))
DEFAULT_HIGH_THRESHOLD = float(os.getenv("DEFAULT_HIGH", "150.0"))
DEFAULT_HYSTERESIS = float(os.getenv("DEFAULT_HYSTERESIS", "5.0"))
DEFAULT_EWMA_ALPHA = float(os.getenv("DEFAULT_EWMA_ALPHA", "0.30"))
