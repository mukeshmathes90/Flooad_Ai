"""Email notification status, history and demo-send endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from backend import config, database
from backend.services import email_service, test_service

router = APIRouter(prefix="/api/email", tags=["email"])


def _mask(s: str | None) -> str:
    if not s:
        return "—"
    if len(s) <= 4:
        return "***"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


@router.get("/status")
async def email_status():
    es = email_service.email_service
    ok, reason = es.validate_config()
    latest = database.get_email_notifications(limit=1)
    latest_row = latest[0] if latest else None
    return {
        "configured": ok,
        "status": "CONFIGURED" if ok else f"NOT CONFIGURED",
        "reason": None if ok else reason,
        "host": es.host,
        "port": es.port,
        "sender": _mask(es.sender),
        "latest_attempt": latest_row.get("created_at") if latest_row else None,
        "latest_delivery_status": latest_row.get("status") if latest_row else None,
        "latest_subject": latest_row.get("subject") if latest_row else None,
        "latest_failure_reason": latest_row.get("error") if latest_row else None,
        "sent_count": sum(1 for r in database.get_email_notifications(limit=500) if r.get("status") == "sent"),
    }


@router.get("/history")
async def email_history(limit: int = Query(default=50, ge=1, le=200)):
    rows = database.get_email_notifications(limit=limit)
    safe = []
    for r in rows:
        safe.append({
            "id": r.get("id"),
            "created_at": r.get("created_at"),
            "subject": r.get("subject"),
            "status": r.get("status"),
            "is_test": bool(r.get("is_test")),
            "error": r.get("error"),
            "sent_at": r.get("sent_at"),
            "alert_id": r.get("alert_id"),
        })
    return {"count": len(safe), "emails": safe}


@router.post("/send-test")
async def send_test_email(scenario: str = Query(default="warning")):
    """Send a clearly-marked ``[TEST]`` demo email (explicit operator action).

    This is the ONLY path that emails on test data.  Real alert transitions
    during live operation still email normally; test-scenario readings only
    ever produce this demo, which is tagged ``[TEST]`` in the subject and stored
    with ``is_test=1``.
    """
    scenario = (scenario or "warning").strip().lower()
    if scenario not in test_service.SCENARIOS:
        return {
            "sent": False,
            "status": "invalid_scenario",
            "error": f"unknown scenario '{scenario}'",
            "allowed": sorted(test_service.SCENARIOS.keys()),
        }

    result = test_service.send_demo_email(scenario, None)
    return {
        "sent": bool(result.get("sent")),
        "status": result.get("status"),
        "subject": result.get("subject"),
        "recipients": [config.mask_email(a) if hasattr(config, "mask_email") else a
                       for a in (result.get("recipients") or [])],
        "error": result.get("failure_reason"),
        "demo": True,
        "scenario": scenario,
        "note": "Demo email is tagged [TEST] and never counts as a real alert.",
    }
