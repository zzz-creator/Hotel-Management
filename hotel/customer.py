# type: ignore
"""customer: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see PLAN-split-main-py.md)."""
import logging
from . import db
from . import ui
from . import session as _mod_session
from . import billing
from . import concierge
from . import core
from . import keycards
from . import loyalty
from . import notifications
from . import orders
from . import reservations
from . import rooms

__all__ = [
    'register_customer',
    'customer_login',
    'authenticate_customer',
    '_customer_profile',
    'customer_session_label',
    'print_my_invoice',
    'view_my_loyalty_status',
    'load_customer_history',
    'show_customer_history',
    'my_history',
    'search_customer_profiles',
    '_pick_customer_account',
    'create_guest_account',
    '_update_account_field',
    'edit_guest_account',
    'reset_guest_password',
    'delete_guest_account',
    'link_stay_to_guest_account',
    'customer_panel',
]




def register_customer(email, last_name, first_name, password, phone=None):
    """Create a booking-desk account. Returns the new CustomerID, or None.

    The unique filtered index on Email (migration 019) is what actually prevents two
    accounts sharing an address; it is a real database guarantee, not a check-then-insert
    race. That matters because the login lookup is by email, so a duplicate would make
    the account ambiguous.

    `phone` is optional contact detail; the booking desk never asks for it, the Admin
    Panel's Create Guest Account does.
    """
    with db.get_connection() as conn:
        if conn is None:
            return None
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CustomerID FROM CustomerProfiles WHERE Email = ?", (email,))
            if cursor.fetchone():
                logging.info("An account already exists for that email. Please sign in instead.")
                return None
            cursor.execute(
                "INSERT INTO CustomerProfiles (LastName, FirstName, Email, Phone, Password) "
                "OUTPUT INSERTED.CustomerID VALUES (?, ?, ?, ?, ?)",
                (last_name, first_name, email, phone, password),
            )
            new_row = cursor.fetchone()
            conn.commit()
            core.log_audit("CREATE", "CustomerProfile", email, "Booking account registered")
            return int(new_row[0]) if new_row and new_row[0] is not None else None
        except Exception as e:
            # Most likely a duplicate email losing the race against a concurrent signup.
            logging.error(f"Could not create account: {e}")
            return None


def authenticate_customer(email, password=None):
    """Non-interactive guest credential check (the login service).

    The twin the web path calls and the console adapter delegates to (PLAN-web-api.md
    phase 2). Returns a status dict:

      {"status": "ok", "customer_id": int, "first_name": str}  -- password matched; the
                                                                   LOGIN audit is written
                                                                   naming `email` as the
                                                                   actor (the guest is the
                                                                   person logging in).
      {"status": "wrong_password"}                             -- password did not match.
      {"status": "unknown"}                                    -- no profile with that
                                                                   email.
      {"status": "no_password"}                                -- account exists but has
                                                                   never had a password
                                                                   set (front-desk
                                                                   account).
      {"status": "unavailable"}                                -- database unreachable;
                                                                   a missing Password
                                                                   column means 019 is not
                                                                   applied yet.

    With `password=None` (the console adapter's discovery call) the service returns the
    same facts without comparing anything: a known account with a password is
    {"status": "ready", "customer_id": ..., "first_name": ...}, so the adapter can decide
    whether to prompt for a password, offer registration, or refuse before asking for
    anything. The service never touches `session.CURRENT_CUSTOMER` -- the console adapter
    sets the process global on success, the web path reads the customer out of its cookie.
    """
    try:
        with db.get_connection() as conn:
            if conn is None:
                return {"status": "unavailable"}
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CustomerID, FirstName, LastName, Password FROM CustomerProfiles "
                "WHERE Email = ?", (email,))
            row = cursor.fetchone()
    except Exception as e:
        # A missing Password column means 019 is not applied yet.
        logging.error(f"Booking login is unavailable: {e}")
        return {"status": "unavailable"}

    if row is None:
        return {"status": "unknown"}
    customer_id, first_name, _last_name, stored = row.CustomerID, row.FirstName, row.LastName, row.Password
    if not stored:
        return {"status": "no_password"}
    if password is None:
        return {"status": "ready", "customer_id": int(customer_id), "first_name": first_name}
    if password == stored:
        core.log_audit("LOGIN", "CustomerProfile", email, "Booking desk sign-in", user=email)
        return {"status": "ok", "customer_id": int(customer_id), "first_name": first_name}
    return {"status": "wrong_password"}


