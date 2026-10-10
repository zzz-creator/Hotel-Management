# type: ignore
"""reservations: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
import time
import re
from datetime import datetime
from datetime import timedelta
from . import db
from . import ui
from . import core
from . import keycards
from . import loyalty
from . import session
from . import rooms as _mod_rooms

__all__ = [
    'add_reservation',
    '_archive_row',
    'archive_reservation',
    '_book_reservation_in_conn',
    'search_availability',
    'arrivals_departures_board',
    'show_arrivals_departures_board',
    '_show_housekeeping_rooms',
    'show_availability_search',
    'delete_reservation',
    'RESERVATION_RESET_SCOPES',
    'RESERVATION_RESET_CONFIRM',
    'delete_all_reservations',
    'edit_reservation',
    'view_reservations',
    'search_reservations',
    'link_reservation_customer',
    'check_in_eligibility',
    'perform_check_in',
    'check_in',
]




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
        today = core.business_date()
        check_in = ui.ask_date("Enter check-in date (YYYY-MM-DD)", default=str(today))
        check_out = ui.ask_date("Enter check-out date (YYYY-MM-DD)", default=str(today + timedelta(days=1)))
        if check_out <= check_in:
            logging.info("Check-out date must be after check-in date. Reservation not added.")
            return
        with db.get_connection() as conn:
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
            room_status = _mod_rooms.get_room_status(room_number)
            if room_status in _mod_rooms.UNBOOKABLE_ROOM_STATUSES:
                if room_status == "Maintenance":
                    logging.info(f"Room {room_number} is under maintenance and cannot be reserved.")
                else:
                    logging.info(f"Room {room_number} is '{room_status}' and cannot be reserved until housekeeping marks it Available.")
                return
            # If this room is not tracked in Rooms yet (e.g. seed data not applied),
            # register it so the status board stays complete.
            _mod_rooms.upsert_room_if_missing(room_number)
            # The rate is captured HERE, at the moment the stay is made, and check-out
            # bills this number rather than whatever RoomTypes says then. Without it an
            # admin rate edit silently re-prices every confirmed booking.
            nightly_rate = _mod_rooms.get_nightly_rate(_mod_rooms.get_room_type(room_number))
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
                     _mod_rooms._rate_or_none(nightly_rate), room_number),
                )
                conn.commit()
                core.log_audit("UPDATE", "Reservation", room_number,
                          f"Re-booked for {last_name} {first_name} ({check_in} to {check_out}) at "
                          f"${nightly_rate:,.2f}/night; previous stay archived")
                logging.info(f"Room {room_number} re-booked for {last_name} {first_name} (check-in {check_in}, check-out {check_out}, ${nightly_rate:,.2f} per night). Previous stay archived.")
            else:
                cursor.execute(
                    "INSERT INTO Reservations (RoomNumber, Floor, LastName, FirstName, CheckInDate, CheckOutDate, NightlyRate) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (room_number, floor, last_name, first_name, check_in, check_out,
                     _mod_rooms._rate_or_none(nightly_rate)),
                )
                conn.commit()
                core.log_audit("CREATE", "Reservation", room_number,
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
        if _mod_rooms._reservations_have_captured_rate():
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
        room_type = _mod_rooms.get_room_type(row.RoomNumber)
        # The archive records the rate the outgoing stay WAS billed at, not today's rate
        # for its category, so a later re-pricing cannot rewrite what that stay cost.
        archived_rate = _mod_rooms.stay_nightly_rate(
            row[6] if len(row) > 6 else None, _mod_rooms.get_nightly_rate(room_type))
        cursor.execute(
            "INSERT INTO ReservationArchive (RoomNumber, Floor, LastName, FirstName, "
            "CheckInDate, CheckOutDate, Nights, RoomType, NightlyRate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row.RoomNumber, row.Floor, row.LastName, row.FirstName,
             row.CheckInDate, row.CheckOutDate, core.stay_nights(row.CheckInDate, row.CheckOutDate),
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
        with db.get_connection() as conn:
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
    today = core.business_date()
    m = re.match(r'^(\d+)(\d{3})$', room_number)
    floor = int(m.group(1)) if m else 0
    cursor = conn.cursor()
    actual_type = None
    if room_type is not None:
        actual_type = _mod_rooms._room_type_in_conn(cursor, room_number)
        if actual_type is not None and actual_type.strip().lower() != room_type.strip().lower():
            return False, f"room {room_number} is a {actual_type}, not a {room_type}"
    # The rate follows the room's ACTUAL category, which is the one that was just
    # verified. A room with no Rooms row is "cannot verify" and is allowed through, so it
    # falls back to the guest's requested category.
    rate = _mod_rooms._rate_or_none(_mod_rooms._nightly_rate_in_conn(cursor, actual_type or room_type))
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
    status = _mod_rooms._room_status_in_conn(cursor, room_number)
    if status in _mod_rooms.UNBOOKABLE_ROOM_STATUSES:
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
        with db.get_connection() as conn:
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
            placeholders = ",".join("?" for _ in _mod_rooms.UNBOOKABLE_ROOM_STATUSES)
            where = [f"(Status IS NULL OR Status NOT IN ({placeholders}))"]
            params.extend(_mod_rooms.UNBOOKABLE_ROOM_STATUSES)
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
            if core.stays_overlap(start, end, check_in, check_out)}
    return [(r[0], r[1], r[2]) for r in rooms if r[0] not in busy][:limit]


def arrivals_departures_board(on_date=None):
    """Staff board for a given day: arrivals, in-house guests, and departures.

    Returns (arrivals, in_house, departures) where each entry is a
    (room_number, last_name, first_name, check_in, check_out) tuple.
    """
    on_date = on_date or core.business_date()
    try:
        with db.get_connection() as conn:
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
    today = core.business_date()
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
        with db.get_connection() as conn:
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
    today = core.business_date()
    check_in = ui.ask_date("Check-in date", default=str(today))
    check_out = ui.ask_date("Check-out date", default=str(today + timedelta(days=1)))
    if check_out <= check_in:
        logging.info("Check-out date must be after check-in date.")
        return
    room_type = input(f"Room type (blank for any of: {', '.join(rt for rt, _ in _mod_rooms.get_room_types())}): ").strip()
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
        [(rn, rt, st, f"${_mod_rooms.get_nightly_rate(rt):,.2f}") for rn, rt, st in results],
    )


def delete_reservation():
    try:
        room_number = input("Enter room number (floor + 3-digit code) to delete: ").strip()
        with db.get_connection() as conn:
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
            keycards.revoke_active_key_cards(room_number, "reservation deleted")
            core.log_audit("DELETE", "Reservation", room_number, "Reservation deleted")
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
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            targets = core._existing_tables(cursor, tables)
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
    if not core.require_master_override():
        return
    typed = input(f"Type {RESERVATION_RESET_CONFIRM} to confirm: ").strip()
    if typed != RESERVATION_RESET_CONFIRM:
        logging.info("Confirmation text did not match. No reservations were deleted.")
        return

    deleted = {}
    try:
        with db.get_connection() as conn:
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

    core.log_audit("DELETE", "Reservation", "ALL",
              f"Bulk reservation reset ({scope_label}): "
              + ", ".join(f"{k}={v}" for k, v in deleted.items()))
    ui.success(f"Deleted {deleted.get('Reservations', 0)} reservation(s).")
    ui.show_table("Deleted rows", ["Table", "Rows"],
                  [(k, v) for k, v in deleted.items() if v])


def edit_reservation():
    """Edit an existing reservation, including the room number and floor."""
    with db.get_connection() as conn:
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
                    if target_reservation is not None and target_reservation.CheckOutDate > core.business_date():
                        logging.info(f"Room number {new_room_number} is already reserved (check-out {target_reservation.CheckOutDate}).")
                        return
                    if target_reservation is not None:
                        logging.info(f"Room {new_room_number} has a completed past stay on record; its reservation row will be reused.")
                    # Availability guard: never move a guest into a room that is not
                    # sellable -- maintenance, or not yet cleaned. See
                    # UNBOOKABLE_ROOM_STATUSES.
                    target_status = _mod_rooms._room_status_in_conn(cursor, new_room_number)
                    if target_status in _mod_rooms.UNBOOKABLE_ROOM_STATUSES:
                        if target_status == "Maintenance":
                            logging.info(f"Room {new_room_number} is under maintenance and cannot be assigned.")
                        else:
                            logging.info(f"Room {new_room_number} is '{target_status}' and cannot be assigned until housekeeping marks it Available.")
                        return
                    # Keep the status board complete if the target room is not tracked yet.
                    _mod_rooms.upsert_room_if_missing(new_room_number)
                    # A move is the one thing that legitimately re-captures the rate: the
                    # guest is being given a different room, and the stay has to be billed
                    # at the rate of the room they are actually in. This is NOT the same as
                    # an admin rate edit, which never touches a booked stay. The outgoing
                    # stay's own rate went into ReservationArchive when the room was
                    # re-let, so the old number is not lost.
                    new_rate = _mod_rooms._rate_or_none(
                        _mod_rooms._nightly_rate_in_conn(cursor, _mod_rooms._room_type_in_conn(cursor, new_room_number)))

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
                    if core.get_loyalty_enabled():
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
                    keycards.move_key_cards(old_room_number, new_room_number)

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
                    core.log_audit("UPDATE", "Reservation", new_room_number,
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
                    core.log_audit("UPDATE", "Reservation", new_room_number,
                              f"Guest details edited to {new_last_name} {new_first_name}")
                    logging.info(f"Reservation for room {old_room_number} updated successfully. \n New details: Room Number: {new_room_number}, Floor: {new_floor}, Last Name: {new_last_name}, First Name: {new_first_name}")
            else:
                logging.info("No reservation found with that room number.")
        except Exception as e:
            logging.error(f"Error editing reservation: {e}")

def view_reservations():
    """Display all reservations including the floor."""
    with db.get_connection() as conn:
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
    # connection closed by context manager

def search_reservations():
    try:
        search_type = input("Search by room number (RN)/last name(LN): ").strip().lower()
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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
            # rowcount 0 means the WHERE clause matched nothing: the room is gone, or
            # the stay already belongs to a DIFFERENT customer. Read back what is true
            # so the caller can tell "already mine" (fine) from "not linked" (report).
            cursor.execute(
                "SELECT CustomerID FROM Reservations WHERE RoomNumber = ?",
                (room_number,),
            )
            row = cursor.fetchone()
            owner = row.CustomerID if row else None
            return owner is not None and int(owner) == int(customer_id)
    except Exception as e:
        # Before 019 there is no CustomerID column; the stay simply has no loyalty.
        logging.debug(f"Could not link reservation {room_number} to a customer: {e}")
        return False


def check_in_eligibility(room_number, today=None):
    """Non-interactive check-in pre-flight: is this stay inside its reservation window?

    The console adapter runs this BEFORE prompting for contact details, so a guest
    whose check-in will be refused is never asked for anything (that is how the old
    flow read the gate). The web path skips it -- perform_check_in() re-checks the
    window itself as its first step.

    Returns {"ok": True}, or {"ok": False, "reason": "window", "check_in": ...,
    "check_out": ..., "today": ...} when the stay exists but its window is not active.
    A missing reservation row or an unreachable database returns {"ok": False,
    "reason": "no_stay" | "unavailable"}; the console treats those two the way the
    original flow did, i.e. it still attempts the check-in, because validate_room()
    certified the stay a moment earlier.
    """
    today = today or core.business_date()
    try:
        with db.get_connection() as conn:
            if conn is None:
                return {"ok": False, "reason": "unavailable"}
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CheckInDate, CheckOutDate FROM Reservations WHERE RoomNumber = ?",
                (room_number,),
            )
            row = cursor.fetchone()
            if not row:
                return {"ok": False, "reason": "no_stay"}
            if not core.reservation_window_active(row.CheckInDate, row.CheckOutDate, today):
                return {"ok": False, "reason": "window",
                        "check_in": row.CheckInDate, "check_out": row.CheckOutDate,
                        "today": today}
            return {"ok": True}
    except Exception as e:
        logging.error(f"Error checking check-in window: {e}")
        return {"ok": False, "reason": "unavailable"}


def perform_check_in(room_number, first_name, email=None, phone=None, actor=None):
    """Non-interactive check-in state changes for a known stay.

    The twin the web path calls and the console adapter delegates to (PLAN-web-api.md
    phase 2). Mirrors check_in()'s STATE, not its screen: the window gate, the
    room -> "Occupied" status, the customer-profile upsert and stay link, and the key
    card, then ONE audit row naming `actor` instead of the process global. Every later
    step is best-effort the same way the original was -- a key-card failure must not
    fail the check-in.

    Returns:
      {"ok": True, "room_number": ..., "key_card": str|None, "customer_id": int|None}
      {"ok": False, "reason": "window", "check_in": ..., "check_out": ..., "today": ...}

    `email`/`phone` are the contact fields the console prompts for between the gate and
    this call (hence the separate check_in_eligibility() pre-flight); the web path
    passes them straight from the request. A stay is still linked to a name-only
    profile when both are blank, exactly as the console flow did.
    """
    today = core.business_date()
    try:
        with db.get_connection() as conn:
            if conn is not None:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT CheckInDate, CheckOutDate FROM Reservations WHERE RoomNumber = ?",
                    (room_number,),
                )
                row = cursor.fetchone()
                if row and not core.reservation_window_active(row.CheckInDate, row.CheckOutDate, today):
                    return {"ok": False, "reason": "window",
                            "check_in": row.CheckInDate, "check_out": row.CheckOutDate,
                            "today": today}
        # A valid stay: now it is real state, mark the room occupied on check-in.
        _mod_rooms.set_room_status(room_number, "Occupied")
    except Exception as e:
        logging.error(f"Error syncing room status on check-in: {e}")
    # Capture or refresh the guest's contact details in CustomerProfiles.
    customer_id = None
    try:
        with db.get_connection() as conn:
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
                    customer_id = core.upsert_customer_profile(last_name, guest_first, email, phone)
                    if customer_id:
                        # A front-desk or pre-019 stay has no customer link yet. Now
                        # that the profile exists, attach it so the stay's points are
                        # credited to this person instead of to the room.
                        link_reservation_customer(room_number, customer_id)
    except Exception as e:
        logging.error(f"Error saving customer profile on check-in: {e}")
    # Issue the guest's key card, expiring at the reservation's check-out.
    key_card = None
    try:
        with db.get_connection() as conn:
            guest_last = None
            if conn is not None:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT LastName FROM Reservations WHERE RoomNumber = ?", (room_number,)
                )
                res = cursor.fetchone()
                if res:
                    guest_last = res[0]
        key_card = keycards.issue_key_card(room_number, guest_last, first_name)
    except Exception as e:
        key_card = None
        logging.error(f"Error issuing key card on check-in: {e}")
    core.log_audit("UPDATE", "Reservation", room_number,
              f"Check-in completed for {first_name}"
              + (f" (key card {key_card})" if key_card else ""),
              user=actor)
    return {"ok": True, "room_number": room_number, "key_card": key_card,
            "customer_id": customer_id}


def check_in():
    """Handle customer check-in using validate_room and display amenities."""
    room_number, first_name = core.validate_room()  # Assume validate_room returns (room_number, first_name)
    if room_number and first_name:
        logging.info("Checking in...")
        time.sleep(2)
        logging.info("Verifying Identity...")
        time.sleep(2)
        logging.info("Finalizing check-in...")
        time.sleep(1)
        # The window gate runs BEFORE the contact prompts -- a guest whose check-in
        # will be refused is never asked for their details. The state changes happen
        # in perform_check_in(), which re-checks the gate for the web path.
        gate = check_in_eligibility(room_number)
        if not gate["ok"] and gate["reason"] == "window":
            logging.info(
                f"Check-in refused: today ({gate['today']}) is outside the reservation's "
                f"date window ({gate['check_in']} to {gate['check_out']}). "
                "Please verify the stay dates before checking in."
            )
            return
        email = input("Please enter your email (optional, press Enter to skip): ").strip() or None
        phone = input("Please enter your phone number (optional, press Enter to skip): ").strip() or None
        result = perform_check_in(room_number, first_name, email=email, phone=phone,
                                  actor=session.CURRENT_USER)
        if not result["ok"]:
            # The gate moved against us between the pre-flight and the transaction;
            # say so and stop, exactly as the original gate did.
            logging.info(
                f"Check-in refused: today ({result['today']}) is outside the reservation's "
                f"date window ({result['check_in']} to {result['check_out']}). "
                "Please verify the stay dates before checking in."
            )
            return
        if result["customer_id"]:
            logging.info("Contact details saved to your customer profile.")
        else:
            logging.info("Could not save contact details at this time.")
        key_card = result["key_card"]
        logging.info("Room number and key card are being prepared...")
        time.sleep(2)
        logging.info(f"Your room number is {room_number}.")
        if key_card:
            # Show the same card panel the guest sees under "My Key Card", then state
            # the number in plain text as well so it is readable without the box.
            keycards.show_active_key_card(room_number, title="Your Key Card")
            logging.info(f"Your key card number is {key_card} and is ready for use.")
        else:
            logging.info("Your key card is ready for use. Please collect it from the front desk.")
        logging.info(f"Check-in successful! Welcome to {core.get_hotel_name()}, {first_name.capitalize()}!")
        logging.info("Please enjoy your stay!")
        logging.info(f"If you need assistance, please call the front desk at {room_number}-56.\n")
        if core.get_loyalty_enabled():
            try:
                details = loyalty.get_tier_details_by_room(room_number)
            except Exception:
                details = None
            # Perks are text -- but at least the guest hears which ones this stay earns.
            # The discount % and point multiplier are enforced at check-out and in the
            # accrual functions; free-text extras (breakfast, late checkout) are honored
            # by the front desk reading this line.
            if details and details.get("perks"):
                logging.info(f"Your {details['tier']} status includes: {details['perks']}")
        amenities = core.get_amenities()
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
        core.log_audit("UPDATE", "Reservation", "-",
                  "Check-in refused: no reservation matched the name and room given")
