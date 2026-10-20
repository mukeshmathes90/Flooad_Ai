"""ESP32 pull-mode endpoints: configure / test / status."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.schemas import ESPConfigIn, ESPTestIn
from backend.services import esp_client

router = APIRouter(prefix="/api/esp", tags=["esp32"])


@router.get("/config")
async def get_esp_config():
    return esp_client.get_config()


@router.post("/config")
async def set_esp_config(body: ESPConfigIn):
    try:
        cfg = esp_client.set_config(body.host, body.port)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Nudge the poller so the change takes effect immediately.
    esp_client.start()
    return {"ok": True, "config": cfg, "note": "Saved. The backend will poll the ESP32 automatically."}


@router.post("/config/clear")
async def clear_esp_config():
    return {"ok": True, "config": esp_client.clear_config(),
            "note": "ESP address cleared. The backend will no longer pull from a device."}


@router.get("/status")
async def esp_status():
    return esp_client.status()


@router.post("/test")
async def test_esp(body: ESPTestIn):
    try:
        return esp_client.test_connection(body.host or "", body.port)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
