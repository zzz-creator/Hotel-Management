# type: ignore
"""booking_ledger: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
import random
from datetime import timedelta
from . import db
from . import session
from . import core

__all__ = [
    'BOOKING_REF_PREFIX',
    'BOOKING_DEPOSIT_NIGHTS',
    'PAYMENT_KIND_DEPOSIT',
    'PAYMENT_KIND_PREPAYMENT',
    'PAYMENT_KIND_REFUND',
    'PAYMENT_KIND_FORFEIT',
    'booking_quote',
    'booking_payment_options',
    'settle_with_prepayment',
    'refund_decision',
    'booking_refund_policy',
    'BOOKING_REF_WRITE_ATTEMPTS',
    '_write_booking_charge',
    'new_booking_ref',
    'booking_reference_exists',
    'BookingRefTaken',
    '_original_charge_card',
    'record_booking_payment',
    'get_booking_paid_total',
    'allocate_booking_credit',
    'get_outstanding_booking_credit',
    'apply_booking_credit',
    '_apply_credit_legacy',
    '_set_partial_credit_unsupported',
]




## =========================
# Booking Desk (public, no login)
# =========================
# A guest books a room by TYPE from the main menu: the app picks the first available
# room of that type and hands back the room number as their reference. Money taken up
# front lives in ReservationPayments (migration 018) and is credited against the
# check-out bill rather than posted to the folio, so it never inflates room revenue.
#
# The pure helpers below hold the money rules so they can be unit-tested without a
# database; the wizards around them only gather input and write rows.
BOOKING_REF_PREFIX = "BK"
BOOKING_DEPOSIT_NIGHTS = 1
# Booking payment kinds. These mirror the CK_ReservationPayments_Kind CHECK constraint.
PAYMENT_KIND_DEPOSIT = "Deposit"
PAYMENT_KIND_PREPAYMENT = "Prepayment"
PAYMENT_KIND_REFUND = "Refund"
PAYMENT_KIND_FORFEIT = "Forfeit"


def booking_quote(nights, nightly_rate, tax_rate):
    """Price a stay: the room component, its tax, and the total the guest will owe.

    Mirrors the check-out folio exactly -- the room group is taxed and the F&B group is
    not yet in play -- so a full prepayment here covers the room bill precisely.
    Negative inputs are clamped rather than rejected so a stray setting value can never
    produce a negative quote.
    """
    nights = max(0, int(nights or 0))
    rate = max(0.0, float(nightly_rate or 0.0))
    tax_rate = max(0.0, float(tax_rate or 0.0))
    subtotal = round(nights * rate, 2)
    tax_amount = round(subtotal * tax_rate, 2)
    return {
        "nights": nights,
        "nightly_rate": round(rate, 2),
        "tax_rate": tax_rate,
        "subtotal": subtotal,
        "tax": tax_amount,
        "total": round(subtotal + tax_amount, 2),
    }


def booking_payment_options(nights, nightly_rate, tax_rate):
    """Offer the deposit / pay-in-full choices for a quote, as (choice, label, kind, amount).

    A deposit is REQUIRED to secure the room: there is no pay-at-check-out option, so a
    confirmed booking is always backed by a ReservationPayments row. That row is also
    what makes the booking reference findable by view_my_booking()/cancel_booking(),
    which is why a no-prepayment path is not offered. The deposit is capped at the stay
    length so a one-night stay cannot be asked for a second night up front.
    """
    quote = booking_quote(nights, nightly_rate, tax_rate)
    options = []
    deposit_nights = min(BOOKING_DEPOSIT_NIGHTS, quote["nights"])
    # The `or` keeps the list non-empty for a $0 quote (a zero-rated room, or a
    # zero-night stay), which would otherwise make the wizard's minimum=1 /
    # maximum=0 prompt impossible to satisfy. The $0 row is still written, so the
    # booking stays findable by its reference.
    if deposit_nights > 0 or quote["total"] <= 0:
        deposit = booking_quote(deposit_nights, nightly_rate, tax_rate)
        remainder = round(quote["total"] - deposit["total"], 2)
        options.append((
            len(options) + 1,
            f"Pay a {deposit_nights}-night deposit of ${deposit['total']:,.2f} now "
            f"(then ${remainder:,.2f} at check-out)",
            PAYMENT_KIND_DEPOSIT,
            deposit["total"],
        ))
    if quote["total"] > 0:
        options.append((
            len(options) + 1,
            f"Pay in full now: ${quote['total']:,.2f}",
            PAYMENT_KIND_PREPAYMENT,
            quote["total"],
        ))
    return options


def settle_with_prepayment(total, credit):
    """Apply booking credit to a balance, never crediting more than is owed.

    The booking quote is an ESTIMATE in exactly one way: F&B is added at check-out. The room
    rate itself is captured at booking (022) and cannot move afterwards. A guest who prepaid
    against more nights than they stayed, or who ordered less than a full room service
    spend, can therefore end up with credit left over (reported as credit_unused for a later
    refund) or short (balance_due).
    """
    total = max(0.0, float(total or 0.0))
    credit = max(0.0, float(credit or 0.0))
    applied = round(min(credit, total), 2)
    return {
        "total": round(total, 2),
        "credit_available": round(credit, 2),
        "credit_applied": applied,
        "credit_unused": round(credit - applied, 2),
        "balance_due": round(total - applied, 2),
    }


def refund_decision(days_until_check_in, cutoff_days, amount_paid):
    """Decide what a cancellation owes the guest, as {action, kind, amount, reason}.

    `action` is 'refused' (the check-in date has arrived), 'none' (nothing was paid),
    'refund' (cancelling at least `cutoff_days` ahead) or 'forfeit' (inside the cutoff,
    so the deposit is kept). Both 'refund' and 'forfeit' write a NEGATIVE row so the
    stay's payments always net back to zero; the Kind is what distinguishes money
    returned from money kept.
    """
    days = int(days_until_check_in)
    cutoff = max(0, int(cutoff_days))
    paid = round(max(0.0, float(amount_paid or 0.0)), 2)
    if days <= 0:
        # 0 is the arrival date itself, not "still cancellable". Refusing here matters
        # because cancel_booking() DELETEs the reservation row: letting a guest cancel on
        # the day they arrive would wipe an in-house stay, mark the room Available while
        # they are in it, and leave check-out with no reservation to bill against.
        return {"action": "refused", "kind": None, "amount": 0.0,
                "reason": ("check-in is today or earlier, so this stay can no longer be "
                           "cancelled online")}
    if paid <= 0:
        return {"action": "none", "kind": None, "amount": 0.0,
                "reason": "no payment was taken for this booking"}
    if days >= cutoff:
        return {"action": "refund", "kind": PAYMENT_KIND_REFUND, "amount": -paid,
                "reason": f"cancelled {days} day(s) ahead, inside the {cutoff}-day free-cancellation window"}
    return {"action": "forfeit", "kind": PAYMENT_KIND_FORFEIT, "amount": -paid,
            "reason": f"cancelled only {days} day(s) ahead, inside the {cutoff}-day cutoff, so the deposit is forfeited"}


def booking_refund_policy(check_in, cutoff_days, amount_charged=None):
    """The guest-facing cancellation policy for a stay, as a sentence.

    Stating this at BOOKING time is the whole point: a deposit is non-refundable inside
    the cutoff, and a guest cannot be held to that unless they are told before they hand
    over a card. The wording must agree with `refund_decision()`, which is why the
    deadline is derived from the same `days >= cutoff` rule rather than restated by hand.

    Returns {'cutoff': int, 'deadline': date, 'summary': str}.
    """
    cutoff = max(0, int(cutoff_days))
    # A stay cannot be cancelled once check-in arrives (refund_decision() refuses
    # days_until <= 0), so the last refundable day is never later than the day before
    # arrival. max(cutoff, 1) keeps the promise honest at cutoff 0, where a naive
    # "cutoff days ahead" deadline would land ON the arrival date and offer a refund
    # that is then refused.
    refundable_days = max(cutoff, 1)
    deadline = check_in - timedelta(days=refundable_days)
    held = "the deposit" if amount_charged is None else f"the ${float(amount_charged):,.2f} paid"
    summary = (f"Cancel on or before {deadline} - at least {refundable_days} day(s) before "
               f"check-in - for a full refund. Cancel after that and {held} is forfeited. "
               f"From the check-in date onwards the booking cannot be cancelled online, so "
               f"please contact the front desk.")
    return {"cutoff": cutoff, "refundable_days": refundable_days,
            "deadline": deadline, "summary": summary}


BOOKING_REF_WRITE_ATTEMPTS = 5


def _write_booking_charge(conn, room_number, booking_ref, stay_check_in, kind, amount,
                          nights_covered, card_last4=None):
    """Write a booking's initial charge, retrying if the reference was taken. Returns the
    reference actually used, or None if the charge could not be recorded.

    A reference collision is a race with another guest, not a failure of the booking, so it
    is retried with a fresh reference rather than being reported to the guest. Migration
    021's filtered unique index is what makes the collision detectable: before it, the
    pre-flight probe in new_booking_ref() was the only defence and two sessions could both
    pass it.

    `card_last4` names the card the guest paid with; None keeps the console behaviour of
    reading `session.LAST_CARD_DIGITS` inside record_booking_payment().

    Retrying the INSERT inside the same transaction relies on SQL Server not aborting the
    transaction on a constraint violation (the default, with XACT_ABORT off). If the driver
    ever did abort it, the retry's own failure is caught below and reported as a hard
    failure, so the caller still rolls back rather than committing a partial booking.
    """
    for attempt in range(BOOKING_REF_WRITE_ATTEMPTS):
        ref = booking_ref if attempt == 0 else new_booking_ref()
        try:
            payment_id = record_booking_payment(room_number, ref, stay_check_in, kind,
                                                amount, nights_covered=nights_covered,
                                                conn=conn, card_last4=card_last4)
        except BookingRefTaken:
            logging.debug(f"Booking reference {ref} was taken concurrently; minting another.")
            continue
        if payment_id is None:
            return None
        return ref
    logging.error(f"Could not find a free booking reference in {BOOKING_REF_WRITE_ATTEMPTS} attempts.")
    return None


def new_booking_ref():
    """Generate a short guest-facing booking reference, e.g. 'BK-4F2A9C'."""
    while True:
        ref = f"{BOOKING_REF_PREFIX}-{random.randint(0x100000, 0xFFFFFF):06X}"
        if not booking_reference_exists(ref):
            return ref


def booking_reference_exists(ref):
    """True when the reference is already taken. Missing table (no 018) means free."""
    with db.get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM ReservationPayments WHERE BookingRef = ?", (ref,))
            return cursor.fetchone() is not None
        except Exception as e:
            logging.debug(f"Booking reference probe skipped ({type(e).__name__}: {e})")
            return False


class BookingRefTaken(Exception):
    """A booking reference was already taken by a concurrent booking.

    Raised instead of returning None so the caller can tell "this reference is gone, mint
    another and retry" apart from "the payment could not be recorded at all", which is a
    real failure the guest should be told about. The uniqueness itself is enforced by the
    filtered unique index in migration 021, not by any check here.
    """


def _original_charge_card(cursor, booking_ref):
    """The card a booking was originally charged to, for a reversal row.

    A Refund/Forfeit row must be attributed to the card that took the money, NOT to
    `LAST_CARD_DIGITS`. That global is the last card the PROCESS authorised, and a
    cancellation never prompts for a card, so it still holds whatever some earlier,
    unrelated guest paid with -- a refund row would then name a stranger's card. Reading it
    back from the booking's own initial charge is the only self-consistent answer, and it
    is what makes a refund traceable to the payment it reverses.

    Returns None when there is no such charge (a legacy row, or 018 not applied).
    """
    try:
        cursor.execute(
            "SELECT TOP 1 CardLast4 FROM ReservationPayments "
            "WHERE BookingRef = ? AND Kind IN ('Deposit', 'Prepayment') AND Amount > 0 "
            "ORDER BY PaymentID",
            (booking_ref,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logging.debug(f"Could not read the original card for {booking_ref}: "
                      f"{type(e).__name__}: {e}")
        return None


def record_booking_payment(room_number, booking_ref, stay_check_in, kind, amount,
                           nights_covered=None, notes=None, conn=None, card_last4=None):
    """Append one signed row to ReservationPayments. Returns the new PaymentID or None.

    Pass `conn` to join a caller's transaction (a declined card must not leave a
    half-written booking behind). `kind` CHECK-constrained, so an unknown kind is a bug
    we want to hear about rather than a silently ignored payment.

    Raises BookingRefTaken when the reference collides with another booking's initial
    charge, which migration 021's filtered unique index makes a real possibility rather
    than a theoretical one.

    `conn` is not optional in practice: both callers in this module pass one
    (`_write_booking_charge`, `cancel_booking`), so the shipping path is the `conn is not
    None` branch at the bottom, which never opens a connection of its own and therefore
    never touches `main.get_connection()`. On that path the IntegrityError arrives
    intact, `_is_duplicate_key_error()` recognises it, and BookingRefTaken is raised and
    retried. `tests/verify_e2e.py` asserts this against a real server.

    The `conn is None` branch has no caller in main.py. It now behaves like the rest of
    the app: a statement failure propagates with pyodbc's own type, so a collision is
    classified here and raised, rather than being logged and answered with a silent
    `None`. It behaved differently only while main.py carried a second, broken copy of
    `get_connection()` -- see AGENTS.md section 7.
    """
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        logging.error(f"Refusing to record a non-numeric booking payment: {amount!r}")
        return None

    def _write(cursor):
        # A reversal is attributed to the card that took the original money; a charge is
        # attributed to the card just authorised.
        if kind in (PAYMENT_KIND_REFUND, PAYMENT_KIND_FORFEIT):
            digits = _original_charge_card(cursor, booking_ref)
        elif card_last4 is not None:
            # The web path passes the request's digits explicitly so a charge row never
            # names a card some other request authorised (PLAN-web-api.md phase 3).
            digits = card_last4
        else:
            # Console callers rely on the process-global 'last card authorised'.
            digits = session.LAST_CARD_DIGITS or None
        cursor.execute(
            "INSERT INTO ReservationPayments "
            "(RoomNumber, BookingRef, StayCheckIn, Kind, Amount, NightsCovered, CardLast4, Notes) "
            "OUTPUT INSERTED.PaymentID "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (room_number, booking_ref, stay_check_in, kind, amount,
             nights_covered, digits, notes),
        )
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    try:
        if conn is not None:
            return _write(conn.cursor())
        with db.get_connection() as own:
            if own is None:
                return None
            payment_id = _write(own.cursor())
            own.commit()
            return payment_id
    except Exception as e:
        if core._is_duplicate_key_error(e):
            raise BookingRefTaken(booking_ref) from e
        # A missing table means migration 018 is not applied yet.
        logging.error(f"Could not record booking payment: {e}")
        return None


def get_booking_paid_total(room_number, stay_check_in):
    """Net money still held for a stay: charges positive, refunds/forfeits negative.

    Only counts rows for THIS stay (same room + same check-in date), because a room is
    re-let after every check-out and an older booking's money must never be credited to
    the next guest's bill.
    """
    with db.get_connection() as conn:
        if conn is None:
            return 0.0
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ISNULL(SUM(Amount), 0) FROM ReservationPayments "
                "WHERE RoomNumber = ? AND StayCheckIn = ?",
                (room_number, stay_check_in),
            )
            row = cursor.fetchone()
            return round(float(row[0] or 0.0), 2) if row else 0.0
        except Exception as e:
            # Missing table means migration 018 is not applied: not an error, this runs
            # on every check-out, so it must not spam the log.
            logging.debug(f"Booking payments unavailable ({type(e).__name__}: {e})")
            return 0.0


def allocate_booking_credit(payments, amount):
    """Split `amount` of credit across a stay's unconsumed payment rows.

    Pure, so the split is unit-testable without a database (tests/test_booking.py).

    `payments` is [(PaymentID, remaining_amount)] in the order the credit should be
    consumed -- oldest row first -- where `remaining_amount` is Amount - already applied.
    Returns [(PaymentID, consumed)] with `consumed > 0`, in the same order, summing to at
    most `amount`.

    A row is allowed to be only PARTLY consumed, and the return value is what makes that
    representable: the pre-020 schema could only set a boolean IsApplied per row, so
    crediting a bill smaller than a prepayment either lied (marking $300 applied against a
    $250 bill) or stranded the surplus with no way to say how much was used. Consuming
    oldest-first also means the surplus sits in the guest's most recent payment rather
    than being spread across several rows.
    """
    remaining = round(max(0.0, float(amount or 0.0)), 2)
    allocation = []
    for payment_id, available in payments or []:
        if remaining <= 0:
            break
        available = round(max(0.0, float(available or 0.0)), 2)
        if available <= 0:
            continue
        consumed = round(min(available, remaining), 2)
        if consumed > 0:
            allocation.append((payment_id, consumed))
            remaining = round(remaining - consumed, 2)
    return allocation


def get_outstanding_booking_credit(room_number, stay_check_in, conn=None):
    """Unspent booking credit for a stay, ready to deduct from the check-out bill.

    Counts only what each row has NOT already given to an invoice
    (`Amount - AppliedAmount`), so a retried check-out after a declined card still sees
    the full credit, and a partly-consumed row contributes only its remainder.

    Rows are signed (refunds/forfeits are negative), so a returned deposit reduces the
    credit held; the total is floored at zero because a stay owes no negative credit.
    """
    def _read_partial(cursor):
        cursor.execute(
            "SELECT ISNULL(SUM(Amount - AppliedAmount), 0) FROM ReservationPayments "
            "WHERE RoomNumber = ? AND StayCheckIn = ?",
            (room_number, stay_check_in),
        )
        row = cursor.fetchone()
        return round(max(0.0, float(row[0] or 0.0)), 2) if row else 0.0

    def _read_legacy(cursor):
        # Pre-020: a row is consumed whole or not at all, so IsApplied = 0 is the
        # whole remainder. Used until AppliedAmount exists (see AGENTS.md).
        cursor.execute(
            "SELECT ISNULL(SUM(Amount), 0) FROM ReservationPayments "
            "WHERE RoomNumber = ? AND StayCheckIn = ? AND IsApplied = 0",
            (room_number, stay_check_in),
        )
        row = cursor.fetchone()
        return round(max(0.0, float(row[0] or 0.0)), 2) if row else 0.0

    def _read(cursor):
        if session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT is False:
            return _read_legacy(cursor)
        try:
            return _read_partial(cursor)
        except Exception as e:
            _set_partial_credit_unsupported(e)
            return _read_legacy(cursor)

    if conn is not None:
        return _read(conn.cursor())
    with db.get_connection() as own:
        if own is None:
            return 0.0
        try:
            return _read(own.cursor())
        except Exception as e:
            # See get_booking_paid_total(): an unapplied 018 is expected, not an error.
            logging.debug(f"Booking credit unavailable ({type(e).__name__}: {e})")
            return 0.0


def apply_booking_credit(room_number, stay_check_in, credit, invoice_id, conn):
    """Consume `credit` of a stay's unspent booking payments, recording what was used.

    Runs inside the caller's transaction alongside the invoice insert, so the credit and
    the invoice either both land or neither does. No-ops when there is nothing to apply.

    Each row records the amount it gave to this invoice in `AppliedAmount` and flips
    `IsApplied` only once it is fully consumed, so a partly-used row is neither reported
    as spent nor silently dropped. Returns the number of rows touched.
    """
    if credit <= 0 or invoice_id is None:
        return 0
    try:
        cursor = conn.cursor()
        if session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT is False:
            return _apply_credit_legacy(cursor, room_number, stay_check_in, invoice_id)
        try:
            cursor.execute(
                "SELECT PaymentID, Amount - AppliedAmount FROM ReservationPayments "
                "WHERE RoomNumber = ? AND StayCheckIn = ? AND Amount - AppliedAmount > 0 "
                "ORDER BY PaymentID",
                (room_number, stay_check_in),
            )
            rows = [(int(r[0]), float(r[1] or 0.0)) for r in cursor.fetchall()]
        except Exception as e:
            _set_partial_credit_unsupported(e)
            return _apply_credit_legacy(cursor, room_number, stay_check_in, invoice_id)

        touched = 0
        for payment_id, consumed in allocate_booking_credit(rows, credit):
            cursor.execute(
                "UPDATE ReservationPayments SET "
                "  AppliedAmount = ISNULL(AppliedAmount, 0) + ?, "
                "  IsApplied = CASE WHEN ISNULL(AppliedAmount, 0) + ? >= Amount "
                "                   THEN 1 ELSE IsApplied END, "
                "  AppliedToInvoiceID = ? "
                "WHERE PaymentID = ?",
                (consumed, consumed, invoice_id, payment_id),
            )
            touched += 1
        return touched
    except Exception as e:
        logging.error(f"Error applying booking credit to invoice {invoice_id}: {e}")
        return 0


def _apply_credit_legacy(cursor, room_number, stay_check_in, invoice_id):
    """Pre-020 fallback: a row can only be consumed whole, so mark the lot.

    Kept so check-out keeps working before migration 020 is applied. It over-claims on
    the rare bill-smaller-than-prepayment case (see allocate_booking_credit), which is
    strictly better than crediting the same payment twice.
    """
    cursor.execute(
        "UPDATE ReservationPayments SET IsApplied = 1, AppliedToInvoiceID = ? "
        "WHERE RoomNumber = ? AND StayCheckIn = ? AND IsApplied = 0",
        (invoice_id, room_number, stay_check_in),
    )
    return max(0, int(cursor.rowcount or 0))


def _set_partial_credit_unsupported(error):
    """Latch the pre-020 booking-credit shape after a failed partial-credit query.

    Only latches on a "that column or table isn't there" failure: an unapplied 020, or a
    wholly unapplied 018. Any other error is left alone rather than cached as a permanent
    capability answer -- a deadlock or a dropped connection must not pin the app to the
    legacy path for the rest of the session.
    """
    message = str(error).lower()
    missing_column = "invalid column" in message and "appliedamount" in message
    missing_table = "invalid object name" in message and "reservationpayments" in message
    if missing_column or missing_table:
        if session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT is None:
            logging.debug("ReservationPayments.AppliedAmount missing (migration 020 not "
                          "applied yet) -- consuming booking credit whole-row.")
        session._RESERVATION_PAYMENTS_PARTIAL_SUPPORT = False