def customer_login(allow_register=True):
    """Sign a guest in at the booking desk, registering them on first use.

    Returns the CustomerID on success, or None if the guest gave up. A blank email or
    password is never accepted, because both are the only handle the account has: there
    is no "guest" row without them, and a booking always has an owner.

    `allow_register=False` is the Customer menu's gate: an unknown email is REFUSED with
    "book a room first" rather than registered, and returns None immediately so the panel
    can bounce the visitor to the main menu. The in-house panel must not sign anyone up --
    a front-desk guest's account is created by staff, not typed into a public menu by the
    guest. The default stays True because first-use registration is load-bearing at the
    Bookings desk: a guest with no account cannot book (docs/BOOKING.md §1).
    """
    if _mod_session.CURRENT_CUSTOMER is not None:
        return _mod_session.CURRENT_CUSTOMER
    for attempt in range(1, core.get_customer_login_max_attempts() + 1):
        email = input("Email address: ").strip()
        if not email:
            logging.info("Your email address is required to book a room.")
            continue
        # The credential work lives in authenticate_customer(); this adapter first asks
        # what KIND of account this email is (unknown / no password / ready) so it knows
        # whether to prompt for a password, register the guest, or refuse -- without
        # asking for a password it does not yet know it needs.
        known = authenticate_customer(email)
        status = known.get("status")
        if status == "unavailable":
            ui.error("Booking accounts are not available right now. Please contact the front desk.")
            return None
        if status == "unknown":
            if not allow_register:
                logging.info(
                    f"No booking account was found for {email}. Book a room first "
                    "(main menu -> 3. Bookings), or ask the front desk to create your "
                    "account -- accounts are not created from this menu."
                )
                return None
            # First visit. Create the account rather than sending the guest away: a guest
            # with no account cannot book, and a guest who cannot book has no way to be
            # remembered for their next stay.
            last_name = input("New here -- enter your last name: ").strip()
            first_name = input("Enter your first name: ").strip()
            if not last_name or not first_name:
                logging.info("Both first and last name are required to create an account.")
                continue
            password = input("Choose a password: ").strip()
            if not password:
                logging.info("A password is required to create an account.")
                continue
            customer_id = register_customer(email, last_name, first_name, password)
            if customer_id:
                _mod_session.CURRENT_CUSTOMER = customer_id
                core.log_audit("LOGIN", "CustomerProfile", email, "Booking account created at sign-up", user=email)
                logging.info(f"Welcome, {first_name}. Your booking account is ready.")
                return customer_id
            continue
        if status == "no_password":
            logging.info("That account has no password yet. Please contact the front desk to set one.")
            return None
        # A known account with a password: prompt for it and verify.
        password = input("Password: ").strip()
        check = authenticate_customer(email, password)
        if check.get("status") == "ok":
            _mod_session.CURRENT_CUSTOMER = check["customer_id"]
            logging.info(f"Welcome back, {check['first_name']}.")
            return _mod_session.CURRENT_CUSTOMER
        remaining = core.get_customer_login_max_attempts() - attempt
        if remaining > 0:
            logging.info(f"Wrong password. {remaining} attempt(s) left.")
    logging.info("Too many failed attempts. Please try again later, or contact the front desk "
                 "and we can help with the booking.")
    return None


def _customer_profile(customer_id):
    """Return (last_name, first_name, email) for a CustomerID, or None if it is gone."""
    if not customer_id:
        return None
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT LastName, FirstName, Email FROM CustomerProfiles WHERE CustomerID = ?",
                (customer_id,))
            row = cursor.fetchone()
            return (row.LastName, row.FirstName, row.Email) if row else None
    except Exception as e:
        logging.debug(f"Could not read customer profile {customer_id}: {e}")
        return None


