"""
ESP32 pull mode.

The default firmware PUSHes readings to this backend. As an alternative the
ESP32 can run a tiny web server that exposes its latest reading, and this
service PULLS from it.

The operator enters the ESP32's IP (obtained from its OLED / serial output)
from the dashboard. It is persisted in the DB ``settings`` table so it survives
restarts. The poller runs in a daemon thread and never blocks ingestion,
predictions, the dashboard or email.

Endpoint expected on the ESP32:
    GET http://<ESP_IP>:<port>/api/readings     -> JSON with sensor fields
    (GET /data and GET / are accepted as fallbacks)

A poll failure is NOT an error condition for the rest of the system - the
status is simply reported back on the dashboard.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from backend import config, database

_ESP_HOST_KEY = "esp_host"
_ESP_PORT_KEY = "esp_port"
_PATHS = ("/api/readings", "/data", "/")

_host_lock = threading.Lock()
_poll_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "host": "",
    "port": config.ESP_HTTP_PORT,
    "enabled": False,
    "polling": False,
    "connected": None,
    "last_ok_at": None,
    "last_error": None,
    "last_error_at": None,
    "polls": 0,
    "failures": 0,
    "readings_ingested": 0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host_re() -> re.Pattern[str]:
    # IPv4, IPv6-ish or a hostname. Conservative - just reject whitespace/URLs.
    return re.compile(r"^[A-Za-z0-9.\-_:%\[\]]+$")


# --------------------------------------------------------------------------- #
# Configuration (persisted in the DB)
# --------------------------------------------------------------------------- #
def get_config() -> dict:
    host = (database.get_setting(_ESP_HOST_KEY, "") or "").strip()
    port_s = database.get_setting(_ESP_PORT_KEY, "") or ""
    try:
        port = int(port_s) if port_s else int(config.ESP_HTTP_PORT)
    except ValueError:
        port = int(config.ESP_HTTP_PORT)
    return {"host": host, "port": port, "enabled": bool(host)}


def set_config(host: str, port: int | None = None) -> dict:
    host = (host or "").strip()
    # Accept a pasted URL and reduce it to a bare host.
    host = re.sub(r"^https?://", "", host).split("/")[0].split(":")[0].strip()
    if host and not _host_re().match(host):
        raise ValueError("Invalid ESP address. Enter the IP shown on the ESP32 OLED, e.g. 192.168.43.55")
    if port is None:
        port = get_config()["port"]
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        raise ValueError("Invalid ESP port. Use a number between 1 and 65535.")
    database.set_setting(_ESP_HOST_KEY, host)
    database.set_setting(_ESP_PORT_KEY, str(port))
    with _host_lock:
        _status["host"] = host
        _status["port"] = port
        _status["enabled"] = bool(host)
    return get_config()


def clear_config() -> dict:
    return set_config("", config.ESP_HTTP_PORT)


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #
def _fetch_json(host: str, port: int) -> dict:
    last_err: Exception | None = None
    for path in _PATHS:
        url = f"http://{host}:{port}{path}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=config.ESP_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
            last_err = ValueError(f"ESP returned a non-object payload from {path}")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise last_err or RuntimeError("no response from ESP32")


def _record_ok(host: str, port: int) -> None:
    with _status_lock:
        _status.update({
            "host": host,
            "port": port,
            "enabled": True,
            "connected": True,
            "last_ok_at": _now_iso(),
            "last_error": None,
            "polls": _status["polls"] + 1,
        })


def _record_failure(host: str, port: int, error: Exception) -> None:
    with _status_lock:
        _status.update({
            "host": host,
            "port": port,
            "enabled": True,
            "connected": False,
            "last_error": str(error)[:200],
            "last_error_at": _now_iso(),
            "polls": _status["polls"] + 1,
            "failures": _status["failures"] + 1,
        })


def poll_once() -> dict:
    """Fetch one reading from the ESP32 and ingest it. Safe to call directly."""
    cfg = get_config()
    host = cfg["host"]
    port = cfg["port"]
    if not host:
        return {"ok": False, "reason": "no ESP address configured"}
    try:
        payload = _fetch_json(host, port)
    except Exception as exc:  # noqa: BLE001
        _record_failure(host, port, exc)
        return {"ok": False, "reason": str(exc)[:200]}

    payload.setdefault("device_id", "esp32-pull")
    payload.setdefault("source", "esp32")
    payload.setdefault("firmware", "esp32-pull")
    # The ESP clock may be unsynced and send an empty timestamp - treat it as
    # absent so the server records its own timestamp instead.
    if not payload.get("timestamp"):
        payload["timestamp"] = None
    try:
        result, errors = _ingest(payload, client_ip=host)
    except Exception as exc:  # noqa: BLE001
        _record_failure(host, port, exc)
        return {"ok": False, "reason": "ingest failed: " + str(exc)[:160]}

    _record_ok(host, port)
    with _status_lock:
        if result.get("valid"):
            _status["readings_ingested"] += 1
    return {"ok": True, "valid": bool(result.get("valid")), "errors": errors,
            "risk": result.get("overall_risk")}


def _ingest(payload: dict, client_ip: str | None):
    from backend.services import sensor_service
    return sensor_service.ingest_reading(payload, client_ip=client_ip)


def _loop() -> None:
    while not _stop_event.is_set():
        cfg = get_config()
        if cfg["host"] and config.ESP_POLL_ENABLED:
            with _status_lock:
                _status["polling"] = True
            try:
                poll_once()
            except Exception:  # noqa: BLE001 - never let the thread die
                pass
        else:
            with _status_lock:
                _status["polling"] = False
        _stop_event.wait(config.ESP_POLL_INTERVAL_SECONDS)


def start() -> None:
    """Start the background poller (idempotent)."""
    global _poll_thread
    if _poll_thread and _poll_thread.is_alive():
        return
    cfg = get_config()
    with _status_lock:
        _status["host"] = cfg["host"]
        _status["port"] = cfg["port"]
        _status["enabled"] = cfg["enabled"]
    _stop_event.clear()
    _poll_thread = threading.Thread(target=_loop, name="esp32-pull", daemon=True)
    _poll_thread.start()


def stop() -> None:
    _stop_event.set()


def status() -> dict:
    with _status_lock:
        out = dict(_status)
    cfg = get_config()
    out["configured"] = cfg["enabled"]
    out["host"] = cfg["host"] or out.get("host") or ""
    out["port"] = cfg["port"]
    out["endpoint"] = f"http://{cfg['host']}:{cfg['port']}/api/readings" if cfg["enabled"] else None
    out["last_ok"] = out.pop("last_ok_at", None)
    out["last_error_at"] = out.get("last_error_at")
    return out


def test_connection(host: str, port: int | None = None) -> dict:
    """One-off reachability check used by the dashboard "Test" button."""
    cfg = get_config()
    host = (host or cfg["host"]).strip()
    if not host:
        return {"ok": False, "reason": "no ESP address supplied"}
    port = int(port or cfg["port"])
    try:
        payload = _fetch_json(host, port)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}
    return {"ok": True, "device_id": payload.get("device_id"),
            "fields": sorted(payload.keys())}
