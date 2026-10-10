# type: ignore
"""keycards: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see plans/PLAN-split-main-py.md)."""
import logging
import random
import string
from datetime import datetime
from datetime import timedelta
import pyodbc
from hotel import db
from hotel import ui
from hotel import session
from hotel import core
from hotel import rooms

__all__ = [
    'KEY_CARD_PREFIX',
    '_new_card_number',
    'issue_key_card',
    '_insert_card_with_unique_number',
    'revoke_active_key_cards',
    'move_key_cards',
    'get_active_key_card',
    '_render_key_card',
    'show_active_key_card',
    'get_key_cards_for_room',
    '_log_door_event',
    'try_open_door',
    'revoke_key_card',
    'door_access_menu',
    'view_my_key_card',
]


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
            with db.get_connection() as conn:
                if conn is None:
                    return None
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM KeyCards WHERE CardNumber = ?", (candidate,))
                if not cursor.fetchone():
                    return candidate
        except Exception as e:
            logging.debug(f"Card number probe failed ({type(e).__name__}: {e})")
            return None


def issue_key_card(room_number, last_name=None, first_name=None):
    """Issue an Active key card for a checked-in stay, expiring at check-out.

    A card is only ever issued for a stay that is checked in **today**: the room's
    reservation must be inside its half-open window (check-in <= today < check-out)
    and, when the room is tracked in `Rooms`, housekeeping must have it `Occupied`.
    That is what stops a card being handed out for a vacant or future room, and it
    guarantees the card has a real expiry -- the stay's check-out day -- rather than
    the open-ended card the old code issued for a room with no reservation.

    Any existing Active card for the room is revoked first, so a re-issued card never
    leaves a stale one live. Returns the card number, or None.
    """
    if not room_number:
        return None
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CheckInDate, CheckOutDate FROM Reservations WHERE RoomNumber = ?",
                (room_number,),
            )
            stay = cursor.fetchone()
            if not stay or not core.reservation_window_active(
                    stay[0], stay[1], core.business_date()):
                logging.info(
                    f"No key card issued for room {room_number}: no guest is checked in "
                    "there today."
                )
                return None
            room_status = rooms._room_status_in_conn(cursor, room_number)
            if room_status is not None and room_status != "Occupied":
                logging.info(
                    f"No key card issued for room {room_number}: the room is "
                    f"'{room_status}', not Occupied."
                )
                return None
            # Expiry is the stay's check-out day, so the card is valid through the last
            # night even though check-out itself is the morning after.
            expires_at = datetime.combine(stay[1], datetime.min.time()) + timedelta(days=1)
            cursor.execute(
                "UPDATE KeyCards SET Status = 'Revoked', RevokedAt = ?, RevokeReason = ? "
                "WHERE RoomNumber = ? AND Status = 'Active'",
                (datetime.now(), "Superseded by a new card", room_number),
            )
            card_number = _insert_card_with_unique_number(
                cursor, room_number, last_name, first_name, expires_at, session.CURRENT_USER)
            if not card_number:
                return None
            conn.commit()
            core.log_audit("ISSUE", "KeyCard", card_number,
                      f"Issued for room {room_number}, valid until {expires_at:%Y-%m-%d}")
            logging.info(f"Key card {card_number} issued for room {room_number}.")
            return card_number
    except Exception as e:
        # Migration 017 not applied: door access is an add-on, never block check-in.
        logging.debug(f"Key card issue skipped ({type(e).__name__}: {e})")
        return None


