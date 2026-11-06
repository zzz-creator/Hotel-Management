# type: ignore
"""conference: conference room catalog (Phase A). Split into its own module following the
hotel/ package conventions: module-qualified cross calls, get_connection() context manager,
conn is None guard after every DB open, log_audit for changes."""
import logging
from hotel import db
from hotel import ui
from hotel import session
from hotel import core
from hotel import payments

__all__ = [
    'CONFERENCE_SETUP_TYPES',
    'validate_conference_room',
    'get_conference_rooms',
    'get_conference_room',
    'add_conference_room',
    'update_conference_room_rate',
    'set_conference_room_active',
    'get_room_setups',
    'add_room_setup',
    'remove_room_setup',
    'conference_room_admin_menu',
    'conference_rooms_panel',
    'book_conference_room',
    'conference_bookings_inbox',
    'conference_guest_panel',
    'view_my_conference_bookings',
]

CONFERENCE_SETUP_TYPES = ("Theatre", "Classroom", "Banquet", "U-Shape", "Boardroom")


def validate_conference_room(room_number, name, capacity, hourly_rate):
    """Pure normaliser. Returns (ok, cleaned) or (False, None) on bad input.

    Mirrors validate_room_layout(): bounds live here so the wizard, the admin CRUD and
    any future caller agree on what is acceptable. A mistyped prompt must never traceback.
    """
    try:
        room_number = str(room_number or "").strip()
        name = str(name or "").strip()
        capacity = int(capacity)
        hourly_rate = float(hourly_rate)
    except (TypeError, ValueError):
        return False, None
    if not room_number or not name:
        return False, None
    if not (1 <= capacity <= 10000):
        return False, None
    if not (0 <= hourly_rate <= 100000):
        return False, None
    return True, (room_number, name, capacity, round(hourly_rate, 2))


def get_conference_rooms(active_only=True):
    """Return list of (RoomID, RoomNumber, Name, Capacity, HourlyRate, Active) rows."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT RoomID, RoomNumber, Name, Capacity, HourlyRate, Active "
                "FROM ConferenceRooms " + ("WHERE Active = 1 " if active_only else "")
                + "ORDER BY RoomNumber"
            )
            return [(r[0], r[1], r[2], r[3], float(r[4]) if r[4] is not None else 0.0,
                     bool(r[5])) for r in cursor.fetchall()]
    except Exception as e:
        logging.debug(f"ConferenceRooms unreadable ({type(e).__name__}: {e})")
        return []


def get_conference_room(room_number):
    """Return (RoomID, RoomNumber, Name, Capacity, HourlyRate, Active) or None."""
    if not room_number:
        return None
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT RoomID, RoomNumber, Name, Capacity, HourlyRate, Active "
                "FROM ConferenceRooms WHERE RoomNumber = ?", (room_number,))
            row = cursor.fetchone()
            if not row:
                return None
            return (row[0], row[1], row[2], row[3],
                    float(row[4]) if row[4] is not None else 0.0, bool(row[5]))
    except Exception as e:
        logging.debug(f"ConferenceRooms unreadable ({type(e).__name__}: {e})")
        return None


def add_conference_room(room_number, name, capacity, hourly_rate):
    """Insert a new conference room. Returns the new RoomID or None."""
    ok, cleaned = validate_conference_room(room_number, name, capacity, hourly_rate)
    if not ok:
        logging.info("Invalid conference room details.")
        return None
    room_number, name, capacity, hourly_rate = cleaned
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ConferenceRooms (RoomNumber, Name, Capacity, HourlyRate) "
                "OUTPUT INSERTED.RoomID VALUES (?, ?, ?, ?)",
                (room_number, name, capacity, hourly_rate))
            row = cursor.fetchone()
            new_id = int(row[0]) if row and row[0] is not None else None
            conn.commit()
        if new_id is not None:
            core.log_audit("CREATE", "ConferenceRoom", new_id,
                           f"{room_number} ({name}) cap {capacity} @ {hourly_rate:.2f}/hr")
        return new_id
    except Exception as e:
        logging.error(f"Error adding conference room: {e}")
        return None


def update_conference_room_rate(room_number, hourly_rate):
    """Update a conference room's hourly rate. Returns True on success."""
    try:
        hourly_rate = round(float(hourly_rate), 2)
    except (TypeError, ValueError):
        logging.info("Invalid hourly rate.")
        return False
    if hourly_rate < 0:
        logging.info("Hourly rate cannot be negative.")
        return False
    room = get_conference_room(room_number)
    if room is None:
        logging.info("Conference room not found.")
        return False
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE ConferenceRooms SET HourlyRate = ? WHERE RoomNumber = ?",
                (hourly_rate, room_number))
            if cursor.rowcount == 0:
                return False
            conn.commit()
        core.log_audit("UPDATE", "ConferenceRoom", room[0],
                       f"HourlyRate -> {hourly_rate:.2f}",
                       old_value=f"{room[4]:.2f}", new_value=f"{hourly_rate:.2f}")
        return True
    except Exception as e:
        logging.error(f"Error updating conference room rate: {e}")
        return False


