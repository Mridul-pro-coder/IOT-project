"""
Modular interface for a FUTURE population / per-user ML personalization layer.

This module is a stable seam, not a model. It defines the contract any
model must satisfy, keeps today's system fully functional on rule-based
thresholds, and — importantly — refuses to let callers describe a
manually-configured threshold set as "transfer learning" or any learned
model until one is actually trained and evaluated.

Design intent:
  - `RuleBasedPredictor` is the default. It never claims to be learned.
  - `Personalizer` protocol is the interface a future model implements.
  - `load_personalizer()` returns RuleBasedPredictor unless a trained model
    artifact + evaluation evidence exist on disk, in which case it may
    return the real one (still opt-in and clearly labelled experimental).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


@dataclass
class ModelContext:
    """Everything a personalizer needs to refine a risk estimate."""
    user_id: int
    value: float
    data_source: str                     # 'real' | 'simulated'
    recent_readings: list[float]         # chronological, filtered values
    recent_symptoms: list[dict]          # user's symptom logs, most recent last
    thresholds: dict                     # low/moderate/high/hysteresis


@dataclass
class ModelOutput:
    """A personalizer's response. `adjusted_level` may override the
    rule-based level; if None, the caller keeps the rule-based result."""
    adjusted_level: Optional[str] = None
    confidence: Optional[float] = None   # 0..1 when the model asserts
    reasons: list[str] = None            # human-readable explanation

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []


@runtime_checkable
class Personalizer(Protocol):
    """Interface a future trained model must implement.

    Implementations MUST be trained on labelled data and MUST expose
    `is_trained()` returning True only after actual training + evaluation.
    """
    name: str

    def is_trained(self) -> bool: ...
    def is_transfer_learning(self) -> bool: ...
    def predict(self, ctx: ModelContext) -> ModelOutput: ...


class RuleBasedPredictor:
    """The default, honest personalizer: pure thresholds + hysteresis.

    It exists so the pipeline has a concrete Personalizer today, and so any
    future model can be swapped in behind the same interface without
    touching the rest of the system.
    """
    name = "rule-based"

    def is_trained(self) -> bool:
        return False  # nothing was learned

    def is_transfer_learning(self) -> bool:
        return False  # explicitly NOT transfer learning

    def predict(self, ctx: ModelContext) -> ModelOutput:
        # No adjustment: the rule engine's band is the result.
        return ModelOutput(adjusted_level=None, confidence=None,
                           reasons=["threshold rules only; no model applied"])


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
_ARTIFACT = "user_calibration_model.json"
_EVIDENCE = "evaluation_evidence.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_personalizer(user_id: Optional[int] = None) -> Personalizer:
    """Return a Personalizer for a user.

    If a trained artifact AND evaluation evidence exist for that user, the
    real model could be loaded here; for now this always returns the
    rule-based predictor. The guards below are deliberate and testable:
    they are what stops anyone from labelling rule thresholds as learned.
    """
    calib_dir = _repo_root() / "data" / "calibration"
    art = calib_dir / _ARTIFACT
    ev = calib_dir / _EVIDENCE

    if art.exists() and ev.exists():
        try:
            evidence = json.loads(ev.read_text())
            # A model is only "trained" if the evidence file says so AND has
            # a positive holdout sample count. Anything less -> refuse.
            if (evidence.get("trained") is True
                    and int(evidence.get("n_holdout", 0)) > 0):
                # Future: load and wrap a real model here. Until then, even
                # with artifacts present we stay rule-based and flag it.
                print(f"[ml] artifacts present for user {user_id}; "
                      f"real model loading not yet wired — staying rule-based")
        except (json.JSONDecodeError, ValueError, OSError):
            print("[ml] calibration artifacts malformed; staying rule-based")

    return RuleBasedPredictor()


def describe_personalizer(p: Personalizer) -> dict:
    """Metadata for the UI: makes the 'no transfer learning' guarantee
    visible rather than buried in code."""
    return {
        "name": p.name,
        "trained": p.is_trained(),
        "transfer_learning": p.is_transfer_learning(),
        "note": ("manually configured thresholds — NOT a learned model"
                 if not p.is_trained() else "trained model (experimental)"),
    }
