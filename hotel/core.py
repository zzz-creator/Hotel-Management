# type: ignore
"""core: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
import os
import sys
import time
import random
import string
import getpass
import configparser
import re
from datetime import datetime
from . import db
from . import ui
from . import session

__all__ = [
    'config',
    'config_path',
    'server',
    'database',
    'username',
    'password',
    'DEFAULT_TIERS',
    'DEFAULT_ROOM_TYPE_MULTIPLIERS',
    'DEFAULT_ROOM_TYPE_RATES',
    'DEFAULT_HOTEL_SETTINGS',
    'CHARGE_GROUP_ROOM',
    'CHARGE_GROUP_FNB',
    'CONNECTION_STRING',
    '_ensure_database_config',
    'require_master_override',
    'log_audit',
    'get_setting',
    'set_setting',
    '_setting_float',
    '_setting_int',
    'get_peak_factor',
    'get_offpeak_factor',
    'get_tax_rate',
    'get_hotel_name',
    'get_loyalty_enabled',
    'get_lockout_threshold',
    'get_lockout_duration',
    'get_customer_login_max_attempts',
    'get_loyalty_accrual_points_per_unit',
    'get_loyalty_redemption_points_per_currency_unit',
    'get_loyalty_points_per_night',
    'reservation_window_active',
    'business_date',
    'generate_code',
    'normalize_room_number',
    'VALIDATE_ROOM_MAX_ATTEMPTS',
    'validate_room',
    'BOOKING_REFUND_CUTOFF_DAYS_DEFAULT',
    'get_booking_refund_cutoff_days',
    '_DUPLICATE_KEY_NUMBERS',
    '_DUPLICATE_KEY_GENERIC_STATE',
    '_DUPLICATE_KEY_GENERIC_STATE_INT',
    '_is_integrity_code',
    '_is_duplicate_key_error',
    '_reservation_check_in',
    '_existing_tables',
    'stay_nights',
    'stays_overlap',
    '_prompt_role',
    'STAFF_ROLES',
    '_edit_general_settings',
    'reset_settings_to_defaults',
    'upsert_customer_profile',
    'get_amenities',
    '_scalar_count',
    '_prompt_new_password',
    'user_exists',
    'add_user_with_password',
]



# Database connection settings
config = configparser.ConfigParser()
# The code lives under hotel/, but config.ini stays at the repository root (it is untracked
# and shared by every entry point), so resolve it as the package's parent directory.
config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.ini')
config.read(config_path)
#DATABASE
server = config.get('database', 'server', fallback='' )
database = config.get('database', 'database', fallback='')
username = config.get('database', 'username', fallback='')
password = config.get('database', 'password', fallback='')
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
# The full set of HotelSettings rows the app treats as core, with the values a fresh
# database.sql install seeds. Item 3 of the settings work uses this as the reset target.
DEFAULT_HOTEL_SETTINGS = {
    'hotel_name': 'The Grand Oasis Hotel',
    'tax_rate': '0.13',
    'peak_factor': '1.20',
    'offpeak_factor': '0.90',
    'loyalty_enabled': '1',
    'loyalty_points_per_night': '100',
    'loyalty_accrual_points_per_unit': '0.5',
    'loyalty_redemption_points_per_currency_unit': '100',
    'lockout_threshold': '3',
    'lockout_duration': '5',
    'customer_login_max_attempts': '3',
    'booking_refund_cutoff_days': '7',
}
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
db.init(CONNECTION_STRING)


def _ensure_database_config():
    """Make sure config.ini holds a usable [database] section, prompting if it does not.

    Runs before anything that opens a connection -- including the onboarding marker
    check -- because a fresh checkout has no config.ini at all, so there is nothing to
    read the marker through. On a non-interactive run (tests, piped stdin) it only
    warns and leaves the database connection down; every get_connection() caller
    already handles a None connection.
    """
    global server, database, username, password, CONNECTION_STRING
    if (config.get('database', 'server', fallback='').strip()
            and config.get('database', 'database', fallback='').strip()
            and config.get('database', 'username', fallback='').strip()):
        return
    if not sys.stdin.isatty():
        logging.warning("config.ini is missing its [database] section and stdin is not "
                        "interactive -- cannot prompt. Copy config.ini.example to "
                        "config.ini and fill in server, database, username and password.")
        return
    ui.info("No database connection is configured yet. Let's set one up.")
    server = input("SQL Server host [localhost]: ").strip() or 'localhost'
    database = input("Database name [hotelSystem]: ").strip() or 'hotelSystem'
    username = input("Database user [admin]: ").strip() or 'admin'
    import getpass as _getpass
    password = _getpass.getpass("Database password: ")
    try:
        with open(config_path, 'w', encoding='utf-8') as fh:
            fh.write("[database]\n")
            fh.write(f"server = {server}\n")
            fh.write(f"database = {database}\n")
            fh.write(f"username = {username}\n")
            fh.write(f"password = {password}\n")
        logging.info("Wrote %s. Everything else is configured from the admin menus.", config_path)
    except OSError as e:
        logging.error("Could not write %s: %s", config_path, e)
        return
    config.read(config_path)
    CONNECTION_STRING = (
        'DRIVER={ODBC Driver 17 for SQL Server};'
        f'SERVER={server};'
        f'DATABASE={database};'
        f'UID={username};'
        f'PWD={password}'
    )
    db.init(CONNECTION_STRING)


## =========================
# Audit Trail
## =========================
def require_master_override(prompt="Enter master override secret: "):
    """Verify the master override secret, returning True when it is granted.

    Centralises the check used by admin_login(), the fraud-unlock path, and the
    destructive admin actions. The secret is the password of the plaintext 'master'
    account in Users, matching on username alone. Compare is plaintext on purpose
    (see AGENTS.md -- this is a teaching project and passwords are stored in the clear).
    """
    secret = getpass.getpass(prompt).strip()
    if not secret:
        logging.info("A master override secret is required for this action.")
        return False
    try:
        with db.get_connection() as conn:
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


def log_audit(action, entity_type, entity_id="", details="", old_value=None, new_value=None, user=None):
    """Record who changed what in dbo.AuditLog (migration 016).

    `old_value` / `new_value` (migration 026) record the before/after for value
    changes. The caller owns the before-image and passes it in: re-reading the
    row inside log_audit would be wrong for a re-let, where the old row has
    already been archived or overwritten by the time the audit call happens.
    Both are optional and nullable, so every pre-026 call site keeps working.

    `user` names the actor who made the change. The default (`None`) keeps the
    console behaviour of auditing as `session.CURRENT_USER` -- the process global
    `admin_login()` sets -- and "system" when nobody is signed in. The web path
    passes the request principal explicitly, because under concurrent users the
    global is "the last user who logged in on any console", not the one behind
    this request (PLAN-web-api.md).

    Opens its own connection deliberately: the surrounding business transaction may be
    mid-flight, and an audit write must neither be rolled back with it nor be blamed for
    a failure. Never raises -- a missing AuditLog table (migration 016 not yet applied)
    must not break the operation being audited, so failures are reported once at debug
    level and otherwise swallowed.
    """
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO AuditLog (Username, Action, EntityType, EntityID, Details, OldValue, NewValue) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str((user if user is not None else session.CURRENT_USER) or "system"),
                 str(action or "UNKNOWN"),
                 str(entity_type or "Unknown"), str(entity_id or ""), str(details or ""),
                 None if old_value is None else str(old_value),
                 None if new_value is None else str(new_value)),
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
    with db.get_connection() as conn:
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
    with db.get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT SettingValue FROM HotelSettings WHERE SettingKey = ?", (setting_key,))
            old_row = cursor.fetchone()
            old_value = old_row[0] if old_row is not None and old_row[0] is not None else None
            if old_row is not None:
                cursor.execute("UPDATE HotelSettings SET SettingValue = ? WHERE SettingKey = ?", (str(value), setting_key))
            else:
                cursor.execute("INSERT INTO HotelSettings (SettingKey, SettingValue) VALUES (?, ?)", (setting_key, str(value)))
            conn.commit()
            logging.info("Setting '%s' updated to %s.", setting_key, value)
            log_audit("UPDATE", "Setting", setting_key, f"{setting_key} -> {value}",
                      old_value=old_value, new_value=value)
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
    return _setting_float('tax_rate', 0.13)


def get_hotel_name():
    """The hotel's display name, seeded into HotelSettings and editable by admins."""
    try:
        name = get_setting('hotel_name', None)
        return str(name).strip() if name and str(name).strip() else "The Grand Oasis Hotel"
    except Exception:
        return "The Grand Oasis Hotel"


