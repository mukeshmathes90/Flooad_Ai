"""
Sensor service: validate, store and analyse incoming readings.

This is the orchestrator for one incoming measurement:

    validate -> store -> current-level risk -> ML prediction -> overall risk/alert

Invalid readings are stored flagged (valid=0) for diagnostics but never enter the
risk engine or the prediction context.
"""

from __future__ import annotations

from typing import Optional

from backend import config, database
from backend.services import alert_service, alert_state_service, email_service, prediction_service, validation_service


def _fmt_level(level) -> str:
    return f"{level:.1f} cm" if isinstance(level, (int, float)) else "?"


def ingest_reading(payload: dict, client_ip: Optional[str] = None) -> tuple[dict, list[str]]:
    """
    Returns ``(result, errors)``.  ``errors`` is non-empty when the payload was
    physically invalid; the API maps that to HTTP 422.
    """
    normalized, errors = validation_service.validate_sensor_payload(payload)
    valid = len(errors) == 0

    normalized["valid"] = valid
    normalized["invalid_reason"] = "; ".join(errors) if errors else None

    reading_id = database.insert_reading(normalized)
    device_id = normalized.get("device_id", "unknown")
    database.upsert_device(device_id, ip=client_ip, firmware=payload.get("firmware"))

    source = normalized.get("source", "esp32")
    is_test = source == "test"

    result: dict = {
        "stored": True,
        "reading_id": reading_id,
        "valid": valid,
        "invalid_reason": normalized["invalid_reason"],
        "reading": normalized,
        "source": source,
        "is_test": is_test,
        "current_risk": "unknown",
        "forecast_risk": "unknown",
        "overall_risk": "unknown",
        "rate_cm_per_min": None,
        "prediction": None,
        "alerts": [],
    }

    if not valid:
        # Do NOT run risk/prediction on invalid data; never treat as a zero
        # reading that could look like normal conditions.
        result["note"] = "reading rejected: physical validation failed; not used for risk or prediction"
        return result, errors

    # ---- current measured-level risk ------------------------------------- #
    recent = prediction_service.get_context_readings(source)
    rate = alert_service.rate_of_change_cm_per_min(recent)
    result["rate_cm_per_min"] = round(rate, 3) if rate is not None else None

    # Rate-of-rise needs at least 2 points for a meaningful slope; suppress it
    # on the first reading of a fresh source to avoid spurious escalation.
    n_recent = len([r for r in recent if r.get("water_level_cm") is not None])
    current_risk = alert_service.classify_current_risk(
        normalized.get("water_level_cm"),
        rate if n_recent >= 2 else None,
    )
    result["current_risk"] = current_risk

    # Determine risk-state transition (startup-suppressed, duplicate-safe).
    # Test readings are routed to the isolated TEST state machine so a scenario
    # can never move the LIVE risk state or fire a live email.
    state = alert_state_service.alert_state
    should_notify, trans_type, trans_info = state.record_valid_reading(current_risk, is_test=is_test)
    result["risk_transition"] = {"should_notify": should_notify, "type": trans_type, "info": trans_info}
    if should_notify and trans_info and trans_type in ("to_alert", "recovery", "upgrade", "downgrade"):
        latest_reading = database.get_latest_reading(source=source) or {}
        created = latest_reading.get("created_epoch")
        age = None
        if created is not None:
            from datetime import datetime, timezone
            age = datetime.now(timezone.utc).timestamp() - float(created)
        if age is None:
            dev_status = "unknown"
        elif age > config.OFFLINE_AFTER_SECONDS:
            dev_status = "offline"
        elif age > config.STALE_AFTER_SECONDS:
            dev_status = "stale"
        else:
            dev_status = "live"
        html_body, text_body = alert_service.build_transition_email_html(
            trans_info, normalized, dev_status, thresholds=config.thresholds()
        )
        email_subject = alert_service._email_subject(trans_type, trans_info.get("to", "unknown"))
        email_result = email_service.email_service.send_email(
            subject=email_subject,
            html_body=html_body, text_body=text_body, is_test=is_test
        )
        notified = bool(email_result.get("sent"))
        db_alert_id = database.insert_alert(
            {
                "device_id": device_id,
                "risk_level": trans_info.get("to", current_risk),
                "origin": "risk_transition",
                "message": f"Risk transition {trans_type}: {trans_info.get('from','normal')} -> {trans_info.get('to','unknown')}",
                "dedupe_key": f"risk_transition:{trans_info.get('from','normal')}:{trans_info.get('to','unknown')}",
                "notified": notified,
                "notification_status": email_result.get("status"),
            }
        )
        database.insert_email_notification({
            "alert_id": db_alert_id,
            "subject": email_subject,
            "html_body": html_body,
            "text_body": text_body,
            "is_test": is_test,
            "status": email_result.get("status"),
            "error": email_result.get("error"),
            "sent_at": email_result.get("sent_at"),
        })
        state.record_email_sent(db_alert_id, email_result.get("status"), is_test=is_test)
        result["alerts"].append({
            "logged": True,
            "alert_id": db_alert_id,
            "risk_level": trans_info.get("to", current_risk),
            "notified": notified,
            "notification_status": email_result.get("status"),
        })
    if current_risk in ("warning", "high", "critical") and not should_notify:
        # Log the alert-level reading for the dashboard history, but only email
        # on a genuine risk-state transition (handled above). Repeated readings
        # in the same category must not generate duplicate emails.
        origin = "test_sensor_current" if is_test else "sensor_current"
        msg = (
            f"[{current_risk.upper()}] Measured water level {_fmt_level(normalized.get('water_level_cm'))}"
            + (f", rising {rate:.2f} cm/min" if rate else "")
            + (" [TEST SCENARIO]" if is_test else "")
            + "."
        )
        result["alerts"].append(
            alert_service.record_alert(
                current_risk, origin, msg,
                device_id=device_id,
                dedupe_key=f"{origin}:{current_risk}",
                is_test=is_test,
            )
        )

    # ---- ML forecast ------------------------------------------------------ #
    pred = prediction_service.run_prediction(source=source, is_test=is_test)
    result["prediction"] = pred
    result["forecast_risk"] = pred.get("forecast_risk", "unknown")
    result["alerts"].extend(pred.get("alerts", []))

    result["overall_risk"] = alert_service.higher(current_risk, result["forecast_risk"])
    return result, errors


def latest_status(source: Optional[str] = None) -> dict:
    """Connectivity / freshness summary for the dashboard."""
    from datetime import datetime, timezone

    reading = database.get_latest_reading(source=source)
    if not reading:
        return {"connected": False, "status": "no_data", "last_seen": None, "age_seconds": None}
    created = reading.get("created_epoch")
    age = None
    if created is not None:
        age = datetime.now(timezone.utc).timestamp() - float(created)
    if age is None:
        status = "unknown"
    elif age > config.OFFLINE_AFTER_SECONDS:
        status = "offline"
    elif age > config.STALE_AFTER_SECONDS:
        status = "stale"
    else:
        status = "live"
    return {
        "connected": status in ("live", "stale"),
        "status": status,
        "last_seen": reading.get("server_timestamp"),
        "age_seconds": round(age, 1) if age is not None else None,
        "device_id": reading.get("device_id"),
        "source": reading.get("source"),
    }
