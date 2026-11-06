# type: ignore
import logging
from hotel import session
from hotel import bookings
from hotel import customer


def run_bookings(shell):
    items = [
        ("1. Book a Room", bookings.book_room),
        ("2. View My Booking", bookings.view_my_booking),
        ("3. Cancel My Booking", bookings.cancel_booking),
        ("4. Sign Out", lambda: (setattr(session, "CURRENT_CUSTOMER", None), logging.info("You have been signed out."))),
    ]
    shell.show_buttons("Bookings", items)
    return