def _insert_card_with_unique_number(cursor, room_number, last_name, first_name,
                                    expires_at, issued_by, attempts=5):
    """INSERT a new Active card, retrying a fresh number on a uniqueness collision.

    `_new_card_number()` probes for a free number, but the probe and this INSERT are
    not atomic, so a race can still hit `UQ_KeyCards_CardNumber`. Retry a new number a
    few times instead of failing the whole issue; return the number written, or None.
    """
    for _ in range(attempts):
        card_number = _new_card_number()
        if not card_number:
            return None
        try:
            cursor.execute(
                "INSERT INTO KeyCards (CardNumber, RoomNumber, LastName, FirstName, Status, "
                "IssuedAt, ExpiresAt, IssuedBy) VALUES (?, ?, ?, ?, 'Active', ?, ?, ?)",
                (card_number, room_number, last_name, first_name, datetime.now(),
                 expires_at, issued_by),
            )
            return card_number
        except pyodbc.IntegrityError:
            logging.debug(f"Card number {card_number} already taken; trying another.")
    logging.warning(
        f"Could not find a unique key card number after {attempts} attempts."
    )
    return None


def revoke_active_key_cards(room_number, reason=""):
    """Revoke every Active card for a room. Returns the number revoked."""
    if not room_number:
        return 0
    try:
        with db.get_connection() as conn:
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
                core.log_audit("REVOKE", "KeyCard", card, f"Room {room_number}: {reason or 'revoked'}")
            return cursor.rowcount or 0
    except Exception as e:
        logging.debug(f"Key card revoke skipped ({type(e).__name__}: {e})")
        return 0


def move_key_cards(old_room_number, new_room_number):
    """Re-point a guest's active cards when a reservation moves to another room."""
    if not old_room_number or not new_room_number:
        return 0
    try:
        with db.get_connection() as conn:
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
                core.log_audit("UPDATE", "KeyCard", new_room_number,
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
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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
                core.log_audit("UPDATE", "KeyCard", card_number, "Auto-expired on door read")
                return False, f"Card {card_number} expired on {expires_at:%Y-%m-%d}."
            if room_number and core.normalize_room_number(card_room) != core.normalize_room_number(room_number):
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
        with db.get_connection() as conn:
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
            core.log_audit("REVOKE", "KeyCard", card_number, reason or "Revoked by staff")
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
        ui.pause()
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
            with db.get_connection() as conn:
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
                # issue_key_card() logs the specific reason it refused (no checked-in
                # guest, room not Occupied) at info; a missing migration 017 only logs
                # at debug, so name it here rather than staying silent about it.
                logging.info(
                    "No key card was issued. A guest must be checked in to the room today, "
                    "and migration 017 must be applied."
                )
        elif choice == '2':
            card_number = input("Enter card number to revoke: ").strip()
            revoke_key_card(card_number, "Revoked at front desk")
        elif choice == '3':
            card_number = input("Enter card number reported lost: ").strip()
            try:
                with db.get_connection() as conn:
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
                        core.log_audit("REVOKE", "KeyCard", card_number, "Reported lost")
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
                with db.get_connection() as conn:
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


def _render_key_card(card, title="My Key Card"):
    """Render a KeyCards row as the guest-facing card panel.

    Pure formatting: no lookup and no door event, so the check-in flow can reuse
    the exact panel the guest sees under "My Key Card".
    """
    expires = card.ExpiresAt.strftime("%Y-%m-%d") if card.ExpiresAt else "end of stay"
    ui.box(
        title,
        f"Card number: {card.CardNumber}\n"
        f"Room:         {card.RoomNumber}\n"
        f"Status:       {card.Status}\n"
        f"Valid until:  {expires}",
    )


def show_active_key_card(room_number, title="My Key Card"):
    """Show the Active card for a room as a card panel.

    Returns the card row when one was displayed, else None. Shared by "My Key
    Card" and by check-in, so both render the same card and show its number.
    """
    card = get_active_key_card(room_number)
    if not card:
        return None
    _render_key_card(card, title)
    return card


def view_my_key_card():
    """Guest: show the key card for the room they are staying in."""
    room_number, first_name = core.validate_room()
    if not room_number:
        logging.info("Could not verify your room.")
        return
    card = show_active_key_card(room_number)
    if not card:
        logging.info(f"No active key card for room {room_number}. Please see the front desk.")
        return
    _log_door_event(card.CardNumber, room_number, "Granted", "Guest viewed key card")
