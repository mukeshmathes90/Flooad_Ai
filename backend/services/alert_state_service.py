"""
Alert state management to enforce startup suppression, transition tracking,
duplicate suppression across restarts and safe retry handling.

Two completely independent state machines exist:

  * ``alert_state``     - the LIVE one, driven by real ESP32 readings.
  * ``alert_state_test``- the TEST one, driven only by the dashboard's
    "TEST ..." scenario buttons.

Keeping them separate is what makes the LIVE/TEST toggle safe: pressing
TEST CRITICAL advances only the test state, so it can never make the live
machine believe a flood started (which would otherwise fire a bogus RECOVERY
email when live data returned to normal).
"""
from __future__ import annotations

import threading
from typing import Optional, Dict, Any, Tuple

from backend import database, config


# Ensure state table exists
_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_valid_risk TEXT NOT NULL DEFAULT 'normal',
    last_transition_risk TEXT,
    last_email_sent_epoch REAL,
    last_email_status TEXT,
    last_alert_id INTEGER,
    initialized INTEGER NOT NULL DEFAULT 0,
    last_transition_at TEXT,
    last_transition_epoch REAL,
    updated_at TEXT NOT NULL,
    updated_epoch REAL NOT NULL
);
INSERT OR IGNORE INTO alert_state (id, last_valid_risk, initialized, updated_at, updated_epoch)
VALUES (1, 'normal', 0, '1970-01-01T00:00:00+00:00', 0.0);
"""

# Mirror of the live table for TEST scenarios. Same columns, no CHECK constraint
# so the row id can be anything (it is always 1).
_TEST_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_state_test (
    id INTEGER PRIMARY KEY,
    last_valid_risk TEXT NOT NULL DEFAULT 'normal',
    last_transition_risk TEXT,
    last_email_sent_epoch REAL,
    last_email_status TEXT,
    last_alert_id INTEGER,
    initialized INTEGER NOT NULL DEFAULT 0,
    last_transition_at TEXT,
    last_transition_epoch REAL,
    updated_at TEXT NOT NULL,
    updated_epoch REAL NOT NULL
);
INSERT OR IGNORE INTO alert_state_test (id, last_valid_risk, initialized, updated_at, updated_epoch)
VALUES (1, 'normal', 0, '1970-01-01T00:00:00+00:00', 0.0);
"""

_LIVE_TABLE = "alert_state"
_TEST_TABLE = "alert_state_test"

_RISK_RANK = config.RISK_RANK


def _epoch() -> float:
    return database.epoch()


def _iso() -> str:
    return database.iso()


def _rank(r: str) -> int:
    return _RISK_RANK.get(r or "unknown", -1)


def _table(is_test: bool) -> str:
    return _TEST_TABLE if is_test else _LIVE_TABLE


def ensure_alert_state_table() -> None:
    with database.cursor(commit=True) as cur:
        cur.executescript(_STATE_SCHEMA)
        cur.executescript(_TEST_STATE_SCHEMA)


class AlertStateManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        ensure_alert_state_table()

    def get_state(self, is_test: bool = False) -> Dict[str, Any]:
        with database.cursor() as cur:
            cur.execute(f"SELECT * FROM {_table(is_test)} WHERE id=1")
            row = cur.fetchone()
            return dict(row) if row else {}

    def _persist_state(self, risk: str, transition_risk: Optional[str],
                       is_test: bool = False) -> None:
        with database.cursor(commit=True) as cur:
            cur.execute(
                f"""
                UPDATE {_table(is_test)}
                SET initialized = 1,
                    last_valid_risk = ?,
                    last_transition_risk = ?,
                    last_transition_at = ?,
                    last_transition_epoch = ?,
                    updated_at = ?,
                    updated_epoch = ?
                WHERE id=1
                """,
                (risk, transition_risk, _iso(), _epoch(), _iso(), _epoch()),
            )

    def is_initialized(self, is_test: bool = False) -> bool:
        s = self.get_state(is_test)
        return bool(s.get("initialized"))

    def initialize_from_db(self, current_valid_risk: str | None = None,
                           is_test: bool = False) -> None:
        """Mark as initialized with current established valid risk (no email)."""
        with self._lock:
            s = self.get_state(is_test)
            risk = current_valid_risk or s.get("last_valid_risk") or "normal"
            if _rank(risk) < 0:
                risk = "normal"
            with database.cursor(commit=True) as cur:
                cur.execute(
                    f"""
                    UPDATE {_table(is_test)}
                    SET initialized = 1,
                        last_valid_risk = ?,
                        updated_at = ?,
                        updated_epoch = ?
                    WHERE id=1
                    """,
                    (risk, _iso(), _epoch()),
                )

    def record_valid_reading(self, detected_risk: str,
                             is_test: bool = False) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Record detected valid risk. Returns (should_notify, transition_type, transition_info).
        Transition types: 'to_alert', 'recovery', 'upgrade', 'downgrade', 'none'.
        Startup suppression: if not initialized, initialize and return no notify.

        ``is_test=True`` routes the reading to the isolated TEST state machine,
        so a scenario can never move the LIVE risk state or trigger a live email.
        """
        if _rank(detected_risk) < 0:
            detected_risk = "unknown"

        with self._lock:
            s = self.get_state(is_test)
            if not s.get("initialized"):
                # First valid reading after startup: establish the current risk
                # state silently. No email is ever sent for the initial state,
                # whatever category it falls in (normal / warning / high / critical).
                seed = "normal" if _rank(detected_risk) < _rank("warning") else detected_risk
                self._persist_state(seed, transition_risk=None, is_test=is_test)
                return False, None, None

            last = s.get("last_valid_risk") or "normal"
            if detected_risk == last:
                return False, None, None

            trans_type = self._classify_transition(last, detected_risk)
            trans_info = {
                "from": last,
                "to": detected_risk,
                "type": trans_type,
                "at": _iso(),
                "epoch": _epoch(),
                "is_test": is_test,
            }
            with database.cursor(commit=True) as cur:
                cur.execute(
                    f"""
                    UPDATE {_table(is_test)}
                    SET last_valid_risk = ?,
                        last_transition_risk = ?,
                        last_transition_at = ?,
                        last_transition_epoch = ?,
                        updated_at = ?,
                        updated_epoch = ?
                    WHERE id=1
                    """,
                    (detected_risk, detected_risk, trans_info["at"], trans_info["epoch"], _iso(), _epoch()),
                )
            if trans_type == "none":
                return False, None, None
            return True, trans_type, trans_info

    def _classify_transition(self, from_r: str, to_r: str) -> str:
        fr = _rank(from_r)
        tr = _rank(to_r)
        if tr < 0:
            return "none"
        # recovery to normal
        if tr == _rank("normal") and fr >= _rank("warning"):
            return "recovery"
        # to alert from normal/unknown
        if fr < _rank("warning") and tr >= _rank("warning"):
            return "to_alert"
        # both alert levels
        if fr >= _rank("warning") and tr >= _rank("warning"):
            if tr > fr:
                return "upgrade"
            if tr < fr:
                return "downgrade"
            return "none"
        # to lower non-normal? not expected, treat as none
        if fr >= _rank("warning") and tr < _rank("warning") and tr > 0:
            return "none"
        return "none"

    def record_email_sent(self, alert_id: Optional[int], status: str,
                          is_test: bool = False) -> None:
        """Book-keep the last notification attempt on the matching state row.

        Live transitions update the live row; test transitions update the test
        row, so a scenario can never overwrite live notification history.
        """
        with self._lock:
            with database.cursor(commit=True) as cur:
                cur.execute(
                    f"""
                    UPDATE {_table(is_test)}
                    SET last_email_sent_epoch = ?,
                        last_email_status = ?,
                        last_alert_id = ?,
                        updated_at = ?,
                        updated_epoch = ?
                    WHERE id=1
                    """,
                    (_epoch(), status, alert_id, _iso(), _epoch()),
                )


alert_state = AlertStateManager()
