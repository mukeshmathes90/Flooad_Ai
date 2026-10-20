"""
SQLite persistence layer.

Uses the standard-library ``sqlite3`` module with one short-lived connection per
operation (fast, thread-safe, no external dependency).  All schema creation is
idempotent so the database is created automatically on first start and existing
data is preserved on later starts.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from backend import config


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #
_DB_LOCK = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: Optional[datetime] = None) -> str:
    return (ts or utcnow()).isoformat()


def epoch(ts: Optional[datetime] = None) -> float:
    return (ts or utcnow()).timestamp()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def cursor(commit: bool = False):
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id               TEXT    NOT NULL DEFAULT 'unknown',
    water_level_cm          REAL,
    ultrasonic_distance_cm  REAL,
    rainfall_reading        REAL,
    rainfall_intensity      REAL,
    water_sensor_raw        REAL,
    pir_motion              INTEGER,
    source                  TEXT    NOT NULL DEFAULT 'esp32',
    valid                   INTEGER NOT NULL DEFAULT 1,
    invalid_reason          TEXT,
    device_timestamp        TEXT,
    server_timestamp        TEXT    NOT NULL,
    created_epoch           REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_epoch ON sensor_readings(created_epoch);
CREATE INDEX IF NOT EXISTS idx_readings_source ON sensor_readings(source);

CREATE TABLE IF NOT EXISTS predictions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version           TEXT,
    created_at              TEXT    NOT NULL,
    created_epoch           REAL    NOT NULL,
    input_reading_id        INTEGER,
    horizon_min             REAL,
    raw_predicted_level_cm  REAL,
    predicted_level_cm      REAL,
    accepted                INTEGER NOT NULL DEFAULT 0,
    validation_status       TEXT,
    reason                  TEXT,
    risk_level              TEXT,
    source                  TEXT    NOT NULL DEFAULT 'esp32'
);
CREATE INDEX IF NOT EXISTS idx_predictions_epoch ON predictions(created_epoch);

CREATE TABLE IF NOT EXISTS alerts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT    NOT NULL,
    created_epoch           REAL    NOT NULL,
    device_id               TEXT,
    risk_level              TEXT    NOT NULL,
    origin                  TEXT,
    message                 TEXT,
    dedupe_key              TEXT,
    notified                INTEGER NOT NULL DEFAULT 0,
    notification_status     TEXT    NOT NULL DEFAULT 'not_configured'
);
CREATE INDEX IF NOT EXISTS idx_alerts_epoch ON alerts(created_epoch);

CREATE TABLE IF NOT EXISTS devices (
    device_id               TEXT PRIMARY KEY,
    last_seen_epoch         REAL,
    last_seen_at            TEXT,
    last_ip                 TEXT,
    firmware                TEXT
);

CREATE TABLE IF NOT EXISTS email_notifications (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT NOT NULL,
    created_epoch           REAL NOT NULL,
    alert_id                INTEGER,
    subject                 TEXT,
    html_body               TEXT,
    text_body               TEXT,
    is_test                 INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'pending',
    error                   TEXT,
    sent_at                 TEXT,
    sent_epoch              REAL
);
CREATE INDEX IF NOT EXISTS idx_email_alert ON email_notifications(alert_id);
CREATE INDEX IF NOT EXISTS idx_email_epoch ON email_notifications(created_epoch);

CREATE TABLE IF NOT EXISTS settings (
    key                     TEXT PRIMARY KEY,
    value                   TEXT,
    updated_at              TEXT
);
"""


def init_db() -> None:
    """Create tables/indices if they do not exist. Safe to call on every start."""
    with _DB_LOCK:
        conn = connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# Sensor readings
# --------------------------------------------------------------------------- #
def insert_reading(data: dict) -> int:
    """Insert a fully-normalised reading dict and return its row id."""
    now = utcnow()
    with cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO sensor_readings (
                device_id, water_level_cm, ultrasonic_distance_cm, rainfall_reading,
                rainfall_intensity, water_sensor_raw, pir_motion, source, valid,
                invalid_reason, device_timestamp, server_timestamp, created_epoch
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("device_id", "unknown"),
                data.get("water_level_cm"),
                data.get("ultrasonic_distance_cm"),
                data.get("rainfall_reading"),
                data.get("rainfall_intensity"),
                data.get("water_sensor_raw"),
                1 if data.get("pir_motion") else 0,
                data.get("source", "esp32"),
                1 if data.get("valid", True) else 0,
                data.get("invalid_reason"),
                data.get("device_timestamp"),
                data.get("server_timestamp") or iso(now),
                data.get("created_epoch") or epoch(now),
            ),
        )
        return int(cur.lastrowid)


def get_recent_readings(limit: int = 100, source: Optional[str] = None,
                        only_valid: bool = True) -> list[dict]:
    sql = "SELECT * FROM sensor_readings"
    clauses: list[str] = []
    params: list[Any] = []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if only_valid:
        clauses.append("valid = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_epoch DESC LIMIT ?"
    params.append(limit)
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def get_latest_reading(source: Optional[str] = None,
                       only_valid: bool = True) -> Optional[dict]:
    rows = get_recent_readings(limit=1, source=source, only_valid=only_valid)
    return rows[0] if rows else None


def get_reading_count(source: Optional[str] = None) -> int:
    sql = "SELECT COUNT(*) AS c FROM sensor_readings"
    params: list[Any] = []
    if source is not None:
        sql += " WHERE source = ?"
        params.append(source)
    with cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()["c"])


