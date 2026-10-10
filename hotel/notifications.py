# type: ignore
"""notifications: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see plans/PLAN-split-main-py.md)."""
import logging
from datetime import datetime
from hotel import db
from hotel import ui
from hotel import session
from hotel import core

__all__ = [
    'send_notification_to_customer',
    'view_notifications_for_room',
    'send_alert_to_staff',
    'view_staff_alerts',
]



def send_notification_to_customer():
    """Record a notification for a guest, persisted so the guest can actually read it."""
    try:
        while True:
            room_number = input("Enter customer's room number: ").strip()
            with db.get_connection() as conn:
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
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO Notifications (RoomNumber, Message, Channel, SentBy) "
                "OUTPUT INSERTED.NotificationID VALUES (?, ?, ?, ?)",
                (room_number, message, channel_name, session.CURRENT_USER),
            )
            conn.commit()
        core.log_audit("CREATE", "Notification", room_number, f"[{channel_name}] {message}")
        logging.info(f"Notification sent to room {room_number}: {message}")
    except Exception as e:
        logging.error(f"Error sending notification: {e}")


def view_notifications_for_room():
    """Guest: read the notifications left for their room."""
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
            ui.pause()
            ui.show_menu("Send Alert to Staff", [
                f"{idx}. {role}" for idx, role in enumerate(core.STAFF_ROLES, 1)
            ] + ["0. Cancel"])
            raw = input("Enter the number of the staff role: ").strip()
            if raw == '0':
                return
            if raw.isdigit() and 1 <= int(raw) <= len(core.STAFF_ROLES):
                selected_role = core.STAFF_ROLES[int(raw) - 1]
                break
            logging.info("Invalid choice. Please try again.")
        message = input("Enter alert message: ").strip()
        if not message:
            logging.info("Empty message. Alert not sent.")
            return
        severity = input("Severity - (I)nfo/(W)arning/(C)ritical (default info): ").strip().lower() or 'i'
        severity_name = {'i': 'Info', 'w': 'Warning', 'c': 'Critical'}.get(severity, 'Info')
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO StaffAlerts (TargetRole, Message, Severity) "
                "OUTPUT INSERTED.AlertID VALUES (?, ?, ?)",
                (selected_role, message, severity_name),
            )
            conn.commit()
        core.log_audit("CREATE", "StaffAlert", selected_role, f"[{severity_name}] {message}")
        logging.info(f"Alert sent to {selected_role} staff: {message}")
    except Exception as e:
        logging.error(f"Error sending alert: {e}")


def view_staff_alerts():
    """Staff: list alerts, newest first, and acknowledge one."""
    try:
        with db.get_connection() as conn:
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
                        (datetime.now(), session.CURRENT_USER, int(raw)),
                    )
                    conn.commit()
                    core.log_audit("ACK", "StaffAlert", int(raw), f"Acknowledged by {session.CURRENT_USER}")
                    logging.info(f"Alert {raw} acknowledged.")
    except Exception as e:
        logging.error(f"Error viewing staff alerts: {e}")
