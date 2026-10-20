"""
Shared feature engineering used by BOTH training and inference.

Keeping this in one place guarantees the feature names, order, units and
pre-processing are identical during training and prediction (a common source of
silent model degradation).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

# Fixed look-back window (number of samples, oldest -> newest).
WINDOW = 10

# Feature names in the exact order produced by ``build_feature_vector``.
FEATURE_NAMES = [
    "water_level_cm",
    "wl_lag1",
    "wl_lag2",
    "wl_lag3",
    "wl_lag5",
    "wl_diff1",
    "wl_rate",
    "wl_mean5",
    "wl_mean10",
    "wl_std10",
    "rainfall_reading",
    "rainfall_mean5",
    "rainfall_intensity",
    "water_sensor_raw",
    "hour_sin",
    "hour_cos",
]


class InsufficientData(ValueError):
    """Raised when there are not enough valid readings to build features."""


def _f(value: Optional[float], default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except (TypeError, ValueError):
        return default


def _hour_of(reading: dict) -> float:
    """Best-effort hour-of-day (0..24) from a reading, else 0."""
    ts = reading.get("timestamp") or reading.get("device_timestamp")
    if ts:
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).hour + \
                datetime.fromisoformat(str(ts).replace("Z", "+00:00")).minute / 60.0
        except (ValueError, TypeError):
            pass
    ep = reading.get("created_epoch")
    if ep is not None:
        try:
            dt = datetime.fromtimestamp(float(ep))
            return dt.hour + dt.minute / 60.0
        except (ValueError, TypeError, OSError):
            pass
    return 0.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _slope(values: list[float]) -> float:
    """Least-squares slope of values vs their sample index (units/sample)."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = _mean(xs)
    my = _mean(values)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((xs[i] - mx) * (values[i] - my) for i in range(n)) / denom


def build_feature_vector(readings: list[dict]) -> list[float]:
    """
    Build the ordered feature vector from a chronological list of readings
    (oldest first).  Only the last ``WINDOW`` readings are used and no future
    information is consumed.
    """
    if not readings:
        raise InsufficientData("no readings supplied")
    if len(readings) < 3:
        raise InsufficientData(f"need at least 3 readings, got {len(readings)}")

    window = readings[-WINDOW:]
    levels = [_f(r.get("water_level_cm")) for r in window]
    rains = [_f(r.get("rainfall_reading")) for r in window]

    def lag(k: int) -> float:
        return levels[-1 - k] if len(levels) > k else levels[0]

    diffs = [levels[i] - levels[i - 1] for i in range(1, len(levels))]
    last_diff = diffs[-1] if diffs else 0.0
    last = window[-1]

    features = [
        levels[-1],
        lag(1),
        lag(2),
        lag(3),
        lag(5),
        last_diff,
        _slope(levels),
        _mean(levels[-5:]),
        _mean(levels),
        _std(levels),
        rains[-1],
        _mean(rains[-5:]),
        _f(last.get("rainfall_intensity")),
        _f(last.get("water_sensor_raw")),
        math.sin(2 * math.pi * _hour_of(last) / 24.0),
        math.cos(2 * math.pi * _hour_of(last) / 24.0),
    ]
    return features


def feature_dict(readings: list[dict]) -> dict:
    return dict(zip(FEATURE_NAMES, build_feature_vector(readings)))
