"""Pydantic request/response schemas for the FastAPI backend (Pydantic v2)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class SensorReadingIn(BaseModel):
    """Payload posted by the ESP32 to POST /api/sensors/data."""

    model_config = {"extra": "ignore"}

    device_id: str = Field(default="esp32-01", max_length=64)
    # Water column above the sensor reference point (cm).
    water_level_cm: Optional[float] = None
    # Raw HC-SR04 distance (cm). If only this is supplied the backend derives the
    # water level from the configured reference height.
    ultrasonic_distance_cm: Optional[float] = None
    # Raw 12-bit rain-sensor ADC value (0..4095).
    rainfall_reading: Optional[float] = None
    # Calibrated *relative* rainfall percentage (0..100). Not mm/hour.
    rainfall_intensity: Optional[float] = None
    # Raw 12-bit analogue water-sensor ADC value (0..4095).
    water_sensor_raw: Optional[float] = None
    # PIR motion flag - informational only, never flood evidence.
    pir_motion: Optional[int] = None
    # Device clock (ISO-8601). Server time is always recorded separately.
    timestamp: Optional[str] = None
    source: str = Field(default="esp32", max_length=32)
    firmware: Optional[str] = Field(default=None, max_length=64)

    @field_validator("pir_motion", mode="before")
    @classmethod
    def _coerce_pir(cls, v):
        if v is None:
            return None
        if isinstance(v, bool):
            return int(v)
        return v

    def device_timestamp(self) -> Optional[str]:
        return self.timestamp


class TestScenarioIn(BaseModel):
    """Body for POST /api/test/scenario."""

    scenario: str = Field(default="normal")
    device_id: str = Field(default="test-rig-01", max_length=64)

    @field_validator("scenario")
    @classmethod
    def _known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"normal", "warning", "high", "critical"}:
            raise ValueError("scenario must be one of: normal, warning, high, critical")
        return v


class DeviceInfoIn(BaseModel):
    device_id: str
    ip: Optional[str] = None
    firmware: Optional[str] = None


class ESPConfigIn(BaseModel):
    """Body for POST /api/esp/config - the ESP32 address to pull from."""

    host: str = Field(default="", max_length=128)
    port: Optional[int] = Field(default=None, ge=1, le=65535)


class ESPTestIn(BaseModel):
    """Body for POST /api/esp/test."""

    host: Optional[str] = Field(default=None, max_length=128)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
