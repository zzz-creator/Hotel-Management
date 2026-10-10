# type: ignore
"""Entry point and compatibility facade.

The application used to live here in one ~9,000-line file. It is now split by domain --
rooms, items, loyalty, payments, keycards, reservations, booking_ledger, billing, orders,
notifications, bookings, customer, concierge, admin, onboarding -- on top of core (config,
settings, audit, shared helpers) and session (process-wide mutable state). See
`plans/PLAN-split-main-py.md` for the layout and the rules the modules follow.

This file keeps three jobs:

1. **The facade.** `import main as app` still reaches every moved name (tests and
   tests/verify_e2e.py are written against `app.<name>`), including the exception classes
   (`app.BookingRefTaken`, `app.LoyaltyRedemptionError`) and the private helpers.
2. **`get_connection`.** One definition, aliased from db.py. Do NOT redefine it here: the
   old copy wrapped its yield in `except Exception` and re-yielded, which is illegal in a
   generator, so every database error surfaced as "generator didn't stop after throw()"
   with no server message. AGENTS.md section 5 relies on this staying an alias.
3. **The entry point.** `handle_cli_args()` and `main()` below, and the `if __name__`
   guard.

Mutable state (CURRENT_USER, CURRENT_CUSTOMER, LAST_CARD_DIGITS and the three cached
capability probes) lives in session.py and is forwarded by `__getattr__`, so reading
`app.CURRENT_USER` always sees the live value rather than a stale import-time copy.
Writers use `session.X = ...`; no module imports main.
"""
import argparse
import logging
import sys
import time

from hotel import db
from hotel import reports
from hotel import ui

# Named imports for the few owners main() itself calls. The star imports below bind the
# same names, but a star import binds a value once, so `patch_main("run_first_run_onboarding")`
# (which patches onboarding.run_first_run_onboarding) would miss main()'s copy. Calling
# through the module keeps main() patchable like every other caller.
from hotel import core
from hotel import rooms
from hotel import loyalty
from hotel import onboarding
from hotel import customer
from hotel import admin
from hotel import bookings

# Importing core is also what reads config.ini and calls db.init(CONNECTION_STRING).
from hotel.core import *            # noqa: F401,F403
from hotel.rooms import *           # noqa: F401,F403
from hotel.items import *           # noqa: F401,F403
from hotel.loyalty import *         # noqa: F401,F403
from hotel.payments import *        # noqa: F401,F403
from hotel.keycards import *        # noqa: F401,F403
from hotel.reservations import *    # noqa: F401,F403
from hotel.booking_ledger import *  # noqa: F401,F403
from hotel.billing import *         # noqa: F401,F403
from hotel.orders import *          # noqa: F401,F403
from hotel.notifications import *   # noqa: F401,F403
from hotel.bookings import *        # noqa: F401,F403
from hotel.customer import *        # noqa: F401,F403
from hotel.concierge import *       # noqa: F401,F403
from hotel.admin import *           # noqa: F401,F403
from hotel.onboarding import *      # noqa: F401,F403

# Set up logging
#logging.basicConfig(filename='hotel_management.log', level=logging.DEBUG, format='%(asctime)s:%(levelname)s:%(message)s')
class CustomFormatter(logging.Formatter):
    def format(self, record):
        if record.levelno == logging.INFO:
            return record.getMessage()
        return f"{record.levelname}: {record.getMessage()}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# Apply the custom formatter to all handlers
for handler in logger.handlers:
    handler.setFormatter(CustomFormatter())

## =========================
# Database Connection & Utilities
## =========================
# One definition of this, not two. main.py used to carry its own copy that wrapped the
# `yield` in `except Exception` and then yielded again, which is illegal in a generator:
# every error raised inside a `with get_connection()` block reached the caller as
# `RuntimeError: generator didn't stop after throw()` instead of pyodbc's real message.
# db.get_connection() has always had the correct shape -- it returns None only when the
# *connect* fails, and lets a statement failure propagate with its own type and message.
# AGENTS.md section 5 says so; this copy is the one that broke it, and nothing else in
# the app ever needed a second definition.
#
# `db.init(CONNECTION_STRING)` in core.py is what keeps the two in step, and it is the
# seam tests/verify_e2e.py re-points at a disposable database. Do NOT redefine this
# function here. If a caller needs different failure behaviour, wrap the body in its own
# try/except -- that is the only correct place to swallow a database error.
get_connection = db.get_connection