def customer_session_label():
    """A "Signed in as ..." line for the menu subtitle, or "Not signed in".

    Read through _customer_profile() so every panel shows the identity a shared
    console is actually holding. A profile that has since been deleted (or an
    unreachable database) degrades to the bare id rather than raising: this is
    display for a menu, not a gate -- the gates are customer_login() and
    validate_room().
    """
    if _mod_session.CURRENT_CUSTOMER is None:
        return "Not signed in"
    profile = _customer_profile(_mod_session.CURRENT_CUSTOMER)
    if not profile:
        return f"Signed in as guest #{_mod_session.CURRENT_CUSTOMER}"
    last_name, first_name, email = profile
    name = " ".join(p for p in (first_name, last_name) if p)
    if email:
        return f"Signed in as {name} ({email})"
    if name:
        return f"Signed in as {name}"
    return f"Signed in as guest #{_mod_session.CURRENT_CUSTOMER}"


def print_my_invoice():
    """Customer: view and print the invoices for their room."""
    room_number, first_name = core.validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify your last name, first name, and room number. Please try again.")
        return
    invoices = billing.list_invoices_for_room(room_number)
    if not invoices:
        logging.info("No invoices found for your room.")
        return
    ui.show_table(
        f"My Invoices - Room {room_number}",
        ["Invoice #", "Room", "Date", "Total", "Paid"],
        [(inv.InvoiceID, inv.RoomNumber, inv.InvoiceDate,
          f"${float(inv.TotalAmount):.2f}", f"${float(inv.AmountPaid):.2f}") for inv in invoices],
    )
    raw = input("Enter invoice number to print (blank to cancel): ").strip()
    if raw.isdigit():
        billing.print_invoice(int(raw))


def view_my_loyalty_status():
    """Customer: show current loyalty tier, perks, multiplier, discount, and progress to next tier."""
    if not core.get_loyalty_enabled():
        logging.info("Loyalty program is not enabled. Contact administration to enable it.")
        return
    room_number, first_name = core.validate_room()
    if not room_number:
        logging.info("Could not verify your room.")
        return
    loyalty.create_loyalty_account_if_missing(room_number)
    details = loyalty.get_tier_details_by_room(room_number)
    if not details:
        logging.info("Could not load loyalty tier details. Please contact front desk.")
        return
    points = loyalty.get_points_by_room(room_number)
    lifetime = loyalty.get_lifetime_points_by_room(room_number)
    # The balance belongs to the guest, not the room, so the guest is named here.
    # A room number is guessable and may already have been re-let to somebody else,
    # which is exactly how the wrong person would be shown the wrong balance.
    rows = [
        ("Guest", first_name or "-"),
        ("Room", room_number),
        ("Lifetime Points", str(lifetime)),
        ("Available Points", str(points)),
        ("Current Tier", details["tier"]),
        ("Room Category", rooms.get_room_type(room_number)),
        ("Points Multiplier", f"x{details['points_multiplier']:.2f}"),
        ("Points per Night", f"{core.get_loyalty_points_per_night() * rooms.get_room_type_multiplier(rooms.get_room_type(room_number)) * details['points_multiplier']:.0f} (base {core.get_loyalty_points_per_night()} x category x tier)"),
        ("Tier Discount", f"{details['discount_percent']:.0f}% off room-service bills"),
        ("Perks", details["perks"] if details.get("perks") else "None"),
    ]
    tiers = loyalty._tiers_from_db() or core.DEFAULT_TIERS
    for i, (name, _min_pts, _mult, _disc, _perks) in enumerate(tiers):
        if name == details["tier"]:
            if i + 1 < len(tiers) and lifetime < tiers[i + 1][1]:
                rows.append(("Points to Next Tier", f"{tiers[i + 1][1] - lifetime} pts to {tiers[i + 1][0]}"))
            else:
                rows.append(("Tier Status", "Highest tier reached - enjoy the perks!"))
            break
    ui.show_table("My Loyalty Status", ["Field", "Value"], rows)


