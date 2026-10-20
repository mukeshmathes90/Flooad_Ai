"""
Prediction service: runs the trained model, validates each output, stores the
results, and computes the forecast risk.

The raw model output and the validated/accepted value are always kept separate.
When the model is unavailable, NO prediction is fabricated - the service reports
an explicit error (an optional, clearly-labelled persistence fallback can be
enabled via FLOOD_ALLOW_FALLBACK, and it never drives risk escalation).
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Optional

from backend import config, database
from backend.services import alert_service, validation_service
from ml import features
from ml.predict import ModelNotAvailable, get_predictor


ALLOW_FALLBACK = os.getenv("FLOOD_ALLOW_FALLBACK", "false").strip().lower() in {"1", "true", "yes"}


def _now() -> tuple[str, float]:
    dt = datetime.now(timezone.utc)
    return dt.isoformat(), dt.timestamp()


def get_context_readings(source: str = "esp32", limit: int = 60) -> list[dict]:
    """Chronological (oldest-first) list of valid readings for a data source."""
    recent = database.get_recent_readings(limit=limit, source=source, only_valid=True)
    recent = [r for r in recent if r.get("water_level_cm") is not None]
    recent.reverse()
    return recent


def run_prediction(source: str = "esp32", is_test: bool = False) -> dict:
    """
    Full prediction cycle for a data source.

    Returns a structured result used by the API and the dashboard.
    """
    context = get_context_readings(source=source)
    result: dict = {
        "source": source,
        "is_test": is_test,
        "available": False,
        "model_version": config.MODEL_VERSION,
        "error": None,
        "data_quality": {"status": "ok", "n_readings": len(context)},
        "predictions": [],
        "forecast_risk": "unknown",
        "alerts": [],
    }

    if not context:
        result["data_quality"] = {"status": "no_data", "n_readings": 0}
        result["error"] = "no valid readings available for this source"
        return result

    latest = context[-1]
    created = latest.get("created_epoch")
    age = None
    if created is not None:
        age = datetime.now(timezone.utc).timestamp() - float(created)
    if age is not None and age > config.STALE_AFTER_SECONDS:
        result["data_quality"] = {
            "status": "stale",
            "n_readings": len(context),
            "age_seconds": round(age, 1),
        }
    elif len(context) < features.WINDOW:
        result["data_quality"] = {
            "status": "limited_history",
            "n_readings": len(context),
        }

    predictor = get_predictor()
    latest_level = latest.get("water_level_cm")
    recent_levels = [float(r["water_level_cm"]) for r in context[-5:] if r.get("water_level_cm") is not None]

    raw_predictions: list[dict] = []
    if predictor.is_available:
        try:
            raw_predictions = predictor.predict_raw(context)
            result["available"] = True
            result["model_version"] = predictor.metadata.get("model_version", config.MODEL_VERSION)
        except (ModelNotAvailable, features.InsufficientData) as exc:
            result["error"] = f"prediction failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"prediction failed: {exc}"
    else:
        result["error"] = predictor.load_error or "model not available"

    # Explicitly-labelled fallback (opt-in only). Never used for risk escalation.
    if not result["available"] and ALLOW_FALLBACK and latest_level is not None:
        raw_predictions = [
            {"horizon_min": float(h), "raw_predicted_level_cm": float(latest_level),
             "model_version": "fallback-persistence"}
            for h in config.FORECAST_HORIZONS_MIN
        ]
        result["error"] = (result["error"] or "") + " | using labelled persistence fallback"
        result["fallback"] = True

    now_iso, now_epoch = _now()
    accepted_values: list[float] = []
    batch: list[dict] = []

    for raw in raw_predictions:
        h = raw["horizon_min"]
        raw_val = raw["raw_predicted_level_cm"]
        v = validation_service.validate_prediction(
            raw_value=raw_val,
            latest_level_cm=latest_level,
            horizon_min=h,
            recent_levels=recent_levels,
        )
        risk = (config.risk_from_level_cm(v["value"])
                if v["accepted"] and not v.get("fallback") else "unknown")
        if v["accepted"] and v["value"] is not None and not result.get("fallback"):
            accepted_values.append(v["value"])

        record = {
            "model_version": raw.get("model_version", config.MODEL_VERSION),
            "created_at": now_iso,
            "created_epoch": now_epoch,
            "input_reading_id": latest.get("id"),
            "horizon_min": h,
            "raw_predicted_level_cm": raw_val if raw_val is None or not math.isnan(raw_val) else None,
            "predicted_level_cm": v["value"],
            "accepted": v["accepted"],
            "validation_status": v["status"],
            "reason": v["reason"],
            "risk_level": risk,
            "source": source,
        }
        pred_id = database.insert_prediction(record)
        record["id"] = pred_id
        record["achieved_epoch"] = now_epoch + h * 60.0
        batch.append(record)

    result["predictions"] = batch
    if accepted_values:
        result["forecast_risk"] = alert_service.classify_forecast_risk(accepted_values)
    elif result.get("fallback"):
        result["forecast_risk"] = "unknown"  # fallback never drives risk

    # Forecast alert. Test scenarios are still logged (to exercise the alert
    # pipeline and dashboard) but are tagged with a test origin and NEVER send a
    # real notification (notified=False, status='skipped_test').
    if result["forecast_risk"] in ("warning", "high", "critical"):
        target = max(accepted_values) if accepted_values else latest_level
        origin = "test_ml_forecast" if is_test else "ml_forecast"
        msg = (
            f"[{result['forecast_risk'].upper()}] ML forecast: water level may reach "
            f"{target:.1f} cm within {max(p['horizon_min'] for p in batch):.0f} min "
            f"(model {result['model_version']})"
            + (" [TEST SCENARIO]" if is_test else "")
            + "."
        )
        alert = alert_service.record_alert(
            result["forecast_risk"], origin, msg,
            device_id=latest.get("device_id", "unknown"),
            dedupe_key=f"{origin}:{result['forecast_risk']}:{int(target // 5)}",
            is_test=is_test,
        )
        result["alerts"].append(alert)

    return result