def __getattr__(name):
    """Forward reads of session-owned state to session (PEP 562).

    The star imports above deliberately exclude session.py: they bind a value once, and
    the session state changes at runtime. `app.CURRENT_CUSTOMER` must read the live value,
    so an unknown attribute is looked up in session before failing.
    """
    from hotel import session
    try:
        return getattr(session, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def handle_cli_args():
    parser = argparse.ArgumentParser(description="Hotel Management Reporting CLI")
    parser.add_argument("--report", choices=sorted(reports.REPORTS), help="Report type to export")
    parser.add_argument("--format", choices=["csv"], default="csv", help="Export format (only csv is supported)")
    parser.add_argument("--room", help="Room number: reports the whole history of the guest who held that stay")
    parser.add_argument("--customer", help="Customer ID, email, or 'LastName FirstName' for loyalty statements")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD): revenue and occupancy reports")
    parser.add_argument("--end", help="End date (YYYY-MM-DD): revenue and occupancy reports")
    parser.add_argument("--floor", help="Floor number to limit the housekeeping report")
    parser.add_argument("--date", help="Board date (YYYY-MM-DD) for the housekeeping report; "
                                       "defaults to the business date")
    parser.add_argument("--booking-ref", help="Limit the booking_ledger report to one reference")
    args, _ = parser.parse_known_args()
    if not args.report:
        return False
    try:
        if args.report == 'loyalty':
            paths = reports.export_loyalty_statements(export_format=args.format,
                                                     room_number=args.room,
                                                     customer=args.customer)
        elif args.report == 'revenue':
            paths = reports.export_revenue(export_format=args.format, start_date=args.start, end_date=args.end)
        elif args.report == 'occupancy':
            paths = reports.export_occupancy(export_format=args.format, start_date=args.start,
                                            end_date=args.end)
        elif args.report == 'housekeeping':
            paths = reports.export_housekeeping(export_format=args.format, floor=args.floor,
                                                on_date=args.date)
        elif args.report == 'booking_ledger':
            paths = reports.export_booking_ledger(export_format=args.format,
                                                  booking_ref=args.booking_ref)
        else:
            paths = reports.REPORTS[args.report](export_format=args.format)
        for path in paths:
            logging.info(f"Exported report: {path}")
        return True
    except Exception as e:
        logging.error(f"Report export failed: {e}")
        return False


## =========================
# Main Entry Point
## =========================
def main():
    # A fresh checkout has no config.ini, so before anything that opens a connection
    # (the loyalty-table probe and the onboarding marker check included) give the
    # operator a chance to type in the database settings and get a connection at all.
    core._ensure_database_config()
    # Ensure loyalty DB objects exist if loyalty is enabled
    try:
        loyalty.ensure_loyalty_tables()
    except Exception:
        pass
    # Ensure the room-type rate table is populated so nightly room charges can be posted.
    try:
        rooms.ensure_room_types_seeded()
    except Exception:
        pass

    if handle_cli_args():
        return

    # First run only: a database nobody has set up yet has no accounts, so there is nothing
    # to log into and the main menu would be a wall of failed logins. After this it never
    # runs again -- the marker is what stops it, not the state of the Users table.
    if not onboarding.onboarding_completed():
        onboarding.run_first_run_onboarding()

    while True:
        ui.pause()
        ui.clear_screen()
        ui.show_menu(f"Welcome to {core.get_hotel_name()}!", [
            "1. Customer",
            "2. Admin",
            "3. Bookings",
            "4. Exit",
        ], subtitle=customer.customer_session_label())
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            customer.customer_panel()
        elif choice == '2':
            admin.admin_panel()
        elif choice == '3':
            bookings.booking_panel()
        elif choice == '4':
            logging.info("Exiting Program")
            time.sleep(2)
            sys.exit(0)
        else:
            logging.info("Invalid choice. Please try again.")


if __name__ == "__main__":
    main()
