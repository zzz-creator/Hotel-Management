# type: ignore
"""rooms: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see plans/PLAN-split-main-py.md)."""
import logging
from hotel import db
from hotel import ui
from hotel import session
from hotel import core

__all__ = [
    'ROOM_STATUSES',
    'UNBOOKABLE_ROOM_STATUSES',
    'MAX_ROOM_FLOORS',
    'MAX_ROOMS_PER_FLOOR',
    'get_room_status',
    'upsert_room_if_missing',
    'set_room_status',
    'get_room_type',
    '_room_type_in_conn',
    '_room_status_in_conn',
    '_room_type_setting_key',
    'get_room_type_multiplier',
    'get_all_room_type_multipliers',
    'get_room_types',
    'get_nightly_rate',
    '_nightly_rate_in_conn',
    '_rate_or_none',
    'stay_nightly_rate',
    '_reservations_have_captured_rate',
    'get_captured_nightly_rate',
    'update_room_type_rate',
    'ensure_room_types_seeded',
    'rooms_dashboard',
    'view_rooms',
    'update_room_status',
    'rooms_admin_menu',
    'view_room_type_rates',
    'RECTANGULAR_ROOM_CATEGORIES',
    '_rooms_generation_sql',
    '_ROOMS_DESCRIPTION_SQL',
    'validate_room_layout',
    'count_rooms_to_add',
    'seed_rooms',
]




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
    with db.get_connection() as conn:
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
    with db.get_connection() as conn:
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
    with db.get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT Status FROM Rooms WHERE RoomNumber = ?", (room_number,))
            old_row = cursor.fetchone()
            old_status = old_row[0] if old_row is not None else None
            cursor.execute("UPDATE Rooms SET Status = ? WHERE RoomNumber = ?", (status, room_number))
            conn.commit()
            core.log_audit("UPDATE", "Room", room_number, f"Status -> {status}",
                      old_value=old_status, new_value=status)
            return True
        except Exception as e:
            logging.error(f"Error setting room status: {e}")
            return False


def get_room_type(room_number):
    """Return a room's category from Rooms, falling back to 'Standard' if unknown."""
    if not room_number:
        return "Standard"
    with db.get_connection() as conn:
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
    default = core.DEFAULT_ROOM_TYPE_MULTIPLIERS.get(room_type, 1.0)
    try:
        return float(core.get_setting(_room_type_setting_key(room_type), default) or default)
    except (TypeError, ValueError):
        return default


def get_all_room_type_multipliers():
    """Ordered (room type, multiplier) pairs for every known category."""
    return [(rt, get_room_type_multiplier(rt)) for rt in core.DEFAULT_ROOM_TYPE_MULTIPLIERS]


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
    with db.get_connection() as conn:
        if conn is None:
            return [(rt, rate) for rt, rate in core.DEFAULT_ROOM_TYPE_RATES.items()]
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
        return [(rt, rate, True) for rt, rate in core.DEFAULT_ROOM_TYPE_RATES.items()]
    return [(rt, rate) for rt, rate in core.DEFAULT_ROOM_TYPE_RATES.items()]


def get_nightly_rate(room_type):
    """Nightly rate for a room category. Falls back to Standard, then to 0.0."""
    if not room_type:
        room_type = "Standard"
    try:
        with db.get_connection() as conn:
            if conn is None:
                return float(core.DEFAULT_ROOM_TYPE_RATES.get(room_type, 0.0))
            cursor = conn.cursor()
            cursor.execute("SELECT NightlyRate FROM RoomTypes WHERE RoomType = ?", (room_type,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                return float(row[0])
    except Exception as e:
        logging.error(f"Error reading nightly rate: {e}")
    if room_type in core.DEFAULT_ROOM_TYPE_RATES:
        return float(core.DEFAULT_ROOM_TYPE_RATES[room_type])
    return float(core.DEFAULT_ROOM_TYPE_RATES["Standard"])


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
    return float(core.DEFAULT_ROOM_TYPE_RATES.get(room_type, core.DEFAULT_ROOM_TYPE_RATES["Standard"]))


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


def _reservations_have_captured_rate():
    """Whether Reservations.NightlyRate exists (migration 022). Cached after the first probe.

    Check-out must keep working on a database that has not applied 022, so an uncaptured
    stay is billed at the current rate -- the pre-022 behaviour -- rather than failing
    with an invalid-column error in the middle of settling a bill.
    """
    if session._RESERVATIONS_CAPTURED_RATE_SUPPORT is not None:
        return session._RESERVATIONS_CAPTURED_RATE_SUPPORT
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT COL_LENGTH('dbo.Reservations', 'NightlyRate')")
            row = cursor.fetchone()
            session._RESERVATIONS_CAPTURED_RATE_SUPPORT = bool(row and row[0])
    except Exception as e:
        logging.debug(f"Reservations.NightlyRate probe failed ({type(e).__name__}: {e})")
        session._RESERVATIONS_CAPTURED_RATE_SUPPORT = False
    return session._RESERVATIONS_CAPTURED_RATE_SUPPORT


def get_captured_nightly_rate(room_number):
    """The nightly rate captured on a room's live reservation, or None if there is none.

    None covers all three "not captured" cases and they are deliberately not told apart:
    no live reservation, a pre-022 row, or a category that had no rate. Each of them
    means the same thing to the caller -- fall back to the current rate.
    """
    if not room_number or not _reservations_have_captured_rate():
        return None
    try:
        with db.get_connection() as conn:
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
    with db.get_connection() as conn:
        if conn is None:
            logging.info("Database connection failed.")
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT NightlyRate FROM RoomTypes WHERE RoomType = ?", (room_type,))
            old_row = cursor.fetchone()
            old_rate = float(old_row[0]) if old_row is not None and old_row[0] is not None else None
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
            core.log_audit("UPDATE", "RoomType", room_type, f"NightlyRate -> {nightly_rate:.2f}",
                      old_value=f"{old_rate:.2f}" if old_rate is not None else None,
                      new_value=f"{nightly_rate:.2f}")
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
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT RoomType FROM RoomTypes")
            existing = {str(r[0]).strip().lower() for r in cursor.fetchall()}
            added = 0
            for room_type, rate in core.DEFAULT_ROOM_TYPE_RATES.items():
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
# Rooms & Housekeeping
## =========================
def rooms_dashboard():
    """Global room snapshot: status counts + housekeeping load by floor."""
    try:
        with db.get_connection() as conn:
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
    on_date = core.business_date()
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
        with db.get_connection() as conn:
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
        pts_per_night[rt] = core.get_loyalty_points_per_night() * get_room_type_multiplier(rt)

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
            ui.pause()
            ui.show_menu("Rooms & Housekeeping", [
                "1. Room Dashboard",
                "2. Explore Floor",
                "3. Back to Admin Panel",
            ])
        else:
            ui.pause()
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
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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
                core.log_audit("CREATE", "Room", "layout",
                          f"Seeded {added} room(s) from the "
                          f"{'tower' if tower else f'{floors}f x {rooms_per_floor}'} layout")
                logging.info(f"Seeded {added} room(s).")
            return added
    except Exception as e:
        logging.error(f"Error seeding rooms: {e}")
        return 0
