"""End-to-end API tests for the Flood Prediction and Early Warning System."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "database" in body and body["database"]["ok"] is True
    assert "model" in body


def test_api_index(client):
    r = client.get("/api")
    assert r.status_code == 200
    assert "endpoints" in r.json()


def test_ingest_valid_reading_normal(client):
    payload = {
        "device_id": "esp32-test",
        "source": "esp32",
        "water_level_cm": 10.0,
        "ultrasonic_distance_cm": 90.0,
        "rainfall_reading": 350,
        "rainfall_intensity": 0.0,
        "water_sensor_raw": 400,
        "pir_motion": 0,
    }
    r = client.post("/api/sensors/data", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["stored"] is True
    assert body["current_risk"] == "normal"
    assert body["reading"]["water_level_cm"] == 10.0


def test_distance_to_level_derivation(client):
    # only distance supplied -> backend derives water level = reference - distance
    r = client.post("/api/sensors/data", json={
        "device_id": "esp32-derive", "source": "esp32",
        "ultrasonic_distance_cm": 85.0, "rainfall_reading": 300,
    })
    assert r.status_code == 200, r.text
    assert abs(r.json()["reading"]["water_level_cm"] - 15.0) < 1e-6


def test_ingest_invalid_reading_rejected(client):
    # water level far outside the physical range -> 422, stored but valid=0
    r = client.post("/api/sensors/data", json={
        "device_id": "esp32-bad", "source": "esp32",
        "water_level_cm": 500.0, "ultrasonic_distance_cm": -400.0,
    })
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["valid"] is False
    assert body["errors"]
    assert body["reading"]["water_level_cm"] != 0  # never coerced to a fake zero


def test_ultrasonic_timeout_not_flood(client):
    # an out-of-range / timeout distance must not be treated as a flood
    r = client.post("/api/sensors/data", json={
        "device_id": "esp32-timeout", "source": "esp32",
        "ultrasonic_distance_cm": 0.0,
    })
    assert r.status_code == 422
    assert r.json()["overall_risk"] in ("unknown", "normal")


def test_latest_and_recent(client):
    r = client.get("/api/sensors/latest")
    assert r.status_code == 200
    assert "reading" in r.json()
    r2 = client.get("/api/sensors/recent?limit=10")
    assert r2.status_code == 200
    assert r2.json()["count"] >= 1


def test_thresholds_endpoint(client):
    r = client.get("/api/alerts/thresholds")
    assert r.status_code == 200
    t = r.json()["thresholds"]
    assert t["warning_cm"] == 30.0
    assert t["high_cm"] == 45.0
    assert t["critical_cm"] == 65.0


def test_test_scenario_normal(client):
    r = client.post("/api/test/scenario", json={"scenario": "normal"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_test"] is True
    assert body["final"]["current_risk"] == "normal"


def test_test_scenario_critical(client):
    r = client.post("/api/test/scenario", json={"scenario": "critical"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["final"]["current_risk"] == "critical"
    assert body["final"]["overall_risk"] in ("critical", "high")


def test_test_scenario_unknown_rejected(client):
    r = client.post("/api/test/scenario", json={"scenario": "banana"})
    assert r.status_code == 422  # pydantic validation error


def test_reset_test_data(client):
    client.post("/api/test/scenario", json={"scenario": "warning"})
    r = client.post("/api/test/reset")
    assert r.status_code == 200
    assert "cleared" in r.json()
    # test source should now be empty
    recent = client.get("/api/sensors/recent?source=test").json()
    assert recent["count"] == 0


def test_dashboard_summary(client):
    r = client.get("/api/dashboard/summary?source=esp32")
    assert r.status_code == 200
    body = r.json()
    for key in ("device_status", "latest_reading", "current_risk", "prediction",
                "thresholds", "recent_readings", "alerts", "model_metrics"):
        assert key in body


def test_prediction_endpoints(client):
    assert client.get("/api/predictions/latest").status_code == 200
    assert client.get("/api/predictions/model").status_code == 200
    assert client.get("/api/predictions/history?limit=5").status_code == 200


def test_alert_endpoints(client):
    assert client.get("/api/alerts/history").status_code == 200
    assert client.get("/api/alerts/notification-status").status_code == 200