def load_customer_history(last_name, first_name):
    """Profile row + stay list + invoice list for a guest. Used by both the
    customer "My History" view and the admin/manager profiles lookup.

    Stays and invoices are fetched separately (an invoice is linked to a room
    number, and a stay may have several invoices) so stays are never duplicated.
    """
    profile = None
    stays = []
    invoices = []
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None, [], []
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CustomerID, LastName, FirstName, Email, Phone, Preferences "
                "FROM CustomerProfiles WHERE LastName = ? AND FirstName = ? ORDER BY CustomerID DESC",
                (last_name, first_name),
            )
            profile = cursor.fetchone()
            cursor.execute(
                "SELECT RoomNumber, CheckInDate, CheckOutDate, "
                "  DATEDIFF(day, CheckInDate, CheckOutDate) AS Nights "
                "FROM Reservations WHERE LastName = ? AND FirstName = ? "
                "ORDER BY CheckInDate DESC",
                (last_name, first_name),
            )
            stays = cursor.fetchall()
            cursor.execute(
                "SELECT i.RoomNumber, i.InvoiceID, i.InvoiceDate, i.TotalAmount, i.AmountPaid "
                "FROM Invoices i WHERE EXISTS ("
                "  SELECT 1 FROM Reservations r WHERE r.RoomNumber = i.RoomNumber "
                "  AND r.LastName = ? AND r.FirstName = ?) "
                "ORDER BY i.InvoiceDate DESC",
                (last_name, first_name),
            )
            invoices = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error loading customer history: {e}")
    return profile, stays, invoices


def show_customer_history(profile, stays, invoices, profile_title="Customer Profile", history_title="Stay History", summary_title="Stay Summary"):
    """Render a guest's profile box, stay list, invoice list, and summary."""
    if profile:
        ui.show_table(profile_title, ["Field", "Value"], [
            ("Customer ID", profile.CustomerID),
            ("Name", f"{profile.FirstName} {profile.LastName}"),
            ("Email", profile.Email or "-"),
            ("Phone", profile.Phone or "-"),
            ("Preferences", profile.Preferences or "-"),
        ])
    else:
        logging.info("No saved customer profile on file for this booking (contact details are captured at check-in).")

    if not stays:
        logging.info("No stay history found.")
        return

    ui.show_table(
        history_title,
        ["Room", "Check-In", "Check-Out", "Nights"],
        [
            (
                s.RoomNumber,
                s.CheckInDate,
                s.CheckOutDate,
                s.Nights,
            )
            for s in stays
        ],
    )
    if invoices:
        ui.show_table(
            f"{history_title} - Invoices",
            ["Room", "Invoice #", "Invoice Date", "Total", "Paid"],
            [
                (
                    inv.RoomNumber,
                    inv.InvoiceID,
                    inv.InvoiceDate.strftime("%Y-%m-%d") if inv.InvoiceDate else "-",
                    f"${float(inv.TotalAmount):.2f}",
                    f"${float(inv.AmountPaid):.2f}",
                )
                for inv in invoices
            ],
        )
    total_invoiced = sum(float(i.TotalAmount) for i in invoices if i.TotalAmount is not None)
    ui.show_table(summary_title, ["Field", "Value"], [
        ("Total Stays", len(stays)),
        ("Total Invoices", len(invoices)),
        ("Total Invoiced", f"${total_invoiced:.2f}"),
    ])


def my_history():
    """Customer view: saved profile contact details joined to stays and invoices."""
    room_number, first_name = core.validate_room()
    if room_number is None or first_name is None:
        logging.info("Could not verify your last name, first name, and room number. Please try again.")
        return
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT LastName, FirstName FROM Reservations WHERE RoomNumber = ?",
                (room_number,),
            )
            res = cursor.fetchone()
            if not res:
                logging.info("Reservation not found for that room.")
                return
            last_name = res.LastName or ""
            guest_first = res.FirstName or first_name
    except Exception as e:
        logging.error(f"Error loading customer history: {e}")
        return
    profile, stays, invoices = load_customer_history(last_name, guest_first)
    show_customer_history(
        profile,
        stays,
        invoices,
        profile_title="My Customer Profile",
        history_title="My Stay History",
        summary_title="My Stay Summary",
    )


