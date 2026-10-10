# type: ignore
"""concierge: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see plans/PLAN-split-main-py.md)."""
import logging
from datetime import datetime
from hotel import db
from hotel import ui
from hotel import session
from hotel import core

__all__ = [
    'contact_concierge',
    'view_my_concierge_requests',
    'guest_requests_menu',
    '_concierge_inbox',
    '_feedback_inbox',
]




def contact_concierge():
    """File a concierge request. Persisted, tracked, and answerable by staff."""
    room_number = ""
    first_name = ""
    last_name = ""
    try:
        room_number, first_name = core.validate_room()
        if room_number:
            with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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
        core.log_audit("CREATE", "ConciergeRequest", request_id, f"Room {room_number}: {message}")
        logging.info("Your message has been sent. A concierge will get back to you shortly.")
        logging.info(f"For immediate assistance, please call the front desk at 123-456-7890.")
        logging.info(f"Your request reference is #{request_id}.")
    except Exception as e:
        logging.error(f"Error contacting concierge: {e}")


def view_my_concierge_requests():
    """Guest: check the status of the concierge requests they filed."""
    room_number, _ = core.validate_room()
    if not room_number:
        logging.info("Could not verify your room.")
        return
    try:
        with db.get_connection() as conn:
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
        ui.pause()
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
        with db.get_connection() as conn:
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
                (response, new_status, new_status, datetime.now(), session.CURRENT_USER, int(raw)),
            )
            conn.commit()
        core.log_audit("RESOLVE" if resolve else "UPDATE", "ConciergeRequest", int(raw), f"Status -> {new_status}")
        if request[1]:
            try:
                with db.get_connection() as conn:
                    if conn is not None:
                        conn.cursor().execute(
                            "INSERT INTO Notifications (RoomNumber, Message, Channel, SentBy) "
                            "VALUES (?, ?, 'In-Room', ?)",
                            (request[1], f"Concierge reply: {response}", session.CURRENT_USER),
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
        with db.get_connection() as conn:
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
