"""
Validation / safety layer.

Two responsibilities:

1. ``validate_sensor_payload`` - check incoming ESP32 readings against physical
   limits and derive missing fields (e.g. water level from ultrasonic distance).

2. ``validate_prediction`` - independently sanity-check every raw model output
   before it is allowed near the dashboard or the risk engine.  Invalid outputs
   are marked rejected / unavailable with a reason and are never silently
   replaced by a fabricated "valid" value.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from backend import config


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x) and not math.isinf(x)


# --------------------------------------------------------------------------- #
# Sensor payload validation
# --------------------------------------------------------------------------- #
def validate_sensor_payload(payload: dict) -> tuple[dict, list[str]]:
    """
    Normalise and validate a sensor dict.

    Returns ``(normalised, errors)``.  ``normalised`` always has the canonical
    keys so it can be persisted (with valid=False when errors are non-empty).
    """
    errors: list[str] = []
    out: dict = {
        "device_id": payload.get("device_id") or "unknown",
        "source": payload.get("source") or "esp32",
        "water_level_cm": payload.get("water_level_cm"),
        "ultrasonic_distance_cm": payload.get("ultrasonic_distance_cm"),
        "rainfall_reading": payload.get("rainfall_reading"),
        "rainfall_intensity": payload.get("rainfall_intensity"),
        "water_sensor_raw": payload.get("water_sensor_raw"),
        "pir_motion": payload.get("pir_motion"),
        "device_timestamp": payload.get("device_timestamp") or payload.get("timestamp"),
        # optional server-side overrides (used by the software test harness to
        # spread a scenario over a realistic time window)
        "created_epoch": payload.get("created_epoch"),
        "server_timestamp": payload.get("server_timestamp"),
    }

    wl = out["water_level_cm"]
    dist = out["ultrasonic_distance_cm"]

    # numeric coercion
    for key in ("water_level_cm", "ultrasonic_distance_cm", "rainfall_reading",
                "rainfall_intensity", "water_sensor_raw"):
        val = out[key]
        if val is None:
            continue
        try:
            out[key] = float(val)
        except (TypeError, ValueError):
            errors.append(f"{key} is not numeric: {val!r}")
            out[key] = None

    wl = out["water_level_cm"]
    dist = out["ultrasonic_distance_cm"]

    if wl is None and dist is None:
        errors.append("no water_level_cm or ultrasonic_distance_cm supplied")

    # Derive water level from distance (and vice-versa) using the calibrated
    # reference height.
    if dist is not None and _finite(dist):
        if dist < config.ULTRASONIC_MIN_CM or dist > config.ULTRASONIC_MAX_CM:
            errors.append(
                f"ultrasonic_distance_cm={dist} outside usable range "
                f"[{config.ULTRASONIC_MIN_CM}, {config.ULTRASONIC_MAX_CM}]"
            )
        elif wl is None:
            out["water_level_cm"] = round(config.REFERENCE_HEIGHT_CM - dist, 3)
    if wl is not None and _finite(wl) and dist is None:
        out["ultrasonic_distance_cm"] = round(config.REFERENCE_HEIGHT_CM - wl, 3)

    wl = out["water_level_cm"]
    if wl is not None:
        if not _finite(wl):
            errors.append(f"water_level_cm is not finite: {wl}")
        elif wl < config.MIN_WATER_LEVEL_CM or wl > config.MAX_WATER_LEVEL_CM:
            errors.append(
                f"water_level_cm={wl} outside physical range "
                f"[{config.MIN_WATER_LEVEL_CM}, {config.MAX_WATER_LEVEL_CM}]"
            )

    rain = out["rainfall_reading"]
    if rain is not None and _finite(rain):
        if rain < 0 or rain > config.ADC_MAX:
            errors.append(f"rainfall_reading={rain} outside ADC range [0,{config.ADC_MAX}]")
    elif rain is not None:
        errors.append("rainfall_reading is not finite")

    ri = out["rainfall_intensity"]
    if ri is not None and _finite(ri):
        if ri < 0 or ri > 100.0001:
            errors.append(f"rainfall_intensity={ri} outside [0,100]")
    elif ri is not None:
        errors.append("rainfall_intensity is not finite")

    ws = out["water_sensor_raw"]
    if ws is not None and _finite(ws):
        if ws < 0 or ws > config.ADC_MAX:
            errors.append(f"water_sensor_raw={ws} outside ADC range [0,{config.ADC_MAX}]")

    # device clock skew guard (informational; server time is authoritative)
    ts = out["device_timestamp"]
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            skew = abs(dt.timestamp() - datetime.now(timezone.utc).timestamp())
            if skew > config.MAX_CLOCK_SKEW_SECONDS:
                out["clock_skew_seconds"] = round(skew, 1)
        except (ValueError, TypeError):
            errors.append(f"device_timestamp not ISO-8601: {ts!r}")

    return out, errors


# --------------------------------------------------------------------------- #
# Prediction validation
# --------------------------------------------------------------------------- #
def validate_prediction(
    raw_value: Optional[float],
    latest_level_cm: Optional[float],
    horizon_min: float,
    recent_levels: Optional[list[float]] = None,
) -> dict:
    """
    Validate a single raw model output.

    Returns a dict:
        {
          "raw": float|None,
          "value": float|None,        # accepted / constrained value
          "accepted": bool,
          "status": "accepted"|"constrained"|"rejected"|"unavailable",
          "reason": str,
        }

    The raw value is always preserved separately from the value that is actually
    used for risk classification.
    """
    result = {
        "raw": raw_value,
        "value": None,
        "accepted": False,
        "status": "rejected",
        "reason": "",
    }

    if raw_value is None:
        result["status"] = "unavailable"
        result["reason"] = "model produced no value"
        return result

    if not _finite(raw_value):
        result["reason"] = f"non-finite model output ({raw_value})"
        return result

    lo = config.MIN_WATER_LEVEL_CM
    hi = config.MAX_WATER_LEVEL_CM
    # Deviation from the latest measured level is the strongest independent
    # consistency check available without a hydraulic model.
    if latest_level_cm is not None and _finite(latest_level_cm):
        deviation = raw_value - latest_level_cm
        if abs(deviation) > config.MAX_PREDICTION_DEVIATION_CM:
            result["reason"] = (
                f"implausible jump: {deviation:+.1f} cm from latest reading "
                f"(limit +/-{config.MAX_PREDICTION_DEVIATION_CM} cm)"
            )
            return result

        implied_rate = deviation / max(horizon_min, 1e-6)
        if abs(implied_rate) > config.MAX_REASONABLE_RATE_CM_PER_MIN * 1.5:
            result["reason"] = (
                f"implied rate of change {implied_rate:.1f} cm/min exceeds "
                f"physical limit {config.MAX_REASONABLE_RATE_CM_PER_MIN} cm/min"
            )
            return result

    # Extremely out of physical range -> reject outright.
    if raw_value < lo - 5.0 or raw_value > hi + 5.0:
        result["reason"] = (
            f"raw output {raw_value:.1f} cm far outside physical range "
            f"[{lo}, {hi}] cm"
        )
        return result

    # Mildly out of range -> constrain (never silently, always documented).
    if raw_value < lo or raw_value > hi:
        constrained = min(max(raw_value, lo), hi)
        result.update(
            value=constrained,
            accepted=True,
            status="constrained",
            reason=(
                f"raw {raw_value:.2f} cm bounded to physical limit "
                f"[{lo}, {hi}] cm -> {constrained:.2f} cm"
            ),
        )
        return result

    result.update(
        value=float(raw_value),
        accepted=True,
        status="accepted",
        reason="passed physical and consistency checks",
    )
    return result