def search_customer_profiles():
    """Admin/manager: search CustomerProfiles and drill into a guest's history."""
    term = input("Search by last name, first name, email, or phone: ").strip()
    if not term:
        logging.info("Search term required.")
        return
    # Treat the search term literally: escape LIKE wildcards so '%'/'_' don't broaden the match.
    esc = term.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    pattern = f"%{esc}%"
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT CustomerID, LastName, FirstName, Email, Phone "
                "FROM CustomerProfiles "
                "WHERE LastName LIKE ? OR FirstName LIKE ? OR Email LIKE ? OR Phone LIKE ? "
                "ORDER BY LastName, FirstName",
                (pattern, pattern, pattern, pattern),
            )
            rows = cursor.fetchall()
    except Exception as e:
        logging.error(f"Error searching customer profiles: {e}")
        return
    if not rows:
        logging.info("No matching customer profiles found.")
        return
    ui.show_table(
        "Matching Customer Profiles",
        ["#", "Customer ID", "Last Name", "First Name", "Email", "Phone"],
        [(i + 1, r.CustomerID, r.LastName, r.FirstName, r.Email or "-", r.Phone or "-") for i, r in enumerate(rows)],
    )
    raw = input("Enter the row number to view details and history (blank to cancel): ").strip()
    if not raw.isdigit():
        return
    idx = int(raw)
    if not (1 <= idx <= len(rows)):
        logging.info("Invalid selection.")
        return
    selected = rows[idx - 1]
    profile, stays, invoices = load_customer_history(selected.LastName, selected.FirstName)
    show_customer_history(
        profile,
        stays,
        invoices,
        profile_title=f"Customer Profile - {selected.FirstName} {selected.LastName}",
        history_title=f"Stay History - {selected.FirstName} {selected.LastName}",
        summary_title=f"Stay Summary - {selected.FirstName} {selected.LastName}",
    )


def _pick_customer_account(prompt="Search guest account by full name or email: "):
    """Admin: resolve a guest account to its profile row for an account action.

    Returns a row with CustomerID, LastName, FirstName, Email, Phone -- or None.
    Matching is EXACT (an email, or "Last First"), following _pick_loyalty_customer():
    a surname alone must never be enough to choose the person an action applies to,
    because these actions change or delete a real account. Multiple matches are
    offered as a pick list, so a shared name cannot silently hit the first row.
    """
    query = input(prompt).strip()
    if not query:
        logging.info("No account specified.")
        return None
    try:
        with db.get_connection() as conn:
            if conn is None:
                return None
            cursor = conn.cursor()
            cursor.execute(
                "SELECT TOP 10 CustomerID, LastName, FirstName, Email, Phone "
                "FROM CustomerProfiles "
                "WHERE Email = ? OR LastName + ' ' + FirstName = ? "
                "OR (LastName = ? AND FirstName = ?) ORDER BY CustomerID DESC",
                (query, query, query, query),
            )
            matches = cursor.fetchall()
        if not matches:
            logging.info(
                f"No guest account found for '{query}'. If the guest has none, create "
                "one first (Accounts -> Create Guest Account); if the stay predates "
                "accounts, it may have no profile at all."
            )
            return None
        if len(matches) == 1:
            return matches[0]
        ui.show_table("Matching guest accounts", ["#", "ID", "Guest", "Email", "Phone"],
                      [(i + 1, r.CustomerID, f"{r.LastName}, {r.FirstName}",
                        r.Email or "-", r.Phone or "-") for i, r in enumerate(matches)])
        raw = input("Enter the row number of the right account (blank to cancel): ").strip()
        if not raw.isdigit():
            return None
        idx = int(raw)
        if not (1 <= idx <= len(matches)):
            logging.info("Invalid selection.")
            return None
        return matches[idx - 1]
    except Exception as e:
        logging.error(f"Error finding guest account: {e}")
        return None


