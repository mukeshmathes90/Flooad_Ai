"""
Tests for the email alert service.

Covers: startup suppression, no-send on unchanged risk, single-send on
genuine transition, test-scenario isolation, and safe retry on SMTP failure.
SMTP is mocked - no real email is ever sent during the test suite.
"""
import pytest

from backend.services import alert_state_service, email_service, sensor_service
from backend.services.email_service import EmailService
from backend import database


@pytest.fixture(autouse=True)
def fresh_state():
    """Reset alert state and reading history before each test (fresh system)."""
    database.init_db()
    with database.cursor(commit=True) as cur:
        cur.execute("DELETE FROM alert_state")
        cur.execute("DELETE FROM sensor_readings")
        cur.execute("DELETE FROM alerts")
        cur.execute("DELETE FROM email_notifications")
        cur.execute(
            """INSERT INTO alert_state
               (id, last_valid_risk, initialized, updated_at, updated_epoch)
               VALUES (1, 'normal', 0, '1970-01-01T00:00:00+00:00', 0.0)"""
        )
    _seq["n"] = 0
    yield


import time

_seq = {"n": 0}
_BASE = time.time() - 4000.0


def _send(reading, device_id="test-device"):
    _seq["n"] += 1
    ts = _BASE + _seq["n"] * 600.0  # 10 minutes apart -> gentle, realistic slope
    payload = {
        "device_id": device_id,
        "source": "esp32",
        "water_level_cm": reading,
        "ultrasonic_distance_cm": 100.0 - reading,
        "rainfall_reading": 0,
        "rainfall_intensity": 0.0,
        "water_sensor_raw": 0,
        "pir_motion": 0,
        "timestamp": None,
        "created_epoch": ts,
        "server_timestamp": None,
        "firmware": "test",
    }
    return sensor_service.ingest_reading(payload)


