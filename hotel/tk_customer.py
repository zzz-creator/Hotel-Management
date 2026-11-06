# type: ignore
import logging
from hotel import customer, reservations, orders, billing, loyalty, concierge
from hotel import keycards, notifications, core, ui


def run_customer(shell):
    if not customer.customer_login(allow_register=False):
        logging.info("A booking account is required to use the Customer menu. Returning to the main menu.")
        shell.pause()
        return
    session = customer.customer_session_label()

    def join_loyalty():
        if not core.get_loyalty_enabled():
            logging.info("Loyalty Program is not enabled. Contact administration to enable it.")
            return
        room_number = ui.ask_text("Enter your room number to join/verify loyalty account: ").strip()
        if not room_number:
            logging.info("Invalid room number.")
            return
        if loyalty.create_loyalty_account_if_missing(room_number):
            points = loyalty.get_points_by_room(room_number)
            logging.info(f"Loyalty account ready for room {room_number}. Current points: {points}.")
        else:
            logging.info("Failed to create or verify loyalty account. Please contact front desk.")

    items = [
        ("1. Check In", reservations.check_in),
        ("2. Place Order", orders.order_item),
        ("3. Check Out", billing.check_out),
        ("4. View Amenities", orders.view_amenities),
        ("5. Provide Feedback", orders.provide_feedback),
        ("6. View Promotions", orders.view_promotions),
        ("7. Join Loyalty Program", join_loyalty),
        ("8. Track Order Status", orders.track_order_status),
        ("9. Contact Concierge", concierge.contact_concierge),
        ("10. View My Concierge Requests", concierge.view_my_concierge_requests),
        ("11. View My Loyalty Tier & Perks", customer.view_my_loyalty_status),
        ("12. Print My Invoice", customer.print_my_invoice),
        ("13. View My History", customer.my_history),
        ("14. My Key Card", keycards.view_my_key_card),
        ("15. My Notifications", notifications.view_notifications_for_room),
    ]

    def show():
        btns = [(label, action) for label, action in items]
        btns.append(("Sign Out", lambda: None))
        shell.show_buttons("Customer Menu - " + session, btns)

    show()
    return
