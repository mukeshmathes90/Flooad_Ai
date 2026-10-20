"""Unit tests for validation, risk classification and feature engineering."""

import math

from backend import config
from backend.services import alert_service, validation_service
from ml import features


# --------------------------------------------------------------- validation --
def test_validate_sensor_payload_ok():
    norm, errors = validation_service.validate_sensor_payload({
        "device_id": "d1", "water_level_cm": 12.0, "ultrasonic_distance_cm": 88.0,
        "rainfall_reading": 500, "rainfall_intensity": 3.0, "water_sensor_raw": 800,
    })
    assert errors == []
    assert norm["water_level_cm"] == 12.0


def test_validate_sensor_payload_out_of_range():
    _, errors = validation_service.validate_sensor_payload({
        "water_level_cm": 999.0,
    })
    assert any("physical range" in e for e in errors)


def test_validate_sensor_payload_nan():
    _, errors = validation_service.validate_sensor_payload({
        "water_level_cm": float("nan"),
    })
    assert errors


def test_validate_prediction_rejects_nan():
    v = validation_service.validate_prediction(float("nan"), 10.0, 2)
    assert v["accepted"] is False
    assert v["status"] == "rejected"
    assert "non-finite" in v["reason"]


def test_validate_prediction_rejects_implausible_jump():
    v = validation_service.validate_prediction(90.0, 10.0, 2)
    assert v["accepted"] is False
    assert "implausible jump" in v["reason"]


def test_validate_prediction_constrains_slightly_out_of_range():
    v = validation_service.validate_prediction(105.0, 95.0, 2)
    assert v["accepted"] is True
    assert v["status"] == "constrained"
    assert v["value"] == config.MAX_WATER_LEVEL_CM


def test_validate_prediction_accepts_normal():
    v = validation_service.validate_prediction(12.0, 10.0, 2)
    assert v["accepted"] is True
    assert v["status"] == "accepted"
    assert v["value"] == 12.0


def test_validate_prediction_missing_value():
    v = validation_service.validate_prediction(None, 10.0, 2)
    assert v["status"] == "unavailable"


# --------------------------------------------------------------------- risk --
def test_risk_thresholds():
    assert config.risk_from_level_cm(10) == "normal"
    assert config.risk_from_level_cm(30) == "warning"
    assert config.risk_from_level_cm(45) == "high"
    assert config.risk_from_level_cm(65) == "critical"
    assert config.risk_from_level_cm(None) == "unknown"


def test_current_risk_rapid_rise_escalates():
    assert alert_service.classify_current_risk(10.0, 7.0) == "high"
    assert alert_service.classify_current_risk(10.0, 3.5) == "warning"
    assert alert_service.classify_current_risk(10.0, 0.1) == "normal"


def test_forecast_risk_from_values():
    assert alert_service.classify_forecast_risk([12, 20, 31]) == "warning"
    assert alert_service.classify_forecast_risk([50]) == "high"
    assert alert_service.classify_forecast_risk([]) == "unknown"


def test_higher_risk():
    assert alert_service.higher("normal", "high") == "high"
    assert alert_service.higher("critical", "warning") == "critical"
    assert alert_service.higher("unknown", "normal") == "normal"


# ----------------------------------------------------------------- features --
def _reading(level, rain=300, hour=0.0):
    return {"water_level_cm": level, "rainfall_reading": rain,
            "rainfall_intensity": 0.0, "water_sensor_raw": 500,
            "created_epoch": 1700000000 + hour * 3600}


def test_feature_vector_length_and_names():
    rows = [_reading(10 + i * 0.5) for i in range(features.WINDOW)]
    vec = features.build_feature_vector(rows)
    assert len(vec) == len(features.FEATURE_NAMES) == 16
    assert all(not (isinstance(x, float) and math.isnan(x)) for x in vec)


def test_feature_vector_reflects_trend():
    rising = [_reading(10 + i) for i in range(features.WINDOW)]
    flat = [_reading(10) for i in range(features.WINDOW)]
    v_rise = dict(zip(features.FEATURE_NAMES, features.build_feature_vector(rising)))
    v_flat = dict(zip(features.FEATURE_NAMES, features.build_feature_vector(flat)))
    assert v_rise["wl_rate"] > v_flat["wl_rate"]
    assert v_rise["wl_diff1"] > 0


def test_feature_vector_insufficient_data():
    try:
        features.build_feature_vector([_reading(10)])
    except features.InsufficientData:
        return
    raise AssertionError("expected InsufficientData")
