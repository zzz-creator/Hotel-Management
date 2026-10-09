# type: ignore
"""payments: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
from datetime import datetime
import session

__all__ = [
    'luhn_check',
    'process_credit_card',
    'validate_card',
    'validate_expiration_date',
]



## =========================
# Billing & Payment
## =========================
def luhn_check(card_number):
    """Validate credit card number using Luhn's algorithm.

    Returns False for anything that is not a run of digits, rather than raising: a guest
    who mistypes their card must get "Invalid credit card number", not a traceback.
    """
    text = str(card_number or "").strip()
    if not text.isdigit():
        return False

    def digits_of(n):
        return [int(d) for d in str(n)]

    digits = digits_of(text)
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]

    checksum = sum(odd_digits)

    for d in even_digits:
        checksum += sum(digits_of(d * 2))

    return checksum % 10 == 0


def process_credit_card(amount):
    """Simulate credit card processing for the exact `amount` to charge.

    Callers pass the fully tax-inclusive total (see bill_room_transactions(),
    order_item(), and the booking deposit/prepayment flows), so no tax is added here.
    """
    amount = float(amount)
    logging.info(f"Processing credit card payment of ${amount:.2f}...")

    # Get credit card details
    card_number = input("Enter your credit card number: ").strip()

    # Validate credit card number using Luhn's algorithm
    if not luhn_check(card_number) or card_number == "":
        logging.info("Invalid credit card number. Payment Cancelled.")
        session.LAST_CARD_DIGITS = None
        return False

    expiration_date = input("Enter your credit card expiration date (MM/YYYY): ").strip()

    # Validate expiration date
    if not validate_expiration_date(expiration_date):
        logging.info("Invalid or expired credit card expiration date. Payment Cancelled.")
        session.LAST_CARD_DIGITS = None
        return False

    cvv = input("Enter your credit card CVV: ").strip()

    # Validate CVV length
    if len(cvv) != 3:
        logging.info("Invalid CVV. Payment Cancelled.")
        session.LAST_CARD_DIGITS = None
        return False
    # Remember only the last four digits, never the PAN or CVV.
    session.LAST_CARD_DIGITS = card_number[-4:]
    return True

def validate_expiration_date(expiration_date):

    """Validate if the credit card expiration date is valid and not expired."""
    try:
        # Parse expiration date
        exp_month, exp_year = expiration_date.split('/')
        exp_month = int(exp_month)
        exp_year = int(exp_year)

        # Get current date
        current_date = datetime.now()
        current_year = current_date.year
        current_month = current_date.month

        # Check if the expiration date is in the future. The month must be a real
        # month, otherwise "00/2030" would slip through the year check.
        if not 1 <= exp_month <= 12:
            return False

        if exp_year > current_year or (exp_year == current_year and exp_month >= current_month):
            return True
        else:
            return False
    except (ValueError, IndexError):
        # Return False if the date format is invalid
        return False


def validate_card(card_number, expiration_date, cvv=None):
    """Non-interactive card gate for the web path (PLAN-web-api.md phase 3).

    Mirrors process_credit_card()'s acceptance criteria -- Luhn, a real non-expired
    month, a 3-digit CVV, in that order -- but returns (ok, reason, last4) instead of
    prompting, and **never** writes session.LAST_CARD_DIGITS. That global is the
    console's process-wide convenience; under concurrent web users guest A's digits
    must never be attributed to guest B, so the caller owns the returned digits and
    passes them into record_booking_payment(card_last4=...).

    `reason` is None on success and a console-identical message on failure, so the
    endpoint layer can hand it back to the caller unchanged. `cvv=None` skips the CVV
    check for a caller that does not collect one. Unlike process_credit_card(), the
    CVV here must be all digits, not merely three characters long.
    """
    if not luhn_check(card_number):
        return False, "Invalid credit card number.", None
    if not validate_expiration_date(expiration_date):
        return False, "Invalid or expired credit card expiration date.", None
    if cvv is not None and (not str(cvv).isdigit() or len(str(cvv)) != 3):
        return False, "Invalid CVV.", None
    return True, None, str(card_number).strip()[-4:]
