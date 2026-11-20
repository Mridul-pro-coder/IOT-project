"""Tests for the future-ML seam: it must NOT claim transfer learning or a
learned model when only rule-based thresholds exist (requirement 11)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import ml_adapter  # noqa: E402


def test_rule_predictor_is_not_trained_or_transfer():
    p = ml_adapter.RuleBasedPredictor()
    assert p.is_trained() is False
    assert p.is_transfer_learning() is False


def test_loaded_personalizer_is_rule_based_by_default():
    p = ml_adapter.load_personalizer(user_id=1)
    meta = ml_adapter.describe_personalizer(p)
    assert meta["name"] == "rule-based"
    assert meta["trained"] is False
    assert meta["transfer_learning"] is False
    assert "NOT a learned model" in meta["note"]


def test_model_output_does_not_override_when_absent():
    p = ml_adapter.RuleBasedPredictor()
    out = p.predict(ml_adapter.ModelContext(
        user_id=1, value=110.0, data_source="real",
        recent_readings=[100, 105, 110], recent_symptoms=[],
        thresholds={"high": 150}))
    assert out.adjusted_level is None  # keeps the rule-based band
    assert out.reasons


def test_protocol_is_satisfied():
    assert isinstance(ml_adapter.RuleBasedPredictor(), ml_adapter.Personalizer)
