# type: ignore
import logging
from hotel import session
from hotel import conference
from hotel import customer as _c


def run_conference(shell):
    try:
        _c.customer_login(allow_register=False)
    except Exception:
        pass
    cid = session.CURRENT_CUSTOMER
    if cid is None:
        logging.info("Please sign in to book a conference room.")
        shell.pause()
        return
    items = [
        ("1. Browse rooms", conference.conference_rooms_panel),
        ("2. Book a conference room", lambda: conference.book_conference_room(customer_id=cid)),
        ("3. My conference bookings", lambda: conference.view_my_conference_bookings(cid)),
    ]
    shell.show_buttons("Conference Rooms", items)
    return
