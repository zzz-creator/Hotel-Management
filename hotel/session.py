# type: ignore
"""Process-wide mutable session state.

Split out of main.py so a module can hold state without importing the
entry point. Tests patch these through the owning module, and main.py
forwards attribute reads here (module __getattr__), so `app.CURRENT_USER`
still reads the live value.
"""
# The operator signed in for the current session. Set by admin_login(); audit rows fall
# back to 'system' for guest-facing actions that have no authenticated operator.
CURRENT_USER = "system"
# The signed-in guest at the public booking desk, as a CustomerProfiles.CustomerID.
# `None` means nobody is logged in. Loyalty is keyed on this, NOT on the room number:
# a balance that belongs to a room gets inherited by whoever checks into that room next.
# Like CURRENT_USER it stays set across menus, but Sign Out on the Customer or Bookings
# menu clears it: the next visitor must log in fresh rather than inherit this session.
CURRENT_CUSTOMER = None


# False once a probe proves Reservations has no NightlyRate column (migration 022 not
# applied). None = unknown, or the column is there. Only the negative answer is cached:
# it is the one that saves a failing query per check-out, and a working probe is cheap.
_RESERVATIONS_CAPTURED_RATE_SUPPORT = None


_INVOICES_PREPAID_SUPPORT = None

# False once a query proves ReservationPayments has no per-row AppliedAmount (migration
# 020 not applied). None = the partial-credit path still works, or is untried. Only the
# negative answer is cached: it is the one that saves a failed query per check-out, and a
# successful partial query costs nothing to repeat.
_RESERVATION_PAYMENTS_PARTIAL_SUPPORT = None

# Last four digits of the card most recently accepted by process_credit_card(), so
# booking receipts and refunds can name the card without ever storing the full number.
# None means "no card on file".
LAST_CARD_DIGITS = None
