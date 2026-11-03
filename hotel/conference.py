# type: ignore
"""conference: conference room catalog (Phase A). Split into its own module following the
hotel/ package conventions: module-qualified cross calls, get_connection() context manager,
conn is None guard after every DB open, log_audit for changes."""
import logging
from hotel import db
from hotel import ui
from hotel import session
from hotel import core

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
                       f"Active -> {1 if active else 0}")
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
        choice = input("Enter your choice: ").strip()
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
            room_number = input("Room number (label, e.g. CR-101): ").strip()
            name = input("Room name: ").strip()
            try:
                capacity = int(input("Capacity: ").strip())
            except ValueError:
                logging.info("Invalid capacity.")
                continue
            try:
                rate = float(input("Hourly rate: ").strip())
            except ValueError:
                logging.info("Invalid rate.")
                continue
            new_id = add_conference_room(room_number, name, capacity, rate)
            if new_id is not None:
                ui.success(f"Conference room '{room_number}' added (id {new_id}).")
                # Offer to add at least one setup type right away.
                if ui.ask_confirmation("Add a setup type now?", default="y"):
                    st = input("Setup type (" + ", ".join(CONFERENCE_SETUP_TYPES) + "): ").strip()
                    try:
                        cap = int(input("Capacity for that setup: ").strip())
                    except ValueError:
                        cap = capacity
                    add_room_setup(room_number, st, cap)
        elif choice == '3':
            room_number = input("Room number: ").strip()
            room = get_conference_room(room_number)
            if room is None:
                logging.info("Conference room not found.")
                continue
            try:
                new_rate = float(input(f"New hourly rate (current ${room[4]:,.2f}): ").strip())
            except ValueError:
                logging.info("Invalid rate.")
                continue
            if update_conference_room_rate(room_number, new_rate):
                ui.success(f"Rate updated for {room_number}.")
        elif choice == '4':
            room_number = input("Room number: ").strip()
            room = get_conference_room(room_number)
            if room is None:
                logging.info("Conference room not found.")
                continue
            new_state = not room[5]
            if set_conference_room_active(room_number, new_state):
                ui.success(f"{room_number} is now {'active' if new_state else 'inactive'}.")
        elif choice == '5':
            room_number = input("Room number: ").strip()
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
                sub = input("Enter your choice: ").strip()
                if sub == '1':
                    st = input("Setup type (" + ", ".join(CONFERENCE_SETUP_TYPES) + "): ").strip()
                    try:
                        cap = int(input("Capacity: ").strip())
                    except ValueError:
                        logging.info("Invalid capacity.")
                        continue
                    if add_room_setup(room_number, st, cap):
                        ui.success("Setup added.")
                elif sub == '2':
                    try:
                        sid = int(input("Setup ID to remove: ").strip())
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