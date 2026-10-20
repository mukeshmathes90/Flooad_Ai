"""Prediction endpoints (raw + validated model outputs, model metadata)."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Query

from backend import config, database
from backend.services import prediction_service
from ml.predict import get_predictor

router = APIRouter(prefix="/api/predictions", tags=["predictions"])


@router.get("/latest")
async def latest_predictions(source: Optional[str] = Query(default=None)):
    batch = database.get_latest_prediction_batch()
    if source is not None:
        batch = [p for p in batch if p.get("source") == source]
    accepted = [p for p in batch if p.get("accepted")]
    forecast_risk = "unknown"
    if accepted:
        from backend.services.alert_service import classify_forecast_risk

        forecast_risk = classify_forecast_risk([p["predicted_level_cm"] for p in accepted])
    return {
        "available": bool(batch),
        "forecast_risk": forecast_risk,
        "horizons": [
            {
                "horizon_min": p["horizon_min"],
                "raw_predicted_level_cm": p["raw_predicted_level_cm"],
                "predicted_level_cm": p["predicted_level_cm"],
                "accepted": bool(p["accepted"]),
                "validation_status": p["validation_status"],
                "reason": p["reason"],
                "risk_level": p["risk_level"],
                "model_version": p["model_version"],
                "created_at": p["created_at"],
            }
            for p in batch
        ],
    }


@router.get("/history")
async def prediction_history(limit: int = Query(default=200, ge=1, le=5000)):
    rows = database.get_prediction_history(limit=limit)
    return {"count": len(rows), "predictions": rows}


@router.post("/run")
async def run_prediction(source: str = Query(default="esp32")):
    return prediction_service.run_prediction(source=source, is_test=(source == "test"))


@router.get("/model")
async def model_info():
    predictor = get_predictor()
    report = None
    if config.EVAL_REPORT_PATH.exists():
        try:
            report = json.loads(config.EVAL_REPORT_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            report = {"error": "could not read evaluation report"}
    return {
        "available": predictor.is_available,
        "load_error": predictor.load_error,
        "metadata": predictor.metadata,
        "evaluation": report,
        "thresholds": config.thresholds(),
    }
