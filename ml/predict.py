"""
Inference wrapper around the trained forecasting model.

Returns *raw* model outputs only.  All sanity/validation and risk classification
happen in the backend services (backend/services/validation_service.py and
alert_service.py) so that raw and accepted values stay clearly separated.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from backend import config
from ml import features


class ModelNotAvailable(RuntimeError):
    pass


class ModelLoadError(RuntimeError):
    pass


class FloodPredictor:
    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._meta: Optional[dict] = None
        self._load_error: Optional[str] = None
        self.load()

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        with self._lock:
            self._model = None
            self._meta = None
            self._load_error = None
            if not config.MODEL_PATH.exists():
                self._load_error = f"model file missing: {config.MODEL_PATH}"
                return
            try:
                import joblib

                self._model = joblib.load(config.MODEL_PATH)
                if config.FEATURE_META_PATH.exists():
                    self._meta = json.loads(config.FEATURE_META_PATH.read_text(encoding="utf-8"))
                else:
                    self._meta = {
                        "feature_names": features.FEATURE_NAMES,
                        "horizons_min": config.FORECAST_HORIZONS_MIN,
                        "model_version": config.MODEL_VERSION,
                    }
            except Exception as exc:  # noqa: BLE001
                self._load_error = f"failed to load model: {exc}"
                self._model = None

    @property
    def is_available(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    @property
    def metadata(self) -> dict:
        return self._meta or {}

    @property
    def horizons(self) -> list[float]:
        return list(self.metadata.get("horizons_min", config.FORECAST_HORIZONS_MIN))

    @property
    def feature_names(self) -> list[str]:
        return list(self.metadata.get("feature_names", features.FEATURE_NAMES))

    # ------------------------------------------------------------------ #
    def predict_raw(self, readings: list[dict]) -> list[dict]:
        """
        Predict future water levels for each horizon.

        ``readings`` must be chronological (oldest first) and contain at least
        ``features.WINDOW`` rows for a fully-specified feature vector.
        """
        if self._model is None:
            raise ModelNotAvailable(self._load_error or "model not loaded")

        expected = self.feature_names
        vec = features.build_feature_vector(readings)
        if expected != features.FEATURE_NAMES:
            # Feature order changed since training - fail loudly rather than
            # silently feeding mis-ordered features to the model.
            raise ModelLoadError(
                "feature mismatch: trained=" + ",".join(expected) +
                " runtime=" + ",".join(features.FEATURE_NAMES)
            )
        X = np.asarray([vec], dtype=float)
        preds = np.asarray(self._model.predict(X), dtype=float).reshape(-1)

        out: list[dict] = []
        for i, h in enumerate(self.horizons):
            raw = float(preds[i]) if i < len(preds) else float("nan")
            out.append(
                {
                    "horizon_min": float(h),
                    "raw_predicted_level_cm": raw,
                    "model_version": self.metadata.get("model_version", config.MODEL_VERSION),
                }
            )
        return out


# Module-level singleton used by the prediction service.
_PREDICTOR: Optional[FloodPredictor] = None
_PREDICTOR_LOCK = threading.Lock()


def get_predictor(force_reload: bool = False) -> FloodPredictor:
    global _PREDICTOR
    with _PREDICTOR_LOCK:
        if _PREDICTOR is None:
            _PREDICTOR = FloodPredictor()
        elif force_reload:
            _PREDICTOR.load()
    return _PREDICTOR
