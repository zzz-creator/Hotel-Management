# type: ignore
"""conference_catering: separate catering module for conference bookings."""
import logging
from hotel import db
from hotel import ui
from hotel import core

__all__ = [
    'CATERING_PACKAGES',
    'place_catering_order',
    'list_catering_orders',
    'mark_catering_delivered',
    'catering_admin_menu',
]

CATERING_PACKAGES = {
    "Breakfast": 15.00,
    "Lunch": 25.00,
    "Dinner": 40.00,
    "Tea break": 8.00,
}


def place_catering_order(booking_id, package_type, head_count, unit_price=None):
    if package_type not in CATERING_PACKAGES and unit_price is None:
        logging.info("Unknown package.")
        return None
    if unit_price is None:
        unit_price = CATERING_PACKAGES[package_type]
    try:
        head_count = int(head_count)
        unit_price = float(unit_price)
    except (TypeError, ValueError):
        logging.info("Invalid values.")
        return None
    total = round(head_count * unit_price, 2)
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ConferenceCateringOrders (BookingID, PackageType, HeadCount, UnitPrice, TotalAmount) "
                "OUTPUT INSERTED.CateringOrderID VALUES (?, ?, ?, ?, ?)",
                (booking_id, package_type, head_count, unit_price, total))
            row = cursor.fetchone()
            oid = int(row[0]) if row and row[0] is not None else None
            conn.commit()
        if oid is not None:
            core.log_audit("CREATE", "ConferenceCatering", oid, f"booking {booking_id}: {package_type} x{head_count}")
        return oid
    except Exception as e:
        logging.error(f"Error placing catering order: {e}")
        return None


def list_catering_orders(booking_id=None, status=None):
    try:
        with db.get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            sql = ("SELECT CateringOrderID, BookingID, PackageType, HeadCount, UnitPrice, "
                   "TotalAmount, Status, PlacedAt FROM ConferenceCateringOrders WHERE 1=1")
            params = []
            if booking_id is not None:
                sql += " AND BookingID = ?"
                params.append(booking_id)
            if status:
                sql += " AND Status = ?"
                params.append(status)
            sql += " ORDER BY PlacedAt DESC"
            cursor.execute(sql, params)
            return [tuple(r) for r in cursor.fetchall()]
    except Exception as e:
        logging.debug(f"Catering unreadable ({type(e).__name__}: {e})")
        return []


def mark_catering_delivered(order_id):
    try:
        with db.get_connection() as conn:
            if conn is None:
                return False
            cursor = conn.cursor()
            cursor.execute("UPDATE ConferenceCateringOrders SET Status='Delivered', DeliveredAt=GETDATE() WHERE CateringOrderID=?", (order_id,))
            if cursor.rowcount == 0:
                return False
            conn.commit()
        core.log_audit("UPDATE", "ConferenceCatering", order_id, "Delivered")
        return True
    except Exception as e:
        logging.error(f"Error: {e}")
        return False


def catering_admin_menu():
    while True:
        ui.pause()
        ui.show_menu("Conference Catering", [
            "1. List catering orders",
            "2. Place a catering order",
            "3. Mark delivered",
            "4. Back",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            orders = list_catering_orders()
            if not orders:
                ui.info("No catering orders.")
            else:
                ui.show_table("Catering Orders",
                              ["#", "Booking", "Package", "Heads", "Unit", "Total", "Status"],
                              [(o[0], o[1], o[2], o[3], f"${o[4]:,.2f}", f"${o[5]:,.2f}", o[6]) for o in orders])
        elif choice == '2':
            try:
                bid = int(input("Booking ID: ").strip())
            except ValueError:
                logging.info("Invalid id.")
                continue
            ui.show_table("Packages", ["Package", "Per head"], [(k, f"${v:,.2f}") for k, v in CATERING_PACKAGES.items()])
            pkg = input("Package: ").strip()
            try:
                heads = int(input("Head count: ").strip())
            except ValueError:
                logging.info("Invalid count.")
                continue
            oid = place_catering_order(bid, pkg, heads)
            if oid is not None:
                ui.success(f"Catering order #{oid} placed.")
        elif choice == '3':
            try:
                oid = int(input("Order ID: ").strip())
            except ValueError:
                logging.info("Invalid id.")
                continue
            if mark_catering_delivered(oid):
                ui.success("Marked delivered.")
        elif choice == '4':
            return
        else:
            logging.info("Invalid choice.")