def create_guest_account():
    """Admin/front desk: create a booking account for a guest who has none.

    This is the counterpart to the Customer menu's gate: that menu refuses unknown
    emails and tells the guest to ask the front desk, and this is where the desk
    says yes. Lengths are checked against the column widths because an over-long
    value is a pyodbc truncation error, and a mistyped prompt must produce a
    message, not a traceback.
    """
    last_name = input("Last name: ").strip()
    first_name = input("First name: ").strip()
    email = input("Email address (the guest signs in with this): ").strip()
    phone = input("Phone number (optional): ").strip()
    password = input("Password: ").strip()
    if not last_name or not first_name:
        logging.info("Both a last name and a first name are required.")
        return
    if not email:
        logging.info("An email address is required -- it is what the guest signs in with.")
        return
    if not password:
        logging.info("A password is required.")
        return
    for label, value, limit in (("last name", last_name, 50), ("first name", first_name, 50),
                                ("email", email, 100), ("phone", phone, 20),
                                ("password", password, 100)):
        if len(value) > limit:
            logging.info(f"That {label} is too long (maximum {limit} characters). Please shorten it.")
            return
    customer_id = register_customer(email, last_name, first_name, password, phone=phone or None)
    if customer_id:
        logging.info(
            f"Account created for {first_name} {last_name}. They can now sign in at "
            "the Customer menu with that email and password."
        )


def _update_account_field(account, field, limit):
    """Write one editable contact field, refusing duplicates and over-long values.

    `field` is one of the literals "Email", "Phone", "Preferences" from
    edit_guest_account's menu, never user input, so it is safe to interpolate.
    """
    value = input(f"New {field.lower()} (blank to cancel): ").strip()
    if not value:
        logging.info("No change made.")
        return
    if len(value) > limit:
        logging.info(f"That is too long for {field} (maximum {limit} characters).")
        return
    customer_id = int(account.CustomerID)
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            if field == "Email":
                cursor.execute(
                    "SELECT 1 FROM CustomerProfiles WHERE Email = ? AND CustomerID <> ?",
                    (value, customer_id),
                )
                if cursor.fetchone():
                    # The filtered unique index would reject it anyway; say so in a
                    # sentence rather than handing the operator a constraint error.
                    logging.info("Another account already uses that email address.")
                    return
            old_value = getattr(account, field, None)
            cursor.execute(
                f"UPDATE CustomerProfiles SET {field} = ? WHERE CustomerID = ?",
                (value, customer_id),
            )
            conn.commit()
        # Email is the login handle; everything else is contact detail. The audit
        # row carries the values so a front-desk mistake is recoverable.
        core.log_audit("UPDATE", "CustomerProfile", str(customer_id), f"{field} changed",
                  old_value=old_value, new_value=value)
        logging.info(f"{field} updated.")
    except Exception as e:
        logging.error(f"Could not update {field.lower()}: {e}")


