"""
Flood-risk classification, alert logging and (optional) remote notification.

Risk levels: normal < warning < high < critical (see backend/config.py).

Two *separate* signals are produced and never conflated:
  * current_risk  - from the latest MEASURED water level and its rate of rise.
  * forecast_risk - from the VALIDATED model prediction only.

A rejected / unavailable prediction can never raise the risk by itself, and a
faulty sensor cannot create a false critical warning (invalid readings are not
passed here at all).
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from backend import config, database


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _rank(level: str) -> int:
    return config.RISK_RANK.get(level, -1)


def higher(a: str, b: str) -> str:
    return a if _rank(a) >= _rank(b) else b


# Readings closer together than this (in seconds) are treated as one instant.
# Without a floor, a burst of readings stamped milliseconds apart divides a few
# millimetres by a near-zero time delta and the slope explodes into thousands of
# cm/min, falsely escalating a calm pond to "high risk".
MIN_SLOPE_SPAN_SECONDS = 3.0


def rate_of_change_cm_per_min(readings: list[dict]) -> Optional[float]:
    """Least-squares slope (cm/min) over the last few valid readings."""
    pts = [r for r in readings if r.get("water_level_cm") is not None]
    if len(pts) < 2:
        return None
    pts = pts[-5:]
    xs: list[float] = []
    ys: list[float] = []
    used_epochs = True
    for r in pts:
        t = r.get("created_epoch")
        if t is None:
            continue
        xs.append(float(t))
        ys.append(float(r["water_level_cm"]))
    if len(xs) < 2:
        # fall back to sample index if epochs unavailable
        used_epochs = False
        xs = list(range(len(pts)))
        ys = [float(r["water_level_cm"]) for r in pts]
    span_sec = max(xs) - min(xs)
    if used_epochs:
        # Real timestamps: refuse to compute a slope over a span too short for
        # a cm/min figure to mean anything physically.
        if span_sec < MIN_SLOPE_SPAN_SECONDS:
            return None
        span_min = span_sec / 60.0
    else:
        span_min = span_sec / 60.0
    if span_min <= 0:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    slope_per_sec = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom
    return slope_per_sec * 60.0


# --------------------------------------------------------------------------- #
# Risk classification
# --------------------------------------------------------------------------- #
def classify_current_risk(level_cm: Optional[float], rate_cm_per_min: Optional[float] = None) -> str:
    if level_cm is None or (isinstance(level_cm, float) and math.isnan(level_cm)):
        return "unknown"
    base = config.risk_from_level_cm(level_cm)
    if rate_cm_per_min is not None:
        if rate_cm_per_min >= config.RAPID_RISE_HIGH_CM_PER_MIN:
            base = higher(base, "high")
        elif rate_cm_per_min >= config.RAPID_RISE_WARNING_CM_PER_MIN:
            base = higher(base, "warning")
    return base


def classify_forecast_risk(accepted_values_cm: list[float]) -> str:
    vals = [v for v in accepted_values_cm if v is not None and not math.isnan(v)]
    if not vals:
        return "unknown"
    return config.risk_from_level_cm(max(vals))


# --------------------------------------------------------------------------- #
# Remote notification (SMS / webhook)
# --------------------------------------------------------------------------- #
def sms_configured() -> bool:
    if config.SMS_PROVIDER == "http":
        return bool(config.SMS_HTTP_URL)
    if config.SMS_PROVIDER == "twilio":
        return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN
                    and config.TWILIO_FROM_NUMBER and config.SMS_TO_NUMBER)
    return False


def send_notification(message: str) -> tuple[bool, str]:
    """
    Attempt remote delivery.

    Returns ``(notified, status)`` where status is one of
    'not_configured', 'sent', 'failed: <reason>'.  'sent' means the provider
    accepted the request (HTTP 2xx) - it does not guarantee handset delivery.
    """
    if not sms_configured():
        return False, "not_configured"

    try:
        if config.SMS_PROVIDER == "http":
            payload = json.dumps({"to": config.SMS_TO_NUMBER, "message": message}).encode()
            req = urllib.request.Request(config.SMS_HTTP_URL, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            if config.SMS_HTTP_TOKEN:
                req.add_header("Authorization", f"Bearer {config.SMS_HTTP_TOKEN}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True, "sent"
                return False, f"failed: HTTP {resp.status}"

        if config.SMS_PROVIDER == "twilio":
            url = (
                f"https://api.twilio.com/2010-04-01/Accounts/"
                f"{config.TWILIO_ACCOUNT_SID}/Messages.json"
            )
            data = urllib.parse.urlencode(
                {"To": config.SMS_TO_NUMBER, "From": config.TWILIO_FROM_NUMBER,
                 "Body": message}
            ).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            import base64

            token = base64.b64encode(
                f"{config.TWILIO_ACCOUNT_SID}:{config.TWILIO_AUTH_TOKEN}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {token}")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True, "sent"
                return False, f"failed: HTTP {resp.status}"
    except urllib.error.URLError as exc:
        return False, f"failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"failed: {exc}"

    return False, "failed: unknown provider"


# --------------------------------------------------------------------------- #
# Alert recording
# --------------------------------------------------------------------------- #
def record_alert(
    risk_level: str,
    origin: str,
    message: str,
    device_id: str = "unknown",
    dedupe_key: Optional[str] = None,
    is_test: bool = False,
    prediction: Optional[dict] = None,
) -> dict:
    """
    Persist an alert with duplicate suppression and optional notification.

    Test alerts are stored but NEVER trigger a real SMS/hardware action.
    Returns a dict describing what happened (never over-claims delivery).
    """
    dedupe_key = dedupe_key or f"{origin}:{risk_level}"
    if risk_level in ("normal", "unknown"):
        return {"logged": False, "reason": "risk below alert threshold", "risk_level": risk_level}

    if database.recent_alert_exists(dedupe_key, config.ALERT_DEDUPE_WINDOW_SECONDS):
        return {"logged": False, "reason": "duplicate suppressed", "risk_level": risk_level,
                "dedupe_key": dedupe_key}

    notified = False
    if is_test:
        status = "skipped_test"
    else:
        notified, status = send_notification(message)

    alert_id = database.insert_alert(
        {
            "device_id": device_id,
            "risk_level": risk_level,
            "origin": origin,
            "message": message,
            "dedupe_key": dedupe_key,
            "notified": notified,
            "notification_status": status,
        }
    )
    return {
        "logged": True,
        "alert_id": alert_id,
        "risk_level": risk_level,
        "origin": origin,
        "notified": notified,
        "notification_status": status,
        "message": message,
    }


def _email_subject(trans_type: str, to_risk: str) -> str:
    if trans_type == "recovery":
        return "[Flood Alert] RECOVERY — Risk Returned to Normal"
    if to_risk == "critical":
        return "[Flood Alert] CRITICAL — Urgent Flood Risk"
    if to_risk == "high":
        return "[Flood Alert] HIGH RISK — Immediate Attention Required"
    if to_risk == "warning":
        return "[Flood Alert] WARNING — Water Level Rising"
    return f"[Flood Alert] {to_risk.upper()} — Risk Transition"


def build_transition_email_html(
    transition_info: dict,
    reading: dict,
    device_status: str,
    prediction: Optional[dict] = None,
    thresholds: Optional[dict] = None,
) -> tuple[str, str]:
    """Build (html_body, text_body) for a risk transition email."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    prev = transition_info.get("from", "normal")
    new = transition_info.get("to", "unknown")
    ttype = transition_info.get("type", "alert")
    at = transition_info.get("at", now)

    t = thresholds or config.thresholds()
    color = {"normal": "#16a34a", "warning": "#d97706", "high": "#dc2626", "critical": "#7c2d12"}.get(new, "#334155")
    badge = new.replace("_", " ").upper()

    level = reading.get("water_level_cm")
    level_str = f"{level:.1f} cm" if isinstance(level, (int, float)) else "n/a"
    dist = reading.get("ultrasonic_distance_cm")
    dist_str = f"{dist:.1f} cm" if isinstance(dist, (int, float)) else "n/a"
    rain = reading.get("rainfall_reading")
    rain_str = f"{rain:.0f} (12-bit ADC)" if isinstance(rain, (int, float)) else "n/a"
    rain_pct = reading.get("rainfall_intensity")
    rain_pct_str = f"{rain_pct:.1f}%" if isinstance(rain_pct, (int, float)) else "n/a"

    pred_html = ""
    pred_text = ""
    if prediction and prediction.get("available"):
        preds = prediction.get("predictions") or []
        accepted = [p for p in preds if p.get("accepted")]
        if accepted:
            peak = max(accepted, key=lambda p: p.get("predicted_level_cm") or 0)
            horizon = peak.get("horizon_min", "?")
            plevel = peak.get("predicted_level_cm")
            plevel_str = f"{plevel:.1f} cm" if isinstance(plevel, (int, float)) else "n/a"
            pred_html = f"""
            <tr><td>Predicted level (+{horizon} min)</td><td>{plevel_str}</td></tr>
            <tr><td>Forecast risk</td><td>{prediction.get('forecast_risk', 'unknown')}</td></tr>
            """
            pred_text = f"Predicted level (+{horizon} min): {plevel_str}\nForecast risk: {prediction.get('forecast_risk', 'unknown')}\n"
        else:
            pred_html = "<tr><td>Prediction</td><td>No accepted forecast (validation layer)</td></tr>"
            pred_text = "Prediction: No accepted forecast (validation layer)\n"
    else:
        pred_html = "<tr><td>Prediction</td><td>Unavailable</td></tr>"
        pred_text = "Prediction: Unavailable\n"

    html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;background:#f4f6f9;padding:20px;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:8px;border:1px solid #e2e8f0;">
    <div style="background:{color};color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
      <h2 style="margin:0;">[Flood Alert] {badge}</h2>
      <p style="margin:4px 0 0;opacity:0.9;">Flood Prediction and Early Warning System</p>
    </div>
    <div style="padding:20px;">
      <p>A risk transition has been detected.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr style="background:#f8fafc;"><td style="padding:8px;border:1px solid #e2e8f0;"><b>Previous risk</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{prev.upper()}</td></tr>
        <tr><td style="padding:8px;border:1px solid #e2e8f0;"><b>New risk</b></td><td style="padding:8px;border:1px solid #e2e8f0;color:{color};font-weight:bold;">{new.upper()}</td></tr>
        <tr style="background:#f8fafc;"><td style="padding:8px;border:1px solid #e2e8f0;"><b>Transition type</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{ttype}</td></tr>
        <tr><td style="padding:8px;border:1px solid #e2e8f0;"><b>Transition time</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{at}</td></tr>
        <tr style="background:#f8fafc;"><td style="padding:8px;border:1px solid #e2e8f0;"><b>Device status</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{device_status}</td></tr>
        <tr><td style="padding:8px;border:1px solid #e2e8f0;"><b>Water level</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{level_str}</td></tr>
        <tr style="background:#f8fafc;"><td style="padding:8px;border:1px solid #e2e8f0;"><b>Ultrasonic distance</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{dist_str}</td></tr>
        <tr><td style="padding:8px;border:1px solid #e2e8f0;"><b>Rainfall (raw ADC)</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{rain_str}</td></tr>
        <tr style="background:#f8fafc;"><td style="padding:8px;border:1px solid #e2e8f0;"><b>Rainfall intensity</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{rain_pct_str}</td></tr>
        {pred_html}
      </table>
      <p style="margin-top:16px;color:#64748b;font-size:12px;">
        Thresholds: warning &ge; {t.get('warning_cm', 30)} cm, high &ge; {t.get('high_cm', 45)} cm, critical &ge; {t.get('critical_cm', 65)} cm.
        Dashboard: {config.DASHBOARD_URL}
      </p>
    </div>
    <div style="padding:12px 20px;background:#f8fafc;border-radius:0 0 8px 8px;color:#94a3b8;font-size:12px;">
      Sent by the Flood Prediction and Early Warning System. No action required if this is a test.
    </div>
  </div>
