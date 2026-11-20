"""
Service layer that turns one MQTT telemetry payload into per-user risk
state, personalized alerts (deduplicated), sensor provenance, and the
shared-buzzer decision. Called by the MQTT service on every telemetry
message. Kept DB-only so it is fully testable without a broker.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from . import database, risk_engine
from .risk_engine import UserRisk, ALERT_LEVELS


@dataclass
class EvaluationResult:
    risks: list[UserRisk]
    actions_taken: int
    shared_buzzer_on: bool          # team policy: any present user HIGH
    sensor_config_logged: bool
    value: float


def _reading_value(payload: dict) -> Optional[float]:
    """Prefer the device-filtered value; fall back to raw; else None."""
    for key in ("filtered_adc", "raw_adc"):
        v = payload.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _data_source(payload: dict) -> str:
    return "simulated" if payload.get("simulation") else "real"


def evaluate_reading(payload: dict) -> EvaluationResult:
    """Evaluate one telemetry payload against every PRESENT user's profile.

    Side effects (all in SQLite):
      - user_current_risk upserted per present user
      - user_alerts opened / touched / closed per dedup rules
      - sensor_config provenance row (only when the data source flips or
        first seen, to avoid row spam)
    No side effect on the ESP32 here — shared-buzzer state is returned so
    the caller (mqtt_service) can publish the command topic.
    """
    value = _reading_value(payload)
    src = _data_source(payload)
    if value is None:
        # No usable reading -> record provenance state, no risk change.
        return EvaluationResult([], 0, False, False, 0.0)

    users = database.present_users()
    if not users:
        _maybe_log_sensor_config(payload, src)
        return EvaluationResult([], 0, False, True, value)

    prev_risks = {r["user_id"]: r for r in database.get_user_risk()}
    open_by_user: dict[int, dict[str, int]] = {
        u["id"]: {a["level"]: a["id"]
                  for a in database.get_open_alerts(user_id=u["id"])}
        for u in users
    }

    risks: list[UserRisk] = []
    for u in users:
        thr = risk_engine.Thresholds.from_row(u)
        prev_level = prev_risks.get(u["id"], {}).get("level", "CLEAR")
        level = risk_engine.classify(value, thr, prev_level)
        risk = UserRisk(user_id=u["id"], user_name=u["name"],
                        level=level, previous_level=prev_level,
                        changed=(level != prev_level), value=value,
                        data_source=src, thresholds=thr)
        risks.append(risk)

        database.set_user_risk(u["id"], level, value, src)

        # Apply deduplicated alert actions.
        for act in risk_engine.plan_alert_actions(risk, open_by_user[u["id"]]):
            if act.action == "open":
                database.open_alert(risk.user_id, act.level, value, src)
                _log_event_for_alert(risk, "opened")
            elif act.action == "touch":
                database.touch_alert(act.alert_id, value)
            elif act.action == "close":
                database.close_alert(act.alert_id)
                _log_event_for_alert(risk, "cleared", act.level)

    _maybe_log_sensor_config(payload, src)
    team = risk_engine.team_high_users(risks)
    return EvaluationResult(
        risks=risks,
        actions_taken=len(risks),
        shared_buzzer_on=bool(team),
        sensor_config_logged=True,
        value=value)


def _log_event_for_alert(risk: UserRisk, kind: str, level: Optional[str] = None) -> None:
    lvl = level or risk.level
    database.record_event(
        device_id="backend",
        level=lvl,
        message=f"user '{risk.user_name}' {kind} {lvl} alert "
                f"(value {risk.value:.1f}, {risk.data_source} data)",
        source="backend-personalization",
        detail={"user_id": risk.user_id, "kind": kind,
                "data_source": risk.data_source})


_last_source: Optional[str] = None


def _maybe_log_sensor_config(payload: dict, src: str) -> bool:
    """Log a provenance row only when the data source flips (real<->sim)
    or on the very first observation. Prevents one-row-per-telemetry spam.
    """
    global _last_source
    if src == _last_source:
        return False
    _last_source = src
    database.record_sensor_config(
        data_source=src,
        device_id=str(payload.get("device_id", "")),
        sensor_model=str(payload.get("sensor_model", "MQ-series")),
        calibration_status="uncalibrated",
        raw_units="adc_counts")
    return True


# ---------------------------------------------------------------------------
# Shared buzzer command builder (published to the device by mqtt_service)
# ---------------------------------------------------------------------------
def build_buzzer_command(on: bool, reasons: list[str],
                         device_id: str = "") -> dict:
    """Structured command for the ESP32's shared-policy buzzer input.

    The device treats this as the *team* policy: on = at least one present
    user is HIGH. It still runs its own local fallback, so a lost backend
    or Wi-Fi link does not disable the device-side safety path.
    """
    return {
        "device_id": device_id,
        "event": "buzzer_command",
        "command": "on" if on else "off",
        "reasons": reasons,          # e.g. ["Aarav (sensitive) HIGH"]
        "shared_policy": True,
        "local_fallback_unchanged": True,
        "ts": 0,                    # device fills / ignores
    }
