# type: ignore
"""bookings: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
from datetime import timedelta
import db
import ui
import session
import booking_ledger
import core
import customer
import payments
import reservations
import rooms

__all__ = [
    'book_room',
    '_find_booking',
    '_own_booking',
    'view_my_booking',
    'cancel_booking',
    'booking_panel',
]




def book_room():
    """Public booking wizard: sign in, choose a room TYPE, dates, and pay up front.

    The guest never picks a room number -- the app assigns the first available room of
    the chosen type and returns it as their reference. The QUOTE is an estimate, because
    the guest has not chosen a room yet and F&B is not in play, but the RATE is not: it is
    captured onto the reservation when the booking commits (migration 022) and check-out
    bills that number, so an admin rate change between booking and arrival cannot move
    the total. The credit is still capped at what is owed, so a bill can come in under
    what was taken (see §4 of docs/BOOKING.md).
    """
    logging.info("\n--- Book a Room ---")
    # A login is required, not optional: the booking is tied to a profile, which is what
    # gives the guest their loyalty balance across stays and lets the desk see their
    # bookings later.
    customer_id = customer.customer_login()
    if not customer_id:
        logging.info("A booking account is required to book a room.")
        return
    room_types = rooms.get_room_types()
    if not room_types:
        logging.info("No room types are currently available for booking.")
        return
    ui.show_table("Available Room Types", ["Choice", "Room Type", "Nightly Rate"],
                  [(i, rt, f"${rate:,.2f}") for i, (rt, rate) in enumerate(room_types, 1)])
    choice = ui.ask_number("Enter the number of the room type you want",
                           minimum=1, maximum=len(room_types))
    if choice is None:
        return
    room_type, nightly_rate = room_types[choice - 1]

    today = core.business_date()
    check_in = ui.ask_date("Check-in date (YYYY-MM-DD)", default=str(today))
    if check_in is None:
        return
    check_out = ui.ask_date("Check-out date (YYYY-MM-DD)", default=str(check_in + timedelta(days=1)))
    if check_out is None:
        return
    if check_out <= check_in:
        logging.info("Check-out date must be after check-in date.")
        return
    if check_in < today:
        logging.info("Check-in date cannot be in the past.")
        return

    nights = core.stay_nights(check_in, check_out)
    if nights <= 0:
        logging.info("That stay is at least one night long.")
        return

    # The name on the reservation is the ACCOUNT holder's, not a second free-text
    # identity: the reservation's CustomerID is what links the stay to the login, and
    # two names on one account would let a stay's loyalty be read under a different name
    # than the one the guest signs in with.
    profile = customer._customer_profile(customer_id)
    if profile and profile[0] and profile[1]:
        last_name, first_name = profile[0], profile[1]
        logging.info(f"Booking as {first_name} {last_name}"
                     + (f" <{profile[2]}>." if profile[2] else "."))
    else:
        last_name = input("Enter your last name: ").strip()
        first_name = input("Enter your first name: ").strip()
        if not last_name or not first_name:
            logging.info("Both first and last name are required to hold a reservation.")
            return

    quote = booking_ledger.booking_quote(nights, nightly_rate, core.get_tax_rate())
    ui.box("Booking Estimate",
           f"Room type: {room_type}\n"
           f"Nightly rate: ${quote['nightly_rate']:,.2f}\n"
           f"Nights: {nights}\n"
           f"Room subtotal: ${quote['subtotal']:,.2f}\n"
           f"Tax ({quote['tax_rate']*100:.0f}%): ${quote['tax']:,.2f}\n"
           f"Estimated total: ${quote['total']:,.2f}\n"
           f"This rate is locked in when you book. Any food, beverage or other charges are "
           f"billed separately at check-out.")
    logging.info(f"Any food, beverage or other charges are billed separately at check-out.")

    # Pick the room BEFORE taking payment, so a sold-out type fails fast with no card
    # details requested. A handful of candidates is enough to survive another desk
    # grabbing the same room between the availability read and the insert.
    candidates = reservations.search_availability(check_in, check_out, room_type=room_type, limit=5)
    if not candidates:
        logging.info(f"No {room_type} rooms are available for {check_in} to {check_out}.")
        return

    # Say what the cancellation terms ARE before taking any money, not after. A guest
    # who is only shown the policy on the confirmation screen has already committed.
    refund_cutoff = core.get_booking_refund_cutoff_days()
    ui.box("Cancellation Policy", booking_ledger.booking_refund_policy(check_in, refund_cutoff)["summary"])

    options = booking_ledger.booking_payment_options(nights, nightly_rate, quote["tax_rate"])
    ui.show_menu("Payment Options", [f"{c}. {label}" for c, label, _kind, _amt in options])
    pay_choice = ui.ask_number("Choose a payment option", minimum=1, maximum=len(options))
    if pay_choice is None:
        return
    _choice, _label, pay_kind, pay_amount = options[pay_choice - 1]

    booking_ref = booking_ledger.new_booking_ref()
    claimed = None
    reasons = []
    with db.get_connection() as conn:
        if conn is None:
            logging.info("Database connection failed. No booking was made.")
            return
        for room_number, _rt, _status in candidates:
            try:
                ok, reason = reservations._book_reservation_in_conn(
                    conn, room_number, last_name, first_name, check_in, check_out,
                    customer_id=customer_id, room_type=room_type)
            except Exception as e:
                # Most likely the room was claimed by someone else first: Reservations
                # is keyed on RoomNumber, so the insert collides. Try the next candidate.
                reasons.append(f"room {room_number}: {e}")
                continue
            if ok:
                claimed = room_number
                break
            reasons.append(reason)
        if claimed is None:
            conn.rollback()
            logging.info("We could not hold a room for those dates. Please try different dates.")
            for r in reasons[:3]:
                logging.info(f"  - {r}")
            return
        # Money first, reservation second: a declined card must not leave a held room.
        # The payment row is written even when the amount is $0 (a zero-rated room), so
        # every confirmed booking is findable by its reference.
        if pay_kind:
            if pay_amount > 0:
                logging.info(f"Total to charge now: ${pay_amount:,.2f}")
                if not payments.process_credit_card(pay_amount):
                    conn.rollback()
                    logging.info("Payment was declined, so the room has not been held. Nothing was charged.")
                    return
            else:
                logging.info("The deposit comes to $0.00, so no card is needed.")
            booking_ref = booking_ledger._write_booking_charge(
                conn, claimed, booking_ref, check_in, pay_kind, pay_amount,
                nights if pay_kind == booking_ledger.PAYMENT_KIND_PREPAYMENT
                else min(booking_ledger.BOOKING_DEPOSIT_NIGHTS, nights))
            if booking_ref is None:
                conn.rollback()
                logging.info("Could not record the payment, so the booking was cancelled. Nothing was charged.")
                return
        try:
            conn.commit()
        except Exception as e:
            conn.rollback()
            logging.error(f"Could not save the booking: {e}")
            return

    core.log_audit("CREATE", "Reservation", claimed,
              f"Public booking {booking_ref}: {last_name} {first_name}, customer {customer_id}, "
              f"{room_type}, {check_in} to {check_out}, prepay={pay_kind or 'none'}")
    rooms.upsert_room_if_missing(claimed, room_type=room_type)

    # Read the LOCKED rate back off the row rather than reusing the quote's: the quote
    # was priced before the transaction opened, and an admin could have re-priced the
    # category in the gap. This is the number check-out will bill, so the confirmation
    # shows the one that is actually going to be charged.
    locked_rate = rooms.stay_nightly_rate(rooms.get_captured_nightly_rate(claimed), nightly_rate)
    if locked_rate != round(float(nightly_rate), 2):
        logging.info(f"Note: the {room_type} rate is now ${locked_rate:,.2f} per night, "
                     f"which is the rate locked in for this booking.")

    lines = [
        f"Booking reference: {booking_ref}",
        f"Room: {claimed} ({room_type})",
        f"Guest: {first_name} {last_name}",
        f"Check-in: {check_in}",
        f"Check-out: {check_out} ({nights} night(s))",
        f"Estimated total: ${quote['total']:,.2f}",
    ]
    if pay_kind:
        lines.append(f"Paid now: ${pay_amount:,.2f} ({pay_kind})")
        remaining = booking_ledger.settle_with_prepayment(quote["total"], pay_amount)
        if remaining["balance_due"] > 0:
            lines.append(f"Estimated balance at check-out: ${remaining['balance_due']:,.2f}")
        elif remaining["credit_unused"] > 0:
            lines.append(f"Estimated credit to refund if unused: ${remaining['credit_unused']:,.2f}")
        if session.LAST_CARD_DIGITS:
            lines.append(f"Card ending {session.LAST_CARD_DIGITS}")
    else:
        lines.append(f"Payment: ${quote['total']:,.2f} due at check-out")
    lines.append(f"Your ${locked_rate:,.2f} per night is the rate you will be billed at check-out.")
    lines.append("")
    lines.append("Cancellation policy")
    lines.append(booking_ledger.booking_refund_policy(check_in, refund_cutoff, amount_charged=pay_amount)["summary"])
    ui.box("Booking Confirmed", "\n".join(lines))
    logging.info("Please quote your booking reference at the front desk.")


def _find_booking(booking_ref, customer_id=None):
    """Look up a live reservation by booking reference, via that stay's payment rows.

    Returns (room_number, check_in, check_out, last_name, first_name, customer_id) or
    None. `customer_id` is the owner of the stay, and is None for a pre-019 or
    front-desk booking.

    When `customer_id` is given, the lookup is RESTRICTED to that guest's own stays. A
    booking reference is only 8 characters and is shown on the guest's own screen, but
    an 8-character code is brute-forceable, and without this check anybody who guessed
    one could read a stranger's room and cancel their reservation. A stay with no linked
    owner can never match, so a legacy booking has to be handled at the front desk.
    """
    # Read the clock BEFORE opening the connection: business_date() opens its own, and a
    # nested connection on top of a live cursor is an extra round trip that has no
    # business happening inside this query.
    today = core.business_date()
    with db.get_connection() as conn:
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            if customer_id is not None:
                cursor.execute(
                    "SELECT TOP 1 r.RoomNumber, r.CheckInDate, r.CheckOutDate, r.LastName, "
                    "r.FirstName, r.CustomerID "
                    "FROM Reservations r "
                    "JOIN ReservationPayments p ON p.RoomNumber = r.RoomNumber "
                    "  AND p.StayCheckIn = r.CheckInDate "
                    "WHERE p.BookingRef = ? AND r.CheckOutDate >= ? AND r.CustomerID = ? "
                    "ORDER BY r.CheckInDate DESC",
                    (booking_ref, today, customer_id),
                )
            else:
                cursor.execute(
                    "SELECT TOP 1 r.RoomNumber, r.CheckInDate, r.CheckOutDate, r.LastName, "
                    "r.FirstName, r.CustomerID "
                    "FROM Reservations r "
                    "JOIN ReservationPayments p ON p.RoomNumber = r.RoomNumber "
                    "  AND p.StayCheckIn = r.CheckInDate "
                    "WHERE p.BookingRef = ? AND r.CheckOutDate >= ? "
                    "ORDER BY r.CheckInDate DESC",
                    (booking_ref, today),
                )
            row = cursor.fetchone()
            if not row:
                return None
            return (row.RoomNumber, row.CheckInDate, row.CheckOutDate,
                    row.LastName, row.FirstName, row.CustomerID)
        except Exception as e:
            logging.error(f"Error looking up booking {booking_ref}: {e}")
            return None


def _own_booking(booking_ref, customer_id):
    """Resolve a booking reference for a signed-in guest, or None with a reason shown.

    The "not found" message deliberately does not say whether the reference exists but
    belongs to somebody else: telling a caller that their guess was a real code owned by
    another guest would confirm the guess and leak that guest's existence.
    """
    if not customer_id:
        logging.info("Please sign in to your booking account first.")
        return None
    found = _find_booking(booking_ref, customer_id)
    if found is None:
        logging.info(f"We could not find an upcoming booking with reference {booking_ref} on your account.")
        logging.info(f"Check the reference from your confirmation (it starts with '{booking_ledger.BOOKING_REF_PREFIX}-').")
        logging.info("Bookings made at the front desk, or before online booking existed, are handled by staff.")
    return found


def view_my_booking():
    """Show a guest their booking and what they have already paid.

    Requires a sign-in, and only ever shows bookings on the signed-in guest's own
    account.
    """
    customer_id = customer.customer_login()
    if not customer_id:
        return
    booking_ref = input(f"Enter your booking reference (the code starting with "
                        f"'{booking_ledger.BOOKING_REF_PREFIX}-', NOT the room number): ").strip().upper()
    if not booking_ref:
        return
    found = _own_booking(booking_ref, customer_id)
    if found is None:
        return
    room_number, check_in, check_out, last_name, first_name, _owner = found
    paid = booking_ledger.get_booking_paid_total(room_number, check_in)
    credit = booking_ledger.get_outstanding_booking_credit(room_number, check_in)
    nights = core.stay_nights(check_in, check_out)
    ui.show_table(
        f"Booking {booking_ref}",
        ["Detail", "Value"],
        [
            ("Guest", f"{first_name} {last_name}"),
            ("Room", room_number),
            ("Check-in", str(check_in)),
            ("Check-out", f"{check_out} ({nights} night(s))"),
            ("Paid to date", f"${paid:,.2f}"),
            ("Credit towards check-out", f"${credit:,.2f}"),
        ],
    )
    cutoff = core.get_booking_refund_cutoff_days()
    days_until = (check_in - core.business_date()).days
    decision = booking_ledger.refund_decision(days_until, cutoff, paid)
    if decision["action"] == "refund":
        logging.info(f"Free cancellation until {cutoff} day(s) before check-in: cancelling now would refund ${-decision['amount']:,.2f}.")
    elif decision["action"] == "forfeit":
        logging.info(f"Free cancellation until {cutoff} day(s) before check-in: cancelling now would forfeit the ${-decision['amount']:,.2f} paid.")


def cancel_booking():
    """Cancel a public booking and settle the money under the refund policy.

    The reservation row is released so the room returns to availability, and the
    money is settled with a single signed ReservationPayments row: a negative 'Refund'
    when cancelling inside the free window, or a negative 'Forfeit' when the deposit is
    kept. The guest must type the reference back to confirm.

    Requires a sign-in, and the reference must belong to the signed-in guest: a booking
    reference is short enough to be guessed, and cancelling somebody else's reservation
    would both destroy their stay and pay out their deposit to them.
    """
    customer_id = customer.customer_login()
    if not customer_id:
        return
    booking_ref = input(f"Enter your booking reference (the code starting with "
                        f"'{booking_ledger.BOOKING_REF_PREFIX}-', NOT the room number): ").strip().upper()
    if not booking_ref:
        return
    found = _own_booking(booking_ref, customer_id)
    if found is None:
        return
    room_number, check_in, check_out, last_name, first_name, _owner = found
    days_until = (check_in - core.business_date()).days
    paid = booking_ledger.get_booking_paid_total(room_number, check_in)
    decision = booking_ledger.refund_decision(days_until, core.get_booking_refund_cutoff_days(), paid)

    if decision["action"] == "refused":
        logging.info(f"Booking {booking_ref} cannot be cancelled: {decision['reason']}.")
        logging.info("Please speak to the front desk.")
        return

    logging.info(f"Booking {booking_ref}: room {room_number}, {first_name} {last_name}, "
                 f"check-in {check_in}, check-out {check_out}.")
    if decision["action"] == "refund":
        logging.info(f"Cancellation outcome: full refund of ${-decision['amount']:,.2f} to the original card.")
    elif decision["action"] == "forfeit":
        logging.info(f"Cancellation outcome: the ${-decision['amount']:,.2f} paid is forfeited "
                     f"({decision['reason']}).")
    else:
        logging.info("Nothing was paid for this booking, so there is no refund to process.")
    if not ui.ask_confirmation("Cancel this booking?"):
        logging.info("Booking left unchanged.")
        return
    confirm = input(f"Type {booking_ref} to confirm cancellation: ").strip().upper()
    if confirm != booking_ref:
        logging.info("Reference did not match. Booking left unchanged.")
        return

    with db.get_connection() as conn:
        if conn is None:
            logging.info("Database connection failed. The booking was not cancelled.")
            return
        try:
            cursor = conn.cursor()
            # Guard on the same dates AND the same owner we quoted, so neither a desk-side
            # re-book nor a change of owner can be cancelled out from under the next guest.
            cursor.execute(
                "DELETE FROM Reservations WHERE RoomNumber = ? AND CheckInDate = ? "
                "AND CheckOutDate = ? AND CustomerID = ?",
                (room_number, check_in, check_out, customer_id),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                logging.info("This booking changed before it could be cancelled. Please start again.")
                return
            # Deliberately NOT archived: a cancelled booking never happened, and an
            # archive row would make search_availability() treat the room as occupied
            # for those dates. The money rows below are the cancellation's history.
            if decision["action"] in ("refund", "forfeit"):
                booking_ledger.record_booking_payment(room_number, booking_ref, check_in,
                                       decision["kind"], decision["amount"],
                                       notes=decision["reason"], conn=conn)
            conn.commit()
        except Exception as e:
            logging.error(f"Error cancelling booking {booking_ref}: {e}")
            return
    core.log_audit("CANCEL", "Reservation", room_number,
              f"Cancelled public booking {booking_ref} ({first_name} {last_name}, from {check_in}); "
              f"outcome={decision['action']} amount={decision['amount']}")
    logging.info(f"Booking {booking_ref} cancelled. Room {room_number} is available again.")
    if decision["action"] == "refund":
        logging.info(f"${-decision['amount']:,.2f} will be refunded to the original card in 5-10 business days.")
    elif decision["action"] == "forfeit":
        logging.info("The deposit has been retained as the cancellation fee.")


def booking_panel():
    """Public booking menu, on the main screen.

    No staff login, but each action does require a guest booking account: "Book", "View"
    and "Cancel" all operate on the signed-in guest's own account, and the sign-in is
    cached in CURRENT_CUSTOMER for the rest of the session.
    """
    while True:
        ui.pause()
        ui.show_menu("Bookings", [
            "1. Book a Room",
            "2. View My Booking",
            "3. Cancel My Booking",
            "4. Sign Out",
        ], subtitle=customer.customer_session_label())
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            book_room()
        elif choice == '2':
            view_my_booking()
        elif choice == '3':
            cancel_booking()
        elif choice == '4':
            session.CURRENT_CUSTOMER = None  # Sign out: the next guest must log in fresh
            logging.info("You have been signed out.")
            break
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()