def edit_guest_account():
    """Admin: change the contact details on a guest account.

    Names are deliberately NOT editable here: stay history and the admin lookup
    are keyed on LastName + FirstName (load_customer_history()), so a rename would
    orphan every past stay. Correct a typed name at the reservation instead.
    """
    account = _pick_customer_account()
    if not account:
        return
    while True:
        ui.pause()
        ui.show_menu(f"Edit Account #{account.CustomerID} - {account.FirstName} {account.LastName}", [
            "1. Change email",
            "2. Change phone",
            "3. Change preferences",
            "4. Back",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            _update_account_field(account, "Email", 100)
        elif choice == '2':
            _update_account_field(account, "Phone", 20)
        elif choice == '3':
            _update_account_field(account, "Preferences", 255)
        elif choice == '4':
            return
        else:
            logging.info("Invalid choice. Please try again.")


def reset_guest_password():
    """Admin: set a new sign-in password on a guest account."""
    account = _pick_customer_account()
    if not account:
        return
    password = input(f"New password for {account.FirstName} {account.LastName}: ").strip()
    if not password:
        logging.info("A password is required.")
        return
    if len(password) > 100:
        logging.info("That password is too long (maximum 100 characters).")
        return
    customer_id = int(account.CustomerID)
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE CustomerProfiles SET Password = ? WHERE CustomerID = ?",
                (password, customer_id),
            )
            conn.commit()
        # The new value stays out of the audit row: plaintext storage is a
        # documented design choice, but the audit trail is still a report.
        core.log_audit("UPDATE", "CustomerProfile", str(customer_id), "Password reset")
        logging.info("Password reset. The guest can sign in with the new password immediately.")
    except Exception as e:
        logging.error(f"Could not reset the password: {e}")


def delete_guest_account():
    """Admin: delete a guest account -- only when it holds no history.

    Stays, loyalty accounts and loyalty transactions all reference CustomerProfiles
    with plain NO ACTION foreign keys, so a profile with any history cannot be
    deleted anyway; the pre-check turns that database error into a sentence, and
    refuses before asking for the master override when there is nothing it could
    delete. The override requirement is the same one every destructive admin
    action carries (AGENTS.md §3).
    """
    account = _pick_customer_account()
    if not account:
        return
    customer_id = int(account.CustomerID)
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT "
                " (SELECT COUNT(*) FROM Reservations WHERE CustomerID = ?), "
                " (SELECT COUNT(*) FROM LoyaltyAccounts WHERE CustomerID = ?), "
                " (SELECT COUNT(*) FROM LoyaltyTransactions WHERE CustomerID = ?)",
                (customer_id, customer_id, customer_id),
            )
            stays, loyalty_accounts, ledger = cursor.fetchone()
    except Exception as e:
        # Without the counts the delete is a guess: a missing loyalty table (001 not
        # applied) must refuse rather than proceed blind.
        logging.info(
            f"Could not verify this account's history, so nothing was deleted ({e})."
        )
        return
    if stays or loyalty_accounts or ledger:
        logging.info(
            f"Refusing: this account holds history -- {stays} stay(s), "
            f"{loyalty_accounts} loyalty account(s), {ledger} loyalty transaction(s). "
            "Deleting it would destroy booking and points history that reports read."
        )
        return
    if not core.require_master_override(prompt="Master override required to delete a guest account: "):
        return
    confirm = account.Email or f"{account.LastName}, {account.FirstName}"
    typed = input(f"Type '{confirm}' to confirm deletion: ").strip()
    if typed != confirm:
        logging.info("Confirmation did not match. Nothing was deleted.")
        return
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("DELETE FROM CustomerProfiles WHERE CustomerID = ?", (customer_id,))
            conn.commit()
        core.log_audit("DELETE", "CustomerProfile", str(customer_id),
                  f"Deleted empty account '{account.LastName}, {account.FirstName}'")
        logging.info("Account deleted.")
    except Exception as e:
        # A stay can be created between the check and the delete; the foreign key
        # then does its job and the operator gets a sentence, not a traceback.
        logging.error(f"Could not delete the account: {e}")


def link_stay_to_guest_account():
    """Front desk: attach a desk-made stay to a guest account.

    A reservation taken at the desk has no online account at the time, so
    Reservations.CustomerID is NULL: the stay's loyalty has nobody to credit and
    the Customer menu gate has nothing to recognise. Linking here closes that
    loop. Only UNLINKED live stays are offered -- link_reservation_customer()
    will not re-point a stay that already belongs to someone.
    """
    account = _pick_customer_account()
    if not account:
        return
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT RoomNumber, LastName, FirstName, CheckInDate, CheckOutDate "
                "FROM Reservations WHERE CustomerID IS NULL "
                "ORDER BY CheckInDate DESC, RoomNumber",
            )
            unlinked = cursor.fetchall()
    except Exception as e:
        logging.error(f"Could not list unlinked stays: {e}")
        return
    if not unlinked:
        logging.info("Every live stay is already linked to a guest account.")
        return
    ui.show_table(
        "Stays not yet linked to an account",
        ["#", "Room", "Guest", "Check In", "Check Out"],
        [(i + 1, r.RoomNumber, f"{r.LastName or ''}, {r.FirstName or ''}".strip(", "),
          r.CheckInDate, r.CheckOutDate) for i, r in enumerate(unlinked)],
    )
    raw = input("Enter the row number to link (blank to cancel): ").strip()
    if not raw.isdigit():
        return
    idx = int(raw)
    if not (1 <= idx <= len(unlinked)):
        logging.info("Invalid selection.")
        return
    stay = unlinked[idx - 1]
    guest_on_stay = f"{stay.LastName or ''}, {stay.FirstName or ''}".strip(", ")
    if input(
            f"Link room {stay.RoomNumber} ({guest_on_stay}) to "
            f"{account.FirstName} {account.LastName}? (y/n): "
    ).strip().lower() != 'y':
        logging.info("Not linked.")
        return
    if reservations.link_reservation_customer(stay.RoomNumber, int(account.CustomerID)):
        logging.info(f"Room {stay.RoomNumber} is now {account.FirstName} {account.LastName}'s stay.")
    else:
        logging.info(
            "The stay could not be linked -- it may have just been linked by someone "
            "else, or it already belongs to another guest. Check the reservation first."
        )


