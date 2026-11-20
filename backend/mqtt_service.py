"""
AirSense MQTT service.

Runs as a standalone process. Responsibilities:
  - subscribe to device telemetry / status / alerts / lastwill topics
  - validate incoming JSON
  - persist to SQLite
  - track device online/offline (heartbeat TTL)
  - publish validated device config back to the ESP32

The Streamlit dashboard reads the SQLite database directly, so the two
processes stay decoupled. Start this service BEFORE (or alongside) the
dashboard:

    python -m backend.mqtt_service
"""
from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

from . import config, database


class AirsenseMQTTService:
    """Manages the paho client and routes messages to persistence + alerts."""

    def __init__(self) -> None:
        database.init_db()
        self.client = mqtt.Client(client_id=config.MQTT_CLIENT_ID,
                                  protocol=mqtt.MQTTv311)
        if config.MQTT_USER:
            self.client.username_pw_set(config.MQTT_USER, config.MQTT_PASSWORD)
        self._running = False
        self._wire_callbacks()

    # -- paho callbacks ----------------------------------------------------
    def _wire_callbacks(self) -> None:
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, _userdata, _flags, _reason_code) -> None:
        print(f"[mqtt] connected to {config.MQTT_HOST}:{config.MQTT_PORT}; "
              f"subscribing to {config.TOPIC_PREFIX}#")
        # Wildcard subscribes to everything under the prefix, incl. lastwill.
        client.subscribe(config.TOPIC_PREFIX + "#", qos=0)

    def _on_disconnect(self, client, _userdata, _reason_code) -> None:
        print(f"[mqtt] broker connection lost (rc={reason_desc(_reason_code)}); "
              f"auto-reconnect will retry.")

    def _on_message(self, client, _userdata, msg) -> None:
        topic = msg.topic
        raw = msg.payload
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"[mqtt] non-JSON payload on {topic}: {exc}")
            return

        if not isinstance(payload, dict):
            print(f"[mqtt] payload is not an object on {topic}: {raw!r}")
            return

        if topic.endswith("telemetry"):
            self._handle_telemetry(payload)
        elif topic.endswith("status"):
            self._handle_status(payload)
        elif topic.endswith("lastwill"):
            self._handle_lastwill(payload)
        elif topic.endswith("alerts"):
            self._handle_alert(payload)
        elif topic.endswith("config"):
            self._handle_config(payload)
        else:
            print(f"[mqtt] unknown topic {topic}")

    # -- topic handlers ----------------------------------------------------
    def _handle_telemetry(self, p: dict) -> None:
        # Light validation: require a device id and a numeric reading of some
        # kind. Malformed messages are dropped, not written.
        if "device_id" not in p and "raw_adc" not in p and "filtered_adc" not in p:
            print(f"[mqtt] telemetry rejected (missing fields): {p}")
            return
        database.insert_telemetry(p)

        # Derive backend-side exposure events from transitions, so the
        # "events" table is populated even if the firmware didn't emit one.
        _emit_transition_event(p)

        # Multi-user personalization: evaluate this reading against every
        # present user's thresholds and apply the shared buzzer policy.
        from . import evaluation
        try:
            result = evaluation.evaluate_reading(p)
            if result.risks:
                high = [r for r in result.risks if r.level == "HIGH"]
                if high:
                    print(f"[person] HIGH: {', '.join(r.user_name for r in high)}")
                cmd = evaluation.build_buzzer_command(
                    result.shared_buzzer_on,
                    reasons=[f"{r.user_name} HIGH" for r in high],
                    device_id=str(p.get("device_id", "")))
                if self.client.is_connected():
                    info = self.client.publish(
                        config.TOPIC_PREFIX + "cmd/buzzer",
                        json.dumps(cmd), qos=0)
                    info.wait_for_publish(timeout=2.0)
                    print(f"[buzzer] shared policy -> {cmd['command']}")
        except Exception as exc:  # noqa: BLE001 - personalization must not
            # crash the ingest path; log and continue.
            print(f"[person] evaluation failed: {exc}")

    def _handle_status(self, p: dict) -> None:
        database.upsert_device_status(p)

    def _handle_lastwill(self, p: dict) -> None:
        # Broker published our LWT -> the device is gone.
        database.upsert_device_status({**p, "state": "offline"})
        print(f"[mqtt] LWT received: {p.get('device_id')} marked offline")

    def _handle_alert(self, p: dict) -> None:
        database.record_event(
            device_id=str(p.get("device_id", "unknown")),
            level=str(p.get("level", "UNKNOWN")),
            message=str(p.get("message", "")),
            source="device",
            detail={k: v for k, v in p.items() if k not in
                    ("device_id", "level", "message", "event")})

    def _handle_config(self, p: dict) -> None:
        # The device ACKs config_applied / config_rejected here.
        ev = p.get("event", "")
        if ev in ("config_applied", "config_rejected"):
            print(f"[mqtt] device config {ev}: {p}")
        # (config commands sent TO the device originate from publish_config.)

    # -- outbound ----------------------------------------------------------
    def publish_config(self, cfg: dict) -> int:
        """Publish a validated config to the device. Returns paho result code.

        Validation mirrors the firmware's reject rules so we don't push
        configurations the device would refuse.
        """
        errors = validate_config(cfg)
        if errors:
            print(f"[mqtt] config rejected locally: {errors}")
            raise ValueError("invalid config: " + "; ".join(errors))
        payload = {
            "moderate_threshold": cfg.get("moderate_threshold",
                                          config.DEFAULT_MODERATE_THRESHOLD),
            "high_threshold": cfg.get("high_threshold",
                                      config.DEFAULT_HIGH_THRESHOLD),
            "hysteresis": cfg.get("hysteresis", config.DEFAULT_HYSTERESIS),
            "ewma_alpha": cfg.get("ewma_alpha", config.DEFAULT_EWMA_ALPHA),
            "buzzer_enabled": cfg.get("buzzer_enabled", True),
            "publish_interval_ms": cfg.get("publish_interval_ms", 2000),
        }
        info = self.client.publish(config.TOPIC_CONFIG,
                                   json.dumps(payload), qos=0)
        info.wait_for_publish(timeout=5.0)
        return info.rc

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=60)
        self.client.loop_start()
        self._running = True
        print("[mqtt] service running. Ctrl+C to stop.")

    def stop(self) -> None:
        self._running = False
        self.client.loop_stop()
        self.client.disconnect()

    def run_forever(self) -> None:
        self.start()
        while self._running:
            time.sleep(1)