</body></html>"""

    text = f"""[Flood Alert] {badge}
Flood Prediction and Early Warning System

A risk transition has been detected.
Previous risk: {prev.upper()}
New risk: {new.upper()}
Transition type: {ttype}
Transition time: {at}
Device status: {device_status}
Water level: {level_str}
Ultrasonic distance: {dist_str}
Rainfall (raw ADC): {rain_str}
Rainfall intensity: {rain_pct_str}
{pred_text}
Thresholds: warning >= {t.get('warning_cm', 30)} cm, high >= {t.get('high_cm', 45)} cm, critical >= {t.get('critical_cm', 65)} cm.
Dashboard: {config.DASHBOARD_URL}
"""
    return html, text


def _test_email_subject(label: str) -> str:
    """Subject for the demo email sent by the TEST buttons.

    The [TEST] tag is deliberate and must never be removed: it makes it
    impossible to mistake a demo for a real flood alert.
    """
    return f"[Flood Alert][TEST] {label} — Demo only, no real flood"


def build_test_email_html(
    scenario: str,
    label: str,
    reading: Optional[dict] = None,
    thresholds: Optional[dict] = None,
) -> tuple[str, str]:
    """Build (html_body, text_body) for a demo email triggered by a TEST button.

    The message is styled like a real alert so operators can check the layout,
    but it is unmistakably marked as a TEST scenario: no hardware was actuated
    and no real flood is occurring.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    reading = reading or {}
    t = thresholds or config.thresholds()

    risk = (reading.get("overall_risk") or reading.get("current_risk") or scenario or "unknown")
    color = {"normal": "#16a34a", "warning": "#d97706", "high": "#dc2626", "critical": "#7c2d12"}.get(
        scenario, "#0f766e"
    )
    badge = f"{label.upper()} (TEST)"

    level = reading.get("water_level_cm")
    if level is None and reading.get("reading"):
        level = reading["reading"].get("water_level_cm")
    level_str = f"{level:.1f} cm" if isinstance(level, (int, float)) else "n/a"

    html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;background:#f4f6f9;padding:20px;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:8px;border:1px solid #e2e8f0;">
    <div style="background:{color};color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
      <div style="font-size:12px;letter-spacing:1px;opacity:0.95;font-weight:bold;">TEST SCENARIO — DEMO EMAIL</div>
      <h2 style="margin:4px 0 0;">[Flood Alert] {badge}</h2>
      <p style="margin:4px 0 0;opacity:0.9;">Flood Prediction and Early Warning System</p>
    </div>
    <div style="background:#fff7ed;border-bottom:1px solid #fed7aa;padding:12px 20px;color:#9a3412;font-size:13px;">
      <b>This is a simulated alert.</b> The readings below were injected by the
      dashboard's <b>TEST {label.upper()}</b> button (source=<code>test</code>).
      No real flood is occurring and no SMS or hardware action was performed.
    </div>
    <div style="padding:20px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr style="background:#f8fafc;"><td style="padding:8px;border:1px solid #e2e8f0;"><b>Scenario</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{label}</td></tr>
        <tr><td style="padding:8px;border:1px solid #e2e8f0;"><b>Simulated risk</b></td><td style="padding:8px;border:1px solid #e2e8f0;color:{color};font-weight:bold;">{str(risk).upper()}</td></tr>
        <tr style="background:#f8fafc;"><td style="padding:8px;border:1px solid #e2e8f0;"><b>Water level</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{level_str}</td></tr>
        <tr><td style="padding:8px;border:1px solid #e2e8f0;"><b>Data source</b></td><td style="padding:8px;border:1px solid #e2e8f0;">test (not live ESP32)</td></tr>
        <tr style="background:#f8fafc;"><td style="padding:8px;border:1px solid #e2e8f0;"><b>Generated</b></td><td style="padding:8px;border:1px solid #e2e8f0;">{now}</td></tr>
      </table>
      <p style="margin-top:16px;color:#64748b;font-size:12px;">
        Thresholds: warning &ge; {t.get('warning_cm', 30)} cm, high &ge; {t.get('high_cm', 45)} cm, critical &ge; {t.get('critical_cm', 65)} cm.
        Dashboard: {config.DASHBOARD_URL}
      </p>
    </div>
    <div style="padding:12px 20px;background:#f8fafc;border-radius:0 0 8px 8px;color:#94a3b8;font-size:12px;">
      This demo email was requested from the dashboard. Real alerts never carry the [TEST] tag.
    </div>
  </div>
</body></html>"""

    text = f"""[Flood Alert][TEST] {label} — DEMO ONLY
Flood Prediction and Early Warning System

*** THIS IS A SIMULATED ALERT ***
Injected by the dashboard TEST {label.upper()} button (source=test).
No real flood is occurring and no SMS or hardware action was performed.

Scenario: {label}
Simulated risk: {str(risk).upper()}
Water level: {level_str}
Data source: test (not live ESP32)
Generated: {now}

Thresholds: warning >= {t.get('warning_cm', 30)} cm, high >= {t.get('high_cm', 45)} cm, critical >= {t.get('critical_cm', 65)} cm.
Dashboard: {config.DASHBOARD_URL}
"""
    return html, text