def get_loyalty_enabled():
    """Whether the loyalty programme is on. Stored as '1'/'0' text in HotelSettings."""
    try:
        raw = get_setting('loyalty_enabled', None)
        if raw is None or str(raw).strip() == '':
            return True
        return str(raw).strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    except Exception:
        return True


def get_lockout_threshold():
    """Failed admin login attempts before lockout."""
    return _setting_int('lockout_threshold', 3)


def get_lockout_duration():
    """Lockout length in minutes."""
    return _setting_int('lockout_duration', 5)


def get_customer_login_max_attempts():
    """Failed guest (booking desk) login attempts before the menu stops asking."""
    return _setting_int('customer_login_max_attempts', 3)


def get_loyalty_accrual_points_per_unit():
    """Points earned per $1 of F&B / room-service spend. A float on purpose.

    Reads as a fraction because the only calibrated value is one BELOW the room earn
    rate -- see `points_per_dollar_order_vs_room()`, which fails if the two are ever
    reordered. `_setting_float` keeps an admin typo from breaking check-out.
    """
    return _setting_float('loyalty_accrual_points_per_unit', 0.5)


def get_loyalty_redemption_points_per_currency_unit():
    return _setting_int('loyalty_redemption_points_per_currency_unit', 100)


def get_loyalty_points_per_night():
    """Base loyalty points awarded per night stayed (before category/tier multipliers)."""
    return _setting_int('loyalty_points_per_night', 100)