class _MockSMTP:
    """Mock SMTP that records sends, can simulate failures."""
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times
        self.calls = 0

    def send(self, to_addr=None, subject="", html_body="", text_body="", is_test=False,
             force_send=False, mock_override=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            return {"sent": False, "delivered": False, "status": "failed: mocked",
                    "error": "mocked failure", "timestamp": "2026-01-01T00:00:00Z"}
        # Faithful to EmailService: test data never auto-delivers unless force_send
        # (the TEST buttons).  This keeps call-counts meaningful for scenario tests.
        if is_test and not force_send:
            return {"sent": False, "delivered": False, "status": "skipped_test",
                    "error": None, "timestamp": "2026-01-01T00:00:00Z"}
        to_addr = to_addr or email_service.email_service.get_recipients()
        self.sent.append({"to": to_addr, "subject": subject, "html": html_body,
                          "text": text_body, "is_test": is_test, "force_send": force_send})
        return {"sent": True, "delivered": True, "status": "sent",
                "error": None, "timestamp": "2026-01-01T00:00:00Z"}


def _patch(monkeypatch, mock):
    import backend.services.sensor_service as _ss
    monkeypatch.setattr(_ss.email_service.email_service, "send_email", mock.send, raising=False)


def test_startup_no_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    assert mock.calls == 0


def test_first_normal_no_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    assert mock.calls == 0


def test_first_warning_no_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(32.0)  # warning level
    assert mock.calls == 0


def test_first_high_no_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(50.0)  # high risk
    assert mock.calls == 0


def test_first_critical_no_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(70.0)  # critical
    assert mock.calls == 0


def test_repeated_normal_no_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(10.0)
    _send(10.0)
    assert mock.calls == 0


def test_normal_to_warning_one_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    assert mock.calls == 1
    assert "WARNING" in mock.sent[0]["subject"]


def test_warning_to_high_one_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    _send(50.0)
    assert mock.calls == 2
    assert "HIGH RISK" in mock.sent[1]["subject"]


def test_high_to_critical_one_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    _send(50.0)
    _send(70.0)
    assert mock.calls == 3
    assert "CRITICAL" in mock.sent[2]["subject"]


def test_critical_to_high_sends_update(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    _send(50.0)
    _send(70.0)
    _send(50.0)
    assert mock.calls == 4
    assert "HIGH RISK" in mock.sent[3]["subject"]


def test_critical_to_normal_recovery(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    _send(50.0)
    _send(70.0)
    _send(10.0)
    assert mock.calls == 4
    assert "RECOVERY" in mock.sent[3]["subject"]


def test_repeated_same_category_no_duplicate(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    _send(32.0)
    _send(32.0)
    _send(32.0)
    assert mock.calls == 1


def test_invalid_reading_no_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    payload = {
        "device_id": "bad-device",
        "source": "esp32",
        "water_level_cm": 1000.0,
        "ultrasonic_distance_cm": -50.0,
        "rainfall_reading": 0,
        "rainfall_intensity": 0.0,
        "water_sensor_raw": 0,
        "pir_motion": 0,
        "timestamp": None,
        "firmware": "test",
    }
    sensor_service.ingest_reading(payload)
    assert mock.calls == 0


def test_reconnect_no_duplicate(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    _send(32.0)
    assert mock.calls == 1


def test_restart_no_startup_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    # simulate restart: initialize from db preserves state, no new email
    alert_state_service.alert_state.initialize_from_db("warning")
    _send(32.0)
    assert mock.calls == 1


def test_smtp_failure_recorded_not_crash(monkeypatch):
    mock = _MockSMTP(fail_times=10)
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    assert mock.calls > 0
    # state advanced
    st = alert_state_service.alert_state.get_state()
    assert st.get("last_valid_risk") == "warning"


def test_failed_retry_no_duplicate_success(monkeypatch):
    mock_fail = _MockSMTP(fail_times=10)
    _patch(monkeypatch, mock_fail)
    _send(10.0)
    _send(32.0)
    assert len(mock_fail.sent) == 0

    # now patch with a successful mock and retry
    mock_ok = _MockSMTP()
    _patch(monkeypatch, mock_ok)
    alert_state_service.alert_state.record_email_sent(None, "sent")
    st = alert_state_service.alert_state.get_state()
    assert st.get("last_valid_risk") == "warning"
    assert mock_ok.calls == 0


def test_test_scenario_no_email(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    payload = {
        "device_id": "test-scenario",
        "source": "test",
        "water_level_cm": 32.0,
        "ultrasonic_distance_cm": 68.0,
        "rainfall_reading": 0,
        "rainfall_intensity": 0.0,
        "water_sensor_raw": 0,
        "pir_motion": 0,
        "timestamp": None,
        "firmware": "test",
    }
    sensor_service.ingest_reading(payload)
    assert mock.calls == 0


def test_email_content_contains_measurements(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    assert len(mock.sent) == 1
    html = mock.sent[0]["html"]
    assert "32.0 cm" in html or "32.0" in html
    assert "Flood Prediction and Early Warning System" in html


def test_email_subject_correct_for_recovery(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    _send(10.0)
    assert "RECOVERY" in mock.sent[-1]["subject"]


def test_email_history_persisted(monkeypatch):
    mock = _MockSMTP()
    _patch(monkeypatch, mock)
    _send(10.0)
    _send(32.0)
    rows = database.get_email_notifications(limit=10)
    assert len(rows) >= 1
    assert rows[0]["subject"]
    assert rows[0]["html_body"]


# --------------------------------------------------------------------------- #
# TEST-button demo email
# --------------------------------------------------------------------------- #
def test_send_email_is_test_skips_by_default():
    """A plain is_test send must NOT hit SMTP (guards against alert spam)."""
    svc = EmailService()
    svc.mock = False
    calls = []
    svc._deliver = lambda *a, **k: calls.append(a) or (True, None)
    res = svc.send_email(subject="x", html_body="<p>x</p>", text_body="x", is_test=True)
    assert res["status"] == "skipped_test"
    assert calls == []


def test_send_email_force_send_overrides_skip():
    """force_send=True (used only by the TEST buttons) actually delivers."""
    svc = EmailService()
    svc.mock = False
    svc.sender, svc.password = "a@example.com", "pw"
    delivered = []
    svc._deliver = lambda *a, **k: delivered.append(a) or (True, None)
    svc.validate_config = lambda: (True, None)
    res = svc.send_email(
        subject="[Flood Alert][TEST] Warning — Demo only, no real flood",
        html_body="<p>demo</p>", text_body="demo",
        is_test=True, force_send=True,
    )
    assert res["status"] == "sent"
    assert len(delivered) == 1


def test_demo_email_on_test_button(monkeypatch):
    """Pressing TEST WARNING sends exactly one clearly-tagged demo email."""
    from backend.services import test_service
    mock = _MockSMTP()
    _patch(monkeypatch, mock)

    result = test_service.run_scenario("warning")

    assert len(mock.sent) == 1, "exactly one demo email must actually be delivered"
    sent = mock.sent[0]
    assert "[TEST]" in sent["subject"]
    assert sent["is_test"] is True
    assert sent["force_send"] is True
    assert result["email"]["sent"] is True
    assert "[TEST]" in result["email"]["subject"]


def test_demo_email_body_is_marked_test(monkeypatch):
    """The demo email must be unmistakably a simulation in subject AND body."""
    from backend.services import test_service
    mock = _MockSMTP()
    _patch(monkeypatch, mock)

    test_service.run_scenario("critical")
    sent = mock.sent[0]
    assert "[TEST]" in sent["subject"]
    assert "TEST SCENARIO" in sent["html"]
    assert "simulated" in sent["html"].lower()
    assert "[TEST]" in sent["subject"]


def test_demo_email_recorded_with_is_test_flag(monkeypatch):
    """Demo emails are logged with is_test=1 so they never read as real alerts."""
    from backend.services import test_service
    mock = _MockSMTP()
    _patch(monkeypatch, mock)

    test_service.run_scenario("high")

    rows = database.get_email_notifications(limit=10)
    test_rows = [r for r in rows if r.get("is_test")]
    assert test_rows, "at least the demo email must be logged as is_test"
    # Every logged notification for a scenario is flagged is_test - none can
    # masquerade as a real alert.
    assert all(r.get("is_test") for r in rows if "[TEST]" in (r.get("subject") or "")
               or (r.get("status") or "") == "skipped_test")

    demo = [r for r in rows if "[TEST]" in (r.get("subject") or "")]
    assert len(demo) == 1
    assert demo[0]["status"] == "sent"
    assert demo[0]["is_test"] == 1


def test_demo_email_does_not_advance_alert_state(monkeypatch):
    """A TEST button must not poison the live risk state or raise a live email."""
    from backend.services import test_service
    mock = _MockSMTP()
    _patch(monkeypatch, mock)

    _send(10.0)                      # live, normal, silent
    live_before = alert_state_service.alert_state.get_state()
    n_sent_before = len(mock.sent)

    test_service.run_scenario("critical")

    live_after = alert_state_service.alert_state.get_state()
    test_state = alert_state_service.alert_state.get_state(is_test=True)

    # The LIVE machine is untouched: still 'normal', so no bogus RECOVERY later.
    assert live_after["last_valid_risk"] == live_before["last_valid_risk"] == "normal"
    # Only the demo email went out - the scenario's own transitions are skipped_test.
    assert len(mock.sent) - n_sent_before == 1
    assert all("[TEST]" in s["subject"] for s in mock.sent[n_sent_before:])
    # The scenario DID exercise the isolated TEST machine (real pipeline).
    assert test_state.get("last_valid_risk") == "critical"


def test_test_scenario_transitions_stay_isolated(monkeypatch):
    """Live normal -> TEST critical -> live normal must NOT email a RECOVERY."""
    from backend.services import test_service, sensor_service as ss
    mock = _MockSMTP()
    _patch(monkeypatch, mock)

    _send(10.0)                                    # live normal, silent
    sent_before = len(mock.sent)

    test_service.run_scenario("critical")          # TEST critical
    assert alert_state_service.alert_state.get_state()["last_valid_risk"] == "normal"

    _send(9.0)                                     # live still normal
    sent_after = len(mock.sent)

    real = [s for s in mock.sent[sent_before:sent_after] if "[TEST]" not in s["subject"]]
    assert real == [], "no live/RECOVERY email may fire because of a TEST scenario"


def test_demo_email_disabled_option(monkeypatch):
    """run_scenario(send_email=False) injects data but sends nothing."""
    from backend.services import test_service
    mock = _MockSMTP()
    _patch(monkeypatch, mock)

    result = test_service.run_scenario("warning", send_email=False)
    assert len(mock.sent) == 0, "no email may be delivered when send_email=False"
    assert result["email"] is None


def test_burst_readings_do_not_false_escalate(monkeypatch):
    """Near-simultaneous readings must not explode the cm/min slope.

    Sub-second jitter dividing a few millimetres by a near-zero time delta used
    to produce thousands of cm/min and falsely escalate calm water to 'high'.
    """
    from backend.services import alert_service
    import time as _t

    now = _t.time()
    burst = [
        {"water_level_cm": 10.0, "created_epoch": now - 0.002},
        {"water_level_cm": 11.0, "created_epoch": now - 0.001},
        {"water_level_cm": 10.5, "created_epoch": now},
    ]
    assert alert_service.rate_of_change_cm_per_min(burst) is None

    # A realistic 10-minute spacing still yields a usable slope.
    spaced = [
        {"water_level_cm": 10.0, "created_epoch": now - 1200},
        {"water_level_cm": 12.0, "created_epoch": now - 600},
        {"water_level_cm": 14.0, "created_epoch": now},
    ]
    rate = alert_service.rate_of_change_cm_per_min(spaced)
    assert rate is not None
    assert 0.0 < rate < 1.0          # ~1 cm/min, not thousands


def test_live_normal_then_test_critical_then_normal_stays_silent(monkeypatch):
    """End-to-end: the full toggle cycle must emit exactly one email (the demo)."""
    from backend.services import test_service
    mock = _MockSMTP()
    _patch(monkeypatch, mock)

    _send(10.0)
    _send(11.0)
    _send(10.5)
    assert len(mock.sent) == 0, "calm live data must stay silent"

    test_service.run_scenario("critical")
    _send(9.5)
    _send(9.0)

    real = [s for s in mock.sent if "[TEST]" not in s["subject"]]
    assert real == [], f"expected no live email, got {real}"
    assert len(mock.sent) == 1 and "[TEST]" in mock.sent[0]["subject"]


def test_send_test_endpoint(monkeypatch):
    """POST /api/email/send-test returns a [TEST] subject and does not error."""
    from fastapi.testclient import TestClient
    from backend.main import app
    mock = _MockSMTP()
    _patch(monkeypatch, mock)

    client = TestClient(app)
    r = client.post("/api/email/send-test?scenario=warning")
    assert r.status_code == 200
    body = r.json()
    assert body["demo"] is True
    assert "[TEST]" in body["subject"]
    assert len(mock.sent) == 1


def test_send_test_endpoint_rejects_bad_scenario():
    from fastapi.testclient import TestClient
    from backend.main import app
    client = TestClient(app)
    r = client.post("/api/email/send-test?scenario=banana")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert body["status"] == "invalid_scenario"
