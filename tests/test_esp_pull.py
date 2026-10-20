"""
Tests for ESP32 pull mode (backend fetches from the ESP32's web server).

Covers: configuration + persistence, validation, URL normalisation, a real
end-to-end pull against a local HTTP server, and failure handling. No real
hardware is required.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend import config, database
from backend.services import esp_client

DEVICE_PAYLOAD = {
    "device_id": "esp32-pull-test",
    "source": "esp32",
    "firmware": "flood_monitor/1.0",
    "water_level_cm": 42.5,
    "ultrasonic_distance_cm": 57.5,
    "rainfall_reading": 1200,
    "rainfall_intensity": 30.0,
    "water_sensor_raw": 900,
    "pir_motion": 0,
    "timestamp": None,
}


@pytest.fixture(autouse=True)
def fresh():
    database.init_db()
    with database.cursor(commit=True) as cur:
        cur.execute("DELETE FROM settings")
        cur.execute("DELETE FROM sensor_readings")
    esp_client.clear_config()
    # esp_client keeps a module-level status dict - reset it so tests are isolated.
    with esp_client._status_lock:
        esp_client._status.update({
            "connected": None, "last_ok_at": None, "last_error": None,
            "last_error_at": None, "polls": 0, "failures": 0, "readings_ingested": 0,
        })
    yield
    esp_client.clear_config()


class _FakeESP(BaseHTTPRequestHandler):
    """Minimal ESP32 stand-in: serves JSON at /api/readings."""
    payload = DEVICE_PAYLOAD
    calls = 0

    def do_GET(self):
        type(self).calls += 1
        if self.path in ("/api/readings", "/data", "/"):
            body = json.dumps(type(self).payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # keep the test output quiet
        pass


@pytest.fixture
def fake_esp():
    server = HTTPServer(("127.0.0.1", 0), _FakeESP)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": "127.0.0.1", "port": port}
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_no_host_by_default():
    cfg = esp_client.get_config()
    assert cfg["host"] == ""
    assert cfg["enabled"] is False


def test_set_config_and_persist():
    esp_client.set_config("192.168.43.55", 80)
    cfg = esp_client.get_config()
    assert cfg == {"host": "192.168.43.55", "port": 80, "enabled": True}
    # Survives a fresh read (i.e. it is persisted in the DB, not memory).
    assert database.get_setting("esp_host") == "192.168.43.55"


def test_url_paste_is_normalised():
    esp_client.set_config("http://192.168.43.77:8080/api/readings")
    assert esp_client.get_config()["host"] == "192.168.43.77"


def test_invalid_host_rejected():
    with pytest.raises(ValueError):
        esp_client.set_config("bad host!")
    with pytest.raises(ValueError):
        esp_client.set_config("host name with spaces")


def test_invalid_port_rejected():
    with pytest.raises(ValueError):
        esp_client.set_config("1.2.3.4", 0)
    with pytest.raises(ValueError):
        esp_client.set_config("1.2.3.4", 70000)


def test_clear_config():
    esp_client.set_config("10.0.0.5")
    esp_client.clear_config()
    assert esp_client.get_config()["host"] == ""


# --------------------------------------------------------------------------- #
# Pulling
# --------------------------------------------------------------------------- #
def test_poll_ingests_reading(fake_esp):
    esp_client.set_config(fake_esp["host"], fake_esp["port"])
    result = esp_client.poll_once()
    assert result["ok"] is True
    latest = database.get_latest_reading(source="esp32")
    assert latest is not None
    assert latest["device_id"] == "esp32-pull-test"
    assert latest["water_level_cm"] == pytest.approx(42.5, abs=0.01)


def test_poll_without_config_is_noop():
    result = esp_client.poll_once()
    assert result["ok"] is False
    assert "no ESP address" in result["reason"]


def test_poll_reports_unreachable_host(fake_esp):
    # Use a port that nothing is listening on.
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    esp_client.set_config("127.0.0.1", dead_port)
    result = esp_client.poll_once()
    assert result["ok"] is False
    assert result["reason"]
    status = esp_client.status()
    assert status["connected"] is False
    assert status["last_error"]


def test_status_reflects_success(fake_esp):
    esp_client.set_config(fake_esp["host"], fake_esp["port"])
    esp_client.poll_once()
    st = esp_client.status()
    assert st["connected"] is True
    assert st["last_ok"]
    assert st["readings_ingested"] == 1
    assert st["endpoint"].startswith("http://")


def test_test_connection_reports_device(fake_esp):
    out = esp_client.test_connection(fake_esp["host"], fake_esp["port"])
    assert out["ok"] is True
    assert out["device_id"] == "esp32-pull-test"


def test_test_connection_failure():
    out = esp_client.test_connection("127.0.0.1", 1)
    assert out["ok"] is False
    assert out["reason"]


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_esp_api_roundtrip():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as c:
        assert c.get("/api/esp/config").json()["host"] == ""
        r = c.post("/api/esp/config", json={"host": "192.168.43.9", "port": 80})
        assert r.status_code == 200
        assert r.json()["config"]["host"] == "192.168.43.9"
        assert c.get("/api/esp/config").json()["host"] == "192.168.43.9"
        s = c.get("/api/dashboard/summary?source=esp32").json()
        assert s["esp32"]["host"] == "192.168.43.9"
        assert s["esp32"]["configured"] is True
        assert c.post("/api/esp/config/clear").json()["ok"] is True


def test_esp_api_rejects_bad_input():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as c:
        r = c.post("/api/esp/config", json={"host": "no spaces allowed here"})
        assert r.status_code == 422
        assert "ESP" in r.json()["detail"]
        r = c.post("/api/esp/config", json={"host": "1.2.3.4", "port": 99999})
        assert r.status_code == 422
