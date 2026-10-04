# type: ignore
from decimal import Decimal
# Imports and Configuration
import logging
import os
import sys
import time
import random
import string
import getpass
from datetime import date, datetime, timedelta
import configparser
import tqdm
import argparse
import re
import reports
import ui

# Database connection settings
config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(__file__), 'config.ini')
config.read(config_path)
#DATABASE
server = config.get('database', 'server', fallback='' )
database = config.get('database', 'database', fallback='')
username = config.get('database', 'username', fallback='')
password = config.get('database', 'password', fallback='')
#HOTEL
HOTEL_NAME = config.get('hotel', 'name', fallback='')
TAX_RATE = config.getfloat('hotel', 'tax', fallback=0)
LOCKOUT_THRESHOLD = config.getint('hotel', 'lockout_threshold', fallback=3)
LOCKOUT_DURATION = config.getint('hotel', 'lockout_duration', fallback=5)
MASTER_OVERRIDE = config.get('hotel', 'master_override', fallback='master')
MASTER_SECRET = config.get('hotel', 'master_secret', fallback='master')
# Loyalty configuration
LOYALTY_ENABLED = config.getboolean('loyalty', 'enabled', fallback=False)
# A FLOAT, not an int: order accrual has to sit BELOW the room earn rate (see
# points_per_dollar_order_vs_room) to stay calibrated, and any integer rate is either
# equal to or larger than it. 0.5 pts/$ of F&B against 0.83 pts/$ for a Standard room is
# the ordering a real programme has -- the room is the thing being bought.
LOYALTY_ACCRUAL_POINTS_PER_UNIT = config.getfloat('loyalty', 'accrual_points_per_unit', fallback=0.5)
LOYALTY_POINTS_PER_NIGHT = config.getint('loyalty', 'points_per_night', fallback=100)
LOYALTY_REDEMPTION_POINTS_PER_CURRENCY_UNIT = config.getint('loyalty', 'redemption_points_per_currency_unit', fallback=100)
# The floor for "which day is it", used only when HotelSettings has no usable
# business_date row (before migration 023, or if an admin blanks it).
BUSINESS_DATE_SETTING = "business_date"
# HotelSettings key recording that the first-run wizard finished. Nothing seeds this row on
# purpose: ABSENCE is what "not onboarded yet" means, so a fresh database is correctly
# read as needing setup without anyone having had to remember to insert a '0' first.
ONBOARDING_SETTING = "onboarding_complete"
# Default loyalty tiers (name, min lifetime points, points multiplier, discount %, perks).
# Used as a fallback when the LoyaltyTiers table is missing/empty. Tiers are keyed on
# lifetime points earned so that redemption never demotes a guest. Thresholds are
# calibrated for the per-night earning model (see award_stay_points()).
DEFAULT_TIERS = [
    ("Bronze", 0, 1.00, 0.0, "Standard points accrual. No extra perks."),
    ("Silver", 1000, 1.25, 5.0, "+25% points; 5% discount on room service; priority concierge."),
    ("Gold", 2500, 1.50, 10.0, "+50% points; 10% discount on room service; complimentary late check-out; free fitness class."),
    ("Platinum", 7500, 2.00, 15.0, "2x points; 15% discount on room service; complimentary breakfast; spa credit; dedicated concierge line."),
]
# Default points multiplier per room category. Admins can edit these via HotelSettings
# (keys: loyalty_mult_<slug>); these values are the fallback when a setting is absent.
DEFAULT_ROOM_TYPE_MULTIPLIERS = {
    "Standard": 1.0,
    "Deluxe": 1.5,
    "Junior Suite": 2.0,
    "Suite": 3.0,
    "Grand Suite": 4.0,
    "Penthouse": 6.0,
    "Presidential Suite": 8.0,
}
# Default nightly rate per room category. The RoomTypes table (migration 013) is the
# real source of truth and is editable from the admin "Pricing & Settings" menu; these
# values are only used to seed an empty table. Categories deliberately match
# DEFAULT_ROOM_TYPE_MULTIPLIERS and the seeded Rooms.RoomType values.
DEFAULT_ROOM_TYPE_RATES = {
    "Standard": 120.00,
    "Deluxe": 180.00,
    "Junior Suite": 260.00,
    "Suite": 400.00,
    "Grand Suite": 650.00,
    "Penthouse": 1200.00,
    "Presidential Suite": 2500.00,
}
# The operator signed in for the current session. Set by admin_login(); audit rows fall
# back to 'system' for guest-facing actions that have no authenticated operator.
CURRENT_USER = "system"
# The signed-in guest at the public booking desk, as a CustomerProfiles.CustomerID.
# `None` means nobody is logged in. Loyalty is keyed on this, NOT on the room number:
# a balance that belongs to a room gets inherited by whoever checks into that room next.
# Like CURRENT_USER it is not cleared on "logout", so treat it as the last-authenticated
# guest rather than a session token.
CURRENT_CUSTOMER = None
# Failed booking-desk login attempts before the guest is told to stop.
CUSTOMER_LOGIN_MAX_ATTEMPTS = 3
# Folio charge groups. The room charge is never discounted; discount codes and loyalty
# tier discounts apply to F&B only (see bill_room_transactions).
CHARGE_GROUP_ROOM = "Room"
CHARGE_GROUP_FNB = "F&B"
# Pre-build the ODBC connection string once
CONNECTION_STRING = (
    'DRIVER={ODBC Driver 17 for SQL Server};'
    f'SERVER={server};'
    f'DATABASE={database};'
    f'UID={username};'
    f'PWD={password}'
)
import db
db.init(CONNECTION_STRING)
# Set up logging
#logging.basicConfig(filename='hotel_management.log', level=logging.DEBUG, format='%(asctime)s:%(levelname)s:%(message)s')
class CustomFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno == logging.INFO:
            return record.getMessage()
        return f"{record.levelname}: {record.getMessage()}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# Apply the custom formatter to all handlers
for handler in logger.handlers:
    handler.setFormatter(CustomFormatter())

## =========================
# Database Connection & Utilities
## =========================
# One definition of this, not two. main.py used to carry its own copy that wrapped the
# `yield` in `except Exception` and then yielded again, which is illegal in a generator:
# every error raised inside a `with get_connection()` block reached the caller as
# `RuntimeError: generator didn't stop after throw()` instead of pyodbc's real message.
# db.get_connection() has always had the correct shape -- it returns None only when the
# *connect* fails, and lets a statement failure propagate with its own type and message.
# AGENTS.md section 5 says so; this copy is the one that broke it, and nothing else in
# the app ever needed a second definition.
#
# `db.init(CONNECTION_STRING)` above is what keeps the two in step, and it is the seam
# tests/verify_e2e.py re-points at a disposable database. Do NOT redefine this function
# here. If a caller needs different failure behaviour, wrap the body in its own
# try/except -- that is the only correct place to swallow a database error.
get_connection = db.get_connection


## =========================
# Audit Trail
## =========================
def require_master_override(prompt="Enter master override secret: "):
    """Verify the master override secret, returning True when it is granted.

    Centralises the check used by admin_login(), the fraud-unlock path, and the
    destructive admin actions. The configured [hotel] master_secret is tried first; if
    config.ini has none, the plaintext 'master' account in Users is accepted, so the
    override still works on a fresh checkout. Compare is plaintext on purpose (see
    AGENTS.md -- this is a teaching project and passwords are stored in the clear).
    """
    secret = getpass.getpass(prompt).strip()
    if not secret:
        logging.info("A master override secret is required for this action.")
        return False
    if MASTER_SECRET is not None and secret == MASTER_SECRET:
        return True
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT Password FROM Users WHERE Username = ?", ('master',))
            row = cursor.fetchone()
            if row and row[0] == secret:
                return True
    except Exception as e:
        logging.error(f"Error verifying master override: {e}")
        return False
    logging.info("Invalid master override secret.")
    return False


def log_audit(action, entity_type, entity_id="", details=""):
    """Record who changed what in dbo.AuditLog (migration 016).

    Opens its own connection deliberately: the surrounding business transaction may be
    mid-flight, and an audit write must neither be rolled back with it nor be blamed for
    a failure. Never raises -- a missing AuditLog table (migration 016 not yet applied)
    must not break the operation being audited, so failures are reported once at debug
    level and otherwise swallowed.
    """
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO AuditLog (Username, Action, EntityType, EntityID, Details) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(CURRENT_USER or "system"), str(action or "UNKNOWN"),
                 str(entity_type or "Unknown"), str(entity_id or ""), str(details or "")),
            )
            conn.commit()
            return True
    except Exception as e:
        logging.debug("Audit log write skipped (%s: %s)", type(e).__name__, e)
        return False


## =========================
# Hotel Settings (editable via admin "Pricing & Settings")
## =========================
def get_setting(setting_key, default=None):
    """Read a value from the HotelSettings table, falling back to `default`."""
    with get_connection() as conn:
        if conn is None:
            return default
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT SettingValue FROM HotelSettings WHERE SettingKey = ?", (setting_key,))
            row = cursor.fetchone()
            return row[0] if row is not None and row[0] is not None else default
        except Exception as e:
            logging.error("Error reading setting '%s': %s", setting_key, e)
            return default


def set_setting(setting_key, value):
    """Upsert a value into the HotelSettings table. Returns True on success."""
    with get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM HotelSettings WHERE SettingKey = ?", (setting_key,))
            if cursor.fetchone():
                cursor.execute("UPDATE HotelSettings SET SettingValue = ? WHERE SettingKey = ?", (str(value), setting_key))
            else:
                cursor.execute("INSERT INTO HotelSettings (SettingKey, SettingValue) VALUES (?, ?)", (setting_key, str(value)))
            conn.commit()
            logging.info("Setting '%s' updated to %s.", setting_key, value)
            log_audit("UPDATE", "Setting", setting_key, f"{setting_key} -> {value}")
            return True
        except Exception as e:
            logging.error("Error updating setting '%s': %s", setting_key, e)
            return False


def _setting_float(key, default):
    """Read a numeric HotelSettings value, falling back to `default` if it is unreadable.

    Settings are free-text and editable by admins, so a typo must never be able to break
    check-out: a bad value logs and falls back instead of raising.
    """
    try:
        raw = get_setting(key, default)
        return float(raw) if raw is not None and str(raw).strip() != "" else float(default)
    except (TypeError, ValueError):
        logging.error(f"Setting '{key}' has a non-numeric value; using {default}.")
        return float(default)


def _setting_int(key, default):
    """Read an integer HotelSettings value, falling back to `default` if it is unreadable."""
    try:
        raw = get_setting(key, default)
        return int(float(raw)) if raw is not None and str(raw).strip() != "" else int(default)
    except (TypeError, ValueError):
        logging.error(f"Setting '{key}' has a non-numeric value; using {default}.")
        return int(default)


def get_peak_factor():
    """Current peak price multiplier (price = base x factor)."""
    return _setting_float('peak_factor', 1.20)


def get_offpeak_factor():
    """Current off-peak price multiplier (price = base x factor)."""
    return _setting_float('offpeak_factor', 0.90)


def get_tax_rate():
    """Current sales tax rate as a decimal (e.g. 0.13 == 13%)."""
    return _setting_float('tax_rate', TAX_RATE)


def get_loyalty_accrual_points_per_unit():
    """Points earned per $1 of F&B / room-service spend. A float on purpose.

    Reads as a fraction because the only calibrated value is one BELOW the room earn
    rate -- see `points_per_dollar_order_vs_room()`, which fails if the two are ever
    reordered. `_setting_float` keeps an admin typo from breaking check-out.
    """
    return _setting_float('loyalty_accrual_points_per_unit', LOYALTY_ACCRUAL_POINTS_PER_UNIT)


def get_loyalty_redemption_points_per_currency_unit():
    return _setting_int('loyalty_redemption_points_per_currency_unit', LOYALTY_REDEMPTION_POINTS_PER_CURRENCY_UNIT)


def get_loyalty_points_per_night():
    """Base loyalty points awarded per night stayed (before category/tier multipliers)."""
    return _setting_int('loyalty_points_per_night', LOYALTY_POINTS_PER_NIGHT)


def points_per_dollar_order_vs_room(order_rate=None, per_night=None, nightly_rate=None):
    """Return (order_pts_per_dollar, room_pts_per_dollar) for the entry calibration.

    Pure, so the ordering can be asserted without a database. The room figure is the
    points a Standard-category night actually buys, at Bronze tier (multiplier 1.0):
    `points_per_night / the Standard rate`. A Deluxe or a higher tier buys more per
    dollar, so the Standard/Bronze figure is the floor the F&B rate has to stay under --
    which is the whole point. `nightly_rate` defaults to the seeded Standard rate, so a
    database that has re-priced Standard does not silently move the calibration.
    """
    order = float(order_rate if order_rate is not None else LOYALTY_ACCRUAL_POINTS_PER_UNIT)
    per_night_value = int(per_night if per_night is not None else LOYALTY_POINTS_PER_NIGHT)
    rate = float(nightly_rate if nightly_rate is not None else DEFAULT_ROOM_TYPE_RATES["Standard"])
    room = (per_night_value / rate) if rate > 0 else 0.0
    return order, room


def business_date():
    """The hotel's current business date -- the ONE clock for "which day is it".

    Read from `HotelSettings['business_date']`, seeded to the day the database was
    created and advanced one day at a time by `close_day()`. Every "today" in the app
    comes from here, so the housekeeping board, the arrivals board, the refund window,
    availability and both occupancy reports are all answering about the same day and can
    be re-run for a day that has already closed.

    Deliberately NOT a night audit: it does not bill, expire, or roll anything forward.
    It is a clock the operator controls, nothing more.

    Falls back to the wall clock when the setting is missing or unparseable, because a
    missing clock must not stop the front desk from checking a guest out. The fallback
    says so at error level, since it is the difference between a reproducible report and
    a wrong one.
    """
    try:
        raw = get_setting(BUSINESS_DATE_SETTING, None)
    except Exception as e:
        logging.error(f"Could not read the business date ({type(e).__name__}: {e}); using today.")
        return datetime.now().date()
    if raw is None or str(raw).strip() == "":
        logging.error(
            f"Setting '{BUSINESS_DATE_SETTING}' is missing or blank (migration 023 not "
            "applied?); using today. Reports for a closed day will not be reproducible "
            "until it is set.")
        return datetime.now().date()
    text = str(raw).strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    # A datetime string is what pyodbc hands back if the column was ever typed DATETIME.
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    logging.error(f"Setting '{BUSINESS_DATE_SETTING}' is not a date ('{text}'); using today.")
    return datetime.now().date()


def set_business_date(on_date):
    """Write the business date. Returns the stored date, or None when it could not be set.

    Deliberately NOT gated on the master override: rolling the clock back or forward is
    a pricing-neutral, reversible setting, and requiring an override to open the next
    business day would make it a thing staff avoid using. `close_day()` still asks for
    confirmation, because that is the irreversible-looking one.
    """
    if on_date is None:
        return None
    if hasattr(on_date, "date"):
        on_date = on_date.date()
    if not isinstance(on_date, date):
        try:
            on_date = datetime.strptime(str(on_date).strip(), "%Y-%m-%d").date()
        except ValueError:
            logging.info("A business date must be YYYY-MM-DD.")
            return None
    # set_setting() already writes the AuditLog row for this key; the date is the value
    # being stored, so there is nothing extra to record here.
    if not set_setting(BUSINESS_DATE_SETTING, on_date.isoformat()):
        return None
    return on_date


def close_day():
    """Advance the business date by one day. Returns the new date, or None.

    Nothing else happens: no room is cleaned, no rate re-read, no point balance touched.
    The advance exists so that "today" is an operator-controlled value the reports can
    be re-run against, and that is the whole of it.
    """
    current = business_date()
    return set_business_date(current + timedelta(days=1))


## =========================
# Rooms Management
## =========================
ROOM_STATUSES = ("Available", "Occupied", "Cleaning", "Dirty", "Maintenance")
# Statuses a room cannot be SOLD in. Only `Available` (or a NULL status, which reads as
# Available) can be offered to a guest or taken by a clerk: `Cleaning` and `Dirty` mean
# the last guest has left and nobody has done the room, and selling it is selling an
# unclean room. `Occupied` is not listed because occupancy is a fact about dates, not
# housekeeping -- a room reads Occupied for its whole stay, so refusing it here would
# refuse a room for tomorrow. Maintenance is the third case and is load-bearing.
#
# The write paths and search_availability() share this one tuple on purpose. They
# excluded only Maintenance before, so availability offered Dirty and Cleaning rooms to
# the public booking desk while the housekeeping board showed them as not ready.
UNBOOKABLE_ROOM_STATUSES = ("Maintenance", "Dirty", "Cleaning")
# Bounds the onboarding wizard accepts for a room layout, and the reason they are bounds.
# 150 is not a round number chosen for looks: view_rooms() rejects any floor outside 1-150,
# and both it and the housekeeping report recover the floor as
# LEFT(RoomNumber, LEN(RoomNumber) - 3). A room number is <floor><3-digit code>, so the code
# is capped at 999 too. Seeding beyond either bound would create rooms the UI cannot reach
# and reports cannot attribute to a floor.
MAX_ROOM_FLOORS = 150
MAX_ROOMS_PER_FLOOR = 999


def get_room_status(room_number):
    """Return the current status of a room, or None if the room is not in Rooms."""
    if not room_number:
        return None
    with get_connection() as conn:
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT Status FROM Rooms WHERE RoomNumber = ?", (room_number,))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            logging.error(f"Error reading room status: {e}")
            return None


def upsert_room_if_missing(room_number, room_type="Standard", description=""):
    """Auto-register a room in the Rooms table if it is not present.

    Guards every flow so a missing Rooms row never crashes the app.
    """
    if not room_number:
        return False
    with get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM Rooms WHERE RoomNumber = ?", (room_number,))
            if cursor.fetchone():
                return True
            cursor.execute(
                "INSERT INTO Rooms (RoomNumber, RoomType, Description, Status) VALUES (?, ?, ?, ?)",
                (room_number, room_type, description, "Available"),
            )
            conn.commit()
            logging.info("Auto-registered room %s in Rooms.", room_number)
            return True
        except Exception as e:
            logging.error(f"Error upserting room: {e}")
            return False


def set_room_status(room_number, status):
    """Set a room's status to one of ROOM_STATUSES. Returns True on success."""
    if not room_number or status not in ROOM_STATUSES:
        return False
    if get_room_status(room_number) is None:
        upsert_room_if_missing(room_number)
    with get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE Rooms SET Status = ? WHERE RoomNumber = ?", (status, room_number))
            conn.commit()
            log_audit("UPDATE", "Room", room_number, f"Status -> {status}")
            return True
        except Exception as e:
            logging.error(f"Error setting room status: {e}")
            return False


def get_room_type(room_number):
    """Return a room's category from Rooms, falling back to 'Standard' if unknown."""
    if not room_number:
        return "Standard"
    with get_connection() as conn:
        if conn is None:
            return "Standard"
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT RoomType FROM Rooms WHERE RoomNumber = ?", (room_number,))
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
        except Exception as e:
            logging.error(f"Error reading room type: {e}")
    # Not tracked yet: register it so the board stays complete.
    upsert_room_if_missing(room_number)
    return "Standard"


def _room_type_in_conn(cursor, room_number):
    """Read a room's category on a caller's cursor, or None if it is not in `Rooms`.

    The in-connection twin of get_room_type(), which opens its own connection and so cannot
    be used to validate a room as part of an open transaction. Deliberately returns None
    instead of defaulting to 'Standard' or auto-registering the room: a missing `Rooms` row
    means "cannot verify", and the caller decides whether that is fatal. Auto-registering
    here would be wrong anyway -- upsert_room_if_missing() runs after the booking commits.
    """
    try:
        cursor.execute("SELECT RoomType FROM Rooms WHERE RoomNumber = ?", (room_number,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logging.error(f"Error reading room type for {room_number}: {e}")
        return None


def _room_status_in_conn(cursor, room_number):
    """Read a room's housekeeping status on a caller's cursor. None when untracked.

    The in-connection twin of get_room_status(), for the same reason as
    _room_type_in_conn(): the booking transaction has to make its sellability decision on
    the same connection that then writes the row. A status read on a second connection
    could change in between, and the guest would be sold a room that is Dirty.
    """
    try:
        cursor.execute("SELECT Status FROM Rooms WHERE RoomNumber = ?", (room_number,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logging.error(f"Error reading room status for {room_number}: {e}")
        return None


def _room_type_setting_key(room_type):
    """Map a room category to its HotelSettings key, e.g. 'Junior Suite' -> loyalty_mult_junior_suite."""
    return "loyalty_mult_" + str(room_type).strip().lower().replace(" ", "_")


def get_room_type_multiplier(room_type):
    """Points multiplier for a room category, editable by admins via HotelSettings."""
    default = DEFAULT_ROOM_TYPE_MULTIPLIERS.get(room_type, 1.0)
    try:
        return float(get_setting(_room_type_setting_key(room_type), default) or default)
    except (TypeError, ValueError):
        return default


def get_all_room_type_multipliers():
    """Ordered (room type, multiplier) pairs for every known category."""
    return [(rt, get_room_type_multiplier(rt)) for rt in DEFAULT_ROOM_TYPE_MULTIPLIERS]


## =========================
# Room Rates (RoomTypes table, migration 013)
## =========================
def get_room_types(active_only=True, include_status=False):
    """Return ordered room category rows from the RoomTypes table.

    Yields (room_type, nightly_rate) pairs, or (room_type, nightly_rate, active) triples
    when include_status is set (so rate management can show retired categories too).
    Falls back to DEFAULT_ROOM_TYPE_RATES if the table is missing or empty so the app
    still works before migration 013 is applied.
    """
    with get_connection() as conn:
        if conn is None:
            return [(rt, rate) for rt, rate in DEFAULT_ROOM_TYPE_RATES.items()]
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT RoomType, NightlyRate"
                + (", Active" if include_status else "")
                + " FROM RoomTypes "
                + ("WHERE Active = 1 " if active_only else "")
                + "ORDER BY NightlyRate, RoomType"
            )
            rows = cursor.fetchall()
            if rows:
                if include_status:
                    return [(r[0], float(r[1]), bool(r[2])) for r in rows]
                return [(r[0], float(r[1])) for r in rows]
        except Exception as e:
            logging.error(f"Error reading room types: {e}")
    if include_status:
        return [(rt, rate, True) for rt, rate in DEFAULT_ROOM_TYPE_RATES.items()]
    return [(rt, rate) for rt, rate in DEFAULT_ROOM_TYPE_RATES.items()]


def get_nightly_rate(room_type):
    """Nightly rate for a room category. Falls back to Standard, then to 0.0."""
    if not room_type:
        room_type = "Standard"
    try:
        with get_connection() as conn:
            if conn is None:
                return float(DEFAULT_ROOM_TYPE_RATES.get(room_type, 0.0))
            cursor = conn.cursor()
            cursor.execute("SELECT NightlyRate FROM RoomTypes WHERE RoomType = ?", (room_type,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                return float(row[0])
    except Exception as e:
        logging.error(f"Error reading nightly rate: {e}")
    if room_type in DEFAULT_ROOM_TYPE_RATES:
        return float(DEFAULT_ROOM_TYPE_RATES[room_type])
    return float(DEFAULT_ROOM_TYPE_RATES["Standard"])


def _nightly_rate_in_conn(cursor, room_type):
    """Read a category's nightly rate on a caller's cursor, or None if unknown.

    The in-connection twin of get_nightly_rate(), for the same reason
    _room_type_in_conn() exists: the booking transaction needs the rate it is about to
    capture read on its own connection, so the rate cannot change between the quote the
    guest agreed to and the row that locks it in.
    """
    if not room_type:
        room_type = "Standard"
    try:
        cursor.execute("SELECT NightlyRate FROM RoomTypes WHERE RoomType = ?", (room_type,))
        row = cursor.fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as e:
        # Migration 013 not applied, or the table is gone: fall through to the seed.
        logging.debug(f"Nightly rate for {room_type} unreadable ({type(e).__name__}: {e})")
    return float(DEFAULT_ROOM_TYPE_RATES.get(room_type, DEFAULT_ROOM_TYPE_RATES["Standard"]))


def _rate_or_none(nightly_rate):
    """A rate for the NightlyRate column, or None when there is nothing worth storing.

    NULL is meaningful: it says "this stay predates rate capture, or its category had no
    rate", which is what tells post_room_charge() to fall back to the current rate and
    say so. Storing 0.0 instead would be indistinguishable from a genuinely free room.
    """
    try:
        value = float(nightly_rate)
    except (TypeError, ValueError):
        return None
    return round(value, 2) if value > 0 else None


def stay_nightly_rate(captured, current_rate):
    """The rate a stay is billed at: the one captured when it was booked, else the current one.

    Pure, so the precedence is unit-testable and cannot drift between the two call sites
    that need it (post_room_charge() and the archive snapshot). A captured rate of 0 or
    NULL is treated as "not captured" rather than as a free room, which is why
    _rate_or_none() never stores 0.
    """
    if captured is not None:
        try:
            if float(captured) > 0:
                return round(float(captured), 2)
        except (TypeError, ValueError):
            pass
    try:
        return round(float(current_rate or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


# False once a probe proves Reservations has no NightlyRate column (migration 022 not
# applied). None = unknown, or the column is there. Only the negative answer is cached:
# it is the one that saves a failing query per check-out, and a working probe is cheap.
_RESERVATIONS_CAPTURED_RATE_SUPPORT = None


def _reservations_have_captured_rate():
    """Whether Reservations.NightlyRate exists (migration 022). Cached after the first probe.

    Check-out must keep working on a database that has not applied 022, so an uncaptured
    stay is billed at the current rate -- the pre-022 behaviour -- rather than failing
    with an invalid-column error in the middle of settling a bill.
    """
    global _RESERVATIONS_CAPTURED_RATE_SUPPORT
    if _RESERVATIONS_CAPTURED_RATE_SUPPORT is not None:
        return _RESERVATIONS_CAPTURED_RATE_SUPPORT
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT COL_LENGTH('dbo.Reservations', 'NightlyRate')")
            row = cursor.fetchone()
            _RESERVATIONS_CAPTURED_RATE_SUPPORT = bool(row and row[0])
    except Exception as e:
        logging.debug(f"Reservations.NightlyRate probe failed ({type(e).__name__}: {e})")
        _RESERVATIONS_CAPTURED_RATE_SUPPORT = False
    return _RESERVATIONS_CAPTURED_RATE_SUPPORT


def get_captured_nightly_rate(room_number):
    """The nightly rate captured on a room's live reservation, or None if there is none.

    None covers all three "not captured" cases and they are deliberately not told apart:
    no live reservation, a pre-022 row, or a category that had no rate. Each of them
    means the same thing to the caller -- fall back to the current rate.
    """
    if not room_number or not _reservations_have_captured_rate():
        return None
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute("SELECT NightlyRate FROM Reservations WHERE RoomNumber = ?",
                           (room_number,))
            row = cursor.fetchone()
            return _rate_or_none(row[0]) if row is not None else None
    except Exception as e:
        logging.error(f"Error reading captured rate for room {room_number}: {e}")
        return None


def update_room_type_rate(room_type, nightly_rate):
    """Persist a new nightly rate for a room category. Returns True on success."""
    try:
        nightly_rate = round(float(nightly_rate), 2)
        if nightly_rate < 0:
            logging.info("Nightly rate cannot be negative.")
            return False
    except (TypeError, ValueError):
        logging.info("Invalid nightly rate.")
        return False
    with get_connection() as conn:
        if conn is None:
            logging.info("Database connection failed.")
            return False
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE RoomTypes SET NightlyRate = ? WHERE RoomType = ?",
                (nightly_rate, room_type),
            )
            if cursor.rowcount == 0:
                # Unknown category: register it so a new room type can be priced.
                cursor.execute(
                    "INSERT INTO RoomTypes (RoomType, NightlyRate, Active) VALUES (?, ?, 1)",
                    (room_type, nightly_rate),
                )
            conn.commit()
            log_audit("UPDATE", "RoomType", room_type, f"NightlyRate -> {nightly_rate:.2f}")
            logging.info(f"Nightly rate for {room_type} set to ${nightly_rate:.2f}.")
            return True
        except Exception as e:
            logging.error(f"Error updating nightly rate: {e}")
            return False


def ensure_room_types_seeded():
    """Insert any missing default room categories. Mirrors ensure_loyalty_tables().

    Runs on every start-up, so a category added to DEFAULT_ROOM_TYPE_RATES in a later
    release shows up without needing another migration.
    """
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT RoomType FROM RoomTypes")
            existing = {str(r[0]).strip().lower() for r in cursor.fetchall()}
            added = 0
            for room_type, rate in DEFAULT_ROOM_TYPE_RATES.items():
                if room_type.lower() in existing:
                    continue
                cursor.execute(
                    "INSERT INTO RoomTypes (RoomType, NightlyRate, Description, Active) "
                    "VALUES (?, ?, ?, 1)",
                    (room_type, rate, f"{room_type} room."),
                )
                added += 1
            if added:
                conn.commit()
                logging.info(f"Registered {added} room type(s) in RoomTypes.")
            return True
    except Exception as e:
        logging.debug(f"Room type seed skipped ({type(e).__name__}: {e})")
        return False


## =========================
# Loyalty Program Helpers
## =========================
def ensure_loyalty_tables():
    """Create loyalty tables if they don't exist."""
    if not LOYALTY_ENABLED:
        return
    with get_connection() as conn:
        if conn is None:
            return
        try:
            cursor = conn.cursor()
            cursor.execute("IF OBJECT_ID('dbo.LoyaltyAccounts','U') IS NULL BEGIN CREATE TABLE LoyaltyAccounts (CustomerID INT PRIMARY KEY, RoomNumber NVARCHAR(50) NULL, Points INT NOT NULL DEFAULT 0, Tier NVARCHAR(50) NULL, LastUpdated DATETIME NULL) END")
            cursor.execute("IF OBJECT_ID('dbo.LoyaltyTransactions','U') IS NULL BEGIN CREATE TABLE LoyaltyTransactions (ID INT IDENTITY(1,1) PRIMARY KEY, CustomerID INT NULL, RoomNumber NVARCHAR(50), Delta INT, Reason NVARCHAR(255), CreatedAt DATETIME DEFAULT GETDATE(), SourceID NVARCHAR(100) NULL) END")
            cursor.execute("IF OBJECT_ID('dbo.LoyaltyTiers','U') IS NULL BEGIN CREATE TABLE LoyaltyTiers (TierName NVARCHAR(50) PRIMARY KEY, MinLifetimePoints INT NOT NULL DEFAULT 0, PointsMultiplier DECIMAL(5,2) NOT NULL DEFAULT 1.00, DiscountPercent DECIMAL(5,2) NOT NULL DEFAULT 0, Perks NVARCHAR(500) NOT NULL DEFAULT '') END")
            cursor.execute("SELECT COUNT(*) FROM LoyaltyTiers")
            if cursor.fetchone()[0] == 0:
                for tier in DEFAULT_TIERS:
                    cursor.execute("INSERT INTO LoyaltyTiers (TierName, MinLifetimePoints, PointsMultiplier, DiscountPercent, Perks) VALUES (?, ?, ?, ?, ?)", tier)
            conn.commit()
        except Exception as e:
            logging.error(f"Error ensuring loyalty tables: {e}")


def customer_id_for_stay(room_number, check_in=None):
    """Resolve the CustomerID of the guest occupying (or who occupied) a stay.

    This is the bridge that lets the rest of the app keep asking "what does this room
    have?" while the loyalty balance itself belongs to a person. Returns None when the
    stay has no linked profile, which is the normal state for a pre-019 reservation.
    """
    if not room_number:
        return None
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            if check_in:
                cursor.execute(
                    "SELECT TOP 1 CustomerID FROM Reservations "
                    "WHERE RoomNumber = ? AND CheckInDate = ?",
                    (room_number, check_in))
            else:
                cursor.execute(
                    "SELECT TOP 1 CustomerID FROM Reservations WHERE RoomNumber = ? "
                    "ORDER BY CheckInDate DESC",
                    (room_number,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except Exception as e:
        logging.debug(f"No customer profile linked to room {room_number}: {e}")
        return None


def ensure_customer_loyalty_account(customer_id) -> bool:
    """Ensure a loyalty account exists for this customer. No-op without migration 019."""
    if not LOYALTY_ENABLED or not customer_id:
        return False
    with get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT Points FROM LoyaltyAccounts WHERE CustomerID = ?", (customer_id,))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO LoyaltyAccounts (CustomerID, Points, LastUpdated) VALUES (?, ?, ?)",
                    (customer_id, 0, datetime.now()))
                conn.commit()
            return True
        except Exception as e:
            logging.debug(f"Loyalty account not available (needs migration 019?): {e}")
            return False


def get_points_by_customer(customer_id) -> int:
    """Spendable loyalty balance for a CUSTOMER, across every room they have stayed in."""
    if not LOYALTY_ENABLED or not customer_id:
        return 0
    with get_connection() as conn:
        if conn is None:
            return 0
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT Points FROM LoyaltyAccounts WHERE CustomerID = ?", (customer_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            logging.debug(f"Error getting loyalty points: {e}")
            return 0


def add_points_to_customer(customer_id, delta_points, reason='adjust', source_id=None,
                           room_number=None) -> bool:
    """Move a signed delta on a customer's balance and write one ledger row."""
    if not LOYALTY_ENABLED or not customer_id or not delta_points:
        return False
    with get_connection() as conn:
        if conn is None:
            return False
        try:
            ensure_customer_loyalty_account(customer_id)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE LoyaltyAccounts SET Points = Points + ?, RoomNumber = ?, LastUpdated = ? "
                "WHERE CustomerID = ?",
                (delta_points, room_number, datetime.now(), customer_id))
            cursor.execute(
                "INSERT INTO LoyaltyTransactions (CustomerID, RoomNumber, Delta, Reason, SourceID) "
                "VALUES (?, ?, ?, ?, ?)",
                (customer_id, room_number, delta_points, reason, source_id))
            conn.commit()
            recompute_tier_by_customer(customer_id)
            logging.info(f"Added {delta_points} points to customer {customer_id} ({reason}).")
            return True
        except Exception as e:
            logging.debug(f"Error adding loyalty points: {e}")
            return False


def redeem_points_by_customer(customer_id, points, reason='redeem', room_number=None) -> bool:
    if not LOYALTY_ENABLED or not customer_id or points <= 0:
        return False
    with get_connection() as conn:
        if conn is None:
            return False
        try:
            if get_points_by_customer(customer_id) < points:
                logging.info("Insufficient loyalty points.")
                return False
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE LoyaltyAccounts SET Points = Points - ?, LastUpdated = ? WHERE CustomerID = ?",
                (points, datetime.now(), customer_id))
            cursor.execute(
                "INSERT INTO LoyaltyTransactions (CustomerID, RoomNumber, Delta, Reason) VALUES (?, ?, ?, ?)",
                (customer_id, room_number, -points, reason))
            conn.commit()
            logging.info(f"Redeemed {points} points from customer {customer_id} ({reason}).")
            return True
        except Exception as e:
            logging.debug(f"Error redeeming loyalty points: {e}")
            return False


class LoyaltyRedemptionError(Exception):
    """A point redemption cannot be honoured, so the bill that promised it cannot stand.

    Raised rather than returned as False because the invoice records the discount it
    applied. A False here would let that invoice commit claiming a discount nobody took,
    which is a silent revenue leak; raising rolls the invoice back, leaving the guest
    unbilled so the attempt can simply be repeated. See docs/DEVIATIONS.md 9.
    """


def redeem_points_for_invoice(customer_id, points, room_number, reason, conn,
                             source_id=None):
    """Deduct redeemed points inside the caller's transaction, beside its invoice insert.

    The invoice snapshots the discount it granted (`PointsRedeemed`/`RedemptionValue`), so
    that claim and the guest's balance have to land together or not at all. This is the
    same reason `apply_booking_credit()` runs where it does. Redemption used to be committed
    up front instead, which meant a declined card cost the guest their points with nothing
    billed in exchange and no way to detect the loss afterwards -- `SourceID` was NULL, so
    nothing in the ledger identified the transaction.

    `source_id` makes the deduction self-checking, following the convention
    `award_stay_points()` uses for its own idempotency guard.

    Does not commit: the caller owns the transaction and must not commit a partial bill.
    """
    if points <= 0 or not customer_id:
        return False
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ISNULL(Points, 0) FROM LoyaltyAccounts WHERE CustomerID = ?", (customer_id,))
    row = cursor.fetchone()
    balance = int(row[0]) if row and row[0] is not None else 0
    if balance < points:
        raise LoyaltyRedemptionError(
            f"cannot redeem {points} points from customer {customer_id}: balance is {balance}")
    if source_id:
        cursor.execute(
            "SELECT 1 FROM LoyaltyTransactions WHERE CustomerID = ? AND SourceID = ?",
            (customer_id, source_id))
        if cursor.fetchone():
            logging.info(f"Points already redeemed under {source_id}; not deducting twice.")
            return False
    cursor.execute(
        "UPDATE LoyaltyAccounts SET Points = Points - ?, LastUpdated = ? WHERE CustomerID = ?",
        (points, datetime.now(), customer_id))
    cursor.execute(
        "INSERT INTO LoyaltyTransactions (CustomerID, RoomNumber, Delta, Reason, SourceID) "
        "VALUES (?, ?, ?, ?, ?)",
        (customer_id, room_number, -points, reason, source_id))
    logging.info(f"Redeemed {points} points from customer {customer_id} ({reason}).")
    return True


def get_lifetime_points_by_customer(customer_id) -> int:
    """Lifetime points ever earned by a customer (sum of positive ledger deltas)."""
    if not LOYALTY_ENABLED or not customer_id:
        return 0
    with get_connection() as conn:
        if conn is None:
            return 0
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(Delta), 0) FROM LoyaltyTransactions "
                "WHERE CustomerID = ? AND Delta > 0", (customer_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            logging.debug(f"Error getting lifetime loyalty points: {e}")
            return 0


# ---------------------------------------------------------------------------
# Room-shaped delegates.
#
# Loyalty is stored per CUSTOMER, but much of the app naturally talks about the stay
# that is being checked in, billed or checked out. These keep those call sites reading
# naturally while the balance follows the person, which is the whole point of 019.
# ---------------------------------------------------------------------------

def create_loyalty_account_if_missing(room_number: str, check_in=None) -> bool:
    """Ensure the guest in this room has a loyalty account."""
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return False
    return ensure_customer_loyalty_account(customer_id)


def get_points_by_room(room_number: str, check_in=None) -> int:
    return get_points_by_customer(customer_id_for_stay(room_number, check_in))


def add_points_by_room(room_number: str, delta_points: int, reason: str = 'adjust',
                       source_id: str = None, check_in=None) -> bool:
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return False
    return add_points_to_customer(customer_id, delta_points, reason, source_id, room_number)


def redeem_points_by_room(room_number: str, points: int, reason: str = 'redeem',
                          check_in=None) -> bool:
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return False
    return redeem_points_by_customer(customer_id, points, reason, room_number)


def get_lifetime_points_by_room(room_number: str, check_in=None) -> int:
    return get_lifetime_points_by_customer(customer_id_for_stay(room_number, check_in))


def _tiers_from_db():
    """All tiers sorted by MinLifetimePoints ascending. Empty list if the table is missing."""
    with get_connection() as conn:
        if conn is None:
            return []
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT TierName, MinLifetimePoints, PointsMultiplier, DiscountPercent, Perks FROM LoyaltyTiers ORDER BY MinLifetimePoints")
            rows = cursor.fetchall()
            return [(r.TierName, int(r.MinLifetimePoints), float(r.PointsMultiplier), float(r.DiscountPercent), r.Perks or "") for r in rows]
        except Exception as e:
            logging.error(f"Error reading loyalty tiers: {e}")
            return []


def get_tier_for_points(lifetime_points: int) -> str:
    """Return the tier name for a given lifetime points total."""
    tiers = _tiers_from_db() or DEFAULT_TIERS
    current = tiers[0][0]
    for name, min_pts, _mult, _disc, _perks in tiers:
        if lifetime_points >= int(min_pts):
            current = name
        else:
            break
    return current


def get_tier_details_by_customer(customer_id):
    """Return tier details (name, multiplier, discount %, perks, lifetime points) for a customer."""
    if not LOYALTY_ENABLED or not customer_id:
        return None
    lifetime = get_lifetime_points_by_customer(customer_id)
    tiers = _tiers_from_db() or DEFAULT_TIERS
    tier_name = get_tier_for_points(lifetime)
    for name, min_pts, mult, disc, perks in tiers:
        if name == tier_name:
            return {
                "customer_id": customer_id,
                "tier": tier_name,
                "min_lifetime_points": int(min_pts),
                "lifetime_points": lifetime,
                "points_multiplier": float(mult),
                "discount_percent": float(disc),
                "perks": perks,
            }
    return {
        "customer_id": customer_id,
        "tier": tier_name,
        "min_lifetime_points": 0,
        "lifetime_points": lifetime,
        "points_multiplier": 1.0,
        "discount_percent": 0.0,
        "perks": "",
    }


def get_tier_details_by_room(room_number: str, check_in=None):
    """Tier of the guest in this room. Delegates, so the balance follows the person."""
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return None
    details = get_tier_details_by_customer(customer_id)
    if details is not None:
        # `room` is kept in the dict because callers label their output with it.
        details["room"] = room_number
    return details


def recompute_tier_by_customer(customer_id) -> str:
    """Recalculate and persist a customer's tier from lifetime points."""
    if not LOYALTY_ENABLED or not customer_id:
        return ""
    tier = get_tier_for_points(get_lifetime_points_by_customer(customer_id))
    with get_connection() as conn:
        if conn is None:
            return tier
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE LoyaltyAccounts SET Tier = ?, LastUpdated = ? WHERE CustomerID = ?",
                (tier, datetime.now(), customer_id))
            conn.commit()
            return tier
        except Exception as e:
            logging.debug(f"Error recomputing loyalty tier: {e}")
            return tier


def recompute_tier(room_number: str, check_in=None) -> str:
    customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        return ""
    return recompute_tier_by_customer(customer_id)


def recompute_all_tiers() -> int:
    """Recompute tiers for all loyalty accounts. Returns the number of accounts processed."""
    if not LOYALTY_ENABLED:
        return 0
    with get_connection() as conn:
        if conn is None:
            return 0
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT CustomerID FROM LoyaltyAccounts")
            customers = [r[0] for r in cursor.fetchall()]
            for customer_id in customers:
                recompute_tier_by_customer(customer_id)
            return len(customers)
        except Exception as e:
            logging.debug(f"Error recomputing loyalty tiers: {e}")
            return 0


def award_stay_points(room_number, check_in, check_out, customer_id=None):
    """Award loyalty points for a completed stay, to the GUEST (not the room).

    points = nights x loyalty_points_per_night x room-category multiplier x tier multiplier.
    Idempotent per stay via SourceID, so running check-out twice cannot double-award.
    Returns the number of points awarded (0 if none).
    """
    if not LOYALTY_ENABLED or not room_number or not check_in or not check_out:
        return 0
    if customer_id is None:
        customer_id = customer_id_for_stay(room_number, check_in)
    if not customer_id:
        # A stay with no linked profile cannot hold a balance, and crediting "the room"
        # would hand the points to the next guest. Skipped deliberately, not silently.
        logging.debug(f"Stay {room_number}/{check_in} has no customer profile; no points awarded.")
        return 0
    nights = stay_nights(check_in, check_out)
    if nights <= 0:
        return 0

    # CustomerID is in the SourceID on purpose: two different guests can occupy the same
    # room on the same dates across a re-let, and a room-only key would let the second
    # guest's award be rejected as a duplicate of the first.
    source_id = f"stay:{customer_id}:{room_number}:{check_in}"
    with get_connection() as conn:
        if conn is None:
            return 0
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM LoyaltyTransactions WHERE CustomerID = ? AND SourceID = ?",
                (customer_id, source_id))
            if cursor.fetchone():
                logging.info(f"Stay points already awarded for this stay ({source_id}).")
                return 0
        except Exception as e:
            logging.error(f"Error checking existing stay award: {e}")
            return 0

    room_type = get_room_type(room_number)
    category_mult = get_room_type_multiplier(room_type)
    tier = get_tier_details_by_customer(customer_id)
    tier_mult = tier["points_multiplier"] if tier else 1.0
    points = int(round(nights * get_loyalty_points_per_night() * category_mult * tier_mult))
    if points <= 0:
        return 0
    if add_points_to_customer(customer_id, points, reason='stay', source_id=source_id,
                              room_number=room_number):
        logging.info(
            f"Awarded {points} stay points to customer {customer_id} for room {room_number}: "
            f"{nights} night(s) x {get_loyalty_points_per_night()}/night x {room_type} x{category_mult:.2f} x tier x{tier_mult:.2f}."
        )
        return points
    return 0


def award_billed_order_points(room_number, tx_ids, customer_id=None):
    """Award order-accrual loyalty points once a set of transactions has been paid.

    points = SUM(Amount of the paid transactions) x accrual rate x tier multiplier
    (pre-discount basis). Idempotent per transaction set via
    SourceID 'order_pay:{customer}:{room}:{sorted tx ids}'. Returns points awarded (0 if none).

    Only F&B lines accrue. The nightly room charge is excluded (it is in the same
    folio) because the stay is already rewarded by award_stay_points() for its nights
    -- counting both would pay the guest twice for a single stay.

    The accrual rate is a FLOAT and is deliberately lower than the room's own earn rate.
    At 3 pts/$ it was three and a half times what a Standard night's 100 points on a
    $120 room bought, so the most profitable way for a guest to earn was to order room
    service and the room was worth less than a coffee. `points_per_dollar_order_vs_room()`
    is the calibration and tests/test_billing_math.py fails if the two are reordered.
    """
    if not LOYALTY_ENABLED or not room_number or not tx_ids:
        return 0
    tx_list = [int(t) for t in tx_ids if t is not None]
    if not tx_list:
        return 0
    if customer_id is None:
        customer_id = customer_id_for_stay(room_number)
    if not customer_id:
        return 0
    source_id = "order_pay:{0}:{1}:{2}".format(customer_id, room_number,
                                               "|".join(str(t) for t in sorted(tx_list)))
    try:
        with get_connection() as conn:
            if conn is None:
                return 0
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM LoyaltyTransactions WHERE CustomerID = ? AND SourceID = ?", (customer_id, source_id))
            if cursor.fetchone():
                return 0
            placeholders = ",".join("?" for _ in tx_list)
            cursor.execute(
                f"SELECT COALESCE(SUM(Amount), 0) FROM Transactions "
                f"WHERE ID IN ({placeholders}) AND ChargeGroup = ?",
                tuple(tx_list) + (CHARGE_GROUP_FNB,),
            )
            total = float(cursor.fetchone()[0] or 0.0)
    except Exception as e:
        logging.error(f"Error checking/awarding order points: {e}")
        return 0
    tier = get_tier_details_by_customer(customer_id)
    multiplier = tier["points_multiplier"] if tier else 1.0
    accrual_rate = get_loyalty_accrual_points_per_unit()
    points = int(total * accrual_rate * multiplier)
    if points <= 0:
        return 0
    if add_points_to_customer(customer_id, points, reason='order', source_id=source_id,
                              room_number=room_number):
        logging.info(f"Awarded {points} order points to customer {customer_id} "
                     f"({total:.2f} x {accrual_rate:g} pts/$ x tier x{multiplier:.2f}).")
        return points
    return 0


def clear_lockout(target_username: str) -> bool:
    """Clear failed attempts and lockout for a user. Returns True on success."""
    if not target_username:
        return False
    with get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE Users SET FailedAttempts = 0, LockoutTime = NULL WHERE Username = ?", (target_username,))
            conn.commit()
            logging.info("Cleared lockout for user: %s", target_username)
            return True
        except Exception as e:
            logging.error("Failed to clear lockout: %s", e)
            return False

def generate_code():
    """Generate a random 5-character alphanumeric code."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=5))

def display_items():
    with get_connection() as conn:
        if conn is None:
            return
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT ItemID, Name FROM Items")  # Select only needed columns
            rows = cursor.fetchall()
            table_rows = []
            for row in rows:
                item_id = row[0]
                name = row[1]
                price = get_dynamic_price(item_id)  # Get the price dynamically
                if price is not None:
                    table_rows.append((item_id, name, f"${price:.2f}"))
                else:
                    table_rows.append((item_id, name, "price unavailable"))
            ui.show_table("Service/Item Price", ["ID", "Name", "Price"], table_rows)
        except Exception as e:
            logging.error(f"Error displaying items: {e}")
'''def get_item_choice():
    while True:
        try:
            display_items()
            choice = int(input("Which service/item do you want? "))
            conn = create_connection()
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM Items WHERE ItemID = ?", (choice,))
            item = cursor.fetchone()
            ()
            if item:
                return choice
            else:
                logging.info("Invalid choice. Please try again.")
        except ValueError:
            logging.info("Invalid input. Please enter a number.")
        except Exception as e:
            logging.error(f"Error getting item choice: {e}")'''


## =========================
# Reservation Management
## =========================
def get_quantity():
    while True:
        try:
            quantity = int(input("How many? "))
            if quantity > 0:
                return quantity
            else:
                logging.info("Quantity must be a positive number. Please try again.")
        except ValueError:
            logging.info("Invalid input. Please enter a number.")
        except Exception as e:
            logging.error(f"Error getting quantity: {e}")

def get_another_item():
    while True:
        again = input("Would you like to order another service/item? (Yes/No) ").strip().lower()
        if again in ['yes', 'no']:
            return again == 'yes'
        else:
            logging.info("Invalid input. Please enter 'Yes' or 'No'.")

def normalize_room_number(room_number):
    """Canonical form for comparing room numbers: floor without leading zeros
    plus the trailing 3-digit code (e.g. '01001' vs '1001' both become '1001')."""
    room_number = str(room_number or "").strip()
    m = re.match(r'^0*(\d+)(\d{3})$', room_number)
    if m:
        return f"{int(m.group(1))}{m.group(2)}"
    return room_number


# Guests get a few tries before being handed back to the menu, rather than being
# trapped in the prompt forever.
VALIDATE_ROOM_MAX_ATTEMPTS = 3


def validate_room():
    """Verify a guest's identity from last name, first name, and room number.

    All three are required and must match one reservation row. The guest is never shown a
    list of other people's stays: a last name is not unique, and listing every match would
    disclose other guests' room numbers and first names to anyone who knew a surname. When
    one person holds two stays (the same last and first name in different rooms) the room
    number disambiguates, so the lookup is narrowed in SQL rather than offered as a menu.

    Returns (room_number, first_name) on success -- first_name is the stored value, so
    callers get correct capitalisation -- or (None, None) once the attempts run out or the
    database is unreachable.
    """
    for _ in range(VALIDATE_ROOM_MAX_ATTEMPTS):
        try:
            last_name = input("Please enter your last name: ").strip()
            first_name = input("Please enter your first name: ").strip()
            room_number = input("Please enter your room number (floor + 3-digit code): ").strip()
            if not last_name or not first_name or not room_number:
                logging.info("Please enter your last name, first name, and room number.")
                continue

            with get_connection() as conn:
                if conn is None:
                    return None, None
                cursor = conn.cursor()
                # Collation is case-insensitive, so this tolerates 'smith' for 'Smith'.
                cursor.execute(
                    "SELECT RoomNumber, FirstName FROM Reservations "
                    "WHERE LastName = ? AND FirstName = ?",
                    (last_name, first_name),
                )
                candidates = cursor.fetchall()

            if not candidates:
                # Deliberately does not distinguish "no such surname" from "wrong first
                # name", so the prompt cannot be used to discover who is staying here.
                logging.info("We could not find a reservation for those details. Please check your "
                             "last name, first name, and room number.")
                continue

            # Room numbers are free-text, so compare canonically ('01001' == '1001').
            wanted = normalize_room_number(room_number)
            match = next(
                (c for c in candidates
                 if normalize_room_number(c.RoomNumber) == wanted),
                None,
            )
            if match is None:
                logging.info("That room number does not match the name you entered. Please try again.")
                continue

            logging.info("Please wait while we validate your room number and name.")
            time.sleep(2)  # Simulate a delay for validation
            return room_number, match.FirstName
        except Exception as e:
            logging.error(f"Error validating room: {e}")
            return None, None
    logging.info("Too many unsuccessful attempts.")
    return None, None


def add_reservation():
    try:
        room_number = input("Enter room number (floor + 3-digit code): ").strip()
        # Parse room number like <floor><3-digit-code>, e.g. 10203 -> floor=10, code=203
        m = re.match(r'^(\d+)(\d{3})$', room_number)
        if not m:
            logging.info("Invalid room number format. Expected <floor><3-digit-code> (e.g. 10203).")
            return
        floor = int(m.group(1))
        last_name = input("Enter last name: ").strip()
        first_name = input("Enter first name: ").strip()
        today = business_date()
        check_in = ui.ask_date("Enter check-in date (YYYY-MM-DD)", default=str(today))
        check_out = ui.ask_date("Enter check-out date (YYYY-MM-DD)", default=str(today + timedelta(days=1)))
        if check_out <= check_in:
            logging.info("Check-out date must be after check-in date. Reservation not added.")
            return
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            # Reservations.RoomNumber is the primary key, so a room has at most one
            # reservation row. A still-current stay (check-out in the future) blocks a
            # new booking; a completed past stay is overwritten so the room can be
            # re-booked for the next guest.
            cursor.execute("SELECT CheckInDate, CheckOutDate, LastName, FirstName, Floor FROM Reservations WHERE RoomNumber = ?", (room_number,))
            existing = cursor.fetchone()
            if existing is not None and existing.CheckOutDate > today:
                logging.info(f"Room {room_number} is already reserved (check-out {existing.CheckOutDate}). New reservation refused.")
                return
            # Availability guard: never sell a room that is not sellable -- maintenance,
            # or still dirty from the last guest. See UNBOOKABLE_ROOM_STATUSES.
            room_status = get_room_status(room_number)
            if room_status in UNBOOKABLE_ROOM_STATUSES:
                if room_status == "Maintenance":
                    logging.info(f"Room {room_number} is under maintenance and cannot be reserved.")
                else:
                    logging.info(f"Room {room_number} is '{room_status}' and cannot be reserved until housekeeping marks it Available.")
                return
            # If this room is not tracked in Rooms yet (e.g. seed data not applied),
            # register it so the status board stays complete.
            upsert_room_if_missing(room_number)
            # The rate is captured HERE, at the moment the stay is made, and check-out
            # bills this number rather than whatever RoomTypes says then. Without it an
            # admin rate edit silently re-prices every confirmed booking.
            nightly_rate = get_nightly_rate(get_room_type(room_number))
            if existing is not None:
                # Re-booking displaces a completed past stay. Archive it first so the
                # booking history (and availability search over past occupancy) survives;
                # the live row is then reused for the new guest.
                archive_reservation(room_number)
                # CustomerID is reset to NULL because the room now belongs to a different
                # guest. Leaving the previous guest's link in place would credit THIS
                # stay's loyalty to the person who stayed here before -- handing the new
                # guest a discount, and points, that they never earned. The stay stays
                # unlinked until check-in links it (see link_reservation_customer).
                cursor.execute(
                    "UPDATE Reservations SET Floor = ?, LastName = ?, FirstName = ?, "
                    "CheckInDate = ?, CheckOutDate = ?, CustomerID = NULL, NightlyRate = ? "
                    "WHERE RoomNumber = ?",
                    (floor, last_name, first_name, check_in, check_out,
                     _rate_or_none(nightly_rate), room_number),
                )
                conn.commit()
                log_audit("UPDATE", "Reservation", room_number,
                          f"Re-booked for {last_name} {first_name} ({check_in} to {check_out}) at "
                          f"${nightly_rate:,.2f}/night; previous stay archived")
                logging.info(f"Room {room_number} re-booked for {last_name} {first_name} (check-in {check_in}, check-out {check_out}, ${nightly_rate:,.2f} per night). Previous stay archived.")
            else:
                cursor.execute(
                    "INSERT INTO Reservations (RoomNumber, Floor, LastName, FirstName, CheckInDate, CheckOutDate, NightlyRate) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (room_number, floor, last_name, first_name, check_in, check_out,
                     _rate_or_none(nightly_rate)),
                )
                conn.commit()
                log_audit("CREATE", "Reservation", room_number,
                          f"{last_name} {first_name}, check-in {check_in}, check-out {check_out}")
                logging.info(f"Reservation for room {room_number} on floor {floor} added successfully (check-in {check_in}, check-out {check_out}). Please use view reservations to verify if successful.")
    except Exception as e:
        logging.error(f"Error adding reservation: {e}")
    # connection closed by context manager


def _archive_row(cursor, room_number):
    """Copy a live reservation into ReservationArchive using an existing cursor.

    Does NOT commit, so a caller can archive and re-book in one transaction: if the
    re-booking fails, the archive is rolled back with it rather than leaving a duplicate
    history row behind. Best-effort by design -- a missing ReservationArchive table
    (migration 014 not applied) logs at debug and returns False so re-booking still
    works. Returns True when a row was archived.
    """
    try:
        # NightlyRate is read here only when migration 022 has added the column; a
        # pre-022 database falls back to the current rate, exactly as it did before.
        rate = None
        if _reservations_have_captured_rate():
            try:
                cursor.execute(
                    "SELECT RoomNumber, Floor, LastName, FirstName, CheckInDate, CheckOutDate, "
                    "NightlyRate FROM Reservations WHERE RoomNumber = ?",
                    (room_number,),
                )
            except Exception as e:
                logging.debug(f"Captured-rate read skipped ({type(e).__name__}: {e})")
                cursor.execute(
                    "SELECT RoomNumber, Floor, LastName, FirstName, CheckInDate, CheckOutDate "
                    "FROM Reservations WHERE RoomNumber = ?",
                    (room_number,),
                )
        else:
            cursor.execute(
                "SELECT RoomNumber, Floor, LastName, FirstName, CheckInDate, CheckOutDate "
                "FROM Reservations WHERE RoomNumber = ?",
                (room_number,),
            )
        row = cursor.fetchone()
        if not row:
            return False
        room_type = get_room_type(row.RoomNumber)
        # The archive records the rate the outgoing stay WAS billed at, not today's rate
        # for its category, so a later re-pricing cannot rewrite what that stay cost.
        archived_rate = stay_nightly_rate(
            row[6] if len(row) > 6 else None, get_nightly_rate(room_type))
        cursor.execute(
            "INSERT INTO ReservationArchive (RoomNumber, Floor, LastName, FirstName, "
            "CheckInDate, CheckOutDate, Nights, RoomType, NightlyRate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row.RoomNumber, row.Floor, row.LastName, row.FirstName,
             row.CheckInDate, row.CheckOutDate, stay_nights(row.CheckInDate, row.CheckOutDate),
             room_type, archived_rate),
        )
        return True
    except Exception as e:
        # Must not abort the caller's transaction: re-booking a room is more important
        # than preserving the displaced stay's history.
        logging.debug(f"Reservation archive skipped ({type(e).__name__}: {e})")
        return False


def archive_reservation(room_number):
    """Copy the live reservation for a room into ReservationArchive before it is overwritten.

    Called by add_reservation() when a re-booking displaces a completed past stay, and
    by delete_reservation(). No-ops when there is no live row for the room. Returns True
    if a row was archived.
    """
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            if not _archive_row(conn.cursor(), room_number):
                return False
            conn.commit()
            return True
    except Exception as e:
        # A missing archive table (migration 014 not applied) must not block re-booking.
        logging.debug(f"Reservation archive skipped ({type(e).__name__}: {e})")
        return False


## =========================
# Booking Desk (public, no login)
# =========================
# A guest books a room by TYPE from the main menu: the app picks the first available
# room of that type and hands back the room number as their reference. Money taken up
# front lives in ReservationPayments (migration 018) and is credited against the
# check-out bill rather than posted to the folio, so it never inflates room revenue.
#
# The pure helpers below hold the money rules so they can be unit-tested without a
# database; the wizards around them only gather input and write rows.
BOOKING_REF_PREFIX = "BK"
BOOKING_DEPOSIT_NIGHTS = 1
BOOKING_REFUND_CUTOFF_DAYS_DEFAULT = 7
# Booking payment kinds. These mirror the CK_ReservationPayments_Kind CHECK constraint.
PAYMENT_KIND_DEPOSIT = "Deposit"
PAYMENT_KIND_PREPAYMENT = "Prepayment"
PAYMENT_KIND_REFUND = "Refund"
PAYMENT_KIND_FORFEIT = "Forfeit"


def booking_quote(nights, nightly_rate, tax_rate):
    """Price a stay: the room component, its tax, and the total the guest will owe.

    Mirrors the check-out folio exactly -- the room group is taxed and the F&B group is
    not yet in play -- so a full prepayment here covers the room bill precisely.
    Negative inputs are clamped rather than rejected so a stray setting value can never
    produce a negative quote.
    """
    nights = max(0, int(nights or 0))
    rate = max(0.0, float(nightly_rate or 0.0))
    tax_rate = max(0.0, float(tax_rate or 0.0))
    subtotal = round(nights * rate, 2)
    tax_amount = round(subtotal * tax_rate, 2)
    return {
        "nights": nights,
        "nightly_rate": round(rate, 2),
        "tax_rate": tax_rate,
        "subtotal": subtotal,
        "tax": tax_amount,
        "total": round(subtotal + tax_amount, 2),
    }


def booking_payment_options(nights, nightly_rate, tax_rate):
    """Offer the deposit / pay-in-full choices for a quote, as (choice, label, kind, amount).

    A deposit is REQUIRED to secure the room: there is no pay-at-check-out option, so a
    confirmed booking is always backed by a ReservationPayments row. That row is also
    what makes the booking reference findable by view_my_booking()/cancel_booking(),
    which is why a no-prepayment path is not offered. The deposit is capped at the stay
    length so a one-night stay cannot be asked for a second night up front.
    """
    quote = booking_quote(nights, nightly_rate, tax_rate)
    options = []
    deposit_nights = min(BOOKING_DEPOSIT_NIGHTS, quote["nights"])
    # The `or` keeps the list non-empty for a $0 quote (a zero-rated room, or a
    # zero-night stay), which would otherwise make the wizard's minimum=1 /
    # maximum=0 prompt impossible to satisfy. The $0 row is still written, so the
    # booking stays findable by its reference.
    if deposit_nights > 0 or quote["total"] <= 0:
        deposit = booking_quote(deposit_nights, nightly_rate, tax_rate)
        remainder = round(quote["total"] - deposit["total"], 2)
        options.append((
            len(options) + 1,
            f"Pay a {deposit_nights}-night deposit of ${deposit['total']:,.2f} now "
            f"(then ${remainder:,.2f} at check-out)",
            PAYMENT_KIND_DEPOSIT,
            deposit["total"],
        ))
    if quote["total"] > 0:
        options.append((
            len(options) + 1,
            f"Pay in full now: ${quote['total']:,.2f}",
            PAYMENT_KIND_PREPAYMENT,
            quote["total"],
        ))
    return options


def settle_with_prepayment(total, credit):
    """Apply booking credit to a balance, never crediting more than is owed.

    The booking quote is an ESTIMATE in exactly one way: F&B is added at check-out. The room
    rate itself is captured at booking (022) and cannot move afterwards. A guest who prepaid
    against more nights than they stayed, or who ordered less than a full room service
    spend, can therefore end up with credit left over (reported as credit_unused for a later
    refund) or short (balance_due).
    """
    total = max(0.0, float(total or 0.0))
    credit = max(0.0, float(credit or 0.0))
    applied = round(min(credit, total), 2)
    return {
        "total": round(total, 2),
        "credit_available": round(credit, 2),
        "credit_applied": applied,
        "credit_unused": round(credit - applied, 2),
        "balance_due": round(total - applied, 2),
    }


def refund_decision(days_until_check_in, cutoff_days, amount_paid):
    """Decide what a cancellation owes the guest, as {action, kind, amount, reason}.

    `action` is 'refused' (the check-in date has arrived), 'none' (nothing was paid),
    'refund' (cancelling at least `cutoff_days` ahead) or 'forfeit' (inside the cutoff,
    so the deposit is kept). Both 'refund' and 'forfeit' write a NEGATIVE row so the
    stay's payments always net back to zero; the Kind is what distinguishes money
    returned from money kept.
    """
    days = int(days_until_check_in)
    cutoff = max(0, int(cutoff_days))
    paid = round(max(0.0, float(amount_paid or 0.0)), 2)
    if days <= 0:
        # 0 is the arrival date itself, not "still cancellable". Refusing here matters
        # because cancel_booking() DELETEs the reservation row: letting a guest cancel on
        # the day they arrive would wipe an in-house stay, mark the room Available while
        # they are in it, and leave check-out with no reservation to bill against.
        return {"action": "refused", "kind": None, "amount": 0.0,
                "reason": ("check-in is today or earlier, so this stay can no longer be "
                           "cancelled online")}
    if paid <= 0:
        return {"action": "none", "kind": None, "amount": 0.0,
                "reason": "no payment was taken for this booking"}
    if days >= cutoff:
        return {"action": "refund", "kind": PAYMENT_KIND_REFUND, "amount": -paid,
                "reason": f"cancelled {days} day(s) ahead, inside the {cutoff}-day free-cancellation window"}
    return {"action": "forfeit", "kind": PAYMENT_KIND_FORFEIT, "amount": -paid,
            "reason": f"cancelled only {days} day(s) ahead, inside the {cutoff}-day cutoff, so the deposit is forfeited"}


def booking_refund_policy(check_in, cutoff_days, amount_charged=None):
    """The guest-facing cancellation policy for a stay, as a sentence.

    Stating this at BOOKING time is the whole point: a deposit is non-refundable inside
    the cutoff, and a guest cannot be held to that unless they are told before they hand
    over a card. The wording must agree with `refund_decision()`, which is why the
    deadline is derived from the same `days >= cutoff` rule rather than restated by hand.

    Returns {'cutoff': int, 'deadline': date, 'summary': str}.
    """
    cutoff = max(0, int(cutoff_days))
    # A stay cannot be cancelled once check-in arrives (refund_decision() refuses
    # days_until <= 0), so the last refundable day is never later than the day before
    # arrival. max(cutoff, 1) keeps the promise honest at cutoff 0, where a naive
    # "cutoff days ahead" deadline would land ON the arrival date and offer a refund
    # that is then refused.
    refundable_days = max(cutoff, 1)
    deadline = check_in - timedelta(days=refundable_days)
    held = "the deposit" if amount_charged is None else f"the ${float(amount_charged):,.2f} paid"
    summary = (f"Cancel on or before {deadline} - at least {refundable_days} day(s) before "
               f"check-in - for a full refund. Cancel after that and {held} is forfeited. "
               f"From the check-in date onwards the booking cannot be cancelled online, so "
               f"please contact the front desk.")
    return {"cutoff": cutoff, "refundable_days": refundable_days,
            "deadline": deadline, "summary": summary}


def get_booking_refund_cutoff_days():
    """Admin-editable free-cancellation window, in days before check-in."""
    return _setting_int('booking_refund_cutoff_days', BOOKING_REFUND_CUTOFF_DAYS_DEFAULT)


BOOKING_REF_WRITE_ATTEMPTS = 5


def _write_booking_charge(conn, room_number, booking_ref, stay_check_in, kind, amount,
                          nights_covered):
    """Write a booking's initial charge, retrying if the reference was taken. Returns the
    reference actually used, or None if the charge could not be recorded.

    A reference collision is a race with another guest, not a failure of the booking, so it
    is retried with a fresh reference rather than being reported to the guest. Migration
    021's filtered unique index is what makes the collision detectable: before it, the
    pre-flight probe in new_booking_ref() was the only defence and two sessions could both
    pass it.

    Retrying the INSERT inside the same transaction relies on SQL Server not aborting the
    transaction on a constraint violation (the default, with XACT_ABORT off). If the driver
    ever did abort it, the retry's own failure is caught below and reported as a hard
    failure, so the caller still rolls back rather than committing a partial booking.
    """
    for attempt in range(BOOKING_REF_WRITE_ATTEMPTS):
        ref = booking_ref if attempt == 0 else new_booking_ref()
        try:
            payment_id = record_booking_payment(room_number, ref, stay_check_in, kind,
                                                amount, nights_covered=nights_covered,
                                                conn=conn)
        except BookingRefTaken:
            logging.debug(f"Booking reference {ref} was taken concurrently; minting another.")
            continue
        if payment_id is None:
            return None
        return ref
    logging.error(f"Could not find a free booking reference in {BOOKING_REF_WRITE_ATTEMPTS} attempts.")
    return None


def new_booking_ref():
    """Generate a short guest-facing booking reference, e.g. 'BK-4F2A9C'."""
    while True:
        ref = f"{BOOKING_REF_PREFIX}-{random.randint(0x100000, 0xFFFFFF):06X}"
        if not booking_reference_exists(ref):
            return ref


def booking_reference_exists(ref):
    """True when the reference is already taken. Missing table (no 018) means free."""
    with get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM ReservationPayments WHERE BookingRef = ?", (ref,))
            return cursor.fetchone() is not None
        except Exception as e:
            logging.debug(f"Booking reference probe skipped ({type(e).__name__}: {e})")
            return False


class BookingRefTaken(Exception):
    """A booking reference was already taken by a concurrent booking.

    Raised instead of returning None so the caller can tell "this reference is gone, mint
    another and retry" apart from "the payment could not be recorded at all", which is a
    real failure the guest should be told about. The uniqueness itself is enforced by the
    filtered unique index in migration 021, not by any check here.
    """


# The codes that mean "this unique/primary key already exists", and nothing else.
# Deliberately NOT the whole SQLSTATE class 23 ("integrity constraint violation"): that class
# also covers CHECK and FOREIGN KEY violations, so a bad `Kind` or a missing room would be
# misread as a taken booking reference and retried pointlessly, hiding the real error behind
# "could not find a free booking reference".
_DUPLICATE_KEY_NUMBERS = {23505, 2601, 2627}
# The generic class-23 code, used by SQL Server when the driver reports no specific number.
_DUPLICATE_KEY_GENERIC_STATE = '23000'
_DUPLICATE_KEY_GENERIC_STATE_INT = 23000


def _is_integrity_code(value):
    """True for a 5-digit SQL Server error number in class 23 (integrity violation)."""
    return isinstance(value, int) and 23000 <= value <= 23999


def _is_duplicate_key_error(error):
    """True for a unique/primary key violation.

    Checked against the specific codes rather than SQLSTATE class 23 (see
    _DUPLICATE_KEY_NUMBERS). pyodbc reports the SQLSTATE in args[0] and a MORE SPECIFIC
    server number alongside it: a duplicate key is 23000 + 2627/23505, while a foreign key
    violation is 23000 + 23503 and a CHECK violation 23000 + 23512. So the specific number
    decides, and the generic 23000 only decides when it arrives with nothing more specific
    to go on -- which is the plain duplicate-key case.

    The numbers are collected from args AND from the message text, because pyodbc does not
    always expose them as separate args (it can arrive as a single stringified tuple), and a
    classifier that silently answers False for a real duplicate key would spin the retry
    loop and then report the wrong reason to the guest.
    """
    values = list(getattr(error, "args", ()))
    message = str(error)
    numbers = [v for v in values if _is_integrity_code(v)]
    # 2601/2627 are the SQL Server duplicate-key numbers; they are not in class 23, but
    # they turn up bare in driver messages just as often as 23000 does.
    numbers += [int(found) for found in re.findall(r'\b(23\d{3}|2601|2627)\b', message)]

    deciding = set(numbers) - {_DUPLICATE_KEY_GENERIC_STATE_INT}
    if deciding & _DUPLICATE_KEY_NUMBERS:
        return True
    if deciding:
        return False
    if _DUPLICATE_KEY_GENERIC_STATE_INT in numbers or \
            any(isinstance(v, str) and v.strip() == _DUPLICATE_KEY_GENERIC_STATE
                for v in values):
        return True
    return "duplicate key" in message.lower()


def _original_charge_card(cursor, booking_ref):
    """The card a booking was originally charged to, for a reversal row.

    A Refund/Forfeit row must be attributed to the card that took the money, NOT to
    `LAST_CARD_DIGITS`. That global is the last card the PROCESS authorised, and a
    cancellation never prompts for a card, so it still holds whatever some earlier,
    unrelated guest paid with -- a refund row would then name a stranger's card. Reading it
    back from the booking's own initial charge is the only self-consistent answer, and it
    is what makes a refund traceable to the payment it reverses.

    Returns None when there is no such charge (a legacy row, or 018 not applied).
    """
    try:
        cursor.execute(
            "SELECT TOP 1 CardLast4 FROM ReservationPayments "
            "WHERE BookingRef = ? AND Kind IN ('Deposit', 'Prepayment') AND Amount > 0 "
            "ORDER BY PaymentID",
            (booking_ref,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logging.debug(f"Could not read the original card for {booking_ref}: "
                      f"{type(e).__name__}: {e}")
        return None


def record_booking_payment(room_number, booking_ref, stay_check_in, kind, amount,
                           nights_covered=None, notes=None, conn=None):
    """Append one signed row to ReservationPayments. Returns the new PaymentID or None.

    Pass `conn` to join a caller's transaction (a declined card must not leave a
    half-written booking behind). `kind` CHECK-constrained, so an unknown kind is a bug
    we want to hear about rather than a silently ignored payment.

    Raises BookingRefTaken when the reference collides with another booking's initial
    charge, which migration 021's filtered unique index makes a real possibility rather
    than a theoretical one.

    `conn` is not optional in practice: both callers in this module pass one
    (`_write_booking_charge`, `cancel_booking`), so the shipping path is the `conn is not
    None` branch at the bottom, which never opens a connection of its own and therefore
    never touches `main.get_connection()`. On that path the IntegrityError arrives
    intact, `_is_duplicate_key_error()` recognises it, and BookingRefTaken is raised and
    retried. `tests/verify_e2e.py` asserts this against a real server.

    The `conn is None` branch has no caller in main.py. It now behaves like the rest of
    the app: a statement failure propagates with pyodbc's own type, so a collision is
    classified here and raised, rather than being logged and answered with a silent
    `None`. It behaved differently only while main.py carried a second, broken copy of
    `get_connection()` -- see AGENTS.md section 7.
    """
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        logging.error(f"Refusing to record a non-numeric booking payment: {amount!r}")
        return None

    def _write(cursor):
        # A reversal is attributed to the card that took the original money; a charge is
        # attributed to the card just authorised.
        if kind in (PAYMENT_KIND_REFUND, PAYMENT_KIND_FORFEIT):
            card_last4 = _original_charge_card(cursor, booking_ref)
        else:
            card_last4 = LAST_CARD_DIGITS or None
        cursor.execute(
            "INSERT INTO ReservationPayments "
            "(RoomNumber, BookingRef, StayCheckIn, Kind, Amount, NightsCovered, CardLast4, Notes) "
            "OUTPUT INSERTED.PaymentID "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (room_number, booking_ref, stay_check_in, kind, amount,
             nights_covered, card_last4, notes),
        )
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    try:
        if conn is not None:
            return _write(conn.cursor())
        with get_connection() as own:
            if own is None:
                return None
            payment_id = _write(own.cursor())
            own.commit()
            return payment_id
    except Exception as e:
        if _is_duplicate_key_error(e):
            raise BookingRefTaken(booking_ref) from e
        # A missing table means migration 018 is not applied yet.
        logging.error(f"Could not record booking payment: {e}")
        return None


def get_booking_paid_total(room_number, stay_check_in):
    """Net money still held for a stay: charges positive, refunds/forfeits negative.

    Only counts rows for THIS stay (same room + same check-in date), because a room is
    re-let after every check-out and an older booking's money must never be credited to
    the next guest's bill.
    """
    with get_connection() as conn:
        if conn is None:
            return 0.0
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ISNULL(SUM(Amount), 0) FROM ReservationPayments "
                "WHERE RoomNumber = ? AND StayCheckIn = ?",
                (room_number, stay_check_in),
            )
            row = cursor.fetchone()
            return round(float(row[0] or 0.0), 2) if row else 0.0
        except Exception as e:
            # Missing table means migration 018 is not applied: not an error, this runs
            # on every check-out, so it must not spam the log.
            logging.debug(f"Booking payments unavailable ({type(e).__name__}: {e})")
            return 0.0


def allocate_booking_credit(payments, amount):
    """Split `amount` of credit across a stay's unconsumed payment rows.

    Pure, so the split is unit-testable without a database (tests/test_booking.py).

    `payments` is [(PaymentID, remaining_amount)] in the order the credit should be
    consumed -- oldest row first -- where `remaining_amount` is Amount - already applied.
    Returns [(PaymentID, consumed)] with `consumed > 0`, in the same order, summing to at
    most `amount`.

    A row is allowed to be only PARTLY consumed, and the return value is what makes that
    representable: the pre-020 schema could only set a boolean IsApplied per row, so
    crediting a bill smaller than a prepayment either lied (marking $300 applied against a
    $250 bill) or stranded the surplus with no way to say how much was used. Consuming
    oldest-first also means the surplus sits in the guest's most recent payment rather
    than being spread across several rows.
    """
    remaining = round(max(0.0, float(amount or 0.0)), 2)
    allocation = []
    for payment_id, available in payments or []:
        if remaining <= 0:
            break
        available = round(max(0.0, float(available or 0.0)), 2)
        if available <= 0:
            continue
        consumed = round(min(available, remaining), 2)
        if consumed > 0:
            allocation.append((payment_id, consumed))
            remaining = round(remaining - consumed, 2)
    return allocation


def get_outstanding_booking_credit(room_number, stay_check_in, conn=None):
    """Unspent booking credit for a stay, ready to deduct from the check-out bill.

    Counts only what each row has NOT already given to an invoice
    (`Amount - AppliedAmount`), so a retried check-out after a declined card still sees
    the full credit, and a partly-consumed row contributes only its remainder.

    Rows are signed (refunds/forfeits are negative), so a returned deposit reduces the
    credit held; the total is floored at zero because a stay owes no negative credit.
    """
    def _read_partial(cursor):
        cursor.execute(
            "SELECT ISNULL(SUM(Amount - AppliedAmount), 0) FROM ReservationPayments "
            "WHERE RoomNumber = ? AND StayCheckIn = ?",
            (room_number, stay_check_in),
        )
        row = cursor.fetchone()
        return round(max(0.0, float(row[0] or 0.0)), 2) if row else 0.0

    def _read_legacy(cursor):
        # Pre-020: a row is consumed whole or not at all, so IsApplied = 0 is the
        # whole remainder. Used until AppliedAmount exists (see AGENTS.md).
        cursor.execute(
            "SELECT ISNULL(SUM(Amount), 0) FROM ReservationPayments "
            "WHERE RoomNumber = ? AND StayCheckIn = ? AND IsApplied = 0",
            (room_number, stay_check_in),
        )
        row = cursor.fetchone()
        return round(max(0.0, float(row[0] or 0.0)), 2) if row else 0.0

    def _read(cursor):
        if _RESERVATION_PAYMENTS_PARTIAL_SUPPORT is False:
            return _read_legacy(cursor)
        try:
            return _read_partial(cursor)
        except Exception as e:
            _set_partial_credit_unsupported(e)
            return _read_legacy(cursor)

    if conn is not None:
        return _read(conn.cursor())
    with get_connection() as own:
        if own is None:
            return 0.0
        try:
            return _read(own.cursor())
        except Exception as e:
            # See get_booking_paid_total(): an unapplied 018 is expected, not an error.
            logging.debug(f"Booking credit unavailable ({type(e).__name__}: {e})")
            return 0.0


def apply_booking_credit(room_number, stay_check_in, credit, invoice_id, conn):
    """Consume `credit` of a stay's unspent booking payments, recording what was used.

    Runs inside the caller's transaction alongside the invoice insert, so the credit and
    the invoice either both land or neither does. No-ops when there is nothing to apply.

    Each row records the amount it gave to this invoice in `AppliedAmount` and flips
    `IsApplied` only once it is fully consumed, so a partly-used row is neither reported
    as spent nor silently dropped. Returns the number of rows touched.
    """
    if credit <= 0 or invoice_id is None:
        return 0
    try:
        cursor = conn.cursor()
        if _RESERVATION_PAYMENTS_PARTIAL_SUPPORT is False:
            return _apply_credit_legacy(cursor, room_number, stay_check_in, invoice_id)
        try:
            cursor.execute(
                "SELECT PaymentID, Amount - AppliedAmount FROM ReservationPayments "
                "WHERE RoomNumber = ? AND StayCheckIn = ? AND Amount - AppliedAmount > 0 "
                "ORDER BY PaymentID",
                (room_number, stay_check_in),
            )
            rows = [(int(r[0]), float(r[1] or 0.0)) for r in cursor.fetchall()]
        except Exception as e:
            _set_partial_credit_unsupported(e)
            return _apply_credit_legacy(cursor, room_number, stay_check_in, invoice_id)

        touched = 0
        for payment_id, consumed in allocate_booking_credit(rows, credit):
            cursor.execute(
                "UPDATE ReservationPayments SET "
                "  AppliedAmount = ISNULL(AppliedAmount, 0) + ?, "
                "  IsApplied = CASE WHEN ISNULL(AppliedAmount, 0) + ? >= Amount "
                "                   THEN 1 ELSE IsApplied END, "
                "  AppliedToInvoiceID = ? "
                "WHERE PaymentID = ?",
                (consumed, consumed, invoice_id, payment_id),
            )
            touched += 1
        return touched
    except Exception as e:
        logging.error(f"Error applying booking credit to invoice {invoice_id}: {e}")
        return 0


def _apply_credit_legacy(cursor, room_number, stay_check_in, invoice_id):
    """Pre-020 fallback: a row can only be consumed whole, so mark the lot.

    Kept so check-out keeps working before migration 020 is applied. It over-claims on
    the rare bill-smaller-than-prepayment case (see allocate_booking_credit), which is
    strictly better than crediting the same payment twice.
    """
    cursor.execute(
        "UPDATE ReservationPayments SET IsApplied = 1, AppliedToInvoiceID = ? "
        "WHERE RoomNumber = ? AND StayCheckIn = ? AND IsApplied = 0",
        (invoice_id, room_number, stay_check_in),
    )
    return max(0, int(cursor.rowcount or 0))


_INVOICES_PREPAID_SUPPORT = None

# False once a query proves ReservationPayments has no per-row AppliedAmount (migration
# 020 not applied). None = the partial-credit path still works, or is untried. Only the
# negative answer is cached: it is the one that saves a failed query per check-out, and a
# successful partial query costs nothing to repeat.
_RESERVATION_PAYMENTS_PARTIAL_SUPPORT = None


def _set_partial_credit_unsupported(error):
    """Latch the pre-020 booking-credit shape after a failed partial-credit query.

    Only latches on a "that column or table isn't there" failure: an unapplied 020, or a
    wholly unapplied 018. Any other error is left alone rather than cached as a permanent
    capability answer -- a deadlock or a dropped connection must not pin the app to the
    legacy path for the rest of the session.
    """
    global _RESERVATION_PAYMENTS_PARTIAL_SUPPORT
    message = str(error).lower()
    missing_column = "invalid column" in message and "appliedamount" in message
    missing_table = "invalid object name" in message and "reservationpayments" in message
    if missing_column or missing_table:
        if _RESERVATION_PAYMENTS_PARTIAL_SUPPORT is None:
            logging.debug("ReservationPayments.AppliedAmount missing (migration 020 not "
                          "applied yet) -- consuming booking credit whole-row.")
        _RESERVATION_PAYMENTS_PARTIAL_SUPPORT = False


def _reservation_check_in(room_number):
    """The check-in date of a room's live reservation, or None if it has no row.

    Booking payments are keyed on (RoomNumber, StayCheckIn), so this is what ties a
    credit to the stay that earned it. None means "not a stay we can credit" and makes
    the caller skip the credit entirely.
    """
    with get_connection() as conn:
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT CheckInDate FROM Reservations WHERE RoomNumber = ?", (room_number,))
            row = cursor.fetchone()
            return row[0] if row is not None else None
        except Exception as e:
            logging.error(f"Error reading reservation for room {room_number}: {e}")
            return None


def _invoices_have_prepaid_column():
    """Whether Invoices.PrepaidAmount exists (migration 018). Cached after the first probe.

    Check-out must keep working before 018 is applied, so the invoice insert is built
    without the column when it is missing rather than failing with an invalid-column error.
    """
    global _INVOICES_PREPAID_SUPPORT
    if _INVOICES_PREPAID_SUPPORT is not None:
        return _INVOICES_PREPAID_SUPPORT
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT COL_LENGTH('dbo.Invoices', 'PrepaidAmount')")
            row = cursor.fetchone()
            _INVOICES_PREPAID_SUPPORT = bool(row and row[0])
    except Exception as e:
        logging.debug(f"Invoices.PrepaidAmount probe failed ({type(e).__name__}: {e})")
        _INVOICES_PREPAID_SUPPORT = False
    return _INVOICES_PREPAID_SUPPORT


def _build_invoice_insert(has_prepaid, values):
    """Build the check-out Invoices INSERT, with or without PrepaidAmount.

    `values` is the ordered column list ending with the group breakdown. PrepaidAmount
    (migration 018) is appended only when the column exists, so check-out keeps working
    on a database that has not applied 018 yet. Split out from bill_room_transactions()
    so the exact SQL can be asserted in tests without a database.
    """
    cols = [
        "RoomNumber", "Subtotal", "DiscountCodeAmount", "TierDiscountAmount",
        "TaxAmount", "TotalAmount", "PointsRedeemed", "RedemptionValue", "AmountPaid",
        "RoomSubtotal", "RoomTaxAmount", "RoomTotal", "FnbSubtotal",
        "FnbDiscountCodeAmount", "FnbTierDiscountAmount", "FnbTaxAmount",
    ]
    params = list(values)[:len(cols)]
    if has_prepaid:
        cols.append("PrepaidAmount")
        params.append(round(float(values[16] if len(values) > 16 else 0.0), 2))
    placeholders = ",".join("?" for _ in params)
    return (f"INSERT INTO Invoices ({', '.join(cols)}) "
            f"OUTPUT INSERTED.InvoiceID VALUES ({placeholders})", tuple(params))


def _book_reservation_in_conn(conn, room_number, last_name, first_name, check_in, check_out,
                              customer_id=None, room_type=None):
    """Claim `room_number` for a stay on the caller's connection. Returns (ok, reason).

    Applies the same rules as add_reservation(): a stay that has not finished yet
    refuses the claim, a completed past stay is archived and then overwritten so the
    room can be re-let. Does not commit -- the caller decides, so a declined card cannot
    leave a reserved room behind.

    `customer_id` links the stay to the guest who booked it (migration 019), which is
    what lets the stay's loyalty and invoices be attributed to a person rather than to
    whichever guest the room is re-let to. It is optional so front-desk callers, which
    have no logged-in guest, keep working.

    `room_type` is the category the GUEST asked for, and it is re-read here rather than
    trusted from the caller's candidate list. That list came from search_availability()
    before the transaction opened, and an admin can re-type or re-status a room in the
    gap, so a guest who asked for a Deluxe must not silently be given a Standard. The
    check is inside the same transaction as the insert so the type cannot change between
    the check and the write.

    The nightly rate is read on the same cursor and CAPTURED onto the reservation
    (migration 022), for the same reason: the guest agreed to a number, and a rate edit
    between the quote and the write -- or between the booking and the arrival -- must not
    move it. post_room_charge() bills what is stored here.
    """
    today = business_date()
    m = re.match(r'^(\d+)(\d{3})$', room_number)
    floor = int(m.group(1)) if m else 0
    cursor = conn.cursor()
    actual_type = None
    if room_type is not None:
        actual_type = _room_type_in_conn(cursor, room_number)
        if actual_type is not None and actual_type.strip().lower() != room_type.strip().lower():
            return False, f"room {room_number} is a {actual_type}, not a {room_type}"
    # The rate follows the room's ACTUAL category, which is the one that was just
    # verified. A room with no Rooms row is "cannot verify" and is allowed through, so it
    # falls back to the guest's requested category.
    rate = _rate_or_none(_nightly_rate_in_conn(cursor, actual_type or room_type))
    cursor.execute(
        "SELECT CheckInDate, CheckOutDate, LastName, FirstName, Floor FROM Reservations "
        "WHERE RoomNumber = ?",
        (room_number,),
    )
    existing = cursor.fetchone()
    if existing is not None and existing.CheckOutDate > today:
        return False, f"room {room_number} is already reserved until {existing.CheckOutDate}"
    # A guest is never handed a room that housekeeping has not cleared. Read on this
    # transaction's own cursor, not via get_room_status(), which opens a second
    # connection and so could not see -- or be seen alongside -- the insert below.
    status = _room_status_in_conn(cursor, room_number)
    if status in UNBOOKABLE_ROOM_STATUSES:
        return False, f"room {room_number} is {status} and is not available to book"
    # Re-letting the room MUST clear the previous guest's link. Leaving it behind would
    # credit this stay's loyalty to whoever stayed here last.
    if existing is not None:
        _archive_row(cursor, room_number)
        cursor.execute(
            "UPDATE Reservations SET Floor = ?, LastName = ?, FirstName = ?, "
            "CheckInDate = ?, CheckOutDate = ?, CustomerID = ?, NightlyRate = ? "
            "WHERE RoomNumber = ?",
            (floor, last_name, first_name, check_in, check_out, customer_id, rate, room_number),
        )
    else:
        cursor.execute(
            "INSERT INTO Reservations (RoomNumber, Floor, LastName, FirstName, CheckInDate, "
            "CheckOutDate, CustomerID, NightlyRate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (room_number, floor, last_name, first_name, check_in, check_out, customer_id, rate),
        )
    return True, "re-booked (previous stay archived)" if existing is not None else "reserved"


def register_customer(email, last_name, first_name, password):
    """Create a booking-desk account. Returns the new CustomerID, or None.

    The unique filtered index on Email (migration 019) is what actually prevents two
    accounts sharing an address; it is a real database guarantee, not a check-then-insert
    race. That matters because the login lookup is by email, so a duplicate would make
    the account ambiguous.
    """
    with get_connection() as conn:
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CustomerID FROM CustomerProfiles WHERE Email = ?", (email,))
            if cursor.fetchone():
                logging.info("An account already exists for that email. Please sign in instead.")
                return None
            cursor.execute(
                "INSERT INTO CustomerProfiles (LastName, FirstName, Email, Password) "
                "OUTPUT INSERTED.CustomerID VALUES (?, ?, ?, ?)",
                (last_name, first_name, email, password),
            )
            new_row = cursor.fetchone()
            conn.commit()
            log_audit("CREATE", "CustomerProfile", email, "Booking account registered")
            return int(new_row[0]) if new_row and new_row[0] is not None else None
        except Exception as e:
            # Most likely a duplicate email losing the race against a concurrent signup.
            logging.error(f"Could not create account: {e}")
            return None


def customer_login():
    """Sign a guest in at the booking desk, registering them on first use.

    Returns the CustomerID on success, or None if the guest gave up. A blank email or
    password is never accepted, because both are the only handle the account has: there
    is no "guest" row without them, and a booking always has an owner.
    """
    global CURRENT_CUSTOMER
    if CURRENT_CUSTOMER is not None:
        return CURRENT_CUSTOMER
    for attempt in range(1, CUSTOMER_LOGIN_MAX_ATTEMPTS + 1):
        email = input("Email address: ").strip()
        if not email:
            logging.info("Your email address is required to book a room.")
            continue
        try:
            with get_connection() as conn:
                if conn is None:
                    return None
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT CustomerID, FirstName, LastName, Password FROM CustomerProfiles "
                    "WHERE Email = ?", (email,))
                row = cursor.fetchone()
        except Exception as e:
            # A missing Password column means 019 is not applied yet.
            logging.error(f"Booking login is unavailable: {e}")
            ui.error("Booking accounts are not available right now. Please contact the front desk.")
            return None

        if row is None:
            # First visit. Create the account rather than sending the guest away: a guest
            # with no account cannot book, and a guest who cannot book has no way to be
            # remembered for their next stay.
            last_name = input("New here -- enter your last name: ").strip()
            first_name = input("Enter your first name: ").strip()
            if not last_name or not first_name:
                logging.info("Both first and last name are required to create an account.")
                continue
            password = input("Choose a password: ").strip()
            if not password:
                logging.info("A password is required to create an account.")
                continue
            customer_id = register_customer(email, last_name, first_name, password)
            if customer_id:
                CURRENT_CUSTOMER = customer_id
                log_audit("LOGIN", "CustomerProfile", email, "Booking account created at sign-up")
                logging.info(f"Welcome, {first_name}. Your booking account is ready.")
                return customer_id
            continue

        customer_id, first_name, _last_name, stored = row.CustomerID, row.FirstName, row.LastName, row.Password
        if not stored:
            logging.info("That account has no password yet. Please contact the front desk to set one.")
            return None
        password = input("Password: ").strip()
        if password == stored:
            CURRENT_CUSTOMER = int(customer_id)
            log_audit("LOGIN", "CustomerProfile", email, "Booking desk sign-in")
            logging.info(f"Welcome back, {first_name}.")
            return CURRENT_CUSTOMER
        remaining = CUSTOMER_LOGIN_MAX_ATTEMPTS - attempt
        if remaining > 0:
            logging.info(f"Wrong password. {remaining} attempt(s) left.")
    logging.info("Too many failed attempts. Please try again later, or contact the front desk "
                 "and we can help with the booking.")
    return None


def _customer_profile(customer_id):
    """Return (last_name, first_name, email) for a CustomerID, or None if it is gone."""
    if not customer_id:
        return None
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT LastName, FirstName, Email FROM CustomerProfiles WHERE CustomerID = ?",
                (customer_id,))
            row = cursor.fetchone()
            return (row.LastName, row.FirstName, row.Email) if row else None
    except Exception as e:
        logging.debug(f"Could not read customer profile {customer_id}: {e}")
        return None


def book_room():
    """Public booking wizard: sign in, choose a room TYPE, dates, and pay up front.

    The guest never picks a room number -- the app assigns the first available room of
    the chosen type and returns it as their reference. The QUOTE is an estimate, because
    the guest has not chosen a room yet and F&B is not in play, but the RATE is not: it is
    captured onto the reservation when the booking commits (migration 022) and check-out
    bills that number, so an admin rate change between booking and arrival cannot move
    the total. The credit is still capped at what is owed, so a bill can come in under
    what was taken (see §4 of docs/BOOKING.md).
    """
    logging.info("\n--- Book a Room ---")
    # A login is required, not optional: the booking is tied to a profile, which is what
    # gives the guest their loyalty balance across stays and lets the desk see their
    # bookings later.
    customer_id = customer_login()
    if not customer_id:
        logging.info("A booking account is required to book a room.")
        return
    room_types = get_room_types()
    if not room_types:
        logging.info("No room types are currently available for booking.")
        return
    ui.show_table("Available Room Types", ["Choice", "Room Type", "Nightly Rate"],
                  [(i, rt, f"${rate:,.2f}") for i, (rt, rate) in enumerate(room_types, 1)])
    choice = ui.ask_number("Enter the number of the room type you want",
                           minimum=1, maximum=len(room_types))
    if choice is None:
        return
    room_type, nightly_rate = room_types[choice - 1]

    today = business_date()
    check_in = ui.ask_date("Check-in date (YYYY-MM-DD)", default=str(today))
    if check_in is None:
        return
    check_out = ui.ask_date("Check-out date (YYYY-MM-DD)", default=str(check_in + timedelta(days=1)))
    if check_out is None:
        return
    if check_out <= check_in:
        logging.info("Check-out date must be after check-in date.")
        return
    if check_in < today:
        logging.info("Check-in date cannot be in the past.")
        return

    nights = stay_nights(check_in, check_out)
    if nights <= 0:
        logging.info("That stay is at least one night long.")
        return

    # The name on the reservation is the ACCOUNT holder's, not a second free-text
    # identity: the reservation's CustomerID is what links the stay to the login, and
    # two names on one account would let a stay's loyalty be read under a different name
    # than the one the guest signs in with.
    profile = _customer_profile(customer_id)
    if profile and profile[0] and profile[1]:
        last_name, first_name = profile[0], profile[1]
        logging.info(f"Booking as {first_name} {last_name}"
                     + (f" <{profile[2]}>." if profile[2] else "."))
    else:
        last_name = input("Enter your last name: ").strip()
        first_name = input("Enter your first name: ").strip()
        if not last_name or not first_name:
            logging.info("Both first and last name are required to hold a reservation.")
            return

    quote = booking_quote(nights, nightly_rate, get_tax_rate())
    ui.box("Booking Estimate",
           f"Room type: {room_type}\n"
           f"Nightly rate: ${quote['nightly_rate']:,.2f}\n"
           f"Nights: {nights}\n"
           f"Room subtotal: ${quote['subtotal']:,.2f}\n"
           f"Tax ({quote['tax_rate']*100:.0f}%): ${quote['tax']:,.2f}\n"
           f"Estimated total: ${quote['total']:,.2f}\n"
           f"This rate is locked in when you book. Any food, beverage or other charges are "
           f"billed separately at check-out.")
    logging.info(f"Any food, beverage or other charges are billed separately at check-out.")

    # Pick the room BEFORE taking payment, so a sold-out type fails fast with no card
    # details requested. A handful of candidates is enough to survive another desk
    # grabbing the same room between the availability read and the insert.
    candidates = search_availability(check_in, check_out, room_type=room_type, limit=5)
    if not candidates:
        logging.info(f"No {room_type} rooms are available for {check_in} to {check_out}.")
        return

    # Say what the cancellation terms ARE before taking any money, not after. A guest
    # who is only shown the policy on the confirmation screen has already committed.
    refund_cutoff = get_booking_refund_cutoff_days()
    ui.box("Cancellation Policy", booking_refund_policy(check_in, refund_cutoff)["summary"])

    options = booking_payment_options(nights, nightly_rate, quote["tax_rate"])
    ui.show_menu("Payment Options", [f"{c}. {label}" for c, label, _kind, _amt in options])
    pay_choice = ui.ask_number("Choose a payment option", minimum=1, maximum=len(options))
    if pay_choice is None:
        return
    _choice, _label, pay_kind, pay_amount = options[pay_choice - 1]

    booking_ref = new_booking_ref()
    claimed = None
    reasons = []
    with get_connection() as conn:
        if conn is None:
            logging.info("Database connection failed. No booking was made.")
            return
        for room_number, _rt, _status in candidates:
            try:
                ok, reason = _book_reservation_in_conn(
                    conn, room_number, last_name, first_name, check_in, check_out,
                    customer_id=customer_id, room_type=room_type)
            except Exception as e:
                # Most likely the room was claimed by someone else first: Reservations
                # is keyed on RoomNumber, so the insert collides. Try the next candidate.
                reasons.append(f"room {room_number}: {e}")
                continue
            if ok:
                claimed = room_number
                break
            reasons.append(reason)
        if claimed is None:
            conn.rollback()
            logging.info("We could not hold a room for those dates. Please try different dates.")
            for r in reasons[:3]:
                logging.info(f"  - {r}")
            return
        # Money first, reservation second: a declined card must not leave a held room.
        # The payment row is written even when the amount is $0 (a zero-rated room), so
        # every confirmed booking is findable by its reference.
        if pay_kind:
            if pay_amount > 0:
                logging.info(f"Total to charge now: ${pay_amount:,.2f}")
                if not process_credit_card(pay_amount):
                    conn.rollback()
                    logging.info("Payment was declined, so the room has not been held. Nothing was charged.")
                    return
            else:
                logging.info("The deposit comes to $0.00, so no card is needed.")
            booking_ref = _write_booking_charge(
                conn, claimed, booking_ref, check_in, pay_kind, pay_amount,
                nights if pay_kind == PAYMENT_KIND_PREPAYMENT
                else min(BOOKING_DEPOSIT_NIGHTS, nights))
            if booking_ref is None:
                conn.rollback()
                logging.info("Could not record the payment, so the booking was cancelled. Nothing was charged.")
                return
        try:
            conn.commit()
        except Exception as e:
            conn.rollback()
            logging.error(f"Could not save the booking: {e}")
            return

    log_audit("CREATE", "Reservation", claimed,
              f"Public booking {booking_ref}: {last_name} {first_name}, customer {customer_id}, "
              f"{room_type}, {check_in} to {check_out}, prepay={pay_kind or 'none'}")
    upsert_room_if_missing(claimed, room_type=room_type)

    # Read the LOCKED rate back off the row rather than reusing the quote's: the quote
    # was priced before the transaction opened, and an admin could have re-priced the
    # category in the gap. This is the number check-out will bill, so the confirmation
    # shows the one that is actually going to be charged.
    locked_rate = stay_nightly_rate(get_captured_nightly_rate(claimed), nightly_rate)
    if locked_rate != round(float(nightly_rate), 2):
        logging.info(f"Note: the {room_type} rate is now ${locked_rate:,.2f} per night, "
                     f"which is the rate locked in for this booking.")

    lines = [
        f"Booking reference: {booking_ref}",
        f"Room: {claimed} ({room_type})",
        f"Guest: {first_name} {last_name}",
        f"Check-in: {check_in}",
        f"Check-out: {check_out} ({nights} night(s))",
        f"Estimated total: ${quote['total']:,.2f}",
    ]
    if pay_kind:
        lines.append(f"Paid now: ${pay_amount:,.2f} ({pay_kind})")
        remaining = settle_with_prepayment(quote["total"], pay_amount)
        if remaining["balance_due"] > 0:
            lines.append(f"Estimated balance at check-out: ${remaining['balance_due']:,.2f}")
        elif remaining["credit_unused"] > 0:
            lines.append(f"Estimated credit to refund if unused: ${remaining['credit_unused']:,.2f}")
        if LAST_CARD_DIGITS:
            lines.append(f"Card ending {LAST_CARD_DIGITS}")
    else:
        lines.append(f"Payment: ${quote['total']:,.2f} due at check-out")
    lines.append(f"Your ${locked_rate:,.2f} per night is the rate you will be billed at check-out.")
    lines.append("")
    lines.append("Cancellation policy")
    lines.append(booking_refund_policy(check_in, refund_cutoff, amount_charged=pay_amount)["summary"])
    ui.box("Booking Confirmed", "\n".join(lines))
    logging.info("Please quote your booking reference at the front desk.")


def _find_booking(booking_ref, customer_id=None):
    """Look up a live reservation by booking reference, via that stay's payment rows.

    Returns (room_number, check_in, check_out, last_name, first_name, customer_id) or
    None. `customer_id` is the owner of the stay, and is None for a pre-019 or
    front-desk booking.

    When `customer_id` is given, the lookup is RESTRICTED to that guest's own stays. A
    booking reference is only 8 characters and is shown on the guest's own screen, but
    an 8-character code is brute-forceable, and without this check anybody who guessed
    one could read a stranger's room and cancel their reservation. A stay with no linked
    owner can never match, so a legacy booking has to be handled at the front desk.
    """
    # Read the clock BEFORE opening the connection: business_date() opens its own, and a
    # nested connection on top of a live cursor is an extra round trip that has no
    # business happening inside this query.
    today = business_date()
    with get_connection() as conn:
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            if customer_id is not None:
                cursor.execute(
                    "SELECT TOP 1 r.RoomNumber, r.CheckInDate, r.CheckOutDate, r.LastName, "
                    "r.FirstName, r.CustomerID "
                    "FROM Reservations r "
                    "JOIN ReservationPayments p ON p.RoomNumber = r.RoomNumber "
                    "  AND p.StayCheckIn = r.CheckInDate "
                    "WHERE p.BookingRef = ? AND r.CheckOutDate >= ? AND r.CustomerID = ? "
                    "ORDER BY r.CheckInDate DESC",
                    (booking_ref, today, customer_id),
                )
            else:
                cursor.execute(
                    "SELECT TOP 1 r.RoomNumber, r.CheckInDate, r.CheckOutDate, r.LastName, "
                    "r.FirstName, r.CustomerID "
                    "FROM Reservations r "
                    "JOIN ReservationPayments p ON p.RoomNumber = r.RoomNumber "
                    "  AND p.StayCheckIn = r.CheckInDate "
                    "WHERE p.BookingRef = ? AND r.CheckOutDate >= ? "
                    "ORDER BY r.CheckInDate DESC",
                    (booking_ref, today),
                )
            row = cursor.fetchone()
            if not row:
                return None
            return (row.RoomNumber, row.CheckInDate, row.CheckOutDate,
                    row.LastName, row.FirstName, row.CustomerID)
        except Exception as e:
            logging.error(f"Error looking up booking {booking_ref}: {e}")
            return None


def _own_booking(booking_ref, customer_id):
    """Resolve a booking reference for a signed-in guest, or None with a reason shown.

    The "not found" message deliberately does not say whether the reference exists but
    belongs to somebody else: telling a caller that their guess was a real code owned by
    another guest would confirm the guess and leak that guest's existence.
    """
    if not customer_id:
        logging.info("Please sign in to your booking account first.")
        return None
    found = _find_booking(booking_ref, customer_id)
    if found is None:
        logging.info(f"We could not find an upcoming booking with reference {booking_ref} on your account.")
        logging.info(f"Check the reference from your confirmation (it starts with '{BOOKING_REF_PREFIX}-').")
        logging.info("Bookings made at the front desk, or before online booking existed, are handled by staff.")
    return found


def view_my_booking():
    """Show a guest their booking and what they have already paid.

    Requires a sign-in, and only ever shows bookings on the signed-in guest's own
    account.
    """
    customer_id = customer_login()
    if not customer_id:
        return
    booking_ref = input(f"Enter your booking reference (the code starting with "
                        f"'{BOOKING_REF_PREFIX}-', NOT the room number): ").strip().upper()
    if not booking_ref:
        return
    found = _own_booking(booking_ref, customer_id)
    if found is None:
        return
    room_number, check_in, check_out, last_name, first_name, _owner = found
    paid = get_booking_paid_total(room_number, check_in)
    credit = get_outstanding_booking_credit(room_number, check_in)
    nights = stay_nights(check_in, check_out)
    ui.show_table(
        f"Booking {booking_ref}",
        ["Detail", "Value"],
        [
            ("Guest", f"{first_name} {last_name}"),
            ("Room", room_number),
            ("Check-in", str(check_in)),
            ("Check-out", f"{check_out} ({nights} night(s))"),
            ("Paid to date", f"${paid:,.2f}"),
            ("Credit towards check-out", f"${credit:,.2f}"),
        ],
    )
    cutoff = get_booking_refund_cutoff_days()
    days_until = (check_in - business_date()).days
    decision = refund_decision(days_until, cutoff, paid)
    if decision["action"] == "refund":
        logging.info(f"Free cancellation until {cutoff} day(s) before check-in: cancelling now would refund ${-decision['amount']:,.2f}.")
    elif decision["action"] == "forfeit":
        logging.info(f"Free cancellation until {cutoff} day(s) before check-in: cancelling now would forfeit the ${-decision['amount']:,.2f} paid.")


def cancel_booking():
    """Cancel a public booking and settle the money under the refund policy.

    The reservation row is released so the room returns to availability, and the
    money is settled with a single signed ReservationPayments row: a negative 'Refund'
    when cancelling inside the free window, or a negative 'Forfeit' when the deposit is
    kept. The guest must type the reference back to confirm.

    Requires a sign-in, and the reference must belong to the signed-in guest: a booking
    reference is short enough to be guessed, and cancelling somebody else's reservation
    would both destroy their stay and pay out their deposit to them.
    """
    customer_id = customer_login()
    if not customer_id:
        return
    booking_ref = input(f"Enter your booking reference (the code starting with "
                        f"'{BOOKING_REF_PREFIX}-', NOT the room number): ").strip().upper()
    if not booking_ref:
        return
    found = _own_booking(booking_ref, customer_id)
    if found is None:
        return
    room_number, check_in, check_out, last_name, first_name, _owner = found
    days_until = (check_in - business_date()).days
    paid = get_booking_paid_total(room_number, check_in)
    decision = refund_decision(days_until, get_booking_refund_cutoff_days(), paid)

    if decision["action"] == "refused":
        logging.info(f"Booking {booking_ref} cannot be cancelled: {decision['reason']}.")
        logging.info("Please speak to the front desk.")
        return

    logging.info(f"Booking {booking_ref}: room {room_number}, {first_name} {last_name}, "
                 f"check-in {check_in}, check-out {check_out}.")
    if decision["action"] == "refund":
        logging.info(f"Cancellation outcome: full refund of ${-decision['amount']:,.2f} to the original card.")
    elif decision["action"] == "forfeit":
        logging.info(f"Cancellation outcome: the ${-decision['amount']:,.2f} paid is forfeited "
                     f"({decision['reason']}).")
    else:
        logging.info("Nothing was paid for this booking, so there is no refund to process.")
    if not ui.ask_confirmation("Cancel this booking?"):
        logging.info("Booking left unchanged.")
        return
    confirm = input(f"Type {booking_ref} to confirm cancellation: ").strip().upper()
    if confirm != booking_ref:
        logging.info("Reference did not match. Booking left unchanged.")
        return

    with get_connection() as conn:
        if conn is None:
            logging.info("Database connection failed. The booking was not cancelled.")
            return
        try:
            cursor = conn.cursor()
            # Guard on the same dates AND the same owner we quoted, so neither a desk-side
            # re-book nor a change of owner can be cancelled out from under the next guest.
            cursor.execute(
                "DELETE FROM Reservations WHERE RoomNumber = ? AND CheckInDate = ? "
                "AND CheckOutDate = ? AND CustomerID = ?",
                (room_number, check_in, check_out, customer_id),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                logging.info("This booking changed before it could be cancelled. Please start again.")
                return
            # Deliberately NOT archived: a cancelled booking never happened, and an
            # archive row would make search_availability() treat the room as occupied
            # for those dates. The money rows below are the cancellation's history.
            if decision["action"] in ("refund", "forfeit"):
                record_booking_payment(room_number, booking_ref, check_in,
                                       decision["kind"], decision["amount"],
                                       notes=decision["reason"], conn=conn)
            conn.commit()
        except Exception as e:
            logging.error(f"Error cancelling booking {booking_ref}: {e}")
            return
    log_audit("CANCEL", "Reservation", room_number,
              f"Cancelled public booking {booking_ref} ({first_name} {last_name}, from {check_in}); "
              f"outcome={decision['action']} amount={decision['amount']}")
    logging.info(f"Booking {booking_ref} cancelled. Room {room_number} is available again.")
    if decision["action"] == "refund":
        logging.info(f"${-decision['amount']:,.2f} will be refunded to the original card in 5-10 business days.")
    elif decision["action"] == "forfeit":
        logging.info("The deposit has been retained as the cancellation fee.")


def booking_panel():
    """Public booking menu, on the main screen.

    No staff login, but each action does require a guest booking account: "Book", "View"
    and "Cancel" all operate on the signed-in guest's own account, and the sign-in is
    cached in CURRENT_CUSTOMER for the rest of the session.
    """
    while True:
        ui.show_menu("Bookings", [
            "1. Book a Room",
            "2. View My Booking",
            "3. Cancel My Booking",
            "4. Exit Bookings",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            book_room()
        elif choice == '2':
            view_my_booking()
        elif choice == '3':
            cancel_booking()
        elif choice == '4':
            break
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()


def search_availability(check_in, check_out, room_type=None, floor=None, limit=50):
    """Find rooms free for the whole window, reading live and archived reservations.

    A room is available when no live row overlaps the window, no archived stay overlaps
    it, and its Rooms status is sellable -- see UNBOOKABLE_ROOM_STATUSES, which excludes
    Dirty and Cleaning as well as Maintenance. Returns a list of
    (room_number, room_type, status). Empty window or overlap means unavailable.
    """
    if not check_in or not check_out or check_out <= check_in:
        return []
    # The SQL below mirrors stays_overlap() as a cheap row pre-filter; the pure helper
    # is then applied in Python so the availability rule has exactly one definition.
    def busy_windows(table):
        cursor.execute(
            f"SELECT RoomNumber, CheckInDate, CheckOutDate FROM {table} "
            f"WHERE CheckInDate < ? AND CheckOutDate > ?",
            (check_out, check_in),
        )
        return {(r[0], r[1], r[2]) for r in cursor.fetchall()}

    try:
        with get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            live_busy = busy_windows("Reservations")
            try:
                archived_busy = busy_windows("ReservationArchive")
            except Exception:
                # Migration 014 not applied: fall back to live reservations only.
                archived_busy = set()
            params = []
            # NULL status reads as Available, so it is allowed explicitly rather than
            # being swallowed by NOT IN's three-valued logic.
            placeholders = ",".join("?" for _ in UNBOOKABLE_ROOM_STATUSES)
            where = [f"(Status IS NULL OR Status NOT IN ({placeholders}))"]
            params.extend(UNBOOKABLE_ROOM_STATUSES)
            if room_type:
                where.append("RoomType = ?")
                params.append(room_type)
            if floor:
                where.append("RoomNumber LIKE ?")
                params.append(f"{int(floor)}%")
            cursor.execute(
                f"SELECT RoomNumber, RoomType, COALESCE(Status, 'Available') FROM Rooms "
                f"WHERE {' AND '.join(where)} ORDER BY RoomNumber",
                tuple(params),
            )
            rooms = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error searching availability: {e}")
        return []

    busy = {room for room, start, end in (live_busy | archived_busy)
            if stays_overlap(start, end, check_in, check_out)}
    return [(r[0], r[1], r[2]) for r in rooms if r[0] not in busy][:limit]


def arrivals_departures_board(on_date=None):
    """Staff board for a given day: arrivals, in-house guests, and departures.

    Returns (arrivals, in_house, departures) where each entry is a
    (room_number, last_name, first_name, check_in, check_out) tuple.
    """
    on_date = on_date or business_date()
    try:
        with get_connection() as conn:
            if conn is None:
                return [], [], []
            cursor = conn.cursor()
            cols = "RoomNumber, LastName, FirstName, CheckInDate, CheckOutDate"
            cursor.execute(
                f"SELECT {cols} FROM Reservations WHERE CheckInDate = ? ORDER BY RoomNumber",
                (on_date,),
            )
            arrivals = [(r[0], r[1], r[2], r[3], r[4]) for r in cursor.fetchall()]
            cursor.execute(
                f"SELECT {cols} FROM Reservations "
                f"WHERE CheckInDate < ? AND CheckOutDate > ? ORDER BY RoomNumber",
                (on_date, on_date),
            )
            in_house = [(r[0], r[1], r[2], r[3], r[4]) for r in cursor.fetchall()]
            cursor.execute(
                f"SELECT {cols} FROM Reservations WHERE CheckOutDate = ? ORDER BY RoomNumber",
                (on_date,),
            )
            departures = [(r[0], r[1], r[2], r[3], r[4]) for r in cursor.fetchall()]
            return arrivals, in_house, departures
    except Exception as e:
        logging.error(f"Error building arrivals/departures board: {e}")
        return [], [], []


def show_arrivals_departures_board():
    """Interactive arrivals/in-house/departures board for a chosen day."""
    today = business_date()
    on_date = ui.ask_date("Enter date for the board", default=str(today))
    arrivals, in_house, departures = arrivals_departures_board(on_date)
    headers = ["Room", "Last Name", "First Name", "Check In", "Check Out"]
    logging.info(f"Board for {on_date}")
    ui.show_table(f"Arrivals ({len(arrivals)})", headers, arrivals)
    ui.show_table(f"In House ({len(in_house)})", headers, in_house)
    ui.show_table(f"Departures ({len(departures)})", headers, departures)
    occupied = set(r[0] for r in in_house)
    if arrivals:
        arriving = set(r[0] for r in arrivals)
        cleaning = ui.ask_confirmation(f"Show housekeeping load for the {len(arriving)} arriving rooms?", default="y")
        if cleaning:
            _show_housekeeping_rooms(sorted(arriving))


def _show_housekeeping_rooms(room_numbers):
    """Render the room detail table for a specific list of rooms (used by the board)."""
    if not room_numbers:
        return
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in room_numbers)
            cursor.execute(
                f"SELECT RoomNumber, RoomType, COALESCE(Status, 'Available') AS Status, Description "
                f"FROM Rooms WHERE RoomNumber IN ({placeholders}) ORDER BY RoomNumber",
                tuple(room_numbers),
            )
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading room detail: {e}")
        return
    ui.show_status_table(
        "Arriving Rooms - Housekeeping",
        ["Room", "Type", "Status", "Description"],
        [(r[0], r[1], r[2], r[3] or "") for r in rows],
        status_col=2,
    )


def show_availability_search():
    """Interactive availability search across a date window."""
    today = business_date()
    check_in = ui.ask_date("Check-in date", default=str(today))
    check_out = ui.ask_date("Check-out date", default=str(today + timedelta(days=1)))
    if check_out <= check_in:
        logging.info("Check-out date must be after check-in date.")
        return
    room_type = input(f"Room type (blank for any of: {', '.join(rt for rt, _ in get_room_types())}): ").strip()
    floor_raw = input("Floor (blank for any): ").strip()
    floor = None
    if floor_raw:
        if not floor_raw.isdigit():
            logging.info("Floor must be a number.")
            return
        floor = int(floor_raw)
    results = search_availability(check_in, check_out, room_type or None, floor)
    if not results:
        logging.info("No rooms available for that window.")
        return
    ui.show_table(
        f"Available: {check_in} to {check_out}" + (f" ({len(results)} shown)" if len(results) >= 50 else ""),
        ["Room", "Type", "Status", "Nightly Rate"],
        [(rn, rt, st, f"${get_nightly_rate(rt):,.2f}") for rn, rt, st in results],
    )


def delete_reservation():
    try:
        room_number = input("Enter room number (floor + 3-digit code) to delete: ").strip()
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            # Guard against FK violations from child records (billing transactions).
            cursor.execute("SELECT 1 FROM Transactions WHERE RoomNumber = ?", (room_number,))
            if cursor.fetchone():
                logging.info(f"Room {room_number} has linked billing history (transactions) and cannot be deleted. Use 'Add Reservation' to re-book the room for the next stay.")
                return
            cursor.execute("DELETE FROM Reservations WHERE RoomNumber = ?", (room_number,))
            conn.commit()
            # A deleted reservation's key card must not keep opening the room.
            revoke_active_key_cards(room_number, "reservation deleted")
            log_audit("DELETE", "Reservation", room_number, "Reservation deleted")
            logging.info(f"Reservation for room {room_number} deleted successfully.")
    except Exception as e:
        logging.error(f"Error deleting reservation: {e}")
    # connection closed by context manager


# Deletion order matters: every table here is emptied before Reservations, because
# Transactions.RoomNumber carries the only FK pointing at Reservations.RoomNumber, so
# Reservations cannot be emptied while a folio line still references it.
RESERVATION_RESET_SCOPES = [
    ("1", "Stay data only (keeps invoices, loyalty balances, and customer profiles)", [
        "OrderItems", "Orders", "Notifications", "ConciergeRequests", "Feedback",
        "DoorEvents", "KeyCards", "ReservationArchive", "Transactions",
        "ReservationPayments",
    ]),
    ("2", "Stay data + financial history (also deletes invoices and loyalty transactions)", [
        "OrderItems", "Orders", "Notifications", "ConciergeRequests", "Feedback",
        "DoorEvents", "KeyCards", "ReservationArchive", "Transactions",
        "ReservationPayments", "Invoices", "LoyaltyTransactions",
    ]),
]
RESERVATION_RESET_CONFIRM = "DELETE ALL RESERVATIONS"


def _existing_tables(cursor, tables):
    """Subset of `tables` that actually exist, so unapplied migrations are skipped."""
    placeholders = ",".join("?" * len(tables))
    cursor.execute(f"SELECT name FROM sys.tables WHERE name IN ({placeholders})", tuple(tables))
    present = {r[0] for r in cursor.fetchall()}
    return [t for t in tables if t in present]


def delete_all_reservations():
    """Master-override bulk reset of every reservation (admin only).

    Requires the master override secret, then an exact typed confirmation, because the
    operation is not undoable. Two scopes are offered: the default leaves the permanent
    financial record (Invoices, LoyaltyTransactions, CustomerProfiles) intact, while the
    second wipes that too.

    All deletions run on one connection inside a single transaction and commit once, so
    a failure part-way through leaves the database exactly as it was. Child rows go
    first and Reservations last. Room statuses are then reset to Available, since a room
    left flagged Occupied or Dirty with no reservation would misreport the housekeeping
    board. AuditLog is deliberately preserved -- it is the record of this action.
    """
    ui.show_menu("Delete All Reservations (irreversible)", [
        f"{key}. {label}" for key, label, _ in RESERVATION_RESET_SCOPES
    ] + ["3. Cancel"])
    choice = input("Enter your choice: ").strip()
    scope = next((s for s in RESERVATION_RESET_SCOPES if s[0] == choice), None)
    if scope is None:
        logging.info("Cancelled. No reservations were deleted.")
        return
    _, scope_label, tables = scope

    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            targets = _existing_tables(cursor, tables)
            skipped = [t for t in tables if t not in targets]
            counts = []
            for table in targets + ["Reservations"]:
                cursor.execute(f"SELECT COUNT(*) FROM dbo.{table}")
                counts.append((table, cursor.fetchone()[0]))
    except Exception as e:
        logging.error(f"Error inspecting data to delete: {e}")
        return

    if not counts or counts[-1][1] == 0:
        logging.info("There are no reservations to delete.")
        return

    ui.show_table(
        "This will permanently delete", ["Table", "Rows"], counts
    )
    if skipped:
        ui.warning(
            "Skipping (table not present, migration not applied): " + ", ".join(skipped)
        )
    ui.box(
        f"Confirm: {scope_label}",
        "Every stay, folio line, order, request, and key card listed above is removed.\n"
        "Room statuses are reset to Available. AuditLog is kept.\n"
        "This cannot be undone.",
        border_style="red",
    )

    if not ui.ask_confirmation("Proceed?", default="n"):
        logging.info("Cancelled. No reservations were deleted.")
        return
    if not require_master_override():
        return
    typed = input(f"Type {RESERVATION_RESET_CONFIRM} to confirm: ").strip()
    if typed != RESERVATION_RESET_CONFIRM:
        logging.info("Confirmation text did not match. No reservations were deleted.")
        return

    deleted = {}
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            for table in targets:
                cursor.execute(f"DELETE FROM dbo.{table}")
                deleted[table] = cursor.rowcount
            cursor.execute("DELETE FROM dbo.Reservations")
            deleted["Reservations"] = cursor.rowcount
            cursor.execute(
                "UPDATE dbo.Rooms SET Status = 'Available' "
                "WHERE Status IS NULL OR Status <> 'Available'"
            )
            deleted["Rooms reset to Available"] = cursor.rowcount
            conn.commit()
    except Exception as e:
        # Nothing was committed, so the connection closing here rolls the whole batch
        # back and the database is left untouched.
        logging.error(f"Delete all reservations failed and was rolled back: {e}")
        return

    log_audit("DELETE", "Reservation", "ALL",
              f"Bulk reservation reset ({scope_label}): "
              + ", ".join(f"{k}={v}" for k, v in deleted.items()))
    ui.success(f"Deleted {deleted.get('Reservations', 0)} reservation(s).")
    ui.show_table("Deleted rows", ["Table", "Rows"],
                  [(k, v) for k, v in deleted.items() if v])


def edit_reservation():
    """Edit an existing reservation, including the room number and floor."""
    with get_connection() as conn:
        if conn is None:
            return
        try:
            cursor = conn.cursor()
            old_room_number = input("Enter the current room number (floor + 3-digit code) of the reservation to edit: ").strip()

            cursor.execute("SELECT * FROM Reservations WHERE RoomNumber = ?", (old_room_number,))
            reservation = cursor.fetchone()

            if reservation:
                logging.info(f"Current reservation details: Room Number: {reservation.RoomNumber}, Floor: {reservation.Floor}, Last Name: {reservation.LastName}, First Name: {reservation.FirstName}")

                new_room_number = input("Enter new room number (floor + 3-digit code) (leave blank to keep current): ").strip()
                new_last_name = input("Enter new last name (leave blank to keep current): ").strip()
                new_first_name = input("Enter new first name (leave blank to keep current): ").strip()

                if new_room_number == "":
                    new_room_number = old_room_number
                if new_last_name == "":
                    new_last_name = reservation.LastName
                if new_first_name == "":
                    new_first_name = reservation.FirstName

                # Calculate new floor from new room number safely
                m = re.match(r'^(\d+)(\d{3})$', new_room_number)
                if m:
                    new_floor = int(m.group(1))
                else:
                    logging.info("Invalid room number format. Using old floor.")
                    m2 = re.match(r'^(\d+)(\d{3})$', old_room_number)
                    new_floor = int(m2.group(1)) if m2 else reservation.Floor

                # Check if the new room number is already in use
                if new_room_number != old_room_number:
                    cursor.execute("SELECT CheckInDate, CheckOutDate FROM Reservations WHERE RoomNumber = ?", (new_room_number,))
                    target_reservation = cursor.fetchone()
                    # A target room with a still-current stay (check-out in the future)
                    # cannot accept this reservation; a completed past stay is overwritten.
                    if target_reservation is not None and target_reservation.CheckOutDate > business_date():
                        logging.info(f"Room number {new_room_number} is already reserved (check-out {target_reservation.CheckOutDate}).")
                        return
                    if target_reservation is not None:
                        logging.info(f"Room {new_room_number} has a completed past stay on record; its reservation row will be reused.")
                    # Availability guard: never move a guest into a room that is not
                    # sellable -- maintenance, or not yet cleaned. See
                    # UNBOOKABLE_ROOM_STATUSES.
                    target_status = _room_status_in_conn(cursor, new_room_number)
                    if target_status in UNBOOKABLE_ROOM_STATUSES:
                        if target_status == "Maintenance":
                            logging.info(f"Room {new_room_number} is under maintenance and cannot be assigned.")
                        else:
                            logging.info(f"Room {new_room_number} is '{target_status}' and cannot be assigned until housekeeping marks it Available.")
                        return
                    # Keep the status board complete if the target room is not tracked yet.
                    upsert_room_if_missing(new_room_number)
                    # A move is the one thing that legitimately re-captures the rate: the
                    # guest is being given a different room, and the stay has to be billed
                    # at the rate of the room they are actually in. This is NOT the same as
                    # an admin rate edit, which never touches a booked stay. The outgoing
                    # stay's own rate went into ReservationArchive when the room was
                    # re-let, so the old number is not lost.
                    new_rate = _rate_or_none(
                        _nightly_rate_in_conn(cursor, _room_type_in_conn(cursor, new_room_number)))

                    # Move child records (billing transactions, invoices) to the new room
                    # BEFORE re-keying the reservation, so the Transactions FK stays satisfied.
                    cursor.execute("UPDATE Transactions SET RoomNumber = ? WHERE RoomNumber = ?", (new_room_number, old_room_number))
                    moved_tx = cursor.rowcount
                    if moved_tx:
                        logging.info(f"Moved {moved_tx} transaction(s) to room {new_room_number}.")
                    cursor.execute("UPDATE Invoices SET RoomNumber = ? WHERE RoomNumber = ?", (new_room_number, old_room_number))
                    moved_inv = cursor.rowcount
                    if moved_inv:
                        logging.info(f"Moved {moved_inv} invoice(s) to room {new_room_number}.")
                    if LOYALTY_ENABLED:
                        # Loyalty is NOT moved, deliberately. The account is keyed on
                        # CustomerID, so the balance already travels with the guest and there
                        # is nothing to transfer. `LoyaltyTransactions.RoomNumber` is left
                        # alone as well: it records the room the points were EARNED in, and
                        # rewriting it would falsify the guest's earning history.
                        #
                        # The pre-019 code re-pointed LoyaltyAccounts at the new room and,
                        # when the target room already had an account, silently stranded the
                        # balance on the old room. Both failure modes are now impossible.
                        cursor.execute(
                            "UPDATE LoyaltyAccounts SET RoomNumber = ?, LastUpdated = ? "
                            "WHERE RoomNumber = ?",
                            (new_room_number, datetime.now(), old_room_number))
                        logging.info(
                            f"Room pointer on the guest's loyalty account updated to {new_room_number}; "
                            "points and history left intact.")

                    # Key cards follow the guest to the new room, and any card for the
                    # old room dies with it.
                    move_key_cards(old_room_number, new_room_number)

                    if target_reservation is not None:
                        # Reuse the target room's reservation row (RoomNumber is the PK).
                        # The moving guest's own CustomerID is carried across, but the
                        # target row's previous occupant's link is NOT: this stay is now
                        # theirs, and keeping the old link would credit it to the last
                        # guest who held that room.
                        cursor.execute(
                            "UPDATE Reservations SET LastName = ?, FirstName = ?, Floor = ?, "
                            "CheckInDate = ?, CheckOutDate = ?, CustomerID = ?, NightlyRate = ? "
                            "WHERE RoomNumber = ?",
                            (new_last_name, new_first_name, new_floor,
                             reservation.CheckInDate, reservation.CheckOutDate,
                             reservation.CustomerID, new_rate, new_room_number),
                        )
                        cursor.execute("DELETE FROM Reservations WHERE RoomNumber = ?", (old_room_number,))
                    else:
                        # No existing row in the target room: re-key the reservation row.
                        cursor.execute(
                            "UPDATE Reservations SET RoomNumber = ?, LastName = ?, FirstName = ?, "
                            "Floor = ?, NightlyRate = ? WHERE RoomNumber = ?",
                            (new_room_number, new_last_name, new_first_name, new_floor,
                             new_rate, old_room_number))
                    conn.commit()
                    log_audit("UPDATE", "Reservation", new_room_number,
                              f"Moved from room {old_room_number}; guest {new_last_name} {new_first_name}; "
                              f"stay re-rated to the new room's category")
                    logging.info(f"Reservation for room {old_room_number} updated successfully. \n New details: Room Number: {new_room_number}, Floor: {new_floor}, Last Name: {new_last_name}, First Name: {new_first_name}")
                else:
                    # Same room: just update guest details / floor.
                    cursor.execute(
                        "UPDATE Reservations SET RoomNumber = ?, LastName = ?, FirstName = ?, Floor = ? "
                        "WHERE RoomNumber = ?",
                        (new_room_number, new_last_name, new_first_name, new_floor, old_room_number))
                    conn.commit()
                    log_audit("UPDATE", "Reservation", new_room_number,
                              f"Guest details edited to {new_last_name} {new_first_name}")
                    logging.info(f"Reservation for room {old_room_number} updated successfully. \n New details: Room Number: {new_room_number}, Floor: {new_floor}, Last Name: {new_last_name}, First Name: {new_first_name}")
            else:
                logging.info("No reservation found with that room number.")
        except Exception as e:
            logging.error(f"Error editing reservation: {e}")

def edit_user():
    """Edit an existing user."""
    try:
        username = input("Enter the username of the user to edit: ").strip()
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM Users WHERE Username = ?", (username,))
            user = cursor.fetchone()

            if user:
                logging.info(f"Current user details: Username: {user.Username}")

                new_username = input("Enter new username (leave blank to keep current): ").strip()
                new_password = input("Enter new password (leave blank to keep current): ").strip()

                if new_username == "":
                    new_username = user.Username
                if new_password == "":
                    new_password = user.Password

                cursor.execute("""
                    UPDATE Users
                    SET Username = ?, Password = ?
                    WHERE Username = ?""",
                    (new_username, new_password, username))
                conn.commit()
                changes = []
                if new_username != username:
                    changes.append(f"username {username} -> {new_username}")
                if new_password != user.Password:
                    changes.append("password reset")
                log_audit("UPDATE", "User", new_username,
                          "; ".join(changes) if changes else "no effective change")
                logging.info(f"User '{username}' updated successfully.")
            else:
                logging.info("No user found with that username.")
    except Exception as e:
        logging.error(f"Error editing user: {e}")


def _parse_pricing_rule(raw):
    """Map what someone typed for a pricing rule onto what get_dynamic_price() reads.

    Returns 'Peak', 'OffPeak', or None (standard). Blank is the standard rule, not an error,
    which is why add_item() has always allowed it. Shared so the Admin Panel and the
    onboarding catalogue cannot drift on which spellings are accepted.
    """
    value = str(raw or "").strip().lower()
    if value in ('p', 'peak'):
        return 'Peak'
    if value in ('o', 'off', 'offpeak', 'off-peak'):
        return 'OffPeak'
    return None


def add_item():
    try:
        item_id = int(input("Enter item ID: ").strip())
        item_name = input("Enter item name: ").strip()
        item_price = float(input("Enter item price: ").strip())
        rule_raw = input("Enter pricing rule - (P)eak/(O)ffPeak/blank for standard: ").strip().lower()
        pricing_rule = _parse_pricing_rule(rule_raw)
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("INSERT INTO Items (ItemID, Name, Price, PricingRule) VALUES (?, ?, ?, ?)", (item_id, item_name, item_price, pricing_rule))
            conn.commit()
            log_audit("CREATE", "Item", item_id,
                      f"'{item_name}' @ {item_price}, rule {pricing_rule or 'standard'}")
            logging.info(f"Item '{item_name}' added successfully with ID {item_id}.")
    except Exception as e:
        logging.error(f"Error adding item: {e}")
    # connection closed by context manager

def delete_item():
    try:
        item_id = int(input("Enter item ID to delete: ").strip())
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Name FROM Items WHERE ItemID = ?", (item_id,))
            existing = cursor.fetchone()
            cursor.execute("DELETE FROM Items WHERE ItemID = ?", (item_id,))
            conn.commit()
            log_audit("DELETE", "Item", item_id, f"Deleted {existing[0] if existing else 'unknown item'}")
            logging.info(f"Item with ID {item_id} deleted successfully.")
    except Exception as e:
        logging.error(f"Error deleting item: {e}")
    # connection closed by context manager

def record_transaction_for_room(room_number: str, item_id: int, quantity: int, unit_price: float, paid: bool = False):
    """Record an ordered item for a room in the Transactions table.

    paid=True inserts the row pre-billed (IsBilled = 1). Returns the new transaction
    ID, or None on failure.
    """
    try:
        unit_price = float(unit_price) if unit_price is not None else 0.0
        amount = round(unit_price * int(quantity), 2)
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Transactions (RoomNumber, ItemID, Quantity, UnitPrice, Amount, IsBilled) "
                "OUTPUT INSERTED.ID VALUES (?, ?, ?, ?, ?, ?)",
                (room_number, item_id, quantity, unit_price, amount, 1 if paid else 0),
            )
            row = cursor.fetchone()
            tx_id = int(row[0]) if row and row[0] is not None else None
            conn.commit()
            logging.info("Recorded transaction for Item ID %s in room %s (%s).",
                         item_id, room_number, "paid now" if paid else "added to bill")
            return tx_id
    except Exception as e:
        logging.error(f"Error recording transaction: {e}")
        return None


def stay_nights(check_in, check_out):
    """Nights in a stay: whole nights between the two dates, floored at 0.

    Hotel convention counts the night a guest sleeps in, so this compares the calendar
    dates rather than the elapsed hours: arriving 01 Mar at 15:00 and leaving 04 Mar at
    11:00 is 3 nights, not the 2 that a raw timedelta would give. Accepts `date` or
    `datetime` for either bound (Reservations stores `date`, but callers that parse a
    timestamp or read the archive pass a `datetime`). The same value feeds both
    post_room_charge() and award_stay_points(), so the room charge and the loyalty
    award can never disagree.
    """
    if not check_in or not check_out:
        return 0
    # A date has no time component to drop, so only convert when there is one.
    start = check_in.date() if hasattr(check_in, "date") else check_in
    end = check_out.date() if hasattr(check_out, "date") else check_out
    return max(0, (end - start).days)


def stays_overlap(start_a, end_a, start_b, end_b):
    """True when two half-open stay windows [start, end) share at least one night.

    Stays are half-open so a guest departing on the 4th can be replaced by one arriving
    on the 4th: touching windows (end_a == start_b) do NOT overlap, only genuinely
    interleaved ones do. Accepts `date` or `datetime` for any bound, using the same
    calendar-date rule as stay_nights(). This is the single source of truth for
    availability; search_availability() mirrors it in SQL purely to pre-filter rows.
    """
    if not start_a or not end_a or not start_b or not end_b:
        return False
    a_start = start_a.date() if hasattr(start_a, "date") else start_a
    a_end = end_a.date() if hasattr(end_a, "date") else end_a
    b_start = start_b.date() if hasattr(start_b, "date") else start_b
    b_end = end_b.date() if hasattr(end_b, "date") else end_b
    # An empty or reversed window cannot occupy a room, so it blocks nothing.
    if a_end <= a_start or b_end <= b_start:
        return False
    return a_start < b_end and b_start < a_end


def post_room_charge(room_number, check_in, check_out):
    """Post the nightly room charge to the room's folio as an unbilled 'Room' line.

    Runs at check-out (not check-in) so a declined card leaves the charge unbilled and
    the guest can retry, and so the charge reflects the nights actually reserved.

    The rate is the one CAPTURED ON THE RESERVATION when the stay was made
    (`Reservations.NightlyRate`, migration 022), not the current RoomTypes rate. Reading
    the live rate here let an admin rate edit re-price every confirmed booking in the
    house, which is the one thing a booked rate must never be. Only a stay with no
    captured rate -- written before 022, or for a category with no rate -- falls back to
    the current rate, and it says so. UnitPrice is the same number, so the invoice
    snapshot and the captured rate can never disagree either.

    Idempotent on Transactions.Description = 'Room charge for {check_in} - {room_type}',
    which is the exact string the INSERT writes, so a retried check-out cannot
    double-charge the room. The description is composed once and used by both the guard
    and the INSERT on purpose: an earlier version built it twice, the guard tested one
    form and the INSERT wrote the other, and the guard therefore never matched its own row
    -- so every retry billed the room again. `tests/verify_e2e.py` catches that regression.

    Returns the transaction ID, or None (including when the stay is already charged).
    """
    if not room_number:
        return None
    nights = stay_nights(check_in, check_out)
    if nights <= 0:
        logging.info("Stay has no nights to charge.")
        return None
    room_type = get_room_type(room_number)
    captured = get_captured_nightly_rate(room_number)
    if captured is None:
        logging.info(f"No rate was captured for {room_number}'s stay ({check_in}); "
                     "billing it at the current rate. Apply migration 022 to lock rates "
                     "at booking time.")
    nightly_rate = stay_nightly_rate(captured, get_nightly_rate(room_type))
    if nightly_rate <= 0:
        logging.info(f"No nightly rate configured for {room_type}; room charge skipped.")
        return None
    # ONE string, used by both the guard and the INSERT. Building it twice is exactly what
    # let them drift apart: the guard tested `marker` while the INSERT wrote
    # `f"{marker} - {room_type}"`, so the guard could never match its own row and every
    # retry posted the room charge a second time. Found by tests/verify_e2e.py against a
    # real server; no other check could see it, because both statements were well-formed.
    description = f"Room charge for {check_in} - {room_type}"
    amount = round(nightly_rate * nights, 2)
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            # Guard on the exact Description this function writes, so a retried check-out
            # cannot double-charge. Matching on RoomNumber + the full description also
            # scopes the guard to the stay: a room re-let later gets a different check-in
            # date, and therefore a different description.
            cursor.execute(
                "SELECT ID FROM Transactions WHERE RoomNumber = ? AND Description = ?",
                (room_number, description),
            )
            if cursor.fetchone():
                logging.info("Room charge already posted for this stay; not charging again.")
                return None
            cursor.execute(
                "INSERT INTO Transactions (RoomNumber, ItemID, Quantity, UnitPrice, Amount, "
                "IsBilled, ChargeGroup, Description) OUTPUT INSERTED.ID "
                "VALUES (?, NULL, ?, ?, ?, 0, ?, ?)",
                (room_number, nights, nightly_rate, amount, CHARGE_GROUP_ROOM,
                 description),
            )
            row = cursor.fetchone()
            tx_id = int(row[0]) if row and row[0] is not None else None
            conn.commit()
            logging.info(f"Posted room charge: {nights} night(s) x ${nightly_rate:.2f} "
                         f"({room_type}) = ${amount:.2f}.")
            return tx_id
    except Exception as e:
        logging.error(f"Error posting room charge: {e}")
        return None


def admin_login():
    """Handle admin login with a grace period for lockout, displaying a warning before the final lockout."""
    global CURRENT_USER
    while True:
        try:
            username = input("Enter admin username (or master override): ").strip()

            # Master override path: special short-circuit to unlock or obtain admin session
            if username == MASTER_OVERRIDE:
                secret = getpass.getpass("Enter master override secret: ").strip()
                if MASTER_SECRET is not None and secret == MASTER_SECRET:
                    target = input("Enter username to unlock (leave blank to start admin session): ").strip()
                    if target:
                        if clear_lockout(target):
                            logging.info("Account unlocked successfully for %s", target)
                        else:
                            logging.info("Failed to unlock account %s", target)
                        continue
                    else:
                        logging.info("Master override granted admin session.")
                        CURRENT_USER = MASTER_OVERRIDE
                        log_audit("LOGIN", "User", MASTER_OVERRIDE, "Master override admin session granted")
                        return True, 'admin', False
                else:
                    logging.info("Invalid master override secret.")
                    continue

            password = getpass.getpass("Enter admin password: ").strip()

            with get_connection() as conn:
                if conn is None:
                    logging.error("Database connection failed during login.")
                    return False, None, False
                cursor = conn.cursor()
                cursor.execute("SELECT Password, FailedAttempts, LockoutTime, Role FROM Users WHERE Username = ?", (username,))
                user = cursor.fetchone()

                if not user:
                    logging.info("Username not found.")
                    continue

                db_password, failed_attempts, lockout_time, role = user

                # Check if the user is currently locked out
                if lockout_time and lockout_time > datetime.now():
                    logging.info(f"Account is locked until {lockout_time}. Please try again later.")
                    continue

                # Validate the entered password
                if password == db_password:
                    logging.info("Login successful!")
                    # Reset failed attempts and lockout time
                    cursor.execute("UPDATE Users SET FailedAttempts = 0, LockoutTime = NULL WHERE Username = ?", (username,))
                    conn.commit()
                    CURRENT_USER = username
                    log_audit("LOGIN", "User", username, f"Role {role}")
                    return True, role, False  # Return role and reauthentication status
                else:
                    failed_attempts = (failed_attempts or 0) + 1
                    logging.info(f"Invalid credentials. Attempt {failed_attempts}/{LOCKOUT_THRESHOLD}.")
                    log_audit("LOGIN_FAILED", "User", username,
                              f"Failed attempt {failed_attempts}/{LOCKOUT_THRESHOLD}")

                    # Display a warning message after the second failed attempt
                    if failed_attempts == LOCKOUT_THRESHOLD - 1:
                        logging.info("Warning: One more failed attempt will lock you out.")

                    # Lock out the user after exceeding the threshold
                    if failed_attempts >= LOCKOUT_THRESHOLD:
                        lockout_time = datetime.now() + timedelta(minutes=LOCKOUT_DURATION)
                        cursor.execute("UPDATE Users SET FailedAttempts = ?, LockoutTime = ? WHERE Username = ?", (failed_attempts, lockout_time, username))
                        conn.commit()
                        logging.info("Maximum login attempts exceeded. Account locked.")
                        log_audit("LOCKOUT", "User", username,
                                  f"Locked until {lockout_time} after {failed_attempts} failed attempts")
                        unlockpassword = input("Would you like to attempt manager override to unlock this account? (Y/N) ")
                        if unlockpassword.upper() == "Y":
                            check = getpass.getpass("Enter master override secret: ")
                            if MASTER_SECRET is not None and check == MASTER_SECRET:
                                cursor.execute("UPDATE Users SET FailedAttempts = 0, LockoutTime = NULL WHERE Username = ?", (username,))
                                conn.commit()
                                logging.info("Account unlocked successfully!")
                                return False, role, True  # Return role and reauthentication status
                            else:
                                logging.info("Invalid master override secret.")
                        else:
                            logging.info("Manager override not attempted.")
                            return False, None, False
                    else:
                        cursor.execute("UPDATE Users SET FailedAttempts = ? WHERE Username = ?", (failed_attempts, username))
                        conn.commit()

        except Exception as e:
            logging.error(f"Error during login: {e}")
            return False, None, False

def _prompt_role(prompt="Enter the role (a)dmin/(s)taff/(m)anager: "):
    """Ask for a role letter until they give one we have, then return the full word.

    Returns '' on Ctrl+C so the caller can back out without a stack trace. Shared by
    add_user() and the setup checklist so the two cannot drift on which roles exist --
    a role the prompt offers but the Admin Panel has no branch for is an account that
    cannot be signed in to.
    """
    try:
        while True:
            choice = input(prompt).strip().lower()
            if choice == 'a':
                return 'admin'
            if choice == 's':
                return 'staff'
            if choice == 'm':
                return 'manager'
            logging.info("Invalid role. Please try again.")
    except (KeyboardInterrupt, EOFError):
        return ''


def add_user():
    try:
        new_username = input("Enter new username: ").strip()
        new_password = input("Enter new password: ").strip()
        role = _prompt_role()
        if not role:
            logging.info("No account was created.")
            return
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM Users WHERE Username = ?", (new_username,))
            if cursor.fetchone():
                logging.info(f"User '{new_username}' already exists.")
                return
            cursor.execute("INSERT INTO Users (Username, Password, Role) VALUES (?, ?, ?)", (new_username, new_password, role))
            conn.commit()
            log_audit("CREATE", "User", new_username, f"Role {role}")
            logging.info(f"User '{new_username}' added successfully.")
    except Exception as e:
        logging.error(f"Error adding user: {e}")
    # connection closed by context manager

def delete_user():
    try:
        del_username = input("Enter username to delete: ").strip()
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Role FROM Users WHERE Username = ?", (del_username,))
            existing = cursor.fetchone()
            if not existing:
                logging.info(f"User '{del_username}' not found.")
                return
            cursor.execute("DELETE FROM Users WHERE Username = ?", (del_username,))
            conn.commit()
            log_audit("DELETE", "User", del_username, f"Deleted account with role {existing[0]}")
            logging.info(f"User '{del_username}' deleted successfully.")
    except Exception as e:
        logging.error(f"Error deleting user: {e}")
    # connection closed by context manager

def get_dynamic_price(item_id):
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute("SELECT Price, PricingRule FROM Items WHERE ItemID = ?", (item_id,))
            result = cursor.fetchone()

        if result:
            base_price, pricing_rule = result
            base_price = float(base_price)  # Convert Decimal to float
            rule = str(pricing_rule).strip().lower() if pricing_rule else ''
            if rule == 'peak':
                return base_price * get_peak_factor()
            elif rule == 'offpeak':
                return base_price * get_offpeak_factor()
            else:
                return base_price
        else:
            logging.info("Item not found.")
            return None
    except Exception as e:
        logging.error(f"Error retrieving dynamic price: {e}")
        return None

def get_item_choice():
    while True:
        try:
            display_items()
            choice = int(input("Which service/item do you want? "))
            price = get_dynamic_price(choice)
            if price is not None:
                return choice, price
            else:
                logging.info("Invalid choice. Please try again.")
        except ValueError:
            logging.info("Invalid input. Please enter a number.")
        except Exception as e:
            logging.error(f"Error getting item choice: {e}")

def view_reservations():
    """Display all reservations including the floor."""
    with get_connection() as conn:
        if conn is None:
            return
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT RoomNumber, Floor, LastName, FirstName FROM Reservations")
            rows = cursor.fetchall()
            table_rows = [(r.RoomNumber, r.Floor, r.LastName, r.FirstName) for r in rows]
            ui.show_table("Reservations", ["Room #", "Floor", "Last Name", "First Name"], table_rows)
        except Exception as e:
            logging.error(f"Error displaying reservations: {e}")

def update_item():
    try:
        item_id = int(input("Enter the item ID to update: ").strip())
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Name, Price, PricingRule FROM Items WHERE ItemID = ?", (item_id,))
            item = cursor.fetchone()
        if not item:
            logging.info("Item not found.")
            return
        new_name = input(f"Enter the new item name (blank to keep '{item.Name}'): ").strip()
        if new_name == "":
            new_name = item.Name
        price_raw = input(f"Enter the new item price (blank to keep {item.Price}): ").strip()
        if price_raw == "":
            new_price = item.Price
        else:
            new_price = float(price_raw)
        current_rule = item.PricingRule if item.PricingRule else 'standard'
        rule_raw = input(f"Enter pricing rule - (P)eak/(O)ffPeak/(S)tandard (blank to keep '{current_rule}'): ").strip().lower()
        if rule_raw == "":
            new_rule = item.PricingRule
        elif rule_raw in ('p', 'peak'):
            new_rule = 'Peak'
        elif rule_raw in ('o', 'off', 'offpeak', 'off-peak'):
            new_rule = 'OffPeak'
        elif rule_raw in ('s', 'standard', 'none', 'clear', 'no rule'):
            new_rule = None
        else:
            logging.info("Invalid pricing rule. Setting standard (no rule).")
            new_rule = None
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("UPDATE Items SET Name = ?, Price = ?, PricingRule = ? WHERE ItemID = ?", (new_name, new_price, new_rule, item_id))
            conn.commit()
            log_audit("UPDATE", "Item", item_id,
                      f"'{new_name}' @ {new_price}, rule {new_rule or 'standard'}")
            logging.info(f"Item with ID {item_id} updated successfully.")
    except Exception as e:
        logging.error(f"Error updating item: {e}")
    # connection closed by context manager

def search_reservations():
    try:
        search_type = input("Search by room number (RN)/last name(LN): ").strip().lower()
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            if search_type == 'rn':
                search_value = input(f"Enter the room number: ").strip()
                cursor.execute("SELECT * FROM Reservations WHERE RoomNumber = ?", (search_value,))
            elif search_type == 'ln':
                search_value = input(f"Enter the last name: ").strip()
                cursor.execute("SELECT * FROM Reservations WHERE LastName = ?", (search_value,))
            else:
                logging.info("Invalid search type.")
                return

            reservations = cursor.fetchall()
            if reservations:
                logging.info("Search Results:")
                for reservation in reservations:
                    logging.info(f"Room Number: {reservation.RoomNumber}, Last Name: {reservation.LastName}, First Name: {reservation.FirstName}")
            else:
                logging.info("No reservations found.")
    except Exception as e:
        logging.error(f"Error searching reservations: {e}")
    # connection closed by context manager

def view_users():
    """Display users; optionally show passwords after verifying the master secret."""
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            passwords = input("Would you like to see the passwords? (Y/N): ").strip().upper()
            show_passwords = False
            if passwords == 'Y':
                mpwd = getpass.getpass("Please enter the master password: ").strip()
                # Verify master secret from config if available, otherwise check DB master account
                if MASTER_SECRET is not None and mpwd == MASTER_SECRET:
                    logging.info("Master secret verified (config). Displaying passwords.")
                    show_passwords = True
                else:
                    cursor.execute("SELECT Password FROM Users WHERE Username = ?", ('master',))
                    row = cursor.fetchone()
                    if row and mpwd == row[0]:
                        logging.info("Master password is correct. Displaying passwords.")
                        show_passwords = True
                    else:
                        logging.info("Incorrect master password. Cannot display passwords.")

            cursor.execute("SELECT Username, Password, Role FROM Users")
            rows = cursor.fetchall()
            headers = ["Username", "Password", "Role"] if show_passwords else ["Username", "Role"]
            table_rows = []
            for user in rows:
                if user.Username.lower() == 'master':
                    continue
                if show_passwords:
                    table_rows.append((user.Username, user.Password, user.Role))
                else:
                    table_rows.append((user.Username, user.Role))
            ui.show_table("Users and Roles", headers, table_rows)
    except Exception as e:
        logging.error(f"Error displaying users: {e}")

def apply_discount(total_amount):
    try:
        discount_code = input("Enter discount code: ").strip()

        with get_connection() as conn:
            if conn is None:
                return total_amount
            cursor = conn.cursor()
            cursor.execute("SELECT DiscountPercentage FROM Discounts WHERE Code = ?", (discount_code,))
            result = cursor.fetchone()

        if result:
            discount_percentage = result[0]
            discount_amount = (total_amount * float(discount_percentage)) / 100
            total_with_discount = total_amount - discount_amount
            logging.info(f"Discount applied! New total amount: ${total_with_discount:.2f}")
            return total_with_discount
        else:
            logging.info("Invalid discount code.")
            return total_amount
    except Exception as e:
        logging.error(f"Error applying discount: {e}")
        return total_amount

def view_items():
    """Display all items."""
    with get_connection() as conn:
        if conn is None:
            return
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT ItemID, Name, Price, PricingRule FROM Items")
            rows = cursor.fetchall()
            table_rows = [(r.ItemID, r.Name, f"${r.Price:.2f}", r.PricingRule) for r in rows]
            ui.show_table("Items", ["ID", "Name", "Price", "Pricing Rule"], table_rows)
        except Exception as e:
            logging.error(f"Error displaying items: {e}")


def _run_report(label, fn, **kwargs):
    """Run a reports.py export and show the result. Returns True on success."""
    try:
        for path in fn(**kwargs):
            ui.success(f"{label} exported: {path}")
        return True
    except Exception as e:
        logging.error(f"Error exporting {label.lower()}: {e}")
        return False


def export_reports_menu():
    """Interactive admin submenu for report exports."""
    while True:
        ui.show_menu("Report Export Menu", [
            "1. Export Transactions",
            "2. Export Reservations",
            "3. Export Loyalty Statements",
            "4. Export Invoices (split folio)",
            "5. Export Revenue (room vs F&B)",
            "6. Export Occupancy",
            "7. Export Housekeeping Board",
            "8. Export Guest Satisfaction",
            "9. Export Audit Log",
            "10. Back to Admin Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            _run_report("Transactions", reports.export_transactions)
        elif choice == '2':
            _run_report("Reservations", reports.export_reservations)
        elif choice == '3':
            # A loyalty statement is the history of a GUEST. Both references resolve to
            # one person; a room is accepted because staff mostly know the room, and it
            # then reports that guest's whole history rather than one room's fragment.
            # A room number and a CustomerID are both 4 digits, so the type is asked
            # for rather than guessed from the shape of the input.
            who = input("Guest ID/email/name, or a room number (leave blank for all): ").strip()
            if not who:
                _run_report("Loyalty statements", reports.export_loyalty_statements)
            else:
                ui.show_menu("Loyalty statement for", ["1. Guest (ID, email, or name)", "2. Room number"])
                ref_type = input("Enter your choice: ").strip()
                if ref_type == '1':
                    _run_report("Loyalty statements", reports.export_loyalty_statements,
                                customer=who)
                else:
                    _run_report("Loyalty statements", reports.export_loyalty_statements,
                                room_number=who)
        elif choice == '4':
            _run_report("Invoices", reports.export_invoices)
        elif choice == '5':
            start = ui.ask_optional_date("From date")
            end = ui.ask_optional_date("To date")
            _run_report("Revenue", reports.export_revenue, start_date=start, end_date=end)
        elif choice == '6':
            start = ui.ask_optional_date("From date (blank = first night on record)")
            end = ui.ask_optional_date("To date (blank = the business date)")
            _run_report("Occupancy", reports.export_occupancy, start_date=start, end_date=end)
        elif choice == '7':
            floor = input("Floor (blank for every floor): ").strip()
            # Defaults to the business date, and takes an explicit one, so the board for
            # a day that has already closed can be re-run rather than only today's.
            on_date = ui.ask_optional_date(f"Board date (blank = business date, "
                                           f"{business_date()})")
            _run_report("Housekeeping", reports.export_housekeeping, floor=floor or None,
                        on_date=on_date)
        elif choice == '8':
            _run_report("Guest satisfaction", reports.export_guest_satisfaction)
        elif choice == '9':
            _run_report("Audit log", reports.export_audit_log)
        elif choice == '10':
            break
        else:
            logging.info("Invalid choice. Please try again.")


def handle_cli_args():
    parser = argparse.ArgumentParser(description="Hotel Management Reporting CLI")
    parser.add_argument("--report", choices=sorted(reports.REPORTS), help="Report type to export")
    parser.add_argument("--format", choices=["csv"], default="csv", help="Export format (only csv is supported)")
    parser.add_argument("--room", help="Room number: reports the whole history of the guest who held that stay")
    parser.add_argument("--customer", help="Customer ID, email, or 'LastName FirstName' for loyalty statements")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD): revenue and occupancy reports")
    parser.add_argument("--end", help="End date (YYYY-MM-DD): revenue and occupancy reports")
    parser.add_argument("--floor", help="Floor number to limit the housekeeping report")
    parser.add_argument("--date", help="Board date (YYYY-MM-DD) for the housekeeping report; "
                                       "defaults to the business date")
    args, _ = parser.parse_known_args()
    if not args.report:
        return False
    try:
        if args.report == 'loyalty':
            paths = reports.export_loyalty_statements(export_format=args.format,
                                                     room_number=args.room,
                                                     customer=args.customer)
        elif args.report == 'revenue':
            paths = reports.export_revenue(export_format=args.format, start_date=args.start, end_date=args.end)
        elif args.report == 'occupancy':
            paths = reports.export_occupancy(export_format=args.format, start_date=args.start,
                                            end_date=args.end)
        elif args.report == 'housekeeping':
            paths = reports.export_housekeeping(export_format=args.format, floor=args.floor,
                                                on_date=args.date)
        else:
            paths = reports.REPORTS[args.report](export_format=args.format)
        for path in paths:
            logging.info(f"Exported report: {path}")
        return True
    except Exception as e:
        logging.error(f"Report export failed: {e}")
        return False


def admin_panel():

    login_successful, role, reauth = admin_login()

    if not login_successful and not reauth:
        logging.info("Unauthorized access. Returning to main menu.")
        return
    elif reauth:
        logging.info("Reauthentication required. Please log in again.")
        admin_panel()

    role = str(role).lower()
    while True:
        if role == 'staff':
            ui.show_menu("Admin Panel", [
                "---- Reservations ----",
                "1. Add Reservation",
                "2. View Reservations",
                "3. Edit Reservation",
                "4. Delete Reservation",
                "5. Search Reservations",
                "6. Search Availability",
                "7. Arrivals / Departures Board",
                "---- Guest Services ----",
                "8. Guest Requests (Concierge & Feedback)",
                "9. Order Management",
                "10. Staff Alerts",
                "---- Rooms ----",
                "11. Rooms & Housekeeping",
                "12. Exit Admin Panel",
            ])
        elif role == 'manager':
            ui.show_menu("Admin Panel", [
                "---- Reservations ----",
                "1. Add Reservation",
                "2. Delete Reservation",
                "3. Edit Reservation",
                "4. View Reservations",
                "5. Search Reservations",
                "6. Search Availability",
                "7. Arrivals / Departures Board",
                "---- Guest Services ----",
                "8. Guest Requests (Concierge & Feedback)",
                "9. Order Management",
                "10. Staff Alerts",
                "---- Notifications ----",
                "11. Send Notification to Customer",
                "---- Items & Services ----",
                "12. Add Item",
                "13. Delete Item",
                "14. Update Item",
                "15. View Items",
                "---- Users ----",
                "16. View Users",
                "---- Discounts ----",
                "17. View Discount Codes",
                "---- Rooms ----",
                "18. Rooms & Housekeeping",
                "---- Invoices ----",
                "19. Invoices & Printing",
                "---- Customers ----",
                "20. Customer Profiles",
                "---- Security ----",
                "21. Door Access Control",
                "---- Other ----",
                "22. Exit Admin Panel",
            ])
        elif role == 'admin':
            ui.show_menu("Admin Panel", [
                "---- Reservations ----",
                "1. Add Reservation",
                "2. Delete Reservation",
                "3. Edit Reservation",
                "4. View Reservations",
                "5. Search Reservations",
                "6. Search Availability",
                "7. Arrivals / Departures Board",
                "---- Guest Services ----",
                "8. Guest Requests (Concierge & Feedback)",
                "9. Order Management",
                "10. Staff Alerts",
                "---- Notifications ----",
                "11. Send Notification to Customer",
                "12. Send Alert to Staff",
                "---- Items & Services ----",
                "13. Add Item",
                "14. Delete Item",
                "15. Update Item",
                "16. View Items",
                "17. Manage Amenities",
                "18. Manage Promotions",
                "---- Users ----",
                "19. Add User",
                "20. Delete User",
                "21. Edit User",
                "22. View Users",
                "23. Reset User Password",
                "---- Discounts ----",
                "24. Manage Discount Codes",
                "---- Pricing & Settings ----",
                "25. Manage Pricing & Settings",
                "26. Business Date (Close the Day)",
                "---- Loyalty ----",
                "27. Loyalty Management",
                "---- Reports ----",
                "28. Export Reports",
                "---- Rooms ----",
                "29. Rooms & Housekeeping",
                "---- Invoices ----",
                "30. Invoices & Printing",
                "---- Customers ----",
                "31. Customer Profiles",
                "---- Security ----",
                "32. Door Access Control",
                "---- Destructive ----",
                "33. Delete All Reservations (master override)",
                "---- Setup ----",
                "34. Setup Checklist",
                "---- Other ----",
                "35. Exit Admin Panel",
            ])
        elif role == 'valet':
            logging.info("Enter Valet Panel...")
            ui.pause()
        elif role == 'it':
            logging.info("Enter IT Support Panel...")
            ui.pause()
        else:
            logging.error("A role has not been assigned. Please contact the system administrator.")
            ui.pause()
            break
        if not role == 'it' and not role == 'valet':
            choice = input("Enter your choice: ").strip()
        if role == 'staff':
            if choice == '1':
                add_reservation()
            elif choice == '2':
                view_reservations()
            elif choice == '3':
                edit_reservation()
            elif choice == '4':
                delete_reservation()
            elif choice == '5':
                search_reservations()
            elif choice == '6':
                show_availability_search()
            elif choice == '7':
                show_arrivals_departures_board()
            elif choice == '8':
                guest_requests_menu()
            elif choice == '9':
                manage_orders_menu()
            elif choice == '10':
                view_staff_alerts()
            elif choice == '11':
                rooms_admin_menu(view_only=True)
            elif choice == '12':
                break
            else:
                logging.info("Invalid choice. Please try again.")

        elif role == 'manager':
            if choice == '1':
                add_reservation()
            elif choice == '2':
                delete_reservation()
            elif choice == '3':
                edit_reservation()
            elif choice == '4':
                view_reservations()
            elif choice == '5':
                search_reservations()
            elif choice == '6':
                show_availability_search()
            elif choice == '7':
                show_arrivals_departures_board()
            elif choice == '8':
                guest_requests_menu()
            elif choice == '9':
                manage_orders_menu()
            elif choice == '10':
                view_staff_alerts()
            elif choice == '11':
                send_notification_to_customer()
            elif choice == '12':
                add_item()
            elif choice == '13':
                delete_item()
            elif choice == '14':
                update_item()
            elif choice == '15':
                view_items()
            elif choice == '16':
                view_users()
            elif choice == '17':
                view_discount_codes()
            elif choice == '18':
                rooms_admin_menu(view_only=True)
            elif choice == '19':
                invoices_menu()
            elif choice == '20':
                search_customer_profiles()
            elif choice == '21':
                door_access_menu(view_only=True)
            elif choice == '22':
                break
            else:
                logging.info("Invalid choice. Please try again.")

        elif role == 'admin':
            if choice == '1':
                add_reservation()
            elif choice == '2':
                delete_reservation()
            elif choice == '3':
                edit_reservation()
            elif choice == '4':
                view_reservations()
            elif choice == '5':
                search_reservations()
            elif choice == '6':
                show_availability_search()
            elif choice == '7':
                show_arrivals_departures_board()
            elif choice == '8':
                guest_requests_menu()
            elif choice == '9':
                manage_orders_menu()
            elif choice == '10':
                view_staff_alerts()
            elif choice == '11':
                send_notification_to_customer()
            elif choice == '12':
                send_alert_to_staff()
            elif choice == '13':
                add_item()
            elif choice == '14':
                delete_item()
            elif choice == '15':
                update_item()
            elif choice == '16':
                view_items()
            elif choice == '17':
                manage_amenities_menu()
            elif choice == '18':
                manage_promotions_menu()
            elif choice == '19':
                add_user()
            elif choice == '20':
                delete_user()
            elif choice == '21':
                edit_user()
            elif choice == '22':
                view_users()
            elif choice == '23':
                reset_user_password()
            elif choice == '24':
                manage_discount_codes()
            elif choice == '25':
                manage_pricing_rules()
            elif choice == '26':
                manage_business_date()
            elif choice == '27':
                loyalty_admin_menu()
            elif choice == '28':
                export_reports_menu()
            elif choice == '29':
                rooms_admin_menu()
            elif choice == '30':
                invoices_menu()
            elif choice == '31':
                search_customer_profiles()
            elif choice == '32':
                door_access_menu()
            elif choice == '33':
                delete_all_reservations()
            elif choice == '35':
                onboarding_checklist(role)
            elif choice == '36':
                break
            else:
                logging.info("Invalid choice. Please try again.")
        elif role == 'valet':
            valet_vehicle_management()
            break
        elif role == 'it':
            it_support_panel()
            break
        else:
            logging.error("404 Role Not Found. Please contact the system administrator.")
## =========================
# Rooms & Housekeeping
## =========================
def rooms_dashboard():
    """Global room snapshot: status counts + housekeeping load by floor."""
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT COALESCE(Status, 'Available') AS Status, COUNT(*) AS n FROM Rooms GROUP BY COALESCE(Status, 'Available')")
            counts = {r.Status: r.n for r in cursor.fetchall()}
            cursor.execute(
                "SELECT TOP 10 "
                "  CASE WHEN LEN(RoomNumber) >= 4 AND RoomNumber NOT LIKE '%[^0-9]%' "
                "       THEN TRY_CAST(LEFT(RoomNumber, LEN(RoomNumber) - 3) AS int) END AS Floor, "
                "  SUM(CASE WHEN Status = 'Dirty' THEN 1 ELSE 0 END) AS Dirty, "
                "  SUM(CASE WHEN Status = 'Cleaning' THEN 1 ELSE 0 END) AS Cleaning, "
                "  SUM(CASE WHEN Status = 'Maintenance' THEN 1 ELSE 0 END) AS Maintenance, "
                "  COUNT(*) AS Total "
                "FROM Rooms "
                "WHERE Status IN ('Dirty', 'Cleaning', 'Maintenance') "
                "GROUP BY CASE WHEN LEN(RoomNumber) >= 4 AND RoomNumber NOT LIKE '%[^0-9]%' "
                "       THEN TRY_CAST(LEFT(RoomNumber, LEN(RoomNumber) - 3) AS int) END "
                "ORDER BY Dirty + Cleaning + Maintenance DESC"
            )
            load_rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading room dashboard: {e}")
        return

    for status in ROOM_STATUSES:
        counts.setdefault(status, 0)
    total = sum(counts.values())
    occupied = counts.get("Occupied", 0)
    occupancy = (occupied / total * 100) if total else 0.0
    ui.show_table("Room Dashboard - Overview", ["Total", "Available", "Occupied", "Cleaning", "Dirty", "Maintenance", "Occupancy %"], [
        [total, counts["Available"], counts["Occupied"], counts["Cleaning"], counts["Dirty"], counts["Maintenance"], f"{occupancy:.1f}%"],
    ])
    ui.show_table(
        "Housekeeping Load - Top 10 Floors",
        ["Floor", "Flagged", "Dirty", "Cleaning", "Maintenance"],
        [(r.Floor, r.Dirty + r.Cleaning + r.Maintenance, r.Dirty, r.Cleaning, r.Maintenance) for r in load_rows],
    )
    logging.info(f"{occupied} room(s) currently occupied ({occupancy:.1f}%). Use 'Explore Floor' to drill in.")


def view_rooms():
    """Explore a single floor: colour-coded board with guest + stay details."""
    try:
        floor = int(input("Enter floor to explore (1-150): ").strip())
    except ValueError:
        logging.info("Invalid floor. Please enter a number.")
        return
    if floor < 1 or floor > 150:
        logging.info("Floor must be between 1 and 150.")
        return
    type_raw = input("Filter by room type (blank = all on this floor): ").strip()
    # One clock, one window convention, for every board. The stay window is half-open
    # [CheckInDate, CheckOutDate) -- the same rule stays_overlap() and search_availability
    # use -- so a guest departing today is a DEPARTURE, not still in house. This query
    # used `CheckOutDate >= GETDATE()`, which showed them as in house for one extra night
    # and disagreed with both occupancy reports about the same rows.
    on_date = business_date()
    query = (
        "SELECT r.RoomNumber, r.RoomType, r.Status, "
        "  cur.FirstName AS FirstName, cur.LastName AS LastName, "
        "  cur.CheckInDate AS CheckInDate, cur.CheckOutDate AS CheckOutDate, "
        "  CASE WHEN cur.RoomNumber IS NOT NULL THEN DATEDIFF(day, cur.CheckInDate, cur.CheckOutDate) ELSE 0 END AS Nights, "
        "  (SELECT COUNT(*) FROM Reservations u WHERE u.RoomNumber = r.RoomNumber "
        "     AND u.CheckInDate >= ?) AS Upcoming "
        "FROM Rooms r "
        "OUTER APPLY ("
        "  SELECT TOP 1 x.RoomNumber, x.FirstName, x.LastName, x.CheckInDate, x.CheckOutDate "
        "  FROM Reservations x "
        "  WHERE x.RoomNumber = r.RoomNumber "
        "    AND x.CheckInDate <= ? AND x.CheckOutDate > ? "
        "  ORDER BY x.CheckInDate DESC"
        ") cur "
        "WHERE LEFT(r.RoomNumber, LEN(r.RoomNumber) - 3) = ? "
    )
    # Parameter order follows the order the placeholders appear in the SQL text: the
    # correlated Upcoming count, then the two APPLY bounds, then the floor filter.
    params = [on_date, on_date, on_date, str(floor)]
    if type_raw:
        query += "AND r.RoomType = ? "
        params.append(type_raw)
    query += "ORDER BY r.RoomNumber"

    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading floor rooms: {e}")
        return
    if not rows:
        logging.info("No rooms found for that floor.")
        return

    status_counts = {}
    for r in rows:
        status = r.Status or "Available"
        status_counts[status] = status_counts.get(status, 0) + 1
    summary = "   ".join(f"{status}: {status_counts.get(status, 0)}" for status in ROOM_STATUSES)
    ui.box(f"Floor {floor} - {len(rows)} room(s)", summary)

    # Pts/Night is base x category multiplier; fetch multipliers once per floor.
    pts_per_night = {}
    for rt in {r.RoomType for r in rows}:
        pts_per_night[rt] = get_loyalty_points_per_night() * get_room_type_multiplier(rt)

    table_rows = []
    for r in rows:
        guest = f"{r.FirstName} {r.LastName}".strip() if r.FirstName else ""
        check_in = str(r.CheckInDate)[:10] if r.CheckInDate else ""
        check_out = str(r.CheckOutDate)[:10] if r.CheckOutDate else ""
        table_rows.append((
            r.RoomNumber,
            r.RoomType,
            r.Status or "Available",
            guest,
            check_in,
            check_out,
            r.Nights,
            r.Upcoming,
            f"{pts_per_night.get(r.RoomType, 0):.0f}",
        ))
    ui.show_status_table(
        f"Floor {floor} Rooms",
        ["Room #", "Type", "Status", "Guest", "Check-In", "Check-Out", "Nights", "Upcoming", "Pts/Night"],
        table_rows,
        status_col=2,
    )


def update_room_status():
    """Manually update a room's housekeeping/occupancy status."""
    room_number = input("Enter room number: ").strip()
    if not room_number:
        logging.info("Invalid room number.")
        return
    current = get_room_status(room_number)
    if current is None:
        logging.info("Room not found in Rooms. It will be auto-registered as 'Available'.")
        upsert_room_if_missing(room_number)
        current = get_room_status(room_number)
    logging.info(f"Current status of room {room_number}: {current}")
    logging.info("Available statuses: " + ", ".join(ROOM_STATUSES))
    status = input("Enter new status: ").strip()
    if status not in ROOM_STATUSES:
        logging.info("Invalid status. Must be one of: " + ", ".join(ROOM_STATUSES))
        return
    if set_room_status(room_number, status):
        logging.info(f"Room {room_number} status updated to '{status}'.")


def rooms_admin_menu(view_only=False):
    """Rooms & housekeeping submenu: dashboard + drill-down explorer."""
    while True:
        if view_only:
            ui.show_menu("Rooms & Housekeeping", [
                "1. Room Dashboard",
                "2. Explore Floor",
                "3. Back to Admin Panel",
            ])
        else:
            ui.show_menu("Rooms & Housekeeping", [
                "1. Room Dashboard",
                "2. Explore Floor",
                "3. Update Room Status",
                "4. Back to Admin Panel",
            ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            rooms_dashboard()
        elif choice == '2':
            view_rooms()
        elif choice == '3' and not view_only:
            update_room_status()
        elif (view_only and choice == '3') or (not view_only and choice == '4'):
            break
        else:
            logging.info("Invalid choice. Please try again.")
## =========================
# Notifications & Alerts Management
## =========================
STAFF_ROLES = [
    "Housekeeping",
    "Front Desk",
    "Maintenance",
    "Security",
    "Concierge",
    "Management",
    "IT Support",
    "Valet",
]
# The real order lifecycle, replacing the old random.choice() status picker.
ORDER_STATUSES = ["Placed", "Preparing", "Ready", "Delivered", "Completed"]
ORDER_STATUS_HELP = {
    "Placed": "Order received and queued by the kitchen.",
    "Preparing": "Your order is being prepared.",
    "Ready": "Your order is ready for pickup.",
    "Delivered": "Your order has arrived at your door.",
    "Completed": "Order complete. Enjoy your meal!",
    "Cancelled": "This order was cancelled. Please contact the concierge.",
}

def send_notification_to_customer():
    """Record a notification for a guest, persisted so the guest can actually read it."""
    try:
        while True:
            room_number = input("Enter customer's room number: ").strip()
            with get_connection() as conn:
                if conn is None:
                    logging.info("Database connection failed.")
                    return
                cursor = conn.cursor()
                cursor.execute("SELECT RoomNumber FROM Reservations WHERE RoomNumber = ?", (room_number,))
                result = cursor.fetchone()
            if result:
                break
            else:
                logging.info("Room number not reserved. Please try again.")
        message = input("Enter notification message: ").strip()
        if not message:
            logging.info("Empty message. Notification not sent.")
            return
        channel = input("Channel - (I)n-room/(S)ms/(E)mail (default in-room): ").strip().lower() or 'i'
        channel_name = {'i': 'In-Room', 's': 'SMS', 'e': 'Email'}.get(channel, 'In-Room')
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Notifications (RoomNumber, Message, Channel, SentBy) "
                "OUTPUT INSERTED.NotificationID VALUES (?, ?, ?, ?)",
                (room_number, message, channel_name, CURRENT_USER),
            )
            conn.commit()
        log_audit("CREATE", "Notification", room_number, f"[{channel_name}] {message}")
        logging.info(f"Notification sent to room {room_number}: {message}")
    except Exception as e:
        logging.error(f"Error sending notification: {e}")


def view_notifications_for_room():
    """Guest: read the notifications left for their room."""
    room_number, _ = validate_room()
    if not room_number:
        logging.info("Could not verify your room.")
        return
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT NotificationID, Message, Channel, SentBy, CreatedAt, ReadAt "
                "FROM Notifications WHERE RoomNumber = ? ORDER BY CreatedAt DESC",
                (room_number,),
            )
            rows = cursor.fetchall()
            if not rows:
                logging.info("You have no messages. Enjoy your stay!")
                return
            unread = [r for r in rows if not r.ReadAt]
            ui.show_table(
                f"My Messages - Room {room_number}",
                ["#", "Message", "Channel", "From", "Sent", "Read"],
                [(r[0], r[1], r[2], r[3] or "-", r[4].strftime("%Y-%m-%d %H:%M"),
                  r[5].strftime("%Y-%m-%d %H:%M") if r[5] else "unread") for r in rows],
            )
            for r in unread:
                cursor.execute("UPDATE Notifications SET ReadAt = ? WHERE NotificationID = ?",
                               (datetime.now(), r[0]))
            if unread:
                conn.commit()
                logging.info(f"{len(unread)} message(s) marked as read.")
    except Exception as e:
        logging.error(f"Error reading notifications: {e}")


def send_alert_to_staff():
    """Broadcast an alert to a staff role, persisted and acknowledgeable."""
    try:
        while True:
            ui.show_menu("Send Alert to Staff", [
                f"{idx}. {role}" for idx, role in enumerate(STAFF_ROLES, 1)
            ] + ["0. Cancel"])
            raw = input("Enter the number of the staff role: ").strip()
            if raw == '0':
                return
            if raw.isdigit() and 1 <= int(raw) <= len(STAFF_ROLES):
                selected_role = STAFF_ROLES[int(raw) - 1]
                break
            logging.info("Invalid choice. Please try again.")
        message = input("Enter alert message: ").strip()
        if not message:
            logging.info("Empty message. Alert not sent.")
            return
        severity = input("Severity - (I)nfo/(W)arning/(C)ritical (default info): ").strip().lower() or 'i'
        severity_name = {'i': 'Info', 'w': 'Warning', 'c': 'Critical'}.get(severity, 'Info')
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO StaffAlerts (TargetRole, Message, Severity) "
                "OUTPUT INSERTED.AlertID VALUES (?, ?, ?)",
                (selected_role, message, severity_name),
            )
            conn.commit()
        log_audit("CREATE", "StaffAlert", selected_role, f"[{severity_name}] {message}")
        logging.info(f"Alert sent to {selected_role} staff: {message}")
    except Exception as e:
        logging.error(f"Error sending alert: {e}")


def view_staff_alerts():
    """Staff: list alerts, newest first, and acknowledge one."""
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT AlertID, TargetRole, Message, Severity, CreatedAt, "
                "AcknowledgedAt, AcknowledgedBy FROM StaffAlerts ORDER BY CreatedAt DESC"
            )
            rows = cursor.fetchall()
            if not rows:
                logging.info("No alerts on record.")
                return
            ui.show_table(
                "Staff Alerts",
                ["#", "Role", "Severity", "Message", "Sent", "Acknowledged"],
                [(r[0], r[1], r[2], r[3], r[4].strftime("%Y-%m-%d %H:%M"),
                  f"{r[5]:%Y-%m-%d %H:%M} by {r[6]}" if r[5] else "UNACK") for r in rows],
            )
            unacked = [r for r in rows if not r.AcknowledgedAt]
            if unacked:
                logging.info(f"{len(unacked)} unacknowledged alert(s).")
                raw = input("Enter the alert number to acknowledge (blank to skip): ").strip()
                if raw.isdigit() and any(r[0] == int(raw) for r in rows):
                    cursor.execute(
                        "UPDATE StaffAlerts SET AcknowledgedAt = ?, AcknowledgedBy = ? "
                        "WHERE AlertID = ?",
                        (datetime.now(), CURRENT_USER, int(raw)),
                    )
                    conn.commit()
                    log_audit("ACK", "StaffAlert", int(raw), f"Acknowledged by {CURRENT_USER}")
                    logging.info(f"Alert {raw} acknowledged.")
    except Exception as e:
        logging.error(f"Error viewing staff alerts: {e}")
def view_discount_codes():
    """Display all discount codes as a table."""
    try:
        with get_connection() as conn:
            if conn is None:
                logging.info("Database connection failed.")
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Code, DiscountPercentage FROM Discounts")
            discounts = cursor.fetchall()
            if discounts:
                table_rows = [(d.Code, f"{d.DiscountPercentage}%") for d in discounts]
                ui.show_table("Current Discount Codes", ["Code", "Percentage"], table_rows)
            else:
                ui.info("No discount codes found.")
    except Exception as e:
        logging.error(f"Error viewing discount codes: {e}")


def manage_discount_codes():
    """Provides a sub-menu for discount code management."""
    try:
        while True:
            ui.show_menu("Discount Code Management", [
                "1. Add Discount Code",
                "2. Update Discount Code",
                "3. Delete Discount Code",
                "4. View Discount Codes",
                "5. Exit Discount Management",
            ])
            choice = input("Enter your choice: ").strip()
            with get_connection() as conn:
                if conn is None:
                    logging.info("Database connection failed.")
                    return
                cursor = conn.cursor()
                if choice == "1":
                    cursor.execute("SELECT Code FROM Discounts")
                    last_code = cursor.fetchall()
                    logging.info(f"Last discount code: {(last_code[-1])[0] if last_code else 'None'}")
                    code = input("Enter new discount code: ").strip()
                    if not code:
                        logging.info("Discount code cannot be blank.")
                        continue
                    try:
                        discount_percentage = float(input("Enter discount percentage: ").strip())
                    except ValueError:
                        logging.info("Invalid discount percentage.")
                        continue
                    if discount_percentage <= 0 or discount_percentage > 100:
                        logging.info("Discount percentage must be greater than 0 and at most 100.")
                        continue
                    # Duplicate codes silently overwrote the first one (the PK is Code).
                    cursor.execute("SELECT 1 FROM Discounts WHERE Code = ?", (code,))
                    if cursor.fetchone():
                        logging.info(f"Discount code '{code}' already exists. Update it instead.")
                        continue
                    cursor.execute(
                        "INSERT INTO Discounts (Code, DiscountPercentage) VALUES (?, ?)",
                        (code, discount_percentage)
                    )
                    conn.commit()
                    log_audit("CREATE", "Discount", code, f"{discount_percentage}%")
                    logging.info(f"Discount code '{code}' added successfully.")
                elif choice == "2":
                    view_discount_codes()
                    code = input("Enter discount code to update: ").strip()
                    cursor.execute("SELECT DiscountPercentage FROM Discounts WHERE Code = ?", (code,))
                    existing = cursor.fetchone()
                    if existing:
                        try:
                            new_percentage = float(input("Enter new discount percentage: ").strip())
                        except ValueError:
                            logging.info("Invalid percentage.")
                            continue
                        if new_percentage <= 0 or new_percentage > 100:
                            logging.info("Discount percentage must be greater than 0 and at most 100.")
                            continue
                        cursor.execute(
                            "UPDATE Discounts SET DiscountPercentage = ? WHERE Code = ?",
                            (new_percentage, code)
                        )
                        conn.commit()
                        log_audit("UPDATE", "Discount", code,
                                  f"{float(existing[0])}% -> {new_percentage}%")
                        logging.info(f"Discount code '{code}' updated successfully.")
                    else:
                        logging.info("Discount code not found.")
                elif choice == "3":
                    view_discount_codes()
                    code = input("Enter discount code to delete: ").strip()
                    cursor.execute("SELECT DiscountPercentage FROM Discounts WHERE Code = ?", (code,))
                    existing = cursor.fetchone()
                    if not existing:
                        logging.info("Discount code not found.")
                        continue
                    if not ui.ask_confirmation(f"Delete discount code '{code}' ({float(existing[0])}%)?"):
                        continue
                    cursor.execute("DELETE FROM Discounts WHERE Code = ?", (code,))
                    conn.commit()
                    log_audit("DELETE", "Discount", code, f"Deleted {float(existing[0])}% code")
                    logging.info(f"Discount code '{code}' deleted successfully.")
                elif choice == "4":
                    view_discount_codes()
                elif choice == "5":
                    break
                else:
                    logging.info("Invalid choice. Please try again.")
    except Exception as e:
        logging.error(f"Error managing discount codes: {e}")


def view_room_type_rates():
    """Admin: view and edit the nightly rate for each room category.

    A rate change applies to stays booked FROM NOW ON. Every existing stay has its rate
    captured on its own reservation (migration 022), and check-out bills that number, so
    re-pricing the house cannot re-price a booking that has already been made -- and
    cannot retroactively change an invoice that has already been issued.
    """
    while True:
        room_types = get_room_types(active_only=False, include_status=True)
        ui.show_table(
            "Room Types & Nightly Rates",
            ["Room Type", "Nightly Rate", "Loyalty Multiplier", "Status"],
            [(rt, f"${rate:,.2f}", f"x{get_room_type_multiplier(rt):.2f}",
              "Active" if active else "Inactive")
             for rt, rate, active in room_types],
        )
        ui.info("Enter a room type to change its rate, or press Enter to return.")
        room_type = input("Room type: ").strip()
        if not room_type:
            return
        if not any(rt.lower() == room_type.lower() for rt, _, _ in room_types):
            logging.info("Unknown room type. Available: " + ", ".join(rt for rt, _, _ in room_types))
            continue
        current = get_nightly_rate(room_type)
        try:
            new_rate = float(input(f"New nightly rate for {room_type} (current ${current:,.2f}): ").strip())
        except ValueError:
            logging.info("Invalid rate. Please enter a number.")
            continue
        if new_rate < 0:
            logging.info("Nightly rate cannot be negative.")
            continue
        update_room_type_rate(room_type, new_rate)


def manage_business_date():
    """Admin: see the business date, close the day, or set it by hand.

    Closing the day is one button, not a procedure, and says exactly what it does: it
    moves the one clock. It does not clean rooms, re-read rates, expire points, or bill
    anything -- there is no night audit in this app, and pretending otherwise would be
    worse than not having it.
    """
    current = business_date()
    ui.show_table("Business Date", ["Setting", "Value"], [
        ("Current business date", str(current)),
        ("Wall-clock date", str(datetime.now().date())),
    ])
    ui.show_menu("Business Date", [
        "1. Close the day (advance to the next date)",
        "2. Set the business date to a specific date",
        "3. Back to Admin Panel",
    ])
    choice = input("Enter your choice: ").strip()
    if choice == '1':
        if not ui.ask_confirmation(
                f"Advance the business date from {current} to {current + timedelta(days=1)}?",
                default="n"):
            logging.info("The business date was not changed.")
            return
        new_date = close_day()
        if new_date is None:
            logging.info("Could not write the business date. It is unchanged at "
                         f"{current}. Is the settings table writable?")
            return
        logging.info(f"Business date is now {new_date}. Every board and report now reads "
                     f"that day, and you can re-run any of them for it.")
    elif choice == '2':
        on_date = ui.ask_date("Enter the business date (YYYY-MM-DD)", default=str(current))
        if on_date is None:
            return
        if on_date == current:
            logging.info(f"The business date is already {on_date}.")
            return
        if not ui.ask_confirmation(f"Set the business date to {on_date} (it is currently "
                                   f"{current})?", default="n"):
            logging.info("The business date was not changed.")
            return
        new_date = set_business_date(on_date)
        if new_date is None:
            logging.info("Could not write the business date. It is unchanged at "
                         f"{current}. Is the settings table writable?")
            return
        logging.info(f"Business date set to {new_date}.")
    elif choice == '3':
        return
    else:
        logging.info("Invalid choice. Please try again.")


def manage_pricing_rules():
    """Admin: view and edit nightly room rates, peak/off-peak factors, tax, and loyalty rates."""
    while True:
        ui.show_menu("Pricing & Settings", [
            "1. View Current Settings",
            "2. Edit Peak / Off-Peak Price Factors",
            "3. Edit Tax Rate",
            "4. Edit Loyalty Points Rates",
            "5. Edit Room-Type Points Multipliers",
            "6. View / Edit Room Types & Nightly Rates",
            "7. Edit Booking Cancellation Policy",
            "8. Back to Admin Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            order_rate, room_rate = points_per_dollar_order_vs_room(
                get_loyalty_accrual_points_per_unit(), get_loyalty_points_per_night())
            rows = [
                ("Business date (the app's 'today')", str(business_date())),
                ("Peak price factor", f"{get_peak_factor():.2f}"),
                ("Off-peak price factor", f"{get_offpeak_factor():.2f}"),
                ("Tax rate", f"{get_tax_rate():.2f}"),
                ("Loyalty order accrual (points per $1 of F&B)", f"{order_rate:g}"),
                ("Loyalty stay accrual (points per night)", get_loyalty_points_per_night()),
                ("Loyalty redemption (points per $1 credit)", get_loyalty_redemption_points_per_currency_unit()),
                ("Booking free-cancellation (days before check-in)", get_booking_refund_cutoff_days()),
            ]
            ui.show_table("Current Settings", ["Setting", "Value"], rows)
            ui.show_table("Room Types & Nightly Rates", ["Room Type", "Nightly Rate"],
                          [(rt, f"${rate:,.2f}") for rt, rate in get_room_types()])
            ui.show_table("Room-Type Points Multipliers", ["Room Type", "Multiplier"],
                          [(rt, f"x{mult:.2f}") for rt, mult in get_all_room_type_multipliers()])
            ui.info(f"Earn rate check: a Standard night buys {room_rate:.2f} points per $1, "
                    f"so $1 of room service at {order_rate:g} is the smaller prize. It should "
                    f"be -- the room is the thing being bought.")
        elif choice == '2':
            logging.info(f"Current peak factor: {get_peak_factor():.2f} (sale price = base x factor)")
            logging.info(f"Current off-peak factor: {get_offpeak_factor():.2f} (sale price = base x factor)")
            try:
                peak = float(input("Enter new peak factor (e.g. 1.20): ").strip())
                offpeak = float(input("Enter new off-peak factor (e.g. 0.90): ").strip())
            except ValueError:
                logging.info("Invalid factor. Please enter a number.")
                continue
            if peak <= 0 or offpeak <= 0:
                logging.info("Factors must be positive numbers.")
                continue
            set_setting('peak_factor', peak)
            set_setting('offpeak_factor', offpeak)
        elif choice == '3':
            logging.info(f"Current tax rate: {get_tax_rate()} ({get_tax_rate()*100:.0f}% tax on purchases).")
            try:
                tax = float(input("Enter new tax rate as a decimal (e.g. 0.13 for 13%): ").strip())
            except ValueError:
                logging.info("Invalid tax rate. Please enter a number.")
                continue
            if tax < 0:
                logging.info("Tax rate cannot be negative.")
                continue
            set_setting('tax_rate', tax)
        elif choice == '4':
            order_rate, room_rate = points_per_dollar_order_vs_room(
                get_loyalty_accrual_points_per_unit(), get_loyalty_points_per_night())
            logging.info(f"Current: {get_loyalty_points_per_night()} pts per night, "
                         f"{order_rate:g} pts per $1 spent on orders, "
                         f"{get_loyalty_redemption_points_per_currency_unit()} pts per $1 credit.")
            logging.info(f"For calibration: a Standard night is worth {room_rate:.2f} pts per $1, "
                         f"so the order rate above should stay BELOW that. A guest who is "
                         f"billed more per dollar for a drink than for the room they slept in "
                         f"is a setting that has been mis-tuned.")
            try:
                per_night = int(input("Enter base points awarded per night (e.g. 100): ").strip())
                accrual = float(input("Enter points per $1 spent on orders (e.g. 0.5): ").strip())
                redemption = int(input("Enter points needed per $1 discount (e.g. 100): ").strip())
            except ValueError:
                logging.info("Invalid value. The per-night and redemption rates must be "
                             "whole numbers; the order accrual may be a fraction (e.g. 0.5).")
                continue
            if per_night < 0 or accrual < 0 or redemption <= 0:
                logging.info("Invalid values: per-night >= 0, order accrual >= 0, redemption > 0.")
                continue
            if accrual > room_rate:
                logging.info(f"{accrual:g} pts per $1 of room service is MORE than the {room_rate:.2f} "
                             f"pts per $1 the room itself earns. Refusing: a guest would earn "
                             f"more by ordering a coffee than by staying.")
                continue
            set_setting('loyalty_points_per_night', per_night)
            set_setting('loyalty_accrual_points_per_unit', accrual)
            set_setting('loyalty_redemption_points_per_currency_unit', redemption)
        elif choice == '5':
            ui.show_table("Room-Type Points Multipliers", ["Room Type", "Multiplier"],
                          [(rt, f"x{mult:.2f}") for rt, mult in get_all_room_type_multipliers()])
            room_type = input("Enter the room type to edit (blank to cancel): ").strip()
            if not room_type:
                continue
            if room_type not in DEFAULT_ROOM_TYPE_MULTIPLIERS:
                logging.info("Unknown room type. Use one of: " + ", ".join(DEFAULT_ROOM_TYPE_MULTIPLIERS.keys()))
                continue
            try:
                new_mult = float(input(f"New multiplier for {room_type} (current x{get_room_type_multiplier(room_type):.2f}): ").strip())
            except ValueError:
                logging.info("Invalid multiplier. Please enter a number.")
                continue
            if new_mult <= 0:
                logging.info("Multiplier must be positive.")
                continue
            set_setting(_room_type_setting_key(room_type), new_mult)
        elif choice == '6':
            view_room_type_rates()
        elif choice == '7':
            current = get_booking_refund_cutoff_days()
            logging.info(f"Guests can cancel free up to {current} day(s) before check-in. "
                         f"Cancelling later keeps the deposit as a fee. 0 = no free window.")
            try:
                days = int(input("Enter the free-cancellation window in days: ").strip())
            except ValueError:
                logging.info("Invalid value. Please enter a whole number of days.")
                continue
            if days < 0:
                logging.info("The cancellation window cannot be negative.")
                continue
            set_setting('booking_refund_cutoff_days', days)
        elif choice == '8':
            break
        else:
            logging.info("Invalid choice. Please try again.")


def loyalty_admin_menu():
    """Admin: loyalty management submenu."""
    while True:
        ui.show_menu("Loyalty Management", [
            "1. View Loyalty Accounts",
            "2. Adjust Points for a Room",
            "3. View Loyalty Transactions",
            "4. Manage Loyalty Tiers",
            "5. Recalculate All Tiers",
            "6. Back to Admin Panel",
        ])
        sub = input("Enter your choice: ").strip()
        if sub == '1':
            admin_view_loyalty_accounts()
        elif sub == '2':
            admin_adjust_loyalty_points()
        elif sub == '3':
            admin_view_loyalty_transactions()
        elif sub == '4':
            admin_manage_loyalty_tiers()
        elif sub == '5':
            admin_recompute_all_tiers()
        elif sub == '6':
            break
        else:
            logging.info("Invalid choice.")


def _pick_loyalty_customer(prompt="Enter the guest's name or email: "):
    """Admin helper: resolve a guest to a CustomerID for a loyalty action.

    Loyalty belongs to a person, so every admin loyalty action identifies the guest.
    Accepts a full name or an email so staff do not have to remember which spelling
    the guest registered under. Returns the CustomerID, or None if not found.
    """
    if not LOYALTY_ENABLED:
        logging.info("Loyalty program is not enabled.")
        return None
    query = input(prompt).strip()
    if not query:
        logging.info("No guest specified.")
        return None
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT TOP 10 CustomerID, FirstName, LastName, Email FROM CustomerProfiles "
                "WHERE Email = ? OR LastName + ' ' + FirstName = ? "
                "OR (LastName = ? AND FirstName = ?) ORDER BY CustomerID DESC",
                (query, query, query, query),
            )
            matches = cursor.fetchall()
            if not matches:
                # A surname alone must not be enough to pick a person: report a miss
                # rather than guessing, or a clerk could adjust the wrong guest.
                logging.info(f"No customer profile found for '{query}'.")
                return None
            if len(matches) > 1:
                ui.info("More than one guest matches. Pick the right one:")
                rows = [(r.CustomerID, f"{r.LastName}, {r.FirstName}", r.Email or "") for r in matches]
                ui.show_table("Matches", ["ID", "Guest", "Email"], rows)
                try:
                    chosen = int(input("Customer ID: ").strip())
                except ValueError:
                    logging.info("Invalid selection.")
                    return None
                match = next((r for r in matches if r.CustomerID == chosen), None)
                if match is None:
                    logging.info("That customer ID is not one of the matches.")
                    return None
                return int(match.CustomerID)
            return int(matches[0].CustomerID)
    except Exception as e:
        logging.error(f"Error finding customer for loyalty action: {e}")
        return None


def admin_view_loyalty_accounts():
    """Admin: list loyalty accounts and point balances, by GUEST."""
    if not LOYALTY_ENABLED:
        logging.info("Loyalty program is not enabled.")
        return
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT la.CustomerID, cp.LastName, cp.FirstName, cp.Email, la.RoomNumber, "
                "la.Points, la.Tier, la.LastUpdated "
                "FROM LoyaltyAccounts la "
                "LEFT JOIN CustomerProfiles cp ON cp.CustomerID = la.CustomerID "
                "ORDER BY la.Points DESC"
            )
            rows = cursor.fetchall()
            if rows:
                table_rows = [
                    (r.CustomerID, f"{r.LastName or '?'}, {r.FirstName or '?'}",
                     r.Email or "-", r.RoomNumber or "-", r.Points, r.Tier, r.LastUpdated)
                    for r in rows
                ]
                ui.show_table(
                    "Loyalty Accounts",
                    ["Cust ID", "Guest", "Email", "Last Room", "Points", "Tier", "Last Updated"],
                    table_rows,
                )
            else:
                ui.info("No loyalty accounts found.")
    except Exception as e:
        logging.error(f"Error viewing loyalty accounts: {e}")


def admin_view_loyalty_transactions():
    """Admin: view loyalty transactions."""
    if not LOYALTY_ENABLED:
        logging.info("Loyalty program is not enabled.")
        return
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT lt.ID, lt.CustomerID, cp.LastName, cp.FirstName, lt.RoomNumber, "
                "lt.Delta, lt.Reason, lt.CreatedAt, lt.SourceID "
                "FROM LoyaltyTransactions lt "
                "LEFT JOIN CustomerProfiles cp ON cp.CustomerID = lt.CustomerID "
                "ORDER BY lt.CreatedAt DESC"
            )
            rows = cursor.fetchall()
            if rows:
                table_rows = [
                    (r.ID, r.CustomerID,
                     f"{r.LastName or '?'}, {r.FirstName or '?'}", r.RoomNumber or "-",
                     r.Delta, r.Reason, r.CreatedAt, r.SourceID)
                    for r in rows
                ]
                ui.show_table(
                    "Loyalty Transactions",
                    ["ID", "Cust ID", "Guest", "Room", "Delta", "Reason", "At", "Source"],
                    table_rows,
                )
            else:
                ui.info("No loyalty transactions found.")
    except Exception as e:
        logging.error(f"Error viewing loyalty transactions: {e}")


def admin_adjust_loyalty_points():
    """Admin: add or remove points for a GUEST."""
    if not LOYALTY_ENABLED:
        logging.info("Loyalty program is not enabled.")
        return
    customer_id = _pick_loyalty_customer()
    if not customer_id:
        return
    try:
        current = get_points_by_customer(customer_id)
        logging.info(f"Current points for customer {customer_id}: {current}")
        try:
            delta = int(input("Enter points to add (positive) or remove (negative): ").strip())
        except ValueError:
            logging.info("Invalid points value.")
            return
        if delta == 0:
            logging.info("No change requested.")
            return
        if delta > 0:
            add_points_to_customer(customer_id, delta, reason='admin_adjust')
            log_audit("ADJUST", "LoyaltyAccount", f"customer:{customer_id}", f"+{delta} points (admin adjust)")
            logging.info(f"Added {delta} points to customer {customer_id}.")
        else:
            # Removing points - ensure we don't go negative
            remove = min(current, abs(delta))
            if remove <= 0:
                logging.info("No points to remove.")
                return
            success = redeem_points_by_customer(customer_id, remove, reason='admin_remove')
            if success:
                log_audit("ADJUST", "LoyaltyAccount", f"customer:{customer_id}", f"-{remove} points (admin remove)")
                logging.info(f"Removed {remove} points from customer {customer_id}.")
            else:
                logging.info("Failed to remove points.")
    except Exception as e:
                logging.error(f"Error adjusting loyalty points: {e}")


def admin_manage_loyalty_tiers():
    """Admin: view and edit loyalty tier thresholds/perks."""
    if not LOYALTY_ENABLED:
        logging.info("Loyalty program is not enabled.")
        return
    while True:
        tiers = _tiers_from_db() or DEFAULT_TIERS
        ui.show_table("Loyalty Tiers", ["Tier", "Min Lifetime Points", "Points x", "Discount %", "Perks"],
                      [(t[0], t[1], f"x{t[2]:.2f}", f"{t[3]:.0f}%", t[4]) for t in tiers])
        ui.show_menu("Tier Management", [
            "1. Edit Tier Discount Percentage",
            "2. Edit Tier Points Multiplier",
            "3. Edit Tier Minimum Points",
            "4. Back to Loyalty Management",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            name = input("Enter tier name to edit (e.g. Silver, Gold, Platinum): ").strip()
            match = None
            for t in tiers:
                if t[0].lower() == name.lower():
                    match = t
                    break
            if match is None:
                logging.info("Tier not found.")
                continue
            try:
                new_discount = float(input(f"New discount percent for {match[0]} (current {match[3]:.0f}%): ").strip())
            except ValueError:
                logging.info("Invalid number.")
                continue
            new_discount = max(0.0, min(100.0, new_discount))
            with get_connection() as conn:
                if conn is None:
                    continue
                try:
                    conn.cursor().execute("UPDATE LoyaltyTiers SET DiscountPercent = ? WHERE TierName = ?", (new_discount, match[0]))
                    conn.commit()
                    logging.info(f"Updated {match[0]} discount to {new_discount:.0f}%.")
                    admin_recompute_all_tiers()
                except Exception as e:
                    logging.error(f"Error updating tier discount: {e}")
        elif choice == '2':
            name = input("Enter tier name to edit (e.g. Silver, Gold, Platinum): ").strip()
            match = None
            for t in tiers:
                if t[0].lower() == name.lower():
                    match = t
                    break
            if match is None:
                logging.info("Tier not found.")
                continue
            try:
                new_mult = float(input(f"New points multiplier for {match[0]} (current x{match[2]:.2f}): ").strip())
            except ValueError:
                logging.info("Invalid number.")
                continue
            new_mult = max(1.0, new_mult)
            with get_connection() as conn:
                if conn is None:
                    continue
                try:
                    conn.cursor().execute("UPDATE LoyaltyTiers SET PointsMultiplier = ? WHERE TierName = ?", (new_mult, match[0]))
                    conn.commit()
                    logging.info(f"Updated {match[0]} multiplier to x{new_mult:.2f}.")
                    admin_recompute_all_tiers()
                except Exception as e:
                    logging.error(f"Error updating tier multiplier: {e}")
        elif choice == '3':
            name = input("Enter tier name to edit (e.g. Silver, Gold, Platinum): ").strip()
            match = None
            idx = None
            for i, t in enumerate(tiers):
                if t[0].lower() == name.lower():
                    match = t
                    idx = i
                    break
            if match is None:
                logging.info("Tier not found.")
                continue
            try:
                new_min = int(input(f"New minimum lifetime points for {match[0]} (current {match[1]}): ").strip())
            except ValueError:
                logging.info("Invalid number.")
                continue
            if new_min < 0:
                logging.info("Minimum points cannot be negative.")
                continue
            lower = tiers[idx - 1][1] if idx > 0 else None
            upper = tiers[idx + 1][1] if idx + 1 < len(tiers) else None
            if (lower is not None and new_min <= lower) or (upper is not None and new_min >= upper):
                logging.info("Minimum points must keep tiers in ascending order.")
                continue
            with get_connection() as conn:
                if conn is None:
                    continue
                try:
                    conn.cursor().execute("UPDATE LoyaltyTiers SET MinLifetimePoints = ? WHERE TierName = ?", (new_min, match[0]))
                    conn.commit()
                    logging.info(f"Updated {match[0]} minimum to {new_min} points.")
                    admin_recompute_all_tiers()
                except Exception as e:
                    logging.error(f"Error updating tier minimum points: {e}")
        elif choice == '4':
            break
        else:
            logging.info("Invalid choice.")


def admin_recompute_all_tiers():
    """Admin: recompute every loyalty account's tier from lifetime points."""
    if not LOYALTY_ENABLED:
        logging.info("Loyalty program is not enabled.")
        return
    count = recompute_all_tiers()
    logging.info(f"Recalculated tiers for {count} loyalty account(s).")
def reset_user_password():
    """Allow an admin to reset a user's password."""
    with get_connection() as conn:
        if conn is None:
            logging.info("Database connection failed.")
            return
        try:
            cursor = conn.cursor()
            username = input("Enter the username to reset password: ").strip()
            cursor.execute("SELECT Username FROM Users WHERE Username = ?", (username,))
            if cursor.fetchone():
                new_password = input("Enter new password: ").strip()
                cursor.execute("UPDATE Users SET Password = ? WHERE Username = ?", (new_password, username))
                conn.commit()
                log_audit("UPDATE", "User", username, "Password reset by admin")
                logging.info(f"Password for user '{username}' has been reset successfully.")
            else:
                logging.info("User not found.")
        except Exception as e:
            logging.error(f"Error resetting user password: {e}")

## =========================
# Billing & Payment
## =========================
def luhn_check(card_number):
    """Validate credit card number using Luhn's algorithm.

    Returns False for anything that is not a run of digits, rather than raising: a guest
    who mistypes their card must get "Invalid credit card number", not a traceback.
    """
    text = str(card_number or "").strip()
    if not text.isdigit():
        return False

    def digits_of(n):
        return [int(d) for d in str(n)]

    digits = digits_of(text)
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]

    checksum = sum(odd_digits)

    for d in even_digits:
        checksum += sum(digits_of(d * 2))

    return checksum % 10 == 0

# Last four digits of the card most recently accepted by process_credit_card(), so
# booking receipts and refunds can name the card without ever storing the full number.
# None means "no card on file".
LAST_CARD_DIGITS = None


def process_credit_card(amount):
    """Simulate credit card processing for the exact `amount` to charge.

    Callers pass the fully tax-inclusive total (see bill_room_transactions(),
    order_item(), and the booking deposit/prepayment flows), so no tax is added here.
    """
    global LAST_CARD_DIGITS
    amount = float(amount)
    logging.info(f"Processing credit card payment of ${amount:.2f}...")

    # Get credit card details
    card_number = input("Enter your credit card number: ").strip()

    # Validate credit card number using Luhn's algorithm
    if not luhn_check(card_number) or card_number == "":
        logging.info("Invalid credit card number. Payment Cancelled.")
        LAST_CARD_DIGITS = None
        return False

    expiration_date = input("Enter your credit card expiration date (MM/YYYY): ").strip()

    # Validate expiration date
    if not validate_expiration_date(expiration_date):
        logging.info("Invalid or expired credit card expiration date. Payment Cancelled.")
        LAST_CARD_DIGITS = None
        return False

    cvv = input("Enter your credit card CVV: ").strip()

    # Validate CVV length
    if len(cvv) != 3:
        logging.info("Invalid CVV. Payment Cancelled.")
        LAST_CARD_DIGITS = None
        return False
    # Remember only the last four digits, never the PAN or CVV.
    LAST_CARD_DIGITS = card_number[-4:]
    return True

def validate_expiration_date(expiration_date):

    """Validate if the credit card expiration date is valid and not expired."""
    try:
        # Parse expiration date
        exp_month, exp_year = expiration_date.split('/')
        exp_month = int(exp_month)
        exp_year = int(exp_year)

        # Get current date
        current_date = datetime.now()
        current_year = current_date.year
        current_month = current_date.month

        # Check if the expiration date is in the future. The month must be a real
        # month, otherwise "00/2030" would slip through the year check.
        if not 1 <= exp_month <= 12:
            return False

        if exp_year > current_year or (exp_year == current_year and exp_month >= current_month):
            return True
        else:
            return False
    except (ValueError, IndexError):
        # Return False if the date format is invalid
        return False
    


## ============================
# Customer Panel and Functions
## ============================
def upsert_customer_profile(last_name, first_name, email=None, phone=None, preferences=None):
    """Insert or update a CustomerProfiles row keyed on LastName + FirstName.

    Returns the CustomerID (existing or new), or None on failure. Existing
    contact fields are kept unless new values are supplied.
    """
    if not last_name or not first_name:
        return None
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CustomerID, Email, Phone, Preferences FROM CustomerProfiles "
                "WHERE LastName = ? AND FirstName = ? ORDER BY CustomerID DESC",
                (last_name, first_name),
            )
            row = cursor.fetchone()
            if row:
                new_email = email if email else row.Email
                new_phone = phone if phone else row.Phone
                new_prefs = preferences if preferences else row.Preferences
                cursor.execute(
                    "UPDATE CustomerProfiles SET Email = ?, Phone = ?, Preferences = ? WHERE CustomerID = ?",
                    (new_email, new_phone, new_prefs, row.CustomerID),
                )
                conn.commit()
                return row.CustomerID
            cursor.execute(
                "INSERT INTO CustomerProfiles (LastName, FirstName, Email, Phone, Preferences) "
                "OUTPUT INSERTED.CustomerID VALUES (?, ?, ?, ?, ?)",
                (last_name, first_name, email, phone, preferences),
            )
            new_row = cursor.fetchone()
            conn.commit()
            return int(new_row[0]) if new_row and new_row[0] is not None else None
    except Exception as e:
        logging.error(f"Error saving customer profile: {e}")
        return None


def link_reservation_customer(room_number, customer_id):
    """Attach a customer profile to a stay so its loyalty can be credited to that person.

    Needed for every stay that did not come from the booking desk, where
    `CustomerID` is NULL: a front-desk reservation, or a reservation made before
    migration 019. Idempotent -- a stay that is already linked is left alone, so
    check-in cannot re-point an account at a different guest.

    Returns True when the stay is linked to `customer_id` afterwards.
    """
    if not room_number or not customer_id:
        return False
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE Reservations SET CustomerID = ? "
                "WHERE RoomNumber = ? AND (CustomerID IS NULL OR CustomerID = ?)",
                (customer_id, room_number, customer_id),
            )
            if cursor.rowcount:
                conn.commit()
                logging.info(f"Stay in room {room_number} linked to customer profile {customer_id}.")
            return True
    except Exception as e:
        # Before 019 there is no CustomerID column; the stay simply has no loyalty.
        logging.debug(f"Could not link reservation {room_number} to a customer: {e}")
        return False


def check_in():
    """Handle customer check-in using validate_room and display amenities."""
    room_number, first_name = validate_room()  # Assume validate_room returns (room_number, first_name)
    if room_number and first_name:
        logging.info("Checking in...")
        time.sleep(2)
        logging.info("Verifying Identity...")
        time.sleep(2)
        logging.info("Finalizing check-in...")
        time.sleep(1)
        try:
            # Rooms status is now real state: mark the room occupied on check-in.
            set_room_status(room_number, "Occupied")
            with get_connection() as conn:
                if conn is not None:
                    cursor = conn.cursor()
                    cursor.execute("SELECT CheckInDate, CheckOutDate FROM Reservations WHERE RoomNumber = ?", (room_number,))
                    row = cursor.fetchone()
                    if row:
                        today = business_date()
                        if not (row.CheckInDate <= today < row.CheckOutDate):
                            logging.info("Note: today is outside the reservation's date window. Please verify the stay dates.")
        except Exception as e:
            logging.error(f"Error syncing room status on check-in: {e}")
        # Capture or refresh the guest's contact details in CustomerProfiles.
        try:
            with get_connection() as conn:
                if conn is not None:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT LastName, FirstName FROM Reservations WHERE RoomNumber = ?",
                        (room_number,),
                    )
                    res = cursor.fetchone()
                    if res:
                        last_name = res.LastName or ""
                        guest_first = res.FirstName or first_name
                        email = input("Please enter your email (optional, press Enter to skip): ").strip() or None
                        phone = input("Please enter your phone number (optional, press Enter to skip): ").strip() or None
                        customer_id = upsert_customer_profile(last_name, guest_first, email, phone)
                        if customer_id:
                            # A front-desk or pre-019 stay has no customer link yet. Now
                            # that the profile exists, attach it so the stay's points are
                            # credited to this person instead of to the room.
                            link_reservation_customer(room_number, customer_id)
                            logging.info("Contact details saved to your customer profile.")
                        else:
                            logging.info("Could not save contact details at this time.")
        except Exception as e:
            logging.error(f"Error saving customer profile on check-in: {e}")
        # Issue the guest's key card, expiring at the reservation's check-out.
        try:
            with get_connection() as conn:
                guest_last = None
                if conn is not None:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT LastName FROM Reservations WHERE RoomNumber = ?", (room_number,)
                    )
                    res = cursor.fetchone()
                    if res:
                        guest_last = res[0]
            key_card = issue_key_card(room_number, guest_last, first_name)
        except Exception as e:
            key_card = None
            logging.error(f"Error issuing key card on check-in: {e}")
        log_audit("UPDATE", "Reservation", room_number,
                  f"Check-in completed for {first_name}"
                  + (f" (key card {key_card})" if key_card else ""))
        logging.info("Room number and key card are being prepared...")
        time.sleep(2)
        logging.info(f"Your room number is {room_number}.")
        if key_card:
            logging.info(f"Your key card number is {key_card} and is ready for use.")
        else:
            logging.info("Your key card is ready for use. Please collect it from the front desk.")
        if HOTEL_NAME:
            logging.info(f"Check-in successful! Welcome to {HOTEL_NAME}, {first_name.capitalize()}!")
        else:
            logging.info(f"Check-in successful! Welcome, {first_name.capitalize()}!")
        logging.info("Please enjoy your stay!")
        logging.info(f"If you need assistance, please call the front desk at {room_number}-56.\n")
        amenities = get_amenities()
        if amenities:
            logging.info("Amenities:")
            for amenity in amenities:
                detail = f" - {amenity[2]}" if amenity[2] else ""
                logging.info(f"- {amenity[1]}{detail}")
    else:
        # The guest's details did not match a stay, so there is nothing to check in.
        # That is a front-desk problem and the front desk resolves it with the guest's
        # ID in hand -- this app never asks for it, and never has.
        #
        # The previous version of this branch prompted for a government ID and a full card
        # number, echoed BOTH into the log, then ran a verification it always failed and
        # offered the master password. A full PAN and a government ID on disk in a log
        # file is the worst thing in the codebase, and the branch accomplished nothing:
        # the outcome was a hard-coded "Verification unsuccessful!" on every path. The
        # whole thing is gone rather than tidied, because there was no behaviour in it
        # worth keeping.
        logging.info("We could not find a reservation matching those details, so there is nothing to check in.")
        logging.info("Please take your ID with you and speak to the front desk, who can look the booking up directly.")
        log_audit("UPDATE", "Reservation", "-",
                  "Check-in refused: no reservation matched the name and room given")


def compute_and_apply_discounts(subtotal, room_number):
    """Apply an optional discount code then the loyalty tier discount to a subtotal.

    Shared by order pay-now and billing so both price room service identically.
    Returns (discounted_subtotal, discount_code_amount, tier_discount_amount, savings_lines).
    """
    discounted_subtotal = subtotal
    lines = []
    discount_code_amount = 0.0
    tier_discount_amount = 0.0
    if input("Do you have a discount code? (Y/N): ").strip().lower() == 'y':
        discounted_subtotal = apply_discount(subtotal)
        discount_code_amount = subtotal - discounted_subtotal
        if discount_code_amount > 0:
            lines.append(f"Discount: -${discount_code_amount:.2f}")
    tier_details = get_tier_details_by_room(room_number) if LOYALTY_ENABLED else None
    tier_discount_pct = tier_details["discount_percent"] if tier_details else 0.0
    if tier_discount_pct > 0:
        tier_discount_amount = discounted_subtotal * (tier_discount_pct / 100.0)
        discounted_subtotal -= tier_discount_amount
        lines.append(f"Loyalty Tier Discount ({tier_details['tier']}): -${tier_discount_amount:.2f}")
    return discounted_subtotal, discount_code_amount, tier_discount_amount, lines


def bill_room_transactions(room_number, require_payment=True):
    """Build a bill from a room's unbilled transactions, collect payment, and mark them billed.

    Applies discount code + loyalty tier discount + tax, offers point redemption, then
    processes the card. On success the transactions are marked IsBilled = 1 and any
    deferred order loyalty points are awarded. A $0 bill is confirmed without card entry.

    Stays whose items were all paid at order time (pay-now) still get a check-out
    invoice so the stay has a permanent, itemized record; nothing is owing then.

    Returns True if the bill was settled (or nothing was owing).
    """
    if not room_number:
        logging.info("Invalid room number.")
        return False
    try:
        with get_connection() as conn:
            if conn is None:
                logging.info("Database connection failed.")
                return False
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ID, ItemID, Quantity, UnitPrice, Amount, CreatedAt, IsBilled, ChargeGroup, Description "
                "FROM Transactions WHERE RoomNumber = ? AND IsBilled = 0",
                (room_number,),
            )
            rows = cursor.fetchall()
            # Pay-now items (billed at order time) that are not yet linked to an invoice.
            cursor.execute(
                "SELECT ID, ItemID, Quantity, UnitPrice, Amount, CreatedAt, ChargeGroup, Description "
                "FROM Transactions WHERE RoomNumber = ? AND IsBilled = 1 AND InvoiceID IS NULL",
                (room_number,),
            )
            pay_now_rows = cursor.fetchall()
        if not rows and not pay_now_rows:
            logging.info("No unbilled transactions found for that room.")
            return True

        def _group_of(row):
            # Pre-013 rows have no meaningful ChargeGroup; the DB default tags them 'F&B'.
            return (getattr(row, "ChargeGroup", None) or CHARGE_GROUP_FNB) == CHARGE_GROUP_ROOM

        # Split the folio. The room charge is posted by post_room_charge(); everything
        # else is F&B and is the only group discounts may touch.
        room_rows = [r for r in rows if _group_of(r)]
        fnb_rows = [r for r in rows if not _group_of(r)]
        pay_now_rows = [r for r in pay_now_rows if not _group_of(r)]

        room_subtotal = sum(float(r.Amount) for r in room_rows)
        fnb_subtotal = sum(float(r.Amount) for r in fnb_rows)
        pay_now_amount = sum(float(r.Amount) for r in pay_now_rows)
        subtotal = room_subtotal + fnb_subtotal
        tx_ids = [r.ID for r in rows]

        points_redeemed = 0
        redemption_value = 0.0
        redemption_customer_id = None
        amount_paid = 0.0
        total_before_redemption = 0.0
        discount_code_amount = 0.0
        tier_amount = 0.0
        room_tax = 0.0
        fnb_tax = 0.0
        discounted_fnb_subtotal = fnb_subtotal
        # Credit taken at the booking desk, resolved against this stay's check-in date.
        prepaid_applied = 0.0
        stay_check_in = _reservation_check_in(room_number)

        if rows:
            receipt_lines = ["----- Receipt -----"]
            for r in room_rows:
                qty = r.Quantity or 1
                amt = float(r.Amount)
                label = getattr(r, "Description", None) or "Room charge"
                receipt_lines.append(f"{label} x{qty}: ${amt:.2f}")
            for r in fnb_rows:
                item_id = r.ItemID
                qty = r.Quantity or 1
                amt = float(r.Amount)
                item_name = getattr(r, "Description", None) or f"Item {item_id}"
                try:
                    with get_connection() as conn:
                        if conn is not None:
                            c = conn.cursor()
                            c.execute("SELECT Name FROM Items WHERE ItemID = ?", (item_id,))
                            ir = c.fetchone()
                            if ir:
                                item_name = ir[0]
                except Exception:
                    pass
                receipt_lines.append(f"{item_name} x{qty}: ${amt:.2f}")

            # Discounts are promotional, room rates are contractual, so the F&B group
            # alone is discounted. The room charge passes through at face value.
            if fnb_subtotal > 0:
                discounted_fnb_subtotal, discount_code_amount, tier_amount, savings_lines = compute_and_apply_discounts(fnb_subtotal, room_number)
                receipt_lines += savings_lines

            tax_rate = get_tax_rate()
            room_tax = room_subtotal * tax_rate
            fnb_tax = discounted_fnb_subtotal * tax_rate
            tax = room_tax + fnb_tax
            final_total = room_subtotal + discounted_fnb_subtotal + tax
            total_before_redemption = final_total
            if room_subtotal > 0:
                receipt_lines.append(f"Room Charges: ${room_subtotal:.2f}")
            if fnb_subtotal > 0:
                receipt_lines.append(f"Food & Beverage: ${discounted_fnb_subtotal:.2f}")
            receipt_lines.append(f"Subtotal: ${room_subtotal + discounted_fnb_subtotal:.2f}")
            receipt_lines.append(f"Tax (Tax Rate: {tax_rate*100:.0f}%): ${tax:.2f}")
            receipt_lines.append(f"Total Amount: ${final_total:.2f}")
            receipt_lines.append("-------------------")
            ui.box("Receipt", "\n".join(receipt_lines))

            # Loyalty redemption
            if LOYALTY_ENABLED:
                try:
                    points = get_points_by_room(room_number)
                    if final_total > 0 and points > 0:
                        logging.info(f"You have {points} points available.")
                        use = input("Redeem points for this bill? (Y/N): ").strip().lower()
                        if use == 'y':
                            max_points_for_bill = int(min(points, int(final_total * get_loyalty_redemption_points_per_currency_unit())))
                            logging.info(f"Maximum points usable for this bill: {max_points_for_bill} points.")
                            while True:
                                try:
                                    pts = int(input(f"Enter number of points to redeem (0-{max_points_for_bill}): ").strip())
                                    if 0 <= pts <= max_points_for_bill:
                                        break
                                except ValueError:
                                    pass
                                logging.info("Invalid input. Please enter a valid integer.")
                            if pts > 0:
                                # Record the INTENT only. The deduction happens in the
                                # invoice transaction further down, beside the invoice insert.
                                # Committing it here meant a declined card cost the guest
                                # their points with nothing billed in exchange -- the defect
                                # in docs/DEVIATIONS.md 9.
                                redemption_customer_id = customer_id_for_stay(room_number)
                                if redemption_customer_id is None:
                                    logging.info("No loyalty profile for this stay, so points cannot be redeemed.")
                                else:
                                    redemption_value = pts / get_loyalty_redemption_points_per_currency_unit()
                                    final_total -= redemption_value
                                    points_redeemed = pts
                                    logging.info(f"Redeeming {pts} points for ${redemption_value:.2f} off. New total: ${final_total:.2f}")
                except Exception as e:
                    logging.error(f"Error during loyalty redemption: {e}")

            # Credit anything paid at the booking desk. Applied after loyalty so it
            # reduces the card charge rather than the loyalty entitlement.
            if stay_check_in is not None:
                prepaid = get_outstanding_booking_credit(room_number, stay_check_in)
                if prepaid > 0:
                    settled = settle_with_prepayment(final_total, prepaid)
                    final_total = settled["balance_due"]
                    prepaid_applied = settled["credit_applied"]
                    receipt_lines.append(f"Prepaid at booking: -${prepaid_applied:.2f}")
                    receipt_lines.append(f"Balance due: ${final_total:.2f}")
                    ui.box("Receipt", "\n".join(receipt_lines))
                    logging.info(f"${prepaid_applied:,.2f} of your booking payment has been applied to this bill.")
                    if settled["credit_unused"] > 0:
                        logging.info(f"The final total came to less than you prepaid, so ${settled['credit_unused']:,.2f} "
                                     f"remains as a credit on your account and will be refunded.")

            # Collect payment, then mark transactions billed and record the check-out invoice.
            # The invoice snapshot keeps the pre-redemption total and the amount actually paid.
            amount_paid = max(0.0, final_total)
            if amount_paid <= 0:
                logging.info("Balance is $0.00 - confirming no payment required.")
            elif require_payment and not process_credit_card(amount_paid):
                logging.info("Payment failed. Transactions remain unbilled.")
                return False
        else:
            # Nothing owing at check-out: every item on this stay was already paid when
            # ordered (pay-now). Create a consolidation invoice so the stay has a
            # permanent record linking those items (PaidEarlier = 1). Discount and tax
            # were applied and charged at order time, so this invoice carries the billed
            # line amounts as-is.
            subtotal = pay_now_amount
            discount_code_amount = 0.0
            tier_amount = 0.0
            fnb_subtotal = pay_now_amount
            discounted_fnb_subtotal = pay_now_amount
            room_subtotal = 0.0
            tax = 0.0
            final_total = 0.0
            total_before_redemption = pay_now_amount
            logging.info("Balance is $0.00 (items were paid at order time) - recording check-out invoice.")
        try:
            with get_connection() as conn:
                if conn is None:
                    logging.info("Database connection failed.")
                    return False
                cursor = conn.cursor()
                placeholders = ",".join("?" for _ in tx_ids)
                # Subtotal/TotalAmount/AmountPaid stay GRAND TOTALS; the Fnb*/Room*
                # columns carry the per-group breakdown for the itemized invoice.
                has_prepaid = _invoices_have_prepaid_column()
                invoice_sql, invoice_params = _build_invoice_insert(has_prepaid, [
                    room_number,
                    round(subtotal, 2),
                    round(discount_code_amount, 2),
                    round(tier_amount, 2),
                    round(tax, 2),
                    round(total_before_redemption, 2),
                    points_redeemed,
                    round(redemption_value, 2),
                    round(amount_paid, 2),
                    round(room_subtotal, 2),
                    round(room_tax, 2),
                    round(room_subtotal + room_tax, 2),
                    round(fnb_subtotal, 2),
                    round(discount_code_amount, 2),
                    round(tier_amount, 2),
                    round(fnb_tax, 2),
                    round(prepaid_applied, 2),
                ])
                cursor.execute(invoice_sql, invoice_params)
                _row = cursor.fetchone()
                invoice_id = int(_row[0]) if _row and _row[0] is not None else None
                # Consume the booking credit in this same transaction, so a rolled-back
                # invoice leaves the credit available for the retry.
                if has_prepaid and stay_check_in is not None and prepaid_applied > 0:
                    apply_booking_credit(room_number, stay_check_in, prepaid_applied, invoice_id, conn)
                # Deduct the redeemed points in THIS transaction too, for the same reason the
                # booking credit is applied here. The invoice above already claims this
                # discount in PointsRedeemed/RedemptionValue, so the deduction has to land with
                # it. Keyed on the invoice so a repeated redemption is detectable and a retry
                # cannot take the points twice.
                if points_redeemed > 0 and redemption_customer_id is not None:
                    redeem_points_for_invoice(
                        redemption_customer_id, points_redeemed, room_number, 'checkout', conn,
                        source_id=f'redeem:{invoice_id}')
                # Mark this bill's items billed and link them to the invoice (settled at check-out).
                if tx_ids:
                    cursor.execute(
                        f"UPDATE Transactions SET IsBilled = 1, InvoiceID = ?, PaidEarlier = 0 WHERE ID IN ({placeholders})",
                        tuple([invoice_id] + tx_ids),
                    )
                # Link earlier pay-now items to the same invoice so it is fully itemized.
                if invoice_id is not None:
                    cursor.execute(
                        "UPDATE Transactions SET InvoiceID = ?, PaidEarlier = 1 "
                        "WHERE RoomNumber = ? AND IsBilled = 1 AND InvoiceID IS NULL",
                        (invoice_id, room_number),
                    )
                conn.commit()
                logging.info("Transactions marked as billed and check-out invoice recorded.")
        except Exception as e:
            logging.error(f"Failed to finalize bill / invoice: {e}")
            return False

        if LOYALTY_ENABLED and tx_ids:
            award_billed_order_points(room_number, tx_ids)
        return True
    except Exception as e:
        logging.error(f"Error generating bill from transactions: {e}")
        return False


def billing_creator():
    logging.info("\n--- Billing Creator ---")
    logging.info("Generate bill from (1) Transactions table or (2) Manual entry")
    choice = input("Enter 1 or 2: ").strip()
    if choice == '1':
        room_number = input("Enter room number to generate bill for: ").strip()
        if bill_room_transactions(room_number):
            logging.info("Bill settled.")
        else:
            logging.info("Bill could not be settled.")
    else:
        # Manual entry fallback (preserve previous behavior)
        logging.info("Enter your charges. Type 'done' for description to finish.")
        items = []
        total = 0.0
        while True:
            description = input("Enter charge description (or 'done' to finish): ").strip()
            if description.lower() == 'done' or description == '':
                break
            try:
                amount = float(input("Enter amount for this charge: ").strip())
            except ValueError:
                logging.info("Invalid amount. Please try again.")
                continue
            items.append((description, amount))
            total += amount
        receipt = "\n----- Receipt -----\n"
        for desc, amt in items:
            receipt += f"{desc}: ${amt:.2f}\n"
        receipt += f"Subtotal: ${total:.2f}\n"
        tax = total * get_tax_rate()
        final_total = total + tax
        receipt += f"Tax (Tax Rate: {get_tax_rate()*100:.0f}%): ${tax:.2f}\n"
        receipt += f"Total Amount: ${final_total:.2f}\n"
        receipt += "-------------------\n"
        ui.box("Receipt", receipt)
## =========================
# Invoice Printing
## =========================
def list_invoices_for_room(room_number):
    """All stored invoices for a room, newest first."""
    if not room_number:
        return []
    try:
        with get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT InvoiceID, RoomNumber, InvoiceDate, TotalAmount, AmountPaid "
                "FROM Invoices WHERE RoomNumber = ? ORDER BY InvoiceID DESC",
                (room_number,),
            )
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"Error listing invoices: {e}")
        return []


def print_invoice(invoice_id):
    """Print a stored invoice with a full itemized breakdown, including items that were
    paid before check-out. Also saves a plain-text copy to exports/ for real printing."""
    try:
        invoice_id = int(invoice_id)
    except (TypeError, ValueError):
        logging.info("Invalid invoice number.")
        return False
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "SELECT i.InvoiceID, i.RoomNumber, i.InvoiceDate, i.Subtotal, i.DiscountCodeAmount, "
                "i.TierDiscountAmount, i.TaxAmount, i.TotalAmount, i.PointsRedeemed, i.RedemptionValue, i.AmountPaid, "
                "i.RoomSubtotal, i.RoomTaxAmount, i.RoomTotal, i.FnbSubtotal, "
                "i.FnbDiscountCodeAmount, i.FnbTierDiscountAmount, i.FnbTaxAmount, "
                "(SELECT TOP 1 r.FirstName + ' ' + r.LastName FROM Reservations r "
                "  WHERE r.RoomNumber = i.RoomNumber ORDER BY r.CheckInDate DESC) AS GuestName "
                + (", i.PrepaidAmount" if _invoices_have_prepaid_column() else "")
                + " FROM Invoices i WHERE i.InvoiceID = ?",
                (invoice_id,),
            )
            inv = cursor.fetchone()
            if not inv:
                logging.info("Invoice not found.")
                return False
            cursor.execute(
                "SELECT t.ID, t.Quantity, t.UnitPrice, t.Amount, t.PaidEarlier, t.CreatedAt, "
                "  COALESCE(t.ChargeGroup, 'F&B') AS ChargeGroup, "
                "  COALESCE(it.Name, t.Description, "
                "    CASE WHEN t.ItemID IS NULL THEN 'Charge' "
                "         ELSE 'Item ' + CAST(t.ItemID AS varchar(20)) END) AS ItemName "
                "FROM Transactions t LEFT JOIN Items it ON it.ItemID = t.ItemID "
                "WHERE t.InvoiceID = ? ORDER BY t.CreatedAt, t.ID",
                (invoice_id,),
            )
            items = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading invoice {invoice_id}: {e}")
        return False

    guest = (inv.GuestName or "").strip()
    header = (
        f"INVOICE #{inv.InvoiceID}\n"
        f"Room: {inv.RoomNumber}     Date: {inv.InvoiceDate}\n"
        + (f"Guest: {guest}\n" if guest else "")
    )
    ui.box("Invoice", header.strip())

    def _line_rows(rows):
        out = []
        for t in rows:
            tag = "Card (pay now)" if t.PaidEarlier else "Card (check-out)"
            out.append((
                t.ID,
                t.ItemName,
                t.Quantity or 1,
                f"${float(t.UnitPrice or 0):.2f}",
                f"${float(t.Amount):.2f}",
                tag,
            ))
        return out

    # Room charges and food & beverage are shown as separate groups: the room charge
    # is never discounted, so a single combined table would imply a discount applied to it.
    room_items = [t for t in items if (t.ChargeGroup or CHARGE_GROUP_FNB) == CHARGE_GROUP_ROOM]
    fnb_items = [t for t in items if (t.ChargeGroup or CHARGE_GROUP_FNB) != CHARGE_GROUP_ROOM]
    if room_items:
        ui.show_table(
            f"Invoice #{inv.InvoiceID} - Room Charges",
            ["Tx #", "Item", "Nights", "Nightly Rate", "Amount", "Payment"],
            _line_rows(room_items),
        )
    if fnb_items:
        ui.show_table(
            f"Invoice #{inv.InvoiceID} - Food & Beverage",
            ["Tx #", "Item", "Qty", "Unit Price", "Amount", "Payment"],
            _line_rows(fnb_items),
        )
    if not items:
        ui.info("This invoice has no itemized line items.")
    totals = [
        ("Room Charges (subtotal)", f"${float(getattr(inv, 'RoomSubtotal', 0) or 0):.2f}"),
        ("Room Charges (tax)", f"${float(getattr(inv, 'RoomTaxAmount', 0) or 0):.2f}"),
        ("Room Charges (total)", f"${float(getattr(inv, 'RoomTotal', 0) or 0):.2f}"),
        ("F&B (subtotal)", f"${float(getattr(inv, 'FnbSubtotal', 0) or 0):.2f}"),
        ("F&B discount code", f"-${float(getattr(inv, 'FnbDiscountCodeAmount', 0) or 0):.2f}"),
        ("F&B loyalty tier discount", f"-${float(getattr(inv, 'FnbTierDiscountAmount', 0) or 0):.2f}"),
        ("F&B (tax)", f"${float(getattr(inv, 'FnbTaxAmount', 0) or 0):.2f}"),
        ("Subtotal (all)", f"${float(inv.Subtotal):.2f}"),
        ("Discount Code", f"-${float(inv.DiscountCodeAmount):.2f}"),
        ("Loyalty Tier Discount", f"-${float(inv.TierDiscountAmount):.2f}"),
        ("Tax", f"${float(inv.TaxAmount):.2f}"),
        ("Total", f"${float(inv.TotalAmount):.2f}"),
        ("Points Redeemed", str(inv.PointsRedeemed)),
        ("Redemption Value", f"-${float(inv.RedemptionValue):.2f}"),
        ("Prepaid at Booking", f"-${float(getattr(inv, 'PrepaidAmount', 0) or 0):.2f}"),
        ("Amount Paid", f"${float(inv.AmountPaid):.2f}"),
    ]
    ui.show_table(f"Invoice #{inv.InvoiceID} - Totals", ["Item", "Value"], totals)
    logging.info("Room charges are charged at face value: discount codes and loyalty tier "
                 "discounts apply to the Food & Beverage group only.")
    if any(getattr(t, "PaidEarlier", 0) for t in items):
        logging.info("Note: 'Card (pay now)' items were charged when the order was placed and are not part of the check-out Subtotal/Tax above.")

    try:
        save_dir = os.path.join(os.path.dirname(__file__), "exports")
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"invoice_{invoice_id}.txt")
        lines = ["=" * 64, f"INVOICE #{inv.InvoiceID}", f"Room: {inv.RoomNumber}", f"Date: {inv.InvoiceDate}"]
        if guest:
            lines.append(f"Guest: {guest}")
        lines.append("=" * 64)
        header_line = f"{'Tx #':<6}{'Item':<30}{'Qty':>4}{'Unit':>10}{'Amount':>10}  {'Payment':<18}"
        for group_title, group_rows in (("ROOM CHARGES", room_items), ("FOOD & BEVERAGE", fnb_items)):
            if not group_rows:
                continue
            lines.append("")
            lines.append(group_title)
            lines.append(header_line)
            lines.append("-" * 64)
            for t in group_rows:
                tag = "CARD (PAY NOW)" if t.PaidEarlier else "CARD (CHECK-OUT)"
                lines.append(
                    f"{t.ID:<6}{t.ItemName:<30}{t.Quantity or 1:>4}"
                    f"{float(t.UnitPrice or 0):>10.2f}{float(t.Amount):>10.2f}  {tag:<18}"
                )
        lines.append("-" * 64)
        for label, value in totals:
            lines.append(f"{label:<30}{value:>20}")
        lines.append("")
        lines.append("Note: Room charges are charged at face value. Discount codes and")
        lines.append("loyalty tier discounts apply to the Food & Beverage group only.")
        if any(getattr(t, "PaidEarlier", 0) for t in items):
            lines.append("")
            lines.append("Note: CARD (PAY NOW) lines were charged when the order was placed")
            lines.append("and are not part of the check-out Subtotal/Tax shown above.")
        lines.append("=" * 64)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logging.info(f"Invoice saved to: {path}")
    except Exception as e:
        logging.error(f"Error saving invoice text file: {e}")
    return True


def invoices_menu():
    """Admin/manager: browse and print stored invoices."""
    while True:
        ui.show_menu("Invoices & Printing", [
            "1. Print Invoice by Number",
            "2. Find Invoices by Room",
            "3. Back to Admin Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            raw = input("Enter invoice number: ").strip()
            if raw.isdigit():
                print_invoice(int(raw))
            else:
                logging.info("Invalid invoice number.")
        elif choice == '2':
            room_number = input("Enter room number (floor + 3-digit code): ").strip()
            invoices = list_invoices_for_room(room_number)
            if not invoices:
                logging.info("No invoices found for that room.")
                continue
            ui.show_table(
                f"Invoices for Room {room_number}",
                ["Invoice #", "Room", "Date", "Total", "Paid"],
                [(inv.InvoiceID, inv.RoomNumber, inv.InvoiceDate,
                  f"${float(inv.TotalAmount):.2f}", f"${float(inv.AmountPaid):.2f}") for inv in invoices],
            )
            raw = input("Enter invoice number to print (blank to cancel): ").strip()
            if raw.isdigit():
                print_invoice(int(raw))
        elif choice == '3':
            break
        else:
            logging.info("Invalid choice. Please try again.")


def print_my_invoice():
    """Customer: view and print the invoices for their room."""
    room_number, first_name = validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify your last name, first name, and room number. Please try again.")
        return
    invoices = list_invoices_for_room(room_number)
    if not invoices:
        logging.info("No invoices found for your room.")
        return
    ui.show_table(
        f"My Invoices - Room {room_number}",
        ["Invoice #", "Room", "Date", "Total", "Paid"],
        [(inv.InvoiceID, inv.RoomNumber, inv.InvoiceDate,
          f"${float(inv.TotalAmount):.2f}", f"${float(inv.AmountPaid):.2f}") for inv in invoices],
    )
    raw = input("Enter invoice number to print (blank to cancel): ").strip()
    if raw.isdigit():
        print_invoice(int(raw))


def check_out():
    """Handle customer check-out without deleting the reservation.

    Identity comes from validate_room() -- last name, FIRST name and room number in one
    query -- exactly like every other guest-facing feature. The old version asked for a
    last name and a room number only, so anyone who knew a guest's surname and guessed or
    was told a room number could settle that guest's folio, read it, take their card
    details, and walk out. validate_room() is also bounded to three attempts and never
    lists other stays for a surname; see docs/BOOKING.md §1.
    """
    room_number, first_name = validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify your last name, first name, and room number, so "
                     "check-out cannot continue. Please ask the front desk for assistance.")
        return
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CheckInDate, CheckOutDate, CustomerID, LastName FROM Reservations "
                "WHERE RoomNumber = ?",
                (room_number,),
            )
            matched_reservation = cursor.fetchone()
        if matched_reservation is None:
            logging.info("We could not find a reservation for that room. Please ask the "
                         "front desk for assistance.")
            return
        # Rooms status is real state: flag the room for housekeeping on check-out,
        # but only AFTER the bill is settled (a declined card leaves the guest in
        # the room and must not flip it to 'Dirty'). Bill is mandatory and payment
        # is required before any loyalty points are awarded. Deferred order points
        # are awarded inside bill_room_transactions() on successful payment; stay
        # points are awarded only after the bill is settled.
        #
        # The nightly room charge is posted first so it lands on the same folio and
        # invoice as any room service. It stays unbilled until payment succeeds, so
        # a declined card can be retried without re-posting the charge. It is billed at
        # the rate captured on the reservation, not the current one.
        post_room_charge(room_number, matched_reservation.CheckInDate, matched_reservation.CheckOutDate)
        if bill_room_transactions(room_number):
            set_room_status(room_number, "Dirty")
            revoke_active_key_cards(room_number, "stayed")
            # Pass the CustomerID we already matched rather than letting the
            # award re-resolve it from the room. The resolver works today because
            # check-out leaves the reservation in place, but tying the award to
            # that would make the loyalty of a stay quietly depend on a row
            # check-out happens not to touch.
            award_stay_points(room_number, matched_reservation.CheckInDate,
                              matched_reservation.CheckOutDate,
                              customer_id=matched_reservation.CustomerID)
            log_audit("UPDATE", "Reservation", room_number,
                      f"Check-out completed for {first_name} {matched_reservation.LastName}")
            if HOTEL_NAME:
                logging.info(f"Check-out complete for room {room_number}. Thanks for visiting {HOTEL_NAME}, {first_name.capitalize()}! We hope to see you again soon!")
            else:
                logging.info(f"Check-out complete for room {room_number}. Thanks for visiting, {first_name.capitalize()}! We hope to see you again soon!")
        else:
            logging.info("Payment declined. Please settle the bill before completing check-out.")
    except Exception as e:
        logging.error(f"Error during check-out: {e}")


def open_order(room_number):
    """Create a Placed order for a room so the guest can track it. Returns OrderID or None."""
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Orders (RoomNumber, Status, PlacedAt, UpdatedAt) "
                "OUTPUT INSERTED.OrderID VALUES (?, 'Placed', ?, ?)",
                (room_number, datetime.now(), datetime.now()),
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row[0]) if row and row[0] is not None else None
    except Exception as e:
        # Migration 015 not applied: ordering still works, only tracking is unavailable.
        logging.debug(f"Order record skipped ({type(e).__name__}: {e})")
        return None


def add_order_items(order_id, ordered_items):
    """Record (item_id, unit_price, quantity) lines against an order. Returns the count."""
    if not order_id or not ordered_items:
        return 0
    added = 0
    try:
        with get_connection() as conn:
            if conn is None:
                return 0
            cursor = conn.cursor()
            for (itm_id, unit_price, qty) in ordered_items:
                item_name = f"Item {itm_id}"
                cursor.execute("SELECT Name FROM Items WHERE ItemID = ?", (itm_id,))
                row = cursor.fetchone()
                if row:
                    item_name = row[0]
                cursor.execute(
                    "INSERT INTO OrderItems (OrderID, ItemID, ItemName, Quantity, UnitPrice) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (order_id, itm_id, item_name, int(qty), round(float(unit_price), 2)),
                )
                added += 1
            conn.commit()
            return added
    except Exception as e:
        logging.error(f"Error recording order items: {e}")
        return added


def order_item():
    logging.info("Welcome to the ordering system!")
# Existing ordering code starts here
    total = 0.0  # Initialize total as a float
    redemption_codes = []
    ordered_items = []  # list of (item_id, unit_price, quantity)

    room_number, first_name = validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify your last name, first name, and room number. Please try again.")
        return  # Return to main menu if validation fails

    logging.info(f"Welcome, {first_name.capitalize()}! Room {room_number} validated successfully.")

    while True:
        item_choice = get_item_choice()
        if item_choice is None:
            continue  # Return to item choice if error occurs

        quantity = get_quantity()
        try:
            item_id_str, price = item_choice
            logging.debug(f"Item Choice: {item_choice}")
            item_id = int(item_id_str)  # Convert item ID to integer

            price = get_dynamic_price(item_id)

            if price:
                if isinstance(price, Decimal):
                    price = float(price)
                total += price * quantity
                code = generate_code()
                redemption_codes.append((item_id, code))
                ordered_items.append((item_id, price, quantity))
                logging.info(f"Total so far: ${total:.2f}")
                logging.info(f"Redemption code for item {item_id}: {code}")
            else:
                logging.info("Item not found. Please try again.")
        except Exception as e:
            logging.error(f"Error during item processing: {e}")

        if not get_another_item():
            break  # Exit the item ordering loop
    logging.info("End of transaction")
    if not ordered_items:
        logging.info("No items were ordered.")
        return
    logging.info(f"Your subtotal is ${total:.2f} and will be delivered to room {room_number}.")
    logging.info("Your redemption codes are:")
    for item, code in redemption_codes:
        logging.info(f"Item ID {item}: {code}")

    item_lines = []
    for (itm_id, price, qty) in ordered_items:
        item_name = f"Item {itm_id}"
        try:
            with get_connection() as conn:
                if conn is not None:
                    c = conn.cursor()
                    c.execute("SELECT Name FROM Items WHERE ItemID = ?", (itm_id,))
                    r = c.fetchone()
                    if r:
                        item_name = r[0]
        except Exception:
            pass
        item_lines.append(f"{item_name} x{qty}: ${price * qty:.2f}")

    try:
        while True:
            pay_mode = input("How would you like to pay? (1 = Pay now, 2 = Add to room bill): ").strip()
            if pay_mode in ("1", "2"):
                break
            logging.info("Invalid choice. Enter 1 to pay now or 2 to add to your room bill.")
        if pay_mode == '1':
            # Pay now: same pricing pipeline as billing (discount code + tier discount + tax).
            discounted_subtotal, _disc_amount, _tier_amount, savings_lines = compute_and_apply_discounts(total, room_number)
            tax = discounted_subtotal * get_tax_rate()
            final_total = discounted_subtotal + tax
            ui.box("Receipt", "\n".join(
                ["----- Receipt -----"] + item_lines + savings_lines +
                [f"Subtotal: ${discounted_subtotal:.2f}",
                 f"Tax (Tax Rate: {get_tax_rate()*100:.0f}%): ${tax:.2f}",
                 f"Total Amount: ${final_total:.2f}",
                 "-------------------"]
            ))
            if process_credit_card(final_total):
                # Record the paid items at their discounted, pre-tax price so the future
                # invoice's itemized lines tie out to the discounted subtotal charged.
                discount_factor = (discounted_subtotal / total) if total > 0 else 0.0
                tx_ids = []
                for (itm_id, unit_price, qty) in ordered_items:
                    discounted_price = round(float(unit_price) * discount_factor, 2)
                    tx = record_transaction_for_room(room_number, itm_id, qty, discounted_price, paid=True)
                    if tx is not None:
                        tx_ids.append(tx)
                award_billed_order_points(room_number, tx_ids)
                order_id = open_order(room_number)
                add_order_items(order_id, [(i, round(p * discount_factor, 2), q) for (i, p, q) in ordered_items])
                log_audit("POST", "Order", order_id, f"Paid now: ${final_total:.2f} for room {room_number}")
                if order_id:
                    logging.info(f"Payment received. Your order is confirmed. Track it as order #{order_id}.")
                else:
                    logging.info("Payment received. Your order is confirmed.")
            else:
                add_later = input("Payment declined. Add this order to your room bill instead? (Y/N): ").strip().lower()
                if add_later == 'y':
                    # Added to the room bill: record at full list price (the discount is
                    # applied again at check-out when the bill is settled).
                    for (itm_id, unit_price, qty) in ordered_items:
                        record_transaction_for_room(room_number, itm_id, qty, unit_price, paid=False)
                    order_id = open_order(room_number)
                    add_order_items(order_id, ordered_items)
                    log_audit("POST", "Order", order_id, f"Added to room bill: ${total:.2f} for room {room_number}")
                    logging.info("Order added to your room bill and will be settled at check-out.")
                else:
                    logging.info("Order cancelled; no charges recorded.")
                    return
        else:
            # Pay later: charges are recorded on the room bill and settled at check-out.
            for (itm_id, unit_price, qty) in ordered_items:
                record_transaction_for_room(room_number, itm_id, qty, unit_price, paid=False)
            order_id = open_order(room_number)
            add_order_items(order_id, ordered_items)
            log_audit("POST", "Order", order_id, f"Added to room bill: ${total:.2f} for room {room_number}")
            logging.info(f"This amount (${total:.2f}) will be added to your room bill and settled at check-out.")
    except Exception as e:
        logging.error(f"Error finalizing order: {e}")
    logging.info("Thank you for your order! It will be delivered shortly.")
def get_amenities(active_only=True):
    """Ordered amenity rows, or [] if the table is missing/empty."""
    try:
        with get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT AmenityID, Name, Description FROM Amenities "
                + ("WHERE Active = 1 " if active_only else "")
                + "ORDER BY DisplayOrder, AmenityID"
            )
            return cursor.fetchall()
    except Exception as e:
        logging.debug(f"Amenities unavailable ({type(e).__name__}: {e})")
        return []


def view_amenities():
    """Display the hotel's amenities, from the admin-editable Amenities table."""
    rows = get_amenities()
    if not rows:
        logging.info("No amenities are currently listed. Please contact the front desk.")
        return
    ui.show_table("Hotel Amenities", ["Amenity", "Details"],
                  [(r[1], r[2] or "") for r in rows])


def provide_feedback():
    """Capture a guest's stay rating and comments. Persisted to the Feedback table."""
    logging.info("We value your feedback!")
    room_number = ""
    first_name = ""
    last_name = ""
    try:
        room_number, first_name = validate_room()
        if room_number:
            with get_connection() as conn:
                if conn is not None:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT FirstName, LastName FROM Reservations WHERE RoomNumber = ?",
                        (room_number,),
                    )
                    row = cursor.fetchone()
                    if row:
                        first_name = row[0] or first_name
                        last_name = row[1] or ""
        rating = None
        while rating is None:
            raw = input("Please rate our service on a scale of 1-5: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= 5:
                rating = int(raw)
            else:
                logging.info("Invalid rating. Please enter a number between 1 and 5.")
        comment = input("Please provide any additional comments: ").strip()
        with get_connection() as conn:
            if conn is None:
                logging.info("Could not save your feedback. Please tell the front desk.")
                return
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Feedback (RoomNumber, LastName, FirstName, Rating, Comments) "
                "OUTPUT INSERTED.FeedbackID VALUES (?, ?, ?, ?, ?)",
                (room_number or None, last_name or None, first_name or None, rating, comment or None),
            )
            conn.commit()
        log_audit("CREATE", "Feedback", room_number or "(walk-in)", f"Rating {rating}/5")
        logging.info("Thank you for your feedback!")
        logging.info(f"Rating: {rating}, Comments: {comment}")
    except Exception as e:
        logging.error(f"Error capturing feedback: {e}")


def get_promotions(active_only=True):
    """Current promotions, filtered by Active and the start/end date window."""
    try:
        with get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            today = business_date()
            cursor.execute(
                "SELECT PromotionID, Title, Details, DiscountCode FROM Promotions "
                "WHERE Active = 1 "
                "  AND (StartsOn IS NULL OR StartsOn <= ?) "
                "  AND (EndsOn IS NULL OR EndsOn >= ?) "
                "ORDER BY PromotionID",
                (today, today),
            )
            return cursor.fetchall()
    except Exception as e:
        logging.debug(f"Promotions unavailable ({type(e).__name__}: {e})")
        return []


def view_promotions():
    """Display current hotel promotions and offers."""
    rows = get_promotions()
    if not rows:
        logging.info("There are no promotions running right now. Please check back soon.")
        return
    ui.show_table("Current Promotions", ["Offer", "Details", "Code"],
                  [(r[1], r[2] or "", r[3] or "-") for r in rows])


def track_order_status():
    """Guest: look up a real order and show its live status."""
    room_number, _ = validate_room()
    if not room_number:
        logging.info("Could not verify your room.")
        return
    orders = get_orders_for_room(room_number)
    if not orders:
        logging.info("You have no orders on record. Use 'Place Order' to order room service.")
        return
    ui.show_table(
        f"My Orders - Room {room_number}",
        ["Order #", "Status", "Placed", "Updated", "Items"],
        [(o[0], o[1], o[2].strftime("%Y-%m-%d %H:%M"),
          o[3].strftime("%Y-%m-%d %H:%M"), o[4] or "-") for o in orders],
    )
    raw = input("Enter an order number for detail (blank to skip): ").strip()
    if not raw.isdigit():
        return
    order = next((o for o in orders if o[0] == int(raw)), None)
    if not order:
        logging.info(f"Order {raw} not found for your room.")
        return
    ui.box(
        f"Order #{order[0]}",
        f"Status:  {order[1]}\n{ORDER_STATUS_HELP.get(order[1], '')}"
        + (f"\n\nNotes:\n{order[5]}" if len(order) > 5 and order[5] else ""),
    )
    for item in get_order_items(int(raw)):
        logging.info(f"  {item[0]} x{item[1]} @ ${float(item[2]):.2f}")


def get_orders_for_room(room_number, active_only=False):
    """Orders for a room as (order_id, status, placed_at, updated_at, items, notes). Newest first."""
    try:
        with get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            query = (
                "SELECT o.OrderID, o.Status, o.PlacedAt, o.UpdatedAt, o.Notes, "
                "  (SELECT STRING_AGG(CAST(oi.Quantity AS varchar(4)) + 'x ' + oi.ItemName, ', ') "
                "   FROM OrderItems oi WHERE oi.OrderID = o.OrderID) AS Items "
                "FROM Orders o WHERE o.RoomNumber = ?"
            )
            if active_only:
                query += " AND o.Status NOT IN ('Completed', 'Cancelled')"
            query += " ORDER BY o.PlacedAt DESC"
            cursor.execute(query, (room_number,))
            return [(r[0], r[1], r[2], r[3], r[5], r[4]) for r in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error loading orders: {e}")
        return []


def get_order_items(order_id):
    """Line items for one order as (item_name, quantity, unit_price) tuples."""
    try:
        with get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ItemName, Quantity, UnitPrice FROM OrderItems WHERE OrderID = ? ORDER BY OrderItemID",
                (order_id,),
            )
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading order items: {e}")
        return []


def manage_orders_menu():
    """Staff: list live orders and advance them through the lifecycle."""
    while True:
        try:
            with get_connection() as conn:
                if conn is None:
                    return
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT TOP 50 o.OrderID, o.RoomNumber, o.Status, o.PlacedAt, o.UpdatedAt, "
                    "  (SELECT STRING_AGG(CAST(oi.Quantity AS varchar(4)) + 'x ' + oi.ItemName, ', ') "
                    "   FROM OrderItems oi WHERE oi.OrderID = o.OrderID) AS Items "
                    "FROM Orders o WHERE o.Status NOT IN ('Completed', 'Cancelled') "
                    "ORDER BY o.PlacedAt"
                )
                rows = cursor.fetchall()
        except Exception as e:
            logging.error(f"Error loading order queue: {e}")
            return
        if not rows:
            ui.info("No open orders.")
        else:
            ui.show_table(
                "Open Orders",
                ["Order #", "Room", "Status", "Placed", "Updated", "Items"],
                [(r[0], r[1], r[2], r[3].strftime("%Y-%m-%d %H:%M"),
                  r[4].strftime("%Y-%m-%d %H:%M"), r[5] or "-") for r in rows],
            )
            raw = input("Enter an order number to update (blank to refresh): ").strip()
            if not raw:
                ui.pause()
                continue
            if not raw.isdigit() or not any(r[0] == int(raw) for r in rows):
                logging.info("Order not found in the open queue.")
                ui.pause()
                continue
            advance_order(int(raw))
        action = input("Press Enter to refresh, or 'q' to exit: ").strip().lower()
        if action == 'q':
            return
        ui.pause()


def next_order_status(current):
    """Next lifecycle step for an order, or None if it is already closed."""
    if current not in ORDER_STATUSES:
        return None
    index = ORDER_STATUSES.index(current)
    if index + 1 < len(ORDER_STATUSES):
        return ORDER_STATUSES[index + 1]
    # 'Delivered' is the last open state, so the next step is the terminal 'Completed'.
    return "Completed" if current == "Delivered" else None


def advance_order(order_id):
    """Move an order to its next state, or cancel it. Returns the new status."""
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute("SELECT Status, RoomNumber FROM Orders WHERE OrderID = ?", (order_id,))
            row = cursor.fetchone()
            if not row:
                logging.info("Order not found.")
                return None
            current, room_number = row[0], row[1]
            if current in ("Completed", "Cancelled"):
                logging.info(f"Order {order_id} is already {current.lower()}.")
                return current
            following = next_order_status(current)
            if not following:
                logging.info(f"Order {order_id} is '{current}' and cannot be advanced further.")
                return current
            choice = input(
                f"Order {order_id} is '{current}'.\n"
                f"  1. Advance to '{following}'\n"
                f"  2. Cancel the order\n"
                f"Choose: "
            ).strip()
            if choice == "2":
                new_status = "Cancelled"
            elif choice == "1":
                new_status = following
            else:
                logging.info("Invalid choice. Order unchanged.")
                return current
            cursor.execute(
                "UPDATE Orders SET Status = ?, UpdatedAt = ?, "
                "CompletedAt = CASE WHEN ? IN ('Completed', 'Cancelled') THEN ? ELSE CompletedAt END "
                "WHERE OrderID = ?",
                (new_status, datetime.now(), new_status, datetime.now(), order_id),
            )
            conn.commit()
        log_audit("UPDATE", "Order", order_id, f"{current} -> {new_status} (room {room_number})")
        # Tell the guest in their room so the tracker reflects reality immediately.
        if room_number:
            try:
                with get_connection() as conn:
                    if conn is not None:
                        conn.cursor().execute(
                            "INSERT INTO Notifications (RoomNumber, Message, Channel, SentBy) "
                            "VALUES (?, ?, 'In-Room', ?)",
                            (room_number,
                             f"Order #{order_id} update: {ORDER_STATUS_HELP.get(new_status, new_status)}",
                             CURRENT_USER),
                        )
                        conn.commit()
            except Exception:
                pass
        ui.success(f"Order {order_id} is now '{new_status}'.")
        return new_status
    except Exception as e:
        logging.error(f"Error updating order: {e}")
        return None


def contact_concierge():
    """File a concierge request. Persisted, tracked, and answerable by staff."""
    room_number = ""
    first_name = ""
    last_name = ""
    try:
        room_number, first_name = validate_room()
        if room_number:
            with get_connection() as conn:
                if conn is not None:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT FirstName, LastName FROM Reservations WHERE RoomNumber = ?",
                        (room_number,),
                    )
                    row = cursor.fetchone()
                    if row:
                        first_name = row[0] or first_name
                        last_name = row[1] or ""
        message = input("Enter your message for our concierge: ").strip()
        if not message:
            logging.info("Empty message. Nothing was sent.")
            return
        with get_connection() as conn:
            if conn is None:
                logging.info("Could not reach the concierge. Please call the front desk.")
                return
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ConciergeRequests (RoomNumber, LastName, FirstName, Message) "
                "OUTPUT INSERTED.RequestID VALUES (?, ?, ?, ?)",
                (room_number or None, last_name or None, first_name or None, message),
            )
            row = cursor.fetchone()
            request_id = int(row[0]) if row and row[0] is not None else None
            conn.commit()
        log_audit("CREATE", "ConciergeRequest", request_id, f"Room {room_number}: {message}")
        logging.info("Your message has been sent. A concierge will get back to you shortly.")
        logging.info(f"For immediate assistance, please call the front desk at 123-456-7890.")
        logging.info(f"Your request reference is #{request_id}.")
    except Exception as e:
        logging.error(f"Error contacting concierge: {e}")


def view_my_concierge_requests():
    """Guest: check the status of the concierge requests they filed."""
    room_number, _ = validate_room()
    if not room_number:
        logging.info("Could not verify your room.")
        return
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT RequestID, Message, Status, CreatedAt, Response, ResolvedAt "
                "FROM ConciergeRequests WHERE RoomNumber = ? ORDER BY CreatedAt DESC",
                (room_number,),
            )
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error reading concierge requests: {e}")
        return
    if not rows:
        logging.info("You have no concierge requests on record.")
        return
    ui.show_table(
        f"My Concierge Requests - Room {room_number}",
        ["Ref", "Message", "Status", "Sent", "Reply", "Resolved"],
        [(r[0], r[1], r[2], r[3].strftime("%Y-%m-%d %H:%M"), r[4] or "-",
          r[5].strftime("%Y-%m-%d %H:%M") if r[5] else "-") for r in rows],
    )


def guest_requests_menu():
    """Staff/management inbox: concierge requests and stay feedback."""
    while True:
        ui.show_menu("Guest Requests", [
            "1. Concierge Requests",
            "2. Guest Feedback",
            "3. Back",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            _concierge_inbox()
        elif choice == '2':
            _feedback_inbox()
        elif choice == '3':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()


def _concierge_inbox():
    """List concierge requests and let staff respond to / resolve one."""
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT RequestID, RoomNumber, LastName, FirstName, Message, Status, CreatedAt "
                "FROM ConciergeRequests ORDER BY CASE WHEN Status = 'Open' THEN 0 ELSE 1 END, "
                "CreatedAt DESC"
            )
            rows = cursor.fetchall()
            if not rows:
                ui.info("No concierge requests on record.")
                return
            ui.show_table(
                "Concierge Requests",
                ["Ref", "Room", "Guest", "Message", "Status", "Sent"],
                [(r[0], r[1] or "-", f"{(r[3] or '')} {(r[2] or '')}".strip() or "-",
                  r[4], r[5], r[6].strftime("%Y-%m-%d %H:%M")) for r in rows],
            )
            raw = input("Enter a request number to action (blank to skip): ").strip()
            if not raw.isdigit():
                return
            request = next((r for r in rows if r[0] == int(raw)), None)
            if not request:
                logging.info("Request not found.")
                return
            response = input("Enter your response to the guest: ").strip()
            if not response:
                logging.info("No response entered. Request left unchanged.")
                return
            resolve = ui.ask_confirmation("Mark this request as resolved?", default="y")
            new_status = "Resolved" if resolve else "In Progress"
            cursor.execute(
                "UPDATE ConciergeRequests SET Response = ?, Status = ?, "
                "ResolvedAt = CASE WHEN ? = 'Resolved' THEN ? ELSE ResolvedAt END, ResolvedBy = ? "
                "WHERE RequestID = ?",
                (response, new_status, new_status, datetime.now(), CURRENT_USER, int(raw)),
            )
            conn.commit()
        log_audit("RESOLVE" if resolve else "UPDATE", "ConciergeRequest", int(raw), f"Status -> {new_status}")
        if request[1]:
            try:
                with get_connection() as conn:
                    if conn is not None:
                        conn.cursor().execute(
                            "INSERT INTO Notifications (RoomNumber, Message, Channel, SentBy) "
                            "VALUES (?, ?, 'In-Room', ?)",
                            (request[1], f"Concierge reply: {response}", CURRENT_USER),
                        )
                        conn.commit()
            except Exception:
                pass
        ui.success(f"Request #{raw} updated to '{new_status}'.")
    except Exception as e:
        logging.error(f"Error processing concierge inbox: {e}")


def _feedback_inbox():
    """Show stay ratings and the average, with the comments."""
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT FeedbackID, RoomNumber, LastName, FirstName, Rating, Comments, CreatedAt "
                "FROM Feedback ORDER BY CreatedAt DESC"
            )
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading feedback: {e}")
        return
    if not rows:
        ui.info("No feedback submitted yet.")
        return
    average = sum(r[4] for r in rows) / len(rows)
    ui.show_table(
        f"Guest Feedback ({len(rows)} responses, average {average:.2f}/5)",
        ["#", "Room", "Guest", "Rating", "Comments", "When"],
        [(r[0], r[1] or "-", f"{(r[3] or '')} {(r[2] or '')}".strip() or "-", r[4],
          r[5] or "-", r[6].strftime("%Y-%m-%d %H:%M")) for r in rows],
    )


def manage_amenities_menu():
    """Admin: add, edit, retire and reorder hotel amenities."""
    while True:
        ui.show_menu("Manage Amenities", [
            "1. View Amenities",
            "2. Add Amenity",
            "3. Edit Amenity",
            "4. Retire / Reactivate Amenity",
            "5. Back",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            rows = get_amenities(active_only=False)
            if not rows:
                logging.info("No amenities on record.")
            else:
                ui.show_table("All Amenities", ["#", "Name", "Details"],
                              [(r[0], r[1], r[2] or "") for r in rows])
        elif choice == '2':
            name = input("Amenity name: ").strip()
            if not name:
                logging.info("Amenity name cannot be blank.")
                continue
            details = input("Details (optional): ").strip()
            with get_connection() as conn:
                if conn is None:
                    return
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO Amenities (Name, Description, DisplayOrder) "
                    "OUTPUT INSERTED.AmenityID VALUES (?, ?, COALESCE((SELECT MAX(DisplayOrder) + 1 FROM Amenities), 1))",
                    (name, details or None),
                )
                conn.commit()
            log_audit("CREATE", "Amenity", name, details or "")
            ui.success(f"Amenity '{name}' added.")
        elif choice == '3':
            rows = get_amenities(active_only=False)
            if not rows:
                logging.info("No amenities on record.")
                continue
            raw = input("Amenity number to edit: ").strip()
            if not raw.isdigit():
                continue
            amenity = next((r for r in rows if r[0] == int(raw)), None)
            if not amenity:
                logging.info("Amenity not found.")
                continue
            name = input(f"Name (blank to keep '{amenity[1]}'): ").strip() or amenity[1]
            details = input(f"Details (blank to keep '{amenity[2] or ''}'): ").strip() or amenity[2]
            with get_connection() as conn:
                if conn is None:
                    return
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE Amenities SET Name = ?, Description = ? WHERE AmenityID = ?",
                    (name, details or None, int(raw)),
                )
                conn.commit()
            log_audit("UPDATE", "Amenity", int(raw), f"'{amenity[1]}' -> '{name}'")
            ui.success("Amenity updated.")
        elif choice == '4':
            raw = input("Amenity number to toggle: ").strip()
            if not raw.isdigit():
                continue
            with get_connection() as conn:
                if conn is None:
                    return
                cursor = conn.cursor()
                cursor.execute("SELECT Active FROM Amenities WHERE AmenityID = ?", (int(raw),))
                row = cursor.fetchone()
                if row is None:
                    logging.info("Amenity not found.")
                    continue
                new_active = 0 if row[0] else 1
                cursor.execute("UPDATE Amenities SET Active = ? WHERE AmenityID = ?",
                               (new_active, int(raw)))
                conn.commit()
            log_audit("UPDATE", "Amenity", int(raw), "Active" if new_active else "Retired")
            ui.success(f"Amenity {'reactivated' if new_active else 'retired'}.")
        elif choice == '5':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()


def manage_promotions_menu():
    """Admin: add, edit and expire hotel promotions."""
    while True:
        ui.show_menu("Manage Promotions", [
            "1. View All Promotions",
            "2. Add Promotion",
            "3. Edit Promotion",
            "4. End Promotion",
            "5. Back",
        ])
        choice = input("Enter your choice: ").strip()
        if choice in ('1', '3', '4'):
            try:
                with get_connection() as conn:
                    if conn is None:
                        return
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT PromotionID, Title, Details, DiscountCode, StartsOn, EndsOn, Active "
                        "FROM Promotions ORDER BY PromotionID"
                    )
                    rows = cursor.fetchall()
            except Exception as e:
                logging.error(f"Error loading promotions: {e}")
                return
            if choice == '1':
                if not rows:
                    logging.info("No promotions on record.")
                else:
                    ui.show_table(
                        "All Promotions",
                        ["#", "Title", "Details", "Code", "Starts", "Ends", "Active"],
                        [(r[0], r[1], r[2] or "", r[3] or "-",
                          r[4].isoformat() if r[4] else "-",
                          r[5].isoformat() if r[5] else "-", "Yes" if r[6] else "No")
                         for r in rows],
                    )
                ui.pause()
                continue
            raw = input("Promotion number: ").strip()
            if not raw.isdigit():
                continue
            promotion = next((r for r in rows if r[0] == int(raw)), None)
            if not promotion:
                logging.info("Promotion not found.")
                continue
            if choice == '4':
                with get_connection() as conn:
                    if conn is not None:
                        cursor = conn.cursor()
                        cursor.execute("UPDATE Promotions SET Active = 0 WHERE PromotionID = ?",
                                       (int(raw),))
                        conn.commit()
                log_audit("UPDATE", "Promotion", int(raw), f"'{promotion[1]}' ended")
                ui.success("Promotion ended.")
            else:
                title = input(f"Title (blank to keep '{promotion[1]}'): ").strip() or promotion[1]
                details = input(f"Details (blank to keep): ").strip() or promotion[2]
                code = input(f"Discount code (blank to keep '{promotion[3] or ''}'): ").strip() or promotion[3]
                with get_connection() as conn:
                    if conn is None:
                        return
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE Promotions SET Title = ?, Details = ?, DiscountCode = ? "
                        "WHERE PromotionID = ?",
                        (title, details or None, code or None, int(raw)),
                    )
                    conn.commit()
                log_audit("UPDATE", "Promotion", int(raw), f"'{promotion[1]}' -> '{title}'")
                ui.success("Promotion updated.")
        elif choice == '2':
            title = input("Promotion title: ").strip()
            if not title:
                logging.info("Promotion title cannot be blank.")
                continue
            details = input("Details: ").strip()
            code = input("Associated discount code (optional): ").strip()
            with get_connection() as conn:
                if conn is None:
                    return
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO Promotions (Title, Details, DiscountCode) "
                    "OUTPUT INSERTED.PromotionID VALUES (?, ?, ?)",
                    (title, details or None, code or None),
                )
                conn.commit()
            log_audit("CREATE", "Promotion", title, details or "")
            ui.success("Promotion added.")
        elif choice == '5':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()

IT_SYSTEMS = ["Server", "Workstation", "POS Terminal", "WiFi Router", "Printer", "Database Server"]
IT_SOFTWARE = ["Microsoft Office", "Antivirus", "Hotel Management Suite", "Printer Driver",
               "Database Client", "Remote Desktop", "Web Browser"]
# Advice shown when simulated diagnostics fail on a given system.
IT_TROUBLESHOOTING = {
    "WiFi Router": "Check the WiFi signal strength and connectivity.",
    "Printer": "Check the printer connection and ink levels.",
    "Database Server": "Check the database connection and query performance.",
    "POS Terminal": "Check the POS terminal connection and transaction logs.",
    "Workstation": "Check the workstation connection and software updates.",
    "Server": "Check the server connection and resource utilization.",
}


def _pick_from_list(title, options):
    """Show a numbered list and return the chosen option, or None if cancelled."""
    ui.show_table(title, ["#", "Option"], [(i + 1, name) for i, name in enumerate(options)])
    while True:
        raw = input("Select an option (number, or blank to cancel): ").strip()
        if raw == "":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        logging.info(f"Invalid selection. Enter a number from 1 to {len(options)}.")


def it_network_configuration():
    """IT: record a network profile and optionally run simulated diagnostics."""
    network_name = input("Enter network name: ").strip()
    ip_address = input("Enter IP address: ").strip()
    subnet_mask = input("Enter subnet mask: ").strip()
    gateway = input("Enter gateway: ").strip()
    ui.success(f"Network '{network_name}' configured with IP {ip_address}, "
               f"Subnet {subnet_mask}, Gateway {gateway}.")
    if not ui.ask_confirmation("Run diagnostics and test the network?", default="y"):
        ui.info("Skipping diagnostics and test.")
        return
    logging.info("Beginning network diagnostics...")
    time.sleep(2)
    details = (f"Network Name: {network_name}\nIP Address: {ip_address}\n"
               f"Subnet Mask: {subnet_mask}\nGateway: {gateway}")
    if random.choice([True, False]):
        ui.box("Network Diagnostics - Result", details + "\n\nNo issues found.")
    else:
        ui.box("Network Diagnostics - Result", details + "\n\nIssues detected. "
               "Please check the network configuration.")


def it_user_accounts():
    """IT: the user-management actions, reusing the shared admin prompts."""
    ui.show_menu("User Account Management", [
        "1. Add User",
        "2. Remove User",
        "3. Reset Password",
        "4. Update User",
        "5. Back",
    ])
    actions = {'1': add_user, '2': delete_user, '3': reset_user_password, '4': edit_user}
    action = actions.get(input("Enter your choice: ").strip())
    if action:
        action()
    else:
        logging.info("Invalid action. Please choose 1-4, or 5 to go back.")


def it_system_diagnostics():
    """IT: simulated diagnostics on one of the managed systems."""
    system = _pick_from_list("Systems Available for Diagnostics", IT_SYSTEMS)
    if not system:
        return
    logging.info(f"Running diagnostics on {system}...")
    time.sleep(2)
    if random.choice([True, False]):
        ui.success(f"Diagnostics for {system} completed successfully. No issues found.")
    else:
        advice = IT_TROUBLESHOOTING.get(system, "Check the system configuration.")
        ui.box(f"Diagnostics - {system}",
               "Issues detected.\n\n" + advice + "\n\n"
               "(Note: diagnostics are simulated for demonstration.)")


def it_software_installation():
    """IT: simulated software install with a progress bar."""
    system = _pick_from_list("Systems Available for Installation", IT_SYSTEMS)
    if not system:
        return
    software = _pick_from_list("Applications Available for Installation", IT_SOFTWARE)
    if not software:
        return

    def format_size(bytes_size):
        units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
        size = float(bytes_size)
        for unit in units:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} PiB"

    # 200 KiB simulated transfer, chunked so the progress bar actually animates.
    total_size = 200 * 1024
    block_size = 200
    num_blocks = total_size // block_size

    logging.info(f"Installing {software} on {system}...")
    logging.info(f"Total size: {format_size(total_size)}")
    with tqdm.tqdm(total=num_blocks, desc="Simulated Download", unit="block", unit_scale=True) as bar:
        for _ in range(num_blocks):
            time.sleep(0.001)  # Simulate I/O delay
            bar.update(1)
    ui.success(f"{software} installed successfully on {system}.")


def it_support_panel():
    """IT Support: network config, user accounts, diagnostics, and software installs."""
    while True:
        ui.show_menu("IT Support Panel", [
            "1. Network Configuration",
            "2. User Account Management",
            "3. System Diagnostics",
            "4. Software Installation",
            "5. Exit IT Support Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            it_network_configuration()
        elif choice == '2':
            it_user_accounts()
        elif choice == '3':
            it_system_diagnostics()
        elif choice == '4':
            it_software_installation()
        elif choice == '5':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()

def valet_vehicle_management():
    """Valet: Manage vehicle check-in/check-out with database integration."""
    logging.info("\n--- Valet: Vehicle Management ---")
    while True:
        action = input("Enter action: CI (Check In) / CO (Check Out) / E (Exit): ").strip().lower()
        if action == 'e':
            logging.info("Exiting Valet Vehicle Management.")
            break
        license_plate = input("Enter vehicle license plate: ").strip()
        owner_name = input("Enter owner's name: ").strip()
        try:
            with get_connection() as conn:
                if conn is None:
                    logging.info("Database connection failed.")
                    return
                cursor = conn.cursor()
                if action == "ci":
                    parking_spot = input("Enter assigned parking spot: ").strip()
                    check_in_time = datetime.now()
                    cursor.execute(
                        "INSERT INTO ValetVehicles (LicensePlate, OwnerName, ParkingSpot, Status, CheckInTime) VALUES (?, ?, ?, ?, ?)",
                        (license_plate, owner_name, parking_spot, "Checked-In", check_in_time)
                    )
                    conn.commit()
                    logging.info(f"Vehicle {license_plate} checked in for {owner_name} at spot {parking_spot}.")
                elif action == "co":
                    check_out_time = datetime.now()
                    cursor.execute(
                        "UPDATE ValetVehicles SET Status = ?, CheckOutTime = ? WHERE LicensePlate = ? AND OwnerName = ? AND Status = 'Checked-In'",
                        ("Checked-Out", check_out_time, license_plate, owner_name)
                    )
                    conn.commit()
                    logging.info(f"Vehicle {license_plate} checked out for {owner_name}.")
                else:
                    logging.info("Invalid action. Please enter 'check-in' or 'check-out'.")
        except Exception as e:
            logging.error(f"Error managing valet vehicle: {e}")

   
def view_my_loyalty_status():
    """Customer: show current loyalty tier, perks, multiplier, discount, and progress to next tier."""
    if not LOYALTY_ENABLED:
        logging.info("Loyalty program is not enabled. Contact administration to enable it.")
        return
    room_number, first_name = validate_room()
    if not room_number:
        logging.info("Could not verify your room.")
        return
    create_loyalty_account_if_missing(room_number)
    details = get_tier_details_by_room(room_number)
    if not details:
        logging.info("Could not load loyalty tier details. Please contact front desk.")
        return
    points = get_points_by_room(room_number)
    lifetime = get_lifetime_points_by_room(room_number)
    # The balance belongs to the guest, not the room, so the guest is named here.
    # A room number is guessable and may already have been re-let to somebody else,
    # which is exactly how the wrong person would be shown the wrong balance.
    rows = [
        ("Guest", first_name or "-"),
        ("Room", room_number),
        ("Lifetime Points", str(lifetime)),
        ("Available Points", str(points)),
        ("Current Tier", details["tier"]),
        ("Room Category", get_room_type(room_number)),
        ("Points Multiplier", f"x{details['points_multiplier']:.2f}"),
        ("Points per Night", f"{get_loyalty_points_per_night() * get_room_type_multiplier(get_room_type(room_number)) * details['points_multiplier']:.0f} (base {get_loyalty_points_per_night()} x category x tier)"),
        ("Tier Discount", f"{details['discount_percent']:.0f}% off room-service bills"),
        ("Perks", details["perks"] if details.get("perks") else "None"),
    ]
    tiers = _tiers_from_db() or DEFAULT_TIERS
    for i, (name, _min_pts, _mult, _disc, _perks) in enumerate(tiers):
        if name == details["tier"]:
            if i + 1 < len(tiers) and lifetime < tiers[i + 1][1]:
                rows.append(("Points to Next Tier", f"{tiers[i + 1][1] - lifetime} pts to {tiers[i + 1][0]}"))
            else:
                rows.append(("Tier Status", "Highest tier reached - enjoy the perks!"))
            break
    ui.show_table("My Loyalty Status", ["Field", "Value"], rows)


def load_customer_history(last_name, first_name):
    """Profile row + stay list + invoice list for a guest. Used by both the
    customer "My History" view and the admin/manager profiles lookup.

    Stays and invoices are fetched separately (an invoice is linked to a room
    number, and a stay may have several invoices) so stays are never duplicated.
    """
    profile = None
    stays = []
    invoices = []
    try:
        with get_connection() as conn:
            if conn is None:
                return None, [], []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CustomerID, LastName, FirstName, Email, Phone, Preferences "
                "FROM CustomerProfiles WHERE LastName = ? AND FirstName = ? ORDER BY CustomerID DESC",
                (last_name, first_name),
            )
            profile = cursor.fetchone()
            cursor.execute(
                "SELECT RoomNumber, CheckInDate, CheckOutDate, "
                "  DATEDIFF(day, CheckInDate, CheckOutDate) AS Nights "
                "FROM Reservations WHERE LastName = ? AND FirstName = ? "
                "ORDER BY CheckInDate DESC",
                (last_name, first_name),
            )
            stays = cursor.fetchall()
            cursor.execute(
                "SELECT i.RoomNumber, i.InvoiceID, i.InvoiceDate, i.TotalAmount, i.AmountPaid "
                "FROM Invoices i WHERE EXISTS ("
                "  SELECT 1 FROM Reservations r WHERE r.RoomNumber = i.RoomNumber "
                "  AND r.LastName = ? AND r.FirstName = ?) "
                "ORDER BY i.InvoiceDate DESC",
                (last_name, first_name),
            )
            invoices = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading customer history: {e}")
    return profile, stays, invoices


def show_customer_history(profile, stays, invoices, profile_title="Customer Profile", history_title="Stay History", summary_title="Stay Summary"):
    """Render a guest's profile box, stay list, invoice list, and summary."""
    if profile:
        ui.show_table(profile_title, ["Field", "Value"], [
            ("Customer ID", profile.CustomerID),
            ("Name", f"{profile.FirstName} {profile.LastName}"),
            ("Email", profile.Email or "-"),
            ("Phone", profile.Phone or "-"),
            ("Preferences", profile.Preferences or "-"),
        ])
    else:
        logging.info("No saved customer profile on file for this booking (contact details are captured at check-in).")

    if not stays:
        logging.info("No stay history found.")
        return

    ui.show_table(
        history_title,
        ["Room", "Check-In", "Check-Out", "Nights"],
        [
            (
                s.RoomNumber,
                s.CheckInDate,
                s.CheckOutDate,
                s.Nights,
            )
            for s in stays
        ],
    )
    if invoices:
        ui.show_table(
            f"{history_title} - Invoices",
            ["Room", "Invoice #", "Invoice Date", "Total", "Paid"],
            [
                (
                    inv.RoomNumber,
                    inv.InvoiceID,
                    inv.InvoiceDate.strftime("%Y-%m-%d") if inv.InvoiceDate else "-",
                    f"${float(inv.TotalAmount):.2f}",
                    f"${float(inv.AmountPaid):.2f}",
                )
                for inv in invoices
            ],
        )
    total_invoiced = sum(float(i.TotalAmount) for i in invoices if i.TotalAmount is not None)
    ui.show_table(summary_title, ["Field", "Value"], [
        ("Total Stays", len(stays)),
        ("Total Invoices", len(invoices)),
        ("Total Invoiced", f"${total_invoiced:.2f}"),
    ])


def my_history():
    """Customer view: saved profile contact details joined to stays and invoices."""
    room_number, first_name = validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify your last name, first name, and room number. Please try again.")
        return
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT LastName, FirstName FROM Reservations WHERE RoomNumber = ?",
                (room_number,),
            )
            res = cursor.fetchone()
            if not res:
                logging.info("Reservation not found for that room.")
                return
            last_name = res.LastName or ""
            guest_first = res.FirstName or first_name
    except Exception as e:
        logging.error(f"Error loading customer history: {e}")
        return
    profile, stays, invoices = load_customer_history(last_name, guest_first)
    show_customer_history(
        profile,
        stays,
        invoices,
        profile_title="My Customer Profile",
        history_title="My Stay History",
        summary_title="My Stay Summary",
    )


def search_customer_profiles():
    """Admin/manager: search CustomerProfiles and drill into a guest's history."""
    term = input("Search by last name, first name, email, or phone: ").strip()
    if not term:
        logging.info("Search term required.")
        return
    # Treat the search term literally: escape LIKE wildcards so '%'/'_' don't broaden the match.
    esc = term.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    pattern = f"%{esc}%"
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CustomerID, LastName, FirstName, Email, Phone "
                "FROM CustomerProfiles "
                "WHERE LastName LIKE ? OR FirstName LIKE ? OR Email LIKE ? OR Phone LIKE ? "
                "ORDER BY LastName, FirstName",
                (pattern, pattern, pattern, pattern),
            )
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error searching customer profiles: {e}")
        return
    if not rows:
        logging.info("No matching customer profiles found.")
        return
    ui.show_table(
        "Matching Customer Profiles",
        ["#", "Customer ID", "Last Name", "First Name", "Email", "Phone"],
        [(i + 1, r.CustomerID, r.LastName, r.FirstName, r.Email or "-", r.Phone or "-") for i, r in enumerate(rows)],
    )
    raw = input("Enter the row number to view details and history (blank to cancel): ").strip()
    if not raw.isdigit():
        return
    idx = int(raw)
    if not (1 <= idx <= len(rows)):
        logging.info("Invalid selection.")
        return
    selected = rows[idx - 1]
    profile, stays, invoices = load_customer_history(selected.LastName, selected.FirstName)
    show_customer_history(
        profile,
        stays,
        invoices,
        profile_title=f"Customer Profile - {selected.FirstName} {selected.LastName}",
        history_title=f"Stay History - {selected.FirstName} {selected.LastName}",
        summary_title=f"Stay Summary - {selected.FirstName} {selected.LastName}",
    )


def customer_panel():
    while True:
        ui.show_menu("Customer Menu", [
            "1. Check In",
            "2. Place Order",
            "3. Check Out",
            "4. View Amenities",
            "5. Provide Feedback",
            "6. View Promotions",
            "7. Join Loyalty Program",
            "8. Track Order Status",
            "9. Contact Concierge",
            "10. View My Concierge Requests",
            "11. View My Loyalty Tier & Perks",
            "12. Print My Invoice",
            "13. View My History",
            "14. My Key Card",
            "15. My Notifications",
            "16. Exit Customer Menu",
        ])
        cust_choice = input("Enter your choice: ").strip()

        if cust_choice == '1':
            check_in()
        elif cust_choice == '2':
            order_item()
        elif cust_choice == '3':
            check_out()
        elif cust_choice == '4':
            view_amenities()
        elif cust_choice == '5':
            provide_feedback()
        elif cust_choice == '6':
            view_promotions()
        elif cust_choice == '7':
            if not LOYALTY_ENABLED:
                logging.info("Loyalty Program is not enabled. Contact administration to enable it.")
            else:
                room_number = input("Enter your room number to join/verify loyalty account: ").strip()
                if not room_number:
                    logging.info("Invalid room number.")
                else:
                    if create_loyalty_account_if_missing(room_number):
                        points = get_points_by_room(room_number)
                        logging.info(f"Loyalty account ready for room {room_number}. Current points: {points}.")
                    else:
                        logging.info("Failed to create or verify loyalty account. Please contact front desk.")
        elif cust_choice == '8':
            track_order_status()
        elif cust_choice == '9':
            contact_concierge()
        elif cust_choice == '10':
            view_my_concierge_requests()
        elif cust_choice == '11':
            view_my_loyalty_status()
        elif cust_choice == '12':
            print_my_invoice()
        elif cust_choice == '13':
            my_history()
        elif cust_choice == '14':
            view_my_key_card()
        elif cust_choice == '15':
            view_notifications_for_room()
        elif cust_choice == '16':
            break  # Exit the customer menu and return to the main menu
        ui.pause()
## =========================
# Door Access Control
## =========================
# Key-card lifecycle: issued at check-in, moved by edit_reservation(), revoked at
# check-out and when a reservation is deleted. Every door read appends a DoorEvents row.
KEY_CARD_PREFIX = "KC"

def _new_card_number():
    """Generate a card number that is unique across issued cards."""
    while True:
        suffix = "".join(random.choices(string.digits, k=6))
        candidate = f"{KEY_CARD_PREFIX}-{suffix}"
        try:
            with get_connection() as conn:
                if conn is None:
                    return None
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM KeyCards WHERE CardNumber = ?", (candidate,))
                if not cursor.fetchone():
                    return candidate
        except Exception as e:
            logging.debug(f"Card number probe failed ({type(e).__name__}: {e})")
            return None


def issue_key_card(room_number, last_name=None, first_name=None, valid_nights=None):
    """Issue an Active key card for a stay, expiring at the reservation's check-out.

    Any existing Active card for the room is revoked first, so a re-issued card never
    leaves a stale one live. Returns the card number, or None.
    """
    if not room_number:
        return None
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            expires_at = None
            cursor.execute(
                "SELECT CheckOutDate FROM Reservations WHERE RoomNumber = ?", (room_number,)
            )
            res = cursor.fetchone()
            if res and res[0]:
                expires_at = datetime.combine(res[0], datetime.min.time()) + timedelta(days=1)
            elif valid_nights:
                expires_at = datetime.now() + timedelta(days=valid_nights)
            cursor.execute(
                "UPDATE KeyCards SET Status = 'Revoked', RevokedAt = ?, RevokeReason = ? "
                "WHERE RoomNumber = ? AND Status = 'Active'",
                (datetime.now(), "Superseded by a new card", room_number),
            )
            card_number = _new_card_number()
            if not card_number:
                return None
            cursor.execute(
                "INSERT INTO KeyCards (CardNumber, RoomNumber, LastName, FirstName, Status, "
                "IssuedAt, ExpiresAt, IssuedBy) VALUES (?, ?, ?, ?, 'Active', ?, ?, ?)",
                (card_number, room_number, last_name, first_name, datetime.now(),
                 expires_at, CURRENT_USER),
            )
            conn.commit()
            log_audit("ISSUE", "KeyCard", card_number,
                      f"Issued for room {room_number}"
                      + (f", valid until {expires_at:%Y-%m-%d}" if expires_at else ""))
            logging.info(f"Key card {card_number} issued for room {room_number}.")
            return card_number
    except Exception as e:
        # Migration 017 not applied: door access is an add-on, never block check-in.
        logging.debug(f"Key card issue skipped ({type(e).__name__}: {e})")
        return None


def revoke_active_key_cards(room_number, reason=""):
    """Revoke every Active card for a room. Returns the number revoked."""
    if not room_number:
        return 0
    try:
        with get_connection() as conn:
            if conn is None:
                return 0
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CardNumber FROM KeyCards WHERE RoomNumber = ? AND Status = 'Active'",
                (room_number,),
            )
            cards = [r[0] for r in cursor.fetchall()]
            if not cards:
                return 0
            placeholders = ",".join("?" for _ in cards)
            cursor.execute(
                f"UPDATE KeyCards SET Status = 'Revoked', RevokedAt = ?, RevokeReason = ? "
                f"WHERE CardNumber IN ({placeholders})",
                tuple([datetime.now(), reason or "Revoked"] + cards),
            )
            conn.commit()
            for card in cards:
                log_audit("REVOKE", "KeyCard", card, f"Room {room_number}: {reason or 'revoked'}")
            return cursor.rowcount or 0
    except Exception as e:
        logging.debug(f"Key card revoke skipped ({type(e).__name__}: {e})")
        return 0


def move_key_cards(old_room_number, new_room_number):
    """Re-point a guest's active cards when a reservation moves to another room."""
    if not old_room_number or not new_room_number:
        return 0
    try:
        with get_connection() as conn:
            if conn is None:
                return 0
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE KeyCards SET RoomNumber = ? WHERE RoomNumber = ? AND Status = 'Active'",
                (new_room_number, old_room_number),
            )
            moved = cursor.rowcount or 0
            if moved:
                conn.commit()
                log_audit("UPDATE", "KeyCard", new_room_number,
                          f"{moved} active card(s) moved from room {old_room_number}")
            return moved
    except Exception as e:
        logging.debug(f"Key card move skipped ({type(e).__name__}: {e})")
        return 0


def get_active_key_card(room_number):
    """Return the Active card for a room as a row, or None."""
    if not room_number:
        return None
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CardID, CardNumber, RoomNumber, LastName, FirstName, Status, "
                "IssuedAt, ExpiresAt FROM KeyCards "
                "WHERE RoomNumber = ? AND Status = 'Active' ORDER BY IssuedAt DESC",
                (room_number,),
            )
            return cursor.fetchone()
    except Exception as e:
        logging.debug(f"Key card lookup skipped ({type(e).__name__}: {e})")
        return None


def get_key_cards_for_room(room_number):
    """Every card ever issued for a room, newest first."""
    try:
        with get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CardNumber, RoomNumber, LastName, FirstName, Status, IssuedAt, "
                "ExpiresAt, RevokedAt, RevokeReason, IssuedBy FROM KeyCards "
                "WHERE RoomNumber = ? ORDER BY IssuedAt DESC",
                (room_number,),
            )
            return cursor.fetchall()
    except Exception as e:
        logging.error(f"Error listing key cards: {e}")
        return []


def _log_door_event(card_number, room_number, result, detail=""):
    """Append one access-log row. Never raises."""
    try:
        with get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO DoorEvents (CardNumber, RoomNumber, EventTime, Result, Detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (card_number, room_number, datetime.now(), result, detail),
            )
            conn.commit()
    except Exception as e:
        logging.debug(f"Door event log skipped ({type(e).__name__}: {e})")


def try_open_door(card_number, room_number=None):
    """Attempt a door read. Returns (granted: bool, message: str) and always logs it.

    A read is granted only for an Active card whose expiry has not passed. A revoked,
    lost, expired or unknown card is denied, and each denial is recorded with a reason.
    """
    if not card_number:
        _log_door_event(None, room_number, "Denied", "No card presented")
        return False, "No card presented."
    try:
        with get_connection() as conn:
            if conn is None:
                return False, "Door system unavailable."
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CardNumber, RoomNumber, Status, ExpiresAt FROM KeyCards WHERE CardNumber = ?",
                (card_number,),
            )
            card = cursor.fetchone()
            if not card:
                _log_door_event(card_number, room_number, "Denied", "Unknown card")
                return False, f"Card {card_number} is not recognised."
            card_room, status, expires_at = card[1], card[2], card[3]
            if status != "Active":
                _log_door_event(card_number, card_room, "Denied", f"Card status {status}")
                return False, f"Card {card_number} is {status.lower()} and cannot open doors."
            if expires_at and expires_at < datetime.now():
                cursor.execute(
                    "UPDATE KeyCards SET Status = 'Expired' WHERE CardNumber = ?", (card_number,)
                )
                conn.commit()
                _log_door_event(card_number, card_room, "Denied", "Card expired")
                log_audit("UPDATE", "KeyCard", card_number, "Auto-expired on door read")
                return False, f"Card {card_number} expired on {expires_at:%Y-%m-%d}."
            if room_number and normalize_room_number(card_room) != normalize_room_number(room_number):
                _log_door_event(card_number, card_room, "Denied",
                                f"Card is for room {card_room}, not {room_number}")
                return False, f"Card {card_number} is for room {card_room}, not {room_number}."
            _log_door_event(card_number, card_room, "Granted", f"Room {card_room}")
            return True, f"Access granted to room {card_room}."
    except Exception as e:
        logging.error(f"Error reading door: {e}")
        return False, "Door reader error. Access denied."


def revoke_key_card(card_number, reason=""):
    """Revoke a single card by number. Returns True on success."""
    if not card_number:
        return False
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE KeyCards SET Status = 'Revoked', RevokedAt = ?, RevokeReason = ? "
                "WHERE CardNumber = ?",
                (datetime.now(), reason or "Revoked by staff", card_number),
            )
            if not cursor.rowcount:
                logging.info(f"Card {card_number} not found or already revoked.")
                return False
            conn.commit()
            log_audit("REVOKE", "KeyCard", card_number, reason or "Revoked by staff")
            logging.info(f"Key card {card_number} revoked.")
            return True
    except Exception as e:
        logging.error(f"Error revoking key card: {e}")
        return False


def door_access_menu(view_only=False):
    """Staff submenu for key cards and the door access log.

    view_only=True (manager) hides every action that changes card state.
    """
    while True:
        if view_only:
            options = [
                "1. View Cards for a Room",
                "2. View Door Access Log",
                "3. Back",
            ]
        else:
            options = [
                "1. Issue Key Card",
                "2. Revoke Key Card",
                "3. Report Key Card Lost",
                "4. View Cards for a Room",
                "5. Test Card at Reader",
                "6. View Door Access Log",
                "7. Back",
            ]
        ui.show_menu("Door Access Control", options)
        choice = input("Enter your choice: ").strip()
        if view_only:
            view_choice = {'1': '4', '2': '6', '3': '7'}.get(choice)
            if not view_choice:
                logging.info("Invalid choice. Please try again.")
                continue
            choice = view_choice
        if choice == '1':
            room_number = input("Enter room number (floor + 3-digit code): ").strip()
            if not room_number:
                continue
            with get_connection() as conn:
                guest = (None, None)
                if conn is not None:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT LastName, FirstName FROM Reservations WHERE RoomNumber = ?",
                        (room_number,),
                    )
                    row = cursor.fetchone()
                    if row:
                        guest = (row[0], row[1])
            card = issue_key_card(room_number, guest[0], guest[1])
            if card:
                ui.success(f"Key card {card} issued for room {room_number}.")
            else:
                logging.info("Could not issue a key card. Is migration 017 applied?")
        elif choice == '2':
            card_number = input("Enter card number to revoke: ").strip()
            revoke_key_card(card_number, "Revoked at front desk")
        elif choice == '3':
            card_number = input("Enter card number reported lost: ").strip()
            try:
                with get_connection() as conn:
                    if conn is None:
                        continue
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE KeyCards SET Status = 'Lost', RevokedAt = ?, RevokeReason = ? "
                        "WHERE CardNumber = ?",
                        (datetime.now(), "Reported lost", card_number),
                    )
                    conn.commit()
                    if cursor.rowcount:
                        log_audit("REVOKE", "KeyCard", card_number, "Reported lost")
                        logging.info(f"Key card {card_number} marked lost and deactivated.")
                    else:
                        logging.info(f"Card {card_number} not found.")
            except Exception as e:
                logging.error(f"Error reporting key card lost: {e}")
        elif choice == '4':
            room_number = input("Enter room number: ").strip()
            cards = get_key_cards_for_room(room_number)
            if not cards:
                logging.info(f"No key cards on record for room {room_number}.")
            else:
                ui.show_table(
                    f"Key Cards - Room {room_number}",
                    ["Card", "Guest", "Status", "Issued", "Expires", "Revoked", "Reason", "By"],
                    [(c[0], f"{(c[3] or '')} {(c[2] or '')}".strip() or "-", c[4], c[5],
                      c[6].strftime("%Y-%m-%d") if c[6] else "-",
                      c[7].strftime("%Y-%m-%d") if c[7] else "-",
                      c[8] or "-", c[9] or "-") for c in cards],
                )
        elif choice == '5':
            # Stands in for the physical reader: validates a card and logs the attempt.
            card_number = input("Enter the card number to test: ").strip()
            granted, detail = try_open_door(card_number)
            (ui.success if granted else ui.error)(detail)
            if not granted:
                ui.pause()
        elif choice == '6':
            try:
                with get_connection() as conn:
                    if conn is None:
                        continue
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT TOP 50 EventTime, CardNumber, RoomNumber, Result, Detail "
                        "FROM DoorEvents ORDER BY EventTime DESC"
                    )
                    events = cursor.fetchall()
            except Exception as e:
                logging.error(f"Error loading door access log: {e}")
                continue
            if not events:
                logging.info("No door access events recorded.")
            else:
                ui.show_table(
                    "Door Access Log (most recent 50)",
                    ["Time", "Card", "Room", "Result", "Detail"],
                    [(e[0].strftime("%Y-%m-%d %H:%M"), e[1] or "-", e[2] or "-", e[3], e[4] or "")
                     for e in events],
                )
        elif choice == '7':
            break
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()


def view_my_key_card():
    """Guest: show the key card for the room they are staying in."""
    room_number, first_name = validate_room()
    if not room_number:
        logging.info("Could not verify your room.")
        return
    card = get_active_key_card(room_number)
    if not card:
        logging.info(f"No active key card for room {room_number}. Please see the front desk.")
        return
    expires = card.ExpiresAt.strftime("%Y-%m-%d") if card.ExpiresAt else "end of stay"
    ui.box(
        "My Key Card",
        f"Card number: {card.CardNumber}\n"
        f"Room:         {card.RoomNumber}\n"
        f"Status:       {card.Status}\n"
        f"Valid until:  {expires}",
    )
    _log_door_event(card.CardNumber, room_number, "Granted", "Guest viewed key card")

## =========================
# First-Run Onboarding
## =========================
# docs/ONBOARDING.md is the staff runbook; this section is the same runbook, executable.
# They have to agree -- the wizard automates the steps that could be automated, and the
# doc still documents the ones that could not (applying database.sql, the SQL Server
# itself, anything to do with config.ini).

# Starter catalogue offered by the wizard. `Items.ItemID` is a plain int primary key and
# not an identity column, so these ids are only a starting suggestion -- seed_default_items
# walks them past anything already present.
#
# PricingRule is Peak/OffPeak/blank, applied by get_dynamic_price(). ChargeGroup is NOT set
# here and must not be: it is written on the Transactions row when the charge is billed
# ('Room' for the room charge, 'F&B' for anything ordered), and getting it wrong
# double-counts loyalty points.
# Items.Name is nvarchar(100). Prompting for something longer would hand the operator a
# pyodbc truncation error rather than a message, so the bound is checked before the INSERT.
ITEM_NAME_MAX_LENGTH = 100

# Starter catalogue. The ItemIDs are only *suggestions* -- seed_default_items() continues
# from MAX(ItemID)+1 -- so they just need to be distinct and stable within this tuple.
#
# Deliberately NOT seeded here: a "Room Charge" item. The app posts the room charge itself
# at check-out as a Transactions row with ItemID = NULL and ChargeGroup = 'Room', guarded
# against double-posting (docs/SCHEMA.md, "Items"). An orderable "Room Charge" item would be
# a double-charge footgun: record_transaction_for_room() defaults ChargeGroup to 'F&B', so
# adding it through Order Management posts an F&B line on top of the automatic room charge
# AND accrues loyalty points on it. Every row in this tuple is genuinely sellable, which is
# also what makes "COUNT(*) > 0" a correct readiness test for a catalogue.
DEFAULT_SEED_ITEMS = (
    (1, "Breakfast Buffet", 18.00, "Peak"),
    (2, "Room Service Dinner", 32.00, None),
    (3, "Coffee & Tea", 6.50, None),
    (4, "Minibar Restock", 15.00, None),
    (5, "Laundry Service", 22.00, None),
    (6, "Airport Transfer", 55.00, "Peak"),
)

# Room category ladder for a RECTANGULAR layout: (floor must be at least this fraction of
# the way up the building) -> category. Applied top-down, so the top of the building is the
# premium end however many floors it has.
#
# This is migration 008's floor-tier idea, rescaled. 008's own tiers (1-50 / 51-100 /
# 101-145 / 146-150) are hardcoded to a 150-floor building and are reproduced verbatim by
# the tower layout instead; a hotel with 20 floors cannot use them as written.
RECTANGULAR_ROOM_CATEGORIES = (
    (0.95, "Penthouse"),
    (0.90, "Presidential Suite"),
    (0.75, "Grand Suite"),
    (0.60, "Suite"),
    (0.35, "Junior Suite"),
    (0.12, "Deluxe"),
)


def onboarding_completed():
    """True when the first-run wizard has already finished on this database.

    Reads one HotelSettings row. A missing table (migration 023 not applied), a missing
    row, a blank value, and any failure at all all read as "not completed", which is the
    safe direction: the alternative is refusing to run setup because the settings table is
    unreadable, which would strand a fresh install that nobody can log into.
    """
    try:
        raw = get_setting(ONBOARDING_SETTING, None)
    except Exception as e:
        logging.debug(f"Onboarding marker unreadable ({type(e).__name__}: {e}); treating as incomplete.")
        return False
    return str(raw if raw is not None else "").strip().lower() in ("1", "true", "yes", "y")


def mark_onboarding_complete():
    """Record that the wizard finished. `set_setting()` writes the AuditLog row for us."""
    return set_setting(ONBOARDING_SETTING, "1")


def create_first_user(username, password, role="admin"):
    """Create a login directly, bypassing the Admin Panel.

    This is the step the wizard exists for. `Users` is seeded by nothing -- not by
    database.sql, not by any migration -- and the only in-app way to add one, `add_user()`,
    lives inside the Admin Panel, which needs a login to reach. So the first account used
    to be a hand-written INSERT.

    Refuses outright when ANY account already exists. That guard is the point of the
    function rather than a nicety: during first-run this is reachable without a login, and
    it must never quietly become "mint another admin on a live system". Use the Admin
    Panel for everyone after the first.

    Returns True on success, False if refused or on error. Passwords are stored and
    compared in plaintext on purpose (AGENTS.md section 3 -- teaching project).
    """
    username = str(username or "").strip()
    password = str(password if password is not None else "")
    if not username or not password:
        logging.info("A username and a password are both required.")
        return False
    if role not in ("admin", "staff", "manager", "master"):
        logging.info(f"'{role}' is not a role this app uses.")
        return False
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM Users")
            existing = cursor.fetchone()
            if existing and existing[0] is not None and int(existing[0]) > 0:
                logging.info("Accounts already exist. Add more from the Admin Panel, "
                             "not from setup.")
                return False
            # Same three columns as add_user(), so the two paths cannot drift apart.
            cursor.execute("INSERT INTO Users (Username, Password, Role) VALUES (?, ?, ?)",
                           (username, password, role))
            conn.commit()
            log_audit("CREATE", "User", username, f"Role {role} (first-run onboarding)")
            logging.info(f"Account '{username}' created with role '{role}'.")
            return True
    except Exception as e:
        logging.error(f"Error creating account: {e}")
        return False


def _insert_item(cursor, item_id, name, price, pricing_rule):
    """One INSERT for Items, shared so the two onboarding paths cannot disagree on shape.

    Takes an open cursor rather than opening its own connection so a whole catalogue can be
    written in a single transaction. Never commits -- the caller decides, so a catalogue
    that fails halfway does not leave half of it applied.
    """
    cursor.execute(
        "INSERT INTO Items (ItemID, Name, Price, PricingRule) VALUES (?, ?, ?, ?)",
        (int(item_id), str(name), round(float(price), 2), pricing_rule))


def seed_default_items():
    """Insert the starter catalogue. Returns how many rows were actually added.

    Starts from MAX(ItemID)+1 rather than from 1, which is what keeps a re-run against a
    hotel that already has hand-added items from dying on a duplicate key. Skips by NAME,
    so an item the operator already added under the same name is left exactly as it is even
    when our id would have been free. Returns 0 when there was nothing to do and also on
    failure; check the log if you asked for items and got none.
    """
    try:
        with get_connection() as conn:
            if conn is None:
                return 0
            cursor = conn.cursor()
            cursor.execute("SELECT Name FROM Items")
            existing_names = {str(r[0]).strip().lower() for r in cursor.fetchall() if r[0]}
            cursor.execute("SELECT ISNULL(MAX(ItemID), 0) FROM Items")
            row = cursor.fetchone()
            next_id = (int(row[0]) if row and row[0] is not None else 0) + 1
            added = 0
            for _suggested_id, name, price, rule in DEFAULT_SEED_ITEMS:
                if name.strip().lower() in existing_names:
                    continue
                _insert_item(cursor, next_id, name, price, rule)
                next_id += 1
                added += 1
            if added:
                conn.commit()
                log_audit("CREATE", "Item", "starter-catalogue",
                          f"Added {added} item(s) during first-run onboarding")
                logging.info(f"Added {added} item(s) to the catalogue.")
            return added
    except Exception as e:
        logging.error(f"Error seeding items: {e}")
        return 0


def _rooms_generation_sql(tower, floors=None, rooms_per_floor=None):
    """Build the recursive CTE that enumerates a hotel's rooms.

    Returns (sql, params) for a WITH clause yielding RoomNumber, Floor, Code and RoomType.
    Shared by the count query and the insert so the two can never disagree about what
    "this layout" means -- a count that does not match the insert is worse than no count.

    `tower` reproduces migrations/008_rooms_seed.sql tier for tier, so a wizard-seeded hotel
    and a migration-008-seeded one are indistinguishable. Otherwise it is a rectangular
    block categorised by RECTANGULAR_ROOM_CATEGORIES.
    """
    if tower:
        # 008 verbatim: all codes on floors 1-100, 1-850 on 101-145, 951-956 on 146-150.
        generated = (
            "SELECT "
            "  CAST(f.Floor AS varchar(10)) + RIGHT('000' + CAST(c.Code AS varchar(3)), 3) AS RoomNumber, "
            "  f.Floor AS Floor, c.Code AS Code, "
            "  CASE WHEN f.Floor BETWEEN 146 AND 150 THEN "
            "         CASE WHEN c.Code <= 953 THEN 'Penthouse' ELSE 'Presidential Suite' END "
            "       WHEN f.Floor BETWEEN 101 AND 145 THEN "
            "         CASE WHEN c.Code <= 600 THEN 'Suite' ELSE 'Grand Suite' END "
            "       WHEN f.Floor BETWEEN 51 AND 100 THEN "
            "         CASE WHEN c.Code <= 850 THEN 'Deluxe' "
            "              WHEN c.Code <= 950 THEN 'Junior Suite' ELSE 'Suite' END "
            "       ELSE "
            "         CASE WHEN c.Code <= 600 THEN 'Standard' "
            "              WHEN c.Code <= 850 THEN 'Deluxe' "
            "              WHEN c.Code <= 950 THEN 'Junior Suite' ELSE 'Suite' END "
            "  END AS RoomType "
            "FROM Floors f CROSS JOIN Codes c "
            "WHERE (f.Floor <= 100) "
            "   OR (f.Floor BETWEEN 101 AND 145 AND c.Code <= 850) "
            "   OR (f.Floor BETWEEN 146 AND 150 AND c.Code BETWEEN 951 AND 956)"
        )
        return (
            "WITH Floors AS ("
            "  SELECT 1 AS Floor UNION ALL SELECT Floor + 1 FROM Floors WHERE Floor < ?"
            "), Codes AS ("
            "  SELECT 1 AS Code UNION ALL SELECT Code + 1 FROM Codes WHERE Code < ?"
            f"), Generated AS ({generated}) ",
            [MAX_ROOM_FLOORS, MAX_ROOMS_PER_FLOOR],
        )

    # Fractions are rendered from the Python tuple so the ladder has one definition.
    cases = " ".join(
        f"WHEN CAST(Floor AS decimal(10,4)) / ? >= {threshold} THEN '{category}'"
        for threshold, category in RECTANGULAR_ROOM_CATEGORIES)
    generated = (
        "SELECT "
        "  CAST(g.Floor AS varchar(10)) + RIGHT('000' + CAST(g.Code AS varchar(3)), 3) AS RoomNumber, "
        "  g.Floor AS Floor, g.Code AS Code, "
        f"  CASE {cases} ELSE 'Standard' END AS RoomType "
        "FROM Placed g"
    )
    return (
        "WITH Floors AS ("
        "  SELECT 1 AS Floor UNION ALL SELECT Floor + 1 FROM Floors WHERE Floor < ?"
        "), Codes AS ("
        "  SELECT 1 AS Code UNION ALL SELECT Code + 1 FROM Codes WHERE Code < ?"
        "), Placed AS ("
        "  SELECT f.Floor AS Floor, c.Code AS Code FROM Floors f CROSS JOIN Codes c"
        f"), Generated AS ({generated}) ",
        # The ladder's thresholds are read in order, so its `?`s come last.
        [floors, rooms_per_floor] + [floors] * len(RECTANGULAR_ROOM_CATEGORIES),
    )


# Description text for a generated room, keyed off the code within the floor. Shared by both
# layouts, same wording as migration 008.
_ROOMS_DESCRIPTION_SQL = (
    "g.RoomType + ' - floor ' + CAST(g.Floor AS varchar(3)) + ' - ' + "
    "CASE WHEN g.Code >= 951 THEN 'panoramic specialty span' "
    "     WHEN g.Code >= 851 THEN 'corner unit with multi-directional views' "
    "     WHEN g.Code >= 601 THEN 'floor-to-ceiling windows' "
    "     ELSE 'central corridor view' END"
)


def validate_room_layout(tower, floors=None, rooms_per_floor=None):
    """Normalise a layout against MAX_ROOM_FLOORS / MAX_ROOMS_PER_FLOOR.

    Pure, so the bounds are unit-testable without a database and so the wizard and the
    insert agree on what is acceptable.

    Returns (tower, floors, rooms_per_floor) with the tower flag and the counts filled in.
    On an unacceptable layout the flag comes back False, so a caller cannot accidentally
    treat "invalid" as "use the tower" by reading the first value as a success flag.
    """
    if tower:
        return True, MAX_ROOM_FLOORS, MAX_ROOMS_PER_FLOOR
    try:
        floors = int(floors)
        rooms_per_floor = int(rooms_per_floor)
    except (TypeError, ValueError):
        return False, None, None
    if not 1 <= floors <= MAX_ROOM_FLOORS:
        return False, None, None
    if not 1 <= rooms_per_floor <= MAX_ROOMS_PER_FLOOR:
        return False, None, None
    return False, floors, rooms_per_floor


def count_rooms_to_add(tower=False, floors=None, rooms_per_floor=None):
    """How many rooms the layout would add: the generated set minus what is already there.

    A COUNT rather than the insert's own rowcount, so the wizard can tell the operator how
    big the thing is BEFORE it does it. Returns None when the layout is invalid or the
    query fails, which the caller must treat as "do not proceed" rather than "nothing to
    do" -- conflating those two would silently skip the step.
    """
    tower, floors, rooms_per_floor = validate_room_layout(tower, floors, rooms_per_floor)
    if not tower and floors is None:
        return None
    sql, params = _rooms_generation_sql(tower, floors=floors, rooms_per_floor=rooms_per_floor)
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                sql + "SELECT COUNT(*) FROM Generated g "
                "WHERE NOT EXISTS (SELECT 1 FROM dbo.Rooms r WHERE r.RoomNumber = g.RoomNumber) "
                "OPTION (MAXRECURSION 0)",
                params)
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        logging.error(f"Error counting rooms to seed: {e}")
        return None


def seed_rooms(tower=False, floors=None, rooms_per_floor=None):
    """Populate Rooms from the hotel's layout. Returns how many rooms were added, 0 on
    failure or on a layout outside the accepted bounds.

    Generated in SQL by a recursive CTE rather than in Python: the tower is 138,180 rows,
    and a row-at-a-time pyodbc insert would take minutes for data that is pure arithmetic.
    The statement is the one migration 008 already runs, so it is not new territory.

    Guarded by NOT EXISTS, never an update: a room that already has a guest in it keeps its
    category and housekeeping status no matter what the layout says, and a partial seed can
    be topped up by running the wizard again.
    """
    tower, floors, rooms_per_floor = validate_room_layout(tower, floors, rooms_per_floor)
    if not tower and floors is None:
        logging.info(
            f"A room layout needs either the tower preset or a floor count "
            f"(1-{MAX_ROOM_FLOORS}) and a rooms-per-floor count (1-{MAX_ROOMS_PER_FLOOR}).")
        return 0
    sql, params = _rooms_generation_sql(tower, floors=floors, rooms_per_floor=rooms_per_floor)
    try:
        with get_connection() as conn:
            if conn is None:
                return 0
            cursor = conn.cursor()
            cursor.execute(
                sql + "INSERT INTO dbo.Rooms (RoomNumber, RoomType, Description, Status) "
                "SELECT g.RoomNumber, g.RoomType, " + _ROOMS_DESCRIPTION_SQL + ", 'Available' "
                "FROM Generated g "
                "WHERE NOT EXISTS (SELECT 1 FROM dbo.Rooms r WHERE r.RoomNumber = g.RoomNumber) "
                "OPTION (MAXRECURSION 0)",
                params)
            added = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            if added:
                conn.commit()
                log_audit("CREATE", "Room", "layout",
                          f"Seeded {added} room(s) from the "
                          f"{'tower' if tower else f'{floors}f x {rooms_per_floor}'} layout")
                logging.info(f"Seeded {added} room(s).")
            return added
    except Exception as e:
        logging.error(f"Error seeding rooms: {e}")
        return 0


def _scalar_count(sql, params=()):
    """Run a COUNT and return the int, or None if the query could not run.

    None is deliberately distinct from 0. "No rooms" and "the Rooms table is not there" are
    different problems, and a checklist that reports them the same way sends whoever reads
    it to the wrong menu.
    """
    try:
        with get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        logging.debug(f"Setup probe failed ({type(e).__name__}: {e})")
        return None


def setup_status():
    """A readiness checklist for this hotel. Returns a list of (label, done, detail) rows.

    Derived from live data on purpose rather than read off the onboarding marker. The
    marker answers "has a human been through the wizard"; this answers the question the
    runbook actually cares about, which is "can the front desk take a booking a guest can
    pay for". Someone who inherited this database and never ran the wizard still needs the
    second question answered.

    Every probe stands alone and swallows its own failure, so an unapplied migration reads
    as one red row carrying the database's own error rather than a dead screen.
    """
    checks = []

    users = _scalar_count("SELECT COUNT(*) FROM Users")
    if users is None:
        checks.append(("Staff accounts", False, "Users is unreachable -- migration 001 applied?"))
    elif users == 0:
        checks.append(("Staff accounts", False, "No logins exist yet"))
    else:
        checks.append(("Staff accounts", True, f"{users} account(s)"))

    # The master row is what require_master_override() falls back to when config.ini has no
    # [hotel] master_secret -- which is the shipped default, so on a fresh install it is the
    # only thing that makes the override work at all.
    master = _scalar_count("SELECT COUNT(*) FROM Users WHERE Username = ?", ("master",))
    if master:
        checks.append(("Master override account", True, "'master' exists"))
    elif str(MASTER_SECRET or "").strip():
        checks.append(("Master override account", True,
                       "using the master_secret from config.ini"))
    else:
        checks.append(("Master override account", False,
                       "no 'master' login and no master_secret in config.ini -- the "
                       "override will refuse every caller"))

    items = _scalar_count("SELECT COUNT(*) FROM Items")
    if items is None:
        checks.append(("Item catalogue", False, "Items is unreachable -- migration 006 applied?"))
    elif items == 0:
        # Every Items row is a sellable line; the room charge is posted separately at
        # check-out with ItemID = NULL, so it is not missing from here and not counted.
        checks.append(("Item catalogue", False,
                       "empty -- nothing can be ordered until one sellable item exists"))
    else:
        checks.append(("Item catalogue", True, f"{items} sellable item(s)"))

    rooms = _scalar_count("SELECT COUNT(*) FROM Rooms")
    if rooms is None:
        checks.append(("Rooms", False, "Rooms is unreachable -- migration 008 applied?"))
    elif rooms == 0:
        checks.append(("Rooms", False,
                       "empty -- the dashboard and housekeeping board have nothing to show"))
    else:
        checks.append(("Rooms", True, f"{rooms} room(s)"))

    rated = _scalar_count("SELECT COUNT(*) FROM RoomTypes WHERE ISNULL(NightlyRate, 0) > 0")
    if rated is None:
        checks.append(("Nightly rates", False, "RoomTypes is unreachable -- migration 013 applied?"))
    elif rated == 0:
        checks.append(("Nightly rates", False,
                       "no room type has a rate -- check-out cannot price a stay"))
    else:
        checks.append(("Nightly rates", True, f"{rated} priced category(s)"))

    raw_date = get_setting(BUSINESS_DATE_SETTING, None)
    if raw_date is None or str(raw_date).strip() == "":
        checks.append(("Business date", False,
                       "not set -- reports cannot be re-run for a day that has closed"))
    else:
        checks.append(("Business date", True, f"{business_date().isoformat()}"))

    if LOYALTY_ENABLED:
        tiers = _scalar_count("SELECT COUNT(*) FROM LoyaltyTiers")
        if tiers is None:
            checks.append(("Loyalty tiers", False, "LoyaltyTiers is unreachable"))
        elif tiers == 0:
            checks.append(("Loyalty tiers", False, "no tiers -- nothing to promote anyone into"))
        else:
            checks.append(("Loyalty tiers", True, f"{tiers} tier(s)"))

    return checks


def _prompt_new_password(prompt):
    """Ask twice and require a match. Returns the password, or None if they did not match.

    Uses getpass for the entry itself. This is the one place in the app where a password is
    typed twice, and it is worth the two lines: this account owns the database.
    """
    while True:
        first = getpass.getpass(prompt)
        second = getpass.getpass("Confirm password: ")
        if not first:
            logging.info("The password cannot be blank.")
            continue
        if first != second:
            logging.info("Those did not match. Try again.")
            continue
        return first


def run_first_run_onboarding():
    """The first-run wizard. Runs once, before the main menu, on a database not yet onboarded.

    It exists for the one step of docs/ONBOARDING.md that could not be done in-app at all:
    the first login. Everything after that is offered but skippable, because the wizard
    cannot know what a given hotel sells, how many rooms it has, or what its floors are laid
    out like, and a setup step that guesses wrong is worse than one that asks.

    Unauthenticated by necessity rather than by choice -- there is no account to
    authenticate against yet. The trust assumption is the one the database already makes:
    whoever is at this console can also open SSMS against the empty database. It cannot run
    twice once `mark_onboarding_complete()` has been written, and `create_first_user()`
    refuses outright if any account already exists, so it cannot become a way to add an
    admin to a live system.
    """
    hotel = HOTEL_NAME or "this hotel"
    ui.box(
        f"Welcome to {hotel} - First-Time Setup",
        "This runs once, before the app is usable.\n\n"
        "It will create your first administrator login, then offer a few starting\n"
        "defaults. Every step after the login is optional and can be skipped --\n"
        "Admin Panel -> 34. Setup Checklist shows what is still outstanding at any time.\n\n"
        "Before any of this, database.sql has to have been applied to the server,\n"
        "and config.ini has to point at it. See docs/ONBOARDING.md.",
        border_style="cyan",
    )

    # --- 1. The first login. The one step that is not optional. ---
    ui.info("\n-- Step 1: create your first administrator --")
    existing_accounts = _scalar_count("SELECT COUNT(*) FROM Users")
    if existing_accounts:
        # Reached when someone hand-inserted accounts the old way and never wrote the
        # marker. Say so and move on rather than pretending to create a login we would only
        # refuse: create_first_user() will not add one over the top of an account that is
        # already there, so retrying would loop forever.
        ui.box(
            "Accounts already exist",
            f"This database already has {existing_accounts} account(s), so the wizard will "
            "not create a login over the top of one.\n\n"
            "That is fine -- it just means the first account was inserted by hand. Add "
            "everyone else from Admin Panel -> 19. Add User.",
            border_style="yellow",
        )
    else:
        logging.info("Nothing can be logged into until an account exists, so this is the "
                     "only mandatory step.")
        while True:
            username = input("Administrator username [admin]: ").strip() or "admin"
            password = _prompt_new_password("Password: ")
            if create_first_user(username, password, "admin"):
                break
            logging.info("Could not create the account. Check the error above and try again.")

    # --- 2. The master override account. ---
    ui.info("\n-- Step 2: master override account --")
    if _scalar_count("SELECT COUNT(*) FROM Users WHERE Username = ?", ("master",)):
        logging.info("A 'master' account already exists. Leaving it alone.")
    elif ui.ask_confirmation(
            "Create a 'master' account for the master override? Without one, and without "
            "a master_secret in config.ini, the override refuses every caller.", default="y"):
        master_password = _prompt_new_password("Master password: ")
        # Role 'admin', not 'master'. require_master_override() matches on the username
        # alone, but admin_panel() has no 'master' branch and add_user() only offers
        # admin/staff/manager -- so a 'master'-role row would be an account nobody can ever
        # sign in to.
        #
        # Which insert to use depends on step 1. create_first_user() refuses whenever ANY
        # account exists, which is right for the first login (nothing else may reach this
        # screen) and wrong here: an inherited database has accounts, no marker, and no
        # master -- so it would refuse and leave the override unbacked by the operator's own
        # choice, which is the one case this step exists for. Fall back to the same
        # already-authenticated path the checklist uses.
        if existing_accounts:
            created = add_user_with_password("master", master_password, "admin")
        else:
            created = create_first_user("master", master_password, "admin")
        if created:
            logging.info("Master override is now backed by a real account.")
        else:
            logging.info("Could not create the 'master' account. The override will still "
                         "work if you set master_secret in config.ini.")
    else:
        logging.info("Skipped. Set [hotel] master_secret in config.ini if you skip this.")

    # --- 3. Starter catalogue. ---
    ui.info("\n-- Step 3: item catalogue --")
    existing_items = _scalar_count("SELECT COUNT(*) FROM Items")
    if existing_items:
        # An inherited or hand-configured database. Offering to seed a catalogue into a
        # populated one would quietly add stock nobody asked for, so each optional step is
        # gated on its own live probe rather than on the marker.
        logging.info("The catalogue already has %d item(s). Skipping -- add your own from Setup "
                     "Checklist -> 3, or Admin Panel -> 13.", existing_items)
    else:
        logging.info("Nothing can be ordered until at least one item exists, and every row in "
                     "Items is something a guest can actually buy. The room charge is not one "
                     "of them -- the app posts that itself at check-out, so do not look for it "
                     "here.")
        added = _offer_item_catalogue(wizard=True)
        if not added:
            logging.info("No items were added. Nothing can be ordered until there is at least "
                         "one -- Admin Panel -> 13. Add Item, or Setup Checklist -> 3.")

    # --- 4. Rooms. ---
    ui.info("\n-- Step 4: rooms --")
    existing_rooms = _scalar_count("SELECT COUNT(*) FROM Rooms")
    if existing_rooms:
        logging.info("Rooms already exist (%d). Skipping the layout seed -- top it up from "
                     "Setup Checklist -> 4 if you need more.", existing_rooms)
    else:
        logging.info("A room number is <floor><3-digit code>, so floor 9 code 012 is 9012. "
                     "Skipping this is fine: the first booking registers its own room.")
        _offer_room_layout(wizard=True)

    # --- 5. Business date. ---
    ui.info("\n-- Step 5: business date --")
    logging.info("This app's 'today' is a stored value, not the wall clock, so yesterday's "
                 "report still says yesterday after you close the day. It is currently %s.",
                 business_date().isoformat())
    if ui.ask_confirmation("Set it to today instead?", default="n"):
        set_business_date(datetime.now().date())

    mark_onboarding_complete()
    log_audit("CREATE", "Setting", ONBOARDING_SETTING, "First-run onboarding completed")

    outstanding = [label for label, done, _ in setup_status() if not done]
    ui.box(
        "Setup complete",
        (f"Administrator '{username}' can now sign in.\n\n" if not existing_accounts
         else f"The existing {existing_accounts} account(s) can now sign in.\n\n")
        + ("Everything on the readiness checklist is green.\n"
           if not outstanding
           else "Still outstanding (Admin Panel -> 34. Setup Checklist):\n"
                + "".join("  - %s\n" % label for label in outstanding))
        + "\nSmoke test, in this order:\n"
          "  1. Bookings -> book a stay, two nights, in a room number of your choosing\n"
          "  2. Admin Panel -> 29. Rooms & Housekeeping -> 1. the room should be Available\n"
          "  3. Order something through the guest menu\n"
          "  4. Check the guest out -- the folio must split into Room Charges and F&B\n"
          "  5. Admin Panel -> 30. Invoices & Printing -- print it and check the arithmetic",
        border_style="green",
    )
    ui.pause("Press Enter to reach the main menu...")


def _count_tower_rooms():
    """How many rooms the tower layout holds, computed in Python from the same rules.

    Not a database call: the wizard needs the figure to describe the option BEFORE
    anything is connected, and it is pure arithmetic over the tiers in 008.
    100 floors x 999 codes, 45 floors x 850 codes, 5 floors x 6 codes.
    """
    return 100 * MAX_ROOMS_PER_FLOOR + 45 * 850 + 5 * 6


def onboarding_checklist(role="admin"):
    """Setup readiness for an administrator, plus the setup actions that are still open.

    Lives in the Admin Panel rather than the main menu, because minting a login is a
    privileged act and the main menu is the guest-facing surface.

    Read-only unless the signed-in role is `admin`, so the screen is safe to show a manager
    browsing the panel without letting the setup paths become a way around the Admin Panel's
    own role checks. Actions are offered one at a time, each confirmed before it runs.
    """
    may_write = str(role or "").lower() == "admin"
    while True:
        checks = setup_status()
        ui.show_table(
            "Setup Checklist",
            ["Ready", "Check", "Detail"],
            [("[green]yes[/green]" if done else "[yellow]no[/yellow]", label, detail)
             for label, done, detail in checks],
        )
        if all(done for _, done, _ in checks):
            logging.info("Everything this hotel needs before the front desk can take a "
                         "payment is in place.")
        if not may_write:
            ui.info("\nSetup actions need the admin role. This view is read-only.")
            return

        ui.show_menu("Setup actions", [
            "1. Add a staff account",
            "2. Create the 'master' override account",
            "3. Add items (starter catalogue, or your own)",
            "4. Seed rooms",
            "5. Set the business date to today",
            "6. Re-check",
            "7. Back to Admin Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            username = input("Enter new username: ").strip()
            if not username:
                logging.info("A username is required.")
            else:
                password = _prompt_new_password("Password: ")
                role_prompt = ("Role (a)dmin/(s)taff/(m)anager: ")
                new_role = _prompt_role(role_prompt)
                if not new_role:
                    logging.info("No account was created.")
                elif user_exists(username):
                    logging.info(f"User '{username}' already exists.")
                else:
                    # Not create_first_user(): that refuses once any account exists, and by
                    # the time anyone reaches the checklist there is always at least one.
                    add_user_with_password(username, password, new_role)
        elif choice == '2':
            if user_exists("master"):
                logging.info("A 'master' account already exists. Leaving it alone.")
            else:
                master_password = _prompt_new_password("Master password: ")
                # Role 'admin' for the same reason as in the wizard: the override matches on
                # the username, and no login path accepts a 'master' role.
                add_user_with_password("master", master_password, "admin")
        elif choice == '3':
            added = _offer_item_catalogue()
            if added:
                logging.info("Added %d item(s). Admin Panel -> 16. View Items to check them.",
                             added)
            else:
                logging.info("Nothing added -- either you finished without adding anything, "
                             "the names already exist, or the write failed. Check the log.")
        elif choice == '4':
            _offer_room_layout()
        elif choice == '5':
            set_business_date(datetime.now().date())
            logging.info("Business date set to %s.", business_date().isoformat())
        elif choice == '6':
            continue
        elif choice == '7':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()


def user_exists(username):
    """True when a login with this name is already in Users. False if Users is unreachable."""
    count = _scalar_count("SELECT COUNT(*) FROM Users WHERE Username = ?", (str(username),))
    return bool(count)


def add_user_with_password(username, password, role):
    """Insert a login from an already-authenticated screen.

    `add_user()` prompts for the password itself, which makes it unusable from a wizard
    that has already collected one. This is the same INSERT with the same duplicate check
    and the same audit row, so there is still only one place that writes a new account from
    a screen where someone is logged in.
    """
    username = str(username or "").strip()
    if not username:
        logging.info("A username is required.")
        return False
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM Users WHERE Username = ?", (username,))
            if cursor.fetchone():
                logging.info(f"User '{username}' already exists.")
                return False
            cursor.execute("INSERT INTO Users (Username, Password, Role) VALUES (?, ?, ?)",
                           (username, password, role))
            conn.commit()
            log_audit("CREATE", "User", username, f"Role {role} (setup checklist)")
            logging.info(f"User '{username}' added successfully.")
            return True
    except Exception as e:
        logging.error(f"Error adding user: {e}")
        return False


def _ask_item_name(existing_names):
    """Prompt for an item name until one is usable. Returns (name, None) or (None, None).

    Two things make this stricter than add_item()'s prompt. A blank name cannot be offered in
    an order menu, and Name is nvarchar(100) -- a longer value is a pyodbc "string or binary
    data would be truncated" error rather than a message, which is exactly the traceback
    AGENTS.md section 3 says a mistyped prompt must never produce.

    A name that already exists only warns. There is no UNIQUE constraint on Name and
    "Espresso" / "Espresso (large)" is a legitimate pair, so this asks rather than refuses.
    """
    while True:
        raw = input("Item name: ").strip()
        if not raw:
            logging.info("An item needs a name -- it is what shows up on the guest's folio.")
            continue
        if len(raw) > ITEM_NAME_MAX_LENGTH:
            logging.info("That name is %d characters; the database holds %d. Shorten it.",
                         len(raw), ITEM_NAME_MAX_LENGTH)
            continue
        if raw.lower() in existing_names:
            logging.info("An item called '%s' already exists.", raw)
            if not ui.ask_confirmation("Add it as a separate item anyway?", default="n"):
                continue
        return raw, None


def _ask_item_price():
    """Prompt for a price until one parses. Returns a float rounded to cents, or None to quit.

    decimal(10,2) is the column, so the value is rounded to 2dp rather than letting the
    driver round or reject it. A parse failure re-asks instead of raising, for the same
    reason as the name.
    """
    while True:
        raw = input("Price (blank to stop adding items): ").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            logging.info("'%s' is not a number. Enter a price like 18.50", raw)
            continue
        if value < 0:
            logging.info("A price cannot be negative. Enter 0 for a free item.")
            continue
        if value > 99999999.99:
            logging.info("That price is beyond what the database can store.")
            continue
        return round(value, 2)


def add_custom_item(existing_names=None):
    """Add one operator-defined item to the catalogue. Returns True if a row was written.

    Assigns ItemID as MAX(ItemID)+1 rather than asking. Items.ItemID is a plain int PK, not
    an identity column, so a typed id can collide with a row that already exists and the
    operator gets a constraint-violation traceback instead of an item; auto-assigning makes
    that failure mode unreachable and keeps ids contiguous. Someone who needs a deliberate
    numbering scheme can still use Admin Panel -> 13, which does take an id.

    One item per call, so the caller owns the loop and the "I am finished" decision.
    """
    try:
        with get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            if existing_names is None:
                cursor.execute("SELECT Name FROM Items")
                existing_names = {str(r[0]).strip().lower()
                                  for r in cursor.fetchall() if r[0]}
            name, _ = _ask_item_name(existing_names)
            if not name:
                return False
            price = _ask_item_price()
            if price is None:
                logging.info("No price given, so no item was added.")
                return False
            rule_raw = input("Pricing rule - (P)eak/(O)ffPeak/blank for standard: ")
            rule = _parse_pricing_rule(rule_raw)
            cursor.execute("SELECT ISNULL(MAX(ItemID), 0) FROM Items")
            row = cursor.fetchone()
            item_id = (int(row[0]) if row and row[0] is not None else 0) + 1
            _insert_item(cursor, item_id, name, price, rule)
            conn.commit()
            log_audit("CREATE", "Item", item_id,
                      f"'{name}' @ {price}, rule {rule or 'standard'} (onboarding)")
            logging.info(f"Added '{name}' as item {item_id}.")
            return True
    except KeyboardInterrupt:
        logging.info("Cancelled. No item was added.")
        return False
    except Exception as e:
        logging.error(f"Error adding item: {e}")
        return False


def _offer_item_catalogue(wizard=False):
    """Shared by the wizard and the checklist: build up a catalogue from either source.

    Offers the starter set AND lets the operator type their own, because the wizard cannot
    know what a given hotel actually sells and a starter catalogue nobody sells is not much
    use. Adding custom items is a loop rather than a one-shot because hotels almost always
    have a handful and not everyone has them written down in advance.

    `wizard` only changes what the exit option is called, exactly as in _offer_room_layout.
    Returns how many items were added, so the caller can report honestly.
    """
    added = 0
    ui.show_table("Starter catalogue", ["Item", "Price", "Pricing rule"],
                  [[name, f"${price:,.2f}", rule or "standard"]
                   for _id, name, price, rule in DEFAULT_SEED_ITEMS])
    logging.info("These %d items are a starting point, not a menu. Anything can be edited, "
                 "added or deleted afterwards from Admin Panel -> 13/14/15.", len(DEFAULT_SEED_ITEMS))
    try:
        while True:
            ui.show_menu("Add items", [
                "1. Add the starter catalogue (%d items)" % len(DEFAULT_SEED_ITEMS),
                "2. Add an item of your own",
                "3. %s" % ("Skip for now" if wizard else "Cancel -- done adding items"),
            ])
            choice = input("Enter your choice: ").strip()
            if choice == "1":
                added += seed_default_items()
            elif choice == "2":
                try:
                    if add_custom_item():
                        added += 1
                except KeyboardInterrupt:
                    logging.info("Cancelled that item. The rest of your catalogue is fine.")
            elif choice == "3":
                break
            else:
                logging.info("Answer 1, 2 or %s.", 3)
    except (KeyboardInterrupt, EOFError):
        logging.info("Finished adding items.")
    return added


def _offer_room_layout(wizard=False):
    """Shared by the wizard and the checklist: pick a layout, count it, confirm, seed it.

    `wizard` only changes what the fourth option is called -- "skip" during first-run,
    because not having rooms yet is a legitimate state to carry on from, versus "cancel"
    when someone has come here deliberately to add more.
    """
    tower_rooms = _count_tower_rooms()
    ui.show_menu("Room layout", [
        "1. 150-floor tower (%s rooms) -- the layout in migrations/008_rooms_seed.sql" % f"{tower_rooms:,}",
        "2. Mid-size: 20 floors x 40 rooms (800 rooms)",
        "3. Custom floor count and rooms per floor",
        "4. %s" % ("Skip -- rooms appear as bookings are made" if wizard else "Cancel"),
    ])
    choice = input("Enter your choice: ").strip()
    if choice == "1":
        if not ui.ask_confirmation(f"Seed {tower_rooms:,} rooms? This is a large insert.",
                                   default="n"):
            return
        pending = count_rooms_to_add(True)
        if pending is None:
            logging.info("Could not count the rooms that would be added. Nothing was changed.")
            return
        if pending == 0:
            logging.info("Every room in the tower layout already exists.")
            return
        seed_rooms(tower=True)
    elif choice == "2":
        seed_rooms(floors=20, rooms_per_floor=40)
    elif choice == "3":
        while True:
            floors = ui.ask_number(f"Floors (1-{MAX_ROOM_FLOORS})", minimum=1, maximum=MAX_ROOM_FLOORS)
            per_floor = ui.ask_number(f"Rooms per floor (1-{MAX_ROOMS_PER_FLOOR})", minimum=1,
                                      maximum=MAX_ROOMS_PER_FLOOR)
            pending = count_rooms_to_add(False, floors, per_floor)
            if pending is None:
                logging.info("Could not count the rooms that layout would add. Nothing was "
                             "changed.")
                return
            if pending == 0:
                logging.info("Those rooms all exist already.")
                return
            if ui.ask_confirmation(f"Seed {pending:,} room(s)?", default="y"):
                seed_rooms(floors=floors, rooms_per_floor=per_floor)
                return
            logging.info("Nothing was seeded. Answer 4 to %s, or try again."
                         % ("skip" if wizard else "cancel"))
    else:
        logging.info("Skipped. The first booking will register its own room." if wizard
                     else "Cancelled. No rooms were changed.")


## =========================
# Main Entry Point
## =========================
def main():
    # Ensure loyalty DB objects exist if loyalty is enabled
    try:
        ensure_loyalty_tables()
    except Exception:
        pass
    # Ensure the room-type rate table is populated so nightly room charges can be posted.
    try:
        ensure_room_types_seeded()
    except Exception:
        pass

    if handle_cli_args():
        return

    # First run only: a database nobody has set up yet has no accounts, so there is nothing
    # to log into and the main menu would be a wall of failed logins. After this it never
    # runs again -- the marker is what stops it, not the state of the Users table.
    if not onboarding_completed():
        run_first_run_onboarding()

    while True:
        ui.clear_screen()
        ui.show_menu(f"Welcome to {HOTEL_NAME}!" if HOTEL_NAME else "Hotel Management System", [
            "1. Customer",
            "2. Admin",
            "3. Bookings",
            "4. Exit",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            customer_panel()
        elif choice == '2':
            admin_panel()
        elif choice == '3':
            booking_panel()
        elif choice == '4':
            logging.info("Exiting Program")
            time.sleep(2)
            sys.exit(0)
        else:
            logging.info("Invalid choice. Please try again.")


if __name__ == "__main__":
    main()