def reservation_window_active(check_in, check_out, today):
    """Half-open stay window: a guest is expected in when check_in <= today < check_out.

    The same convention DEVIATIONS.md §6 pins everywhere else in the app. Kept pure so
    the check-in gate in check_in() is pinned by a unit test rather than by a run.
    """
    if check_in is None or check_out is None or today is None:
        return False
    return check_in <= today < check_out


def business_date():
    """The hotel's current business date -- as of 5 October 2026, just the wall clock.

    The operator-controlled `HotelSettings['business_date']` clock and `close_day()` were
    removed at the owner's request: "today" now IS today, on the desk terminal. Reports
    that should speak about another day still can -- occupancy, housekeeping and the
    invoice/day views take an explicit date or window -- so a day that has closed can be
    re-run by passing its date, not by rewinding the clock.
    """
    return datetime.now().date()

def generate_code():
    """Generate a random 5-character alphanumeric code."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=5))

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

            with db.get_connection() as conn:
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
BOOKING_REFUND_CUTOFF_DAYS_DEFAULT = 7


def get_booking_refund_cutoff_days():
    """Admin-editable free-cancellation window, in days before check-in."""
    return _setting_int('booking_refund_cutoff_days', BOOKING_REFUND_CUTOFF_DAYS_DEFAULT)


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


def _reservation_check_in(room_number):
    """The check-in date of a room's live reservation, or None if it has no row.

    Booking payments are keyed on (RoomNumber, StayCheckIn), so this is what ties a
    credit to the stay that earned it. None means "not a stay we can credit" and makes
    the caller skip the credit entirely.
    """
    with db.get_connection() as conn:
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


def _existing_tables(cursor, tables):
    """Subset of `tables` that actually exist, so unapplied migrations are skipped."""
    placeholders = ",".join("?" * len(tables))
    cursor.execute(f"SELECT name FROM sys.tables WHERE name IN ({placeholders})", tuple(tables))
    present = {r[0] for r in cursor.fetchall()}
    return [t for t in tables if t in present]


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


def _edit_general_settings():
    """Admin: the settings that are not pricing or loyalty rates."""
    logging.info(f"Current: name '{get_hotel_name()}', loyalty "
                 f"{'on' if get_loyalty_enabled() else 'off'}, lockout after "
                 f"{get_lockout_threshold()} attempt(s) for {get_lockout_duration()} min, "
                 f"guest logins capped at {get_customer_login_max_attempts()}.")
    try:
        name = input(f"Hotel name [{get_hotel_name()}]: ").strip()
        threshold = input(f"Failed admin logins before lockout [{get_lockout_threshold()}]: ").strip()
        duration = input(f"Lockout length in minutes [{get_lockout_duration()}]: ").strip()
        attempts = input(f"Guest login attempts before stop [{get_customer_login_max_attempts()}]: ").strip()
        enabled = input(f"Loyalty programme on? (y/n) [{'y' if get_loyalty_enabled() else 'n'}]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        logging.info("Cancelled. Nothing was changed.")
        return
    if name:
        set_setting('hotel_name', name)
    for key, raw, current in (('lockout_threshold', threshold, get_lockout_threshold()),
                              ('lockout_duration', duration, get_lockout_duration()),
                              ('customer_login_max_attempts', attempts, get_customer_login_max_attempts())):
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            logging.info(f"'{raw}' is not a whole number; {key} kept at {current}.")
            continue
        if value <= 0:
            logging.info(f"{key} must be positive; kept at {current}.")
            continue
        set_setting(key, value)
    if enabled in ('y', 'yes', '1', 'true', 'on'):
        set_setting('loyalty_enabled', '1')
    elif enabled in ('n', 'no', '0', 'false', 'off'):
        set_setting('loyalty_enabled', '0')


def reset_settings_to_defaults():
    """Admin: overwrite the core HotelSettings rows with the seeded defaults.

    Gated the same way as every other destructive admin action: master override
    plus an exact typed confirmation. Room-type multipliers and the per-room-type
    rates are deliberately NOT reset here -- those are catalogue decisions, not
    hotel policy, and would quietly re-price a hotel's whole inventory.
    """
    ui.box(
        "Reset every core hotel setting to the seeded defaults",
        "This overwrites the values the first-run wizard and Pricing & Settings\n"
        "wrote: hotel name, tax, pricing factors, loyalty rates and on/off,\n"
        "lockout policy, guest login cap and the refund window.\n\n"
        "Room-type rates and points multipliers are NOT touched. AuditLog is kept.\n"
        "This cannot be undone.",
        border_style="red",
    )
    if not ui.ask_confirmation("Proceed?", default="n"):
        logging.info("Cancelled. No settings were changed.")
        return
    if not require_master_override():
        return
    typed = input("Type RESET SETTINGS to confirm: ").strip()
    if typed != "RESET SETTINGS":
        logging.info("Confirmation text did not match. No settings were changed.")
        return
    for key, value in DEFAULT_HOTEL_SETTINGS.items():
        set_setting(key, value)
    logging.info("All core settings reset to the seeded defaults.")
    


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
        with db.get_connection() as conn:
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
def get_amenities(active_only=True):
    """Ordered amenity rows, or [] if the table is missing/empty."""
    try:
        with db.get_connection() as conn:
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


def _scalar_count(sql, params=()):
    """Run a COUNT and return the int, or None if the query could not run.

    None is deliberately distinct from 0. "No rooms" and "the Rooms table is not there" are
    different problems, and a checklist that reports them the same way sends whoever reads
    it to the wrong menu.
    """
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        logging.debug(f"Setup probe failed ({type(e).__name__}: {e})")
        return None


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
        with db.get_connection() as conn:
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
