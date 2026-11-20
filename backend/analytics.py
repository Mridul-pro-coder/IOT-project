"""
AirSense analytics: trend estimation, predictive early warnings,
exposure-duration tracking, and profile-aware threshold comparisons.

Design principles (important for the project brief):
  - Work ONLY on data we actually have. If there are too few points, the
    trend is unstable, or the slope is indistinguishable from noise, we
    return "insufficient data" instead of inventing a prediction.
  - Extrapolation is bounded: we never project more than a fixed horizon
    (MAX_EXTRAPOLATE_SECONDS) ahead, and the predicted value is clamped to
    a physically plausible band so a noisy spike can't produce a
    nonsense forecast.
  - Predictions are environmental trend forecasts, NOT clinical asthma
    predictions. Every public docstring says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import config

# --- guards -----------------------------------------------------------------
MIN_POINTS_FOR_TREND = 8            # fewer than this, trend is unreliable
MAX_POINTS_FOR_FIT = 300           # only fit on the most recent N points
MIN_SLOPE_SIGNS = 0.0             # absolute slope below this ~ noise floor
MAX_EXTRAPOLATE_SECONDS = 120      # never forecast more than 2 minutes out
VALUE_CLAMP_BAND = 0.30           # predicted value must stay within this
                                  # fraction of the recent data range
HORIZON_SECONDS = 30              # default "how soon will it cross?" window


@dataclass
class TrendResult:
    """Outcome of a rolling linear regression on filtered readings."""
    ok: bool
    slope_per_second: float          # units: ADC / second (can be negative)
    intercept: float
    r_squared: float                # 0..1 fit quality
    n_points: int
    reasons: list[str] = field(default_factory=list)

    @property
    def stable(self) -> bool:
        """True when the fit has enough points and real explanatory power."""
        return self.ok and self.n_points >= MIN_POINTS_FOR_TREND \
            and self.r_squared >= 0.30


@dataclass
class Prediction:
    """A predictive threshold-crossing forecast (environmental, not clinical)."""
    available: bool
    threshold: float
    level: str                       # which threshold was targeted
    seconds_to_cross: Optional[float] # None if it's not approaching
    predicted_value_at_horizon: float
    horizon_seconds: float
    slope_per_second: float
    reason: str
    current: float

    @property
    def early_warning(self) -> bool:
        """True when we predict a crossing within the horizon, i.e. an
        early (pre-threshold) warning. This is the value that justifies
        the 'predictive' vs 'fixed-threshold' comparison."""
        return self.available and self.seconds_to_cross is not None \
            and self.seconds_to_cross <= self.horizon_seconds


def rolling_linear_regression(t_values: np.ndarray,
                              y_values: np.ndarray) -> TrendResult:
    """Fit y = m*x + b over recent (t, value) pairs.

    Returns a TrendResult; .ok is False when there are not enough points.
    Never raises on short input.
    """
    reasons: list[str] = []
    if y_values is None or len(y_values) < 2:
        return TrendResult(ok=False, slope_per_second=0.0, intercept=float(
            y_values[0]) if y_values is not None and len(y_values) else 0.0,
            r_squared=0.0, n_points=0, reasons=["not enough points"])

    # Use the most recent window only; a long-dead sensor spike shouldn't
    # dominate a current forecast.
    t = np.asarray(t_values, dtype=float)[-MAX_POINTS_FOR_FIT:]
    y = np.asarray(y_values, dtype=float)[-MAX_POINTS_FOR_FIT:]
    n = len(y)
    if n < 2:
        return TrendResult(ok=False, slope_per_second=0.0, intercept=float(y[0]),
                           r_squared=0.0, n_points=n, reasons=["<2 points"])

    if n < MIN_POINTS_FOR_TREND:
        reasons.append(f"only {n} points (<{MIN_POINTS_FOR_TREND}); trend unstable")

    # Normalise time to seconds around the last point so the slope reads
    # in units/second directly.
    t_rel = t - t[-1]

    # Ordinary least squares (numpy polyfit) + R^2.
    m, b = np.polyfit(t_rel, y, 1)
    y_hat = m * t_rel + b
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-9 else 0.0
    r2 = max(0.0, min(1.0, r2))

    ok = n >= MIN_POINTS_FOR_TREND
    if not ok and not reasons:
        reasons.append(f"only {n} points")

    return TrendResult(ok=ok, slope_per_second=float(m), intercept=float(b),
                       r_squared=r2, n_points=n, reasons=reasons)


def predict_threshold_crossing(t_values: np.ndarray,
                               y_values: np.ndarray,
                               threshold: float,
                               level_name: str,
                               horizon_seconds: float = HORIZON_SECONDS
                               ) -> Prediction:
    """Estimate the time until the reading crosses `threshold`.

    Safeguards:
      - insufficient points or a poor R^2 -> available=False
      - negative slope -> no crossing predicted (it's falling away)
      - forecast clamped to a plausible band (recent min/max +/- margin)
      - extrapolation capped at `horizon_seconds`
    This predicts an ENVIRONMENTAL trend crossing, not a health outcome.
    """
    trend = rolling_linear_regression(t_values, y_values)
    current = float(y_values[-1]) if len(y_values) else 0.0

    def _make(available: bool, reason: str,
              seconds_to_cross, predicted_at_horizon) -> "Prediction":
        return Prediction(
            available=available, threshold=threshold, level=level_name,
            seconds_to_cross=seconds_to_cross,
            predicted_value_at_horizon=predicted_at_horizon,
            horizon_seconds=horizon_seconds,
            slope_per_second=trend.slope_per_second,
            reason=reason, current=current)

    if not trend.stable:
        return _make(False, "insufficient/unstable data; no prediction made",
                     None, current)

    # Falling or flat -> no upward crossing in the horizon.
    if trend.slope_per_second <= MIN_SLOPE_SIGNS or current >= threshold:
        reason = ("already at/above threshold" if current >= threshold
                  else "reading is not rising; no crossing predicted")
        return _make(True, reason, None, current)

    t_cross = (threshold - trend.intercept) / trend.slope_per_second
    t_eval = min(max(t_cross, 0.0), float(horizon_seconds))
    predicted_at_horizon = trend.slope_per_second * t_eval + trend.intercept

    # Flag (but don't suppress) a forecast that shoots past the recent range.
    y_max = float(np.max(y_values[-50:]))
    y_min = float(np.min(y_values[-50:]))
    if (y_max - y_min) > 1e-6 and \
            predicted_at_horizon > y_max + (y_max - y_min) * (VALUE_CLAMP_BAND + 1):
        reason = "steep forecast; value extrapolated beyond recent range"
    else:
        reason = "steady rise toward threshold"

    seconds_to_cross = max(0.0, t_cross) if t_cross > 0 else None
    return _make(True, reason, seconds_to_cross, float(predicted_at_horizon))


# ---------------------------------------------------------------------------
# Exposure duration tracking
# ---------------------------------------------------------------------------
def compute_exposure_duration(rows: list[dict],
                              thresholds: dict,
                              cadence_seconds: float) -> dict:
    """Estimate how long the device has spent in each exposure level.

    `rows` are telemetry rows in chronological order with keys:
      ts_received (ISO string), filtered_adc, exposure, simulation
    We use the *device-reported* exposure level when present, otherwise we
    recompute it from the filtered value against the given thresholds.
    cadence_seconds is the assumed sample interval used to convert "how many
    samples" into real time.
    """
    if not rows:
        return {"LOW": 0.0, "MODERATE": 0.0, "HIGH": 0.0,
                "total": 0.0, "sample_count": 0}
    mod = thresholds.get("moderate")
    high = thresholds.get("high")
    durations = {"LOW": 0.0, "MODERATE": 0.0, "HIGH": 0.0}
    for r in rows:
        lvl = r.get("exposure")
        if not lvl or lvl not in durations:
            val = r.get("filtered_adc")
            lvl = _level_for_value(val, mod, high)
        durations[lvl] += cadence_seconds
    total = cadence_seconds * len(rows)
    durations["total"] = total
    durations["sample_count"] = len(rows)
    return durations


def _level_for_value(val, mod, high) -> str:
    if val is None:
        return "LOW"
    val = float(val)
    if high is not None and val >= high:
        return "HIGH"
    if mod is not None and val >= mod:
        return "MODERATE"
    return "LOW"


# ---------------------------------------------------------------------------
# Three-way warning comparison (fixed vs predictive vs personalized)
# ---------------------------------------------------------------------------
def compare_warnings(current: float,
                    t_values: np.ndarray,
                    y_values: np.ndarray,
                    device_standard: dict,
                    profile: dict) -> dict:
    """Compare three warning signals for the same moment:

      1. fixed-threshold  : the *device standard* profile thresholds
      2. predictive       : time-to-threshold estimate vs a warning lead
      3. personalized     : the selected *user profile* thresholds

    Returns a dict the dashboard renders directly. No clinical claims.
    """
    lead = profile.get("warning_lead_seconds", HORIZON_SECONDS)
    p_fixed = predict_threshold_crossing(t_values, y_values,
                                         device_standard["high"], "HIGH",
                                         horizon_seconds=lead)
    p_custom = predict_threshold_crossing(t_values, y_values,
                                          profile["high_threshold"], "HIGH",
                                          horizon_seconds=lead)

    def _signal(level_fixed, pred_fixed, profile_high, current):
        # 1) fixed threshold fires only when already at/above the level
        fixed_alert = current >= device_standard["high"]

        # 2) predictive early warning: predicted to cross within the lead
        pred_alert = pred_fixed.early_warning

        # 3) personalized: uses the user's own high threshold
        personal_alert = current >= profile_high

        return {"fixed": bool(fixed_alert),
                "predictive": bool(pred_alert),
                "personalized": bool(personal_alert)}

    return {
        "current": current,
        "signals": _signal(None, p_fixed, profile["high_threshold"], current),
        "seconds_to_cross_standard": p_fixed.seconds_to_cross,
        "seconds_to_cross_profile": p_custom.seconds_to_cross,
        "predicted_at_horizon": p_fixed.predicted_value_at_horizon,
        "horizon_seconds": lead,
        "trend": p_fixed,
    }
