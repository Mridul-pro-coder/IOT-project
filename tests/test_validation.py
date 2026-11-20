"""Tests for shared device-config validation (service + dashboard guard)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.mqtt_service import validate_config  # noqa: E402


def test_valid_config_passes():
    cfg = {"moderate_threshold": 100.0, "high_threshold": 150.0,
           "hysteresis": 5.0, "ewma_alpha": 0.3, "buzzer_enabled": True,
           "publish_interval_ms": 2000}
    assert validate_config(cfg) == []


def test_high_must_exceed_moderate():
    errs = validate_config({"moderate_threshold": 150.0,
                            "high_threshold": 150.0})
    assert any("exceed" in e for e in errs)


def test_threshold_out_of_range():
    errs = validate_config({"moderate_threshold": 100.0,
                            "high_threshold": 5000.0})
    assert any("out of range" in e for e in errs)


def test_hysteresis_too_large():
    # gap is 20; hysteresis must be < 10
    errs = validate_config({"moderate_threshold": 100.0,
                            "high_threshold": 120.0, "hysteresis": 20.0})
    assert any("hysteresis" in e for e in errs)


def test_alpha_bounds():
    assert validate_config({"moderate_threshold": 100.0,
                           "high_threshold": 150.0, "ewma_alpha": 0.0})
    assert validate_config({"moderate_threshold": 100.0,
                           "high_threshold": 150.0, "ewma_alpha": 1.5})
    assert validate_config({"moderate_threshold": 100.0,
                           "high_threshold": 150.0, "ewma_alpha": 0.3}) == []


def test_publish_interval_bounds():
    base = {"moderate_threshold": 100.0, "high_threshold": 150.0}
    assert validate_config({**base, "publish_interval_ms": 100})
    assert validate_config({**base, "publish_interval_ms": 999999})
    assert validate_config({**base, "publish_interval_ms": 2000}) == []


def test_missing_required_fields():
    errs = validate_config({"moderate_threshold": 100.0})  # no high
    assert any("high" in e for e in errs)