# ---------------------------------------------------------------------------
# Validation shared by the service (before publishing) and by the dashboard
# ---------------------------------------------------------------------------
def validate_config(cfg: dict) -> list[str]:
    """Return a list of problems; empty list means the config is acceptable."""
    errs: list[str] = []
    mod = cfg.get("moderate_threshold")
    high = cfg.get("high_threshold")
    hyp = cfg.get("hysteresis", 0.0)
    alpha = cfg.get("ewma_alpha")
    pub = cfg.get("publish_interval_ms")

    for name, val in (("moderate_threshold", mod), ("high_threshold", high)):
        if val is None:
            errs.append(f"{name} required")
            continue
        if not (0 <= float(val) <= 4095):
            errs.append(f"{name} out of range [0,4095]")

    if mod is not None and high is not None:
        try:
            if float(high) - float(mod) <= 0:
                errs.append("high_threshold must exceed moderate_threshold")
        except (TypeError, ValueError):
            pass

    if hyp is not None:
        try:
            hyp_f = float(hyp)
            if hyp_f < 0:
                errs.append("hysteresis must be >= 0")
            if mod is not None and high is not None and \
                    float(high) - float(mod) > 0:
                if hyp_f > 0.5 * (float(high) - float(mod)):
                    errs.append("hysteresis too large for threshold gap")
        except (TypeError, ValueError):
            errs.append("hysteresis must be numeric")

    if alpha is not None:
        try:
            if not (0.0 < float(alpha) <= 1.0):
                errs.append("ewma_alpha must be in (0,1]")
        except (TypeError, ValueError):
            errs.append("ewma_alpha must be numeric")

    if pub is not None:
        try:
            if not (500 <= int(pub) <= 30000):
                errs.append("publish_interval_ms must be 500..30000")
        except (TypeError, ValueError):
            errs.append("publish_interval_ms must be an integer")

    return errs


# ---------------------------------------------------------------------------
# Backend-side event derivation
# ---------------------------------------------------------------------------
_transition_state: dict[str, str] = {}


def _emit_transition_event(payload: dict) -> None:
    """Record an event when the exposure level transitions (device-said level).

    This is a best-effort mirror of the firmware's alert topic, so the
    history view works even if alerts are lost. Deduplication: one event per
    device per level transition.
    """
    dev = str(payload.get("device_id", "unknown"))
    level = str(payload.get("exposure", "UNKNOWN"))
    prev = _transition_state.get(dev)
    if prev != level:
        if prev is not None:  # skip the very first (no "previous" to report)
            database.record_event(
                device_id=dev,
                level=level,
                message=f"exposure transition {prev} -> {level}",
                source="backend",
                detail={"filtered_adc": payload.get("filtered_adc"),
                        "simulation": bool(payload.get("simulation"))})
        _transition_state[dev] = level


def reason_desc(rc) -> str:
    try:
        return mqtt.connack_string(rc) if hasattr(mqtt, "connack_string") else str(rc)
    except Exception:
        return str(rc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    svc = AirsenseMQTTService()

    def _sig(_signum, _frame):
        print("\n[mqtt] shutting down...")
        svc.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        svc.run_forever()
    except KeyboardInterrupt:
        svc.stop()


if __name__ == "__main__":
    main()