def set_conference_room_active(room_number, active):
    """Activate/deactivate a conference room. Returns True on success."""
    room = get_conference_room(room_number)
    if room is None:
        return False
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE ConferenceRooms SET Active = ? WHERE RoomNumber = ?",
                (1 if active else 0, room_number))
            if cursor.rowcount == 0:
                return False
            conn.commit()
        core.log_audit("UPDATE", "ConferenceRoom", room[0],
                       f"Active -> {1 if active else 0}",
                       old_value="1" if room[5] else "0",
                       new_value="1" if active else "0")
        return True
    except Exception as e:
        logging.error(f"Error updating conference room active flag: {e}")
        return False


def get_room_setups(room_number):
    """Return list of (SetupID, SetupType, Capacity, Active) for a room."""
    room = get_conference_room(room_number)
    if room is None:
        return []
    try:
        with db.get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT SetupID, SetupType, Capacity, Active "
                "FROM ConferenceRoomSetups WHERE RoomID = ? ORDER BY SetupType",
                (room[0],))
            return [(r[0], r[1], r[2], bool(r[3])) for r in cursor.fetchall()]
    except Exception as e:
        logging.debug(f"ConferenceRoomSetups unreadable ({type(e).__name__}: {e})")
        return []


def add_room_setup(room_number, setup_type, capacity):
    """Add a setup type to a conference room. Returns True on success."""
    room = get_conference_room(room_number)
    if room is None:
        logging.info("Conference room not found.")
        return False
    if setup_type not in CONFERENCE_SETUP_TYPES:
        logging.info("Unknown setup type. Available: " + ", ".join(CONFERENCE_SETUP_TYPES))
        return False
    try:
        capacity = int(capacity)
    except (TypeError, ValueError):
        logging.info("Invalid capacity.")
        return False
    if capacity < 0 or capacity > 10000:
        logging.info("Capacity must be between 0 and 10000.")
        return False
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ConferenceRoomSetups (RoomID, SetupType, Capacity) VALUES (?, ?, ?)",
                (room[0], setup_type, capacity))
            conn.commit()
        core.log_audit("CREATE", "ConferenceRoomSetup", room[0],
                       f"{room_number}: {setup_type} cap {capacity}")
        return True
    except Exception as e:
        logging.error(f"Error adding conference room setup: {e}")
        return False