def customer_panel():
    """In-house guest menu. Entry requires a signed-in booking account.

    The gate is what makes "Sign Out" meaningful: nothing inside this menu sets
    CURRENT_CUSTOMER (every action identifies the guest through validate_room()), so
    without a login at entry there is no session for Sign Out to end.
    """
    if not customer_login(allow_register=False):
        logging.info(
            "A booking account is required to use the Customer menu. Returning to the "
            "main menu -- book a room first, or ask the front desk to create your account."
        )
        return
    # Resolved once here: nothing inside this panel changes the account, and Sign Out
    # exits the menu, so the label cannot go stale while it is shown.
    session = customer_session_label()
    while True:
        ui.pause()
        ui.show_menu("Customer Menu", [
            "1. Check In",
            "2. Place Order",
            "3. Check Out",
            "4. View Amenities",
            "5. Provide Feedback",
            "6. View Promotions",
            "7. Join Loyalty Program",
            "8. Track Order Status",
            "9. Contact Concierge",
            "10. View My Concierge Requests",
            "11. View My Loyalty Tier & Perks",
            "12. Print My Invoice",
            "13. View My History",
            "14. My Key Card",
            "15. My Notifications",
            "16. Sign Out",
        ], subtitle=session)
        cust_choice = input("Enter your choice: ").strip()

        if cust_choice == '1':
            reservations.check_in()
        elif cust_choice == '2':
            orders.order_item()
        elif cust_choice == '3':
            billing.check_out()
        elif cust_choice == '4':
            orders.view_amenities()
        elif cust_choice == '5':
            orders.provide_feedback()
        elif cust_choice == '6':
            orders.view_promotions()
        elif cust_choice == '7':
            if not core.get_loyalty_enabled():
                logging.info("Loyalty Program is not enabled. Contact administration to enable it.")
            else:
                room_number = input("Enter your room number to join/verify loyalty account: ").strip()
                if not room_number:
                    logging.info("Invalid room number.")
                else:
                    if loyalty.create_loyalty_account_if_missing(room_number):
                        points = loyalty.get_points_by_room(room_number)
                        logging.info(f"Loyalty account ready for room {room_number}. Current points: {points}.")
                    else:
                        logging.info("Failed to create or verify loyalty account. Please contact front desk.")
        elif cust_choice == '8':
            orders.track_order_status()
        elif cust_choice == '9':
            concierge.contact_concierge()
        elif cust_choice == '10':
            concierge.view_my_concierge_requests()
        elif cust_choice == '11':
            view_my_loyalty_status()
        elif cust_choice == '12':
            print_my_invoice()
        elif cust_choice == '13':
            my_history()
        elif cust_choice == '14':
            keycards.view_my_key_card()
        elif cust_choice == '15':
            notifications.view_notifications_for_room()
        elif cust_choice == '16':
            _mod_session.CURRENT_CUSTOMER = None  # Sign out: the next visitor must log in fresh
            logging.info("You have been signed out.")
            break
        ui.pause()
