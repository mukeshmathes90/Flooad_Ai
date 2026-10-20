"""Alert history and risk-threshold endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from backend import config, database
from backend.services import alert_service

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("/history")
async def alert_history(limit: int = Query(default=100, ge=1, le=5000),
                        origin_like: str | None = Query(default=None)):
    rows = database.get_alert_history(limit=limit)
    if origin_like:
        rows = [r for r in rows if r.get("origin") and origin_like in r["origin"]]
    return {"count": len(rows), "alerts": rows}


@router.get("/thresholds")
async def thresholds():
    return {
        "thresholds": config.thresholds(),
        "risk_levels": config.RISK_LEVELS,
        "rapid_rise_warning_cm_per_min": config.RAPID_RISE_WARNING_CM_PER_MIN,
        "rapid_rise_high_cm_per_min": config.RAPID_RISE_HIGH_CM_PER_MIN,
        "note": (
            "Prototype thresholds. They MUST be calibrated to the real installation "
            "and are not universal real-world flood limits."
        ),
    }


@router.get("/notification-status")
async def notification_status():
    from backend.services import email_service
    es = email_service.email_service
    ok, reason = es.validate_config()
    latest_emails = database.get_email_notifications(limit=5)
    return {
        "provider": config.SMS_PROVIDER or "disabled",
        "configured": alert_service.sms_configured(),
        "email": {
            "configured": ok,
            "status": "CONFIGURED" if ok else f"NOT CONFIGURED ({reason})",
            "host": es.host,
            "port": es.port,
            "sender": es.sender,
            "latest_attempt": latest_emails[0].get("created_at") if latest_emails else None,
            "latest_status": latest_emails[0].get("status") if latest_emails else None,
        },
        "note": (
            "Remote notifications are not configured. Alerts are logged locally only."
            if not alert_service.sms_configured()
            else "Remote notification provider is configured."
        ),
    }