def remove_room_setup(setup_id):
    """Remove a setup type row by id. Returns True on success."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ConferenceRoomSetups WHERE SetupID = ?", (setup_id,))
            if cursor.rowcount == 0:
                return False
            conn.commit()
        core.log_audit("DELETE", "ConferenceRoomSetup", setup_id, "removed")
        return True
    except Exception as e:
        logging.error(f"Error removing conference room setup: {e}")
        return False


def conference_room_admin_menu():
    """Admin submenu: manage conference rooms, setups and rates."""
    while True:
        ui.pause()
        ui.show_menu("Conference Rooms", [
            "1. List conference rooms",
            "2. Add a conference room",
            "3. Set hourly rate",
            "4. Activate / deactivate a room",
            "5. Manage setup types for a room",
            "6. Back to Admin Panel",
        ])
        choice = ui.ask_text("Enter your choice: ")
        if choice == '1':
            rooms = get_conference_rooms(active_only=False)
            if not rooms:
                ui.info("No conference rooms configured yet.")
            else:
                ui.show_table("Conference Rooms",
                              ["#", "Room", "Name", "Capacity", "Rate/hr", "Active"],
                              [(r[0], r[1], r[2], r[3], f"${r[4]:,.2f}",
                                "Yes" if r[5] else "No") for r in rooms])
                for r in rooms:
                    setups = get_room_setups(r[1])
                    if setups:
                        ui.show_table(f"Setups - {r[1]}",
                                      ["Setup", "Capacity", "Active"],
                                      [(s[1], s[2], "Yes" if s[3] else "No") for s in setups])
        elif choice == '2':
            room_number = ui.ask_text("Room number (label, e.g. CR-101): ")
            name = ui.ask_text("Room name: ")
            try:
                capacity = ui.ask_number("Capacity: ")
            except ValueError:
                logging.info("Invalid capacity.")
                continue
            try:
                rate = ui.ask_float("Hourly rate: ")
            except ValueError:
                logging.info("Invalid rate.")
                continue
            new_id = add_conference_room(room_number, name, capacity, rate)
            if new_id is not None:
                ui.success(f"Conference room '{room_number}' added (id {new_id}).")
                # Offer to add at least one setup type right away.
                if ui.ask_confirmation("Add a setup type now?", default="y"):
                    st = ui.ask_text("Setup type (" + ", ".join(CONFERENCE_SETUP_TYPES) + "): ")
                    try:
                        cap = ui.ask_number("Capacity for that setup: ")
                    except ValueError:
                        cap = capacity
                    add_room_setup(room_number, st, cap)
        elif choice == '3':
            room_number = ui.ask_text("Room number: ")
            room = get_conference_room(room_number)
            if room is None:
                logging.info("Conference room not found.")
                continue
            try:
                new_rate = ui.ask_float(f"New hourly rate (current ${room[4]:,.2f}): ")
            except ValueError:
                logging.info("Invalid rate.")
                continue
            if update_conference_room_rate(room_number, new_rate):
                ui.success(f"Rate updated for {room_number}.")
        elif choice == '4':
            room_number = ui.ask_text("Room number: ")
            room = get_conference_room(room_number)
            if room is None:
                logging.info("Conference room not found.")
                continue
            new_state = not room[5]
            if set_conference_room_active(room_number, new_state):
                ui.success(f"{room_number} is now {'active' if new_state else 'inactive'}.")
        elif choice == '5':
            room_number = ui.ask_text("Room number: ")
            room = get_conference_room(room_number)
            if room is None:
                logging.info("Conference room not found.")
                continue
            while True:
                setups = get_room_setups(room_number)
                ui.show_table(f"Setups - {room_number}",
                              ["ID", "Setup", "Capacity", "Active"],
                              [(s[0], s[1], s[2], "Yes" if s[3] else "No") for s in setups]
                              if setups else [])
                ui.show_menu("Setup types", [
                    "1. Add a setup type",
                    "2. Remove a setup type",
                    "3. Back",
                ])
                sub = ui.ask_text("Enter your choice: ")
                if sub == '1':
                    st = ui.ask_text("Setup type (" + ", ".join(CONFERENCE_SETUP_TYPES) + "): ")
                    try:
                        cap = ui.ask_number("Capacity: ")
                    except ValueError:
                        logging.info("Invalid capacity.")
                        continue
                    if add_room_setup(room_number, st, cap):
                        ui.success("Setup added.")
                elif sub == '2':
                    try:
                        sid = ui.ask_number("Setup ID to remove: ")
                    except ValueError:
                        logging.info("Invalid id.")
                        continue
                    if remove_room_setup(sid):
                        ui.success("Setup removed.")
                elif sub == '3':
                    break
                else:
                    logging.info("Answer 1, 2 or 3.")
        elif choice == '6':
            return
        else:
            logging.info("Invalid choice. Please try again.")


def _hours_between(start, end):
    try:
        delta = end - start
        return max(delta.total_seconds() / 3600.0, 0.0)
    except Exception:
        return 0.0


def conference_booking_overlaps(room_id, start, end, exclude_id=None):
    """Return True if any non-cancelled booking overlaps [start, end) for room_id."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            sql = ("SELECT COUNT(*) FROM ConferenceBookings "
                   "WHERE RoomID = ? AND Status <> 'Cancelled' "
                   "AND StartAt < ? AND EndAt > ?")
            params = [room_id, end, start]
            if exclude_id is not None:
                sql += " AND BookingID <> ?"
                params.append(exclude_id)
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return bool(row and row[0] and int(row[0]) > 0)
    except Exception as e:
        logging.debug(f"Overlap check failed ({type(e).__name__}: {e})")
        return False


