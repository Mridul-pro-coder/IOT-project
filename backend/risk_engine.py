"""
Per-user personalized risk engine.

Given ONE shared sensor reading, classify each PRESENT user's environmental
risk band independently from their saved thresholds, using hysteresis so
band changes don't flap on noisy data. This is the module that makes "the
same reading -> different warnings per user" true.

Bands (low to high):  CLEAR < LOW < MODERATE < HIGH
  - HIGH     when value >= high     - hyst
  - MODERATE when value >= moderate - hyst   (and not HIGH)
  - LOW      when value >= low      - hyst   (and not MODERATE)
  - CLEAR    otherwise
Hysteresis is applied at entry (threshold - hyst) and at exit
(threshold - 2*hyst) in the direction of travel, so a reading must move a
full hysteresis band before it can re-enter a band it just left.

This is a rule-based environmental model: it says nothing about clinical
outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

LEVELS = ("CLEAR", "LOW", "MODERATE", "HIGH")
# Bands that generate personalized alerts (LOW/CLEAR are informational only).
ALERT_LEVELS = ("MODERATE", "HIGH")


@dataclass(frozen=True)
class Thresholds:
    low: float
    moderate: float
    high: float
    hysteresis: float = 5.0
    warning_lead_s: float = 30.0

    @classmethod
    def from_row(cls, row: dict) -> "Thresholds":
        return cls(low=float(row["low_threshold"]),
                   moderate=float(row["moderate_threshold"]),
                   high=float(row["high_threshold"]),
                   hysteresis=float(row.get("hysteresis") or 5.0),
                   warning_lead_s=float(row.get("warning_lead_s") or 30.0))


@dataclass
class UserRisk:
    """Result of evaluating one reading for one user."""
    user_id: int
    user_name: str
    level: str                    # current band after hysteresis
    previous_level: str           # band before this reading
    changed: bool
    value: float
    data_source: str              # 'real' | 'simulated'
    thresholds: Thresholds

    @property
    def alerting(self) -> bool:
        return self.level in ALERT_LEVELS


def classify(value: float, thr: Thresholds,
             prev_level: str = "CLEAR") -> str:
    """Return the band for `value` given `prev_level` (hysteresis state).

    Entry into a band happens at (threshold - hyst); exit at
    (threshold - 2*hyst). Moving UP uses entry thresholds; moving DOWN
    uses exit thresholds. Unknown/missing prev_level is treated as CLEAR.
    """
    if prev_level not in LEVELS:
        prev_level = "CLEAR"
    h = thr.hysteresis
    prev_rank = LEVELS.index(prev_level)
    cur_no_hyst = _band_no_hysteresis(value, thr)
    cur_rank = LEVELS.index(cur_no_hyst)

    if cur_rank > prev_rank:
        # Rising: only step up one band at a time, and only through the
        # *entry* threshold (threshold - hyst). A big jump still climbs
        # one band per reading, which is the desired debounce behaviour.
        target = LEVELS[cur_rank]
        entry_value = _entry_value(target, thr)
        if value >= entry_value:
            return target
        return prev_level

    if cur_rank < prev_rank:
        # Falling: only drop one band at a time, and only below the *exit*
        # threshold (threshold - 2*hyst).
        target = LEVELS[cur_rank]
        exit_value = _exit_value(LEVELS[prev_rank], thr)
        if value < exit_value:
            return target
        return prev_level

    return prev_level


def _band_no_hysteresis(value: float, thr: Thresholds) -> str:
    if value >= thr.high:
        return "HIGH"
    if value >= thr.moderate:
        return "MODERATE"
    if value >= thr.low:
        return "LOW"
    return "CLEAR"


def _entry_value(target: str, thr: Thresholds) -> float:
    """Value at which `target` is entered (threshold - hyst)."""
    m = {"HIGH": thr.high, "MODERATE": thr.moderate, "LOW": thr.low}
    return m[target] - thr.hysteresis


def _exit_value(band: str, thr: Thresholds) -> float:
    """Value below which `band` is exited (threshold - 2*hyst)."""
    m = {"HIGH": thr.high, "MODERATE": thr.moderate, "LOW": thr.low}
    return m[band] - 2.0 * thr.hysteresis


# ---------------------------------------------------------------------------
# Alert dedup: one open row per (user, level) ongoing episode
# ---------------------------------------------------------------------------
@dataclass
class AlertAction:
    user_id: int
    user_name: str
    action: str                  # 'open' | 'touch' | 'close' | None
    level: str
    alert_id: Optional[int] = None


def plan_alert_actions(risk: UserRisk,
                       open_alerts: dict[str, int]) -> list[AlertAction]:
    """Pure dedup logic (requirement 7).

    `open_alerts` maps level -> alert_id for this user's currently open
    alerts. Returns the actions to take:
      - entered a new alert band      -> open (and close any lower open one)
      - still in the same alert band  -> touch (move last_seen, no new row)
      - left an alert band            -> close
    """
    actions: list[AlertAction] = []
    new_level = risk.level
    old_level = risk.previous_level

    # Leaving a band -> close its open alert, if any.
    if old_level in ALERT_LEVELS and old_level != new_level:
        aid = open_alerts.get(old_level)
        if aid is not None:
            actions.append(AlertAction(risk.user_id, risk.user_name,
                                       "close", old_level, aid))

    # Entering a new alert band -> open it.
    if new_level in ALERT_LEVELS and new_level != old_level:
        # A jump from LOW/CLEAR straight to HIGH: nothing lower was open.
        # A jump MODERATE -> HIGH: the MODERATE close was issued above; a
        # fresh HIGH alert opens. (One open alert per user, per level.)
        if new_level not in open_alerts:
            actions.append(AlertAction(risk.user_id, risk.user_name,
                                       "open", new_level))

    # Ongoing: still in the same alert band -> touch only.
    if new_level in ALERT_LEVELS and new_level == old_level:
        if new_level in open_alerts:
            actions.append(AlertAction(risk.user_id, risk.user_name,
                                       "touch", new_level,
                                       open_alerts[new_level]))

    return actions


def team_high_users(risks: list[UserRisk]) -> list[UserRisk]:
    """Users currently in HIGH — the shared buzzer policy triggers on any
    of these being present (requirement 8)."""
    return [r for r in risks if r.level == "HIGH" and r.alerting]
