"""
Software test-scenario generator for the dashboard "TEST ... DATA" buttons.

It injects a short, clearly-labelled sequence of readings with ``source="test"``
and runs them through the *real* validation -> database -> prediction ->
validation -> risk pipeline.  No hardware is actuated.

The scenarios are NOT hardcoded results: the risk comes out of the same risk
engine that handles live ESP32 data.

Optionally (``send_email=True``, the default) a demo email is also sent so the
operator can confirm the alert layout and SMTP connectivity.  That email is
unmistakably marked ``[TEST]`` — see ``alert_service.build_test_email_html`` —
and is recorded in ``email_notifications`` with ``is_test=1``.  A demo email is
never counted as a real alert.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from backend import config, database
from backend.services import alert_service, sensor_service


N_SAMPLES = 10
INTERVAL_MIN = 1.0

SCENARIOS = {
    "normal":   {"start_cm": 9.0,  "target_cm": 10.5, "rain_adc": 300, "jitter": 0.08, "label": "Normal"},
    "warning":  {"start_cm": 26.0, "target_cm": 34.0, "rain_adc": 1800, "jitter": 0.15, "label": "Warning"},
    "high":     {"start_cm": 40.0, "target_cm": 52.0, "rain_adc": 2600, "jitter": 0.20, "label": "High-risk"},
    "critical": {"start_cm": 55.0, "target_cm": 71.0, "rain_adc": 3400, "jitter": 0.25, "label": "Critical"},
}


def _rain_to_intensity(reading: float) -> float:
    span = config.RAIN_ADC_WET - config.RAIN_ADC_DRY
    pct = (reading - config.RAIN_ADC_DRY) / span * 100.0
    return round(max(0.0, min(100.0, pct)), 2)


def _scenario_payloads(scenario: str, device_id: str) -> list[dict]:
    spec = SCENARIOS[scenario]
    rng = random.Random(hash(scenario) & 0xFFFF)
    now = datetime.now(timezone.utc)
    payloads: list[dict] = []
    for i in range(N_SAMPLES):
        frac = i / (N_SAMPLES - 1)
        level = spec["start_cm"] + (spec["target_cm"] - spec["start_cm"]) * frac
        level += rng.uniform(-spec["jitter"], spec["jitter"])
        level = max(0.0, min(config.MAX_WATER_LEVEL_CM, level))
        rain = int(max(0, min(config.ADC_MAX, spec["rain_adc"] + rng.randint(-60, 60))))
        ts = now - timedelta(minutes=(N_SAMPLES - 1 - i) * INTERVAL_MIN)
        payloads.append(
            {
                "device_id": device_id,
                "source": "test",
                "water_level_cm": round(level, 2),
                "ultrasonic_distance_cm": round(config.REFERENCE_HEIGHT_CM - level, 2),
                "rainfall_reading": rain,
                "rainfall_intensity": _rain_to_intensity(rain),
                "water_sensor_raw": int((level / config.MAX_WATER_LEVEL_CM) * config.ADC_MAX),
                "pir_motion": 0,
                "timestamp": ts.isoformat(),
                "created_epoch": ts.timestamp(),
                "server_timestamp": ts.isoformat(),
                "firmware": "test-harness",
            }
        )
    return payloads


def send_demo_email(scenario: str, final: dict | None = None) -> dict:
    """Send a clearly-marked ``[TEST]`` demo email for a scenario.

    This is a real SMTP send (unless mock mode is on) so the operator can
    verify the email end-to-end.  The email is always flagged ``is_test`` both
    in the subject and in the stored ``email_notifications`` row.
    """
    label = SCENARIOS.get(scenario, {}).get("label", scenario.title())
    html, text = alert_service.build_test_email_html(scenario, label, final)
    subject = alert_service._test_email_subject(label)

    email_service = None
    try:
        from backend.services.email_service import email_service as _svc
        email_service = _svc
    except Exception:
        email_service = None

    if email_service is None:
        return {"sent": False, "status": "no_email_service", "subject": subject}

    result = email_service.send_email(
        subject=subject,
        html_body=html,
        text_body=text,
        is_test=True,          # never auto-triggered — this is an explicit button press
        force_send=True,       # ...but the operator asked for it, so actually send
        mock_override=None,
    )

    # Record it as a demo attempt so the dashboard "Test emails" table shows it.
    try:
        database.insert_email_notification(
            {
                "subject": subject,
                "html_body": html,
                "text_body": text,
                "is_test": True,
                "status": result.get("status", "unknown"),
                "error": result.get("failure_reason"),
            }
        )
    except Exception:
        pass

    return {
        "sent": result.get("status") in ("sent", "mocked", "ok"),
        "status": result.get("status"),
        "subject": subject,
        "recipients": result.get("recipients", []),
        "failure_reason": result.get("failure_reason"),
        "demo": True,
    }


def run_scenario(scenario: str, device_id: str = "test-rig-01", send_email: bool = True) -> dict:
    """Replace any previous test data and replay the chosen scenario."""
    scenario = scenario.strip().lower()
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")

    cleared = database.clear_test_data()
    payloads = _scenario_payloads(scenario, device_id)

    results = []
    final = None
    for p in payloads:
        result, errors = sensor_service.ingest_reading(p, client_ip="127.0.0.1")
        results.append({
            "valid": result["valid"],
            "errors": errors,
            "current_risk": result["current_risk"],
            "forecast_risk": result["forecast_risk"],
            "water_level_cm": result["reading"].get("water_level_cm"),
        })
        final = result

    email_result = None
    if send_email:
        try:
            email_result = send_demo_email(scenario, final)
        except Exception as exc:  # never let email break the scenario itself
            email_result = {"sent": False, "status": "error", "failure_reason": str(exc), "demo": True}

    return {
        "scenario": scenario,
        "label": SCENARIOS[scenario]["label"],
        "cleared_previous_test_data": cleared,
        "n_readings": len(payloads),
        "timeline": results,
        "final": final,
        "is_test": True,
        "email": email_result,
        "disclaimer": (
            "TEST SCENARIO - these readings have source='test' and are not live "
            "ESP32 measurements. No SMS or hardware action was performed. "
            + ("A clearly-marked [TEST] demo email was sent." if email_result and email_result.get("sent")
               else "No demo email was sent (email not configured or delivery failed).")
        ),
    }
