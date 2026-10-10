# type: ignore
"""orders: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
from decimal import Decimal
from datetime import datetime
from . import db
from . import ui
from . import session
from . import billing
from . import core
from . import items
from . import loyalty
from . import payments

__all__ = [
    'ORDER_STATUSES',
    'ORDER_STATUS_HELP',
    'open_order',
    'add_order_items',
    'place_order',
    'order_item',
    'view_amenities',
    'provide_feedback',
    'get_promotions',
    'view_promotions',
    'track_order_status',
    'get_orders_for_room',
    'get_order_items',
    'get_order',
    'manage_orders_menu',
    'next_order_status',
    'advance_order',
    'manage_amenities_menu',
    'manage_promotions_menu',
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


def open_order(room_number):
    """Create a Placed order for a room so the guest can track it. Returns OrderID or None."""
    try:
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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


def place_order(room_number, ordered_items, pay_mode="bill", card_processor=None,
                discount_code=None, quote=None, actor=None):
    """Record an order's charges and open its trackable record -- the non-interactive tail
    of order_item().

    `ordered_items` are (item_id, list_unit_price, quantity) lines at list price.
    `pay_mode` is "pay_now" or "bill". For "pay_now" the charges are priced (discount
    code + loyalty tier + tax) and each line recorded at its discounted, pre-tax price
    with `paid=True`, the loyalty points awarded, and the card charged through
    `card_processor(amount) -> ok`; `quote` is the (final_total, discounted_subtotal,
    tax, savings_lines, discount_factor) tuple the console computed interactively, or
    billing.price_pay_now() is used when it is None. For "bill" the lines are recorded at
    list price on the room bill, settled at check-out.

    `actor` names the audit row. Returns a dict: {"status": "ok"|"declined", "order_id",
    "total", "paid", "final_total", "discounted_subtotal", "tax", "savings_lines"}. A
    declined card records nothing -- the console then asks whether to bill instead (that
    prompt belongs to the console); the web refuses outright.
    """
    ordered_items = [(int(i), float(p), int(q)) for (i, p, q) in ordered_items if p]
    total = round(sum(p * q for (_i, p, q) in ordered_items), 2)
    result = {"status": "ok", "order_id": None, "total": total, "paid": 0.0,
              "final_total": total, "discounted_subtotal": total, "tax": 0.0,
              "savings_lines": []}
    if pay_mode == "pay_now":
        if quote is None:
            quote = billing.price_pay_now(total, room_number, discount_code)
        final_total, discounted_subtotal, tax, savings_lines, discount_factor = quote
        result.update(final_total=final_total, discounted_subtotal=discounted_subtotal,
                      tax=tax, savings_lines=list(savings_lines))
        authorised = False
        if card_processor is not None:
            try:
                outcome = card_processor(final_total)
                authorised = bool(outcome[0]) if isinstance(outcome, tuple) else bool(outcome)
            except Exception as e:
                logging.error(f"Error processing card for order: {e}")
                authorised = False
        if not authorised:
            result["status"] = "declined"
            return result
        tx_ids = []
        for (itm_id, unit_price, qty) in ordered_items:
            discounted_price = round(unit_price * discount_factor, 2)
            tx = billing.record_transaction_for_room(room_number, itm_id, qty, discounted_price, paid=True)
            if tx is not None:
                tx_ids.append(tx)
        loyalty.award_billed_order_points(room_number, tx_ids)
        order_id = open_order(room_number)
        add_order_items(order_id, [(i, round(p * discount_factor, 2), q) for (i, p, q) in ordered_items])
        result["order_id"] = order_id
        result["paid"] = final_total
        core.log_audit("POST", "Order", order_id, f"Paid now: ${final_total:.2f} for room {room_number}", user=actor)
        return result
    for (itm_id, unit_price, qty) in ordered_items:
        billing.record_transaction_for_room(room_number, itm_id, qty, unit_price, paid=False)
    order_id = open_order(room_number)
    add_order_items(order_id, ordered_items)
    result["order_id"] = order_id
    core.log_audit("POST", "Order", order_id, f"Added to room bill: ${total:.2f} for room {room_number}", user=actor)
    return result


def order_item():
    logging.info("Welcome to the ordering system!")
# Existing ordering code starts here
    total = 0.0  # Initialize total as a float
    redemption_codes = []
    ordered_items = []  # list of (item_id, unit_price, quantity)

    room_number, first_name = core.validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify your last name, first name, and room number. Please try again.")
        return  # Return to main menu if validation fails

    logging.info(f"Welcome, {first_name.capitalize()}! Room {room_number} validated successfully.")

    while True:
        item_choice = items.get_item_choice()
        if item_choice is None:
            continue  # Return to item choice if error occurs

        quantity = items.get_quantity()
        try:
            item_id_str, price = item_choice
            logging.debug(f"Item Choice: {item_choice}")
            item_id = int(item_id_str)  # Convert item ID to integer

            price = items.get_dynamic_price(item_id)

            if price:
                if isinstance(price, Decimal):
                    price = float(price)
                total += price * quantity
                code = core.generate_code()
                redemption_codes.append((item_id, code))
                ordered_items.append((item_id, price, quantity))
                logging.info(f"Total so far: ${total:.2f}")
                logging.info(f"Redemption code for item {item_id}: {code}")
            else:
                logging.info("Item not found. Please try again.")
        except Exception as e:
            logging.error(f"Error during item processing: {e}")

        if not items.get_another_item():
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
            with db.get_connection() as conn:
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
            discounted_subtotal, _disc_amount, _tier_amount, savings_lines = billing.compute_and_apply_discounts(total, room_number)
            tax = discounted_subtotal * core.get_tax_rate()
            final_total = discounted_subtotal + tax
            discount_factor = (discounted_subtotal / total) if total > 0 else 0.0
            ui.box("Receipt", "\n".join(
                ["----- Receipt -----"] + item_lines + savings_lines +
                [f"Subtotal: ${discounted_subtotal:.2f}",
                 f"Tax (Tax Rate: {core.get_tax_rate()*100:.0f}%): ${tax:.2f}",
                 f"Total Amount: ${final_total:.2f}",
                 "-------------------"]
            ))
            placed = place_order(
                room_number, ordered_items, pay_mode="pay_now",
                card_processor=payments.process_credit_card,
                quote=(final_total, discounted_subtotal, tax, savings_lines, discount_factor),
                actor=session.CURRENT_USER,
            )
            if placed["status"] == "declined":
                add_later = input("Payment declined. Add this order to your room bill instead? (Y/N): ").strip().lower()
                if add_later == 'y':
                    # Added to the room bill: record at full list price (the discount is
                    # applied again at check-out when the bill is settled).
                    place_order(room_number, ordered_items, pay_mode="bill", actor=session.CURRENT_USER)
                    logging.info("Order added to your room bill and will be settled at check-out.")
                else:
                    logging.info("Order cancelled; no charges recorded.")
                    return
            elif placed["order_id"]:
                logging.info(f"Payment received. Your order is confirmed. Track it as order #{placed['order_id']}.")
            else:
                logging.info("Payment received. Your order is confirmed.")
        else:
            # Pay later: charges are recorded on the room bill and settled at check-out.
            placed = place_order(room_number, ordered_items, pay_mode="bill", actor=session.CURRENT_USER)
            logging.info(f"This amount (${placed['total']:.2f}) will be added to your room bill and settled at check-out.")
    except Exception as e:
        logging.error(f"Error finalizing order: {e}")
    logging.info("Thank you for your order! It will be delivered shortly.")


def view_amenities():
    """Display the hotel's amenities, from the admin-editable Amenities table."""
    rows = core.get_amenities()
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
        rating = None
        while rating is None:
            raw = input("Please rate our service on a scale of 1-5: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= 5:
                rating = int(raw)
            else:
                logging.info("Invalid rating. Please enter a number between 1 and 5.")
        comment = input("Please provide any additional comments: ").strip()
        with db.get_connection() as conn:
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
        core.log_audit("CREATE", "Feedback", room_number or "(walk-in)", f"Rating {rating}/5")
        logging.info("Thank you for your feedback!")
        logging.info(f"Rating: {rating}, Comments: {comment}")
    except Exception as e:
        logging.error(f"Error capturing feedback: {e}")


def get_promotions(active_only=True):
    """Current promotions, filtered by Active and the start/end date window."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return []
            cursor = conn.cursor()
            today = core.business_date()
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
    room_number, _ = core.validate_room()
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
        with db.get_connection() as conn:
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
        with db.get_connection() as conn:
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


def get_order(order_id):
    """One order's record: (order_id, room_number, status, placed_at, updated_at, notes),
    or None when no such order exists.

    The detail read the console's queue only summarizes, and the pre-read a status change
    does to answer 404 without writing. None means "not found"; a connection failure raises
    RuntimeError and a query failure propagates, so a caller can tell missing from
    unavailable.
    """
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        return None
    with db.get_connection() as conn:
        if conn is None:
            raise RuntimeError("Database connection failed.")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT OrderID, RoomNumber, Status, PlacedAt, UpdatedAt, Notes "
            "FROM Orders WHERE OrderID = ?", (order_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return (row[0], row[1], row[2], row[3], row[4], row[5])


def manage_orders_menu():
    """Staff: list live orders and advance them through the lifecycle."""
    while True:
        try:
            with db.get_connection() as conn:
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


def advance_order(order_id, action=None, actor=None):
    """Move an order to its next state, or cancel it. Returns the new status.

    `action` is "advance" or "cancel"; when it is None the console prompts for the choice.
    `actor` names the audit row and the in-room notification -- the console passes
    session.CURRENT_USER, the web passes the request principal, so a status change is
    attributed to whoever made it rather than to whichever console signed in last.
    """
    try:
        with db.get_connection() as conn:
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
            if action is None:
                choice = input(
                    f"Order {order_id} is '{current}'.\n"
                    f"  1. Advance to '{following}'\n"
                    f"  2. Cancel the order\n"
                    f"Choose: "
                ).strip()
                if choice == "2":
                    action = "cancel"
                elif choice == "1":
                    action = "advance"
                else:
                    logging.info("Invalid choice. Order unchanged.")
                    return current
            if action == "cancel":
                new_status = "Cancelled"
            elif action == "advance":
                new_status = following
            else:
                logging.info("Invalid action. Order unchanged.")
                return current
            cursor.execute(
                "UPDATE Orders SET Status = ?, UpdatedAt = ?, "
                "CompletedAt = CASE WHEN ? IN ('Completed', 'Cancelled') THEN ? ELSE CompletedAt END "
                "WHERE OrderID = ?",
                (new_status, datetime.now(), new_status, datetime.now(), order_id),
            )
            conn.commit()
        core.log_audit("UPDATE", "Order", order_id, f"{current} -> {new_status} (room {room_number})", user=actor)
        # Tell the guest in their room so the tracker reflects reality immediately.
        if room_number:
            try:
                with db.get_connection() as conn:
                    if conn is not None:
                        conn.cursor().execute(
                            "INSERT INTO Notifications (RoomNumber, Message, Channel, SentBy) "
                            "VALUES (?, ?, 'In-Room', ?)",
                            (room_number,
                             f"Order #{order_id} update: {ORDER_STATUS_HELP.get(new_status, new_status)}",
                             actor if actor is not None else session.CURRENT_USER),
                        )
                        conn.commit()
            except Exception:
                pass
        ui.success(f"Order {order_id} is now '{new_status}'.")
        return new_status
    except Exception as e:
        logging.error(f"Error updating order: {e}")
        return None


def manage_amenities_menu():
    """Admin: add, edit, retire and reorder hotel amenities."""
    while True:
        ui.pause()
        ui.show_menu("Manage Amenities", [
            "1. View Amenities",
            "2. Add Amenity",
            "3. Edit Amenity",
            "4. Retire / Reactivate Amenity",
            "5. Back",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            rows = core.get_amenities(active_only=False)
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
            with db.get_connection() as conn:
                if conn is None:
                    return
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO Amenities (Name, Description, DisplayOrder) "
                    "OUTPUT INSERTED.AmenityID VALUES (?, ?, COALESCE((SELECT MAX(DisplayOrder) + 1 FROM Amenities), 1))",
                    (name, details or None),
                )
                conn.commit()
            core.log_audit("CREATE", "Amenity", name, details or "")
            ui.success(f"Amenity '{name}' added.")
        elif choice == '3':
            rows = core.get_amenities(active_only=False)
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
            with db.get_connection() as conn:
                if conn is None:
                    return
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE Amenities SET Name = ?, Description = ? WHERE AmenityID = ?",
                    (name, details or None, int(raw)),
                )
                conn.commit()
            core.log_audit("UPDATE", "Amenity", int(raw), f"'{amenity[1]}' -> '{name}'")
            ui.success("Amenity updated.")
        elif choice == '4':
            raw = input("Amenity number to toggle: ").strip()
            if not raw.isdigit():
                continue
            with db.get_connection() as conn:
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
            core.log_audit("UPDATE", "Amenity", int(raw), "Active" if new_active else "Retired")
            ui.success(f"Amenity {'reactivated' if new_active else 'retired'}.")
        elif choice == '5':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()


def manage_promotions_menu():
    """Admin: add, edit and expire hotel promotions."""
    while True:
        ui.pause()
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
                with db.get_connection() as conn:
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
                with db.get_connection() as conn:
                    if conn is not None:
                        cursor = conn.cursor()
                        cursor.execute("UPDATE Promotions SET Active = 0 WHERE PromotionID = ?",
                                       (int(raw),))
                        conn.commit()
                core.log_audit("UPDATE", "Promotion", int(raw), f"'{promotion[1]}' ended")
                ui.success("Promotion ended.")
            else:
                title = input(f"Title (blank to keep '{promotion[1]}'): ").strip() or promotion[1]
                details = input(f"Details (blank to keep): ").strip() or promotion[2]
                code = input(f"Discount code (blank to keep '{promotion[3] or ''}'): ").strip() or promotion[3]
                with db.get_connection() as conn:
                    if conn is None:
                        return
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE Promotions SET Title = ?, Details = ?, DiscountCode = ? "
                        "WHERE PromotionID = ?",
                        (title, details or None, code or None, int(raw)),
                    )
                    conn.commit()
                core.log_audit("UPDATE", "Promotion", int(raw), f"'{promotion[1]}' -> '{title}'")
                ui.success("Promotion updated.")
        elif choice == '2':
            title = input("Promotion title: ").strip()
            if not title:
                logging.info("Promotion title cannot be blank.")
                continue
            details = input("Details: ").strip()
            code = input("Associated discount code (optional): ").strip()
            with db.get_connection() as conn:
                if conn is None:
                    return
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO Promotions (Title, Details, DiscountCode) "
                    "OUTPUT INSERTED.PromotionID VALUES (?, ?, ?)",
                    (title, details or None, code or None),
                )
                conn.commit()
            core.log_audit("CREATE", "Promotion", title, details or "")
            ui.success("Promotion added.")
        elif choice == '5':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()
