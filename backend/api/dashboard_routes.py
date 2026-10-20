"""Dashboard and software test-scenario endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Query

from backend import config, database
from backend.schemas import TestScenarioIn
from backend.services import alert_service, prediction_service, sensor_service, test_service
from ml.predict import get_predictor

router = APIRouter(prefix="/api", tags=["dashboard"])


def _model_metrics():
    if config.EVAL_REPORT_PATH.exists():
        try:
            report = json.loads(config.EVAL_REPORT_PATH.read_text(encoding="utf-8"))
            return {
                "available": True,
                "model_version": report.get("model_version"),
                "data_source": report.get("data_source"),
                "warning": report.get("warning"),
                "metrics": report.get("metrics"),
                "trained_at": report.get("trained_at"),
                "horizons_min": report.get("horizons_min"),
            }
        except (ValueError, OSError):
            return {"available": False, "error": "could not read evaluation report"}
    return {"available": False, "error": "no evaluation report found"}


@router.get("/dashboard/summary")
async def dashboard_summary(source: str = Query(default="esp32")):
    """Everything the dashboard needs for one polling cycle."""
    predictor = get_predictor()

    latest = database.get_latest_reading(source=source)
    status = sensor_service.latest_status(source=source)
    recent = database.get_recent_readings(limit=120, source=source)
    recent.reverse()

    pred = prediction_service.run_prediction(source=source, is_test=(source == "test"))
    latest_batch = database.get_latest_prediction_batch()
    latest_batch = [p for p in latest_batch if p.get("source") == source]

    current_risk = "unknown"
    rate = None
    if latest and latest.get("valid") and latest.get("water_level_cm") is not None:
        context = prediction_service.get_context_readings(source)
        rate = alert_service.rate_of_change_cm_per_min(context)
        current_risk = alert_service.classify_current_risk(latest["water_level_cm"], rate)

    return {
        "source": source,
        "server_time": database.iso(),
        "device_status": status,
        "latest_reading": latest,
        "current_risk": current_risk,
        "rate_cm_per_min": round(rate, 3) if rate is not None else None,
        "overall_risk": alert_service.higher(current_risk, pred.get("forecast_risk", "unknown")),
        "prediction": {
            "available": pred.get("available", False),
            "model_available": predictor.is_available,
            "model_load_error": predictor.load_error,
            "model_version": pred.get("model_version"),
            "data_quality": pred.get("data_quality"),
            "error": pred.get("error"),
            "forecast_risk": pred.get("forecast_risk", "unknown"),
            "horizons": latest_batch,
        },
        "thresholds": config.thresholds(),
        "risk_levels": config.RISK_LEVELS,
        "model_metrics": _model_metrics(),
        "recent_readings": recent,
        "alerts": database.get_alert_history(limit=25),
        "notification": {
            "provider": config.SMS_PROVIDER or "disabled",
            "configured": alert_service.sms_configured(),
        },
        "email": _email_summary(),
        "esp32": _esp_summary(),
        "db_stats": database.db_stats(),
    }


def _esp_summary():
    from backend.services import esp_client
    st = esp_client.status()
    ok_at = st.get("last_ok") or st.get("last_ok_at")
    return {
        "configured": bool(st.get("configured")),
        "host": st.get("host") or "",
        "port": st.get("port"),
        "endpoint": st.get("endpoint"),
        "connected": st.get("connected"),
        "last_ok": ok_at,
        "last_error": st.get("last_error"),
        "readings_ingested": st.get("readings_ingested", 0),
    }


def _email_summary():
    from backend.services import email_service

    es = email_service.email_service
    ok, reason = es.validate_config()
    latest = database.get_email_notifications(limit=1)
    latest_row = latest[0] if latest else None
    sender = es.sender
    if sender and len(sender) > 4:
        sender = sender[:2] + "*" * (len(sender) - 4) + sender[-2:]
    return {
        "configured": ok,
        "status": "CONFIGURED" if ok else "NOT CONFIGURED",
        "reason": None if ok else reason,
        "host": es.host,
        "port": es.port,
        "sender": sender or "—",
        "recipients": [_mask_addr(a) for a in es.get_recipients()],
        "recipient_count": len(es.get_recipients()),
        "latest_attempt": latest_row.get("created_at") if latest_row else None,
        "latest_delivery_status": latest_row.get("status") if latest_row else None,
        "latest_subject": latest_row.get("subject") if latest_row else None,
        "latest_failure_reason": latest_row.get("error") if latest_row else None,
    }


def _mask_addr(addr: str) -> str:
    if not addr or "@" not in addr:
        return "—"
    name, _, domain = addr.partition("@")
    if len(name) <= 2:
        masked = name[:1] + "*"
    else:
        masked = name[:2] + "*" * (len(name) - 2)
    return f"{masked}@{domain}"


@router.post("/test/scenario")
async def run_test_scenario(body: TestScenarioIn):
    try:
        return test_service.run_scenario(body.scenario, body.device_id)
    except ValueError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/test/reset")
async def reset_test_data():
    return {"cleared": database.clear_test_data(),
            "note": "Only rows with source='test' were removed; live data is intact."}