def get_conference_bookings(room_number=None, status=None):
    """Return list of booking rows (dicts as tuples)."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            sql = ("SELECT b.BookingID, r.RoomNumber, r.Name, b.StartAt, b.EndAt, "
                   "b.AttendeeCount, b.SetupType, b.Status, b.BookedBy, b.CustomerID "
                   "FROM ConferenceBookings b JOIN ConferenceRooms r ON r.RoomID = b.RoomID "
                   "WHERE 1=1")
            params = []
            if room_number:
                sql += " AND r.RoomNumber = ?"
                params.append(room_number)
            if status:
                sql += " AND b.Status = ?"
                params.append(status)
            sql += " ORDER BY b.StartAt DESC"
            cursor.execute(sql, params)
            return [tuple(r) for r in cursor.fetchall()]
    except Exception as e:
        logging.debug(f"ConferenceBookings unreadable ({type(e).__name__}: {e})")
        return []


def create_conference_booking(room_number, start, end, attendee_count, setup_type,
                             equipment=None, booked_by=None, customer_id=None,
                             notes=None):
    """Create a booking after an overlap + capacity check. Returns BookingID or None.

    The caller is responsible for payment having succeeded before calling this, OR
    for passing a pre-validated card. This function does not prompt for a card.
    """
    room = get_conference_room(room_number)
    if room is None:
        logging.info("Conference room not found.")
        return None
    room_id = room[0]
    # capacity check against setup if provided
    if setup_type:
        setups = [s for s in get_room_setups(room_number) if s[1] == setup_type and s[3]]
        if setups:
            if attendee_count > setups[0][2] and setups[0][2] > 0:
                logging.info("Attendee count exceeds setup capacity (%d).", setups[0][2])
                return None
    if attendee_count < 1:
        logging.info("Attendee count must be at least 1.")
        return None
    if end <= start:
        logging.info("End must be after start.")
        return None
    if conference_booking_overlaps(room_id, start, end):
        logging.info("That slot overlaps an existing booking for this room.")
        return None
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ConferenceBookings (RoomID, StartAt, EndAt, CustomerID, "
                "BookedBy, AttendeeCount, SetupType, Notes) "
                "OUTPUT INSERTED.BookingID VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (room_id, start, end, customer_id, booked_by, attendee_count,
                 setup_type, notes))
            row = cursor.fetchone()
            booking_id = int(row[0]) if row and row[0] is not None else None
            if booking_id is not None and equipment:
                for eq_type, qty in equipment.items():
                    cursor.execute(
                        "INSERT INTO ConferenceBookingEquipment (BookingID, EquipmentType, Quantity) "
                        "VALUES (?, ?, ?)", (booking_id, eq_type, max(1, int(qty))))
            conn.commit()
        if booking_id is not None:
            core.log_audit("CREATE", "ConferenceBooking", booking_id,
                           f"{room_number} {start} -> {end} ({attendee_count} attendees)")
        return booking_id
    except Exception as e:
        logging.error(f"Error creating conference booking: {e}")
        return None


def cancel_conference_booking(booking_id, cancelled_by=None):
    """Cancel a booking. Returns True on success."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("SELECT Status, StartAt FROM ConferenceBookings WHERE BookingID = ?",
                           (booking_id,))
            row = cursor.fetchone()
            if not row:
                logging.info("Booking not found.")
                return False
            if row[0] == 'Cancelled':
                logging.info("Already cancelled.")
                return False
            cursor.execute(
                "UPDATE ConferenceBookings SET Status='Cancelled', CancelledAt=GETDATE(), "
                "CancelledBy=? WHERE BookingID = ?", (cancelled_by, booking_id))
            conn.commit()
        core.log_audit("CANCEL", "ConferenceBooking", booking_id,
                       f"by {cancelled_by}")
        return True
    except Exception as e:
        logging.error(f"Error cancelling conference booking: {e}")
        return False


