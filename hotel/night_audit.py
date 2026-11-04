# type: ignore
"""Night-audit snapshot writer.

One table (NightAudit), one writer (run_night_audit), one row per business date.
See docs/DEVIATIONS.md section 3: the business date is the wall clock by design;
this is the business-objects layer that records a day as audited. No report or
scheduler reads the table yet -- the table exists first, and a reader is added
only once there is something to report.
"""

import logging
from datetime import date

from hotel import db, core


def run_night_audit(completed_by=None):
    """Record today's business date as audited. Idempotent: a second call on the
    same day leaves the existing row untouched and reports it as already done.

    Returns True if a row exists for today (whether just written or already
    present), False if the database is unreachable or the write fails.
    """
    today = core.business_date()
    try:
        with db.get_connection() as conn:
            if conn is None:
                logging.error("Night audit skipped: no database connection.")
                return False
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM NightAudit WHERE AuditDate = ?", (today,)
            )
            if cursor.fetchone() is not None:
                logging.info("Night audit already recorded for %s.", today)
                return True
            cursor.execute(
                "INSERT INTO NightAudit (AuditDate, CompletedBy) VALUES (?, ?)",
                (today, completed_by),
            )
            conn.commit()
            logging.info("Night audit recorded for %s.", today)
            return True
    except Exception as e:
        logging.error(f"Night audit failed: {e}")
        return False


def is_audited(day=None):
    """True if `day` (defaults to today) has a NightAudit row."""
    if day is None:
        day = core.business_date()
    if not isinstance(day, date):
        day = day.date() if hasattr(day, "date") else day
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM NightAudit WHERE AuditDate = ?", (day,))
            return cursor.fetchone() is not None
    except Exception:
        return False