# --------------------------------------------------------------------------- #
# Predictions
# --------------------------------------------------------------------------- #
def insert_prediction(data: dict) -> int:
    with cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO predictions (
                model_version, created_at, created_epoch, input_reading_id,
                horizon_min, raw_predicted_level_cm, predicted_level_cm, accepted,
                validation_status, reason, risk_level, source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("model_version"),
                data.get("created_at", iso()),
                data.get("created_epoch", epoch()),
                data.get("input_reading_id"),
                data.get("horizon_min"),
                data.get("raw_predicted_level_cm"),
                data.get("predicted_level_cm"),
                1 if data.get("accepted") else 0,
                data.get("validation_status"),
                data.get("reason"),
                data.get("risk_level"),
                data.get("source", "esp32"),
            ),
        )
        return int(cur.lastrowid)


def get_latest_prediction_batch() -> list[dict]:
    """Return all predictions that share the most recent prediction timestamp."""
    with cursor() as cur:
        cur.execute("SELECT MAX(created_epoch) AS e FROM predictions")
        row = cur.fetchone()
        if not row or row["e"] is None:
            return []
        latest = row["e"]
        cur.execute(
            "SELECT * FROM predictions WHERE created_epoch = ? ORDER BY horizon_min ASC",
            (latest,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_prediction_history(limit: int = 200) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM predictions ORDER BY created_epoch DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def insert_alert(data: dict) -> int:
    with cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO alerts (
                created_at, created_epoch, device_id, risk_level, origin, message,
                dedupe_key, notified, notification_status
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("created_at", iso()),
                data.get("created_epoch", epoch()),
                data.get("device_id"),
                data.get("risk_level"),
                data.get("origin"),
                data.get("message"),
                data.get("dedupe_key"),
                1 if data.get("notified") else 0,
                data.get("notification_status", "not_configured"),
            ),
        )
        return int(cur.lastrowid)


def find_alert_by_id(alert_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM alerts WHERE id=?", (alert_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def recent_alert_exists(dedupe_key: str, within_seconds: int) -> bool:
    cutoff = epoch() - within_seconds
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE dedupe_key = ? AND created_epoch >= ?",
            (dedupe_key, cutoff),
        )
        return int(cur.fetchone()["c"]) > 0


def get_alert_history(limit: int = 100) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM alerts ORDER BY created_epoch DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #
def upsert_device(device_id: str, ip: Optional[str] = None,
                  firmware: Optional[str] = None) -> None:
    now_dt = utcnow()
    with cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO devices (device_id, last_seen_epoch, last_seen_at, last_ip, firmware)
            VALUES (?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen_epoch = excluded.last_seen_epoch,
                last_seen_at = excluded.last_seen_at,
                last_ip = COALESCE(excluded.last_ip, devices.last_ip),
                firmware = COALESCE(excluded.firmware, devices.firmware)
            """,
            (device_id, epoch(now_dt), iso(now_dt), ip, firmware),
        )


def get_device(device_id: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Test-data management (never touches live 'esp32' rows)
# --------------------------------------------------------------------------- #
def clear_test_data() -> dict:
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sensor_readings WHERE source = 'test'")
        n_readings = cur.rowcount
        cur.execute("DELETE FROM predictions WHERE source = 'test'")
        n_preds = cur.rowcount
        cur.execute("DELETE FROM alerts WHERE origin LIKE 'test%'")
        n_alerts = cur.rowcount
        # Reset the isolated TEST risk state so the next scenario starts from a
        # clean slate. The LIVE alert_state row is deliberately untouched.
        cur.execute(
            """
            UPDATE alert_state_test
            SET last_valid_risk='normal', last_transition_risk=NULL,
                initialized=0, last_alert_id=NULL, last_email_status=NULL,
                updated_at=?, updated_epoch=?
            WHERE id=1
            """,
            (iso(), epoch()),
        )
    return {"readings": n_readings, "predictions": n_preds, "alerts": n_alerts}


def db_stats() -> dict:
    with cursor() as cur:
        out = {}
        for table in ("sensor_readings", "predictions", "alerts", "devices", "email_notifications"):
            cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
            out[table] = int(cur.fetchone()["c"])
        return out


# --------------------------------------------------------------------------- #
# Email notifications
# --------------------------------------------------------------------------- #
def insert_email_notification(data: dict) -> int:
    now_dt = utcnow()
    with cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO email_notifications (
                created_at, created_epoch, alert_id, subject, html_body, text_body,
                is_test, status, error, sent_at, sent_epoch
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("created_at", iso(now_dt)),
                data.get("created_epoch", epoch(now_dt)),
                data.get("alert_id"),
                data.get("subject"),
                data.get("html_body"),
                data.get("text_body"),
                1 if data.get("is_test") else 0,
                data.get("status", "pending"),
                data.get("error"),
                data.get("sent_at"),
                data.get("sent_epoch"),
            ),
        )
        return int(cur.lastrowid)


def update_email_notification(email_id: int, status: str, error: str | None = None,
                              sent_at: str | None = None, sent_epoch: float | None = None) -> None:
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE email_notifications SET status=?, error=?, sent_at=?, sent_epoch=? WHERE id=?",
            (status, error, sent_at, sent_epoch, email_id),
        )


def get_email_notifications(limit: int = 50) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM email_notifications ORDER BY created_epoch DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Settings (small key/value store - ESP pull config, etc.)
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str | None = None) -> str | None:
    with cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str | None) -> None:
    now_dt = utcnow()
    with cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, iso(now_dt)),
        )


def get_settings(prefix: str | None = None) -> dict:
    with cursor() as cur:
        if prefix:
            cur.execute("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",))
        else:
            cur.execute("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in cur.fetchall()}