def complete_conference_booking(booking_id):
    """Mark a booking completed and issue a ConferenceInvoice. Returns invoice id or None."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT b.RoomID, b.StartAt, b.EndAt, r.HourlyRate "
                "FROM ConferenceBookings b JOIN ConferenceRooms r ON r.RoomID = b.RoomID "
                "WHERE b.BookingID = ?", (booking_id,))
            row = cursor.fetchone()
            if not row:
                logging.info("Booking not found.")
                return None
            room_id, start, end, rate = row[0], row[1], row[2], float(row[3] or 0)
            hours = _hours_between(start, end)
            subtotal = round(hours * rate, 2)
            tax_rate = core.get_tax_rate()
            tax = round(subtotal * tax_rate, 2)
            total = round(subtotal + tax, 2)
            cursor.execute(
                "UPDATE ConferenceBookings SET Status='Completed' WHERE BookingID = ?",
                (booking_id,))
            cursor.execute(
                "INSERT INTO ConferenceInvoices (BookingID, Subtotal, TaxAmount, TotalAmount, AmountPaid) "
                "OUTPUT INSERTED.ConferenceInvoiceID VALUES (?, ?, ?, ?, ?)",
                (booking_id, subtotal, tax, total, total))
            inv_row = cursor.fetchone()
            inv_id = int(inv_row[0]) if inv_row and inv_row[0] is not None else None
            conn.commit()
        core.log_audit("COMPLETE", "ConferenceBooking", booking_id,
                       f"invoice {inv_id} total {total:.2f}")
        return inv_id
    except Exception as e:
        logging.error(f"Error completing conference booking: {e}")
        return None


def book_conference_room(customer_id=None):
    """Interactive conference room booking wizard (staff or guest)."""
    rooms = get_conference_rooms(active_only=True)
    if not rooms:
        ui.info("No conference rooms available.")
        return
    ui.show_table("Conference Rooms", ["#", "Room", "Name", "Capacity", "Rate/hr"],
                  [(r[0], r[1], r[2], r[3], f"${r[4]:,.2f}") for r in rooms])
    try:
        choice = ui.ask_number("Select room number by index (1-based): ")
    except ValueError:
        logging.info("Invalid selection.")
        return
    if choice < 1 or choice > len(rooms):
        logging.info("Invalid selection.")
        return
    room = rooms[choice - 1]
    room_number = room[1]
    setups = [s for s in get_room_setups(room_number) if s[3]]
    setup_type = None
    if setups:
        ui.show_table(f"Setups - {room_number}", ["#", "Setup", "Capacity"],
                      [(i + 1, s[1], s[2]) for i, s in enumerate(setups)])
        st = ui.ask_text("Select setup (blank = none): ")
        if st.isdigit() and 1 <= int(st) <= len(setups):
            setup_type = setups[int(st) - 1][1]
    try:
        attendee_count = ui.ask_number("Attendee count: ")
    except ValueError:
        logging.info("Invalid count.")
        return
    # capacity check
    if setup_type:
        cap = next((s[2] for s in setups if s[1] == setup_type), room[3])
        if attendee_count > cap and cap > 0:
            logging.info("Attendee count exceeds capacity (%d).", cap)
            return
    from datetime import datetime, timedelta
    try:
        start_str = ui.ask_text("Start (YYYY-MM-DD HH:MM): ")
        end_str = ui.ask_text("End (YYYY-MM-DD HH:MM): ")
        start = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
        end = datetime.strptime(end_str, "%Y-%m-%d %H:%M")
    except ValueError:
        logging.info("Invalid date/time format.")
        return
    if end <= start:
        logging.info("End must be after start.")
        return
    if conference_booking_overlaps(room[0], start, end):
        logging.info("That slot overlaps an existing booking.")
        return
    # equipment
    equipment = {}
    if ui.ask_confirmation("Add equipment?", default="n"):
        while True:
            eq = ui.ask_text("Equipment type (blank to finish): ")
            if not eq:
                break
            try:
                q = int(ui.ask_text(f"Quantity for {eq}: ") or "1")
            except ValueError:
                q = 1
            equipment[eq] = q
    booked_by = session.CURRENT_USER or (f"customer:{customer_id}" if customer_id else "guest")
    # payment
    hours = _hours_between(start, end)
    total = round(hours * room[4], 2)
    tax = round(total * core.get_tax_rate(), 2)
    grand = round(total + tax, 2)
    logging.info(f"Quote: {hours:.2f} hr x ${room[4]:,.2f} = ${total:,.2f} + tax ${tax:,.2f} = ${grand:,.2f}")
    # card payment required
    if not payments.process_credit_card(grand):
        logging.info("Payment declined. No booking created.")
        return
    booking_id = create_conference_booking(
        room_number, start, end, attendee_count, setup_type,
        equipment=equipment, booked_by=booked_by, customer_id=customer_id,
        notes=None)
    if booking_id is None:
        logging.info("Booking could not be created.")
        return
    # record invoice
    try:
        with db.get_connection() as conn:
            if conn is not None:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO ConferenceInvoices (BookingID, Subtotal, TaxAmount, TotalAmount, AmountPaid) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (booking_id, total, tax, grand, grand))
                conn.commit()
    except Exception as e:
        logging.debug(f"Invoice write failed: {e}")
    core.log_audit("CREATE", "ConferenceBooking", booking_id, f"paid {grand:.2f}")
    # staff alert if guest self-service
    if customer_id is not None:
        try:
            with db.get_connection() as conn:
                if conn is not None:
                    conn.cursor().execute(
                        "INSERT INTO StaffAlerts (TargetRole, Message, Severity) VALUES (?, ?, ?)",
                        ("admin", f"New conference booking #{booking_id} by guest customer {customer_id}", "Info"))
                    conn.commit()
        except Exception:
            pass
    ui.success(f"Conference booking #{booking_id} confirmed. Paid ${grand:,.2f}.")


def conference_bookings_inbox():
    """Staff inbox: list and action conference bookings."""
    bookings = get_conference_bookings()
    if not bookings:
        ui.info("No conference bookings on record.")
        return
    ui.show_table("Conference Bookings",
                  ["#", "Room", "Start", "End", "Attendees", "Setup", "Status", "Booked by"],
                  [(b[0], b[1], str(b[3])[:16], str(b[4])[:16], b[5], b[6] or "-", b[7], b[8] or "-")
                   for b in bookings])
    raw = ui.ask_text("Enter a booking number to action (blank to skip): ")
    if not raw.isdigit():
        return
    bid = int(raw)
    ui.show_menu("Action", ["1. Mark in progress", "2. Mark completed", "3. Cancel", "4. Back"])
    act = ui.ask_text("Choice: ")
    if act == '1':
        try:
            with db.get_connection() as conn:
                if conn is not None:
                    conn.cursor().execute("UPDATE ConferenceBookings SET Status='InProgress' WHERE BookingID=?", (bid,))
                    conn.commit()
            ui.success("Marked in progress.")
        except Exception as e:
            logging.error(f"Error: {e}")
    elif act == '2':
        inv = complete_conference_booking(bid)
        if inv is not None:
            ui.success(f"Completed. Invoice #{inv} issued.")
    elif act == '3':
        if cancel_conference_booking(bid, session.CURRENT_USER):
            ui.success("Cancelled.")
    else:
        return


def view_my_conference_bookings(customer_id):
    """Guest: view their conference bookings."""
    bookings = get_conference_bookings()
    mine = [b for b in bookings if b[9] == customer_id]
    if not mine:
        ui.info("No conference bookings on record.")
        return
    ui.show_table("My Conference Bookings",
                  ["#", "Room", "Start", "End", "Attendees", "Setup", "Status"],
                  [(b[0], b[1], str(b[3])[:16], str(b[4])[:16], b[5], b[6] or "-", b[7])
                   for b in mine])


def conference_guest_panel():
    """Guest-facing conference room panel."""
    from hotel import customer as _c
    # reuse customer login gate
    try:
        _c.customer_login(allow_register=False)
    except Exception:
        pass
    cid = session.CURRENT_CUSTOMER
    if cid is None:
        logging.info("Please sign in to book a conference room.")
        return
    while True:
        ui.pause()
        ui.show_menu("Conference Rooms", [
            "1. Browse rooms",
            "2. Book a conference room",
            "3. My conference bookings",
            "4. Back",
        ])
        choice = ui.ask_text("Enter your choice: ")
        if choice == '1':
            conference_rooms_panel()
        elif choice == '2':
            book_conference_room(customer_id=cid)
        elif choice == '3':
            view_my_conference_bookings(cid)
        elif choice == '4':
            return
        else:
            logging.info("Invalid choice.")


def conference_rooms_panel():
    """Guest-facing browse of conference rooms (read-only)."""
    rooms = get_conference_rooms(active_only=True)
    if not rooms:
        ui.info("No conference rooms are available right now.")
        return
    for r in rooms:
        ui.show_table(f"{r[1]} - {r[2]}", ["Field", "Value"],
                      [("Capacity", r[3]), ("Rate per hour", f"${r[4]:,.2f}"),
                       ("Active", "Yes" if r[5] else "No")])
        setups = get_room_setups(r[1])
        if setups:
            ui.show_table(f"Setups - {r[1]}",
                          ["Setup", "Capacity"],
                          [(s[1], s[2]) for s in setups if s[3]])