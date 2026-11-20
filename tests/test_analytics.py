"""Unit tests for the AirSense analytics module (no hardware, no broker)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import analytics  # noqa: E402


def _rising(n=60, start=70.0, rate=2.0):
    """A steady rising series with a bit of noise: ~2 units/sec."""
    t = np.arange(n, dtype=float) * 2.0
    y = start + rate * 2.0 * t / 2.0 + np.sin(t) * 1.5
    return t, y


def test_insufficient_data_returns_no_prediction():
    t = np.arange(3) * 2.0
    y = np.array([70, 72, 75], dtype=float)
    pred = analytics.predict_threshold_crossing(t, y, threshold=150.0,
                                                 level_name="HIGH")
    assert not pred.available
    assert pred.seconds_to_cross is None
    assert "data" in pred.reason.lower() or "unstable" in pred.reason.lower()


def test_rising_trend_predicts_a_crossing():
    t, y = _rising()
    # Target a threshold a bit above the current value: with ~2 units/s the
    # crossing should be seconds away, not "already above".
    threshold = float(y[-1]) + 10.0
    pred = analytics.predict_threshold_crossing(t, y, threshold=threshold,
                                                level_name="HIGH",
                                                horizon_seconds=120.0)
    assert pred.available
    assert pred.seconds_to_cross is not None
    assert 0.0 <= pred.seconds_to_cross <= 60.0
    assert pred.early_warning


def test_falling_trend_does_not_predict_crossing():
    t = np.arange(30) * 2.0
    y = np.linspace(200, 80, 30)  # declining
    pred = analytics.predict_threshold_crossing(t, y, threshold=150.0,
                                                level_name="HIGH")
    assert pred.seconds_to_cross is None


def test_at_or_above_threshold_reports_no_upward_crossing():
    t = np.arange(30) * 2.0
    y = np.full(30, 180.0)  # already above 150
    pred = analytics.predict_threshold_crossing(t, y, threshold=150.0,
                                                level_name="HIGH")
    assert pred.seconds_to_cross is None
    assert "above" in pred.reason.lower() or "at" in pred.reason.lower()


def test_r_squared_of_a_perfect_line_is_one():
    t = np.arange(40) * 2.0
    y = 10.0 * t + 5.0  # perfectly linear
    tr = analytics.rolling_linear_regression(t, y)
    assert tr.ok
    assert tr.r_squared > 0.99
    # y = 10*t + 5 with t in seconds, so the slope is ~10 units/sec.
    assert abs(tr.slope_per_second - 10.0) < 0.2


def test_exposure_duration_counts_each_level():
    rows = [
        {"exposure": "LOW", "filtered_adc": 70},
        {"exposure": "LOW", "filtered_adc": 72},
        {"exposure": "MODERATE", "filtered_adc": 110},
        {"exposure": "HIGH", "filtered_adc": 200},
    ]
    out = analytics.compute_exposure_duration(rows, {"moderate": 100,
                                                     "high": 150},
                                              cadence_seconds=2.0)
    assert out["LOW"] == 4.0
    assert out["MODERATE"] == 2.0
    assert out["HIGH"] == 2.0
    assert out["sample_count"] == 4


def test_recompute_level_when_device_level_missing():
    rows = [
        {"exposure": None, "filtered_adc": 200},  # -> HIGH
        {"exposure": None, "filtered_adc": 120},  # -> MODERATE
        {"exposure": None, "filtered_adc": 80},   # -> LOW
    ]
    out = analytics.compute_exposure_duration(rows, {"moderate": 100,
                                                     "high": 150},
                                              cadence_seconds=1.0)
    assert out["HIGH"] == 1.0
    assert out["MODERATE"] == 1.0
    assert out["LOW"] == 1.0


def test_compare_warnings_uses_all_three_signals():
    t, y = _rising()
    res = analytics.compare_warnings(
        current=y[-1], t_values=t, y_values=y,
        device_standard={"high": 150.0, "moderate": 100.0},
        profile={"high_threshold": 120.0, "warning_lead_seconds": 60.0})
    assert "signals" in res
    assert set(res["signals"].keys()) == {"fixed", "predictive",
                                          "personalized"}
