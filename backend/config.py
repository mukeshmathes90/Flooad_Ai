"""
Central configuration for the Flood Prediction and Early Warning System.

Everything that an operator may want to tune lives here (or can be overridden
through environment variables).  The same values are consumed by:

  * the FastAPI backend (validation, risk engine, alert rules),
  * the ML pipeline (reference height, horizons, sample interval),
  * the tests.

UNITS (documented once, used everywhere)
----------------------------------------
water_level_cm          : centimetres of water above the sensor reference point
ultrasonic_distance_cm  : centimetres measured by HC-SR04 (sensor -> water surface)
rainfall_reading        : raw 12-bit ADC value (0..4095) of the rain sensor
rainfall_intensity      : *calibrated relative* percentage (0..100) derived from
                          the rain ADC. It is NOT a physical mm/hour value unless
                          the sensor has been calibrated in the field.
water_sensor_raw        : raw 12-bit ADC value (0..4095) of the analogue water input
pir_motion              : 0/1 presence detected by the PIR sensor (informational only)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent  # project root

DATA_DIR = BASE_DIR / "data"
MODEL_DIR = BASE_DIR / "ml" / "model"
FRONTEND_DIR = BASE_DIR / "frontend"
LOG_DIR = BASE_DIR / "logs"

HISTORICAL_CSV = Path(os.getenv("FLOOD_HISTORICAL_CSV", str(DATA_DIR / "historical_data.csv")))
SAMPLE_SENSOR_CSV = DATA_DIR / "sample_sensor_data.csv"

MODEL_PATH = Path(os.getenv("FLOOD_MODEL_PATH", str(MODEL_DIR / "flood_model.pkl")))
FEATURE_META_PATH = Path(os.getenv("FLOOD_FEATURE_META", str(MODEL_DIR / "feature_meta.json")))
EVAL_REPORT_PATH = Path(os.getenv("FLOOD_EVAL_REPORT", str(MODEL_DIR / "evaluation_report.json")))

DB_PATH = Path(os.getenv("FLOOD_DB_PATH", str(BASE_DIR / "flood_system.db")))

# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
HOST = os.getenv("FLOOD_HOST", "0.0.0.0")
PORT = int(os.getenv("FLOOD_PORT", "8000"))
DASHBOARD_URL = os.getenv("FLOOD_DASHBOARD_URL", "http://localhost:8000")
API_PREFIX = "/api"

# --------------------------------------------------------------------------- #
# Physical / calibration constants
# --------------------------------------------------------------------------- #
# Distance in cm from the ultrasonic sensor down to the reference "zero" water
# level.  water_level_cm = REFERENCE_HEIGHT_CM - ultrasonic_distance_cm.
# CALIBRATE THIS for the real installation.
REFERENCE_HEIGHT_CM = float(os.getenv("REFERENCE_HEIGHT_CM", "100.0"))

# Physically plausible water-level band (cm). The reference height defines the
# maximum measurable water column for this installation.
MIN_WATER_LEVEL_CM = 0.0
MAX_WATER_LEVEL_CM = REFERENCE_HEIGHT_CM

# HC-SR04 usable range (cm). Readings outside this band are treated as invalid.
ULTRASONIC_MIN_CM = 2.0
ULTRASONIC_MAX_CM = 400.0

# Rain-sensor ADC calibration (12-bit ESP32 ADC).
RAIN_ADC_DRY = int(os.getenv("RAIN_ADC_DRY", "400"))    # dry -> low intensity
RAIN_ADC_WET = int(os.getenv("RAIN_ADC_WET", "3500"))   # wet -> high intensity
ADC_MAX = 4095

# --------------------------------------------------------------------------- #
# Flood-risk thresholds (cm).  PROTOTYPE VALUES - they MUST be calibrated to the
# real installation.  They are NOT universal real-world flood limits.
# --------------------------------------------------------------------------- #
THRESHOLD_WARNING_CM = float(os.getenv("THRESHOLD_WARNING_CM", "30.0"))
THRESHOLD_HIGH_CM = float(os.getenv("THRESHOLD_HIGH_CM", "45.0"))
THRESHOLD_CRITICAL_CM = float(os.getenv("THRESHOLD_CRITICAL_CM", "65.0"))
# Extra documented reference: above this the installation is assumed to overflow.
THRESHOLD_EXTREME_CM = float(os.getenv("THRESHOLD_EXTREME_CM", "80.0"))

# Rate-of-rise escalation: even below the warning level a very fast rise is worth
# a warning.  cm per minute.
RAPID_RISE_WARNING_CM_PER_MIN = float(os.getenv("RAPID_RISE_WARNING_CM_PER_MIN", "3.0"))
RAPID_RISE_HIGH_CM_PER_MIN = float(os.getenv("RAPID_RISE_HIGH_CM_PER_MIN", "6.0"))

# --------------------------------------------------------------------------- #
# Data quality / freshness
# --------------------------------------------------------------------------- #
# A reading older than this is shown as "stale" (dashboard must not present it
# as live).
STALE_AFTER_SECONDS = int(os.getenv("STALE_AFTER_SECONDS", "120"))
# A device not heard from within this window is considered offline.
OFFLINE_AFTER_SECONDS = int(os.getenv("OFFLINE_AFTER_SECONDS", "300"))
# Accept device timestamps at most this far in the future/past (clock skew guard).
MAX_CLOCK_SKEW_SECONDS = int(os.getenv("MAX_CLOCK_SKEW_SECONDS", "600"))

# --------------------------------------------------------------------------- #
# ML / forecasting
# --------------------------------------------------------------------------- #
# Sampling interval assumed when converting horizon minutes -> number of samples.
SAMPLE_INTERVAL_MIN = float(os.getenv("SAMPLE_INTERVAL_MIN", "1.0"))
# Forecast horizons in minutes.
FORECAST_HORIZONS_MIN = [2, 4, 6, 8, 10]
MODEL_VERSION = os.getenv("FLOOD_MODEL_VERSION", "rf-v1")

# Prediction validation limits
# Maximum plausible water-level change per minute (cm/min) used to sanity-check a
# model output against recent measurements.
MAX_REASONABLE_RATE_CM_PER_MIN = float(os.getenv("MAX_REASONABLE_RATE_CM_PER_MIN", "15.0"))
# A model output may not deviate from the latest measurement by more than this.
MAX_PREDICTION_DEVIATION_CM = float(os.getenv("MAX_PREDICTION_DEVIATION_CM", "40.0"))

# Number of most-recent readings the predictor needs to build its feature vector.
# (ml/features.py uses a fixed 10-sample window; keep this >= that value.)
REQUIRED_HISTORY = 10

# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
ALERT_DEDUPE_WINDOW_SECONDS = int(os.getenv("ALERT_DEDUPE_WINDOW_SECONDS", "300"))

# Optional SMS / remote notification.  Leave empty to disable.
# Supported: "" (disabled), "http" (generic webhook), "twilio".
SMS_PROVIDER = os.getenv("FLOOD_SMS_PROVIDER", "").strip().lower()
SMS_HTTP_URL = os.getenv("FLOOD_SMS_HTTP_URL", "")
SMS_HTTP_TOKEN = os.getenv("FLOOD_SMS_HTTP_TOKEN", "")
SMS_TO_NUMBER = os.getenv("FLOOD_SMS_TO", "")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

# --------------------------------------------------------------------------- #
# Email alerts (Gmail SMTP)
# --------------------------------------------------------------------------- #
def parse_email_list(value: str) -> list[str]:
    """Split a comma/semicolon/space separated string into unique addresses."""
    out: list[str] = []
    if not value:
        return out
    for part in re.split(r"[,;\s]+", value):
        addr = part.strip()
        if addr and "@" in addr and addr not in out:
            out.append(addr)
    return out


def _load_email_from_config():
    """Read config.txt.

    Returns (sender, app_password, recipients).

    Supported formats:
      * key=value lines, e.g. ``EMAIL_SENDER=you@gmail.com``,
        ``EMAIL_APP_PASSWORD=abcd efgh ijkl mnop``,
        ``EMAIL_RECIPIENTS=a@x.com,b@y.com``
      * plain lines: line 1 = sender, line 2 = app password (spaces allowed),
        any further lines = recipient addresses.
    """
    cfg_path = BASE_DIR / "config.txt"
    sender = None
    password = None
    recipients: list[str] = []
    if not cfg_path.exists():
        return sender, password, recipients
    try:
        lines = cfg_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return sender, password, recipients

    plain: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s:
            key, val = s.split("=", 1)
            key = key.strip().lower()
            val = val.strip()
            if key in ("email", "smtp_email", "smtp_user", "sender_email", "from_email", "sender"):
                sender = val
            elif key in ("app_password", "password", "smtp_password", "app_pass"):
                password = re.sub(r"\s+", "", val)
            elif key in ("recipients", "recipient", "to", "email_to", "to_email",
                         "alert_email", "alert_emails", "emails"):
                recipients.extend(parse_email_list(val))
            continue
        plain.append(s)

    if sender is None and len(plain) >= 1:
        sender = plain[0]
    if password is None and len(plain) >= 2:
        password = re.sub(r"\s+", "", plain[1])
    for extra in plain[2:]:
        recipients.extend(parse_email_list(extra))

    # de-duplicate, preserving order
    seen = set()
    recipients = [r for r in recipients if not (r in seen or seen.add(r))]
    return sender, password, recipients


_email_from_cfg, _pass_from_cfg, _recipients_from_cfg = _load_email_from_config()

EMAIL_SENDER = os.getenv("EMAIL_SENDER") or os.getenv("SMTP_EMAIL") or _email_from_cfg or ""
EMAIL_APP_PASSWORD = (
    os.getenv("EMAIL_APP_PASSWORD")
    or os.getenv("SMTP_PASSWORD")
    or os.getenv("GMAIL_APP_PASSWORD")
    or _pass_from_cfg
    or ""
)
EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "465"))
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "true").strip().lower() in {"1", "true", "yes", "on"}
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
EMAIL_MOCK = os.getenv("EMAIL_MOCK", "false").strip().lower() in {"1", "true", "yes", "on"}

# --------------------------------------------------------------------------- #
# ESP32 pull mode (optional)
# ---------------------------------------------------------------------------
# By default the ESP32 PUSHes readings to this backend. Alternatively the ESP32
# can run a small web server and the backend PULLs from it. The ESP host is
# entered from the dashboard (persisted in the DB), not here; these are defaults.
ESP_POLL_ENABLED = os.getenv("ESP_POLL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
ESP_POLL_INTERVAL_SECONDS = float(os.getenv("ESP_POLL_INTERVAL_SECONDS", "5"))
ESP_HTTP_PORT = int(os.getenv("ESP_HTTP_PORT", "80"))
ESP_TIMEOUT_SECONDS = float(os.getenv("ESP_TIMEOUT_SECONDS", "4"))

# Recipient list. If none are configured the alert is sent to the sender itself
# (e.g. floodpredictionvcew2428@gmail.com), so single-mailbox setups keep working.
EMAIL_RECIPIENTS = parse_email_list(os.getenv("EMAIL_RECIPIENTS", "")) or list(_recipients_from_cfg)
if not EMAIL_RECIPIENTS and EMAIL_SENDER:
    EMAIL_RECIPIENTS = [EMAIL_SENDER]

# --------------------------------------------------------------------------- #
# Risk levels (single source of truth - ESP32 firmware mirrors these)
# --------------------------------------------------------------------------- #
RISK_LEVELS = ["normal", "warning", "high", "critical", "unknown"]
RISK_RANK = {"unknown": -1, "normal": 0, "warning": 1, "high": 2, "critical": 3}


def risk_from_level_cm(level_cm: float | None) -> str:
    """Map a water level (cm) to a risk category using the configured thresholds."""
    if level_cm is None:
        return "unknown"
    if level_cm >= THRESHOLD_CRITICAL_CM:
        return "critical"
    if level_cm >= THRESHOLD_HIGH_CM:
        return "high"
    if level_cm >= THRESHOLD_WARNING_CM:
        return "warning"
    return "normal"


def thresholds() -> dict:
    """Return the risk thresholds as a dictionary (used by the API/frontend)."""
    return {
        "warning_cm": THRESHOLD_WARNING_CM,
        "high_cm": THRESHOLD_HIGH_CM,
        "critical_cm": THRESHOLD_CRITICAL_CM,
        "extreme_cm": THRESHOLD_EXTREME_CM,
        "reference_height_cm": REFERENCE_HEIGHT_CM,
    }
