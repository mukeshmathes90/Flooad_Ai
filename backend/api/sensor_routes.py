"""Sensor ingestion and read-out endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from backend import database
from backend.schemas import SensorReadingIn
from backend.services import sensor_service

router = APIRouter(prefix="/api/sensors", tags=["sensors"])


@router.post("/data")
async def post_sensor_data(payload: SensorReadingIn, request: Request):
    """
    Receive a reading from the ESP32 (or the test harness) and run the full
    validate -> store -> risk -> predict pipeline.

    Returns 200 for a physically valid reading, 422 for an invalid one (the
    reading is still stored flagged valid=0 for diagnostics).
    """
    client_ip = request.client.host if request.client else None
    data = payload.model_dump()
    data["device_timestamp"] = payload.device_timestamp()
    result, errors = sensor_service.ingest_reading(data, client_ip=client_ip)

    body = {
        "ok": result["valid"],
        "stored": result["stored"],
        "reading_id": result["reading_id"],
        "valid": result["valid"],
        "invalid_reason": result["invalid_reason"],
        "current_risk": result["current_risk"],
        "forecast_risk": result["forecast_risk"],
        "overall_risk": result["overall_risk"],
        "rate_cm_per_min": result["rate_cm_per_min"],
        "reading": result["reading"],
        "prediction": result["prediction"],
        "alerts": result["alerts"],
    }
    if errors:
        body["errors"] = errors
        return JSONResponse(status_code=422, content=body)
    return body


@router.get("/latest")
async def get_latest(source: Optional[str] = Query(default=None)):
    reading = database.get_latest_reading(source=source)
    status = sensor_service.latest_status(source=source)
    return {"reading": reading, "device_status": status}


@router.get("/recent")
async def get_recent(
    limit: int = Query(default=100, ge=1, le=5000),
    source: Optional[str] = Query(default=None),
    only_valid: bool = Query(default=True),
):
    rows = database.get_recent_readings(limit=limit, source=source, only_valid=only_valid)
    return {"count": len(rows), "readings": rows}


@router.get("/status")
async def get_status(source: Optional[str] = Query(default=None)):
    return sensor_service.latest_status(source=source)
